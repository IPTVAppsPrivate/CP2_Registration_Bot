import json
import logging
import requests
import re
import os
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
)

# Load environment variables
load_dotenv()

# Retrieve environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")  # Keep as string, no int conversion
LICENSE_CHECK_URL = os.getenv("LICENSE_CHECK_URL")
LICENSE_STORAGE_FILE = "used_licenses.json"
ATTEMPTS_STORAGE_FILE = "user_attempts.json"

# Admin User ID (set from .env, fallback to default placeholder)
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "123456789"))  

# Ensure critical variables exist before running
if not BOT_TOKEN or not GROUP_CHAT_ID or not LICENSE_CHECK_URL:
    raise ValueError("🚨 Missing required environment variables! Please check your .env file.")

# Configure Logging (set level via .env, default to INFO)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger(__name__)

logger.info("✅ Bot successfully initialized with environment variables.")

# Global storage
users_in_progress = set()
MAX_FAILED_ATTEMPTS = 5
MAX_RETRIES = 3
AUTO_DELETE_TIME = 1500  # ⏳ 25 minutes (1500 seconds)

# Load data from file safely
def load_json_data(file_path):
    if os.path.exists(file_path):
        try:
            with open(file_path, "r") as file:
                return json.load(file)
        except (json.JSONDecodeError, IOError):
            logger.warning(f"⚠️ Warning: Could not load {file_path}, using default empty dictionary.")
            return {}  # If the file is empty or corrupted, return an empty dictionary
    return {}

def save_json_data(file_path, data):
    try:
        with open(file_path, "w") as file:
            json.dump(data, file, indent=4)
    except IOError as e:
        logger.error(f"⚠️ Error saving data to {file_path}: {e}")

# Load used licenses and user attempts
used_license_keys = load_json_data(LICENSE_STORAGE_FILE)
user_attempts = load_json_data(ATTEMPTS_STORAGE_FILE)

def escape_markdown(text):
    """Escapes special characters for MarkdownV2 formatting."""
    escape_chars = r'_*[\]()~`>#+-=|{}.!'
    return re.sub(r'([{}])'.format(re.escape(escape_chars)), r'\\\1', text)

async def auto_delete_message(context, chat_id, message_id):
    """Deletes a message after AUTO_DELETE_TIME (25 minutes)."""
    await asyncio.sleep(AUTO_DELETE_TIME)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"🗑️ Deleted message {message_id} from chat {chat_id}")
    except Exception as e:
        logger.warning(f"⚠️ Failed to delete message {message_id}: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    if update.message.chat.type != "private":
        return

    logger.info(f"📥 Received /start from user: {update.effective_user.id}")

    welcome_message = (
        "Welcome! Please provide your license key for verification.\n\n"
        "Once your license is verified, I will send you the invite link to the group."
    )

    sent_message = await update.message.reply_text(welcome_message)

    # 🕒 Schedule message auto-deletion after 25 minutes
    context.job_queue.run_once(lambda _: asyncio.create_task(auto_delete_message(context, update.message.chat_id, update.message.message_id)), AUTO_DELETE_TIME)
    context.job_queue.run_once(lambda _: asyncio.create_task(auto_delete_message(context, sent_message.chat_id, sent_message.message_id)), AUTO_DELETE_TIME)

async def generate_invite_link(context):
    """Creates an invite link that expires after 12 seconds."""
    expire_time = datetime.utcnow() + timedelta(seconds=12)

    for attempt in range(MAX_RETRIES):
        try:
            invite_link = await context.bot.create_chat_invite_link(
                GROUP_CHAT_ID,
                expire_date=expire_time,  
                member_limit=1,  
                creates_join_request=True  
            )
            return invite_link.invite_link
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed to generate invite link: {e}")
            await asyncio.sleep(2)
    
    return None  # If all attempts fail, return None


async def handle_license(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the license key submission."""
    global users_in_progress, used_license_keys, user_attempts

    if update.message.chat.type != "private":
        return

    user_id = str(update.effective_user.id)
    license_key = update.message.text.strip()

    # 🛑 Reject if the user is a bot
    if update.effective_user.is_bot:
        await update.message.reply_text("🤖 Bots are not allowed to join.")
        return

    # ✅ Check if the user is already in the group
    chat_member = await context.bot.get_chat_member(GROUP_CHAT_ID, user_id)
    if chat_member.status in ["member", "administrator", "creator"]:
        sent_message = await update.message.reply_text(
            "⚠️ You are already in the group. No need for an invite link."
        )
        asyncio.create_task(auto_delete_message(context, sent_message.chat_id, sent_message.message_id))
        return

    # 🔄 Prevent processing the same user multiple times
    if user_id in users_in_progress:
        return

    users_in_progress.add(user_id)

    try:
        # 🚫 Check if the license has already been used
        if license_key in used_license_keys:
            await update.message.reply_text(
                "⚠️ This license key has already been used by another user."
            )
            return

        # ❌ Check for too many failed attempts
        user_attempts[user_id] = user_attempts.get(user_id, 0) + 1
        remaining_attempts = MAX_FAILED_ATTEMPTS - user_attempts[user_id]
        save_json_data(ATTEMPTS_STORAGE_FILE, user_attempts)

        if remaining_attempts < 0:
            sent_message = await update.message.reply_text(
                "🚫 Too many attempts!\n\n"
                "Please contact **@SanchezC137Media** for assistance."
            )
            asyncio.create_task(auto_delete_message(context, sent_message.chat_id, sent_message.message_id))
            return

        if not LICENSE_CHECK_URL:
            await update.message.reply_text("⚠️ Internal error. Please contact support.")
            return

        # 🔍 Validate the license key
        response = requests.post(LICENSE_CHECK_URL, data={"licensekey": license_key}, timeout=10)
        response.raise_for_status()
        response_data = response.json()

        if response_data.get("status") == "Valid":
            invite_link = await generate_invite_link(context)

            if invite_link:
                success_message = escape_markdown(
                    f"✅ Your license key has been verified!\n\n"
                    f"Here is your invite link to the group: [Join Group]({invite_link})"
                )
                sent_message = await update.message.reply_text(success_message, parse_mode=constants.ParseMode.MARKDOWN_V2)

                # 🔹 Save the license key as "used"
                used_license_keys[license_key] = user_id
                save_json_data(LICENSE_STORAGE_FILE, used_license_keys)

                # ⏳ Delete message after 25 minutes
                asyncio.create_task(auto_delete_message(context, sent_message.chat_id, sent_message.message_id))
            else:
                await update.message.reply_text("⚠️ I couldn't generate an invite link. Please contact the admin.")
        else:
            await update.message.reply_text("❌ Invalid license key. Please make sure you have entered a valid key.")

    except requests.exceptions.RequestException:
        await update.message.reply_text("⚠️ Error verifying license key. Please try again later.")

    finally:
        # 🔄 Ensure the user is removed from the processing queue
        users_in_progress.remove(user_id)

    # Verify previous attempts by the user
    user_attempts[user_id] = user_attempts.get(user_id, 0) + 1
    remaining_attempts = MAX_FAILED_ATTEMPTS - user_attempts[user_id]
    save_json_data(ATTEMPTS_STORAGE_FILE, user_attempts)

    if remaining_attempts < 0:
        sent_message = await update.message.reply_text(
            "🚫 Too many attempts!\n\n"
            "Please contact **@SanchezC137Media** for assistance."
        )
        asyncio.create_task(auto_delete_message(context, sent_message.chat_id, sent_message.message_id))
        users_in_progress.remove(user_id)
        return

    if not LICENSE_CHECK_URL:
        await update.message.reply_text("⚠️ Internal error. Please contact support.")
        users_in_progress.remove(user_id)
        return

    try:
        response = requests.post(LICENSE_CHECK_URL, data={"licensekey": license_key}, timeout=10)
        response.raise_for_status()
        response_data = response.json()

        if response_data.get("status") == "Valid":
            invite_link = await generate_invite_link(context)

            if invite_link:
                success_message = escape_markdown(
                    f"✅ Your license key has been verified!\n\n"
                    f"Here is your invite link to the group: [Join Group]({invite_link})"
                )
                sent_message = await update.message.reply_text(success_message, parse_mode=constants.ParseMode.MARKDOWN_V2)

                used_license_keys[license_key] = user_id
                save_json_data(LICENSE_STORAGE_FILE, used_license_keys)

                # 🕒 Auto-delete message in 25 minutes
                asyncio.create_task(auto_delete_message(context, sent_message.chat_id, sent_message.message_id))

            else:
                # ⚠️ If the invite expired, decrease attempts
                remaining_attempts -= 1
                user_attempts[user_id] += 1
                save_json_data(ATTEMPTS_STORAGE_FILE, user_attempts)

                # 🛑 Notify the user their invite expired
                sent_message = await update.message.reply_text(
                    f"⚠️ Your invite link has expired. Please re-enter your license key.\n"
                    f"You have {remaining_attempts} attempts left."
                )
                asyncio.create_task(auto_delete_message(context, sent_message.chat_id, sent_message.message_id))

                # If no attempts remain, lock user out
                if remaining_attempts <= 0:
                    await update.message.reply_text(
                        "🚫 Too many attempts! You are blocked.\n"
                        "Please contact **@SanchezC137Media** for assistance."
                    )

    except requests.exceptions.RequestException:
        await update.message.reply_text("⚠️ Error verifying license key. Please try again later.")

    finally:
        users_in_progress.remove(user_id)


async def unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows an admin to unblock a user by their Telegram ID."""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return

    # Extract user ID from the command
    message_parts = update.message.text.split()
    if len(message_parts) < 2:
        await update.message.reply_text("⚠️ Usage: /unblock <user_id>")
        return

    user_id_to_unblock = message_parts[1]  # Extract second word (ID)

    # Ensure it's a valid numeric ID
    if not user_id_to_unblock.isdigit():
        await update.message.reply_text("⚠️ Invalid user ID format.")
        return

    # Convert to string (because user_attempts keys are stored as strings)
    user_id_to_unblock = str(user_id_to_unblock)

    # Load data
    user_attempts = load_json_data(ATTEMPTS_STORAGE_FILE)
    used_license_keys = load_json_data(LICENSE_STORAGE_FILE)

    # Unblock user by resetting attempts
    if user_id_to_unblock in user_attempts:
        del user_attempts[user_id_to_unblock]
        save_json_data(ATTEMPTS_STORAGE_FILE, user_attempts)

    # Remove from used license keys if necessary
    for license_key, user in list(used_license_keys.items()):
        if user == user_id_to_unblock:
            del used_license_keys[license_key]
            break  # Stop after removing the first match

    save_json_data(LICENSE_STORAGE_FILE, used_license_keys)

    logger.info(f"✅ Admin unblocked user {user_id_to_unblock}")
    await update.message.reply_text(f"✅ User {user_id_to_unblock} has been unblocked.")
