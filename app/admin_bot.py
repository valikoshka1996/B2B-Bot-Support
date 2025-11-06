import os
import asyncio
import inspect
import logging
from datetime import datetime
import html
import math
from .pagination.view_history import view_history_paginated

from dotenv import load_dotenv
from sqlalchemy import exists
from telegram.error import TimedOut, RetryAfter, NetworkError
from telegram.helpers import escape_markdown
from telegram import (
    BotCommand, Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters,
    CallbackQueryHandler, ContextTypes, ConversationHandler
)

from .db import SessionLocal
from .models import Admin, Company, Client, Message, Claim
from .utils import (
    init_db, add_admin, add_company, add_client,
    update_admin, delete_admin,
    update_company, delete_company,
    update_client, delete_client,
    get_company_history
)

# Load environment variables
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ADMIN_TOKEN = os.getenv("TELEGRAM_TOKEN_ADMIN")
INITIAL_ADMIN = os.getenv("INITIAL_ADMIN_ID")
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "support@yourcompany.com")

init_db(initial_admin_tg_id=INITIAL_ADMIN)

# States for adding admin via contact
ASK_CONTACT = range(1)

ASK_ADMIN_ID, ASK_ADMIN_NAME, ASK_ADMIN_SUPER = range(3)
ASK_BROADCAST_TEXT = 200
ASK_BROADCAST_CONFIRM = 201

ASK_CLIENT_CONTACT, ASK_CLIENT_NAME, ASK_CLIENT_COMPANY = range(300, 303)


def log_tracepoint(tag: str, context: ContextTypes.DEFAULT_TYPE = None):
    """Показує чіткий трек у консолі — хто викликав, де і з якими прапорцями."""
    frame = inspect.stack()[1]
    logger.info(
        f"[TRACE] {tag} | caller={frame.function} | "
        f"broadcast_active={context.user_data.get('broadcast_active')} | "
        f"replying_claim_id={context.user_data.get('replying_claim_id')} | "
        f"reply_mode_active={context.user_data.get('reply_mode_active')} | "
        f"has_broadcast={bool(context.user_data.get('broadcast'))}"
    )




# ------------------- HANDLE BROADCAST INPUT -------------------

async def handle_broadcast_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text and update.message.text.strip().startswith("/cancel"):
        return await broadcast_cancel_callback(update, context)
    """Отримуємо текст або медіа для розсилки (з максимальним логуванням)."""
    log_tracepoint("START handle_broadcast_input", context)

    if context.user_data.get("reply_mode_active") or context.user_data.get("replying_claim_id"):
        logger.warning("[BROADCAST_INPUT] IGNORE — адмін у режимі відповіді!")
        return ConversationHandler.END

    if not context.user_data.get("broadcast_active"):
        logger.warning("[BROADCAST_INPUT] IGNORE — broadcast_active=False")
        return ConversationHandler.END

    tg_id = str(update.effective_user.id)
    log_tracepoint("IS_ADMIN_CHECK", context)
    if not await ensure_is_admin(tg_id):
        await update.message.reply_text("⛔ Ви не є адміністратором.")
        return ConversationHandler.END

    session = SessionLocal()
    try:
        text = update.message.caption or update.message.text or None
        file_id, file_type = None, None

        if update.message.photo:
            file_id = update.message.photo[-1].file_id
            file_type = "photo"
        elif update.message.document:
            file_id = update.message.document.file_id
            file_type = "document"
        elif update.message.video:
            file_id = update.message.video.file_id
            file_type = "video"
        elif update.message.voice:
            file_id = update.message.voice.file_id
            file_type = "voice"
        elif update.message.audio:
            file_id = update.message.audio.file_id
            file_type = "audio"

        bc = {"text": text, "file_id": file_id, "file_type": file_type, "media_path": None}
        context.user_data["broadcast"] = bc
        log_tracepoint(f"SET broadcast structure: {bc}", context)

        if file_id:
            try:
                bot = context.bot
                file = await bot.get_file(file_id)
                ext = {
                    "photo": "jpg", "document": "dat", "video": "mp4",
                    "voice": "ogg", "audio": "mp3"
                }.get(file_type, "bin")
                filename = f"broadcast_{file_type}_{int(datetime.utcnow().timestamp())}_{tg_id}.{ext}"
                media_path = f"/data/media/{filename}"
                os.makedirs("/data/media", exist_ok=True)
                await file.download_to_drive(media_path)
                bc["media_path"] = media_path
                logger.info(f"[BROADCAST_INPUT] 📁 Saved file: {media_path}")
            except Exception as e:
                logger.warning(f"[BROADCAST_INPUT] ⚠️ File save failed: {e}")

        confirm_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Підтвердити і надіслати", callback_data="broadcast_confirm")],
            [InlineKeyboardButton("❌ Скасувати", callback_data="broadcast_cancel")]
        ])
        summary = bc["text"] or "(без тексту)"
        if bc["file_type"]:
            summary += f"\n\n(з медіа: {bc['file_type']})"
        log_tracepoint("SEND CONFIRM PROMPT", context)
        await update.message.reply_text(f"📣 Підтвердіть розсилку:\n\n{summary}", reply_markup=confirm_kb)

        log_tracepoint("END handle_broadcast_input", context)
        return ASK_BROADCAST_CONFIRM
    finally:
        session.close()

# ------------------- CLAIM CALLBACK -------------------

async def claim_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробник кнопки 'Відповісти' — автоматично завершує активну розсилку перед переходом у режим відповіді."""
    q = update.callback_query
    await q.answer()

    admin_tg = str(update.effective_user.id)
    log_tracepoint("START claim_callback", context)

    app = context.application
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # 💣 Якщо активна розсилка — знищуємо її сесію повністю
    if context.user_data.get("broadcast_active") or context.user_data.get("broadcast"):
        logger.info("💣 [CLAIM] Виявлено активний broadcast, примусово закриваю його перед переходом у режим відповіді.")

        # очищення user_data
        context.user_data.pop("broadcast_active", None)
        context.user_data.pop("broadcast", None)

        # шукаємо та видаляємо розмову broadcast_conv у PTB
        for group in app.handlers.values():
            for handler in group:
                if isinstance(handler, ConversationHandler) and getattr(handler, "name", "") == "broadcast_conv":
                    key = (chat_id, user_id)
                    if hasattr(handler, "conversations") and key in handler.conversations:
                        handler.conversations.pop(key, None)
                        logger.info(f"🧹 [CLAIM] Broadcast conversation forcibly closed for {user_id}")

        logger.info("✅ [CLAIM] Broadcast очищено перед взяттям запиту.")

    # 💚 Тепер активуємо режим відповіді
    context.user_data["reply_mode_active"] = True

    data = q.data
    if not data or not data.startswith("claim:"):
        return

    try:
        msgid = int(data.split(":", 1)[1])
    except Exception:
        await q.message.reply_text("Неправильний формат запиту.")
        return

    session = SessionLocal()

    try:
        # знайти повідомлення
        message = session.query(Message).filter_by(id=msgid).first()
        if not message:
            await q.message.reply_text("Повідомлення вже не знайдено.")
            return

        # перевірити чи вже є Claim по цьому message_id
        existing = session.query(Claim).filter_by(message_id=msgid).first()
        if existing:
            admin_obj = session.query(Admin).filter_by(id=existing.admin_id).first()
            admin_name = admin_obj.name if admin_obj else str(existing.admin_id)
            await q.message.reply_text(f"⚠️ Запит вже взяв адміністратор {admin_name}")
            return

        # знайти адміна (того, хто натиснув кнопку)
        admin_obj = session.query(Admin).filter_by(tg_id=admin_tg).first()
        if not admin_obj:
            await q.message.reply_text("❌ Ви не зареєстровані як адміністратор.")
            return

        # знайти клієнта (можливо None)
        client_obj = session.query(Client).filter_by(tg_id=message.client_tg_id).first()

        # створити Claim (використовуємо admin_id, client_id, message_id)
        claim = Claim(
            message_id=msgid,
            client_id=client_obj.id if client_obj else None,
            admin_id=admin_obj.id,
            title=f"Запит від {client_obj.name if client_obj else message.client_tg_id}",
            description=(message.text or "")[:4000],
            status="in_progress"
        )
        session.add(claim)
        session.commit()
        session.refresh(claim)

        # сповіщаємо інших адміністраторів
        other_admins = session.query(Admin).filter(Admin.tg_id != admin_tg).all()
        notify_text = f"🔒 Запит #{msgid} взяв адміністратор {admin_obj.name or admin_obj.tg_id}"
        for a in other_admins:
            try:
                await context.bot.send_message(chat_id=int(a.tg_id), text=notify_text)
            except Exception as e:
                logger.warning(f"Can't notify admin {a.tg_id}: {e}")

        # оновлюємо кнопку
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Взято ✅", callback_data="taken")]])
        try:
            await q.edit_message_reply_markup(reply_markup=keyboard)
        except Exception as e:
            logger.debug(f"edit_message_reply_markup failed: {e}")

        # зберігаємо в контекст
        context.user_data["replying_claim_id"] = claim.id

        # 🟢 Повідомлення адміну
        await context.bot.send_message(
            chat_id=int(admin_tg),
            text=(
                f"🟢 Ви взяли запит #{msgid} від клієнта "
                f"{client_obj.name if client_obj else message.client_tg_id}.\n\n"
                f"✍️ Тепер просто напишіть повідомлення у цьому чаті — "
                f"воно буде надіслано клієнту від вашого імені ({admin_obj.name or admin_tg})."
            )
        )

        logger.info(f"✅ Admin {admin_tg} взяв claim #{claim.id}")

    except Exception as e:
        logger.exception(f"Error in claim_callback: {e}")
        try:
            await q.message.reply_text("⚠️ Сталася помилка під час обробки запиту.")
        except Exception:
            pass
    finally:
        session.close()

# ------------------- START CLAIM FLOW -------------------

async def start_claim_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Взяття запиту (з trace логами)."""
    log_tracepoint("START start_claim_flow", context)
    q = update.callback_query
    await q.answer()
    data = q.data
    if not data or not data.startswith("claim:"):
        logger.error("[CLAIM_FLOW] wrong callback data")
        return ConversationHandler.END

    try:
        msgid = int(data.split(":", 1)[1])
    except Exception:
        await q.message.reply_text("Неправильний формат запиту.")
        return ConversationHandler.END

    admin_tg = str(update.effective_user.id)
    session = SessionLocal()
    try:
        message = session.query(Message).filter_by(id=msgid).first()
        if not message:
            await q.message.reply_text("Повідомлення вже не знайдено.")
            return ConversationHandler.END

        existing = session.query(Claim).filter_by(message_id=msgid).first()
        if existing:
            logger.warning(f"[CLAIM_FLOW] already claimed {msgid}")
            return ConversationHandler.END

        admin_obj = session.query(Admin).filter_by(tg_id=admin_tg).first()
        client_obj = session.query(Client).filter_by(tg_id=message.client_tg_id).first()

        claim = Claim(
            message_id=msgid,
            client_id=client_obj.id if client_obj else None,
            admin_id=admin_obj.id,
            title=f"Запит від {client_obj.name if client_obj else message.client_tg_id}",
            description=(message.text or "")[:4000],
            status="in_progress"
        )
        session.add(claim)
        session.commit()
        session.refresh(claim)
        log_tracepoint(f"[CLAIM_FLOW] created claim #{claim.id}", context)

        context.user_data["replying_claim_id"] = claim.id

        await context.bot.send_message(
            chat_id=int(admin_tg),
            text=(f"🟢 Ви взяли запит #{msgid} від {client_obj.name or message.client_tg_id}.\n\n"
                  f"✍️ Тепер просто напишіть повідомлення — "
                  f"воно буде надіслано клієнту від вашого імені.")
        )

        logger.info(f"[CLAIM_FLOW] ✅ claim ready #{claim.id}")
        log_tracepoint("END start_claim_flow", context)
        return ConversationHandler.END
    finally:
        session.close()

# entry для broadcast — окрема проста функція, щоб ConversationHandler точно активувався
async def start_broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Почати масову розсилку — entry point для ConversationHandler."""
    q = update.callback_query
    await q.answer()

    tg_id = str(update.effective_user.id)
    app = context.application
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # 🔒 Перевіряємо адміністратора
    if not await ensure_is_admin(tg_id):
        await q.message.reply_text("⛔ Ви не є адміністратором.")
        return ConversationHandler.END

    # 💣 Насильно видаляємо стару сесію broadcast_conv, якщо зависла
    for group in app.handlers.values():
        for handler in group:
            if isinstance(handler, ConversationHandler) and getattr(handler, "name", "") == "broadcast_conv":
                if hasattr(handler, "conversations"):
                    handler.conversations.pop((chat_id, user_id), None)
                    logger.info(f"💣 [BROADCAST_RESET] Стару сесію broadcast_conv видалено для {user_id}")

    # 🧹 Повністю очищаємо контекст користувача
    context.user_data.clear()

    # 🧩 Повідомлення з інструкцією
    await q.message.reply_text(
        "📣 Введіть текст повідомлення **або** надішліть медіа з підписом, яке потрібно розіслати всім клієнтам.\n\n"
        "Після надсилання натисніть ✅ ПІДТВЕРДИТИ або ❌",
        parse_mode="Markdown"
    )

    context.user_data["broadcast_active"] = True
    logger.info("✅ [BROADCAST] Broadcast режим активовано (нова сесія).")

    return ASK_BROADCAST_TEXT



#хендлер обробки контакту клієнта:
async def handle_client_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка отриманого контакту або вручну введеного ID/username."""
    msg = update.message
    session = SessionLocal()

    tg_id = None
    name = None

    # --- Якщо контакт ---
    if msg.contact:
        contact = msg.contact
        tg_id = str(contact.user_id) if contact.user_id else contact.phone_number
        name_parts = [contact.first_name or "", contact.last_name or ""]
        name = " ".join(p for p in name_parts if p).strip()

    # --- Якщо текст (ID або @username) ---
    elif msg.text:
        text = msg.text.strip()
        if text.startswith("@"):
            tg_id = text[1:]  # зберігаємо як username без @
        else:
            tg_id = text
        name = None  # запитаємо далі

    # --- Перевірка ---
    if not tg_id:
        await msg.reply_text("❌ Не вдалося отримати Telegram ID. Спробуйте ще раз.")
        return ASK_CLIENT_CONTACT

    # Зберігаємо в контексті
    context.user_data["new_client_tg_id"] = tg_id
    context.user_data["new_client_name"] = name

    if not name:
        await msg.reply_text("✏️ Введіть ім’я клієнта:")
        session.close()
        return ASK_CLIENT_NAME

    # Якщо вже є ім’я — одразу переходимо до вибору компанії
    companies = session.query(Company).all()
    if not companies:
        await msg.reply_text("⚠️ Немає жодної компанії. Спочатку додайте компанію.")
        session.close()
        return ConversationHandler.END

    text = "🏢 Доступні компанії:\n" + "\n".join([f"{c.id} — {c.name}" for c in companies])
    await msg.reply_text(text + "\n\nВведіть ID компанії:")
    session.close()
    return ASK_CLIENT_COMPANY

#хендлер обробки введення імені клієнта:

async def handle_client_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка введеного імені клієнта."""
    name = update.message.text.strip()
    context.user_data["new_client_name"] = name

    session = SessionLocal()
    companies = session.query(Company).all()
    if not companies:
        await update.message.reply_text("⚠️ Немає жодної компанії. Спочатку додайте компанію.")
        session.close()
        return ConversationHandler.END

    text = "🏢 Доступні компанії:\n" + "\n".join([f"{c.id} — {c.name}" for c in companies])
    await update.message.reply_text(text + "\n\nВведіть ID компанії:")
    session.close()
    return ASK_CLIENT_COMPANY


#хендлер обробки введення компанії для клієнта:

async def handle_client_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Фінальний крок — збереження клієнта."""
    session = SessionLocal()
    company_id_text = update.message.text.strip()

    if not company_id_text.isdigit():
        await update.message.reply_text("❌ ID компанії має бути числом. Спробуйте ще раз:")
        session.close()
        return ASK_CLIENT_COMPANY

    company_id = int(company_id_text)
    company = session.query(Company).filter_by(id=company_id).first()
    if not company:
        await update.message.reply_text("❌ Компанію не знайдено. Введіть інший ID:")
        session.close()
        return ASK_CLIENT_COMPANY

    company_name = company.name  # ✅ читаємо до закриття сесії

    tg_id = context.user_data.get("new_client_tg_id")
    name = context.user_data.get("new_client_name") or "—"

    # зберігаємо в базу
    add_client(session, tg_id=tg_id, name=name, company_id=company_id)
    session.close()

    await update.message.reply_text(
        f"✅ Клієнта *{name}* успішно додано до компанії *{company_name}*.",
        parse_mode="Markdown"
    )

    logger.info(f"✅ [ADD_CLIENT] Клієнта '{name}' додано до компанії '{company_name}' (tg_id={tg_id})")

    context.user_data.clear()
    return ConversationHandler.END





#повторні спроби та обробка помилок
async def safe_send(client_bot: Bot, send_coro_callable, *args, retry=1, delay_on_timeout=5, **kwargs):
    """
    send_coro_callable — корутина-заглушка типу client_bot.send_message або send_photo (функція, не виклик!)
    Викликається як: await safe_send(bot, bot.send_message, chat_id, text=..., retry=2)
    Повертає True якщо успішно, False якщо провалилися всі спроби.
    """
    try_count = 0
    while True:
        try:
            await send_coro_callable(*args, **kwargs)
            return True
        except RetryAfter as e:
            delay = int(getattr(e, "retry_after", 5))
            logger.warning(f"RateLimit — чекаю {delay}s")
            await asyncio.sleep(delay)
            try_count += 1
        except TimedOut:
            logger.warning(f"TimedOut при відправці, спробую через {delay_on_timeout}s")
            await asyncio.sleep(delay_on_timeout)
            try_count += 1
        except NetworkError:
            logger.warning("NetworkError при відправці — пропускаю цей контакт")
            return False
        except Exception as e:
            logger.exception(f"Несподівана помилка при відправці: {e}")
            return False

        if try_count > retry:
            logger.error("Вичерпано кількість повторних спроб")
            return False

async def broadcast_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tg_id = str(update.effective_user.id)
    if not await ensure_is_admin(tg_id):
        await q.message.reply_text("⛔ Ви не є адміністратором.")
        return

    bc = context.user_data.get("broadcast")
    if not bc:
        await q.message.reply_text("⚠️ Немає підготовленого повідомлення для розсилки.")
        return

    # Параметри розсилки
    delay = float(os.getenv("BROADCAST_DELAY", "0.06"))  # сек між повідомленнями (налаштовувано)
    client_token = os.getenv("TELEGRAM_TOKEN_CLIENT")
    client_bot = Bot(token=client_token)

    session = SessionLocal()
    try:
        clients = session.query(Client.tg_id).all()  # список кортежів
        client_ids = [c[0] for c in clients]
        total = len(client_ids)
        await q.message.reply_text(f"🚀 Починаю розсилку на {total} клієнтів. Це може зайняти деякий час...")

        sent = 0
        failed = 0

        # Відправка: відкриваємо локальний файл (якщо є), і для кожного клієнта посилаємо.
        media_path = bc.get("media_path")
        file_type = bc.get("file_type")
        text = bc.get("text")

        # Для економії: якщо media_path є — будемо відкривати файл щоразу в циклі
        for cid in client_ids:
            try:
                # 1) зберегти запис у БД (direction='out') ПЕРЕД відправкою
                m = Message(client_tg_id=str(cid), admin_tg_id=str(tg_id), direction="out",
                            text=text, file_id=bc.get("file_id"), file_type=file_type,
                            file_path=media_path, company_snapshot=None)
                session.add(m)
                session.commit()

                # 2) відправка через safe_send
                if media_path and os.path.exists(media_path):
                    with open(media_path, "rb") as f:
                        if file_type == "photo":
                            ok = await safe_send(client_bot, client_bot.send_photo, chat_id=int(cid), photo=f, caption=f"📣 {text or ''}")
                        elif file_type == "document":
                            ok = await safe_send(client_bot, client_bot.send_document, chat_id=int(cid), document=f, caption=f"📣 {text or ''}")
                        elif file_type == "video":
                            ok = await safe_send(client_bot, client_bot.send_video, chat_id=int(cid), video=f, caption=f"📣 {text or ''}")
                        elif file_type == "voice" or file_type == "audio":
                            ok = await safe_send(client_bot, client_bot.send_voice if file_type == "voice" else client_bot.send_audio, chat_id=int(cid), voice=f if file_type=="voice" else None, audio=f if file_type=="audio" else None, caption=f"📣 {text or ''}")
                        else:
                            ok = await safe_send(client_bot, client_bot.send_message, chat_id=int(cid), text=f"📣 {text or ''}")
                else:
                    ok = await safe_send(client_bot, client_bot.send_message, chat_id=int(cid), text=f"📣 {text or ''}")

                if ok:
                    sent += 1
                else:
                    failed += 1

            except Exception as e:
                logger.exception(f"Помилка при розсилці клієнту {cid}: {e}")
                failed += 1

            # throttle
            await asyncio.sleep(delay)

        await q.message.reply_text(f"✅ Розсилка завершена. Відправлено: {sent}, помилок: {failed}")

    finally:
        # очистка: видаляємо тимчасовий файл тільки після завершення циклу
        try:
            if bc.get("media_path") and os.path.exists(bc.get("media_path")):
                os.remove(bc.get("media_path"))
                logger.info(f"🗑️ Видалено тимчасове медіа: {bc.get('media_path')}")
        except Exception as e:
            logger.warning(f"⚠️ Не вдалося видалити тимчасовий файл: {e}")

        context.user_data.pop("broadcast", None)
        session.close()
        context.user_data.pop("broadcast", None)
        context.user_data["broadcast_active"] = False
    return ConversationHandler.END

#callback handlers для підтвердження / відміни. Додавши обробку broadcast_confirm та broadcast_cancel в admin_menu_callback або як глобальні CallbackQueryHandler — краще окремим handler-ом:

#Reset бота
async def reset_states_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id if query.message else query.from_user.id
    user_id = query.from_user.id

    await context.bot.send_message(chat_id, "🔄 Скидаю всі стани...")

    logger.warning(f"🔄 Admin {query.from_user.username} ({user_id}) виконав повний reset станів.")

    try:
        # 1) Очистка локальних context-даних
        try:
            context.user_data.clear()
        except Exception:
            logger.debug("Не вдалося context.user_data.clear()", exc_info=True)
        try:
            context.chat_data.clear()
        except Exception:
            logger.debug("Не вдалося context.chat_data.clear()", exc_info=True)

        # 2) Спроба очистити внутрішні структури ConversationHandler у різних формах
        cleared_handlers = 0
        for handlers_group in context.application.handlers.values():
            for handler in handlers_group:
                # перевіряємо тип, щоб уникнути зайвих об'єктів
                try:
                    from telegram.ext import ConversationHandler as PTBConversationHandler
                except Exception:
                    PTBConversationHandler = None

                if PTBConversationHandler and isinstance(handler, PTBConversationHandler):
                    cleaned = False
                    # можливі імена внутрішніх атрибутів у різних версіях PTB
                    possible_attrs = ("conversations", "_conversations", "conversation_storage", "conversation_states")
                    for attr in possible_attrs:
                        conv_obj = getattr(handler, attr, None)
                        if conv_obj is not None:
                            try:
                                # conv_obj може бути dict або спеціальним об'єктом з clear()
                                if hasattr(conv_obj, "clear"):
                                    conv_obj.clear()
                                else:
                                    # якщо ітерабельний mapping — видаляємо ключі
                                    for k in list(conv_obj.keys()):
                                        conv_obj.pop(k, None)
                                cleaned = True
                                break
                            except Exception:
                                logger.debug(f"Не вдалося очистити {attr} у handler {handler}", exc_info=True)
                    if cleaned:
                        cleared_handlers += 1

        # 3) Очистити глобальний application._conversations (якщо є)
        try:
            if hasattr(context.application, "_conversations"):
                # _conversations зазвичай mapping {(chat_id, user_id): state}
                convs = context.application._conversations
                # видаляємо ключі що стосуються поточного чату/юзера
                keys_to_remove = []
                for k in list(convs.keys()):
                    try:
                        # ключ може бути tuple (chat_id, user_id) або інший формат
                        if (isinstance(k, tuple) and (k[0] == chat_id or k[1] == user_id)) or (k == chat_id) or (k == user_id):
                            keys_to_remove.append(k)
                    except Exception:
                        # якщо несподіваний формат — видалимо всі, щоб бути впевненим (fallback)
                        keys_to_remove.append(k)
                for k in keys_to_remove:
                    try:
                        convs.pop(k, None)
                    except Exception:
                        pass
        except Exception:
            logger.debug("Не вдалося почистити context.application._conversations", exc_info=True)

        # 4) Якщо є persistence — спробувати почистити його записи для цього користувача/чату
        try:
            persistence = getattr(context.application, "persistence", None)
            if persistence:
                try:
                    if hasattr(persistence, "drop_user_data"):
                        persistence.drop_user_data(user_id)
                    if hasattr(persistence, "drop_chat_data"):
                        persistence.drop_chat_data(chat_id)
                    # якщо є flush/close методи
                    if hasattr(persistence, "flush"):
                        persistence.flush()
                except Exception as e:
                    logger.warning(f"Не вдалося очистити persistence: {e}")
        except Exception:
            logger.debug("Помилка при роботі з persistence", exc_info=True)

        await context.bot.send_message(chat_id, f"✅ Всі стани очищено. Очищено handlers: {cleared_handlers}")

        # 5) Повернутися в головне меню (start_admin обробляє як message.reply_text або callback)
        # Викликаємо start_admin з оригінальним update (в якому є query.message) — воно відпрацює нормально
        fake_update = Update(update.update_id, message=query.message)
        await start_admin(fake_update, context)

    except Exception as e:
        logger.exception("Помилка під час скидання станів")
        try:
            await context.bot.send_message(chat_id, f"❌ Помилка при скиданні станів: {e}")
        except Exception:
            pass

        
async def silent_broadcast_cancel(context: ContextTypes.DEFAULT_TYPE):
    """Прибирає усі дані розсилки без відправлення повідомлення."""
    bc = context.user_data.pop("broadcast", None)
    if bc and bc.get("media_path"):
        try:
            if os.path.exists(bc["media_path"]):
                os.remove(bc["media_path"])
        except Exception:
            pass
    context.user_data.pop("broadcast_active", None)
    logger.info("🧹 Silent broadcast cancel executed.")

import asyncio
from telegram.ext import ConversationHandler

async def broadcast_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скасування розсилки (на будь-якому етапі: введення або підтвердження)."""
    tg_id = str(update.effective_user.id)
    is_admin = await ensure_is_admin(tg_id)

    # Визначаємо повідомлення для відповіді
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        target = q.message
    else:
        target = update.message

    # Якщо не адмін
    if not is_admin:
        await target.reply_text("⛔ Ви не є адміністратором.")
        return ConversationHandler.END

    # 🧹 Видалення медіа (якщо було)
    bc = context.user_data.get("broadcast")
    if bc and bc.get("media_path"):
        try:
            os.remove(bc["media_path"])
        except Exception:
            pass

    # 🧠 Повне очищення контексту користувача
    context.user_data.clear()

    # 💣 Насильно прибираємо залишки поточної розмови (це головне)
    app = context.application
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    for group in app.handlers.values():
        for handler in group:
            if isinstance(handler, ConversationHandler) and getattr(handler, "name", "") == "broadcast_conv":
                if hasattr(handler, "conversations"):
                    handler.conversations.pop((chat_id, user_id), None)
                    logger.info(f"💣 [BROADCAST_CANCEL] Залишки сесії broadcast_conv видалено для user={user_id}")

    # 💤 Маленька затримка для стабільного виходу зі стану
    await asyncio.sleep(0.2)

    await target.reply_text("❌ Розсилку скасовано.")
    logger.info(f"🧹 [BROADCAST_CANCEL] Розсилку скасовано вручну для admin={tg_id}")

    return ConversationHandler.END






# --- Меню ---
async def start_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    if not await ensure_is_admin(tg_id):
        await update.message.reply_text("Ви не є адміністратором цього бота.")
        return

    keyboard = [
        [InlineKeyboardButton("👤 Додати адміна", callback_data="add_admin")],
        [InlineKeyboardButton("📋 Список адмінів", callback_data="list_admins")],
        [InlineKeyboardButton("✏️ Оновити адміна", callback_data="update_admin")],
        [InlineKeyboardButton("🗑️ Видалити адміна", callback_data="delete_admin")],
        [InlineKeyboardButton("📬 Необроблені повідомлення", callback_data="unprocessed")],   # <- додано
        [InlineKeyboardButton("🏢 Компанії", callback_data="companies_menu")],
        [InlineKeyboardButton("👥 Клієнти", callback_data="clients_menu")],
        [InlineKeyboardButton("🕓 Історія комунікацій", callback_data="history_menu")],
        [InlineKeyboardButton("📣 МАССОВА РОЗСИЛКА", callback_data="broadcast")],
        [InlineKeyboardButton("🔄 Перезавантажити бота", callback_data="reset_states")],  # ← Додано


    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Адмін-панель:", reply_markup=reply_markup)


def safe_md2(value):
    if not value:
        return "-"
    return escape_markdown(str(value), version=2)

# --- Виклик з меню ---
async def admin_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.debug(f"ADMIN_MENU_CALLBACK invoked. data={getattr(update.callback_query, 'data', None)}; from={update.effective_user.id}")
    if context.user_data.get("broadcast_active"):
        context.user_data.pop("broadcast_active", None)
        context.user_data.pop("broadcast", None)

    query = update.callback_query
    await query.answer()
    data = query.data
    tg_id = str(update.effective_user.id)

    if not await ensure_is_admin(tg_id):
        await query.message.reply_text("⛔ Ви не є адміністратором.")
        return

    # --- Додати адміна ---
    if data == "add_admin":
        await query.message.reply_text("👤 Введіть Telegram ID нового адміністратора або надішліть його контакт:")
        context.user_data["action"] = "add_admin"
        context.chat_data["action"] = "add_admin"
        return ASK_CONTACT

    # --- Список адмінів ---
    elif data == "list_admins":
        session = SessionLocal()
        try:
            admins = session.query(Admin).all()
            if not admins:
                await query.message.reply_text("Список адміністраторів порожній.")
                return
            text = "*📋 Список адмінів:*\n"
            for a in admins:
                star = "⭐️" if a.is_super else ""
                text += f"- {a.name or '—'} {star}\n  `tg_id:` {a.tg_id}\n"
            await query.message.reply_text(text, parse_mode="Markdown")
        finally:
            session.close()

    # --- Оновити адміна ---
    elif data == "update_admin":
        await query.message.reply_text("✏️ Введіть Telegram ID адміністратора, якого потрібно оновити:")
        context.user_data["action"] = "update_admin"
        context.chat_data["action"] = "update_admin"
        return ASK_ADMIN_ID

    # --- Видалити адміна ---
    elif data == "delete_admin":
        await query.message.reply_text("🗑️ Введіть Telegram ID адміністратора, якого потрібно видалити:")
        context.user_data["action"] = "delete_admin"
        context.chat_data["action"] = "delete_admin"
        return ASK_ADMIN_ID

    # --- Меню компаній ---
    elif data == "companies_menu":
        keyboard = [
            [InlineKeyboardButton("➕ Додати компанію", callback_data="add_company_menu")],
            [InlineKeyboardButton("✏️ Оновити компанію", callback_data="update_company_menu")],
            [InlineKeyboardButton("🗑️ Видалити компанію", callback_data="delete_company_menu")],
            [InlineKeyboardButton("📋 Переглянути всі", callback_data="list_companies_menu")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")],
        ]
        await query.message.reply_text("🏢 Меню компаній:", reply_markup=InlineKeyboardMarkup(keyboard))




    # --- Необроблені повідомлення ---
    elif data == "unprocessed":
        session = SessionLocal()
        try:
            # беремо всі вхідні messages без пов'язаного claim
            q = session.query(Message).filter(Message.direction == "in")
            q = q.filter(~exists().where(Claim.message_id == Message.id))
            messages = q.order_by(Message.created_at.asc()).limit(100).all()  # ліміт, щоб не спамити

            if not messages:
                await query.message.reply_text("📭 Немає необроблених повідомлень.")
                return

            await query.message.reply_text(f"📬 Знайдено {len(messages)} необроблених повідомлень (показую нові першими).")

            for msg in messages:
                # текст, короткий снэпшот компанії
                notify_text = (
                    f"📩 Повідомлення від клієнта <b>{msg.client.name if hasattr(msg, 'client') and msg.client else msg.client_tg_id}</b>\n"
                    f"🏢 Компанія: {msg.company_snapshot or '-'}\n"
                    f"🆔 MsgID: <code>{msg.id}</code>\n\n"
                    f"💬 {msg.text or '(без тексту)'}"
                )
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Відповісти", callback_data=f"claim:{msg.id}")]])

                # якщо є file_id — використовуємо його напряму (не з диску)
                try:
                    if msg.file_id and msg.file_type:
                        if msg.file_type == "photo":
                            await context.bot.send_photo(chat_id=int(tg_id), photo=msg.file_id, caption=notify_text, parse_mode="HTML", reply_markup=keyboard)
                        elif msg.file_type == "document":
                            await context.bot.send_document(chat_id=int(tg_id), document=msg.file_id, caption=notify_text, parse_mode="HTML", reply_markup=keyboard)
                        elif msg.file_type == "video":
                            await context.bot.send_video(chat_id=int(tg_id), video=msg.file_id, caption=notify_text, parse_mode="HTML", reply_markup=keyboard)
                        elif msg.file_type == "voice":
                            await context.bot.send_voice(chat_id=int(tg_id), voice=msg.file_id, caption=notify_text, parse_mode="HTML", reply_markup=keyboard)
                        elif msg.file_type == "audio":
                            # audio may be send as document or audio
                            await context.bot.send_audio(chat_id=int(tg_id), audio=msg.file_id, caption=notify_text, parse_mode="HTML", reply_markup=keyboard)
                        else:
                            # fallback: просто текстовий варіант
                            await context.bot.send_message(chat_id=int(tg_id), text=notify_text, parse_mode="HTML", reply_markup=keyboard)
                    else:
                        await context.bot.send_message(chat_id=int(tg_id), text=notify_text, parse_mode="HTML", reply_markup=keyboard)
                except Exception as e:
                    logger.warning(f"⚠️ Не вдалося надіслати необроблене повідомлення {msg.id} адміну {tg_id}: {e}")

        finally:
            session.close()


    elif data == "history_menu":
        session = SessionLocal()
        try:
            companies = session.query(Company).all()
            if not companies:
                await query.message.reply_text("📭 Немає компаній для перегляду історії.")
                return

            keyboard = []
            for comp in companies:
                keyboard.append([InlineKeyboardButton(f"{comp.name}", callback_data=f"view_history:{comp.id}")])

            keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")])
            await query.message.reply_text("🕓 Оберіть компанію для перегляду історії:", reply_markup=InlineKeyboardMarkup(keyboard))
        finally:
            session.close()

    elif data.startswith("view_history:"):
        await view_history_paginated(update, context)



    # --- Меню клієнтів ---
    elif data == "clients_menu":
        keyboard = [
            [InlineKeyboardButton("➕ Додати клієнта", callback_data="add_client_menu")],
            [InlineKeyboardButton("✏️ Оновити клієнта", callback_data="update_client_menu")],
            [InlineKeyboardButton("🗑️ Видалити клієнта", callback_data="delete_client_menu")],
            [InlineKeyboardButton("📋 Переглянути всіх", callback_data="list_clients_menu")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")],
        ]
        await query.message.reply_text("👥 Меню клієнтів:", reply_markup=InlineKeyboardMarkup(keyboard))
    # --- CRUD компаній ---
    elif data == "add_company_menu":
        await query.message.reply_text("Введіть дані компанії у форматі:\n`Назва|Контакт|ClientID|ClientSecret`", parse_mode="Markdown")
        context.user_data["action"] = "add_company_menu"

    elif data == "update_company_menu":
        await query.message.reply_text("Введіть нові дані компанії у форматі:\n`id|Назва|Контакт|ClientID|ClientSecret`", parse_mode="Markdown")
        context.user_data["action"] = "update_company_menu"

    elif data == "delete_company_menu":
        await query.message.reply_text("Введіть ID компанії для видалення:")
        context.user_data["action"] = "delete_company_menu"

    elif data == "list_companies_menu":
        session = SessionLocal()
        try:
            companies = session.query(Company).all()
            if not companies:
                await query.message.reply_text("📭 Немає зареєстрованих компаній.")
                return

            text = "<b>🏢 Список компаній з працівниками:</b>\n\n"
            for comp in companies:
                text += (
                    f"<b>🏢 {comp.name or '-'} (ID: {comp.id})</b>\n"
                    f"👤 Контакт: {comp.contact_name or '-'}\n"
                    f"🧩 ClientID: <code>{comp.client_id or '-'}</code>\n"
                    f"🔑 ClientSecret: <code>{comp.client_secret or '-'}</code>\n"
                )

                clients = session.query(Client).filter_by(company_id=comp.id).all()
                if clients:
                    text += "👥 <b>Працівники:</b>\n"
                    for cl in clients:
                        text += f"• {cl.name or '-'} (tg_id: <code>{cl.tg_id}</code>)\n"
                else:
                    text += "👥 Працівників не знайдено.\n"

                text += "\n────────────────────────\n\n"

            await query.message.reply_text(text, parse_mode="HTML")

        finally:
            session.close()

        
    # --- CRUD клієнтів ---
    elif data == "add_client_menu":
        # Скидаємо всі попередні дії
        context.user_data.clear()
        context.user_data["action"] = "add_client_menu"

        await query.message.reply_text(
            "📞 Надішліть контакт клієнта (через 📎), "
            "або введіть його Telegram ID чи юзернейм (@username):"
        )
        return ASK_CLIENT_CONTACT

    elif data == "update_client_menu":
        await query.message.reply_text("Введіть нові дані клієнта у форматі:\n`tg_id|Ім’я|company_id`", parse_mode="Markdown")
        context.user_data["action"] = "update_client_menu"

    elif data == "delete_client_menu":
        await query.message.reply_text("Введіть tg_id клієнта для видалення:")
        context.user_data["action"] = "delete_client_menu"

    elif data == "list_clients_menu":
        session = SessionLocal()
        try:
            clients = session.query(Client).all()
            if not clients:
                await query.message.reply_text("📭 Немає клієнтів.")
                return
            text = "*👥 Список клієнтів:*\n"
            for c in clients:
                cname = session.query(Company).filter_by(id=c.company_id).first()
                comp_name = cname.name if cname else "—"
                text += f"- {c.name or '—'} (`{c.tg_id}`) — 🏢 {comp_name}\n"
            await query.message.reply_text(text, parse_mode="Markdown")
        finally:
            session.close()

    # --- Повернення до головного меню ---
    elif data == "back_to_main":
        keyboard = [
            [InlineKeyboardButton("👤 Додати адміна", callback_data="add_admin")],
            [InlineKeyboardButton("📋 Список адмінів", callback_data="list_admins")],
            [InlineKeyboardButton("✏️ Оновити адміна", callback_data="update_admin")],
            [InlineKeyboardButton("🗑️ Видалити адміна", callback_data="delete_admin")],
            [InlineKeyboardButton("🏢 Компанії", callback_data="companies_menu")],
            [InlineKeyboardButton("👥 Клієнти", callback_data="clients_menu")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text("🔙 Повернення до головного меню:", reply_markup=reply_markup)




async def ensure_is_admin(tg_id: str):
    session = SessionLocal()
    try:
        admin = session.query(Admin).filter_by(tg_id=str(tg_id)).first()
        return admin is not None
    finally:
        session.close()

async def help_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    if not await ensure_is_admin(tg_id):
        await update.message.reply_text("Ви не є адміністратором цього бота.")
        return
    text = "Адмін-панель:\n"
    text += "/add_admin - додати адміна (надішліть контакт після команди)\n"
    text += "/list_admins - список адміністраторів\n"
    text += "/add_company - додати компанію (/add_company Назва|Контакт|ClientID|ClientSecret)\n"
    text += "/list_companies - список компаній\n"
    text += "/register_client - прив'язати клієнта до компанії (/register_client tg_id|ім'я|company_id)\n"
    text += "/history_client tg_id - переглянути історію по клієнту\n"
    text += "\nОновлення та видалення:\n"
    text += "/update_admin tg_id|name|is_super(True/False)\n"
    text += "/delete_admin tg_id\n"
    text += "/update_company id|name|contact|client_id|client_secret\n"
    text += "/delete_company id\n"
    text += "/update_client tg_id|name|company_id\n"
    text += "/delete_client tg_id\n"

    await update.message.reply_text(text)

# Add admin flow: admin sends /add_admin then sends contact (or tg_id text)
async def add_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_is_admin(str(update.effective_user.id)):
        await update.message.reply_text("Доступ заборонено.")
        return ConversationHandler.END
    await update.message.reply_text("Надішліть контакт нового адміністратора або введіть його Telegram ID.")
    return ASK_CONTACT

async def receive_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = SessionLocal()
    try:
        # 1️⃣ Отримуємо контакт або текст
        if update.message.contact:
            tg = update.message.contact.user_id
            name = update.message.contact.first_name
        else:
            tg = update.message.text.strip()
            name = None

        # 2️⃣ Перевіряємо, чи адмін уже існує
        existing = session.query(Admin).filter_by(tg_id=str(tg)).first()
        if existing:
            await update.message.reply_text(f"⚠️ Адмін із Telegram ID {tg} вже існує ({existing.name or 'без імені'}).")
            return ConversationHandler.END

        # 3️⃣ Додаємо нового адміна
        a = add_admin(session, tg_id=str(tg), name=name)

        # 4️⃣ Відправляємо повідомлення новому адміну (якщо бот має до нього доступ)
        try:
            await context.bot.send_message(chat_id=int(tg), text="Привіт! Тебе призначили адміністратором 🚀")
        except Exception as e:
            logger.warning(f"Не вдалося надіслати повідомлення новому адміну {tg}: {e}")

        await update.message.reply_text(f"✅ Адмін доданий: {name or tg}")

    finally:
        session.close()
    return ConversationHandler.END


# --- Обробка введення ID для оновлення/видалення ---
async def process_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = context.user_data.get("action") or context.chat_data.get("action")
    session = SessionLocal()

    try:
        tg = update.message.text.strip()
        admin = session.query(Admin).filter_by(tg_id=str(tg)).first()

        if not admin:
            await update.message.reply_text("❌ Адміністратора з таким ID не знайдено.")
            return ConversationHandler.END

        # --- Update flow ---
        if action == "update_admin":
            context.user_data["tg_id"] = tg
            await update.message.reply_text(f"🔹 Введіть нове ім’я для {tg} (залиште порожнім, щоб не змінювати):")
            context.user_data["step"] = "ask_name"
            return ASK_ADMIN_NAME

        # --- Delete flow ---
        elif action == "delete_admin":
            ok = delete_admin(session, tg)
            if ok:
                await update.message.reply_text(f"✅ Адміна {tg} видалено.")
            else:
                await update.message.reply_text("❌ Адміна не знайдено.")
            return ConversationHandler.END

        # --- Add flow (if reused) ---
        elif action == "add_admin":
            existing = session.query(Admin).filter_by(tg_id=str(tg)).first()
            if existing:
                await update.message.reply_text(f"⚠️ Адмін із Telegram ID {tg} вже існує.")
                return ConversationHandler.END

            a = add_admin(session, tg_id=tg)
            await update.message.reply_text(f"✅ Новий адмін доданий: {tg}")
            return ConversationHandler.END


    finally:
        session.close()
    return ConversationHandler.END


async def process_admin_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = SessionLocal()
    try:
        tg = context.user_data.get("tg_id")
        name = update.message.text.strip()
        admin = session.query(Admin).filter_by(tg_id=tg).first()
        if not admin:
            await update.message.reply_text("❌ Адміністратора не знайдено.")
            return ConversationHandler.END

        if name:
            admin.name = name
        session.commit()

        await update.message.reply_text(f"✅ Ім’я оновлено: {name or '(без змін)'}")
        return ConversationHandler.END
    finally:
        session.close()


async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_is_admin(str(update.effective_user.id)):
        await update.message.reply_text("Доступ заборонено.")
        return
    session = SessionLocal()
    try:
        admins = session.query(Admin).all()
        text = "Адміни:\n"
        for a in admins:
            text += f"- {a.name or '—'} (tg_id: {a.tg_id})\n"
        await update.message.reply_text(text)
    finally:
        session.close()

async def add_company_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_is_admin(str(update.effective_user.id)):
        await update.message.reply_text("Доступ заборонено.")
        return
    # expecting: /add_company Назва|Контакт|ClientID|ClientSecret
    args = update.message.text.partition(" ")[2]
    if not args:
        await update.message.reply_text("Формат: /add_company Назва|Контакт|ClientID|ClientSecret (тільки Назва обов'язкова)")
        return
    parts = [p.strip() for p in args.split("|")]
    name = parts[0]
    contact = parts[1] if len(parts) > 1 else None
    cid = parts[2] if len(parts) > 2 else None
    csec = parts[3] if len(parts) > 3 else None
    session = SessionLocal()
    try:
        c = add_company(session, name=name, contact_name=contact, client_id=cid, client_secret=csec)
        await update.message.reply_text(f"Компанія додана: {c.name} (id={c.id})")
    finally:
        session.close()

async def list_companies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_is_admin(str(update.effective_user.id)):
        await update.message.reply_text("Доступ заборонено.")
        return
    session = SessionLocal()
    try:
        cs = session.query(Company).all()
        text = "Компанії:\n"
        for c in cs:
            text += f"- {c.id}: {c.name} (contact: {c.contact_name or '-'})\n"
        await update.message.reply_text(text)
    finally:
        session.close()

async def register_client_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /register_client tg_id|ім'я|company_id
    if not await ensure_is_admin(str(update.effective_user.id)):
        await update.message.reply_text("Доступ заборонено.")
        return
    args = update.message.text.partition(" ")[2]
    if not args:
        await update.message.reply_text("Формат: /register_client tg_id|ім'я|company_id")
        return
    parts = [p.strip() for p in args.split("|")]
    tg = parts[0]
    name = parts[1] if len(parts) > 1 else None
    comp_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    session = SessionLocal()
    try:
        c = add_client(session, tg_id=tg, name=name, company_id=comp_id)
        await update.message.reply_text(f"Клієнт збережено: {c.tg_id}")
    finally:
        session.close()

async def history_client_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_is_admin(str(update.effective_user.id)):
        await update.message.reply_text("Доступ заборонено.")
        return
    args = update.message.text.partition(" ")[2].strip()
    if not args:
        await update.message.reply_text("Формат: /history_client tg_id")
        return
    tg = str(args)
    session = SessionLocal()
    try:
        msgs = session.query(Message).filter_by(client_tg_id=tg).order_by(Message.created_at).all()
        if not msgs:
            await update.message.reply_text("Повідомлень не знайдено.")
            return
        text = f"Історія розмови з {tg}:\n"
        for m in msgs:
            dir_mark = "📥" if m.direction == "in" else "📤"
            text += f"{dir_mark} {m.created_at} {m.text}\n"
        await update.message.reply_text(text)
    finally:
        session.close()

async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("broadcast_active"):
        context.user_data.pop("broadcast_active", None)
        context.user_data.pop("broadcast", None)

    tg_id = str(update.effective_user.id)
    text = update.message.caption or (update.message.text.strip() if update.message and update.message.text else None)
    session = SessionLocal()

    claim_id = context.user_data.get("replying_claim_id")
    if not claim_id:
        await update.message.reply_text("⚠️ Відсутній активний запит для відповіді.")
        return

    file_id, file_type = None, None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        file_type = "photo"
    elif update.message.document:
        file_id = update.message.document.file_id
        file_type = "document"
    elif update.message.video:
        file_id = update.message.video.file_id
        file_type = "video"
    elif update.message.voice:
        file_id = update.message.voice.file_id
        file_type = "voice"

    file_type = file_type.lower() if file_type else None

    try:
        claim = session.query(Claim).filter_by(id=claim_id).first()
        if not claim:
            await update.message.reply_text("❌ Запит не знайдено.")
            return

        message = session.query(Message).filter_by(id=claim.message_id).first()
        client_tg_id = message.client_tg_id if message else None
        if not client_tg_id:
            await update.message.reply_text("❌ Не вдалося знайти клієнта.")
            return

        media_path = None
        if file_id:
            try:
                bot = context.bot
                file = await bot.get_file(file_id)
                ext = {
                    "photo": "jpg",
                    "document": "dat",
                    "video": "mp4",
                    "voice": "ogg"
                }.get(file_type, "bin")

                filename = f"{file_type}_{int(datetime.utcnow().timestamp())}_{tg_id}.{ext}"
                media_path = f"/data/media/{filename}"
                os.makedirs("/data/media", exist_ok=True)
                await file.download_to_drive(media_path)
                logger.info(f"📁 Медіа збережено: {media_path}")
            except Exception as e:
                logger.warning(f"⚠️ Не вдалося зберегти медіа: {e}")

        reply_msg = Message(
            client_tg_id=client_tg_id,
            direction="out",
            text=text,
            file_id=file_id,
            file_type=file_type,
            file_path=media_path,
            company_snapshot=message.company_snapshot if message else None
        )
        session.add(reply_msg)
        session.commit()

        client_bot = Bot(token=os.getenv("TELEGRAM_TOKEN_CLIENT"))

        try:
            # === ВІДПРАВКА МЕДІА ===
            if media_path and os.path.exists(media_path):
                with open(media_path, "rb") as f:
                    if file_type == "photo":
                        await client_bot.send_photo(chat_id=int(client_tg_id), photo=f, caption=f"💬 Відповідь від менеджера:\n{text or '(без тексту)'}")
                    elif file_type == "document":
                        await client_bot.send_document(chat_id=int(client_tg_id), document=f, caption=f"💬 Відповідь від менеджера:\n{text or '(без тексту)'}")
                    elif file_type == "video":
                        await client_bot.send_video(chat_id=int(client_tg_id), video=f, caption=f"💬 Відповідь від менеджера:\n{text or '(без тексту)'}")
                    elif file_type == "voice":
                        await client_bot.send_voice(chat_id=int(client_tg_id), voice=f, caption="💬 Відповідь від менеджера.")
                logger.info(f"🗑️ Видаляю медіа після відправки: {media_path}")
                os.remove(media_path)
            else:
                # === ВІДПРАВКА ТЕКСТУ ===
                await client_bot.send_message(chat_id=int(client_tg_id), text=f"💬 Відповідь від менеджера:\n{text}")

        # === ОБРОБКА ПОМИЛОК TELEGRAM API ===
        except TimedOut:
            logger.warning(f"⚠️ Telegram API timeout при надсиланні клієнту {client_tg_id}. Повтор через 5 секунд.")
            await asyncio.sleep(5)
            try:
                await client_bot.send_message(chat_id=int(client_tg_id), text=f"💬 Відповідь від менеджера (повторна спроба):\n{text}")
            except Exception as e:
                logger.error(f"❌ Повторна спроба не вдалася: {e}")

        except RetryAfter as e:
            delay = int(getattr(e, 'retry_after', 5))
            logger.warning(f"⚠️ Перевищено ліміт запитів. Чекаю {delay} секунд перед повтором.")
            await asyncio.sleep(delay)
            try:
                await client_bot.send_message(chat_id=int(client_tg_id), text=f"💬 Відповідь від менеджера:\n{text}")
            except Exception as e:
                logger.error(f"❌ Повтор після RateLimit не вдався: {e}")

        except NetworkError:
            logger.warning(f"🌐 Проблема з мережею під час відправки клієнту {client_tg_id}. Повідомлення пропущено.")

        except Exception as e:
            logger.exception(f"❌ Помилка при надсиланні клієнту {client_tg_id}: {e}")

        # --- Відповідь адміну ---
        await update.message.reply_text("✅ Відповідь надіслана клієнту.")
        context.user_data.pop("replying_claim_id", None)

    except Exception as e:
        logger.exception(f"❌ Помилка у handle_admin_reply: {e}")
        await update.message.reply_text("⚠️ Сталася помилка при надсиланні.")
    finally:
        session.close()


    
    
async def reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_is_admin(str(update.effective_user.id)):
        await update.message.reply_text("Доступ заборонено.")
        return

    # формат: /reply client_tg_id Текст відповіді
    args = update.message.text.partition(" ")[2]
    if not args:
        await update.message.reply_text("Формат: /reply client_tg_id Текст відповіді")
        return

    client_tg, _, text = args.partition(" ")
    if not text.strip():
        await update.message.reply_text("Вкажіть текст відповіді.")
        return

    session = SessionLocal()
    try:
        # 1) зберегти вихідне повідомлення у БД
        m = Message(client_tg_id=str(client_tg), admin_tg_id=str(update.effective_user.id), direction='out', text=text)
        session.add(m)
        session.commit()
        session.refresh(m)

        # 2) надіслати клієнту через bot з токеном client
        from telegram import Bot
        client_token = os.getenv("TELEGRAM_TOKEN_CLIENT")
        bot = Bot(token=client_token)
        try:
            await bot.send_message(chat_id=int(client_tg), text=f"Відповідь від адміністратора {update.effective_user.full_name}:\n\n{text}")
            await update.message.reply_text("Відправлено клієнту.")
        except Exception as e:
            # збережено у БД навіть якщо відправка не пройшла
            await update.message.reply_text(f"Не вдалося відправити клієнту: {e}")
    finally:
        session.close()

# --- ADMINS ---
async def update_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_is_admin(str(update.effective_user.id)):
        await update.message.reply_text("Доступ заборонено.")
        return
    # /update_admin tg_id|new_name|is_super(True/False)
    args = update.message.text.partition(" ")[2]
    if not args:
        await update.message.reply_text("Формат: /update_admin tg_id|new_name|is_super(True/False)")
        return
    parts = [p.strip() for p in args.split("|")]
    tg_id = parts[0]
    new_name = parts[1] if len(parts) > 1 else None
    is_super = None
    if len(parts) > 2:
        val = parts[2].lower()
        is_super = True if val in ["true", "1", "yes", "так"] else False
    session = SessionLocal()
    try:
        a = update_admin(session, tg_id=tg_id, new_name=new_name, is_super=is_super)
        if a:
            await update.message.reply_text(f"✅ Адмін оновлений: {a.tg_id} ({a.name})")
        else:
            await update.message.reply_text("❌ Адміна не знайдено.")
    finally:
        session.close()

async def delete_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_is_admin(str(update.effective_user.id)):
        await update.message.reply_text("Доступ заборонено.")
        return
    args = update.message.text.partition(" ")[2].strip()
    if not args:
        await update.message.reply_text("Формат: /delete_admin tg_id")
        return
    tg_id = args
    session = SessionLocal()
    try:
        ok = delete_admin(session, tg_id)
        if ok:
            await update.message.reply_text(f"✅ Адмін {tg_id} видалений.")
        else:
            await update.message.reply_text("❌ Адміна не знайдено.")
    finally:
        session.close()

# --- COMPANIES ---
async def update_company_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_is_admin(str(update.effective_user.id)):
        await update.message.reply_text("Доступ заборонено.")
        return
    args = update.message.text.partition(" ")[2]
    if not args:
        await update.message.reply_text("Формат: /update_company id|name|contact|client_id|client_secret")
        return
    parts = [p.strip() for p in args.split("|")]
    company_id = int(parts[0])
    name = parts[1] if len(parts) > 1 else None
    contact = parts[2] if len(parts) > 2 else None
    cid = parts[3] if len(parts) > 3 else None
    csec = parts[4] if len(parts) > 4 else None
    session = SessionLocal()
    try:
        c = update_company(session, company_id, name, contact, cid, csec)
        if c:
            await update.message.reply_text(f"✅ Компанія оновлена: {c.name} (id={c.id})")
        else:
            await update.message.reply_text("❌ Компанію не знайдено.")
    finally:
        session.close()

async def delete_company_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_is_admin(str(update.effective_user.id)):
        await update.message.reply_text("Доступ заборонено.")
        return
    args = update.message.text.partition(" ")[2].strip()
    if not args:
        await update.message.reply_text("Формат: /delete_company company_id")
        return
    company_id = int(args)
    session = SessionLocal()
    try:
        ok = delete_company(session, company_id)
        if ok:
            await update.message.reply_text(f"✅ Компанію {company_id} видалено.")
        else:
            await update.message.reply_text("❌ Компанію не знайдено.")
    finally:
        session.close()

# --- CLIENTS ---
async def update_client_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_is_admin(str(update.effective_user.id)):
        await update.message.reply_text("Доступ заборонено.")
        return
    args = update.message.text.partition(" ")[2]
    if not args:
        await update.message.reply_text("Формат: /update_client tg_id|name|company_id")
        return
    parts = [p.strip() for p in args.split("|")]
    tg_id = parts[0]
    name = parts[1] if len(parts) > 1 else None
    company_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    session = SessionLocal()
    try:
        c = update_client(session, tg_id, name, company_id)
        if c:
            await update.message.reply_text(f"✅ Клієнт оновлений: {c.tg_id}")
        else:
            await update.message.reply_text("❌ Клієнта не знайдено.")
    finally:
        session.close()

async def delete_client_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_is_admin(str(update.effective_user.id)):
        await update.message.reply_text("Доступ заборонено.")
        return
    args = update.message.text.partition(" ")[2].strip()
    if not args:
        await update.message.reply_text("Формат: /delete_client tg_id")
        return
    tg_id = args
    session = SessionLocal()
    try:
        ok = delete_client(session, tg_id)
        if ok:
            await update.message.reply_text(f"✅ Клієнт {tg_id} видалений.")
        else:
            await update.message.reply_text("❌ Клієнта не знайдено.")
    finally:
        session.close()

async def handle_crud_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("replying_claim_id"):
        # Якщо адмін у режимі відповіді — передаємо на handle_admin_reply
        return await handle_admin_reply(update, context)
    action = context.user_data.get("action")
    if not action:
        return

    session = SessionLocal()
    text = update.message.text.strip()

    try:
        # --- Companies ---
        if action == "add_company_menu":
            parts = [p.strip() for p in text.split("|")]
            c = add_company(session, name=parts[0], contact_name=parts[1] if len(parts) > 1 else None,
                            client_id=parts[2] if len(parts) > 2 else None,
                            client_secret=parts[3] if len(parts) > 3 else None)
            await update.message.reply_text(f"✅ Компанія '{c.name}' додана (id={c.id})")

        elif action == "update_company_menu":
            parts = [p.strip() for p in text.split("|")]
            cid = int(parts[0])
            c = update_company(session, cid, parts[1] if len(parts) > 1 else None,
                               parts[2] if len(parts) > 2 else None,
                               parts[3] if len(parts) > 3 else None,
                               parts[4] if len(parts) > 4 else None)
            await update.message.reply_text(f"✅ Компанія {cid} оновлена." if c else "❌ Не знайдено.")

        elif action == "delete_company_menu":
            ok = delete_company(session, int(text))
            await update.message.reply_text("✅ Компанію видалено." if ok else "❌ Не знайдено.")

        # --- Clients ---
        elif action == "add_client_menu":
            parts = [p.strip() for p in text.split("|")]
            c = add_client(session, tg_id=parts[0], name=parts[1] if len(parts) > 1 else None,
                           company_id=int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None)
            await update.message.reply_text(f"✅ Клієнт {c.name or c.tg_id} доданий.")

        elif action == "update_client_menu":
            parts = [p.strip() for p in text.split("|")]
            c = update_client(session, parts[0], parts[1] if len(parts) > 1 else None,
                              int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None)
            await update.message.reply_text("✅ Клієнт оновлений." if c else "❌ Не знайдено.")

        elif action == "delete_client_menu":
            ok = delete_client(session, text)
            await update.message.reply_text("✅ Клієнта видалено." if ok else "❌ Не знайдено.")

    except Exception as e:
        await update.message.reply_text(f"⚠️ Помилка: {e}")
        raise
    finally:
        session.close()
        context.user_data["action"] = None

async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Головний обробник будь-яких повідомлень від адміна.
    Пріоритет: 1) reply (replying_claim_id)  2) CRUD action  3) broadcast_active  4) handle_admin_reply
    """
    # 1) Якщо адмін взяв claim — відповіді мають бути оброблені насамперед
    if context.user_data.get("replying_claim_id"):
        logger.debug("ℹ️ Повідомлення обробляється як відповідь на claim (replying_claim_id).")
        return await handle_admin_reply(update, context)

    # 1.1 Якщо був прапорець broadcast_active без даних — чистимо (завислий стан)
    if context.user_data.get("broadcast_active") and not context.user_data.get("broadcast"):
        logger.debug("⚠️ Виявлено прапорець broadcast_active без broadcast -> очищаю.")
        context.user_data.pop("broadcast_active", None)

    # 2) Якщо в користувача зараз CRUD-дія — передаємо в CRUD
    action = context.user_data.get("action")
    if action in [
        "add_company_menu", "update_company_menu", "delete_company_menu",
        "add_client_menu", "update_client_menu", "delete_client_menu"
    ]:
        return await handle_crud_input(update, context)

    # 3) Якщо реально в режимі broadcast — нехай broadcast flow обробляє, інакше reply
    if context.user_data.get("broadcast_active"):
        # якщо broadcast_active є — але conversation вже не активний, то очищаємо прапорець
        logger.debug("📣 Адмін в режимі broadcast — повідомлення ігнорується (очікується ввід розсилки).")
        return  # ігноруємо повідомлення для admin chat під час вводу тексту розсилки

    # 4) За замовчуванням — обробка відповіді клієнтові
    context.user_data["reply_mode_active"] = False
    context.user_data.pop("replying_claim_id", None)
    logger.info("✅ [REPLY_MODE] Вимкнено після відправлення повідомлення клієнту.")

    return await handle_admin_reply(update, context)


async def set_admin_commands(app):
    await app.bot.set_my_commands([
        BotCommand("start1", "🔹 Запустити адмін-бота"),
        BotCommand("help_admin", "ℹ️ Допомога по командам"),
    ])
    logger.info("✅ Команди /start і /help_admin додані в меню Telegram")

def run_admin_bot():
    app = ApplicationBuilder().token(ADMIN_TOKEN).post_init(set_admin_commands).build()

    # --- 🧭 Основні команди ---
    app.add_handler(CommandHandler("start1", start_admin))
    app.add_handler(CommandHandler("help_admin", help_admin))
    app.add_handler(CommandHandler("start_admin", start_admin))
#   app.add_handler(CommandHandler("cancel", broadcast_cancel_callback))
    #app.add_handler(CallbackQueryHandler(unknown_callback))

    # --- 🏢 CRUD-команди ---
    app.add_handler(CommandHandler("add_company", add_company_cmd))
    app.add_handler(CommandHandler("list_companies", list_companies))
    app.add_handler(CommandHandler("register_client", register_client_cmd))
    app.add_handler(CommandHandler("history_client", history_client_cmd))
    app.add_handler(CommandHandler("reply", reply_cmd))

    # --- 💬 Callback для кнопки "Відповісти" ---
    app.add_handler(CallbackQueryHandler(claim_callback, pattern=r"^claim:\d+$"))

    # --- 👥 CRUD адміністраторів (окремий ConversationHandler) ---
    admin_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_menu_callback, pattern="^(add_admin|update_admin|delete_admin)$"),
        ],
        states={
            ASK_CONTACT: [MessageHandler(filters.CONTACT | (filters.TEXT & ~filters.COMMAND), receive_contact)],
            ASK_ADMIN_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_admin_input)],
            ASK_ADMIN_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_admin_name)],
        },
        fallbacks=[],
        per_chat=True,
        per_user=True,
        per_message=False,
    )

    app.add_handler(admin_conv)

    # --- 📣 Масова розсилка ---
    broadcast_conv = ConversationHandler(
        name="broadcast_conv",
        entry_points=[
            CallbackQueryHandler(start_broadcast_callback, pattern="^broadcast$")
        ],
        states={
            ASK_BROADCAST_TEXT: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VOICE | filters.VIDEO | filters.AUDIO | filters.Document.ALL)
                    & ~filters.COMMAND,
                    handle_broadcast_input
                ),
                CommandHandler("cancel", broadcast_cancel_callback),
            ],
            ASK_BROADCAST_CONFIRM: [
                CallbackQueryHandler(broadcast_confirm_callback, pattern="^broadcast_confirm$"),
                CallbackQueryHandler(broadcast_cancel_callback, pattern="^broadcast_cancel$"),
            ],
        },
        fallbacks=[],
        per_chat=True,
        per_user=True,
        per_message=False,
    )
    # --- ➕ Додавання клієнта ---
    add_client_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_menu_callback, pattern="^add_client_menu$")],
        states={
            ASK_CLIENT_CONTACT: [
                MessageHandler(filters.CONTACT | (filters.TEXT & ~filters.COMMAND), handle_client_contact)
            ],
            ASK_CLIENT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_client_name)
            ],
            ASK_CLIENT_COMPANY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_client_company)
            ],
        },
        fallbacks=[],
        per_chat=True,
        per_user=True,
        per_message=False,
    )
    app.add_handler(broadcast_conv)

    app.add_handler(add_client_conv)

    # --- 📎 Обробка медіа/тексту поза станами ---
    MEDIA_FILTERS = (
        filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.VOICE | filters.AUDIO
    )

    app.add_handler(MessageHandler(
        (filters.TEXT | MEDIA_FILTERS) & ~filters.COMMAND,
        handle_admin_message
    ))
    app.add_handler(CallbackQueryHandler(reset_states_callback, pattern="^reset_states$"))
    # --- 🧩 Callback для решти меню ---
    app.add_handler(CallbackQueryHandler(view_history_paginated, pattern=r"^history_page:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_menu_callback, pattern=r'^(?!add_admin$|update_admin$|delete_admin$|broadcast$|add_client_menu$|claim:).+'))

    logger.info("✅ Запускаю admin bot")
    app.run_polling()








