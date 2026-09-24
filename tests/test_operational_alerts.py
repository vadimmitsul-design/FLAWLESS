"""Operational notifications never call Telegram in tests or disclose bot tokens."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx

from app.db import SessionLocal
from app.workers import alerts


def test_cooldown_has_independent_instances_and_expires_at_boundary():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = alerts.AlertCooldown()
    second = alerts.AlertCooldown()
    assert first.allow("problem", now=now)
    assert not first.allow("problem", now=now + timedelta(hours=5))
    assert second.allow("problem", now=now)
    assert first.allow("problem", now=now + timedelta(hours=6))


def test_telegram_error_logging_redacts_bot_token(monkeypatch, caplog):
    token = "12345:unit-test-secret"
    error = httpx.RequestError(f"request failed at https://api.telegram.org/bot{token}/sendMessage")
    sender = AsyncMock(side_effect=error)
    monkeypatch.setattr(alerts, "collect_problems", AsyncMock(return_value=[("test", "Alert")]))
    monkeypatch.setattr(alerts, "_admin_chat_ids", AsyncMock(return_value=[123]))
    monkeypatch.setattr(alerts.telegram_bot.settings, "telegram_bot_token", token)
    monkeypatch.setattr(alerts.telegram_bot, "send_message", sender)

    async def run():
        async with SessionLocal() as session:
            await alerts.check_and_notify(session, cooldown=alerts.AlertCooldown())

    asyncio.run(run())
    sender.assert_awaited_once_with(123, "Alert")
    assert "RequestError" in caplog.text
    assert "<ТОКЕН-СКРЫТ>" in caplog.text
    assert token not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
