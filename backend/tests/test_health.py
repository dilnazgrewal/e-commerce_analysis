from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_reports_ok_without_leaking_secrets(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret-token-value")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        response = client.get("/api/health")
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["integrations"]["telegram"] is True
    assert "secret-token-value" not in response.text


def test_webhook_rejects_forged_calls_and_queues_real_ones(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from app.config import get_settings

    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cr+/et==")  # base64-like, as hosts generate
    get_settings.cache_clear()
    queue: asyncio.Queue = asyncio.Queue()
    app.state.bot = SimpleNamespace(update_queue=queue, bot=None)
    try:
        update = {
            "update_id": 1,
            "message": {"message_id": 1, "date": 0, "chat": {"id": 5, "type": "private"}, "text": "/start"},
        }
        assert client.post("/api/telegram/webhook", json=update).status_code == 403
        forged = client.post(
            "/api/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "no"}
        )
        assert forged.status_code == 403 and queue.empty()
        from app.main import telegram_secret_token

        token = telegram_secret_token(get_settings())
        assert token.isalnum()  # Telegram only accepts A-Z a-z 0-9 _ - in the secret
        ok = client.post(
            "/api/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": token}
        )
        assert ok.status_code == 200 and queue.qsize() == 1
    finally:
        app.state.bot = None
        get_settings.cache_clear()


def test_postgres_urls_use_the_psycopg_driver():
    from app.db import normalise_url

    assert normalise_url("postgresql://u:p@h/db?sslmode=require").startswith("postgresql+psycopg://")
    assert normalise_url("postgres://u:p@h/db").startswith("postgresql+psycopg://")
    assert normalise_url("sqlite:///./x.db") == "sqlite:///./x.db"
