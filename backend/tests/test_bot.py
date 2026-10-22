from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.telegram_bot import build_application, help_command, start


def _update(first_name: str | None):
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(
        effective_user=SimpleNamespace(first_name=first_name),
        effective_chat=SimpleNamespace(id=42),
        effective_message=message,
    ), message


@pytest.mark.asyncio
async def test_start_greets_user_by_name_and_says_data_is_simulated():
    update, message = _update("Dilnaz")
    await start(update, None)
    text = message.reply_text.call_args.args[0]
    assert "Dilnaz" in text
    assert "simulated" in text


@pytest.mark.asyncio
async def test_start_handles_missing_name():
    update, message = _update(None)
    await start(update, None)
    assert "there" in message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_help_lists_commands():
    update, message = _update("A")
    await help_command(update, None)
    assert "/start" in message.reply_text.call_args.args[0]


def test_application_registers_commands():
    application = build_application("123456:TEST")
    commands = {
        c for group in application.handlers.values() for h in group for c in getattr(h, "commands", ())
    }
    assert {"start", "help"} <= commands
