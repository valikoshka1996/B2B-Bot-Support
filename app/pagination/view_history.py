import math
import html
import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from app.db import SessionLocal
from app.models import Company, Client, Admin
from app.utils import get_company_history

# Якщо логгер не ініціалізовано — створимо запасний варіант
if 'logger' not in locals():
    logger = logging.getLogger(__name__)


async def view_history_paginated(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє пагінацію історії компанії"""
    query = update.callback_query
    await query.answer()

    data = query.data
    session = SessionLocal()
    try:
        # --- Витягуємо ID компанії та сторінку ---
        if data.startswith("view_history:"):
            company_id = int(data.split(":")[1])
            page = 0
        else:
            # формат: history_page:<company_id>:<page>
            _, company_id, page = data.split(":")
            company_id = int(company_id)
            page = int(page)

        # --- Отримуємо компанію ---
        company = session.query(Company).filter_by(id=company_id).first()
        if not company:
            await query.message.edit_text("❌ Компанію не знайдено.")
            return

        # --- Отримуємо історію ---
        messages = get_company_history(session, company_id)
        if not messages:
            await query.message.edit_text(
                f"📭 У компанії <b>{html.escape(company.name)}</b> немає історії повідомлень.",
                parse_mode="HTML"
            )
            return

        # --- Параметри пагінації ---
        per_page = 4
        total_pages = math.ceil(len(messages) / per_page)
        start = len(messages) - (page + 1) * per_page
        end = len(messages) - page * per_page
        start = max(start, 0)
        subset = messages[start:end]

        # --- Формуємо текст ---
        text = f"<b>🕓 Історія компанії {html.escape(company.name)}</b>\n"
        text += f"<i>Сторінка {page + 1} із {total_pages}</i>\n\n"

        for msg in subset:
            # --- Отримуємо ім’я клієнта ---
            client_name = "Клієнт"
            if msg.client_tg_id:
                client = session.query(Client).filter_by(tg_id=msg.client_tg_id).first()
                if client and client.name:
                    client_name = client.name

            # --- Отримуємо ім’я адміна ---
            admin_name = "Адмін"
            if msg.admin_tg_id:
                admin = session.query(Admin).filter_by(tg_id=msg.admin_tg_id).first()
                if admin and admin.name:
                    admin_name = admin.name

            # --- Визначаємо напрямок повідомлення ---
            if msg.direction == "in":
                sender = f"👤 {html.escape(client_name)}"
                recipient = f"🛠️ {html.escape(admin_name)}"
            else:
                sender = f"🛠️ {html.escape(admin_name)}"
                recipient = f"👤 {html.escape(client_name)}"

            safe_text = html.escape(msg.text or "(без тексту)")

            text += (
                f"<b>{sender} → {recipient}</b>\n"
                f"<i>{msg.created_at.strftime('%Y-%m-%d %H:%M:%S')}</i>\n"
                f"{safe_text}\n"
                f"────────────────────\n"
            )

        # --- Кнопки пагінації ---
        buttons = []
        nav_row = []

        if page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton("⬅️ Старіші", callback_data=f"history_page:{company_id}:{page + 1}")
            )
        if page > 0:
            nav_row.append(
                InlineKeyboardButton("Новіші ➡️", callback_data=f"history_page:{company_id}:{page - 1}")
            )

        if nav_row:
            buttons.append(nav_row)

        buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="history_menu")])

        markup = InlineKeyboardMarkup(buttons)

        await query.message.edit_text(text, parse_mode="HTML", reply_markup=markup)

    except Exception as e:
        logger.error(f"Помилка при пагінації історії: {e}")
        await query.message.edit_text("⚠️ Помилка при завантаженні історії.")
    finally:
        session.close()
