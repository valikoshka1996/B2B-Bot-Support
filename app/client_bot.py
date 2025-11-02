import os
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()


from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from .db import SessionLocal
from .models import Client, Message, Company, Admin
from .utils import init_db
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ADMIN_TOKEN = os.getenv("TELEGRAM_TOKEN_ADMIN")
CLIENT_TOKEN = os.getenv("TELEGRAM_TOKEN_CLIENT")
INITIAL_ADMIN = os.getenv("INITIAL_ADMIN_ID")
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "support@yourcompany.com")
DB_PATH = os.getenv("DB_PATH", "/data/support_bot.db")

# ensure DB + initial admin
init_db(initial_admin_tg_id=INITIAL_ADMIN)

# We will use two Bot instances: one for client (handles user chats) and one admin_bot to notify admins
admin_bot = Bot(token=ADMIN_TOKEN)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(tg_id=tg_id).first()
        if not client:
            await update.message.reply_text(
                f"Ви не зареєстровані в системі як наш Б2Б клієнт. Прохання звернутися з запитом: {SUPPORT_EMAIL}"
            )
            return
        # client exists -> show info
        comp = client.company
        text = f"Назва компанії: {comp.name if comp else '—'}\n"
        text += f"ClientID: {comp.client_id if comp else '—'}\n"
        text += f"ClientSecret: {comp.client_secret if comp else '—'}\n"
        text += f"Ім'я: {client.name or update.effective_user.full_name}\n"
        await update.message.reply_text(text)
    finally:
        session.close()

async def handle_client_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    
    tg_id = str(update.effective_user.id)
    session = SessionLocal()

    try:
        client = session.query(Client).filter_by(tg_id=tg_id).first()
        if not client:
            await update.message.reply_text(
                f"Ви не зареєстровані в системі як наш Б2Б клієнт. "
                f"Прохання звернутися з запитом: {SUPPORT_EMAIL}"
            )
            return
    
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

        company_name = client.company.name if client.company else f"(ID: {client.company_id or 'невідомо'})"

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
        
        msg = Message(
            client_tg_id=tg_id,
            direction='in',
            text=text,
            file_id=file_id,
            file_type=file_type,
            file_path=media_path,
            company_snapshot=company_name
        )        
            
        session.add(msg)
        session.commit()
        session.refresh(msg)

        admins = session.query(Admin.tg_id).all()
        notify_text = (
            f"📩 Нове повідомлення від клієнта <b>{client.name or update.effective_user.full_name}</b>\n"
            f"🏢 Компанія: {company_name}\n"
            f"🆔 TG ID: <code>{tg_id}</code>\n\n"
            f"💬 {text or '(без тексту)'}"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Відповісти", callback_data=f"claim:{msg.id}")]])
    
        admin_bot = Bot(token=os.getenv("TELEGRAM_TOKEN_ADMIN"))
        for a in admins:
            try:
                if media_path and os.path.exists(media_path):
                    with open(media_path, "rb") as f:
                        if file_type == "photo":
                            await admin_bot.send_photo(chat_id=int(a[0]), photo=f, caption=notify_text, parse_mode="HTML", reply_markup=keyboard)
                        elif file_type == "document":
                            await admin_bot.send_document(chat_id=int(a[0]), document=f, caption=notify_text, parse_mode="HTML", reply_markup=keyboard)
                        elif file_type == "video":
                            await admin_bot.send_video(chat_id=int(a[0]), video=f, caption=notify_text, parse_mode="HTML", reply_markup=keyboard)
                        elif file_type == "voice":
                            await admin_bot.send_voice(chat_id=int(a[0]), voice=f, caption=notify_text, parse_mode="HTML", reply_markup=keyboard)
                else:
                    await admin_bot.send_message(chat_id=int(a[0]), text=notify_text, parse_mode="HTML", reply_markup=keyboard)
            except Exception as e:
                logger.warning(f"⚠️ Не вдалося надіслати адміну {a[0]}: {e}")

        # 🔥 Після успішної розсилки всім адмінам — видаляємо локальний файл
        if media_path and os.path.exists(media_path):
            try:
                logger.info(f"🗑️ Видаляю медіа після відправки: {media_path}")
                os.remove(media_path)
            except Exception as e:
                logger.warning(f"⚠️ Не вдалося видалити {media_path}: {e}")
    
        await update.message.reply_text("✅ Ваше повідомлення надіслано менеджерам. Очікуйте відповіді.")

    finally:
        session.close()

            
def run_client_bot():
    app = ApplicationBuilder().token(CLIENT_TOKEN).build()
    
    # --- Команди ---
    app.add_handler(CommandHandler("start", start))
    
    # --- Обробник текстових повідомлень ---
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_client_message))
    
    # --- Обробник МЕДІА ---
    app.add_handler(MessageHandler(
        (
            filters.PHOTO |
            filters.VIDEO |
            filters.AUDIO |
            filters.VOICE |
            filters.ATTACHMENT
        ),
        handle_client_message
    ))

    
    logger.info("Запускаю client bot")
    app.run_polling()
