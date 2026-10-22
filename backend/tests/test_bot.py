"""Bot handlers with fake Telegram updates (no network)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlmodel import SQLModel, create_engine

from app import models  # noqa: F401  (registers tables)
from app.bot import store
from app.bot.service import Services
from app.bot.telegram_bot import build_application, help_command, start
from tests.rules_helpers import POLICY, effects


@pytest.fixture
def context(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'b.db'}")
    SQLModel.metadata.create_all(engine)
    return SimpleNamespace(bot_data={"svc": Services(engine, POLICY, effects(), None)})


def _update(first_name: str | None, chat_id: int = 42):
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(
        effective_user=SimpleNamespace(first_name=first_name),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=message,
    ), message


@pytest.mark.asyncio
async def test_start_greets_user_by_name_says_data_is_simulated_and_registers(context):
    update, message = _update("Dilnaz")
    await start(update, context)
    text = message.reply_text.call_args.args[0]
    assert "Dilnaz" in text and "simulated" in text and "first name" in text
    user = store.get_user(context.bot_data["svc"].engine, 42)
    assert user.first_name == "Dilnaz" and user.consent_at is None  # consent comes with the button


@pytest.mark.asyncio
async def test_start_handles_missing_name(context):
    update, message = _update(None)
    await start(update, context)
    assert "there" in message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_help_lists_commands(context):
    update, message = _update("A")
    await help_command(update, context)
    text = message.reply_text.call_args.args[0]
    assert all(cmd in text for cmd in ("/start", "/browse", "/offer", "/history", "/stop"))


def test_application_registers_commands_and_buttons():
    application = build_application("123456:TEST")
    handlers = [h for group in application.handlers.values() for h in group]
    commands = {c for h in handlers for c in getattr(h, "commands", ())}
    assert {"start", "help", "offer", "browse", "history", "stop", "resume"} <= commands
    patterns = {h.pattern.pattern for h in handlers if getattr(h, "pattern", None) is not None}
    assert {r"^consent$", r"^q:", r"^b:", r"^cart:", r"^rsn:", r"^(claim|nope|why):"} <= patterns
