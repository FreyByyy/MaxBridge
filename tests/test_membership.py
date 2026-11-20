"""Переезд группы в супергруппу не должен выглядеть как «бота добавил посторонний»."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.enums import ChatMemberStatus

from bot.db import Database
from bot.tg import membership

OWNER = 1000
STRANGER = 2000
OLD_ID = -123456789
NEW_ID = -1001234567890
MAX_CHAT = 77


class FakeBot:
    def __init__(self) -> None:
        self.left: list[int] = []
        self.sent: list[int] = []

    async def send_message(self, chat_id: int, text: str, **kw) -> None:  # noqa: ANN003
        self.sent.append(chat_id)

    async def leave_chat(self, chat_id: int) -> None:
        self.left.append(chat_id)


@pytest.fixture
async def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(membership, "MIGRATION_WAIT", 1.0)
    monkeypatch.setattr(membership, "MIGRATION_POLL", 0.05)
    monkeypatch.setattr(membership, "REMOVE_DELAY", 0.3)
    db = Database(tmp_path / "bot.db")
    await db.open()
    await db.add_tg_chat(OLD_ID, "Группа", OWNER)
    await db.set_route(OLD_ID, MAX_CHAT, True)
    notes: list[str] = []

    async def notify_owner(text: str) -> None:
        notes.append(text)

    async def leave_message() -> str:
        return "пока"

    async def can_add_bot(uid: int) -> bool:
        return uid == OWNER

    yield SimpleNamespace(
        db=db,
        bot=FakeBot(),
        notes=notes,
        can_add_bot=can_add_bot,
        is_owner=lambda uid: uid == OWNER,
        settings=SimpleNamespace(leave_message=leave_message),
        forwarder=SimpleNamespace(notify_owner=notify_owner),
    )
    await db.close()


def update(chat_id: int, chat_type: str, old: ChatMemberStatus, new: ChatMemberStatus, by: int):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type, title="Группа"),
        old_chat_member=SimpleNamespace(status=old),
        new_chat_member=SimpleNamespace(status=new),
        from_user=SimpleNamespace(id=by, first_name="Админ", last_name=None, username=None),
    )


def service(chat_id: int, **kw):
    return SimpleNamespace(chat=SimpleNamespace(id=chat_id), **kw)


LEFT, MEMBER = ChatMemberStatus.LEFT, ChatMemberStatus.MEMBER


async def test_supergroup_migration_keeps_bot_and_routes(app):
    # Порядок как у Telegram: бот «уходит» из старой группы, его «добавляет» админ в новую,
    # служебные сообщения о переезде приходят чуть позже.
    await membership.on_bot_membership(update(OLD_ID, "group", MEMBER, LEFT, STRANGER), app)
    join = asyncio.create_task(
        membership.on_bot_membership(update(NEW_ID, "supergroup", LEFT, MEMBER, STRANGER), app)
    )
    await asyncio.sleep(0.2)
    await membership.on_migrate_to(service(OLD_ID, migrate_to_chat_id=NEW_ID), app)
    await membership.on_migrate_from(service(NEW_ID, migrate_from_chat_id=OLD_ID), app)
    await join
    await asyncio.sleep(0.4)  # отложенное удаление старой группы отработало

    assert app.bot.left == []
    assert await app.db.get_tg_chat(NEW_ID) is not None
    assert await app.db.get_tg_chat(OLD_ID) is None
    assert await app.db.routes_to(NEW_ID) == {MAX_CHAT}
    assert "стала супергруппой" in app.notes[-1]


async def test_stranger_in_new_supergroup_still_rejected(app):
    await membership.on_bot_membership(update(-1009999, "supergroup", LEFT, MEMBER, STRANGER), app)
    assert app.bot.left == [-1009999]
    assert "добавил бота" in app.notes[-1]


async def test_bot_removed_from_plain_group_cleaned_up_later(app):
    await membership.on_bot_membership(update(OLD_ID, "group", MEMBER, LEFT, OWNER), app)
    assert await app.db.get_tg_chat(OLD_ID) is not None
    await asyncio.sleep(0.4)
    assert await app.db.get_tg_chat(OLD_ID) is None
