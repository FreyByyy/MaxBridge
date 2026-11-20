"""Типизированный доступ к настройкам владельца, которые хранятся в БД."""

from __future__ import annotations

from .ai import AiSettings
from .config import Config
from .db import Database
from .models import MEDIA_KINDS

DEFAULT_LEAVE_MESSAGE = "Этот бот работает только в чатах, куда его добавил владелец или доверенный пользователь. До свидания!"


class Settings:
    def __init__(self, db: Database, cfg: Config) -> None:
        self.db = db
        self.cfg = cfg

    async def init_defaults(self) -> None:
        """Разделение на объявления/диалоги по умолчанию выключено. Но установка, где источники уже
        настроены (до появления переключателя разделение работало всегда), сохраняет прежнее поведение."""
        if await self.db.get_setting("classification") is None:
            await self.db.set_setting("classification", bool(await self.db.list_groups()))

    async def classification(self) -> bool:
        return bool(await self.db.get_setting("classification", False))

    async def set_classification(self, value: bool) -> None:
        await self.db.set_setting("classification", value)

    async def forward_announcements(self) -> bool:
        return bool(await self.db.get_setting("forward_announcements", True))

    async def forward_dialogs(self) -> bool:
        return bool(await self.db.get_setting("forward_dialogs", False))

    async def set_global(self, key: str, value: bool) -> None:
        assert key in {"forward_announcements", "forward_dialogs"}
        await self.db.set_setting(key, value)

    async def media(self) -> dict[str, bool]:
        stored = await self.db.get_setting("media", {}) or {}
        return {kind: bool(stored.get(kind, True)) for kind in MEDIA_KINDS}

    async def set_media(self, kind: str, value: bool) -> None:
        current = await self.media()
        current[kind] = value
        await self.db.set_setting("media", current)

    async def leave_message(self) -> str:
        return str(await self.db.get_setting("leave_message", DEFAULT_LEAVE_MESSAGE))

    async def set_leave_message(self, text: str) -> None:
        await self.db.set_setting("leave_message", text)

    async def ai(self) -> AiSettings | None:
        stored = await self.db.get_setting("ai")
        if stored == "off":
            return None
        if isinstance(stored, dict) and stored.get("base_url") and stored.get("model"):
            return AiSettings(stored["base_url"], stored.get("api_key", ""), stored["model"])
        if self.cfg.ai_base_url and self.cfg.ai_model:
            return AiSettings(self.cfg.ai_base_url, self.cfg.ai_api_key or "", self.cfg.ai_model)
        return None

    async def set_ai(self, settings: AiSettings | None) -> None:
        if settings is None:
            await self.db.set_setting("ai", "off")
        else:
            await self.db.set_setting(
                "ai", {"base_url": settings.base_url, "api_key": settings.api_key, "model": settings.model}
            )

    async def max_phone(self) -> str | None:
        return await self.db.get_setting("max_phone") or self.cfg.max_phone

    async def set_max_phone(self, phone: str) -> None:
        await self.db.set_setting("max_phone", phone)
