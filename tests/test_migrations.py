"""Старая bot.db (до доработок) должна открываться и получать новые столбцы."""

import sqlite3
from pathlib import Path

from bot.db import Database

OLD_SCHEMA = """
CREATE TABLE max_groups (
    max_chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
    forward_announcements INTEGER NOT NULL DEFAULT 1, forward_dialogs INTEGER NOT NULL DEFAULT 0,
    enabled_at INTEGER NOT NULL
);
CREATE TABLE moderation (
    id INTEGER PRIMARY KEY AUTOINCREMENT, max_chat_id INTEGER NOT NULL, max_msg_id INTEGER NOT NULL,
    owner_msg_id INTEGER, payload TEXT NOT NULL, created_at INTEGER NOT NULL,
    UNIQUE (max_chat_id, max_msg_id)
);
INSERT INTO max_groups VALUES (77, 'Работа', 1, 0, 0);
INSERT INTO moderation (max_chat_id, max_msg_id, payload, created_at) VALUES (77, 1, '{}', 0);
"""


async def test_old_database_is_migrated(tmp_path: Path):
    path = tmp_path / "bot.db"
    con = sqlite3.connect(path)
    con.executescript(OLD_SCHEMA)
    con.close()

    db = Database(path)
    await db.open()
    group = await db.get_group(77)
    assert group is not None and group.full_moderation is False and group.silent_dialogs is False
    assert (await db.list_moderation())[0].kind == "disputed"
    await db.set_group_flag(77, "full_moderation", True)
    assert (await db.get_group(77)).full_moderation is True
    await db.close()
    # Повторное открытие не пытается добавить столбцы ещё раз.
    db = Database(path)
    await db.open()
    await db.close()
