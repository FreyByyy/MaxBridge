"""SQLite-хранилище: настройки, доступ, маршруты, связки сообщений, модерация."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Пользователи Telegram, которым владелец разрешил добавлять бота в чаты.
CREATE TABLE IF NOT EXISTS trusted_users (
    user_id  INTEGER PRIMARY KEY,
    name     TEXT NOT NULL DEFAULT '',
    added_at INTEGER NOT NULL
);

-- Чаты MAX, которые владелец выбрал как источники, и их фильтры.
CREATE TABLE IF NOT EXISTS max_groups (
    max_chat_id           INTEGER PRIMARY KEY,
    title                 TEXT NOT NULL DEFAULT '',
    forward_announcements INTEGER NOT NULL DEFAULT 1,
    forward_dialogs       INTEGER NOT NULL DEFAULT 0,
    full_moderation       INTEGER NOT NULL DEFAULT 0,
    silent_dialogs        INTEGER NOT NULL DEFAULT 0,
    enabled_at            INTEGER NOT NULL
);

-- Какие источники MAX доступны доверенному пользователю.
CREATE TABLE IF NOT EXISTS user_group_access (
    user_id     INTEGER NOT NULL,
    max_chat_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, max_chat_id)
);

-- Чаты Telegram, в которые бот добавлен с разрешения.
CREATE TABLE IF NOT EXISTS tg_chats (
    tg_chat_id INTEGER PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    added_by   INTEGER NOT NULL,
    added_at   INTEGER NOT NULL
);

-- Маршруты: из какого чата MAX в какой чат Telegram.
CREATE TABLE IF NOT EXISTS routes (
    tg_chat_id  INTEGER NOT NULL,
    max_chat_id INTEGER NOT NULL,
    PRIMARY KEY (tg_chat_id, max_chat_id)
);

-- Решение по каждому увиденному сообщению MAX (для правок и удалений).
CREATE TABLE IF NOT EXISTS max_messages (
    max_chat_id INTEGER NOT NULL,
    max_msg_id  INTEGER NOT NULL,
    category    TEXT NOT NULL,
    decided_by  TEXT NOT NULL,
    deleted     INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (max_chat_id, max_msg_id)
);

-- Какие сообщения Telegram показывают сообщение MAX. text_holder=1 — то, в котором текст.
CREATE TABLE IF NOT EXISTS forwarded (
    max_chat_id INTEGER NOT NULL,
    max_msg_id  INTEGER NOT NULL,
    tg_chat_id  INTEGER NOT NULL,
    tg_msg_id   INTEGER NOT NULL,
    text_holder INTEGER NOT NULL DEFAULT 0,
    is_caption  INTEGER NOT NULL DEFAULT 0,
    html        TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (tg_chat_id, tg_msg_id)
);
CREATE INDEX IF NOT EXISTS forwarded_by_max ON forwarded (max_chat_id, max_msg_id);

-- Спорные сообщения, ждущие решения владельца (висят, пока он не ответит).
CREATE TABLE IF NOT EXISTS moderation (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    max_chat_id  INTEGER NOT NULL,
    max_msg_id   INTEGER NOT NULL,
    owner_msg_id INTEGER,
    payload      TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'disputed',
    created_at   INTEGER NOT NULL,
    UNIQUE (max_chat_id, max_msg_id)
);
"""

# Столбцы, добавленные после первого выпуска: в старой bot.db их нет, и CREATE TABLE IF NOT EXISTS
# их не создаст — open() дописывает их через ALTER TABLE.
MIGRATIONS = {
    "max_groups": {
        "full_moderation": "INTEGER NOT NULL DEFAULT 0",
        "silent_dialogs": "INTEGER NOT NULL DEFAULT 0",
    },
    "moderation": {
        "kind": "TEXT NOT NULL DEFAULT 'disputed'",
    },
}

GROUP_FLAGS = {"forward_announcements", "forward_dialogs", "full_moderation", "silent_dialogs"}
MODERATION_DISPUTED = "disputed"
MODERATION_FULL = "full"


@dataclass
class MaxGroup:
    max_chat_id: int
    title: str
    forward_announcements: bool
    forward_dialogs: bool
    enabled_at: int
    full_moderation: bool = False
    silent_dialogs: bool = False


@dataclass
class ForwardedRef:
    tg_chat_id: int
    tg_msg_id: int
    text_holder: bool
    is_caption: bool
    html: str = ""


@dataclass
class Moderation:
    id: int
    max_chat_id: int
    max_msg_id: int
    owner_msg_id: int | None
    payload: dict
    kind: str = MODERATION_DISPUTED


def now() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {r["name"] for r in await self._all(f"PRAGMA table_info({table})")}
            for column, ddl in columns.items():
                if column not in existing:
                    await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database.open() не вызван"
        return self._db

    async def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        await self.db.execute(sql, params)
        await self.db.commit()

    async def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self.db.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def _one(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        async with self.db.execute(sql, params) as cur:
            return await cur.fetchone()

    # --- settings -------------------------------------------------------------------------------

    async def get_setting(self, key: str, default: Any = None) -> Any:
        row = await self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    async def set_setting(self, key: str, value: Any) -> None:
        await self._exec(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    async def delete_setting(self, key: str) -> None:
        await self._exec("DELETE FROM settings WHERE key = ?", (key,))

    # --- trusted users --------------------------------------------------------------------------

    async def add_trusted(self, user_id: int, name: str) -> None:
        await self._exec(
            "INSERT INTO trusted_users (user_id, name, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name",
            (user_id, name, now()),
        )

    async def remove_trusted(self, user_id: int) -> None:
        await self._exec("DELETE FROM trusted_users WHERE user_id = ?", (user_id,))
        await self._exec("DELETE FROM user_group_access WHERE user_id = ?", (user_id,))

    async def is_trusted(self, user_id: int) -> bool:
        return await self._one("SELECT 1 FROM trusted_users WHERE user_id = ?", (user_id,)) is not None

    async def list_trusted(self) -> list[tuple[int, str]]:
        rows = await self._all("SELECT user_id, name FROM trusted_users ORDER BY added_at")
        return [(r["user_id"], r["name"]) for r in rows]

    async def trusted_name(self, user_id: int) -> str | None:
        row = await self._one("SELECT name FROM trusted_users WHERE user_id = ?", (user_id,))
        return row["name"] if row else None

    # --- MAX sources ----------------------------------------------------------------------------

    async def enable_group(self, max_chat_id: int, title: str) -> None:
        await self._exec(
            "INSERT INTO max_groups (max_chat_id, title, enabled_at) VALUES (?, ?, ?) "
            "ON CONFLICT(max_chat_id) DO UPDATE SET title = excluded.title",
            (max_chat_id, title, now()),
        )

    async def disable_group(self, max_chat_id: int) -> None:
        await self._exec("DELETE FROM max_groups WHERE max_chat_id = ?", (max_chat_id,))
        await self._exec("DELETE FROM user_group_access WHERE max_chat_id = ?", (max_chat_id,))
        await self._exec("DELETE FROM routes WHERE max_chat_id = ?", (max_chat_id,))

    async def get_group(self, max_chat_id: int) -> MaxGroup | None:
        row = await self._one("SELECT * FROM max_groups WHERE max_chat_id = ?", (max_chat_id,))
        return _group(row) if row else None

    async def list_groups(self) -> list[MaxGroup]:
        return [_group(r) for r in await self._all("SELECT * FROM max_groups ORDER BY title")]

    async def set_group_flag(self, max_chat_id: int, flag: str, value: bool) -> None:
        if flag not in GROUP_FLAGS:
            raise ValueError(flag)
        await self._exec(f"UPDATE max_groups SET {flag} = ? WHERE max_chat_id = ?", (int(value), max_chat_id))

    # --- per-user access to sources -------------------------------------------------------------

    async def grant_access(self, user_id: int, max_chat_id: int) -> None:
        await self._exec("INSERT OR IGNORE INTO user_group_access VALUES (?, ?)", (user_id, max_chat_id))

    async def revoke_access(self, user_id: int, max_chat_id: int) -> None:
        await self._exec(
            "DELETE FROM user_group_access WHERE user_id = ? AND max_chat_id = ?", (user_id, max_chat_id)
        )
        # Маршруты из этого источника в чаты, которые добавил этот пользователь, тоже снимаются.
        await self._exec(
            "DELETE FROM routes WHERE max_chat_id = ? AND tg_chat_id IN "
            "(SELECT tg_chat_id FROM tg_chats WHERE added_by = ?)",
            (max_chat_id, user_id),
        )

    async def allowed_groups(self, user_id: int) -> set[int]:
        rows = await self._all("SELECT max_chat_id FROM user_group_access WHERE user_id = ?", (user_id,))
        return {r["max_chat_id"] for r in rows}

    # --- Telegram chats and routes --------------------------------------------------------------

    async def add_tg_chat(self, tg_chat_id: int, title: str, added_by: int) -> None:
        await self._exec(
            "INSERT INTO tg_chats (tg_chat_id, title, added_by, added_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tg_chat_id) DO UPDATE SET title = excluded.title, added_by = excluded.added_by",
            (tg_chat_id, title, added_by, now()),
        )

    async def remove_tg_chat(self, tg_chat_id: int) -> None:
        await self._exec("DELETE FROM tg_chats WHERE tg_chat_id = ?", (tg_chat_id,))
        await self._exec("DELETE FROM routes WHERE tg_chat_id = ?", (tg_chat_id,))

    async def migrate_tg_chat(self, old_id: int, new_id: int) -> None:
        for table in ("tg_chats", "routes", "forwarded"):
            await self.db.execute(f"UPDATE OR IGNORE {table} SET tg_chat_id = ? WHERE tg_chat_id = ?", (new_id, old_id))
        await self.db.commit()

    async def get_tg_chat(self, tg_chat_id: int) -> tuple[int, str, int] | None:
        row = await self._one("SELECT * FROM tg_chats WHERE tg_chat_id = ?", (tg_chat_id,))
        return (row["tg_chat_id"], row["title"], row["added_by"]) if row else None

    async def list_tg_chats(self, added_by: int | None = None) -> list[tuple[int, str, int]]:
        if added_by is None:
            rows = await self._all("SELECT * FROM tg_chats ORDER BY added_at")
        else:
            rows = await self._all("SELECT * FROM tg_chats WHERE added_by = ? ORDER BY added_at", (added_by,))
        return [(r["tg_chat_id"], r["title"], r["added_by"]) for r in rows]

    async def set_route(self, tg_chat_id: int, max_chat_id: int, on: bool) -> None:
        if on:
            await self._exec("INSERT OR IGNORE INTO routes VALUES (?, ?)", (tg_chat_id, max_chat_id))
        else:
            await self._exec(
                "DELETE FROM routes WHERE tg_chat_id = ? AND max_chat_id = ?", (tg_chat_id, max_chat_id)
            )

    async def routes_from(self, max_chat_id: int) -> list[int]:
        rows = await self._all(
            "SELECT r.tg_chat_id FROM routes r JOIN tg_chats c ON c.tg_chat_id = r.tg_chat_id "
            "WHERE r.max_chat_id = ?",
            (max_chat_id,),
        )
        return [r["tg_chat_id"] for r in rows]

    async def routes_to(self, tg_chat_id: int) -> set[int]:
        rows = await self._all("SELECT max_chat_id FROM routes WHERE tg_chat_id = ?", (tg_chat_id,))
        return {r["max_chat_id"] for r in rows}

    # --- MAX messages and their Telegram copies -------------------------------------------------

    async def get_decision(self, max_chat_id: int, max_msg_id: int) -> tuple[str, str, bool] | None:
        row = await self._one(
            "SELECT category, decided_by, deleted FROM max_messages WHERE max_chat_id = ? AND max_msg_id = ?",
            (max_chat_id, max_msg_id),
        )
        return (row["category"], row["decided_by"], bool(row["deleted"])) if row else None

    async def set_decision(self, max_chat_id: int, max_msg_id: int, category: str, decided_by: str) -> None:
        await self._exec(
            "INSERT INTO max_messages (max_chat_id, max_msg_id, category, decided_by, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(max_chat_id, max_msg_id) DO UPDATE SET "
            "category = excluded.category, decided_by = excluded.decided_by",
            (max_chat_id, max_msg_id, category, decided_by, now()),
        )

    async def mark_deleted(self, max_chat_id: int, max_msg_id: int) -> None:
        await self._exec(
            "UPDATE max_messages SET deleted = 1 WHERE max_chat_id = ? AND max_msg_id = ?",
            (max_chat_id, max_msg_id),
        )

    async def add_forwarded(
        self, max_chat_id: int, max_msg_id: int, ref: ForwardedRef
    ) -> None:
        await self._exec(
            "INSERT OR REPLACE INTO forwarded VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                max_chat_id,
                max_msg_id,
                ref.tg_chat_id,
                ref.tg_msg_id,
                int(ref.text_holder),
                int(ref.is_caption),
                ref.html,
                now(),
            ),
        )

    async def update_forwarded_html(self, tg_chat_id: int, tg_msg_id: int, html: str) -> None:
        await self._exec(
            "UPDATE forwarded SET html = ? WHERE tg_chat_id = ? AND tg_msg_id = ?", (html, tg_chat_id, tg_msg_id)
        )

    async def get_forwarded(self, max_chat_id: int, max_msg_id: int) -> list[ForwardedRef]:
        rows = await self._all(
            "SELECT * FROM forwarded WHERE max_chat_id = ? AND max_msg_id = ?", (max_chat_id, max_msg_id)
        )
        return [
            ForwardedRef(
                r["tg_chat_id"], r["tg_msg_id"], bool(r["text_holder"]), bool(r["is_caption"]), r["html"]
            )
            for r in rows
        ]

    async def forwarded_chats(self, max_chat_id: int, max_msg_id: int) -> set[int]:
        return {ref.tg_chat_id for ref in await self.get_forwarded(max_chat_id, max_msg_id)}

    async def stats(self, since: int) -> dict[int, dict[str, int]]:
        """По каждому чату MAX: решения по категориям и сколько сообщений реально переслано."""
        out: dict[int, dict[str, int]] = {}
        rows = await self._all(
            "SELECT max_chat_id, category, decided_by, COUNT(*) AS n FROM max_messages "
            "WHERE created_at >= ? GROUP BY max_chat_id, category, decided_by",
            (since,),
        )
        for r in rows:
            bucket = out.setdefault(r["max_chat_id"], {})
            key = "pending" if r["decided_by"] == "pending" else r["category"]
            bucket[key] = bucket.get(key, 0) + r["n"]
            bucket["total"] = bucket.get("total", 0) + r["n"]
        rows = await self._all(
            "SELECT f.max_chat_id, COUNT(DISTINCT f.max_msg_id) AS n FROM forwarded f "
            "JOIN max_messages m ON m.max_chat_id = f.max_chat_id AND m.max_msg_id = f.max_msg_id "
            "WHERE m.created_at >= ? GROUP BY f.max_chat_id",
            (since,),
        )
        for r in rows:
            out.setdefault(r["max_chat_id"], {})["forwarded"] = r["n"]
        return out

    async def cleanup(self, older_than_days: int = 60) -> None:
        cutoff = now() - older_than_days * 86400
        await self.db.execute("DELETE FROM forwarded WHERE created_at < ?", (cutoff,))
        await self.db.execute(
            "DELETE FROM max_messages WHERE created_at < ? AND NOT EXISTS (SELECT 1 FROM moderation m "
            "WHERE m.max_chat_id = max_messages.max_chat_id AND m.max_msg_id = max_messages.max_msg_id)",
            (cutoff,),
        )
        await self.db.commit()

    # --- moderation -----------------------------------------------------------------------------

    async def add_moderation(
        self, max_chat_id: int, max_msg_id: int, payload: dict, kind: str = MODERATION_DISPUTED
    ) -> int:
        await self._exec(
            "INSERT INTO moderation (max_chat_id, max_msg_id, payload, kind, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(max_chat_id, max_msg_id) DO UPDATE SET payload = excluded.payload, kind = excluded.kind",
            (max_chat_id, max_msg_id, json.dumps(payload, ensure_ascii=False), kind, now()),
        )
        row = await self._one(
            "SELECT id FROM moderation WHERE max_chat_id = ? AND max_msg_id = ?", (max_chat_id, max_msg_id)
        )
        assert row is not None
        return int(row["id"])

    async def update_moderation_payload(self, mod_id: int, payload: dict) -> None:
        await self._exec(
            "UPDATE moderation SET payload = ? WHERE id = ?", (json.dumps(payload, ensure_ascii=False), mod_id)
        )

    async def set_moderation_msg(self, mod_id: int, owner_msg_id: int) -> None:
        await self._exec("UPDATE moderation SET owner_msg_id = ? WHERE id = ?", (owner_msg_id, mod_id))

    async def get_moderation(self, mod_id: int) -> Moderation | None:
        row = await self._one("SELECT * FROM moderation WHERE id = ?", (mod_id,))
        return _moderation(row) if row else None

    async def find_moderation(self, max_chat_id: int, max_msg_id: int) -> Moderation | None:
        row = await self._one(
            "SELECT * FROM moderation WHERE max_chat_id = ? AND max_msg_id = ?", (max_chat_id, max_msg_id)
        )
        return _moderation(row) if row else None

    async def list_moderation(self) -> list[Moderation]:
        return [_moderation(r) for r in await self._all("SELECT * FROM moderation ORDER BY created_at")]

    async def remove_moderation(self, mod_id: int) -> None:
        await self._exec("DELETE FROM moderation WHERE id = ?", (mod_id,))


def _moderation(row: aiosqlite.Row) -> Moderation:
    return Moderation(
        id=row["id"],
        max_chat_id=row["max_chat_id"],
        max_msg_id=row["max_msg_id"],
        owner_msg_id=row["owner_msg_id"],
        payload=json.loads(row["payload"]),
        kind=row["kind"],
    )


def _group(row: aiosqlite.Row) -> MaxGroup:
    return MaxGroup(
        max_chat_id=row["max_chat_id"],
        title=row["title"],
        forward_announcements=bool(row["forward_announcements"]),
        forward_dialogs=bool(row["forward_dialogs"]),
        enabled_at=row["enabled_at"],
        full_moderation=bool(row["full_moderation"]),
        silent_dialogs=bool(row["silent_dialogs"]),
    )
