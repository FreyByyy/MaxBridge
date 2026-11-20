"""Вызовы Telegram API с повтором при флуд-контроле и переездом группы в супергруппу."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")
OnMigrate = Callable[[int, int], Awaitable[None]]


async def tg_call(
    fn: Callable[..., Awaitable[T]],
    *,
    chat_id: int,
    on_migrate: OnMigrate | None = None,
    attempts: int = 5,
    **kwargs: Any,
) -> T:
    """fn(chat_id=..., **kwargs) с повторами. Бросает последнюю ошибку, если не вышло."""
    delay = 2.0
    for attempt in range(1, attempts + 1):
        try:
            return await fn(chat_id=chat_id, **kwargs)
        except TelegramRetryAfter as e:
            logger.warning("Флуд-контроль Telegram, жду %s с", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
        except TelegramMigrateToChat as e:
            logger.info("Чат %s стал супергруппой %s", chat_id, e.migrate_to_chat_id)
            if on_migrate is not None:
                await on_migrate(chat_id, e.migrate_to_chat_id)
            chat_id = e.migrate_to_chat_id
        except (TelegramNetworkError, TelegramServerError):
            if attempt == attempts:
                raise
            logger.warning("Сбой связи с Telegram, повтор через %.0f с", delay, exc_info=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
    return await fn(chat_id=chat_id, **kwargs)


def not_modified(error: TelegramBadRequest) -> bool:
    return "message is not modified" in str(error).lower()
