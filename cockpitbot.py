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
    JobQueue
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

# ✅ Global variables
failed_attempts = {}
blocked_users = set()
processing_users = set()
verification_codes = {}
MAX_RETRIES = 3
MAX_FAILED_ATTEMPTS = 5
DELETE_AFTER_SECONDS = 600  # 10 minutes

# ✅ Configure a requests session with TLSAdapter if needed
session = requests.Session()
if LICENSE_CHECK_URL.startswith("https://"):
    class TLSAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            context = create_urllib3_context()
            context.set_ciphers("DEFAULT@SECLEVEL=1")  # Reduce security for compatibility
            kwargs["ssl_context"] = context
            super().init_poolmanager(*args, **kwargs)
    session.mount("https://", TLSAdapter())
else:
    session.mount("http://", HTTPAdapter())

def escape_markdown(text):
    """Escapes special characters for MarkdownV2 formatting."""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(r'([{}])'.format(re.escape(escape_chars)), r'\\\1', text)

async def is_user_in_group(user_id, context: ContextTypes.DEFAULT_TYPE):
    """Checks if the user is already in the group."""
    try:
        member = await context.bot.get_chat_member(GROUP_CHAT_ID, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logger.error(f"Error checking user membership for {user_id}: {e}")
        return False

async def generate_invite_link(context: ContextTypes.DEFAULT_TYPE):
    """Generates an invite link with retries."""
    for attempt in range(MAX_RETRIES):
        try:
            # Using the original approach without extra parameters that may cause errors
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
    """Deletes a message after a specified time."""
    chat_id, message_id = context.job.data
    try:
        await context.bot.delete_message(chat_id, message_id)
    except Exception as e:
        logger.error(f"Failed to delete message {message_id}: {e}")

async def send_and_schedule_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, parse_mode=None):
    """Sends a message and schedules it for deletion after DELETE_AFTER_SECONDS."""
    sent_message = await update.message.reply_text(text, parse_mode=parse_mode)
    context.job_queue.run_once(delete_message, DELETE_AFTER_SECONDS, data=(update.message.chat_id, sent_message.message_id))

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

    # Reject if the user is blocked
    if user_id in blocked_users:
        await send_and_schedule_delete(update, context, "🚫 You have been blocked due to multiple incorrect attempts. Contact admin @SanchezC137Media.")
        return

    # If the license key was already used by another user, block current user
    if license_key in verification_codes and verification_codes[license_key] != user_id:
        blocked_users.add(user_id)
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
            if await is_user_in_group(user_id, context):
                await send_and_schedule_delete(update, context, "✅ You are already a member of the group. No invite needed.")
                return

            invite_link = await generate_invite_link(context)
            if invite_link:
                success_message = escape_markdown(f"✅ License verified. [Join Group]({invite_link})")
                await send_and_schedule_delete(update, context, success_message, parse_mode=constants.ParseMode.MARKDOWN_V2)
                verification_codes[license_key] = user_id
                failed_attempts.pop(user_id, None)
            else:
                await send_and_schedule_delete(update, context, "⚠️ Unable to generate invite link. Contact admin.")
        else:
            failed_attempts[user_id] = failed_attempts.get(user_id, 0) + 1
            if failed_attempts[user_id] >= MAX_FAILED_ATTEMPTS:
                blocked_users.add(user_id)
                await send_and_schedule_delete(update, context, "🚫 Blocked due to multiple incorrect attempts. Contact admin @SanchezC137Media.")
            else:
                attempts_left = MAX_FAILED_ATTEMPTS - failed_attempts[user_id]
                await send_and_schedule_delete(update, context, f"❌ Invalid license key. Please try again. You have {attempts_left} attempts left.")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error verifying license key: {e}")
        await send_and_schedule_delete(update, context, "⚠️ Error verifying license key. Please try again later.")
    finally:
        processing_users.discard(user_id)

if __name__ == "__main__":
    # Build the bot application
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_license))

    # Run polling directly (using run_polling avoids issues with nested event loops)
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
