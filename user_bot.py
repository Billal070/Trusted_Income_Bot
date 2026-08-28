"""
User Bot - the bot regular users interact with.
"""

import io
import logging
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

logger = logging.getLogger(__name__)

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
        [KeyboardButton("\U0001f4b3Get Nid"), KeyboardButton("\U0001f4dd Submit Job")],
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
# Fix future Get Pic: claim photo first, send, THEN deduct.
# If send fails, refund the claim (unmark is_sent) — no credit lost.
# Cross-bot file_id (admin bot -> user bot) fails with BadRequest → fallback: download via Admin Bot and re-upload.

async def get_pic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if db.is_banned(user_id):
        await update.message.reply_text("\U0001f6ab You are banned from using this bot.")
        return

    credits = db.get_credits(user_id)
    if credits < 1:
        await update.message.reply_text("\u274c Insufficient credit. Contact admin to top up.")
        return

    photo_id, file_id = db.claim_photo_for_user(user_id)
    if photo_id is None:
        await update.message.reply_text("\U0001f614 No Nid's available right now. Please check back later.")
        return

    # Show upload action so user sees activity during download (fixes perceived lag)
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
    except Exception:
        pass

    # Try direct file_id send first — most photos will be User-Bot-valid after migration
    try:
        await update.message.reply_photo(photo=file_id)
    except telegram.error.BadRequest as e:
        # Any BadRequest for file_id is treated as cross-bot / invalid id → fallback download+reupload
        logger.warning("Get Pic file_id failed for photo %s (%s), trying fallback: %s", photo_id, file_id[:20], e)
        try:
            async with telegram.Bot(token=ADMIN_BOT_TOKEN) as abot:
                tg_file = await abot.get_file(file_id)
                bio = io.BytesIO()
                await tg_file.download_to_memory(bio)
                bio.seek(0)
                bio.name = "photo.jpg"
                sent = await update.message.reply_photo(photo=bio)
                # Cache User-Bot-valid file_id for next time (makes future sends instant)
                try:
                    if sent.photo:
                        new_fid = sent.photo[-1].file_id
                        db.update_photo_file_id(photo_id, new_fid)
                except Exception:
                    pass
        except Exception as e2:
            logger.error("Get Pic fallback failed for photo %s: %s", photo_id, e2, exc_info=True)
            db.refund_photo_claim(photo_id)
            await update.message.reply_text("\u26a0\ufe0f Photo delivery failed. Your credit was NOT deducted. Please try again — admin should re-add the photo.")
            return
    except Exception as e:
        logger.error("Get Pic send failed for photo %s: %s", photo_id, e, exc_info=True)
        db.refund_photo_claim(photo_id)
        await update.message.reply_text("\u26a0\ufe0f Photo delivery failed. Your credit was NOT deducted. Please try again.")
        return

    # Send succeeded — now deduct credit atomically
    new_balance = db.confirm_photo_delivery(user_id)
    if new_balance is None:
        # Race: credit dropped to 0 between check and confirm — refund photo
        logger.warning("Get Pic confirm failed (race) for user %s photo %s", user_id, photo_id)
        db.refund_photo_claim(photo_id)
        await update.message.reply_text("\u274c Insufficient credit (race). Photo was not charged. Please try again.")
        return

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
        "\U0001f4dd Please send your Job now.\n"
        "Submit Job Like The Format Below \U0001f447\U0001f3fb\n"
        "--------------------------------------------------------------\n"
        "\U0001f587\ufe0fAccount Link:\n"
        "\n"
        "\n"
        "\U0001f5102 Factor Code:\n"
        "\n"
        "\n"
        "\u2709\ufe0fMail:"
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
    app.add_handler(MessageHandler(filters.Regex("^\U0001f4b3Get Nid$"), get_pic))
    app.add_handler(MessageHandler(filters.Regex("^\U0001f4dd Submit Job$"), submit_job))
    app.add_handler(MessageHandler(filters.Regex("^\U0001f4b0 Balance$"), balance))

    # Catch text/photo/document that arrive while awaiting a submission
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND,
        catch_submission,
    ))

    return app
