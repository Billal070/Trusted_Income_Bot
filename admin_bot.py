"""
Admin Bot - manages users, credits, broadcasts, and photo pool.
Only users listed in ADMIN_USER_IDS may use this bot.
"""

import hashlib
import logging
import telegram

logger = logging.getLogger(__name__)
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import database as db
from config import ADMIN_BOT_TOKEN, ADMIN_USER_IDS, USER_BOT_TOKEN


# -- Helpers ---------------------------------------------------

def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


def _user_bot():
    """Lightweight Bot instance using the User Bot token (for broadcasts / credit notifications)."""
    return telegram.Bot(token=USER_BOT_TOKEN)


# -- Keyboard --------------------------------------------------

ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("\U0001f50d Search User"), KeyboardButton("\U0001f4e2 Broadcast")],
        [KeyboardButton("\U0001f4ca Stats")],
    ],
    resize_keyboard=True,
)


# -- /start ----------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return await update.message.reply_text("\u26d4 Unauthorized.")
    await update.message.reply_text("Admin panel ready.", reply_markup=ADMIN_KEYBOARD)


# -- Search User -----------------------------------------------

async def search_user_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return await update.message.reply_text("\u26d4 Unauthorized.")
    context.user_data["admin_state"] = "awaiting_search"
    await update.message.reply_text("Send the user's Telegram ID or @username.")


async def handle_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    if context.user_data.get("admin_state") != "awaiting_search":
        return
    context.user_data["admin_state"] = None

    identifier = update.message.text.strip()
    user = db.search_user(identifier)
    if not user:
        return await update.message.reply_text("\u274c User not found.")

    status = "Active" if not user["is_banned"] else "Banned"
    text = (
        f"\U0001f464 User Info\n"
        f"ID: {user['user_id']}\n"
        f"Username: @{user['username'] or 'N/A'}\n"
        f"Balance: {user['credits']} credits\n"
        f"Status: {status}"
    )
    uid = user["user_id"]
    ban_label = "\u2705 Unban" if user["is_banned"] else "\U0001f6ab Ban"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\u2795 Add Credit", callback_data=f"addcredit:{uid}"),
            InlineKeyboardButton("\u2796 Deduct Credit", callback_data=f"deductcredit:{uid}"),
        ],
        [InlineKeyboardButton(ban_label, callback_data=f"toggleban:{uid}")],
    ])
    await update.message.reply_text(text, reply_markup=keyboard)


# -- Inline callbacks: Add / Deduct / Ban ---------------------

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query: CallbackQuery = update.callback_query
    if not _is_admin(update.effective_user.id):
        return await query.answer("\u26d4 Unauthorized.", show_alert=True)

    await query.answer()
    data = query.data
    parts = data.split(":")
    action = parts[0]
    uid = int(parts[1])

    if action == "addcredit":
        context.user_data["admin_state"] = f"awaiting_addcredit:{uid}"
        await query.edit_message_text(f"Enter amount to ADD to user {uid}:")
    elif action == "deductcredit":
        context.user_data["admin_state"] = f"awaiting_deductcredit:{uid}"
        await query.edit_message_text(f"Enter amount to DEDUCT from user {uid}:")
    elif action == "toggleban":
        user = db.get_user(uid)
        if not user:
            return await query.edit_message_text("\u274c User not found.")
        new_banned = not user["is_banned"]
        db.set_banned(uid, new_banned)
        label = "banned" if new_banned else "unbanned"
        await query.edit_message_text(f"\u2705 User {uid} has been {label}.")


async def handle_credit_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    state = context.user_data.get("admin_state", "")
    if not state.startswith("awaiting_"):
        return

    text = update.message.text.strip()
    if not text.isdigit():
        return await update.message.reply_text("\u26a0\ufe0f Please enter a valid number.")

    amount = int(text)
    parts = state.split(":")
    action = parts[0]
    uid = int(parts[1])
    admin_id = update.effective_user.id

    if action == "awaiting_addcredit":
        new_balance = db.add_credit(uid, amount, admin_id)
        from user_bot import notify_user_credit_change
        await notify_user_credit_change(uid, amount, "add", new_balance)
        await update.message.reply_text(f"\u2705 Added {amount} credits to user {uid}. New balance: {new_balance}")
    elif action == "awaiting_deductcredit":
        new_balance = db.deduct_credit(uid, amount, admin_id)
        from user_bot import notify_user_credit_change
        await notify_user_credit_change(uid, amount, "deduct", new_balance)
        await update.message.reply_text(f"\u2705 Deducted {amount} credits from user {uid}. New balance: {new_balance}")

    context.user_data["admin_state"] = None


# -- Broadcast -------------------------------------------------

async def broadcast_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return await update.message.reply_text("\u26d4 Unauthorized.")
    context.user_data["admin_state"] = "awaiting_broadcast"
    await update.message.reply_text("Send the message you want to broadcast to all users.")


async def handle_broadcast_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    if context.user_data.get("admin_state") != "awaiting_broadcast":
        return
    context.user_data["admin_state"] = None

    user_ids = db.get_all_user_ids()
    total = len(user_ids)
    sent = 0
    bot = _user_bot()
    message = update.message
    logger.info("Broadcast started: %d users to send to", total)

    for uid in user_ids:
        try:
            if message.text:
                await bot.send_message(chat_id=uid, text=message.text)
            elif message.photo:
                await bot.send_photo(chat_id=uid, photo=message.photo[-1].file_id, caption=message.caption or "")
            elif message.document:
                await bot.send_document(chat_id=uid, document=message.document.file_id, caption=message.caption or "")
            else:
                continue
            sent += 1
        except telegram.error.Forbidden:
            logger.warning("Broadcast: user %d has not started the User Bot or blocked it", uid)
            continue
        except Exception as e:
            logger.error("Broadcast failed for user %d: %s", uid, e)
            continue

    await update.message.reply_text(f"\u2705 Broadcast sent to {sent}/{total} users.")


# -- Stats -----------------------------------------------------

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return await update.message.reply_text("\u26d4 Unauthorized.")
    s = db.get_stats()
    text = (
        f"\U0001f4ca Bot Statistics\n\n"
        f"\U0001f465 Total Users: {s['total_users']}\n"
        f"\U0001f4b0 Credits in Circulation: {s['total_credits']}\n"
        f"\U0001f4f7 Photos Available: {s['photos_available']}\n"
        f"\U0001f4e4 Photos Sent: {s['photos_sent']}\n"
        f"\U0001f4dd Pending Submissions: {s['pending_submissions']}"
    )
    await update.message.reply_text(text)


# -- Photo auto-add --------------------------------------------
# Any photo sent to the Admin Bot is automatically added to the photo pool.
# For duplicate detection, we compute a SHA-256 hash of the photo bytes
# and reject the insert if that hash already exists in the photos table.

async def auto_add_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    if not update.message.photo:
        return
    # Skip if we're in addphoto command state (catch_admin_input will handle it)
    if context.user_data.get("admin_state") == "awaiting_addphoto":
        return

    photo = update.message.photo[-1]
    try:
        tg_file = await context.bot.get_file(photo.file_id)
        photo_bytes = await tg_file.download_as_bytearray()
        file_hash = hashlib.sha256(photo_bytes).hexdigest()
    except Exception:
        file_hash = None

    added = db.add_photo(photo.file_id, file_hash)
    if added:
        await update.message.reply_text("\u2705 Photo added to pool.")
    else:
        await update.message.reply_text("\u26a0\ufe0f Duplicate photo (same content already in pool). Skipped.")


async def addphoto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Explicit /addphoto command - same as auto-add but triggered by command."""
    if not _is_admin(update.effective_user.id):
        return await update.message.reply_text("\u26d4 Unauthorized.")
    context.user_data["admin_state"] = "awaiting_addphoto"
    await update.message.reply_text("Send the photo you want to add to the pool.")


# -- /pending --------------------------------------------------

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return await update.message.reply_text("\u26d4 Unauthorized.")
    subs = db.get_pending_submissions()
    if not subs:
        return await update.message.reply_text("\u2705 No pending submissions.")

    lines = ["\U0001f4dd Pending Submissions:\n"]
    for s in subs:
        lines.append(f"\u2022 #{s['id']} | User: {s['user_id']} | Type: {s['content_type']} | {s['submitted_at']}")
    await update.message.reply_text("\n".join(lines))


# -- Catch-all for admin text input ----------------------------

async def catch_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("admin_state", "")
    logger.info("catch_admin_input: state=%s, user=%d", state, update.effective_user.id)
    if state.startswith("awaiting_search"):
        await handle_search_input(update, context)
    elif state.startswith("awaiting_addcredit") or state.startswith("awaiting_deductcredit"):
        await handle_credit_input(update, context)
    elif state == "awaiting_broadcast":
        await handle_broadcast_input(update, context)
    elif state == "awaiting_addphoto":
        if update.message.photo:
            context.user_data["admin_state"] = None
            await auto_add_photo(update, context)
        else:
            await update.message.reply_text("\u26a0\ufe0f Please send a photo.")


# -- Build application -----------------------------------------

def build_admin_bot() -> Application:
    app = Application.builder().token(ADMIN_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addphoto", addphoto_command))
    app.add_handler(CommandHandler("pending", pending_command))

    app.add_handler(MessageHandler(filters.Regex("^\U0001f50d Search User$"), search_user_prompt))
    app.add_handler(MessageHandler(filters.Regex("^\U0001f4e2 Broadcast$"), broadcast_prompt))
    app.add_handler(MessageHandler(filters.Regex("^\U0001f4ca Stats$"), stats))

    app.add_handler(CallbackQueryHandler(callback_handler))

    # Photo auto-add: any photo from an admin
    app.add_handler(MessageHandler(filters.PHOTO & filters.User(ADMIN_USER_IDS), auto_add_photo))

    # Catch-all for admin state-driven text input (must be last)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_USER_IDS),
        catch_admin_input,
    ))

    return app