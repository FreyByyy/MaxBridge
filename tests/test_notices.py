"""Тихие уведомления о связи с MAX."""

import asyncio
from pathlib import Path

from bot import max_client
from bot.max_client import MaxService


async def nothing(*_):  # noqa: ANN002, ANN201
    return None


def service(tmp_path: Path, notes: list[str]) -> MaxService:
    async def notify(text: str) -> None:
        notes.append(text)

    return MaxService(tmp_path, notify, nothing, nothing, nothing)


async def test_first_connect_announced_short_drops_silent(tmp_path):
    notes: list[str] = []
    svc = service(tmp_path, notes)
    await svc._connected("Я")
    assert notes == ["✅ MAX подключён: Я"]
    svc._disconnected()
    await svc._connected("Я")  # обрыв на пару секунд
    assert len(notes) == 1


async def test_long_outage_alert_and_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(max_client, "OUTAGE_ALERT_AFTER", 0.05)
    notes: list[str] = []
    svc = service(tmp_path, notes)
    await svc._connected("Я")
    svc._disconnected()
    await asyncio.sleep(0.15)
    assert "недоступен" in notes[-1]
    await svc._connected("Я")
    assert "снова на связи" in notes[-1]


async def test_reconnect_after_five_minutes_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(max_client, "RECONNECT_NOTICE_AFTER", 0)
    monkeypatch.setattr(max_client, "OUTAGE_ALERT_AFTER", 60)
    notes: list[str] = []
    svc = service(tmp_path, notes)
    await svc._connected("Я")
    svc._disconnected()
    await svc._connected("Я")
    assert "снова на связи" in notes[-1]
