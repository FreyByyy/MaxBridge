"""Общий контекст бота, который получают обработчики Telegram."""

from __future__ import annotations

from dataclasses import dataclass, field

from aiogram import Bot

from .config import Config
from .db import Database
from .forwarder import Forwarder
from .max_client import MaxService
from .rules import RulesStore
from .settings import Settings


@dataclass
class App:
    cfg: Config
    bot: Bot
    db: Database
    rules: RulesStore
    settings: Settings
    forwarder: Forwarder
    max: MaxService
    # Чего бот ждёт от пользователя в личке: user_id -> состояние ("phone", "trusted_id", ...).
    waiting: dict[int, str] = field(default_factory=dict)

    def is_owner(self, user_id: int | None) -> bool:
        return user_id == self.cfg.owner_id

    async def can_add_bot(self, user_id: int | None) -> bool:
        return user_id is not None and (self.is_owner(user_id) or await self.db.is_trusted(user_id))
