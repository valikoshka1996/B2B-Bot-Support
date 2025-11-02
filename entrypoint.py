import os
from dotenv import load_dotenv
from app.db import engine
from app.models import Base

Base.metadata.create_all(bind=engine)

load_dotenv()

BOT_TYPE = os.getenv("BOT_TYPE", "client").lower()

if BOT_TYPE == "admin":
    from app.admin_bot import run_admin_bot as run
elif BOT_TYPE == "client":
    from app.client_bot import run_client_bot as run
else:
    raise RuntimeError("Unknown BOT_TYPE. Use 'admin' or 'client'")

if __name__ == "__main__":
    run()
