"""OfferIQ Telegram bot.

Step 0: replies to /start and /help so classmates can start the bot early
(a Telegram bot can only message people who have started it).
Run locally in polling mode:  uv run python -m app.bot.telegram_bot
"""

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, TypeHandler

from app.config import get_settings

log = logging.getLogger("offeriq.bot")

WELCOME = (
    "🙏 <b>Namaste, {name}! Welcome to OfferIQ.</b>\n\n"
    "OfferIQ is a hackathon project: an engine that decides which shopping offer suits you, "
    "and checks every offer against the shop's rules before you see it.\n\n"
    "🔒 Everything here is <b>simulated</b>: no real orders, no payments, no real data about you.\n\n"
    "You're on the list! Soon you'll take a 4-tap quiz, meet your shopping 'digital twin', "
    "and start receiving personalised offers. Stay tuned. 🎁\n\n"
    "Type /help any time."
)

HELP = (
    "<b>OfferIQ commands</b>\n"
    "/start: join and see the welcome note\n"
    "/help: this message\n\n"
    "Coming soon: /browse, /history, /stop (opt out of offers)."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    name = user.first_name if user and user.first_name else "there"
    log.info("start from chat_id=%s", update.effective_chat.id if update.effective_chat else None)
    await update.effective_message.reply_text(WELCOME.format(name=name), parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log every incoming update (type + chat id only, never message content)."""
    chat_id = update.effective_chat.id if update.effective_chat else None
    kind = next((k for k, v in update.to_dict().items() if k != "update_id" and v), "unknown")
    log.info("update %s: %s from chat_id=%s", update.update_id, kind, chat_id)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("handler error", exc_info=context.error)


async def announce(application: Application) -> None:
    me = await application.bot.get_me()
    log.info("Bot is live: https://t.me/%s, send it /start", me.username)


def build_application(token: str) -> Application:
    application = Application.builder().token(token).post_init(announce).build()
    application.add_handler(TypeHandler(Update, log_update), group=-1)
    application.add_error_handler(on_error)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    return application


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    # httpx logs full request URLs, which contain the bot token — keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    token = get_settings().telegram_bot_token
    if token is None or not token.get_secret_value():
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set. Add it to the repo-root .env (see .env.example).")

    log.info("Starting OfferIQ bot in polling mode")
    build_application(token.get_secret_value()).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
