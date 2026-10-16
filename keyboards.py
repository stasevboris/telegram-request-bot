"""Inline keyboard builders. Callback payloads are handled by the routers."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from content import CASES, CHANNEL_URL, FAQ, SERVICES
from database import STATUSES


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🧰 Услуги", callback_data="menu:services")
    b.button(text="🗂 Примеры работ", callback_data="menu:cases:0")
    b.button(text="📝 Оставить заявку", callback_data="request:start")
    b.button(text="👨‍💻 Позвать человека", callback_data="call_human")
    b.button(text="❓ FAQ", callback_data="menu:faq:0")
    b.button(text="📌 Этапы работы", callback_data="menu:process")
    b.button(text="✉️ Контакты", callback_data="menu:contact")
    b.button(text="🔔 Новости и подписка", callback_data="menu:updates")
    b.button(text="📣 Канал @borislabs_channel", url=CHANNEL_URL)
    b.button(text="🔐 Конфиденциальность", callback_data="menu:privacy")
    if is_admin:
        b.button(text="⚙️ Админ-панель", callback_data="admin:home")
        b.adjust(2, 2, 2, 2, 2, 1)
    else:
        b.adjust(2, 2, 2, 2, 2)
    return b.as_markup()


def auto_answer_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👨‍💻 Всё равно позвать человека", callback_data="force_human")
    b.button(text="📝 Оставить заявку", callback_data="request:start")
    b.button(text="⬅️ В меню", callback_data="menu:home")
    b.adjust(1, 1, 1)
    return b.as_markup()


def human_called_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💬 Написать напрямую в ЛС", url="https://t.me/borislabs")
    b.button(text="📝 Оставить заявку", callback_data="request:start")
    b.button(text="⬅️ В меню", callback_data="menu:home")
    b.adjust(1, 1, 1)
    return b.as_markup()


def updates_keyboard(is_subscribed: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if is_subscribed:
        b.button(text="🔕 Отписаться от новостей", callback_data="updates:unsubscribe")
    else:
        b.button(text="🔔 Добровольно подписаться", callback_data="updates:subscribe")
    b.button(text="⬅️ В меню", callback_data="menu:home")
    b.adjust(1)
    return b.as_markup()


def consent_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Понятно, продолжить", callback_data="consent:accept")
    b.button(text="🔎 Подробнее о данных", callback_data="consent:privacy")
    b.adjust(1)
    return b.as_markup()


def service_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, service in SERVICES.items():
        b.button(text=f"{service['title']} · {service['price']}", callback_data=f"service:{key}")
    b.button(text="⬅️ В меню", callback_data="menu:home")
    b.adjust(1)
    return b.as_markup()


def case_keyboard(index: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if CASES:
        b.button(text="◀ Предыдущий", callback_data=f"menu:cases:{(index - 1) % len(CASES)}")
        b.button(text="Следующий ▶", callback_data=f"menu:cases:{(index + 1) % len(CASES)}")
    b.button(text="📝 Обсудить задачу", callback_data="request:start")
    b.button(text="⬅️ В меню", callback_data="menu:home")
    b.adjust(2, 1, 1)
    return b.as_markup()


def faq_keyboard(index: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if FAQ:
        b.button(text="◀ Предыдущий", callback_data=f"menu:faq:{(index - 1) % len(FAQ)}")
        b.button(text="Следующий ▶", callback_data=f"menu:faq:{(index + 1) % len(FAQ)}")
    b.button(text="⬅️ В меню", callback_data="menu:home")
    b.adjust(2, 1)
    return b.as_markup()


def request_service_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🧪 Лабораторная (от 300 ₽)", callback_data="request:service:lab")
    b.button(text="📚 Курсовой проект (от 3 000 ₽)", callback_data="request:service:coursework")
    b.button(text="🎓 Диплом / ВКР (от 6 000 ₽)", callback_data="request:service:diploma")
    b.button(text="💡 Консультация / ревью (от 1 000 ₽)", callback_data="request:service:review")
    b.button(text="🤖 Telegram-бот (от 7 000 ₽)", callback_data="request:service:telegram_bot")
    b.button(text="⚡ Парсер / скрипт (от 2 000 ₽)", callback_data="request:service:automation")
    b.button(text="🗄 База данных / SQL (от 2 500 ₽)", callback_data="request:service:database")
    b.button(text="🌐 Веб-сервис / API (от 10 000 ₽)", callback_data="request:service:web_backend")
    b.button(text="📦 Другая задача", callback_data="request:service:other")
    b.button(text="❌ Отмена", callback_data="request:cancel")
    b.adjust(1)
    return b.as_markup()


def intake_controls() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Готово, отправить заявку", callback_data="request:finish")
    b.button(text="⏭ Пропустить вложения", callback_data="request:finish")
    b.button(text="❌ Отмена", callback_data="request:cancel")
    b.adjust(1)
    return b.as_markup()


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="request:cancel")
    ]])


def data_delete_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Удалить мои данные", callback_data="privacy:delete:confirm")
    b.button(text="Оставить как есть", callback_data="privacy:delete:cancel")
    b.adjust(1)
    return b.as_markup()


def admin_home_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📥 Последние заявки", callback_data="admin:leads")
    b.button(text="📊 Статистика", callback_data="admin:stats")
    b.button(text="📤 Выгрузить CSV", callback_data="admin:export")
    b.button(text="🔄 Синхронизировать таблицу", callback_data="admin:sync")
    b.button(text="📢 Рассылка подписчикам", callback_data="admin:broadcast:help")
    b.button(text="⬅️ В меню", callback_data="menu:home")
    b.adjust(1)
    return b.as_markup()


def broadcast_confirmation_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Отправить подписчикам", callback_data=f"admin:broadcast:send:{draft_id}")
    b.button(text="🗑 Отменить", callback_data=f"admin:broadcast:cancel:{draft_id}")
    b.adjust(1)
    return b.as_markup()


def lead_list_keyboard(leads: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for lead in leads:
        name = lead.get("display_name") or lead.get("username") or str(lead["telegram_id"])
        title = f"#{lead['id']} · {name[:24]} · {STATUSES.get(lead['status'], lead['status'])}"
        b.button(text=title[:60], callback_data=f"admin:lead:{lead['id']}")
    b.button(text="⬅️ Админ-панель", callback_data="admin:home")
    b.adjust(1)
    return b.as_markup()


def lead_actions_keyboard(lead_id: int, username: str | None = None, user_id: int | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✉️ Ответить через бота", callback_data=f"admin:reply:{lead_id}")
    if username:
        b.button(text="👤 Написать в ЛС", url=f"https://t.me/{username}")
    elif user_id:
        b.button(text="👤 Написать в ЛС", url=f"tg://user?id={user_id}")
    b.button(text="📎 Передать вложения", callback_data=f"admin:attachments:{lead_id}")
    for status, label in STATUSES.items():
        b.button(text=f"Статус: {label}", callback_data=f"admin:set:{lead_id}:{status}")
    b.button(text="⬅️ К заявкам", callback_data="admin:leads")
    b.adjust(1, 1, 1, 2, 2, 2, 2, 1)
    return b.as_markup()


def client_message_admin_keyboard(lead_id: int, username: str | None = None, user_id: int | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✉️ Ответить через бота", callback_data=f"admin:reply:{lead_id}")
    if username:
        b.button(text="👤 Написать клиенту в ЛС", url=f"https://t.me/{username}")
    elif user_id:
        b.button(text="👤 Написать клиенту в ЛС", url=f"tg://user?id={user_id}")
    b.button(text="📂 Карточка заявки", callback_data=f"admin:lead:{lead_id}")
    b.adjust(1, 1, 1)
    return b.as_markup()
