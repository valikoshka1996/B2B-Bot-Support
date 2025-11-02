import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "/data/support_bot.db")
DB_URI = f"sqlite:///{DB_PATH}"

# ensure dir exists
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

engine = create_engine(DB_URI, connect_args={"check_same_thread": False})
SessionLocal = scoped_session(sessionmaker(bind=engine))
