"""OfferIQ API entry point.

Locally: the API only (the bot runs separately in polling mode). In the cloud (WEBHOOK_URL set): one process
serves the API, the dashboard (built React files) and the Telegram bot, which receives updates by webhook.
"""

import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import get_settings

log = logging.getLogger("offeriq.api")
WEBHOOK_PATH = "/api/telegram/webhook"
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    bot = None
    # Render sets RENDER_EXTERNAL_URL itself, so the webhook needs no manual configuration there.
    public_url = settings.webhook_url or os.environ.get("RENDER_EXTERNAL_URL")
    if public_url and settings.telegram_bot_token:
        from app.bot.telegram_bot import announce, build_application, make_services

        logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
        for noisy in ("httpx", "httpx2", "apscheduler"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        bot = build_application(settings.telegram_bot_token.get_secret_value(), make_services())
        await bot.initialize()
        await bot.start()  # runs handlers and the job queue; updates arrive via the webhook route
        secret = (
            settings.telegram_webhook_secret.get_secret_value() if settings.telegram_webhook_secret else None
        )
        await bot.bot.set_webhook(
            public_url.rstrip("/") + WEBHOOK_PATH,
            secret_token=secret,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=False,
        )
        await announce(bot)  # command menu + re-schedule queued offers
        log.info("Webhook mode: Telegram updates arrive at %s", WEBHOOK_PATH)
    app.state.bot = bot
    yield
    if bot is not None:
        await bot.stop()
        await bot.shutdown()


app = FastAPI(
    title="OfferIQ",
    version=__version__,
    description="Decides which offer each customer should get, when, and why — every AI choice rule-checked.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def request_bot_running() -> bool:
    return getattr(app.state, "bot", None) is not None


@app.get("/api/health")
def health() -> dict:
    """Liveness plus which integrations are configured (never the secret values themselves)."""
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "env": settings.app_env,
        "integrations": {
            "telegram": settings.telegram_bot_token is not None,
            "claude": settings.anthropic_api_key is not None,
            "webhook": request_bot_running(),
        },
    }


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> dict:
    """Telegram → bot. The secret header proves the request really comes from Telegram."""
    from telegram import Update

    bot = request.app.state.bot
    if bot is None:
        raise HTTPException(404, "bot not running in webhook mode")
    expected = get_settings().telegram_webhook_secret
    given = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if expected is None or not hmac.compare_digest(given, expected.get_secret_value()):
        raise HTTPException(403, "bad secret")
    await bot.update_queue.put(Update.de_json(await request.json(), bot.bot))
    return {"ok": True}


if FRONTEND_DIST.exists():  # the built dashboard, served by the same process (mounted last)
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="dashboard")
