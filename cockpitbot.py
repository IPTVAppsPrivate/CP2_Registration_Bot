import json
import logging
import requests
import re
import os
import time
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, constants
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackContext,
)
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

# ✅ Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")
LICENSE_CHECK_URL = os.getenv("LICENSE_CHECK_URL")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")

# ✅ Force HTTP if LICENSE_CHECK_URL starts with HTTPS (for compatibility)
if LICENSE_CHECK_URL.startswith("https://"):
    LICENSE_CHECK_URL = LICENSE_CHECK_URL.replace("https://", "http://")

# ✅ Validate that required variables are present
if not BOT_TOKEN or not GROUP_CHAT_ID or not LICENSE_CHECK_URL or not ADMIN_USER_ID:
    raise ValueError("🚨 ERROR: Missing environment variables in the .env file")

# ✅ Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logger.info("✅ Bot successfully initialized with environment variables.")

# ─── Persistence Functions ─────────────────────────────────────────────
def load_json_data(file_path):
    """Safely loads JSON data from a file."""
    if os.path.exists(file_path):
        try:
            with open(file_path, "r") as file:
                return json.load(file)
        except (json.JSONDecodeError, IOError):
            logger.warning(f"⚠️ Warning: Could not load {file_path}, using default empty dictionary.")
            return {}
    return {}

def save_json_data(file_path, data):
    """Saves data as JSON to a file."""
    try:
        with open(file_path, "w") as file:
            json.dump(data, file, indent=4)
    except IOError as e:
        logger.error(f"⚠️ Error saving data to {file_path}: {e}")

# ─── File Names for Persistence ─────────────────────────────────────────
LICENSE_STORAGE_FILE = "used_licenses.json"
ATTEMPTS_STORAGE_FILE = "user_attempts.json"
BLOCKED_USERS_FILE = "blocked_users.json"           # For automatic blocked user IDs (stored as list)
BLOCKED_USERS_DICT_FILE = "blocked_users_dict.json"   # For manual blocked users (username: user_id)

# ─── Global Variables Initialization ─────────────────────────────────────
failed_attempts = {}           
blocked_users = set(load_json_data(BLOCKED_USERS_FILE) or [])
blocked_users_dict = load_json_data(BLOCKED_USERS_DICT_FILE) or {}
processing_users = set()
verification_codes = {}
MAX_RETRIES = 3
MAX_FAILED_ATTEMPTS = 5
DELETE_AFTER_SECONDS = 600     # 10 minutes

# ─── Configure Requests Session ───────────────────────────────────────────
session = requests.Session()
if LICENSE_CHECK_URL.startswith("https://"):
    class TLSAdapter(HTTPAdapter):
        """Forces TLS compatibility with reduced security."""
        def init_poolmanager(self, *args, **kwargs):
            context = create_urllib3_context()
            context.set_ciphers("DEFAULT@SECLEVEL=1")
            kwargs["ssl_context"] = context
            super().init_poolmanager(*args, **kwargs)
    session.mount("https://", TLSAdapter())
else:
    session.mount("http://", HTTPAdapter())

def escape_markdown(text):
    """Escapes special characters for MarkdownV2."""
    escape_chars = r'_*[\]()~`>#+-=|{}.!'
    return re.sub(r'([{}])'.format(re.escape(escape_chars)), r'\\\1', text)

# ─── Helper Functions ─────────────────────────────────────────────────────
async def is_user_in_group(user_id, context: ContextTypes.DEFAULT_TYPE):
    """Checks if the user is already in the group."""
    try:
        member = await context.bot.get_chat_member(GROUP_CHAT_ID, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logger.error(f"Error checking user membership for {user_id}: {e}")
        return False

async def generate_invite_link(context: ContextTypes.DEFAULT_TYPE):
    """Generates an invite link that expires 12 seconds after generation."""
    for attempt in range(MAX_RETRIES):
        try:
            invite_link = await context.bot.create_chat_invite_link(
                GROUP_CHAT_ID,
                expire_date=int(time.time() + 12)
            )
            return invite_link.invite_link
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed to generate invite link: {e}")
            await asyncio.sleep(2)
    return None

async def delete_message(context: CallbackContext):
    """Deletes a message after a set time."""
    chat_id, message_id = context.job.data
    try:
        await context.bot.delete_message(chat_id, message_id)
    except Exception as e:
        logger.error(f"Failed to delete message {message_id}: {e}")

async def send_and_schedule_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, parse_mode=None):
    """Sends a message and schedules its deletion after DELETE_AFTER_SECONDS."""
    sent_message = await update.message.reply_text(text, parse_mode=parse_mode)
    context.job_queue.run_once(delete_message, DELETE_AFTER_SECONDS, data=(update.message.chat_id, sent_message.message_id))

# ─── Bot Command Handlers ──────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    if update.message.chat.type != "private":
        logger.info(f"Ignored /start from chat ID: {update.message.chat_id}")
        return
    logger.info(f"Received /start from user: {update.effective_user.id}")
    welcome_message = (
        "👋 Welcome! Please provide your license key for verification.\n\n"
        "Once verified, I will send you the invite link to the group."
    )
    await send_and_schedule_delete(update, context, welcome_message)

async def handle_license(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles license key verification and invite link generation."""
    global failed_attempts, blocked_users, verification_codes
    user_id = update.effective_user.id
    license_key = update.message.text.strip()

    # Check if the user is already in the group regardless of the license entered
    if await is_user_in_group(user_id, context):
        friendly_message = "🎉 Congratulations! You are already a valued member of the group. Enjoy your stay!"
        await send_and_schedule_delete(update, context, friendly_message)
        return

    # If the user is blocked automatically
    if user_id in blocked_users:
        await send_and_schedule_delete(update, context, "🚫 You have been blocked due to multiple incorrect attempts. Contact admin @SanchezC137Media.")
        return

    # If the license key was already used by another user, block the current user
    if license_key in verification_codes and verification_codes[license_key] != user_id:
        blocked_users.add(user_id)
        save_json_data(BLOCKED_USERS_FILE, list(blocked_users))
        await send_and_schedule_delete(update, context, "🚫 This verification code has already been used. Contact admin @SanchezC137Media.")
        return

    processing_users.add(user_id)
    try:
        if not LICENSE_CHECK_URL:
            await update.message.reply_text("⚠️ Internal error. Please contact support.")
            return

        response = session.post(LICENSE_CHECK_URL, data={"licensekey": license_key}, timeout=10)
        response.raise_for_status()
        response_data = response.json()
        logger.info(f"License check response: {response_data}")

        # Compare status case-insensitively
        if response_data.get("status", "").lower() == "valid":
            invite_link = await generate_invite_link(context)
            if invite_link:
                success_message = escape_markdown(f"✅ Your license key has been verified!\n\nHere is your invite link to the group: [Join Group]({invite_link})")
                await send_and_schedule_delete(update, context, success_message, parse_mode=constants.ParseMode.MARKDOWN_V2)
                verification_codes[license_key] = user_id
                failed_attempts.pop(user_id, None)
            else:
                await send_and_schedule_delete(update, context, "⚠️ Unable to generate invite link. Contact admin.")
        else:
            failed_attempts[user_id] = failed_attempts.get(user_id, 0) + 1
            if failed_attempts[user_id] >= MAX_FAILED_ATTEMPTS:
                blocked_users.add(user_id)
                save_json_data(BLOCKED_USERS_FILE, list(blocked_users))
                await send_and_schedule_delete(update, context, "🚫 Blocked due to multiple incorrect attempts. Contact admin @SanchezC137Media.")
            else:
                attempts_left = MAX_FAILED_ATTEMPTS - failed_attempts[user_id]
                await send_and_schedule_delete(update, context, f"❌ Invalid license key. Please try again. You have {attempts_left} attempts left.")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error verifying license key: {e}")
        await send_and_schedule_delete(update, context, "⚠️ Error verifying license key. Please try again later.")
    finally:
        processing_users.discard(user_id)

# ─── Administrative Commands ─────────────────────────────────────────────
async def admin_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows the admin to block a user by username."""
    if update.effective_user.id != int(ADMIN_USER_ID):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /block <username>")
        return
    username = context.args[0].lstrip('@')
    try:
        chat = await context.bot.get_chat(username)
        user_id = chat.id
        blocked_users.add(user_id)
        blocked_users_dict[username] = user_id
        save_json_data(BLOCKED_USERS_FILE, list(blocked_users))
        save_json_data(BLOCKED_USERS_DICT_FILE, blocked_users_dict)
        await update.message.reply_text(f"✅ User @{username} (ID: {user_id}) has been blocked.")
    except Exception as e:
        await update.message.reply_text(f"❌ Could not block user @{username}. Error: {str(e)}")

async def admin_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows the admin to unblock a user by username."""
    if update.effective_user.id != int(ADMIN_USER_ID):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /unblock <username>")
        return
    username = context.args[0].lstrip('@')
    try:
        if username not in blocked_users_dict:
            await update.message.reply_text(f"❌ User @{username} is not blocked.")
            return
        user_id = blocked_users_dict.pop(username)
        if user_id in blocked_users:
            blocked_users.remove(user_id)
        save_json_data(BLOCKED_USERS_FILE, list(blocked_users))
        save_json_data(BLOCKED_USERS_DICT_FILE, blocked_users_dict)
        await update.message.reply_text(f"✅ User @{username} (ID: {user_id}) has been unblocked.")
    except Exception as e:
        await update.message.reply_text(f"❌ Could not unblock user @{username}. Error: {str(e)}")

async def admin_blocked_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists all blocked users (by username and ID) for the admin."""
    if update.effective_user.id != int(ADMIN_USER_ID):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    if not blocked_users_dict:
        await update.message.reply_text("✅ There are no blocked users.")
        return
    message = "🚫 Blocked Users:\n"
    for username, user_id in blocked_users_dict.items():
        message += f"@{username} (ID: {user_id})\n"
    await update.message.reply_text(message)

# ─── Global dictionary for manual blocked users (persisted) ─────────────
blocked_users_dict = load_json_data(BLOCKED_USERS_DICT_FILE) or {}

# ─── Handler Registration ─────────────────────────────────────────────────
if __name__ == "__main__":
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register regular handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_license))

    # Register administrative command handlers
    application.add_handler(CommandHandler("block", admin_block))
    application.add_handler(CommandHandler("unblock", admin_unblock))
    application.add_handler(CommandHandler("blockuserslist", admin_blocked_users_list))

    # Run polling with optimized parameters:
    # timeout=60 uses long polling to reduce API calls,
    # poll_interval=1.0 sec waits between requests when idle.
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        timeout=60,
        poll_interval=1.0
    )
