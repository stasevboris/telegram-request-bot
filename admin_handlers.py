"""Admin-only commands and inline lead-management panel."""
from __future__ import annotations

import asyncio
import csv
import html
import io
import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from config import Settings
from content import SERVICES
from database import Database, STATUSES
from google_sheets import GoogleSheetsMirror
from keyboards import (
    admin_home_keyboard,
    broadcast_confirmation_keyboard,
    lead_actions_keyboard,
    lead_list_keyboard,
)

log = logging.getLogger(__name__)


def _service_name(key: str) -> str:
    return SERVICES.get(key, {}).get("title", "Другая задача")


def _lead_text(lead: dict[str, Any]) -> str:
    user_id = lead["telegram_id"]
    display_name = _escaped_preview(lead.get("display_name") or "не указано", 250)
    if lead.get("username"):
        client_link = f'@{lead["username"]} (<a href="https://t.me/{lead["username"]}">написать в ЛС</a>)'
    else:
        client_link = f'<a href="tg://user?id={user_id}">{display_name} (написать в ЛС)</a>'
    stack_safe = _escaped_preview(lead.get("stack") or "не указано", 300)
    deadline_safe = _escaped_preview(lead.get("deadline") or "не указан", 250)
    budget_safe = _escaped_preview(lead.get("expected_budget") or "не указан", 200)
    return (
        f"<b>Заявка #{lead['id']}</b>\n"
        f"Создана: {html.escape(lead.get('created_at', ''))}\n"
        f"Статус: {html.escape(STATUSES.get(lead.get('status'), lead.get('status', '')))}\n\n"
        f"👤 <b>Клиент:</b> {display_name}\n"
        f"💬 <b>Контакт:</b> {client_link}\n"
        f"🆔 <b>Telegram ID:</b> <code>{user_id}</code>\n\n"
        f"📌 <b>Услуга:</b> {html.escape(_service_name(lead['service_key']))}\n"
        f"💻 <b>Стек/предмет:</b> {stack_safe}\n"
        f"⏳ <b>Срок:</b> {deadline_safe}\n"
        f"💰 <b>Бюджет:</b> {budget_safe}\n"
        f"Описание: {len(lead.get('description') or '')} символов; полный текст ниже.\n"
        f"Дополнение: {len(lead.get('extra_notes') or '')} символов.\n"
        f"Вложений: {len(lead.get('attachments', []))}\n"
        f"Google Sheets: {'синхронизировано' if lead.get('google_synced_at') else 'ожидает/не настроено'}"
    )


def _escaped_preview(text: str, max_encoded_length: int) -> str:
    """Limit post-HTML-escape size to stay safely within Telegram's message limit."""
    out: list[str] = []
    used = 0
    for char in text:
        encoded = html.escape(char)
        if used + len(encoded) > max_encoded_length:
            break
        out.append(encoded)
        used += len(encoded)
    result = "".join(out)
    return result + ("…" if len(result) < len(text) else "")


def _is_admin(user_id: int, settings: Settings) -> bool:
    return user_id in settings.admin_ids


def _is_admin_context(user_id: int, chat_id: int, settings: Settings) -> bool:
    """Only expose client data in an admin's private chat or the configured admin group."""
    is_allowed_chat = chat_id == user_id or (
        settings.admin_chat_id is not None and chat_id == settings.admin_chat_id
    )
    return _is_admin(user_id, settings) and is_allowed_chat


def create_admin_router(db: Database, sheets: GoogleSheetsMirror, settings: Settings) -> Router:
    router = Router(name="admin")

    async def show_leads(message: Message, status: str | None = None) -> None:
        leads = await db.list_leads(limit=25, status=status)
        if not leads:
            await message.answer("Заявок пока нет.", reply_markup=admin_home_keyboard())
            return
        await message.answer("<b>Последние заявки</b> — нажмите строку, чтобы открыть карточку:", reply_markup=lead_list_keyboard(leads))

    async def render_lead(message: Message, lead_id: int) -> None:
        lead = await db.get_lead(lead_id)
        if not lead:
            await message.answer(f"Заявка #{lead_id} не найдена.")
            return
        await message.answer(
            _lead_text(lead),
            reply_markup=lead_actions_keyboard(lead_id, lead.get("username"), lead.get("telegram_id"))
        )
        for label, value in (("Описание", lead.get("description", "")), ("Дополнение", lead.get("extra_notes", ""))):
            if not value:
                continue
            # Chunk before HTML escaping; even hostile text made entirely of markup
            # characters stays comfortably below Telegram's 4096-character limit.
            chunks = [value[i:i + 500] for i in range(0, len(value), 500)]
            for part, chunk in enumerate(chunks, start=1):
                await message.answer(f"<b>{label} ({part}/{len(chunks)})</b>\n{html.escape(chunk)}")

    async def sync_one(lead_id: int) -> str:
        if not sheets.enabled:
            return "Google Sheets не настроен; заявка сохранена в SQLite."
        lead = await db.get_lead(lead_id)
        if not lead:
            return "Заявка не найдена."
        try:
            if await sheets.sync_lead(lead):
                await db.mark_google_synced(lead_id)
                return "Синхронизировано с Google Sheets."
        except Exception:
            log.exception("Google Sheets sync failed for lead %s", lead_id)
            return "Синхронизация не удалась; запись останется в очереди и будет повторена автоматически."
        return "Синхронизация не выполнена."

    @router.message(Command("admin"))
    async def admin_command(message: Message) -> None:
        if not _is_admin_context(message.from_user.id, message.chat.id, settings):
            return
        await message.answer("<b>Панель управления заявками</b>", reply_markup=admin_home_keyboard())

    @router.message(Command("leads"))
    async def leads_command(message: Message, command: CommandObject) -> None:
        if not _is_admin_context(message.from_user.id, message.chat.id, settings):
            return
        status = (command.args or "").strip() or None
        if status and status not in STATUSES:
            valid = ", ".join(f"<code>{key}</code>" for key in STATUSES)
            await message.answer(f"Неизвестный статус. Допустимые: {valid}")
            return
        await show_leads(message, status)

    @router.message(Command("lead"))
    async def lead_command(message: Message, command: CommandObject) -> None:
        if not _is_admin_context(message.from_user.id, message.chat.id, settings):
            return
        try:
            lead_id = int((command.args or "").strip())
        except ValueError:
            await message.answer("Формат: <code>/lead НОМЕР</code>")
            return
        await render_lead(message, lead_id)

    @router.message(Command("status"))
    async def status_command(message: Message, command: CommandObject) -> None:
        if not _is_admin_context(message.from_user.id, message.chat.id, settings):
            return
        parts = (command.args or "").split()
        if len(parts) != 2 or not parts[0].isdigit() or parts[1] not in STATUSES:
            status_help = "\n".join(f"<code>{key}</code> — {html.escape(label)}" for key, label in STATUSES.items())
            await message.answer(f"Формат: <code>/status НОМЕР СТАТУС</code>\nСтатусы:\n{status_help}")
            return
        lead_id, status = int(parts[0]), parts[1]
        if not await db.update_status(lead_id, status, message.from_user.id):
            await message.answer("Заявка не найдена.")
            return
        sync_result = await sync_one(lead_id)
        await message.answer(f"Статус изменён на «{html.escape(STATUSES[status])}». {html.escape(sync_result)}")

    @router.message(Command("reply"))
    async def reply_command(message: Message, command: CommandObject) -> None:
        if not _is_admin_context(message.from_user.id, message.chat.id, settings):
            return
        args = (command.args or "").strip().split(maxsplit=1)
        if len(args) != 2 or not args[0].isdigit():
            await message.answer("Формат: <code>/reply НОМЕР текст ответа</code>")
            return
        lead_id, text = int(args[0]), args[1]
        lead = await db.get_lead(lead_id)
        if not lead:
            await message.answer("Заявка не найдена.")
            return
        try:
            safe_text = _escaped_preview(text, 3500)
            await message.bot.send_message(
                lead["telegram_id"],
                f"<b>💬 Сообщение от разработчика по заявке #{lead_id}:</b>\n\n"
                f"{safe_text}\n\n"
                f"<i>Вы можете ответить прямо в этот чат или написать напрямую в ЛС: @borislabs</i>"
            )
            await message.answer("Ответ отправлен клиенту.")
        except Exception:
            log.exception("Failed to reply for lead %s", lead_id)
            await message.answer("Не удалось отправить ответ. Возможно, клиент заблокировал бота.")

    @router.message(Command("stats"))
    async def stats_command(message: Message) -> None:
        if not _is_admin_context(message.from_user.id, message.chat.id, settings):
            return
        await send_stats(message, db, sheets.enabled)

    @router.message(Command("export"))
    async def export_command(message: Message) -> None:
        if not _is_admin_context(message.from_user.id, message.chat.id, settings):
            return
        await send_export(message, db)

    @router.message(Command("sync_pending"))
    async def sync_pending_command(message: Message) -> None:
        if not _is_admin_context(message.from_user.id, message.chat.id, settings):
            return
        await run_sync_pending(message, db, sheets)

    @router.message(Command("broadcast"))
    async def broadcast_command(message: Message, command: CommandObject) -> None:
        if not _is_admin_context(message.from_user.id, message.chat.id, settings):
            return
        text = (command.args or "").strip()
        if not text:
            await message.answer(
                "Формат: <code>/broadcast текст сообщения</code>\n"
                "Будет создан предпросмотр. Рассылка отправляется только пользователям, которые отдельно подписались через меню «Новости и подписка»."
            )
            return
        if len(text) > 3500:
            await message.answer("Текст слишком длинный. Максимум — 3500 символов.")
            return
        recipients = await db.marketing_recipients()
        if not recipients:
            await message.answer("Нет активных подписчиков. Рассылку отправить некому.")
            return
        draft_id = await db.create_broadcast_draft(message.from_user.id, text)
        await message.answer(
            f"<b>Предпросмотр рассылки</b>\n\n{_escaped_preview(text, 3000)}\n\n"
            f"Получателей с активным согласием: <b>{len(recipients)}</b>.\n"
            "Нажмите «Отправить» только после проверки текста.",
            reply_markup=broadcast_confirmation_keyboard(draft_id),
        )

    @router.callback_query(F.data == "admin:broadcast:help")
    async def broadcast_help(callback: CallbackQuery) -> None:
        if not _is_admin_context(callback.from_user.id, callback.message.chat.id, settings):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(
            "Создайте проверяемый предпросмотр командой:\n<code>/broadcast Текст сообщения</code>\n\n"
            "Рассылка попадёт только тем пользователям, которые отдельно включили «Новости и подписка». Предварительный просмотр требует ручного подтверждения."
        )

    @router.callback_query(F.data.startswith("admin:broadcast:cancel:"))
    async def broadcast_cancel(callback: CallbackQuery) -> None:
        if not _is_admin_context(callback.from_user.id, callback.message.chat.id, settings):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        try:
            draft_id = int(callback.data.rsplit(":", 1)[1])
        except ValueError:
            await callback.answer("Черновик не найден", show_alert=True)
            return
        await db.delete_broadcast_draft(draft_id, callback.from_user.id)
        await callback.answer("Рассылка отменена")
        await callback.message.answer("Черновик рассылки удалён; сообщения не отправлялись.")

    @router.callback_query(F.data.startswith("admin:broadcast:send:"))
    async def broadcast_send(callback: CallbackQuery, bot: Bot) -> None:
        if not _is_admin_context(callback.from_user.id, callback.message.chat.id, settings):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        try:
            draft_id = int(callback.data.rsplit(":", 1)[1])
        except ValueError:
            await callback.answer("Черновик не найден", show_alert=True)
            return
        draft = await db.consume_broadcast_draft(draft_id, callback.from_user.id)
        if not draft:
            await callback.answer("Черновик уже использован или не найден", show_alert=True)
            return
        await callback.answer("Начинаю отправку")
        await callback.message.answer("Рассылка запущена. Я отправляю сообщения с ограниченной скоростью и отмечаю пользователей, которые заблокировали бота.")
        recipients = await db.marketing_recipients()
        sent = 0
        blocked = 0
        failed = 0
        safe_text = _escaped_preview(draft["message_text"], 3500)
        for telegram_id in recipients:
            try:
                await bot.send_message(telegram_id, safe_text)
                sent += 1
            except TelegramForbiddenError:
                blocked += 1
                await db.deactivate_marketing_subscription(telegram_id)
            except TelegramRetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 0.5)
                try:
                    await bot.send_message(telegram_id, safe_text)
                    sent += 1
                except TelegramForbiddenError:
                    blocked += 1
                    await db.deactivate_marketing_subscription(telegram_id)
                except Exception:
                    failed += 1
                    log.exception("Broadcast retry failed for subscriber %s", telegram_id)
            except Exception:
                failed += 1
                log.exception("Broadcast failed for subscriber %s", telegram_id)
            await asyncio.sleep(0.05)
        await callback.message.answer(
            f"Рассылка завершена. Успешно: {sent}; подписка отключена для заблокировавших бота: {blocked}; прочие ошибки: {failed}."
        )

    @router.callback_query(F.data == "admin:home")
    async def admin_home(callback: CallbackQuery) -> None:
        if not _is_admin_context(callback.from_user.id, callback.message.chat.id, settings):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        await callback.answer()
        await callback.message.answer("<b>Панель управления заявками</b>", reply_markup=admin_home_keyboard())

    @router.callback_query(F.data == "admin:leads")
    async def admin_leads(callback: CallbackQuery) -> None:
        if not _is_admin_context(callback.from_user.id, callback.message.chat.id, settings):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        await callback.answer()
        await show_leads(callback.message)

    @router.callback_query(F.data.startswith("admin:lead:"))
    async def admin_lead(callback: CallbackQuery) -> None:
        if not _is_admin_context(callback.from_user.id, callback.message.chat.id, settings):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        await callback.answer()
        try:
            lead_id = int(callback.data.rsplit(":", 1)[1])
        except ValueError:
            await callback.message.answer("Неверный номер заявки.")
            return
        await render_lead(callback.message, lead_id)

    @router.callback_query(F.data.startswith("admin:set:"))
    async def admin_set_status(callback: CallbackQuery) -> None:
        if not _is_admin_context(callback.from_user.id, callback.message.chat.id, settings):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        parts = callback.data.split(":")
        if len(parts) != 4:
            await callback.answer("Неверная команда", show_alert=True)
            return
        _, _, raw_id, status = parts
        if not raw_id.isdigit() or status not in STATUSES:
            await callback.answer("Неверная заявка или статус", show_alert=True)
            return
        lead_id = int(raw_id)
        if not await db.update_status(lead_id, status, callback.from_user.id):
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        result = await sync_one(lead_id)
        await callback.answer(f"Статус: {STATUSES[status]}")
        await callback.message.answer(f"Заявка #{lead_id}. {html.escape(result)}")
        await render_lead(callback.message, lead_id)

    @router.callback_query(F.data.startswith("admin:reply:"))
    async def admin_reply_start(callback: CallbackQuery, bot: Bot) -> None:
        if not _is_admin_context(callback.from_user.id, callback.message.chat.id, settings):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        try:
            lead_id = int(callback.data.rsplit(":", 1)[1])
        except ValueError:
            await callback.answer("Неверный номер", show_alert=True)
            return
        if not await db.get_lead(lead_id):
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        await db.save_session(callback.from_user.id, "admin_reply", {"lead_id": lead_id})
        try:
            await bot.send_message(callback.from_user.id, f"Введите текст для клиента по заявке #{lead_id}. Для отмены используйте /cancel.")
            await callback.answer("Запрос на ответ отправлен в личный чат с ботом")
        except Exception:
            await db.clear_session(callback.from_user.id)
            await callback.answer("Сначала откройте бота в личном чате и нажмите /start.", show_alert=True)

    @router.callback_query(F.data.startswith("admin:attachments:"))
    async def admin_attachments(callback: CallbackQuery, bot: Bot) -> None:
        if not _is_admin_context(callback.from_user.id, callback.message.chat.id, settings):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        try:
            lead_id = int(callback.data.rsplit(":", 1)[1])
        except ValueError:
            await callback.answer("Неверный номер", show_alert=True)
            return
        lead = await db.get_lead(lead_id)
        if not lead:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        await callback.answer()
        if not lead.get("attachments"):
            await callback.message.answer("Вложений у заявки нет.")
            return
        for item in lead["attachments"]:
            try:
                copied = await bot.copy_message(
                    chat_id=callback.message.chat.id,
                    from_chat_id=lead["telegram_id"],
                    message_id=int(item["message_id"]),
                )
                await db.add_admin_copy(lead_id, callback.message.chat.id, copied.message_id)
            except Exception:
                log.exception("Failed to copy stored attachment for lead %s", lead_id)
                await callback.message.answer("Не удалось скопировать одно из вложений; возможно, исходное сообщение удалено.")

    @router.callback_query(F.data == "admin:stats")
    async def admin_stats(callback: CallbackQuery) -> None:
        if not _is_admin_context(callback.from_user.id, callback.message.chat.id, settings):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        await callback.answer()
        await send_stats(callback.message, db, sheets.enabled)

    @router.callback_query(F.data == "admin:export")
    async def admin_export(callback: CallbackQuery) -> None:
        if not _is_admin_context(callback.from_user.id, callback.message.chat.id, settings):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        await callback.answer()
        await send_export(callback.message, db)

    @router.callback_query(F.data == "admin:sync")
    async def admin_sync(callback: CallbackQuery) -> None:
        if not _is_admin_context(callback.from_user.id, callback.message.chat.id, settings):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        await callback.answer()
        await run_sync_pending(callback.message, db, sheets)

    return router


async def send_stats(message: Message, db: Database, sheets_enabled: bool) -> None:
    stats = await db.stats()
    lines = [
        "<b>Сводка</b>",
        f"Пользователей с согласием: {stats['users']}",
        f"Заявок всего: {stats['leads']}",
        f"Подписчиков на новости: {stats['marketing_opt_ins']}",
        f"Google Sheets: {'настроен' if sheets_enabled else 'не настроен'}; ожидают синхронизации: {stats['sheets_pending']}",
        "\n<b>По статусам:</b>",
    ]
    if stats["by_status"]:
        lines.extend(f"• {html.escape(STATUSES.get(status, status))}: {count}" for status, count in stats["by_status"].items())
    else:
        lines.append("Пока нет заявок.")
    await message.answer("\n".join(lines), reply_markup=admin_home_keyboard())


async def send_export(message: Message, db: Database) -> None:
    rows = await db.export_leads()
    if not rows:
        await message.answer("Пока нечего выгружать.")
        return
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()), extrasaction="ignore")
    writer.writeheader()
    # Neutralize spreadsheet formula prefixes in user-controlled CSV fields.
    safe_rows = []
    for row in rows:
        safe_row = {}
        for key, value in row.items():
            if isinstance(value, str):
                probe = value.lstrip(" \t\r\n")
                if value[:1] in {"\t", "\r"} or (probe and probe[:1] in {"=", "+", "-", "@"}):
                    value = "'" + value
            safe_row[key] = value
        safe_rows.append(safe_row)
    writer.writerows(safe_rows)
    content = ("﻿" + output.getvalue()).encode("utf-8")
    await message.answer_document(
        BufferedInputFile(content, filename="leads_export.csv"),
        caption="Экспорт последних заявок. Файл содержит клиентские данные — храните его безопасно.",
    )


async def run_sync_pending(message: Message, db: Database, sheets: GoogleSheetsMirror) -> None:
    if not sheets.enabled:
        await message.answer("Google Sheets не настроен. Заявки продолжают сохраняться в локальную SQLite-базу.")
        return
    pending = await db.pending_google_sync(100)
    if not pending:
        await message.answer("Все заявки синхронизированы.")
        return
    done = 0
    failed = 0
    for lead in pending:
        try:
            if await sheets.sync_lead(lead):
                await db.mark_google_synced(lead["id"])
                done += 1
            else:
                failed += 1
        except Exception:
            log.exception("Failed to sync lead %s", lead["id"])
            failed += 1
    await message.answer(f"Синхронизация завершена: успешно — {done}, ошибки — {failed}.")
