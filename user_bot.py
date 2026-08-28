"""
User Bot - the bot regular users interact with.
"""

import telegram
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import database as db
from config import USER_BOT_TOKEN, ADMIN_BOT_TOKEN, ADMIN_CHAT_ID

# -- Cross-bot helper ------------------------------------------

def _admin_bot():
    """Lightweight Bot instance for the Admin Bot token (used to relay submissions)."""
    return telegram.Bot(token=ADMIN_BOT_TOKEN)


# -- Credit notification (called from Admin Bot side) ---------

async def notify_user_credit_change(user_id: int, amount: int, action: str, new_balance: int):
    """Send a credit-change notification to a user via the User Bot's own token."""
    if action == "add":
        text = f"✅ {amount} credits has been added to your account by admin! New balance: {new_balance}"
    else:
        text = f"➖ {amount} credits have been deducted from your account. New balance: {new_balance}"
    try:
        async with telegram.Bot(token=USER_BOT_TOKEN) as bot:
            await bot.send_message(chat_id=user_id, text=text)
    except telegram.error.Forbidden:
        pass
    except Exception:
        pass


# -- Keyboard --------------------------------------------------

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("\U0001f4f8 Get Pic"), KeyboardButton("\U0001f4dd Submit Job")],
        [KeyboardButton("\U0001f4b0 Balance")],
    ],
    resize_keyboard=True,
)


# -- /start ----------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.register_user(user.id, user.username, user.first_name)
    if db.is_banned(user.id):
        await update.message.reply_text("\U0001f6ab You are banned from using this bot.")
        return
    await update.message.reply_text("Welcome! Use the buttons below.", reply_markup=MAIN_KEYBOARD)


# -- Get Pic ---------------------------------------------------

async def get_pic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if db.is_banned(user_id):
        await update.message.reply_text("\U0001f6ab You are banned from using this bot.")
        return

    credits = db.get_credits(user_id)
    if credits < 1:
        await update.message.reply_text("\u274c Insufficient credit. Contact admin to top up.")
        return

    result = db.deduct_user_credit_for_pic(user_id)
    if result[0] is False:
        await update.message.reply_text("\U0001f614 No photos available right now. Please check back later.")
        return

    _, file_id, new_balance = result
    await update.message.reply_photo(photo=file_id)
    await update.message.reply_text(
        f"\u2705 Here's your photo! 1 credit deducted. Remaining balance: {new_balance}"
    )


# -- Submit Job ------------------------------------------------

async def submit_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if db.is_banned(update.effective_user.id):
        await update.message.reply_text("\U0001f6ab You are banned from using this bot.")
        return
    context.user_data["awaiting_submission"] = True
    await update.message.reply_text(
        "\U0001f4dd Please send your task now.\n"
        "You can send text, a photo, or a document \u2014 just send it as your next message."
    )


async def handle_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_submission"):
        return
    context.user_data["awaiting_submission"] = False

    user = update.effective_user
    message = update.message

    if message.text:
        content_type = "text"
        content = message.text
        await _relay_submission(user, content_type, content)
    elif message.photo:
        content_type = "photo"
        content = message.photo[-1].file_id
        await _relay_submission(user, content_type, content)
    elif message.document:
        content_type = "document"
        content = message.document.file_id
        await _relay_submission(user, content_type, content)
    else:
        await message.reply_text("\u26a0\ufe0f Unsupported type. Please send text, a photo, or a document.")
        return

    await message.reply_text("\u2705 Your task has been submitted successfully!")


async def _relay_submission(user, content_type: str, content: str):
    db.add_submission(user.id, content_type, content)

    caption = f"\U0001f4e5 New Job Submission\nFrom: @{user.username} (ID: {user.id})"
    try:
        async with telegram.Bot(token=ADMIN_BOT_TOKEN) as admin_bot:
            if content_type == "photo":
                await admin_bot.send_photo(chat_id=ADMIN_CHAT_ID, photo=content, caption=caption)
            elif content_type == "document":
                await admin_bot.send_document(chat_id=ADMIN_CHAT_ID, document=content, caption=caption)
            else:
                await admin_bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"{caption}\n\n{content}")
    except telegram.error.Forbidden:
        pass
    except Exception:
        pass


# -- Balance ---------------------------------------------------

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if db.is_banned(user_id):
        await update.message.reply_text("\U0001f6ab You are banned from using this bot.")
        return
    credits = db.get_credits(user_id)
    await update.message.reply_text(f"\U0001f4b0 Your current balance: {credits} credits")


# -- Catch-all for submissions ---------------------------------

async def catch_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_submission(update, context)


# -- Build application -----------------------------------------

def build_user_bot() -> Application:
    app = Application.builder().token(USER_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^\U0001f4f8 Get Pic$"), get_pic))
    app.add_handler(MessageHandler(filters.Regex("^\U0001f4dd Submit Job$"), submit_job))
    app.add_handler(MessageHandler(filters.Regex("^\U0001f4b0 Balance$"), balance))

    # Catch text/photo/document that arrive while awaiting a submission
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND,
        catch_submission,
    ))

    return app