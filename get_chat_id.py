from telegram import Bot

BOT_TOKEN = "8173206813:AAF38N1E0N7PJl7hbvY2U5CvVDen18Qf-8c"

bot = Bot(token=BOT_TOKEN)

updates = bot.get_updates()

for update in updates:
    if update.message:
        chat_id = update.message.chat.id
        print(f"Chat ID: {chat_id}")
