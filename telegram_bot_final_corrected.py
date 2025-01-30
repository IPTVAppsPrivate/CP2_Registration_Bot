
import json
import logging
import requests
import re
import os
import time
import asyncio
from datetime import datetime
from cryptography.fernet import Fernet
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

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID"))
LICENSE_CHECK_URL = os.getenv("LICENSE_CHECK_URL")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID"))

# Ensure required environment variables are defined
if not BOT_TOKEN or not GROUP_CHAT_ID or not LICENSE_CHECK_URL or not ADMIN_USER_ID:
    raise ValueError("🚨 ERROR: Missing environment variables in the .env file")

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Encryption key for securing license keys
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")

if not ENCRYPTION_KEY:
    ENCRYPTION_KEY = Fernet.generate_key().decode()
    with open(".env", "a") as env_file:
        env_file.write(f"\nENCRYPTION_KEY={ENCRYPTION_KEY}\n")

cipher_suite = Fernet(ENCRYPTION_KEY.encode())

# File paths for logging
BLOCKED_USERS_FILE = "blocked_users.json"
USED_LICENSES_FILE = "used_licenses.json"

# Global storage
failed_attempts = {}
blocked_users = set()
processed_messages = set()
processing_users = set()
verification_codes = {}
MAX_RETRIES = 3
MAX_FAILED_ATTEMPTS = 5
DELETE_AFTER_SECONDS = 600  # 10 minutes

# Ensure JSON log files exist
for file in [BLOCKED_USERS_FILE, USED_LICENSES_FILE]:
    if not os.path.exists(file):
        with open(file, "w") as f:
            json.dump({}, f)

def encrypt_license(license_key):
    """Encrypts a license key for secure storage."""
    return cipher_suite.encrypt(license_key.encode()).decode()

def log_blocked_user(user_id, reason):
    """Logs a blocked user to a JSON file."""
    with open(BLOCKED_USERS_FILE, "r+") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            data = {}
        data[user_id] = {
            "timestamp": datetime.utcnow().isoformat(),
            "reason": reason,
        }
        f.seek(0)
        json.dump(data, f, indent=4)
        f.truncate()

def log_used_license(user_id, license_key):
    """Logs a used license with encryption to a JSON file."""
    encrypted_license = encrypt_license(license_key)
    with open(USED_LICENSES_FILE, "r+") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            data = {}
        data[user_id] = {
            "timestamp": datetime.utcnow().isoformat(),
            "encrypted_license": encrypted_license,
        }
        f.seek(0)
        json.dump(data, f, indent=4)
        f.truncate()

async def is_user_in_group(user_id, context):
    """Check if the user is already a member of the group."""
    try:
        member = await context.bot.get_chat_member(GROUP_CHAT_ID, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.error(f"Error checking user membership for {user_id}: {e}")
        return False

async def generate_invite_link(context):
    """Tries to generate an invite link with retries, ensuring a 12-second expiration."""
    for attempt in range(MAX_RETRIES):
        try:
            invite_link = await context.bot.create_chat_invite_link(
                GROUP_CHAT_ID, expire_date=time.time() + 12
            )
            return invite_link.invite_link
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed to generate invite link: {e}")
            await asyncio.sleep(2)
    return None

async def delete_message(context: CallbackContext):
    """Deletes a message after the set delay."""
    chat_id, message_id = context.job.data
    try:
        await context.bot.delete_message(chat_id, message_id)
    except Exception as e:
        logger.error(f"Failed to delete message {message_id}: {e}")

async def send_and_schedule_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, parse_mode=None):
    """Sends a message and schedules it for deletion after DELETE_AFTER_SECONDS."""
    sent_message = await update.message.reply_text(text, parse_mode=parse_mode)

    if context.job_queue is not None:
        context.job_queue.run_once(delete_message, DELETE_AFTER_SECONDS, data=(update.message.chat_id, sent_message.message_id))
    else:
        logger.error("Job queue is not available! Messages will not be deleted.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    if update.message.chat.type != "private":
        return

    welcome_message = "👋 Welcome! Please provide your license key for verification."
    await send_and_schedule_delete(update, context, welcome_message)

async def handle_license(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles license verification and invite link generation."""
    global processed_messages, processing_users, failed_attempts, blocked_users, verification_codes

    user_id = update.effective_user.id
    license_key = update.message.text.strip()

    if user_id in blocked_users:
        await send_and_schedule_delete(update, context, "🚫 You have been blocked. Contact the administrator.")
        return

    if license_key in verification_codes and verification_codes[license_key] != user_id:
        blocked_users.add(user_id)
        log_blocked_user(str(user_id), "Used a duplicate license key")
        await send_and_schedule_delete(update, context, "🚫 This license has already been used. You have been blocked.")
        return

    processing_users.add(user_id)

    try:
        response = requests.post(LICENSE_CHECK_URL, data={"licensekey": license_key}, timeout=10)
        response.raise_for_status()
        response_data = response.json()

        if response_data.get("status") == "Valid":
            if await is_user_in_group(user_id, context):
                await send_and_schedule_delete(update, context, "✅ You are already a member of the group.")
                return
            
            invite_link = await generate_invite_link(context)

            if invite_link:
                success_message = f"✅ Your license key is valid.\n[Join Group]({invite_link})"
                await send_and_schedule_delete(update, context, success_message, parse_mode=constants.ParseMode.MARKDOWN_V2)
                verification_codes[license_key] = user_id
                log_used_license(str(user_id), license_key)
                failed_attempts.pop(user_id, None)

    finally:
        processing_users.discard(user_id)

if __name__ == "__main__":
    application = ApplicationBuilder().token(BOT_TOKEN).post_init(lambda app: app.job_queue).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_license))
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
