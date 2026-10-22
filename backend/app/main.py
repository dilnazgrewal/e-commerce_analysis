"""OfferIQ API entry point."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.config import get_settings

app = FastAPI(
    title="OfferIQ",
    version=__version__,
    description="Decides which offer each customer should get, when, and why — every AI choice rule-checked.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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
        },
    }
