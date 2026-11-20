"""Подключение к MAX через PyMax: вход, события, перевод сообщений в IncomingMessage, скачивание вложений.

Наружу торчат только методы чтения. Отправка в MAX дополнительно запрещена в readonly_guard.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from collections.abc import Awaitable, Callable
from importlib import resources
from pathlib import Path
from typing import Any

import aiohttp
from pymax import Client, ExtraConfig

from . import readonly_guard
from .models import ADMIN, OWNER, Author, IncomingMessage, Media, Quote

logger = logging.getLogger(__name__)

readonly_guard.install()

TG_UPLOAD_LIMIT = 49 * 1024 * 1024
CHAT_TTL = 10 * 60
USER_TTL = 60 * 60
SURNAMES_TTL = 6 * 60 * 60
MAX_MEMBERS = 2000
SESSION_NAME = "session.db"
RECONNECT_NOTICE_AFTER = 5 * 60
OUTAGE_ALERT_AFTER = 10 * 60

MessageHandler = Callable[[IncomingMessage], Awaitable[None]]
DeleteHandler = Callable[[int, list[int]], Awaitable[None]]
Notify = Callable[[str], Awaitable[None]]


class Prompter:
    """Код из SMS и пароль 2FA запрашиваются у владельца в Telegram и приходят сюда."""

    def __init__(self, notify: Notify) -> None:
        self.notify = notify
        self.waiting: str | None = None  # "code" | "password"
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    async def _ask(self, kind: str, text: str) -> str:
        while not self._queue.empty():
            self._queue.get_nowait()
        self.waiting = kind
        await self.notify(text)
        try:
            return await self._queue.get()
        finally:
            self.waiting = None

    async def get_code(self, phone: str) -> str:
        return await self._ask("code", f"📲 MAX отправил код на {phone}. Пришлите его сюда одним сообщением.")

    async def get_password(self, hint: str | None = None) -> str:
        extra = f" (подсказка: {hint})" if hint else ""
        return await self._ask("password", f"🔐 У аккаунта MAX включён облачный пароль{extra}. Пришлите его сюда.")

    def answer(self, value: str) -> bool:
        if self.waiting is None:
            return False
        self._queue.put_nowait(value.strip())
        return True


def _ssl_context() -> ssl.SSLContext:
    # Серверы MAX подписаны сертификатом Минцифры, которого нет в стандартном хранилище.
    # Этот контекст используется ТОЛЬКО для скачивания вложений с серверов MAX.
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(str(resources.files("pymax._data") / "rootca_ssl_rsa2022.crt"))
    return ctx


def _str(value: Any) -> str:
    return getattr(value, "value", value) if value is not None else ""


def profile_name(user: Any) -> str:
    """Имя из профиля MAX, а не имя из контактов (тип CUSTOM)."""
    names = list(getattr(user, "names", None) or [])
    names.sort(key=lambda n: (str(_str(n.type)).upper() == "CUSTOM", str(_str(n.type)).upper() != "ONEME"))
    for n in names:
        full = " ".join(p for p in (n.first_name, n.last_name) if p) or (n.name or "")
        if full.strip():
            return full.strip()
    return f"id{getattr(user, 'id', '?')}"


def profile_last_name(user: Any) -> str | None:
    for n in getattr(user, "names", None) or []:
        if str(_str(n.type)).upper() == "CUSTOM":
            continue
        if n.last_name:
            return n.last_name
        if n.name and " " in n.name.strip():
            return n.name.strip().split()[-1]
    return None


def profile_username(user: Any) -> str | None:
    link = getattr(user, "link", None)
    if not isinstance(link, str) or not link:
        return None
    nick = link.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
    # Ссылки вида max.ru/u/<хэш> — не никнейм.
    if "/u/" in link or not nick or len(nick) > 32 or not nick[0].isalpha():
        return None
    return nick


def _ms_to_s(value: int) -> int:
    return value // 1000 if value > 10**11 else value


class MaxService:
    def __init__(
        self,
        work_dir: Path,
        notify_owner: Notify,
        on_message: MessageHandler,
        on_edit: MessageHandler,
        on_delete: DeleteHandler,
        show_online: bool = False,
    ) -> None:
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.notify_owner = notify_owner
        self.prompter = Prompter(notify_owner)
        self._on_message = on_message
        self._on_edit = on_edit
        self._on_delete = on_delete
        self.show_online = show_online
        self.client: Client | None = None
        self._task: asyncio.Task | None = None
        self.connected = False
        self._chats: dict[int, tuple[float, Any]] = {}
        self._users: dict[int, tuple[float, Any]] = {}
        self._surnames: dict[int, tuple[float, set[str]]] = {}
        self._http: aiohttp.ClientSession | None = None
        self._announced = False  # владельцу уже сказали «MAX подключён» после запуска / входа
        self._down_since: float | None = None
        self._outage_reported = False
        self._watchdog: asyncio.Task | None = None

    # --- жизненный цикл -------------------------------------------------------------------------

    @property
    def has_session(self) -> bool:
        return (self.work_dir / SESSION_NAME).exists()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, phone: str) -> None:
        await self.stop()
        self._announced = False
        self._down_since = None
        self._outage_reported = False
        client = Client(
            phone=phone,
            work_dir=str(self.work_dir),
            session_name=SESSION_NAME,
            sms_code_provider=self.prompter,
            password_provider=self.prompter,
            extra_config=ExtraConfig(password_max_attempts=3, reconnect=True, reconnect_delay=5.0),
        )
        self._register(client)
        self.client = client
        self._task = asyncio.create_task(self._run(client), name="max-client")

    async def _run(self, client: Client) -> None:
        try:
            await client.start()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — сообщить владельцу о любой причине остановки
            logger.exception("MAX-клиент остановился")
            self.connected = False
            await self.notify_owner(f"❌ Подключение к MAX остановлено: {type(e).__name__}: {e}\nВойти снова: /login")

    async def stop(self) -> None:
        if self.client is not None:
            try:
                await self.client.stop()
            except Exception:  # noqa: BLE001
                logger.debug("client.stop() failed", exc_info=True)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        self.client = None
        self._task = None
        self.connected = False

    async def logout(self) -> None:
        """Завершает сессию на сервере MAX и удаляет её файл."""
        if self.client is not None and self.connected:
            try:
                await self.client.logout()
            except Exception:  # noqa: BLE001
                logger.warning("logout в MAX не удался", exc_info=True)
        await self.stop()
        for f in self.work_dir.glob(SESSION_NAME + "*"):
            f.unlink(missing_ok=True)

    async def close(self) -> None:
        await self.stop()
        if self._http is not None:
            await self._http.close()

    def _register(self, client: Client) -> None:
        @client.on_start()
        async def _started(c: Client) -> None:
            self.connected = True
            if not self.show_online:
                try:
                    c.set_presence(online=False)
                except Exception:  # noqa: BLE001
                    logger.debug("set_presence failed", exc_info=True)
            me = c.me.contact if c.me else None
            name = profile_name(me) if me else "?"
            logger.info("MAX подключён как %s", name)
            await self._connected(name)

        @client.on_message()
        async def _message(message: Any, c: Client) -> None:
            await self._dispatch(self._on_message, message)

        @client.on_message_edit()
        async def _edit(message: Any, c: Client) -> None:
            await self._dispatch(self._on_edit, message)

        @client.on_message_delete()
        async def _delete(event: Any, c: Client) -> None:
            try:
                await self._on_delete(int(event.chat_id), [int(i) for i in event.message_ids])
            except Exception:  # noqa: BLE001
                logger.exception("Ошибка обработки удаления")

        @client.on_disconnect()
        async def _disconnected(*_: Any) -> None:
            logger.warning("Связь с MAX потеряна, переподключаюсь")
            self._disconnected()

    # --- уведомления о связи ----------------------------------------------------------------------
    # Сервер MAX время от времени сам рвёт соединение, и PyMax тут же входит заново. Владельцу пишем
    # только о первом подключении, о возвращении после долгого обрыва и о том, что MAX давно недоступен.

    async def _connected(self, name: str) -> None:
        self.connected = True
        down_since, self._down_since = self._down_since, None
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        if not self._announced:
            self._announced = True
            await self.notify_owner(f"✅ MAX подключён: {name}")
        elif self._outage_reported or (down_since and time.monotonic() - down_since >= RECONNECT_NOTICE_AFTER):
            minutes = max(1, round((time.monotonic() - down_since) / 60)) if down_since else 0
            await self.notify_owner(f"✅ MAX снова на связи (не было ~{minutes} мин)")
        self._outage_reported = False

    def _disconnected(self) -> None:
        self.connected = False
        if self._down_since is None:
            self._down_since = time.monotonic()
            self._watchdog = asyncio.create_task(self._watch_outage(), name="max-outage-watchdog")

    async def _watch_outage(self) -> None:
        await asyncio.sleep(OUTAGE_ALERT_AFTER)
        if not self.connected and self._down_since is not None:
            self._outage_reported = True
            minutes = round(OUTAGE_ALERT_AFTER / 60)
            await self.notify_owner(f"⚠️ MAX недоступен уже {minutes} мин. Бот продолжает переподключаться.")

    async def _dispatch(self, handler: MessageHandler, message: Any) -> None:
        try:
            msg = await self.convert(message)
            if msg is not None:
                await handler(msg)
        except Exception:  # noqa: BLE001 — одно плохое сообщение не должно ронять клиента
            logger.exception("Ошибка обработки сообщения MAX")

    # --- справочники ----------------------------------------------------------------------------

    def _require(self) -> Client:
        if self.client is None or not self.connected:
            raise RuntimeError("MAX не подключён")
        return self.client

    async def get_chat(self, chat_id: int) -> Any | None:
        cached = self._chats.get(chat_id)
        if cached and time.monotonic() - cached[0] < CHAT_TTL:
            return cached[1]
        try:
            chat = await self._require().get_chat(chat_id)
        except Exception:  # noqa: BLE001
            logger.warning("Не удалось получить чат %s", chat_id, exc_info=True)
            return cached[1] if cached else None
        self._chats[chat_id] = (time.monotonic(), chat)
        return chat

    async def get_user(self, user_id: int) -> Any | None:
        cached = self._users.get(user_id)
        if cached and time.monotonic() - cached[0] < USER_TTL:
            return cached[1]
        client = self._require()
        user = None
        try:
            user = await client.get_user(user_id)
        except Exception:  # noqa: BLE001
            logger.warning("Не удалось получить пользователя %s", user_id, exc_info=True)
        if user is None:
            user = client.get_cached_user(user_id)
        if user is not None:
            self._users[user_id] = (time.monotonic(), user)
        return user

    async def user_name(self, user_id: int | None) -> str:
        if user_id is None:
            return "Неизвестный"
        user = await self.get_user(user_id)
        return profile_name(user) if user else f"id{user_id}"

    async def chat_title(self, chat: Any) -> str:
        if chat is None:
            return "Чат MAX"
        if chat.title:
            return chat.title
        # Личный диалог: название — имя собеседника.
        me = self.client.me.contact.id if self.client and self.client.me else None
        others = [uid for uid in (chat.participants or {}) if uid != me]
        if others:
            return await self.user_name(others[0])
        return f"Чат {chat.id}"

    async def list_chats(self) -> list[tuple[int, str, str]]:
        """(id, название, тип) всех чатов аккаунта, которые отдаёт MAX."""
        client = self._require()
        chats: dict[int, Any] = {c.id: c for c in (client.chats or [])}
        try:
            for c in await client.fetch_chats():
                chats.setdefault(c.id, c)
        except Exception:  # noqa: BLE001
            logger.warning("fetch_chats не удался", exc_info=True)
        out = []
        for chat in chats.values():
            self._chats[chat.id] = (time.monotonic(), chat)
            out.append((chat.id, await self.chat_title(chat), str(_str(chat.type))))
        out.sort(key=lambda x: x[1].lower())
        return out

    async def surnames(self, chat_id: int) -> set[str]:
        cached = self._surnames.get(chat_id)
        if cached and time.monotonic() - cached[0] < SURNAMES_TTL:
            return cached[1]
        result: set[str] = set()
        try:
            client = self._require()
            marker = None
            seen = 0
            while seen < MAX_MEMBERS:
                members, marker = await client.get_chat_members(chat_id, marker, 100)
                for m in members:
                    last = profile_last_name(m.contact)
                    if last:
                        result.add(last.lower().replace("ё", "е"))
                seen += len(members)
                if not marker or not members:
                    break
        except Exception:  # noqa: BLE001
            logger.warning("Не удалось получить участников чата %s", chat_id, exc_info=True)
            if cached:
                return cached[1]
        self._surnames[chat_id] = (time.monotonic(), result)
        return result

    async def fetch_message(self, chat_id: int, msg_id: int) -> IncomingMessage | None:
        message = await self._require().get_message(chat_id=chat_id, message_id=msg_id)
        return await self.convert(message) if message is not None else None

    # --- перевод сообщения PyMax -> IncomingMessage ---------------------------------------------

    async def convert(self, message: Any) -> IncomingMessage | None:
        if message is None or message.chat_id is None:
            return None
        attaches = list(message.attaches or [])
        # Служебные сообщения («X вступил в группу» и т.п.) не пересылаем.
        if any(str(_str(getattr(a, "type", ""))) == "CONTROL" for a in attaches):
            return None

        chat = await self.get_chat(message.chat_id)
        title = await self.chat_title(chat)
        author = await self._author(message.sender, chat, title)

        text = message.text or ""
        reply_to = None
        forwarded_from = None
        link = message.link
        if link is not None:
            link_type = str(_str(getattr(link, "type", "")))
            if link_type == "FORWARD":
                src = link.message
                forwarded_from = getattr(link, "chat_name", None) or await self.user_name(src.sender)
                if not text:
                    text = src.text or ""
                if not attaches:
                    attaches = list(src.attaches or [])
            elif link_type == "REPLY":
                src = link.message
                quoted = src.text or ("[вложение]" if src.attaches else "")
                reply_to = Quote(author=await self.user_name(src.sender), text=quoted)

        media, extras = self._attachments(attaches)
        if not text.strip() and not media and not extras and forwarded_from is None:
            return None
        return IncomingMessage(
            chat_id=int(message.chat_id),
            msg_id=int(message.id),
            chat_title=title,
            author=author,
            time=_ms_to_s(int(message.time)),
            text=text,
            media=media,
            reply_to=reply_to,
            forwarded_from=forwarded_from,
            extras=extras,
        )

    async def _author(self, sender: int | None, chat: Any, chat_title: str) -> Author:
        if sender is None:
            # Пост канала — автором считается сам канал.
            return Author(id=None, name=chat_title, role=None)
        user = await self.get_user(sender)
        role = None
        if chat is not None:
            if chat.owner == sender:
                role = OWNER
            elif sender in (chat.admins or []) or sender in (chat.admin_participants or {}):
                role = ADMIN
        return Author(
            id=sender,
            name=profile_name(user) if user else f"id{sender}",
            username=profile_username(user) if user else None,
            role=role,
        )

    @staticmethod
    def _attachments(attaches: list[Any]) -> tuple[list[Media], list[str]]:
        media: list[Media] = []
        extras: list[str] = []
        for a in attaches:
            kind = str(_str(getattr(a, "type", "")))
            if kind == "PHOTO":
                media.append(Media("photo", url=a.base_url, width=a.width, height=a.height))
            elif kind == "VIDEO":
                media.append(
                    Media("video", attach_id=a.video_id, width=a.width, height=a.height, duration=a.duration)
                )
            elif kind == "FILE":
                media.append(Media("file", attach_id=a.file_id, file_name=a.name, size=a.size))
            elif kind == "AUDIO":
                media.append(Media("audio", url=a.url, attach_id=a.audio_id, duration=a.duration))
            elif kind == "STICKER":
                media.append(Media("sticker", url=a.url, width=a.width, height=a.height))
            elif kind == "POLL":
                extras.append(f"📊 Опрос: {getattr(a, 'title', '')}")
            elif kind == "CONTACT":
                extras.append("👤 Контакт")
            elif kind == "CALL":
                extras.append("📞 Звонок")
            elif kind in {"SHARE", "INLINE_KEYBOARD", ""}:
                continue
            else:
                extras.append(f"[{kind.lower()}]")
        return media, extras

    # --- скачивание вложений --------------------------------------------------------------------

    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=_ssl_context()),
                timeout=aiohttp.ClientTimeout(total=300),
            )
        return self._http

    async def media_url(self, msg: IncomingMessage, media: Media) -> str | None:
        if media.kind == "file" and media.attach_id is not None:
            req = await self._require().get_file_by_id(msg.chat_id, msg.msg_id, media.attach_id)
            return req.url if req else None
        if media.kind == "video" and media.attach_id is not None:
            req = await self._require().get_video_by_id(msg.chat_id, msg.msg_id, media.attach_id)
            return req.url if req else None
        return media.url

    async def download(self, msg: IncomingMessage, media: Media) -> bytes:
        """Скачивает вложение. TooLarge — если больше лимита Telegram для ботов."""
        if media.size and media.size > TG_UPLOAD_LIMIT:
            raise TooLarge(media.size)
        url = await self.media_url(msg, media)
        if not url:
            raise DownloadError("MAX не отдал ссылку на вложение")
        session = await self._session()
        async with session.get(url) as resp:
            if resp.status != 200:
                raise DownloadError(f"HTTP {resp.status}")
            if resp.content_length and resp.content_length > TG_UPLOAD_LIMIT:
                raise TooLarge(resp.content_length)
            data = bytearray()
            async for chunk in resp.content.iter_chunked(256 * 1024):
                data.extend(chunk)
                if len(data) > TG_UPLOAD_LIMIT:
                    raise TooLarge(len(data))
            return bytes(data)


class DownloadError(Exception):
    pass


class TooLarge(DownloadError):
    def __init__(self, size: int) -> None:
        super().__init__(f"{size / 1024 / 1024:.0f} МБ")
        self.size = size
