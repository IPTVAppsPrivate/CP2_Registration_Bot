import json
import logging
import requests
import re
import os
import time
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import (
    Update,
    constants,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackContext,
    ChatJoinRequestHandler,
    CallbackQueryHandler
)
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
import nest_asyncio

# Apply nest_asyncio to allow nested event loops
nest_asyncio.apply()

# --- Load environment variables ---
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")
LICENSE_CHECK_URL = os.getenv("LICENSE_CHECK_URL")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")

# --- Force HTTP if LICENSE_CHECK_URL starts with HTTPS (for compatibility) ---
if LICENSE_CHECK_URL.startswith("https://"):
    LICENSE_CHECK_URL = LICENSE_CHECK_URL.replace("https://", "http://")

# --- Validate required variables ---
if not BOT_TOKEN or not GROUP_CHAT_ID or not LICENSE_CHECK_URL or not ADMIN_USER_ID:
    raise ValueError("🚨 ERROR: Missing environment variables in the .env file")

# --- Configure logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logger.info("✅ Bot successfully initialized with environment variables.")

# --- Persistence Functions ---
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

# --- File Names for Persistence ---
LICENSE_STORAGE_FILE = "used_licenses.json"
ATTEMPTS_STORAGE_FILE = "user_attempts.json"
BLOCKED_USERS_FILE = "blocked_users.json"           # Automatic blocked user IDs (stored as list)
BLOCKED_USERS_DICT_FILE = "blocked_users_dict.json"   # Manual blocked users (username: user_id)
USER_DATA_FILE = "user_data.json"                     # Stores user display name info keyed by user ID

# --- Global Variables Initialization ---
failed_attempts = {}
blocked_users = set(load_json_data(BLOCKED_USERS_FILE) or [])
blocked_users_dict = load_json_data(BLOCKED_USERS_DICT_FILE) or {}
# This dictionary will store user details (username or first name) keyed by user id as string.
user_data = load_json_data(USER_DATA_FILE) or {}
processing_users = set()
verification_codes = {}
# Rate limiting: maximum 5 attempts per minute per user
RATE_LIMIT = 5
attempt_timestamps = {}  # user_id -> list of timestamp floats
MAX_RETRIES = 3
MAX_FAILED_ATTEMPTS = 5
DELETE_AFTER_SECONDS = 600  # 10 minutes (600 seconds)
# Set of users whose session is terminated (blocked)
session_ended = set()

# --- New Global Variable for Verified Users ---
verified_users = set()  # Holds user IDs whose license has been verified

# --- Configure a Requests Session with TLSAdapter if needed ---
session = requests.Session()
if LICENSE_CHECK_URL.startswith("https://"):
    class TLSAdapter(HTTPAdapter):
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

# --- Helper Functions ---
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

# --- Function to dynamically reload block lists ---
async def reload_blocked_users(context: ContextTypes.DEFAULT_TYPE):
    global blocked_users, blocked_users_dict, session_ended
    new_blocked = set(load_json_data(BLOCKED_USERS_FILE) or [])
    new_blocked_dict = load_json_data(BLOCKED_USERS_DICT_FILE) or {}
    blocked_users = new_blocked
    blocked_users_dict = new_blocked_dict
    # Remove from session_ended those users that are no longer blocked.
    session_ended = {uid for uid in session_ended if uid in blocked_users}
    logger.info("Blocked users reloaded from file.")

# --- Bot Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command. Processes only private chats."""
    if update.message.chat.type != "private":
        return  # Do not process messages from groups
    user_id = update.effective_user.id
    if user_id in session_ended:
        await send_and_schedule_delete(update, context, "You are blocked. Please contact admin @SanchezC137Media.")
        return
    logger.info(f"Received /start from user: {user_id}")
    welcome_message = (
        "👋 Welcome! Please provide your license key for verification.\n\n"
        "Once verified, you'll receive an invite link. Please note that "
        "when you join the group, your join request will require admin approval."
    )
    await send_and_schedule_delete(update, context, welcome_message)

async def handle_license(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles license key verification and invite link generation. Processes only private chats."""
    if update.message.chat.type != "private":
        return
    global failed_attempts, blocked_users, verification_codes, attempt_timestamps, session_ended, verified_users, user_data
    user_id = update.effective_user.id

    # Update user details in our storage (store using string key)
    user_data[str(user_id)] = update.effective_user.username if update.effective_user.username else update.effective_user.first_name
    save_json_data(USER_DATA_FILE, user_data)

    if user_id in session_ended:
        await send_and_schedule_delete(update, context, "You are blocked. Please contact admin @SanchezC137Media.")
        return

    license_key = update.message.text.strip()

    # Rate limiting: Allow max RATE_LIMIT attempts per minute
    now = time.time()
    attempt_timestamps.setdefault(user_id, [])
    attempt_timestamps[user_id] = [t for t in attempt_timestamps[user_id] if now - t < 60]
    if len(attempt_timestamps[user_id]) >= RATE_LIMIT:
        await update.message.reply_text("🚫 Too many attempts per minute. Please wait a minute before trying again.")
        return
    attempt_timestamps[user_id].append(now)

    # Check if the user is already in the group
    if await is_user_in_group(user_id, context):
        friendly_message = "🎉 Congratulations! You are already a valued member of the group. Enjoy your stay!"
        if license_key and license_key not in verification_codes:
            verification_codes[license_key] = user_id
            save_json_data(LICENSE_STORAGE_FILE, verification_codes)
        await send_and_schedule_delete(update, context, friendly_message)
        return

    # Validate the license via API
    try:
        if not LICENSE_CHECK_URL:
            await update.message.reply_text("⚠️ Internal error. Please contact support.")
            return
        response = session.post(LICENSE_CHECK_URL, data={"licensekey": license_key}, timeout=10)
        response.raise_for_status()
        response_data = response.json()
        logger.info(f"License check response: {response_data}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error verifying license key: {e}")
        await send_and_schedule_delete(update, context, "⚠️ Error verifying license key. Please try again later.")
        return

    if response_data.get("status", "").lower() == "valid":
        # If the license was already used by another user, block this user.
        if license_key in verification_codes and verification_codes[license_key] != user_id:
            blocked_users.add(user_id)
            save_json_data(BLOCKED_USERS_FILE, list(blocked_users))
            if user_id not in session_ended:
                await send_and_schedule_delete(
                    update, context,
                    "You are blocked. Please contact admin @SanchezC137Media."
                )
                session_ended.add(user_id)
            return

        # Mark user as verified
        verified_users.add(user_id)
        
        # Generate the invite link (the group should be set to require join requests)
        invite_link = await generate_invite_link(context)
        if invite_link:
            success_message = (
                f"✅ Your license key has been verified!\n\n"
                f"Please click the link below to send a join request to the group. "
                f"An administrator will review your request.\n\n"
                f"<tg-spoiler><a href=\"{invite_link}\">Join Group</a></tg-spoiler>"
            )
            await send_and_schedule_delete(update, context, success_message, parse_mode=constants.ParseMode.HTML)
            verification_codes[license_key] = user_id
            save_json_data(LICENSE_STORAGE_FILE, verification_codes)
            failed_attempts.pop(user_id, None)
        else:
            await send_and_schedule_delete(update, context, "⚠️ Unable to generate invite link. Contact admin.")
    else:
        failed_attempts[user_id] = failed_attempts.get(user_id, 0) + 1
        if failed_attempts[user_id] >= MAX_FAILED_ATTEMPTS:
            blocked_users.add(user_id)
            save_json_data(BLOCKED_USERS_FILE, list(blocked_users))
            if user_id not in session_ended:
                await send_and_schedule_delete(
                    update, context,
                    "You are blocked. Please contact admin @SanchezC137Media."
                )
                session_ended.add(user_id)
        else:
            attempts_left = MAX_FAILED_ATTEMPTS - failed_attempts[user_id]
            await send_and_schedule_delete(update, context, f"❌ Invalid license key. Please try again. You have {attempts_left} attempts left.")
    processing_users.discard(user_id)

# --- Administrative Commands (only process in private chats) ---

async def admin_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows the admin to block a user by username."""
    if update.message.chat.type != "private":
        return
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
    """
    Unified command to unblock a user by either username or user ID,
    handling both manual and automatic blocks.
    Usage: /unblock <username or user_id>
    """
    if update.message.chat.type != "private":
        return
    if update.effective_user.id != int(ADMIN_USER_ID):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /unblock <username or user_id>")
        return

    identifier = context.args[0].strip()
    target_id = None

    # If the identifier is numeric, treat it as a user ID.
    if identifier.isdigit():
        target_id = int(identifier)
    else:
        # Otherwise, treat it as a username (strip "@" if present).
        username = identifier.lstrip('@')
        # First, check the manual block dictionary.
        if username in blocked_users_dict:
            target_id = blocked_users_dict[username]
        else:
            # If not found, search through automatically blocked users using stored user data.
            for uid in blocked_users:
                stored_username = user_data.get(str(uid))
                if stored_username and stored_username.lower() == username.lower():
                    target_id = uid
                    break

    if target_id is None:
        await update.message.reply_text(f"❌ User {identifier} is not blocked.")
        return

    if target_id not in blocked_users:
        await update.message.reply_text(f"❌ User with ID {target_id} is not in the block list.")
        return

    # Remove the user from the automatic block list.
    blocked_users.remove(target_id)
    # Also remove any entries in the manual block dictionary with this user ID.
    keys_to_remove = [k for k, v in blocked_users_dict.items() if v == target_id]
    for key in keys_to_remove:
        del blocked_users_dict[key]

    save_json_data(BLOCKED_USERS_FILE, list(blocked_users))
    save_json_data(BLOCKED_USERS_DICT_FILE, blocked_users_dict)
    # Also remove the user from the terminated session set to allow future interactions.
    session_ended.discard(target_id)

    # Retrieve a display name from user_data if available.
    display_name = user_data.get(str(target_id), f"ID {target_id}")
    await update.message.reply_text(f"✅ User {display_name} (ID: {target_id}) has been unblocked.")

async def admin_blocked_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists all blocked users (both automatic and manual) for the admin,
    showing both ID and username (if available)."""
    if update.message.chat.type != "private":
        return
    if update.effective_user.id != int(ADMIN_USER_ID):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return

    message = "🚫 Blocked Users:\n"
    # List automatic blocked users from the blocked_users set.
    if blocked_users:
        message += "Automatic Blocks:\n"
        for user_id in blocked_users:
            # Try to get stored user info from our user_data dictionary (keys are strings)
            user_info = user_data.get(str(user_id))
            if user_info:
                message += f"{user_info} (ID: {user_id})\n"
            else:
                # Fallback: attempt to get chat info from Telegram
                try:
                    user = await context.bot.get_chat(user_id)
                    if user.username:
                        user_info = f"@{user.username}"
                    else:
                        user_info = f"{user.first_name}"
                except Exception:
                    user_info = "username unknown"
                message += f"{user_info} (ID: {user_id})\n"
    else:
        message += "No automatic blocks.\n"

    # List manual blocked users from the blocked_users_dict.
    if blocked_users_dict:
        message += "Manual Blocks:\n"
        for username, user_id in blocked_users_dict.items():
            message += f"@{username} (ID: {user_id})\n"
    else:
        message += "No manual blocks.\n"

    await update.message.reply_text(message)

async def admin_unblockid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows the admin to unblock a user by directly providing the user ID."""
    if update.message.chat.type != "private":
        return
    if update.effective_user.id != int(ADMIN_USER_ID):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /unblockid <user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID format. It must be numeric.")
        return
    if target_id in blocked_users:
        blocked_users.remove(target_id)
        # Remove any entries in blocked_users_dict that have this user ID
        keys_to_remove = [k for k, v in blocked_users_dict.items() if v == target_id]
        for key in keys_to_remove:
            del blocked_users_dict[key]
        save_json_data(BLOCKED_USERS_FILE, list(blocked_users))
        save_json_data(BLOCKED_USERS_DICT_FILE, blocked_users_dict)
        # Also remove the user from the terminated session set to allow future interactions
        session_ended.discard(target_id)
        await update.message.reply_text(f"✅ User with ID {target_id} has been unblocked.")
    else:
        await update.message.reply_text(f"❌ User with ID {target_id} is not in the block list.")

# --- New Handler for Join Requests (for the extra approval process) ---
async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles a join request.
    If the user is verified (license verified), notify the admin with inline buttons
    so that the admin can approve or decline the join request.
    Otherwise, automatically decline the join request.
    """
    join_request = update.chat_join_request
    user = join_request.from_user
    chat = join_request.chat

    # Only allow join requests for verified users
    if user.id in verified_users:
        text = (
            f"User {user.first_name} (@{user.username if user.username else 'no username'}) has requested to join the group.\n"
            f"User ID: {user.id}"
        )
        keyboard = [
            [
                InlineKeyboardButton("Approve", callback_data=f"approve:{user.id}"),
                InlineKeyboardButton("Decline", callback_data=f"decline:{user.id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.bot.send_message(chat_id=int(ADMIN_USER_ID), text=text, reply_markup=reply_markup)
    else:
        # Decline join requests from users that are not verified
        await context.bot.decline_chat_join_request(chat.id, user.id)

# --- Callback Query Handler for Join Request Approval ---
async def join_request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Processes the inline button callbacks for approving or declining join requests.
    Expected callback data format: "approve:<user_id>" or "decline:<user_id>".
    """
    query = update.callback_query
    data = query.data
    try:
        user_id = int(data.split(":")[1])
    except (IndexError, ValueError):
        await query.answer("Invalid data")
        return

    if data.startswith("approve:"):
        try:
            await context.bot.approve_chat_join_request(GROUP_CHAT_ID, user_id)
            await query.answer("User approved")
            await query.edit_message_text(f"User with ID {user_id} approved.")
        except Exception as e:
            logger.error(f"Error approving join request for {user_id}: {e}")
            await query.answer("Error approving join request")
    elif data.startswith("decline:"):
        try:
            await context.bot.decline_chat_join_request(GROUP_CHAT_ID, user_id)
            await query.answer("User declined")
            await query.edit_message_text(f"User with ID {user_id} declined.")
        except Exception as e:
            logger.error(f"Error declining join request for {user_id}: {e}")
            await query.answer("Error declining join request")

# --- Global Dictionary for Manual Blocked Users (persisted) ---
blocked_users_dict = load_json_data(BLOCKED_USERS_DICT_FILE) or {}

# --- Set Commands Programmatically ---
async def set_commands(bot):
    commands = [
        ("start", "Start the bot"),
        ("block", "Block a user by username (admin only)"),
        ("unblock", "Unblock a user by username or ID (admin only)"),
        ("blockuserslist", "List blocked users (admin only)"),
        ("unblockid", "Unblock a user by ID (admin only)")
    ]
    await bot.set_my_commands(commands)

# --- Main function to set commands and run the bot ---
async def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Set bot commands
    await set_commands(application.bot)

    # Schedule periodic reloading of blocked users (every 60 seconds)
    application.job_queue.run_repeating(reload_blocked_users, interval=60, first=10)

    # Register regular handlers (only process private chats)
    application.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_license))

    # Register administrative command handlers (only process private chats)
    application.add_handler(CommandHandler("block", admin_block, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("unblock", admin_unblock, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("blockuserslist", admin_blocked_users_list, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("unblockid", admin_unblockid, filters=filters.ChatType.PRIVATE))

    # Register handler for join requests
    application.add_handler(ChatJoinRequestHandler(handle_join_request))
    # Register callback query handler for inline buttons (join request approvals)
    application.add_handler(CallbackQueryHandler(join_request_callback))

    # Run polling with optimized parameters: long polling with timeout=60, poll_interval=1.0, and don't close the event loop.
    await application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        timeout=60,
        poll_interval=1.0,
        close_loop=False
    )

if __name__ == "__main__":
    try:
        # Create a new event loop and set it to avoid conflicts
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())
    except RuntimeError as e:
        if "already running" in str(e):
            loop = asyncio.get_event_loop()
            loop.create_task(main())
            loop.run_forever()
        else:
            raise e
