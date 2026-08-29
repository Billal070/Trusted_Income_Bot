"""
Admin Bot - manages users, credits, broadcasts, and photo pool.
Only users listed in ADMIN_USER_IDS may use this bot.
"""

import asyncio
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
        [KeyboardButton("\U0001f4ca Stats"), KeyboardButton("\U0001f465 Manage Users")],
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

    data = query.data

    # --- Deposit verification callbacks ---
    if data.startswith("dep_approve:") or data.startswith("dep_reject:"):
        is_approve = data.startswith("dep_approve:")
        try:
            dep_id = int(data.split(":")[1])
        except Exception:
            return await query.answer("Invalid deposit ID", show_alert=True)
        await query.answer("Processing..." if is_approve else "Rejecting...")
        dep = db.get_deposit(dep_id)
        if not dep:
            return await query.edit_message_text("\u274c Deposit not found.")
        if dep["status"] != "pending":
            return await query.edit_message_text(f"\u26a0\ufe0f Already {dep['status']}.")
        admin_id = update.effective_user.id
        if is_approve:
            res = db.approve_deposit(dep_id, admin_id)
            if not res:
                return await query.edit_message_text("\u274c Failed to approve.")
            # edit admin message
            try:
                await query.edit_message_text(f"\u2705 Deposit of {dep['amount']} BDT for @{dep['username'] or dep['user_id']} approved. Credits added.")
            except Exception:
                pass
            # notify user via User Bot
            try:
                async with telegram.Bot(token=USER_BOT_TOKEN) as ubot:
                    await ubot.send_message(chat_id=dep["user_id"], text=f"\U0001f389 Success! Your deposit of {dep['amount']} BDT has been approved. {dep['amount']} Credits added to your account! \U0001f4b0 Balance: {res['new_credits']} credits")
            except Exception as e:
                logger.warning("Notify user approve failed: %s", e)
            return
        else:
            res = db.reject_deposit(dep_id, admin_id)
            if not res:
                return await query.edit_message_text("\u274c Failed to reject.")
            try:
                await query.edit_message_text("\u274c Request rejected.")
            except Exception:
                pass
            try:
                async with telegram.Bot(token=USER_BOT_TOKEN) as ubot:
                    await ubot.send_message(chat_id=dep["user_id"], text=f"\u26a0\ufe0f Your deposit request of {dep['amount']} BDT (TrxID: {dep['trx_id']}) was rejected. Contact support for help.")
            except Exception as e:
                logger.warning("Notify user reject failed: %s", e)
            return

    # --- Manage Users callbacks ---
    if data.startswith("mlist:"):
        await query.answer()
        try:
            page = int(data.split(":")[1])
        except Exception:
            page = 0
        return await manage_users_list(update, context, page=page, edit=True)
    if data.startswith("mview:"):
        await query.answer()
        try:
            _, uid_s, page_s = data.split(":")
            uid = int(uid_s); page = int(page_s)
        except Exception:
            return
        return await show_user_detail(query, uid, page)
    if data.startswith("mban:"):
        await query.answer()
        try:
            _, uid_s, page_s = data.split(":")
            uid = int(uid_s); page = int(page_s)
        except Exception:
            return
        user = db.get_user(uid)
        if not user:
            return await query.answer("\u274c User not found.", show_alert=True)
        db.set_banned(uid, not user["is_banned"])
        # refresh list page
        return await manage_users_list(update, context, page=page, edit=True)
    if data.startswith("mdetailban:"):
        await query.answer()
        try:
            _, uid_s, page_s = data.split(":")
            uid = int(uid_s); page = int(page_s)
        except Exception:
            return
        user = db.get_user(uid)
        if not user:
            return await query.answer("\u274c User not found.", show_alert=True)
        db.set_banned(uid, not user["is_banned"])
        return await show_user_detail(query, uid, page)
    if data == "mclose":
        await query.answer()
        try:
            await query.delete_message()
        except Exception:
            await query.edit_message_text("\u2705 Closed.")
        return
    if data.startswith("madd:"):
        await query.answer()
        try:
            _, uid_s, page_s = data.split(":")
            uid = int(uid_s)
        except Exception:
            return
        context.user_data["admin_state"] = f"awaiting_madd:{uid}:{page_s}"
        return await query.edit_message_text(f"Enter amount to ADD to user {uid}:")
    if data.startswith("mdeduct:"):
        await query.answer()
        try:
            _, uid_s, page_s = data.split(":")
            uid = int(uid_s)
        except Exception:
            return
        context.user_data["admin_state"] = f"awaiting_mdeduct:{uid}:{page_s}"
        return await query.edit_message_text(f"Enter amount to DEDUCT from user {uid}:")

    # --- Existing Search-User callbacks ---
    await query.answer()
    parts = data.split(":")
    action = parts[0]
    try:
        uid = int(parts[1])
    except Exception:
        return
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
    elif action == "awaiting_madd":
        # parts = ["awaiting_madd", uid, page]
        try:
            page = int(parts[2]) if len(parts) > 2 else 0
        except Exception:
            page = 0
        new_balance = db.add_credit(uid, amount, admin_id)
        from user_bot import notify_user_credit_change
        await notify_user_credit_change(uid, amount, "add", new_balance)
        await update.message.reply_text(f"\u2705 Added {amount} credits to user {uid}. New balance: {new_balance}")
        # show updated detail view
        u = db.get_user(uid)
        if u:
            sub_count = db.get_submission_count(uid)
            summary = db.get_credit_summary(uid)
            status = "Banned \U0001f6ab" if u["is_banned"] else "Active \u2705"
            text = (
                f"\U0001f464 User Details\n"
                f"ID: {u['user_id']}\n"
                f"Username: @{u['username'] or 'N/A'}\n"
                f"Name: {u['first_name'] or 'N/A'}\n"
                f"Balance: {u['credits']} credits\n"
                f"Status: {status}\n"
                f"\U0001f4dd Total Submissions: {sub_count}\n"
                f"\u2795 Total Added: {summary['added']} credits\n"
                f"\u2796 Total Deducted: {summary['deducted']} credits\n"
                f"Joined: {u['joined_at'] or 'N/A'}"
            )
            ban_label = "\u2705 Unban" if u["is_banned"] else "\U0001f6ab Ban"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("\u2795 Add Credit", callback_data=f"madd:{uid}:{page}"),
                 InlineKeyboardButton("\u2796 Deduct Credit", callback_data=f"mdeduct:{uid}:{page}")],
                [InlineKeyboardButton(ban_label, callback_data=f"mdetailban:{uid}:{page}")],
                [InlineKeyboardButton("\u25c0 Back", callback_data=f"mlist:{page}")],
            ])
            await update.message.reply_text(text, reply_markup=keyboard)
    elif action == "awaiting_mdeduct":
        try:
            page = int(parts[2]) if len(parts) > 2 else 0
        except Exception:
            page = 0
        new_balance = db.deduct_credit(uid, amount, admin_id)
        from user_bot import notify_user_credit_change
        await notify_user_credit_change(uid, amount, "deduct", new_balance)
        await update.message.reply_text(f"\u2705 Deducted {amount} credits from user {uid}. New balance: {new_balance}")
        u = db.get_user(uid)
        if u:
            sub_count = db.get_submission_count(uid)
            summary = db.get_credit_summary(uid)
            status = "Banned \U0001f6ab" if u["is_banned"] else "Active \u2705"
            text = (
                f"\U0001f464 User Details\n"
                f"ID: {u['user_id']}\n"
                f"Username: @{u['username'] or 'N/A'}\n"
                f"Name: {u['first_name'] or 'N/A'}\n"
                f"Balance: {u['credits']} credits\n"
                f"Status: {status}\n"
                f"\U0001f4dd Total Submissions: {sub_count}\n"
                f"\u2795 Total Added: {summary['added']} credits\n"
                f"\u2796 Total Deducted: {summary['deducted']} credits\n"
                f"Joined: {u['joined_at'] or 'N/A'}"
            )
            ban_label = "\u2705 Unban" if u["is_banned"] else "\U0001f6ab Ban"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("\u2795 Add Credit", callback_data=f"madd:{uid}:{page}"),
                 InlineKeyboardButton("\u2796 Deduct Credit", callback_data=f"mdeduct:{uid}:{page}")],
                [InlineKeyboardButton(ban_label, callback_data=f"mdetailban:{uid}:{page}")],
                [InlineKeyboardButton("\u25c0 Back", callback_data=f"mlist:{page}")],
            ])
            await update.message.reply_text(text, reply_markup=keyboard)

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
    if total == 0:
        return await update.message.reply_text("\u26a0\ufe0f No users in database to broadcast to. Users must /start the User Bot first.")

    sent = 0
    failed_details = []
    message = update.message
    logger.info("Broadcast started: %d users to send to", total)

    async with telegram.Bot(token=USER_BOT_TOKEN) as bot:
        for uid in user_ids:
            try:
                if message.text:
                    await bot.send_message(chat_id=uid, text=message.text)
                elif message.photo:
                    await bot.send_photo(chat_id=uid, photo=message.photo[-1].file_id, caption=message.caption or "")
                elif message.document:
                    await bot.send_document(chat_id=uid, document=message.document.file_id, caption=message.caption or "")
                elif message.caption:
                    await bot.send_message(chat_id=uid, text=message.caption)
                else:
                    failed_details.append(f"{uid}: unsupported message type")
                    continue
                sent += 1
            except telegram.error.Forbidden as e:
                msg = f"{uid}: blocked / not started User Bot ({e})"
                logger.warning("Broadcast: %s", msg)
                failed_details.append(msg)
            except Exception as e:
                msg = f"{uid}: {type(e).__name__}: {e}"
                logger.error("Broadcast failed for %s", msg, exc_info=True)
                failed_details.append(msg)

    result = f"\u2705 Broadcast sent to {sent}/{total} users."
    if failed_details:
        result += "\n\nFailed:\n" + "\n".join(failed_details[:10])
        if len(failed_details) > 10:
            result += f"\n...and {len(failed_details)-10} more"
        result += "\n\nTip: user must have pressed /start on the User Bot first, and not blocked it."
    await update.message.reply_text(result)


# -- Stats -----------------------------------------------------

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return await update.message.reply_text("\u26d4 Unauthorized.")
    s = db.get_stats()
    pending_deps = len(db.get_pending_deposits(limit=1000))
    text = (
        f"\U0001f4ca Bot Statistics\n\n"
        f"\U0001f465 Total Users: {s['total_users']}\n"
        f"\U0001f4b0 Credits in Circulation: {s['total_credits']}\n"
        f"\U0001f4f7 Photos Available: {s['photos_available']}\n"
        f"\U0001f4e4 Photos Sent: {s['photos_sent']}\n"
        f"\U0001f4dd Pending Submissions: {s['pending_submissions']}\n"
        f"\U0001f4b3 Pending Deposits: {pending_deps}"
    )
    await update.message.reply_text(text)


# -- 👥 Manage Users (paginated) ---------------------------------

USERS_PER_PAGE = 5

def _manage_list_keyboard(page: int) -> InlineKeyboardMarkup | None:
    total = db.count_users()
    offset = page * USERS_PER_PAGE
    users = db.get_users_paginated(limit=USERS_PER_PAGE, offset=offset)
    if not users and page == 0:
        return None
    rows = []
    for u in users:
        uid = u["user_id"]
        label = f"@{u['username']}" if u["username"] else f"ID:{uid}"
        # truncate long usernames to keep button readable
        if len(label) > 20:
            label = label[:17] + "..."
        ban_label = "\u2705 Unban" if u["is_banned"] else "\U0001f6ab Ban"
        rows.append([
            InlineKeyboardButton(label, callback_data=f"mview:{uid}:{page}"),
            InlineKeyboardButton(ban_label, callback_data=f"mban:{uid}:{page}"),
        ])
    # pagination row
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("\u25c0 Prev", callback_data=f"mlist:{page-1}"))
    if (page + 1) * USERS_PER_PAGE < total:
        nav.append(InlineKeyboardButton("Next \u25b6", callback_data=f"mlist:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("\u274c Close", callback_data="mclose")])
    # add page indicator as non-clickable by editing text outside, not button
    return InlineKeyboardMarkup(rows)

async def manage_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0, edit: bool = False):
    if not _is_admin(update.effective_user.id):
        return
    total = db.count_users()
    if total == 0:
        text = "\U0001f465 No users yet."
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return
    keyboard = _manage_list_keyboard(page)
    total_pages = (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE
    text = f"\U0001f465 Manage Users — page {page+1}/{total_pages} (total {total})\nTap username to view details, Ban to toggle."
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=keyboard)
        except telegram.error.BadRequest:
            pass
    else:
        await update.message.reply_text(text, reply_markup=keyboard)

async def manage_users_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return await update.message.reply_text("\u26d4 Unauthorized.")
    await manage_users_list(update, context, page=0)

def _user_detail_text(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    u = db.get_user(uid)
    if not u:
        return "\u274c User not found.", InlineKeyboardMarkup([[InlineKeyboardButton("\u25c0 Back", callback_data="mlist:0")]])
    sub_count = db.get_submission_count(uid)
    summary = db.get_credit_summary(uid)
    status = "Banned \U0001f6ab" if u["is_banned"] else "Active \u2705"
    text = (
        f"\U0001f464 User Details\n"
        f"ID: {u['user_id']}\n"
        f"Username: @{u['username'] or 'N/A'}\n"
        f"Name: {u['first_name'] or 'N/A'}\n"
        f"Balance: {u['credits']} credits\n"
        f"Status: {status}\n"
        f"\U0001f4dd Total Submissions: {sub_count}\n"
        f"\u2795 Total Added: {summary['added']} credits\n"
        f"\u2796 Total Deducted: {summary['deducted']} credits\n"
        f"Joined: {u['joined_at'] or 'N/A'}"
    )
    ban_label = "\u2705 Unban" if u["is_banned"] else "\U0001f6ab Ban"
    # we encode originating page in callback so Back returns there; default 0
    # caller must build keyboard with correct page; this helper builds without page, so wrappers will override
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\u2795 Add Credit", callback_data=f"madd:{uid}:0"),
            InlineKeyboardButton("\u2796 Deduct Credit", callback_data=f"mdeduct:{uid}:0"),
        ],
        [InlineKeyboardButton(ban_label, callback_data=f"mdetailban:{uid}:0")],
        [InlineKeyboardButton("\u25c0 Back", callback_data="mlist:0")],
    ])
    return text, keyboard

async def show_user_detail(query: CallbackQuery, uid: int, page: int):
    u = db.get_user(uid)
    if not u:
        return await query.edit_message_text("\u274c User not found.")
    sub_count = db.get_submission_count(uid)
    summary = db.get_credit_summary(uid)
    status = "Banned \U0001f6ab" if u["is_banned"] else "Active \u2705"
    text = (
        f"\U0001f464 User Details\n"
        f"ID: {u['user_id']}\n"
        f"Username: @{u['username'] or 'N/A'}\n"
        f"Name: {u['first_name'] or 'N/A'}\n"
        f"Balance: {u['credits']} credits\n"
        f"Status: {status}\n"
        f"\U0001f4dd Total Submissions: {sub_count}\n"
        f"\u2795 Total Added: {summary['added']} credits\n"
        f"\u2796 Total Deducted: {summary['deducted']} credits\n"
        f"Joined: {u['joined_at'] or 'N/A'}"
    )
    ban_label = "\u2705 Unban" if u["is_banned"] else "\U0001f6ab Ban"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\u2795 Add Credit", callback_data=f"madd:{uid}:{page}"),
            InlineKeyboardButton("\u2796 Deduct Credit", callback_data=f"mdeduct:{uid}:{page}"),
        ],
        [InlineKeyboardButton(ban_label, callback_data=f"mdetailban:{uid}:{page}")],
        [InlineKeyboardButton("\u25c0 Back", callback_data=f"mlist:{page}")],
    ])
    await query.edit_message_text(text, reply_markup=keyboard)


# -- Photo auto-add --------------------------------------------
# Any photo sent to the Admin Bot is automatically added to the photo pool.
# For duplicate detection, we compute a SHA-256 hash of the photo bytes
# and reject the insert if that hash already exists in the photos table.

async def _convert_to_user_file_id(admin_file_id: str, photo_db_id: int):
    """Background: make photo fast for User Bot by caching a User-Bot-valid file_id."""
    try:
        import io
        async with telegram.Bot(token=ADMIN_BOT_TOKEN) as abot:
            tg_file = await abot.get_file(admin_file_id)
            bio = io.BytesIO()
            await tg_file.download_to_memory(bio)
            bio.seek(0)
            bio.name = "photo.jpg"
            async with telegram.Bot(token=USER_BOT_TOKEN) as ubot:
                # Send to ADMIN_CHAT_ID to obtain User-Bot file_id (then delete to avoid spam)
                msg = await ubot.send_photo(chat_id=ADMIN_CHAT_ID, photo=bio)
                if msg.photo:
                    new_fid = msg.photo[-1].file_id
                    db.update_photo_file_id(photo_db_id, new_fid)
                    # try to delete temp message to keep chat clean
                    try:
                        await ubot.delete_message(chat_id=ADMIN_CHAT_ID, message_id=msg.message_id)
                    except Exception:
                        pass
                    logger.info("Converted photo %s to User-Bot file_id", photo_db_id)
    except Exception as e:
        logger.warning("Background convert failed for photo %s: %s", photo_db_id, e)

# --- Batched photo summary: aggregate many photos sent at once into one reply ---
_batch = {}  # admin_id -> {chat_id, added, dup, timer_task}

async def _flush_photo_batch(admin_id: int, bot):
    await asyncio.sleep(3.0)
    data = _batch.pop(admin_id, None)
    if not data:
        return
    added = data["added"]
    dup = data["dup"]
    if added == 0 and dup == 0:
        return
    total = db.get_pool_stats()["available"]
    parts = []
    if added:
        parts.append(f"{added} added")
    if dup:
        parts.append(f"{dup} duplicates skipped")
    summary = ", ".join(parts) if parts else "0"
    text = f"\u2705 Batch complete: {summary} \u2014 pool now {total} available."
    if added:
        text += f" (\u23f3 converting {added} for instant delivery...)"
    try:
        await bot.send_message(chat_id=data["chat_id"], text=text)
    except Exception:
        pass

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

    photo_db_id = db.add_photo(photo.file_id, file_hash)
    admin_id = update.effective_user.id
    chat_id = update.effective_chat.id
    # init / update batch
    if admin_id not in _batch:
        _batch[admin_id] = {"chat_id": chat_id, "added": 0, "dup": 0, "timer": None}
    # cancel previous flush timer
    old_timer = _batch[admin_id].get("timer")
    if old_timer and not old_timer.done():
        old_timer.cancel()
        try:
            await old_timer
        except asyncio.CancelledError:
            pass
    if photo_db_id is not None:
        _batch[admin_id]["added"] += 1
        try:
            asyncio.create_task(_convert_to_user_file_id(photo.file_id, photo_db_id))
        except Exception:
            pass
    else:
        _batch[admin_id]["dup"] += 1
    # schedule new flush 3s after last photo
    _batch[admin_id]["timer"] = asyncio.create_task(_flush_photo_batch(admin_id, context.bot))


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

async def deposits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return await update.message.reply_text("\u26d4 Unauthorized.")
    pending = db.get_pending_deposits(limit=20)
    if not pending:
        return await update.message.reply_text("\u2705 No pending deposits.")
    await update.message.reply_text(f"\U0001f4b3 Pending Deposits: {len(pending)}")
    for dep in pending:
        text = (
            f"\U0001f514 Deposit #{dep['id']}\n"
            f"\U0001f464 User: @{dep['username'] or 'N/A'} (ID: {dep['user_id']})\n"
            f"\U0001f4b3 Method: {dep['method']}\n"
            f"\U0001f4b0 Amount: {dep['amount']} BDT\n"
            f"\U0001f194 TrxID: {dep['trx_id']}\n"
            f"\U0001f4c5 Time: {dep['created_at']}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("\u2705 Approve", callback_data=f"dep_approve:{dep['id']}"),
             InlineKeyboardButton("\u274c Reject", callback_data=f"dep_reject:{dep['id']}")]
        ])
        try:
            await update.message.reply_text(text, reply_markup=keyboard)
        except Exception as e:
            logger.warning("Send pending deposit %s failed: %s", dep['id'], e)


# -- Catch-all for admin text input ----------------------------

async def catch_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("admin_state", "")
    logger.info("catch_admin_input: state=%s, user=%d", state, update.effective_user.id)
    if state.startswith("awaiting_search"):
        await handle_search_input(update, context)
    elif state.startswith("awaiting_addcredit") or state.startswith("awaiting_deductcredit") or state.startswith("awaiting_madd") or state.startswith("awaiting_mdeduct"):
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
    app.add_handler(CommandHandler("deposits", deposits_command))

    app.add_handler(MessageHandler(filters.Regex("^\U0001f50d Search User$"), search_user_prompt))
    app.add_handler(MessageHandler(filters.Regex("^\U0001f4e2 Broadcast$"), broadcast_prompt))
    app.add_handler(MessageHandler(filters.Regex("^\U0001f4ca Stats$"), stats))
    app.add_handler(MessageHandler(filters.Regex("^\U0001f465 Manage Users$"), manage_users_button))

    app.add_handler(CallbackQueryHandler(callback_handler))

    # Photo auto-add: any photo from an admin
    app.add_handler(MessageHandler(filters.PHOTO & filters.User(ADMIN_USER_IDS), auto_add_photo))

    # Catch-all for admin state-driven text input (must be last)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_USER_IDS),
        catch_admin_input,
    ))

    return app