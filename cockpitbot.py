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
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

# ✅ Cargar variables de entorno
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")
LICENSE_CHECK_URL = os.getenv("LICENSE_CHECK_URL")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")

# ✅ Asegurar que la API se use en HTTP si no tiene SSL
if LICENSE_CHECK_URL.startswith("https://"):
    LICENSE_CHECK_URL = LICENSE_CHECK_URL.replace("https://", "http://")

# ✅ Validar que las variables necesarias están presentes
if not BOT_TOKEN or not GROUP_CHAT_ID or not LICENSE_CHECK_URL or not ADMIN_USER_ID:
    raise ValueError("🚨 ERROR: Missing environment variables in the .env file")

# ✅ Configuración de logs
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ✅ Variables globales
failed_attempts = {}
blocked_users = set()
processing_users = set()
verification_codes = {}
MAX_RETRIES = 3
MAX_FAILED_ATTEMPTS = 5
DELETE_AFTER_SECONDS = 600

# ✅ Configurar sesión de requests para HTTP y HTTPS
session = requests.Session()

if LICENSE_CHECK_URL.startswith("https://"):
    class TLSAdapter(HTTPAdapter):
        """Forzar compatibilidad con TLS"""
        def init_poolmanager(self, *args, **kwargs):
            context = create_urllib3_context()
            context.set_ciphers("DEFAULT@SECLEVEL=1")  # Reduce la seguridad para compatibilidad
            kwargs["ssl_context"] = context
            super().init_poolmanager(*args, **kwargs)

    session.mount("https://", TLSAdapter())
else:
    session.mount("http://", HTTPAdapter())

# ✅ Funciones auxiliares

def escape_markdown(text):
    """Escapa caracteres especiales en MarkdownV2."""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(r'([{}])'.format(re.escape(escape_chars)), r'\\\1', text)

async def is_user_in_group(user_id, context):
    """Verifica si el usuario ya está en el grupo."""
    try:
        member = await context.bot.get_chat_member(GROUP_CHAT_ID, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logger.error(f"Error checking user membership for {user_id}: {e}")
        return False

async def generate_invite_link(context):
    """Genera un enlace de invitación con reintentos."""
    for attempt in range(MAX_RETRIES):
        try:
            invite_link = await context.bot.create_chat_invite_link(
                GROUP_CHAT_ID, expire_date=int(time.time() + 12)
            )
            return invite_link.invite_link
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed to generate invite link: {e}")
            await asyncio.sleep(2)
    return None

async def delete_message(context: CallbackContext):
    """Borra un mensaje después de un tiempo."""
    chat_id, message_id = context.job.data
    try:
        await context.bot.delete_message(chat_id, message_id)
    except Exception as e:
        logger.error(f"Failed to delete message {message_id}: {e}")

async def send_and_schedule_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, parse_mode=None):
    if update.effective_chat and update.effective_chat.type != 'private':
        return
    """Envía un mensaje y lo programa para ser eliminado."""
    sent_message = await update.message.reply_text(text, parse_mode=parse_mode)
    context.job_queue.run_once(delete_message, DELETE_AFTER_SECONDS, data=(update.message.chat_id, sent_message.message_id))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat and update.effective_chat.type != 'private':
        return
    """Maneja el comando /start."""
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
    if update.effective_chat and update.effective_chat.type != 'private':
        return
    """Maneja la verificación de licencias y generación de enlaces de invitación."""
    global failed_attempts, blocked_users, verification_codes
    
    user_id = update.effective_user.id
    license_key = update.message.text.strip()

    if user_id in blocked_users:
        await send_and_schedule_delete(update, context, "🚫 You have been blocked due to multiple incorrect attempts. Contact admin @SanchezC137Media.")
        return

    if license_key in verification_codes and verification_codes[license_key] != user_id:
        blocked_users.add(user_id)
        await send_and_schedule_delete(update, context, "🚫 This verification code has already been used. Contact admin @SanchezC137Media.")
        return
    
    processing_users.add(user_id)

    try:
        response = session.post(LICENSE_CHECK_URL, data={"licensekey": license_key}, timeout=10)
        response.raise_for_status()
        response_data = response.json()

        if response_data.get("status") == "Valid":
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
                await send_and_schedule_delete(update, context, f"❌ Invalid code. {MAX_FAILED_ATTEMPTS - failed_attempts[user_id]} attempts left.")
    finally:
        processing_users.discard(user_id)

if __name__ == "__main__":
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_license))
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


# ✅ Comando para bloquear usuarios manualmente (solo admin)
async def block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = context.args[0] if context.args else None
    if not user_id:
        await update.message.reply_text("❌ Debes proporcionar un ID de usuario para bloquear.")
        return
    
    if str(update.message.from_user.id) != ADMIN_USER_ID:
        await update.message.reply_text("🚫 No tienes permisos para usar este comando.")
        return

    blocked_users.add(int(user_id))
    await update.message.reply_text(f"✅ Usuario {user_id} ha sido bloqueado.")

# ✅ Comando para desbloquear usuarios manualmente (solo admin)
async def unblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = context.args[0] if context.args else None
    if not user_id:
        await update.message.reply_text("❌ Debes proporcionar un ID de usuario para desbloquear.")
        return
    
    if str(update.message.from_user.id) != ADMIN_USER_ID:
        await update.message.reply_text("🚫 No tienes permisos para usar este comando.")
        return

    blocked_users.discard(int(user_id))
    await update.message.reply_text(f"✅ Usuario {user_id} ha sido desbloqueado.")

application.add_handler(CommandHandler("block", block))
application.add_handler(CommandHandler("unblock", unblock))