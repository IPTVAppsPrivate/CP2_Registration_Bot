import json
import logging
import requests
import re
import os
import time
import asyncio
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

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")  # Keep as string to avoid API conversion issues
LICENSE_CHECK_URL = os.getenv("LICENSE_CHECK_URL")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")  # Admin ID for manual blocking/unblocking

# Ensure required environment variables are defined
if not BOT_TOKEN or not GROUP_CHAT_ID or not LICENSE_CHECK_URL or not ADMIN_USER_ID:
    raise ValueError("🚨 ERROR: Missing environment variables in the .env file")

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Global storage for tracking failed attempts, blocked users, and used verification codes
failed_attempts = {}
blocked_users = set()
processed_messages = set()
processing_users = set()
verification_codes = {}  # Stores used verification codes mapped to user IDs
MAX_RETRIES = 3  # Maximum retries for generating an invite link
MAX_FAILED_ATTEMPTS = 5  # Maximum incorrect attempts before blocking
DELETE_AFTER_SECONDS = 600  # 10 minutes


def escape_markdown(text):
    """Escapes special characters for MarkdownV2 formatting."""
    escape_chars = r'_*[\]()~`>#+-=|{}.!'
    return re.sub(r'([{}])'.format(re.escape(escape_chars)), r'\\\1', text)


async def is_user_in_group(user_id, context):
    """Check if the user is already a member of the group."""
    try:
        member = await context.bot.get_chat_member(GROUP_CHAT_ID, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logger.error(f"Error checking user membership for {user_id}: {e}")
        return False  # Assume user is not in the group if an error occurs


async def generate_invite_link(context):
    """Tries to generate an invite link with retries, ensuring a 12-second expiration."""
    for attempt in range(MAX_RETRIES):
        try:
            invite_link = await context.bot.create_chat_invite_link(
                GROUP_CHAT_ID, expire_date=int(time.time() + 12)  # Convert to integer timestamp
            )
            return invite_link.invite_link
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed to generate invite link: {e}")
            await asyncio.sleep(2)  # Wait before retrying
    return None  # If all retries fail, return None


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
    context.job_queue.run_once(delete_message, DELETE_AFTER_SECONDS, data=(update.message.chat_id, sent_message.message_id))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    if update.message.chat.type != "private":
        logger.info(f"Ignored /start from chat ID: {update.message.chat_id}")
        return

    logger.info(f"Received /start from user: {update.effective_user.id}")
    welcome_message = (
        """👋 Welcome! Please provide your license key for verification.
        
        Once verified, I will send you the invite link to the group."""
    )
    await send_and_schedule_delete(update, context, welcome_message)


async def handle_license(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles license verification and invite link generation."""
    global processed_messages, processing_users, failed_attempts, blocked_users, verification_codes

    user_id = update.effective_user.id
    license_key = update.message.text.strip()

    # ✅ Check if user is blocked
    if user_id in blocked_users:
        await send_and_schedule_delete(update, context, "🚫 You have been blocked due to multiple incorrect attempts. Please contact the administrator @SanchezC137Media .")
        return

    # ✅ Check if this verification code is already registered to another user
    if license_key in verification_codes and verification_codes[license_key] != user_id:
        blocked_users.add(user_id)  # Automatically block the second user
        await send_and_schedule_delete(update, context, "🚫 This verification code has already been used. You have been blocked for suspicious activity. Contact the administrator @SanchezC137Media .")
        return

    # ✅ Safe way to add `user_id` to `processing_users`
    processing_users.add(user_id)

    try:
        response = requests.post(LICENSE_CHECK_URL, data={"licensekey": license_key}, timeout=10)
        response.raise_for_status()
        response_data = response.json()

        if response_data.get("status") == "Valid":
            # ✅ Check if user is already in the group
            if await is_user_in_group(user_id, context):
                await send_and_schedule_delete(update, context, "✅ You are already a member of the group. No invite is needed.")
                return

            # ✅ Generate invite link for users who are NOT in the group
            invite_link = await generate_invite_link(context)

            if invite_link:
                success_message = escape_markdown(
                    f"✅ Your license key has been verified\n\n"
                    f"Here is your invite link to the group: [Join Group]({invite_link})"
                )
                await send_and_schedule_delete(update, context, success_message, parse_mode=constants.ParseMode.MARKDOWN_V2)
                verification_codes[license_key] = user_id
                failed_attempts.pop(user_id, None)
            else:
                await send_and_schedule_delete(update, context, "⚠️ I couldn't generate an invite link after multiple attempts. Please contact the admin.")

        else:
            failed_attempts[user_id] = failed_attempts.get(user_id, 0) + 1
            remaining_attempts = MAX_FAILED_ATTEMPTS - failed_attempts[user_id]
            if failed_attempts[user_id] >= MAX_FAILED_ATTEMPTS:
                blocked_users.add(user_id)
                await send_and_schedule_delete(update, context, "🚫 You have been blocked due to multiple incorrect attempts. Please contact the administrator @SanchezC137Media .")
            else:
                await send_and_schedule_delete(update, context, f"❌ Invalid verification code. You have {remaining_attempts} attempts left before being blocked.")

    finally:
        processing_users.discard(user_id)


async def init_job_queue(app):
    """Ensure job queue is properly initialized."""
    job_queue = app.job_queue  # ✅ Use the built-in job_queue, don't overwrite it
    job_queue.start()


if __name__ == "__main__":
    application = ApplicationBuilder().token(BOT_TOKEN).post_init(init_job_queue).build()

    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_license))

    # Run polling
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
