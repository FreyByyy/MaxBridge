"""Сообщение MAX в виде, не зависящем от библиотеки PyMax (удобно для форматирования и тестов)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

OWNER = "owner"
ADMIN = "admin"

MEDIA_KINDS = ("photo", "video", "audio", "file", "sticker")
MEDIA_NAMES = {
    "photo": "🖼 Фото",
    "video": "🎬 Видео",
    "audio": "🎤 Голосовые",
    "file": "📎 Файлы",
    "sticker": "🙂 Стикеры",
}


@dataclass
class Author:
    id: int | None
    name: str
    username: str | None = None
    role: str | None = None  # OWNER / ADMIN / None — обычный участник


@dataclass
class Media:
    kind: str  # один из MEDIA_KINDS
    # Для скачивания: прямой URL (фото, стикер, голосовое) или id для запроса URL у MAX (файл, видео).
    url: str | None = None
    attach_id: int | None = None
    file_name: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    duration: int | None = None


@dataclass
class Quote:
    author: str
    text: str


@dataclass
class IncomingMessage:
    chat_id: int
    msg_id: int
    chat_title: str
    author: Author
    time: int  # unix, секунды
    text: str = ""
    media: list[Media] = field(default_factory=list)
    reply_to: Quote | None = None
    forwarded_from: str | None = None
    # Прочее, что переслать нельзя (опрос, контакт, звонок…) — короткое описание.
    extras: list[str] = field(default_factory=list)

    @property
    def is_reply(self) -> bool:
        return self.reply_to is not None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> IncomingMessage:
        data = dict(data)
        data["author"] = Author(**data["author"])
        data["media"] = [Media(**m) for m in data.get("media", [])]
        if data.get("reply_to"):
            data["reply_to"] = Quote(**data["reply_to"])
        return cls(**data)
