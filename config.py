import os
from dotenv import load_dotenv

load_dotenv()

USER_BOT_TOKEN = os.environ["USER_BOT_TOKEN"]
ADMIN_BOT_TOKEN = os.environ["ADMIN_BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
ADMIN_USER_IDS = [int(x.strip()) for x in os.environ["ADMIN_USER_IDS"].split(",") if x.strip()]
DB_PATH = os.environ.get("DB_PATH", "database.db")
