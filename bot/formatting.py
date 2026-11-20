"""Оформление сообщения для Telegram (HTML) по шаблону msg.md."""

from __future__ import annotations

import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import ADMIN, OWNER, IncomingMessage

SEPARATOR = "───────────────"
DELETED_MARK = "<b>🗑️ Оригинальное сообщение удалено</b>"
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
QUOTE_LIMIT = 200

_TAG_RE = re.compile(r"<[^>]+>")


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def visible_len(html_text: str) -> int:
    """Длина текста так, как её считает Telegram (без тегов, с раскрытыми сущностями)."""
    return len(html.unescape(_TAG_RE.sub("", html_text)))


def header(msg: IncomingMessage, tz: ZoneInfo, show_chat_title: bool = False) -> str:
    a = msg.author
    line = f"👤 <b>{esc(a.name or 'Без имени')}</b>"
    if a.username:
        line += f" - <b>@{esc(a.username.lstrip('@'))}</b>"
    if a.role == ADMIN:
        line += " | 👔<b>Админ</b>"
    elif a.role == OWNER:
        line += " | ⭕<b>Владелец</b>"
    dt = datetime.fromtimestamp(msg.time, tz)
    lines = [line, f"🕗 <b>{dt:%H:%M}</b> - 📅 <b>{dt:%d.%m.%Y}</b>"]
    if show_chat_title:
        lines.insert(0, f"💬 <b>{esc(msg.chat_title)}</b>")
    return "\n".join(lines)


def body(msg: IncomingMessage) -> str:
    parts: list[str] = []
    if msg.forwarded_from:
        parts.append(f"🔁 <i>Переслано из «{esc(msg.forwarded_from)}»</i>")
    if msg.reply_to:
        quote = msg.reply_to.text.strip().replace("\n", " ")
        if len(quote) > QUOTE_LIMIT:
            quote = quote[: QUOTE_LIMIT - 1] + "…"
        parts.append(
            f"<blockquote>↩️ в ответ на <b>{esc(msg.reply_to.author)}</b>: «{esc(quote) or '…'}»</blockquote>"
        )
    if msg.text.strip():
        parts.append(esc(msg.text.strip()))
    for extra in msg.extras:
        parts.append(f"<i>{esc(extra)}</i>")
    return "\n".join(parts)


def render(
    msg: IncomingMessage, tz: ZoneInfo, deleted: bool = False, show_chat_title: bool = False
) -> str:
    """Полный текст: шапка, разделители, текст, пометка об удалении."""
    out = header(msg, tz, show_chat_title)
    content = body(msg)
    if content:
        out += f"\n\n{SEPARATOR}\n\n{content}\n\n{SEPARATOR}"
    if deleted:
        out += f"\n\n{DELETED_MARK}"
    return out


def fits(html_text: str, limit: int) -> bool:
    return visible_len(html_text) <= limit


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Режет длинный обычный текст (не HTML) по строкам/словам на куски не длиннее limit."""
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks
