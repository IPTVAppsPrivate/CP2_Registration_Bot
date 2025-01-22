import json
import logging
import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    constants
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import os
from dotenv import load_dotenv

# Load environment variables from .env file (useful for local testing)
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Custom escape function for Markdown V2
def escape_markdown_v2(text):
    escape_chars = r'\\_*[]()~`>#+-=|{}.!'
    return ''.join(['\\' + c if c in escape_chars else c for c in text])

# Fetch environment variables
BOT_TOKEN = os.getenv('BOT_TOKEN')  # Telegram bot token
GROUP_CHAT_ID = int(os.getenv('GROUP_CHAT_ID', -100123456789))  # Telegram group chat ID
LICENSE_CHECK_URL = os.getenv('LICENSE_CHECK_URL', "http://example.com/check.php")  # License verification URL

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_message = (
        "*Welcome to the world of Cockpit!*\n\n"
        "I am Cockpit's verifier bot. Our group is a place where you can report bugs, be kept in the loop about our latest features, "
        "find extra goodies and mods... But most importantly, it's a chat group to offer our paying customers support!\n\n"
        "Please send me your license key for verification."
    )
    escaped_message = escape_markdown_v2(welcome_message)
    await update.message.reply_text(escaped_message, parse_mode=constants.ParseMode.MARKDOWN_V2)

async def handle_license(update: Update, context: ContextTypes.DEFAULT_TYPE):
    license_key = update.message.text.strip()
    user_id = update.message.from_user.id

    # Store the license key in user_data for later verification
    context.user_data['pending_license'] = license_key

    # Send a message with "CHECK LICENSE" button
    keyboard = [
        [InlineKeyboardButton("✅ CHECK LICENSE", callback_data='check_license')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    confirmation_message = f"You entered: *{escape_markdown_v2(license_key)}*\n\nTap the button below to verify your license key."
    escaped_confirmation_message = escape_markdown_v2(confirmation_message)
    await update.message.reply_text(
        escaped_confirmation_message,
        parse_mode=constants.ParseMode.MARKDOWN_V2,
        reply_markup=reply_markup
    )

async def check_license_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    license_key = context.user_data.get('pending_license')
    if not license_key:
        no_key_message = "❌ *No license key found. Please send your license key first.*"
        escaped_no_key_message = escape_markdown_v2(no_key_message)
        await query.edit_message_text(escaped_no_key_message, parse_mode=constants.ParseMode.MARKDOWN_V2)
        return

    try:
        # Verify license key with the server
        response = requests.post(LICENSE_CHECK_URL, data={"licensekey": license_key})
        response_data = response.json()
        
    if response.status_code == 200 and response_data.get('valid'):
            try:
                # Add the user to the group
                await context.bot.add_chat_member(
                    chat_id=GROUP_CHAT_ID,
                    user_id=query.from_user.id
                )
                success_message = "✅ *Your license key has been verified!* You've been added to the support group."
                escaped_success_message = escape_markdown_v2(success_message)
                await query.edit_message_text(escaped_success_message, parse_mode=constants.ParseMode.MARKDOWN_V2)
            except Exception as e:
                logger.error(f"Error adding user to group: {e}")
                failure_message = "⚠️ *I couldn't add you to the group.* Please ensure you have initiated a conversation with me."
                escaped_failure_message = escape_markdown_v2(failure_message)
                await query.edit_message_text(escaped_failure_message, parse_mode=constants.ParseMode.MARKDOWN_V2)
        else:
            failure_message = (
                "❌ *Invalid license key.* Please ensure you have purchased a valid license."
            )
            escaped_failure_message = escape_markdown_v2(failure_message)
            await query.edit_message_text(escaped_failure_message, parse_mode=constants.ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error verifying license key: {e}")
        error_message = "⚠️ *An error occurred while verifying your license key. Please try again later.*"
        escaped_error_message = escape_markdown_v2(error_message)
        await query.edit_message_text(escaped_error_message, parse_mode=constants.ParseMode.MARKDOWN_V2)

def main():
    # Ensure the bot token is set
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is not set.")
        return

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_license))
    application.add_handler(CallbackQueryHandler(check_license_callback, pattern='^check_license$'))

    # Start the bot
    application.run_polling()

if __name__ == '__main__':
    main()