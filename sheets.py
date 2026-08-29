import json
import asyncio
import logging
from datetime import datetime, timezone
import gspread
from google.oauth2.service_account import Credentials
from config import GOOGLE_SHEET_ID, GOOGLE_SHEET_NAME, GOOGLE_CREDENTIALS_JSON, GOOGLE_CREDENTIALS_FILE
logger = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_alloc_lock = asyncio.Lock()
def _get_credentials():
    if GOOGLE_CREDENTIALS_JSON:
        try:
            info = json.loads(GOOGLE_CREDENTIALS_JSON)
            return Credentials.from_service_account_info(info, scopes=SCOPES)
        except Exception as e:
            logger.error("Failed to parse GOOGLE_CREDENTIALS_JSON: %s", e)
            raise
    if GOOGLE_CREDENTIALS_FILE:
        return Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
    raise RuntimeError("Google credentials not configured: set GOOGLE_CREDENTIALS_JSON or GOOGLE_CREDENTIALS_FILE")
def _get_worksheet():
    creds = _get_credentials()
    client = gspread.authorize(creds)
    if GOOGLE_SHEET_ID:
        sh = client.open_by_key(GOOGLE_SHEET_ID)
    else:
        sh = client.open(GOOGLE_SHEET_NAME)
    try:
        ws = sh.worksheet(GOOGLE_SHEET_NAME)
    except Exception:
        ws = sh.sheet1
    return ws
def _now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
def count_available():
    ws = _get_worksheet()
    values = ws.get_all_values()
    if not values:
        return 0
    start = 0
    if len(values) > 0 and len(values[0]) >= 2:
        hdr_b = values[0][1].strip().lower() if len(values[0])>1 else ""
        if "telegram" in hdr_b or "username" in hdr_b or "sold" in hdr_b.lower():
            start = 1
    cnt = 0
    for row in values[start:]:
        col_a = row[0].strip() if len(row)>0 else ""
        col_b = row[1].strip() if len(row)>1 else ""
        if col_a and not col_b:
            cnt += 1
    return cnt
async def allocate_items(username: str, quantity: int):
    if not username.startswith("@"):
        username = "@" + username.lstrip("@") if username else "@unknown"
    async with _alloc_lock:
        ws = _get_worksheet()
        values = ws.get_all_values()
        if not values:
            raise ValueError("Sheet is empty")
        start = 0
        if len(values) > 0 and len(values[0]) >= 2:
            hdr_b = values[0][1].strip().lower() if len(values[0])>1 else ""
            if "telegram" in hdr_b or "username" in hdr_b or "sold" in hdr_b.lower():
                start = 1
        available = []
        for idx, row in enumerate(values):
            if idx < start:
                continue
            col_a = row[0].strip() if len(row)>0 else ""
            col_b = row[1].strip() if len(row)>1 else ""
            if col_a and not col_b:
                available.append((idx+1, col_a))
                if len(available) >= quantity:
                    break
        if len(available) < quantity:
            raise ValueError(f"Only {len(available)} items available, requested {quantity}")
        selected = available[:quantity]
        ts = _now_str()
        updates = []
        for row_num, _item in selected:
            updates.append({"range": f"B{row_num}", "values": [[username]]})
            updates.append({"range": f"C{row_num}", "values": [[ts]]})
        if updates:
            ws.batch_update(updates, value_input_option="USER_ENTERED")
        items = [item for _row, item in selected]
        logger.info("Allocated %s items to %s", len(items), username)
        return items
