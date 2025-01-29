import json
import logging
import requests
from telegram import Update, constants
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Fetch configuration from environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")
LICENSE_CHECK_URL = int(os.getenv("LICENSE_CHECK_URL"))

# Configure Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    if update.message.chat.type != "private":
        # Ignore commands from groups or channels
        logger.info(f"Ignored /start from chat ID: {update.message.chat_id}")
        return

    logger.info(f"Received /start from user: {update.effective_user.id}")
    welcome_message = (
        "Welcome! Please provide your license key for verification.\n\n"
        "Once your license is verified, I will send you the invite link to the group."
    )
    await update.message.reply_text(welcome_message)

async def handle_license(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the license key submission."""
    if update.message.chat.type != "private":
        # Ignore messages from groups or channels
        logger.info(f"Ignored message from chat ID: {update.message.chat_id}")
        return

    license_key = update.message.text.strip()
    logger.info(f"License key received: {license_key} from user {update.effective_user.id}")

    try:
        # Verify the license key via the provided API
        response = requests.post(LICENSE_CHECK_URL, data={"licensekey": license_key}, timeout=10)
        logger.debug(f"License verification response: {response.text}")
        response.raise_for_status()
        response_data = response.json()

        if response_data.get("valid"):
            # License key is valid, generate the invite link
            try:
                invite_link = await context.bot.create_chat_invite_link(chat_id=GROUP_CHAT_ID)
                success_message = (
                    "✅ Your license key has been verified!\n\n"
                    f"Here is your invite link to the group: [Join Group]({invite_link.invite_link})"
                )
                await update.message.reply_text(success_message, parse_mode=constants.ParseMode.MARKDOWN_V2)
                logger.info(f"Invite link sent to user {update.effective_user.id}: {invite_link.invite_link}")
            except Exception as e:
                logger.error(f"Error generating invite link: {e}")
                await update.message.reply_text(
                    "⚠️ I couldn't generate an invite link. Please contact the admin."
                )
        else:
            # License key is invalid
            await update.message.reply_text(
                "❌ Invalid license key. Please make sure you have entered a valid key."
            )
    except requests.exceptions.RequestException as e:
        logger.error(f"Error verifying license key: {e}")
        await update.message.reply_text(
            "⚠️ An error occurred while verifying your license key. Please try again later."
        )

def main():
    """Main function to run the bot."""
    if not BOT_TOKEN or not GROUP_CHAT_ID or not LICENSE_CHECK_URL:
        logger.error("Missing required environment variables.")
        return

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_license))

    # Start the bot
    logger.info("Bot is starting...")
    application.run_polling()

if __name__ == "__main__":
    main()
