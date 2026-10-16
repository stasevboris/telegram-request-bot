"""Client-facing menu, consent, and persisted application flow."""
from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from config import Settings
from content import (
    BOT_URL,
    CASES,
    CHANNEL_URL,
    CONTACT_URL,
    FAQ,
    PRIVACY_NOTICE,
    PROCESS,
    SERVICES,
    WELCOME,
    find_auto_answer,
)
from database import Database
from google_sheets import GoogleSheetsMirror
from keyboards import (
    auto_answer_keyboard,
    cancel_keyboard,
    case_keyboard,
    client_message_admin_keyboard,
    consent_keyboard,
    data_delete_keyboard,
    faq_keyboard,
    human_called_keyboard,
    intake_controls,
    main_menu,
    request_service_keyboard,
    service_keyboard,
    updates_keyboard,
)

log = logging.getLogger(__name__)
MAX_TEXT_LENGTH = 4000
ALLOWED_DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".csv",
    ".xls", ".xlsx", ".ppt", ".pptx", ".png", ".jpg", ".jpeg",
    ".py", ".ipynb", ".cs", ".cpp", ".c", ".h", ".java", ".sql", ".json",
    ".xml", ".html", ".css", ".js", ".ts", ".zip", ".rar", ".7z", ".tar", ".gz",
}


def _display_name(user: Any) -> str:
    return " ".join(part for part in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if part).strip()


def _privacy_text(settings: Settings) -> str:
    contact = f"@{settings.contact_username}" if settings.contact_username else "контакт не задан — настройте CONTACT_USERNAME перед запуском"
    return PRIVACY_NOTICE.format(contact=html.escape(contact))


def _service_title(key: str) -> str:
    if key == "other":
        return "Другая задача"
    return SERVICES.get(key, {}).get("title", "Другая задача")


def _escaped_preview(text: str, max_encoded_length: int) -> str:
    result: list[str] = []
    used = 0
    for char in text:
        encoded = html.escape(char)
        if used + len(encoded) > max_encoded_length:
            break
        result.append(encoded)
        used += len(encoded)
    preview = "".join(result)
    return preview + ("…" if len(preview) < len(text) else "")


def _lead_admin_text(lead: dict[str, Any]) -> str:
    title = _service_title(lead["service_key"])
    user_id = lead["telegram_id"]
    display_name = _escaped_preview(lead.get("display_name") or "не указано", 250)
    if lead.get("username"):
        client_link = f'@{lead["username"]} (<a href="https://t.me/{lead["username"]}">написать в ЛС</a>)'
    else:
        client_link = f'<a href="tg://user?id={user_id}">{display_name} (написать в ЛС)</a>'
    attachments = lead.get("attachments", [])
    description = _escaped_preview(lead.get("description") or "—", 1400)
    notes = _escaped_preview(lead.get("extra_notes") or "", 500)
    stack_safe = _escaped_preview(lead.get("stack") or "не указано", 300)
    deadline_safe = _escaped_preview(lead.get("deadline") or "не указан", 250)
    budget_safe = _escaped_preview(lead.get("expected_budget") or "не указан", 200)
    text = (
        f"<b>🚨 Новая заявка #{lead['id']}!</b>\n\n"
        f"👤 <b>Клиент:</b> {display_name}\n"
        f"💬 <b>Контакт:</b> {client_link}\n"
        f"🆔 <b>Telegram ID:</b> <code>{user_id}</code>\n\n"
        f"📌 <b>Услуга:</b> {html.escape(title)}\n"
        f"💻 <b>Стек/предмет:</b> {stack_safe}\n"
        f"⏳ <b>Дедлайн:</b> {deadline_safe}\n"
        f"💰 <b>Бюджет:</b> {budget_safe}\n\n"
        f"📝 <b>Описание:</b>\n{description}\n"
    )
    if notes:
        text += f"\n📎 <b>Дополнение:</b> {notes}\n"
    text += f"\n📂 <b>Вложений:</b> {len(attachments)}\n🕒 <b>Создана:</b> {html.escape(lead.get('created_at', ''))}"
    return text


async def _notify_admins(bot: Bot, db: Database, settings: Settings, lead: dict[str, Any]) -> bool:
    targets = [settings.admin_chat_id] if settings.admin_chat_id is not None else list(settings.admin_ids)
    notified = False
    for target in targets:
        try:
            await bot.send_message(
                target,
                _lead_admin_text(lead),
                reply_markup=_admin_keyboard(lead["id"], lead.get("username"), lead.get("telegram_id")),
            )
            notified = True
            # Copy client materials only to configured admin destinations. Message IDs
            # are saved so the bot can attempt cleanup if the client requests deletion.
            for item in lead.get("attachments", []):
                try:
                    copied = await bot.copy_message(
                        chat_id=target,
                        from_chat_id=lead["telegram_id"],
                        message_id=int(item["message_id"]),
                    )
                    await db.add_admin_copy(lead["id"], target, copied.message_id)
                except Exception:
                    log.exception("Could not copy attachment for lead %s to admin destination", lead["id"])
        except Exception:
            log.exception("Could not notify admin destination %s about lead %s", target, lead["id"])
    return notified


def _admin_keyboard(lead_id: int, username: str | None = None, user_id: int | None = None):
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    b = InlineKeyboardBuilder()
    b.button(text="✉️ Ответить через бота", callback_data=f"admin:reply:{lead_id}")
    if username:
        b.button(text="👤 Написать клиенту в ЛС", url=f"https://t.me/{username}")
    elif user_id:
        b.button(text="👤 Написать клиенту в ЛС", url=f"tg://user?id={user_id}")
    b.button(text="📂 Карточка заявки", callback_data=f"admin:lead:{lead_id}")
    b.adjust(1, 1, 1)
    return b.as_markup()


def create_client_router(db: Database, sheets: GoogleSheetsMirror, settings: Settings) -> Router:
    router = Router(name="client")

    async def accepted(user_id: int) -> bool:
        user = await db.get_user(user_id)
        return bool(user and user.get("consent_at"))

    async def prompt_consent(message: Message) -> None:
        await message.answer(
            _privacy_text(settings) + "\n\nНажмите «Понятно, продолжить», если ознакомились и хотите пользоваться ботом. До этого шага заявка не сохраняется.",
            reply_markup=consent_keyboard(),
        )

    async def show_home(message: Message, user_id: int) -> None:
        await db.touch_user(user_id, message.from_user.username, _display_name(message.from_user))
        await message.answer(WELCOME, reply_markup=main_menu(user_id in settings.admin_ids))

    async def send_lead_to_sheets(lead: dict[str, Any]) -> None:
        if not sheets.enabled:
            return
        try:
            if await sheets.sync_lead(lead):
                await db.mark_google_synced(lead["id"])
        except Exception:
            log.exception("Lead %s saved in SQLite but Google Sheets sync failed; it will be retried", lead["id"])

    async def finish_request(bot: Bot, message: Message, user_id: int) -> None:
        session = await db.get_session(user_id)
        if not session or session.get("state") != "collect_attachments":
            await message.answer("Не нашёл активную заявку. Начните заново через меню.", reply_markup=main_menu(user_id in settings.admin_ids))
            return
        data = session.get("data", {})
        if not (data.get("description") or "").strip():
            await message.answer("Добавьте короткое описание задачи, прежде чем отправить заявку.")
            return

        user = await db.get_user(user_id)
        if not user or not user.get("consent_at"):
            await db.clear_session(user_id)
            await prompt_consent(message)
            return

        if user_id not in settings.admin_ids:
            since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
            if await db.recent_lead_count(user_id, since) >= 5:
                await db.clear_session(user_id)
                await message.answer("За последние 24 часа уже отправлено 5 заявок — это защитное ограничение от спама. Если нужна поправка к уже отправленной заявке, ответьте владельцу в личном чате.", reply_markup=main_menu(False))
                return

        lead = await db.create_lead({
            "telegram_id": user_id,
            "username": user.get("username"),
            "display_name": user.get("display_name", ""),
            "service_key": data.get("service_key", "other"),
            "stack": data.get("stack", ""),
            "deadline": data.get("deadline", ""),
            "expected_budget": data.get("expected_budget", ""),
            "description": data.get("description", ""),
            "extra_notes": data.get("extra_notes", ""),
            "attachments": data.get("attachments", []),
        })
        await db.clear_session(user_id)
        await send_lead_to_sheets(lead)
        notified = await _notify_admins(bot, db, settings, lead)
        if notified:
            confirmation = f"Заявка <b>#{lead['id']}</b> сохранена и передана владельцу бота. Он свяжется с вами после ознакомления; срок ответа зависит от его доступности."
        else:
            confirmation = f"Заявка <b>#{lead['id']}</b> сохранена, но отправить уведомление владельцу сейчас не удалось. Попробуйте позже или используйте контакт из меню «Контакты»."
        await message.answer(
            confirmation + "\n\nУсловия и стоимость ещё не согласованы — дождитесь отдельного подтверждения.",
            reply_markup=main_menu(user_id in settings.admin_ids),
        )

    async def delete_user_data(bot: Bot, user_id: int) -> tuple[int, int, bool]:
        # Try to clean the external mirror before removing local identifiers needed
        # to locate the rows. A failure is reported honestly to the user.
        sheets_failed = False
        sheets_deleted = 0
        if sheets.enabled:
            try:
                sheets_deleted = await sheets.delete_user_rows(user_id)
            except Exception:
                sheets_failed = True
                log.exception("Could not delete Google Sheets rows for Telegram user %s", user_id)
        leads = await db.delete_user_data(user_id)
        # Best-effort cleanup of original attachment messages and admin copies.
        deleted_messages = 0
        for lead in leads:
            for item in lead.get("attachments", []):
                try:
                    await bot.delete_message(user_id, int(item["message_id"]))
                    deleted_messages += 1
                except Exception:
                    pass
            for item in lead.get("admin_copies", []):
                try:
                    await bot.delete_message(int(item["chat_id"]), int(item["message_id"]))
                    deleted_messages += 1
                except Exception:
                    pass
        await db.clear_session(user_id)
        return len(leads), deleted_messages + sheets_deleted, sheets_failed

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        if message.chat.type != "private":
            await message.answer("Для конфиденциальности откройте бота в личном чате и нажмите /start.")
            return
        user = await db.get_user(message.from_user.id)
        if user and user.get("consent_at"):
            await show_home(message, message.from_user.id)
        else:
            await prompt_consent(message)

    @router.message(Command("myid"))
    async def myid_command(message: Message) -> None:
        if message.chat.type != "private":
            await message.answer("Для защиты приватности отправьте /myid боту в личный чат.")
            return
        # This command does not persist the ID or transmit it to the owner.
        await message.answer(f"Ваш числовой Telegram ID: <code>{message.from_user.id}</code>\nИспользуйте его в ADMIN_IDS в файле .env.")

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "Используйте кнопки меню. Команды: /start — меню, /privacy — данные, /mydata — мои записи, /deletedata — запросить удаление, /subscribe — настройка отдельной подписки на новости, /unsubscribe — отписаться, /cancel — отменить текущую заявку."
        )

    @router.message(Command("privacy"))
    async def privacy_command(message: Message) -> None:
        if message.chat.type != "private":
            await message.answer("Для приватности откройте бота в личном чате и отправьте /privacy.")
            return
        await message.answer(_privacy_text(settings), reply_markup=consent_keyboard() if not await accepted(message.from_user.id) else main_menu(message.from_user.id in settings.admin_ids))

    @router.message(Command("mydata"))
    async def mydata_command(message: Message) -> None:
        if message.chat.type != "private":
            await message.answer("Экспорт данных можно запросить только в личном чате с ботом.")
            return
        user_id = message.from_user.id
        if not await accepted(user_id):
            await prompt_consent(message)
            return
        user = await db.get_user(user_id)
        leads = await db.user_leads(user_id)
        session = await db.get_session(user_id)
        draft = None
        if session:
            draft_data = session.get("data", {})
            draft = {
                "state": session.get("state"),
                "updated_at": session.get("updated_at"),
                "fields": {
                    key: draft_data.get(key)
                    for key in ("service_key", "stack", "deadline", "expected_budget", "description", "extra_notes")
                    if key in draft_data
                },
                "attachments": [
                    {key: item.get(key) for key in ("type", "file_name", "size", "message_id") if key in item}
                    for item in draft_data.get("attachments", [])
                ],
            }
        export = {
            "telegram_id": user_id,
            "username": user.get("username") if user else None,
            "display_name": user.get("display_name") if user else None,
            "consent_at": user.get("consent_at") if user else None,
            "last_seen_at": user.get("last_seen_at") if user else None,
            "marketing_updates_subscribed": await db.marketing_subscription_state(user_id),
            "active_draft": draft,
            "stored_leads": [
                {
                    "id": lead["id"], "created_at": lead["created_at"],
                    "service": _service_title(lead["service_key"]), "stack": lead["stack"],
                    "deadline": lead["deadline"], "expected_budget": lead["expected_budget"],
                    "description": lead["description"],
                    "extra_notes": lead["extra_notes"], "status": lead["status"],
                    "attachments": [
                        {key: item.get(key) for key in ("type", "file_name", "size", "message_id") if key in item}
                        for item in lead.get("attachments", [])
                    ],
                }
                for lead in leads
            ],
        }
        payload = json.dumps(export, ensure_ascii=False, indent=2).encode("utf-8")
        await message.answer_document(
            BufferedInputFile(payload, filename="my_telegram_bot_data.json"),
            caption="Экспорт данных, сохранённых этим ботом. Фактические файлы остаются в Telegram и не скачиваются на сервер.",
        )

    @router.message(Command("deletedata"))
    async def deletedata_command(message: Message) -> None:
        if message.chat.type != "private":
            await message.answer("Запрос удаления данных можно выполнить только в личном чате с ботом.")
            return
        if not await accepted(message.from_user.id):
            await message.answer("Сохранённых данных не найдено.")
            return
        await message.answer(
            "Удалить сохранённые ботом заявки, данные профиля и связанные записи Google Sheets? Бот также попробует удалить известные копии файлов, если Telegram API это позволяет. Уже скачанные или скопированные вне управляемых чатов файлы удалить удалённо нельзя.",
            reply_markup=data_delete_keyboard(),
        )

    @router.message(Command("subscribe"))
    async def subscribe_command(message: Message) -> None:
        if message.chat.type != "private":
            await message.answer("Подписаться можно только в личном чате с ботом.")
            return
        if not await accepted(message.from_user.id):
            await prompt_consent(message)
            return
        await message.answer(
            "Новости, обновления услуг и отдельные предложения отправляются только по вашей отдельной подписке. Нажмите кнопку ниже, если хотите её включить; подписка не обязательна и её можно отменить в любой момент.",
            reply_markup=updates_keyboard(await db.marketing_subscription_state(message.from_user.id)),
        )

    @router.message(Command("unsubscribe"))
    async def unsubscribe_command(message: Message) -> None:
        if message.chat.type != "private":
            await message.answer("Отписаться можно только в личном чате с ботом.")
            return
        await db.set_marketing_subscription(message.from_user.id, False)
        await message.answer("Рассылка отключена. Ответы по вашим заявкам это не затрагивает.", reply_markup=main_menu(message.from_user.id in settings.admin_ids))

    @router.message(Command("cancel"))
    async def cancel_command(message: Message) -> None:
        if message.chat.type != "private":
            await message.answer("Управляйте заявкой в личном чате с ботом.")
            return
        await db.clear_session(message.from_user.id)
        await message.answer("Текущий шаг отменён.", reply_markup=main_menu(message.from_user.id in settings.admin_ids))

    @router.callback_query(F.data == "consent:privacy")
    async def consent_privacy(callback: CallbackQuery) -> None:
        await callback.answer()
        await callback.message.answer(_privacy_text(settings), reply_markup=consent_keyboard())

    @router.callback_query(F.data == "consent:accept")
    async def consent_accept(callback: CallbackQuery) -> None:
        user = callback.from_user
        await db.accept_consent(user.id, user.username, _display_name(user))
        await callback.answer("Сохранено")
        await callback.message.answer(WELCOME, reply_markup=main_menu(user.id in settings.admin_ids))

    @router.callback_query(F.data == "privacy:delete:cancel")
    async def delete_cancel(callback: CallbackQuery) -> None:
        await callback.answer("Удаление отменено")
        await callback.message.answer("Данные оставлены без изменений.", reply_markup=main_menu(callback.from_user.id in settings.admin_ids))

    @router.callback_query(F.data == "privacy:delete:confirm")
    async def delete_confirm(callback: CallbackQuery, bot: Bot) -> None:
        count, cleanup_count, sheets_failed = await delete_user_data(bot, callback.from_user.id)
        await callback.answer("Запрос выполнен")
        sheets_note = " Важно: удалить записи из Google Sheets не удалось; свяжитесь с владельцем, чтобы он проверил таблицу." if sheets_failed else ""
        await callback.message.answer(
            f"Данные удалены из локальной базы бота; заявок удалено — {count}. Удалённых записей/сообщений — {cleanup_count}. Некоторые копии сообщений могут быть недоступны для удаления средствами Telegram.{sheets_note} Чтобы снова пользоваться ботом, отправьте /start.",
            reply_markup=None,
        )

    @router.callback_query(F.data == "menu:home")
    async def menu_home(callback: CallbackQuery) -> None:
        await callback.answer()
        if not await accepted(callback.from_user.id):
            await callback.message.answer("Для продолжения подтвердите ознакомление с уведомлением о данных.", reply_markup=consent_keyboard())
            return
        await db.touch_user(callback.from_user.id, callback.from_user.username, _display_name(callback.from_user))
        await callback.message.answer(WELCOME, reply_markup=main_menu(callback.from_user.id in settings.admin_ids))

    @router.callback_query(F.data == "menu:services")
    async def menu_services(callback: CallbackQuery) -> None:
        await callback.answer()
        if not await accepted(callback.from_user.id):
            await callback.message.answer("Сначала отправьте /start и подтвердите уведомление о данных.", reply_markup=consent_keyboard())
            return
        lines = ["<b>Услуги и ориентировочные цены</b>\n"]
        for service in SERVICES.values():
            lines.append(f"<b>{html.escape(service['title'])}</b> — {html.escape(service['price'])}\n{html.escape(service['description'])}\nСрок: {html.escape(service['time'])}")
        lines.append("\nОкончательные цена, срок и результат согласуются после ознакомления с задачей. Нажмите услугу ниже, чтобы оставить заявку.")
        await callback.message.answer("\n\n".join(lines), reply_markup=service_keyboard())

    @router.callback_query(F.data.startswith("service:"))
    async def service_detail(callback: CallbackQuery) -> None:
        await callback.answer()
        if not await accepted(callback.from_user.id):
            await callback.message.answer("Сначала отправьте /start и подтвердите уведомление о данных.", reply_markup=consent_keyboard())
            return
        key = callback.data.split(":", 1)[1]
        service = SERVICES.get(key)
        if not service:
            await callback.message.answer("Эта карточка услуги не найдена.")
            return
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        b = InlineKeyboardBuilder()
        b.button(text="📝 Оставить заявку на эту услугу", callback_data=f"request:choose:{key}")
        b.button(text="⬅️ К услугам", callback_data="menu:services")
        b.adjust(1)
        await callback.message.answer(
            f"<b>{html.escape(service['title'])}</b>\nЦена: {html.escape(service['price'])}\nСрок: {html.escape(service['time'])}\n\n{html.escape(service['description'])}",
            reply_markup=b.as_markup(),
        )

    @router.callback_query(F.data.startswith("menu:cases:"))
    async def menu_cases(callback: CallbackQuery) -> None:
        await callback.answer()
        if not await accepted(callback.from_user.id):
            await callback.message.answer("Сначала отправьте /start и подтвердите уведомление о данных.", reply_markup=consent_keyboard())
            return
        try:
            index = int(callback.data.rsplit(":", 1)[1]) % len(CASES)
        except (ValueError, ZeroDivisionError):
            index = 0
        case = CASES[index]
        await callback.message.answer(
            f"<b>Пример {index + 1} из {len(CASES)}: {html.escape(case['title'])}</b>\n"
            f"Стек: {html.escape(case['stack'])}\n\n{html.escape(case['summary'])}\n\n"
            "Описание сформировано по материалам рабочей папки; перед публикацией проверьте, что имеете право показывать проект и что детали точны.",
            reply_markup=case_keyboard(index),
        )

    @router.callback_query(F.data.startswith("menu:faq:"))
    async def menu_faq(callback: CallbackQuery) -> None:
        await callback.answer()
        if not await accepted(callback.from_user.id):
            await callback.message.answer("Сначала отправьте /start и подтвердите уведомление о данных.", reply_markup=consent_keyboard())
            return
        try:
            index = int(callback.data.rsplit(":", 1)[1]) % len(FAQ)
        except (ValueError, ZeroDivisionError):
            index = 0
        question, answer = FAQ[index]
        await callback.message.answer(f"<b>{html.escape(question)}</b>\n\n{html.escape(answer)}", reply_markup=faq_keyboard(index))

    @router.callback_query(F.data == "menu:privacy")
    async def menu_privacy(callback: CallbackQuery) -> None:
        await callback.answer()
        await callback.message.answer(_privacy_text(settings), reply_markup=main_menu(callback.from_user.id in settings.admin_ids))

    @router.callback_query(F.data == "menu:contact")
    async def menu_contact(callback: CallbackQuery) -> None:
        await callback.answer()
        text = (
            f"Личный контакт: <a href=\"{CONTACT_URL}\">@borislabs</a>\n"
            f"Канал: <a href=\"{CHANNEL_URL}\">@borislabs_channel</a>\n"
            f"Этот бот: <a href=\"{BOT_URL}\">@borislabs_bot</a>\n\n"
            "Заявку можно оставить здесь — бот попросит указать технологию, срок, бюджет, описание и подходящие материалы."
        )
        await callback.message.answer(text, reply_markup=main_menu(callback.from_user.id in settings.admin_ids))

    @router.callback_query(F.data == "menu:updates")
    async def menu_updates(callback: CallbackQuery) -> None:
        if callback.message.chat.type != "private":
            await callback.answer("Откройте бота в личном чате.", show_alert=True)
            return
        await callback.answer()
        if not await accepted(callback.from_user.id):
            await callback.message.answer("Сначала отправьте /start и подтвердите уведомление о данных.", reply_markup=consent_keyboard())
            return
        active = await db.marketing_subscription_state(callback.from_user.id)
        message_text = (
            "<b>Новости и предложения</b>\n\n"
            "По умолчанию подписка выключена. Если включите её, владелец сможет иногда отправлять вам новости сервиса и предложения. Это отдельно от обработки заявок и не обязательно для получения ответа. Отписаться можно в любой момент здесь или командой /unsubscribe."
        )
        await callback.message.answer(message_text, reply_markup=updates_keyboard(active))

    @router.callback_query(F.data == "updates:subscribe")
    async def updates_subscribe(callback: CallbackQuery) -> None:
        if callback.message.chat.type != "private":
            await callback.answer("Подписаться можно только в личном чате.", show_alert=True)
            return
        await callback.answer()
        if not await accepted(callback.from_user.id):
            await callback.message.answer("Сначала подтвердите уведомление о данных через /start.", reply_markup=consent_keyboard())
            return
        await db.set_marketing_subscription(callback.from_user.id, True)
        await callback.message.answer("Подписка включена. Отписаться можно кнопкой ниже или командой /unsubscribe.", reply_markup=updates_keyboard(True))

    @router.callback_query(F.data == "updates:unsubscribe")
    async def updates_unsubscribe(callback: CallbackQuery) -> None:
        if callback.message.chat.type != "private":
            await callback.answer("Управляйте подпиской в личном чате.", show_alert=True)
            return
        await db.set_marketing_subscription(callback.from_user.id, False)
        await callback.answer("Подписка отключена")
        await callback.message.answer("Рассылка отключена. Ответы по заявкам это не затрагивает.", reply_markup=updates_keyboard(False))

    @router.callback_query(F.data == "call_human")
    async def on_call_human(callback: CallbackQuery) -> None:
        await callback.answer()
        if not await accepted(callback.from_user.id):
            await callback.message.answer("Сначала отправьте /start и подтвердите ознакомление.", reply_markup=consent_keyboard())
            return
        await db.save_session(callback.from_user.id, "ask_human", {})
        await callback.message.answer(
            "✍️ <b>Напишите ваш вопрос или задачу:</b>\n\n"
            "Я попробую моментально подсказать ответ по базе знаний, либо сразу позову разработчика в чат!",
            reply_markup=cancel_keyboard(),
        )

    @router.callback_query(F.data == "force_human")
    async def on_force_human(callback: CallbackQuery, bot: Bot) -> None:
        await callback.answer("Зову разработчика...")
        user_id = callback.from_user.id
        display_name = _escaped_preview(_display_name(callback.from_user) or "Клиент", 100)
        if callback.from_user.username:
            direct_link = f'@{callback.from_user.username} (<a href="https://t.me/{callback.from_user.username}">написать в ЛС</a>)'
        else:
            direct_link = f'<a href="tg://user?id={user_id}">{display_name} (написать в ЛС)</a>'

        user_leads = await db.user_leads(user_id)
        latest_lead_id = user_leads[-1]["id"] if user_leads else None
        lead_info = f" по заявке #{latest_lead_id}" if latest_lead_id else " (без заявки)"

        targets = [settings.admin_chat_id] if settings.admin_chat_id is not None else list(settings.admin_ids)
        admin_notice = (
            f"🚨 <b>Клиент нажал «Позвать человека»{lead_info}!</b>\n\n"
            f"👤 <b>Клиент:</b> {display_name}\n"
            f"🔗 <b>Контакт:</b> {direct_link}\n"
            f"🆔 <b>Telegram ID:</b> <code>{user_id}</code>\n\n"
            f"<i>Клиент ждёт ответа оператора. Нажмите «Ответить через бота» или напишите в ЛС:</i>"
        )
        for target in targets:
            try:
                await bot.send_message(
                    target,
                    admin_notice,
                    reply_markup=client_message_admin_keyboard(latest_lead_id, callback.from_user.username, user_id) if latest_lead_id else None,
                )
            except Exception:
                pass

        await callback.message.answer(
            "👨‍💻 <b>Разработчик оповещен!</b>\n\n"
            "Борис (@borislabs) уже получил уведомление и скоро подключится к диалогу.\n\n"
            "💬 Вы также можете написать ему напрямую в ЛС: @borislabs",
            reply_markup=human_called_keyboard(),
        )

    @router.callback_query(F.data == "menu:process")
    async def menu_process(callback: CallbackQuery) -> None:
        await callback.answer()
        await callback.message.answer(PROCESS, reply_markup=main_menu(callback.from_user.id in settings.admin_ids))

    @router.callback_query(F.data == "request:start")
    async def request_start(callback: CallbackQuery) -> None:
        await callback.answer()
        if not await accepted(callback.from_user.id):
            await callback.message.answer("Сначала подтвердите уведомление о данных через /start.", reply_markup=consent_keyboard())
            return
        await db.save_session(callback.from_user.id, "collect_service", {"attachments": []})
        await callback.message.answer("Выберите тип задачи или направление в меню ниже 👇", reply_markup=request_service_keyboard())

    @router.callback_query(F.data.startswith("request:choose:"))
    async def request_choose_service(callback: CallbackQuery) -> None:
        await callback.answer()
        if not await accepted(callback.from_user.id):
            await callback.message.answer("Сначала подтвердите уведомление о данных через /start.", reply_markup=consent_keyboard())
            return
        key = callback.data.rsplit(":", 1)[1]
        if key not in SERVICES:
            await callback.message.answer("Услуга не найдена. Выберите её из списка.")
            return
        await db.save_session(callback.from_user.id, "collect_stack", {"service_key": key, "attachments": []})
        await callback.message.answer("Укажите предмет, дисциплину или стек технологий (например: Python, C#, Базы данных, Вышмат, Информатика):", reply_markup=cancel_keyboard())

    @router.callback_query(F.data.startswith("request:service:"))
    async def request_service(callback: CallbackQuery) -> None:
        await callback.answer()
        if not await accepted(callback.from_user.id):
            await callback.message.answer("Сначала подтвердите уведомление о данных через /start.", reply_markup=consent_keyboard())
            return
        key = callback.data.rsplit(":", 1)[1]
        if key != "other" and key not in SERVICES:
            key = "other"
        await db.save_session(callback.from_user.id, "collect_stack", {"service_key": key, "attachments": []})
        await callback.message.answer("Укажите предмет, дисциплину или стек технологий (например: Python, C#, Базы данных, Вышмат, Информатика):", reply_markup=cancel_keyboard())

    @router.callback_query(F.data == "request:cancel")
    async def request_cancel(callback: CallbackQuery) -> None:
        await db.clear_session(callback.from_user.id)
        await callback.answer("Заявка отменена")
        await callback.message.answer("Черновик заявки удалён.", reply_markup=main_menu(callback.from_user.id in settings.admin_ids))

    @router.callback_query(F.data == "request:finish")
    async def request_finish(callback: CallbackQuery, bot: Bot) -> None:
        await callback.answer()
        await finish_request(bot, callback.message, callback.from_user.id)

    @router.message()
    async def client_message(message: Message, bot: Bot) -> None:
        user_id = message.from_user.id
        if message.chat.type != "private":
            await message.answer("Пришлите заявку боту в личный чат, чтобы не публиковать материалы в группе.")
            return
        user = await db.get_user(user_id)
        if not user or not user.get("consent_at"):
            await prompt_consent(message)
            return
        await db.touch_user(user_id, message.from_user.username, _display_name(message.from_user))

        session = await db.get_session(user_id)
        if session and session.get("state") == "admin_reply" and user_id in settings.admin_ids:
            data = session.get("data", {})
            lead = await db.get_lead(int(data.get("lead_id", 0)))
            if not lead:
                await db.clear_session(user_id)
                await message.answer("Заявка не найдена; режим ответа завершён.")
                return
            text = (message.text or message.caption or "").strip()
            if not text:
                await message.answer("Ответьте текстом, пожалуйста. Для отмены отправьте /cancel.")
                return
            try:
                safe_text = _escaped_preview(text, 3500)
                await bot.send_message(
                    lead["telegram_id"],
                    f"<b>💬 Сообщение от разработчика по заявке #{lead['id']}:</b>\n\n"
                    f"{safe_text}\n\n"
                    f"<i>Вы можете ответить прямо в этот чат или написать напрямую в ЛС: @borislabs</i>",
                )
                await db.clear_session(user_id)
                await message.answer(f"✅ Ответ по заявке #{lead['id']} успешно отправлен клиенту!")
            except Exception:
                log.exception("Failed to reply to user for lead %s", lead["id"])
                await message.answer("Не удалось отправить сообщение. Возможно, клиент заблокировал бота.")
            return

        state = session.get("state") if session else None
        data = session.get("data", {}) if session else {}
        text = (message.text or message.caption or "").strip()

        if state == "ask_human" or not session:
            if state == "ask_human":
                await db.clear_session(user_id)

            if user_id in settings.admin_ids and not session:
                await message.answer(
                    "⚙️ Вы авторизованы как администратор.\n\n"
                    "Чтобы ответить клиенту по заявке, используйте команду:\n"
                    "<code>/reply НОМЕР текст ответа</code>\n\n"
                    "Или откройте список заявок: /leads",
                    reply_markup=main_menu(True),
                )
                return

            user_leads = await db.user_leads(user_id)
            latest_lead_id = user_leads[-1]["id"] if user_leads else None
            targets = [settings.admin_chat_id] if settings.admin_chat_id is not None else list(settings.admin_ids)
            display_name = _escaped_preview(_display_name(message.from_user) or "Клиент", 100)

            if message.from_user.username:
                direct_link = f'@{message.from_user.username} (<a href="https://t.me/{message.from_user.username}">написать в ЛС</a>)'
            else:
                direct_link = f'<a href="tg://user?id={user_id}">{display_name} (написать в ЛС)</a>'

            # 1. Сначала алгоритм проверяет, сможет ли он ответить из базы знаний:
            auto_ans = find_auto_answer(text) if text else None
            if auto_ans:
                topic, answer = auto_ans
                await message.answer(answer, reply_markup=auto_answer_keyboard())

                lead_tag = f" по заявке #{latest_lead_id}" if latest_lead_id else ""
                admin_notify = (
                    f"🤖 <b>Автоответчик сработал{lead_tag}:</b>\n\n"
                    f"👤 <b>Клиент:</b> {display_name} ({direct_link})\n"
                    f"❓ <b>Вопрос:</b> «{_escaped_preview(text, 500)}»\n"
                    f"💡 <b>Тема:</b> {topic}\n\n"
                    f"<i>(Клиент получил автоответ с кнопкой «Всё равно позвать человека»)</i>"
                )
                for target in targets:
                    try:
                        await bot.send_message(
                            target,
                            admin_notify,
                            reply_markup=client_message_admin_keyboard(latest_lead_id, message.from_user.username, user_id) if latest_lead_id else None,
                        )
                    except Exception:
                        pass
                return

            # 2. База знаний не подошла (специфический вопрос, файлы или детали) -> Вопрос РЕАЛЬНО уходит человеку!
            if user_leads:
                latest_lead = user_leads[-1]
                admin_text = (
                    f"🚨 <b>Вопрос от клиента по заявке #{latest_lead['id']} (требуется ответ человека)!</b>\n\n"
                    f"👤 <b>Клиент:</b> {display_name}\n"
                    f"🔗 <b>Контакт:</b> {direct_link}\n"
                    f"📌 <b>Услуга:</b> {html.escape(_service_title(latest_lead['service_key']))}\n\n"
                )
                if text:
                    admin_text += f"<b>Текст:</b>\n{_escaped_preview(text, 3000)}\n\n"
                admin_text += "<i>Нажмите кнопку ниже, чтобы ответить через бота, или напишите в ЛС:</i>"

                for target in targets:
                    try:
                        await bot.send_message(
                            target,
                            admin_text,
                            reply_markup=client_message_admin_keyboard(latest_lead["id"], message.from_user.username, user_id),
                        )
                        if message.document or message.photo:
                            copied = await bot.copy_message(chat_id=target, from_chat_id=user_id, message_id=message.message_id)
                            await db.add_admin_copy(latest_lead["id"], target, copied.message_id)
                    except Exception:
                        log.exception("Could not forward client message to admin %s", target)

                await message.answer(
                    "👨‍💻 <b>Ваш вопрос передан разработчику!</b>\n\n"
                    "Борис (@borislabs) уже получил уведомление и скоро ответит вам здесь в чате.\n\n"
                    "💬 Также вы можете написать ему напрямую в ЛС: @borislabs",
                    reply_markup=human_called_keyboard(),
                )
                return
            else:
                admin_text = (
                    f"🚨 <b>Специфический вопрос от пользователя (требуется человек):</b>\n\n"
                    f"👤 <b>Пользователь:</b> {direct_link}\n"
                    f"🆔 <b>Telegram ID:</b> <code>{user_id}</code>\n\n"
                )
                if text:
                    admin_text += f"<b>Текст:</b>\n{_escaped_preview(text, 3000)}"

                for target in targets:
                    try:
                        await bot.send_message(target, admin_text)
                        if message.document or message.photo:
                            await bot.copy_message(chat_id=target, from_chat_id=user_id, message_id=message.message_id)
                    except Exception:
                        pass

                await message.answer(
                    "👨‍💻 <b>Ваш вопрос передан разработчику!</b>\n\n"
                    "Борис (@borislabs) уже получил уведомление и скоро ответит вам здесь.\n\n"
                    "Вы также можете написать напрямую в ЛС: @borislabs или оформить задачу через кнопку «📝 Оставить заявку» ниже 👇",
                    reply_markup=main_menu(False),
                )
                return

        if state == "collect_stack":
            if not text:
                await message.answer("Отправьте ответ текстом, пожалуйста.")
                return
            data["stack"] = text[:300]
            await db.save_session(user_id, "collect_deadline", data)
            await message.answer("Какой желаемый срок? Укажите дату/время или «гибко / пока не знаю».", reply_markup=cancel_keyboard())
            return

        if state == "collect_deadline":
            if not text:
                await message.answer("Укажите срок текстом или напишите «гибко / без спешки».")
                return
            data["deadline"] = text[:200]
            await db.save_session(user_id, "collect_budget", data)
            await message.answer("Какую стоимость ожидаете / на какой бюджет рассчитываете? (напишите сумму в рублях или «на вашу оценку»):", reply_markup=cancel_keyboard())
            return

        if state == "collect_budget":
            if not text:
                await message.answer("Укажите комфортную сумму или напишите «на вашу оценку».")
                return
            data["expected_budget"] = text[:120]
            await db.save_session(user_id, "collect_description", data)
            await message.answer("Опишите задание подробнее: вариант, тему, особые требования (например, к антиплагиату или оформлению).\n\nНа следующем шаге вы сможете прикрепить файлы (методичку, задание, текст или архив):", reply_markup=cancel_keyboard())
            return

        if state == "collect_description":
            if not text:
                await message.answer("Нужно короткое описание задачи текстом, чтобы я мог передать заявку владельцу.")
                return
            if len(text) > MAX_TEXT_LENGTH:
                await message.answer(f"Описание слишком длинное. Ограничение — {MAX_TEXT_LENGTH} символов; разбейте требования на краткое описание и файл.")
                return
            data["description"] = text
            data["attachments"] = []
            await db.save_session(user_id, "collect_attachments", data)
            await message.answer(
                f"Прикрепите все файлы по заданию (методичку, задание, текст, архив .zip/.rar, скриншоты или фото) — до {settings.max_attachments} файлов.\n\nКогда закончите — нажмите «✅ Готово, отправить заявку».",
                reply_markup=intake_controls(),
            )
            return

        if state == "collect_attachments":
            attachment = None
            size = 0
            if message.document:
                extension = Path(message.document.file_name or "").suffix.lower()
                if extension not in ALLOWED_DOCUMENT_EXTENSIONS:
                    await message.answer(
                        "Этот формат файла не поддерживается. Разрешены документы (Word, PDF, Excel), архивы (.zip, .rar, .7z), исходный код и изображения.",
                        reply_markup=intake_controls(),
                    )
                    return
                attachment = {
                    "type": "document", "message_id": message.message_id,
                    "file_id": message.document.file_id,
                    "file_unique_id": message.document.file_unique_id,
                    "file_name": message.document.file_name or "document",
                }
                size = message.document.file_size or 0
            elif message.photo:
                photo = message.photo[-1]
                attachment = {
                    "type": "photo", "message_id": message.message_id,
                    "file_id": photo.file_id, "file_unique_id": photo.file_unique_id,
                    "file_name": "photo",
                }
                size = photo.file_size or 0
            elif text:
                if text.lower() in {"готово", "готово отправить заявку", "пропустить"}:
                    await finish_request(bot, message, user_id)
                    return
                data["extra_notes"] = (data.get("extra_notes", "") + "\n" + text).strip()[:MAX_TEXT_LENGTH]
                await db.save_session(user_id, "collect_attachments", data)
                await message.answer("Добавил комментарий. Прикрепите материалы или нажмите «Готово».", reply_markup=intake_controls())
                return
            else:
                await message.answer("Поддерживаются документы и изображения. Нажмите «Готово», чтобы отправить заявку.", reply_markup=intake_controls())
                return

            attachments = data.get("attachments", [])
            if size and size > settings.max_attachment_bytes:
                await message.answer(f"Файл слишком большой: лимит {settings.max_attachment_bytes // (1024*1024)} МБ на вложение.", reply_markup=intake_controls())
                return
            if len(attachments) >= settings.max_attachments:
                await message.answer(f"Достигнут лимит вложений ({settings.max_attachments}). Нажмите «Готово» или отмените заявку.", reply_markup=intake_controls())
                return
            attachment["size"] = size
            attachments.append(attachment)
            data["attachments"] = attachments
            await db.save_session(user_id, "collect_attachments", data)
            await message.answer(f"Материал добавлен ({len(attachments)}/{settings.max_attachments}). Можно добавить ещё или нажать «Готово».", reply_markup=intake_controls())
            return

        await db.clear_session(user_id)
        await message.answer("Состояние заявки сбросилось. Начните заново через меню.", reply_markup=main_menu(user_id in settings.admin_ids))

    return router
