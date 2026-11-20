"""Настройки из переменных окружения (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on", "да"}


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    data_dir: Path
    tz: ZoneInfo
    max_phone: str | None
    max_show_online: bool
    ai_base_url: str | None
    ai_api_key: str | None
    ai_model: str | None
    log_level: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    @property
    def rules_path(self) -> Path:
        return self.data_dir / "rules.json"

    @property
    def max_dir(self) -> Path:
        return self.data_dir / "max"


def load_config() -> Config:
    token = _env("BOT_TOKEN")
    owner = _env("OWNER_ID")
    if not token:
        raise SystemExit("BOT_TOKEN не задан (см. .env.example)")
    if not owner or not owner.lstrip("-").isdigit():
        raise SystemExit("OWNER_ID не задан или не число (см. .env.example)")

    data_dir = Path(_env("DATA_DIR", "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        bot_token=token,
        owner_id=int(owner),
        data_dir=data_dir,
        tz=ZoneInfo(_env("TZ", "Europe/Moscow")),
        max_phone=_env("MAX_PHONE"),
        max_show_online=_env_bool("MAX_SHOW_ONLINE", False),
        ai_base_url=_env("AI_BASE_URL"),
        ai_api_key=_env("AI_API_KEY"),
        ai_model=_env("AI_MODEL"),
        log_level=_env("LOG_LEVEL", "INFO"),
    )
