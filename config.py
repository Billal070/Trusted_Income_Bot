import os
from dotenv import load_dotenv

load_dotenv()

USER_BOT_TOKEN = os.environ["USER_BOT_TOKEN"]
ADMIN_BOT_TOKEN = os.environ["ADMIN_BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
ADMIN_USER_IDS = [int(x.strip()) for x in os.environ["ADMIN_USER_IDS"].split(",") if x.strip()]
DB_PATH = os.environ.get("DB_PATH", "database.db")
BKASH_NUMBER = os.environ.get("BKASH_NUMBER", "017XXXXXXXX")
NAGAD_NUMBER = os.environ.get("NAGAD_NUMBER", "018XXXXXXXX")
SUPPORT_LINK = os.environ.get("SUPPORT_LINK", "https://t.me/your_support")
# Google Sheets Product Store
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "Trusted Income Bot")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
GOOGLE_CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "")
PRODUCT_PRICE = float(os.environ.get("PRODUCT_PRICE", "1.5"))
