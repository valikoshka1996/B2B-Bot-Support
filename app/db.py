import os
import logging
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, scoped_session
from dotenv import load_dotenv
from sqlalchemy.exc import OperationalError

# === Логи ===
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s"
)

load_dotenv()

DB_USER = os.getenv("DB_USER", "b2b_support")
DB_PASSWORD = os.getenv("DB_PASSWORD", "ExWdzmWGR5EmEGLF")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_NAME = os.getenv("DB_NAME", "b2b_support")
DB_PORT = os.getenv("DB_PORT", "3306")

# === Пробуємо MySQL ===
DB_URI_MYSQL = f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4"

try:
    engine = create_engine(DB_URI_MYSQL, pool_pre_ping=True)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    logging.info("✅ Підключено до MySQL!")
except OperationalError as e:
    logging.warning(f"⚠️ Не вдалося підключитись до MySQL: {e}")
    logging.info("➡️ Перехід на SQLite...")

    # === SQLite fallback ===
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    SQLITE_PATH = os.path.join(BASE_DIR, "data", "b2b_fallback.db")
    os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)

    DB_URI_SQLITE = f"sqlite:///{SQLITE_PATH}"
    engine = create_engine(DB_URI_SQLITE, connect_args={"check_same_thread": False})
    logging.info(f"✅ Підключено до SQLite ({SQLITE_PATH})")

# === Сесія ===
SessionLocal = scoped_session(sessionmaker(bind=engine))
