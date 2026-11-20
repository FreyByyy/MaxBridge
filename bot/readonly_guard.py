"""Защита «только чтение» для MAX.

Перехватывает каждый кадр, который PyMax отправляет на сервер MAX, и не пропускает запросы,
которые что-то меняют: отправку, правку и удаление сообщений, реакции, отметки «прочитано»,
вступление и выход из чатов, загрузку файлов и т.п. Даже если в коде бота появится ошибка
или кто-то вызовет client.send_message(), запрос до MAX не дойдёт.
"""

from __future__ import annotations

import logging

from pymax.connection.connection import ConnectionManager
from pymax.protocol import Command, Opcode

logger = logging.getLogger(__name__)

BLOCKED_PREFIXES = (
    "MSG_SEND",  # в т.ч. MSG_SEND_CALLBACK
    "MSG_TYPING",
    "MSG_DELETE",  # в т.ч. MSG_DELETE_RANGE, MSG_DELETE_USER_COMMENTS
    "MSG_EDIT",
    "MSG_REACTION",
    "MSG_CANCEL_REACTION",
    "CHAT_MARK",  # отметка «прочитано»
    "CHAT_DELETE",
    "CHAT_CLEAR",
    "CHAT_UPDATE",
    "CHAT_JOIN",
    "CHAT_LEAVE",
    "CHAT_CREATE",
    "CHAT_MEMBERS_UPDATE",
    "CHAT_HIDE",
    "CHAT_PIN",
    "CHAT_COMPLAIN",
    "CHAT_REACTIONS_SETTINGS_SET",
    "COMPLAIN",
    "CONTACT_ADD",
    "CONTACT_UPDATE",
    "REMOVE_CONTACT_PHOTO",
    "LOCATION_SEND",
    "DRAFT_",
    "PROFILE_DELETE",
    "SESSIONS_CLOSE",
    "VIDEO_CHAT_",
    "AUTH_SET_2FA",
    "AUTH_QR_APPROVE",
    "STICKER_CREATE",
    "STORIES_REACT",
    "STORIES_MARK",
    "STORIES_SEND",
    "STORIES_EDIT",
    "STORIES_DELETE",
    "ASSETS_ADD",
    "ASSETS_REMOVE",
    "ASSETS_MOVE",
    "ASSETS_LIST_MODIFY",
    "FOLDERS_UPDATE",
    "FOLDERS_REORDER",
    "FOLDERS_DELETE",
    "SEND_VOTE",
)
BLOCKED_SUBSTRINGS = ("UPLOAD", "COMMENT_SEND", "SEND_COMMENT")


class ReadOnlyViolation(PermissionError):
    """Попытка что-то изменить в MAX — запрещено."""


def _name(opcode: int) -> str:
    try:
        return Opcode(opcode).name
    except ValueError:
        return f"UNKNOWN_{opcode}"


def is_blocked(opcode: int) -> bool:
    name = _name(opcode)
    return name.startswith(BLOCKED_PREFIXES) or any(s in name for s in BLOCKED_SUBSTRINGS)


def _check(frame) -> None:  # noqa: ANN001 — OutboundFrame
    if frame.cmd == Command.REQUEST and is_blocked(frame.opcode):
        logger.error("Заблокирован запрос к MAX: %s (бот работает только на чтение)", _name(frame.opcode))
        raise ReadOnlyViolation(f"MAX read-only: {_name(frame.opcode)} запрещён")


_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    original_send = ConnectionManager.send
    original_request = ConnectionManager.request

    async def send(self, frame, *args, **kwargs):  # noqa: ANN001, ANN202
        _check(frame)
        return await original_send(self, frame, *args, **kwargs)

    async def request(self, frame, *args, **kwargs):  # noqa: ANN001, ANN202
        _check(frame)
        return await original_request(self, frame, *args, **kwargs)

    ConnectionManager.send = send  # type: ignore[method-assign]
    ConnectionManager.request = request  # type: ignore[method-assign]
    _installed = True
