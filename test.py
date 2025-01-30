import json
import logging
import requests
import re
import os
import time
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

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")  # Keep as string, no int conversion
LICENSE_CHECK_URL = os.getenv("LICENSE_CHECK_URL")

# Configure Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Global storage for processed messages to prevent duplication
processed_messages = set()
processing_users = set()  # To prevent duplicate execution of license processing
MAX_RETRIES = 3  # Maximum retries for generating an invite link

def escape_markdown(text):
    """Escapes special characters for MarkdownV2 formatting."""
    escape_chars = r'_*[\]()~`>#+-=|{}.!'
    return re.sub(r'([{}])'.format(re.escape(escape_chars)), r'\\\1', text)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    if update.message.chat.type != "private":
        logger.info(f"Ignored /start from chat ID: {update.message.chat_id}")
        return

    logger.info(f"Received /start from user: {update.effective_user.id}")
    welcome_message = (
        "Welcome! Please provide your license key for verification.\n\n"
        "Once your license is verified, I will send you the invite link to the group."
    )
    await update.message.reply_text(welcome_message)

async def generate_invite_link(context):
    """Tries to generate an invite link with retries, but avoids duplicate failure messages."""
    for attempt in range(MAX_RETRIES):
        try:
            invite_link = await context.bot.create_chat_invite_link(GROUP_CHAT_ID)
            return invite_link.invite_link
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed to generate invite link: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(2)  # Retry after delay
            else:
                return None  # No more attempts after max retries

async def handle_license(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the license key submission."""
    global processed_messages, processing_users

    user_id = update.effective_user.id

    # Prevent duplicate execution
    if user_id in processing_users:
        logger.info(f"Skipping duplicate request from user {user_id}")
        return
    processing_users.add(user_id)  # Mark user as being processed

    if update.message.chat.type != "private":
        logger.info(f"Ignored message from chat ID: {update.message.chat_id}")
        return

    message_id = update.message.message_id

    if (user_id, message_id) in processed_messages:
        logger.info(f"Duplicate message detected from user {user_id}, ignoring...")
        return  # 🔹 Avoid processing the same message multiple times

    processed_messages.add((user_id, message_id))  # Add the message to the processed set

    license_key = update.message.text.strip()
    logger.info(f"License key received: {license_key} from user {user_id}")

    if not LICENSE_CHECK_URL:
        logger.error("🚨 LICENSE_CHECK_URL is missing from .env file")
        await update.message.reply_text("⚠️ Internal error. Please contact support.")
        return

    try:
        # Verify the license key via the provided API
        response = requests.post(LICENSE_CHECK_URL, data={"licensekey": license_key}, timeout=10)
        logger.debug(f"License verification response: {response.text}")
        response.raise_for_status()

        try:
            response_data = response.json()
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON response from API: {response.text}")
            await update.message.reply_text("⚠️ Error processing license verification. Please try again later.")
            return

        if response_data.get("status") == "Valid":
            # Ensure bot has permission to invite users
            chat_member = await context.bot.get_chat_member(GROUP_CHAT_ID, context.bot.id)
            if chat_member.status not in ["administrator", "creator"]:
                logger.error("🚨 The bot is not an admin in the group!")
                await update.message.reply_text(
                    "⚠️ I don't have permission to create invite links. Please contact the admin."
                )
                return

            # Try generating an invite link
            invite_link = await generate_invite_link(context)

            if invite_link:
                success_message = escape_markdown(
                    f"✅ Your license key has been verified\n\n"
                    f"Here is your invite link to the group: [Join Group]({invite_link})"
                )
                await update.message.reply_text(success_message, parse_mode=constants.ParseMode.MARKDOWN_V2)
                logger.info(f"Invite link sent to user {user_id}: {invite_link}")
            else:
                error_message = escape_markdown(
                    "⚠️ I couldn't generate an invite link after multiple attempts. Please contact the admin."
                )
                await update.message.reply_text(error_message, parse_mode=constants.ParseMode.MARKDOWN_V2)

            return  # 🔹 Stop execution to avoid duplicate messages

        else:
            await update.message.reply_text(
                "❌ Invalid license key. Please make sure you have entered a valid key."
            )

    except requests.exceptions.RequestException as e:
        logger.error(f"Error verifying license key: {e}")
        await update.message.reply_text(
            "⚠️ An error occurred while verifying your license key. Please try again later."
        )

    finally:
        # Ensure the user is removed from the processing set after completion
        processing_users.discard(user_id)

def main():
    """Main function to run the bot."""
    print("✅ Starting bot...")  # Debugging message

    if not BOT_TOKEN or not GROUP_CHAT_ID or not LICENSE_CHECK_URL:
        logger.error("🚨 Missing required environment variables!")
        print("🚨 Missing required environment variables! Check .env file.")
        return

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_license))

    print("✅ Bot is running...")  # Debugging message
    logger.info("Bot is starting...")

    # ✅ Solution: Removed `clean=True`
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
