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
