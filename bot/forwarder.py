"""Ядро: решение по сообщению MAX, пересылка в Telegram, правки, удаления, модерация спорных."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections import defaultdict
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    ReplyParameters,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .ai import AiError, classify_with_ai
from .classifier import (
    ANNOUNCEMENT,
    CATEGORY_NAMES,
    DIALOG,
    DISPUTED,
    ClassifyInput,
    Verdict,
    classify,
    learnable_words,
    stop_reason,
)
from .config import Config
from .db import MODERATION_DISPUTED, MODERATION_FULL, Database, ForwardedRef, MaxGroup
from .formatting import (
    CAPTION_LIMIT,
    DELETED_MARK,
    TEXT_LIMIT,
    esc,
    fits,
    render,
    split_text,
)
from .models import ADMIN, MEDIA_NAMES, OWNER, IncomingMessage, Media
from .rules import RulesStore
from .settings import Settings
from .tg_send import not_modified, tg_call

if TYPE_CHECKING:
    from .max_client import MaxService

logger = logging.getLogger(__name__)

MEDIA = "media"  # есть вложение включённого типа — пересылается всегда
SKIP = "skip"  # нечего пересылать (только вложения выключенных типов)
ALL = "all"  # разделение не используется — пересылается всё
STOPPED = "stopped"  # стоп-слово или стоп-автор
FULL_MODERATION = "full"  # группа в режиме «всё на модерацию», решения ещё нет
APPROVED = "approved"  # владелец нажал «Переслать»
REJECTED = "rejected"  # владелец нажал «Не пересылать»
PENDING = "pending"
AI_ERROR_NOTIFY_EVERY = 3600
# Сообщения старше момента включения источника — это история, её не пересылаем.
HISTORY_GRACE = 60


@dataclasses.dataclass
class Downloaded:
    media: Media
    data: bytes | None
    error: str | None = None


class Forwarder:
    def __init__(self, bot: Bot, db: Database, rules: RulesStore, settings: Settings, cfg: Config) -> None:
        self.bot = bot
        self.db = db
        self.rules = rules
        self.settings = settings
        self.cfg = cfg
        self.max: MaxService | None = None
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_ai_error_notice = 0.0

    def attach(self, max_service: MaxService) -> None:
        self.max = max_service

    async def notify_owner(self, text: str, **kwargs) -> Message | None:  # noqa: ANN003
        try:
            return await tg_call(self.bot.send_message, chat_id=self.cfg.owner_id, text=text, **kwargs)
        except TelegramAPIError:
            logger.warning("Не удалось написать владельцу", exc_info=True)
            return None

    async def _migrate(self, old: int, new: int) -> None:
        await self.db.migrate_tg_chat(old, new)

    # --- события MAX ----------------------------------------------------------------------------

    async def on_new(self, msg: IncomingMessage) -> None:
        async with self._locks[msg.chat_id]:
            await self._handle_new(msg)

    async def on_edit(self, msg: IncomingMessage) -> None:
        async with self._locks[msg.chat_id]:
            await self._handle_edit(msg)

    async def on_delete(self, chat_id: int, msg_ids: list[int]) -> None:
        async with self._locks[chat_id]:
            for msg_id in msg_ids:
                await self._handle_delete(chat_id, msg_id)

    async def _handle_new(self, msg: IncomingMessage) -> None:
        group = await self.db.get_group(msg.chat_id)
        if group is None:
            return
        if msg.time < group.enabled_at - HISTORY_GRACE:
            return
        if await self.db.get_decision(msg.chat_id, msg.msg_id) is not None:
            return
        await self._decide_and_route(msg, group)

    async def _decide_and_route(self, msg: IncomingMessage, group: MaxGroup) -> None:
        category, decided_by, verdict = await self.decide(msg, group)
        logger.info(
            "MAX %s/%s «%s»: %s (%s%s)",
            msg.chat_id,
            msg.msg_id,
            group.title,
            category,
            decided_by,
            f", баллы {verdict.score}" if verdict else "",
        )
        if decided_by == PENDING:
            await self.db.set_decision(msg.chat_id, msg.msg_id, category, PENDING)
            kind = MODERATION_FULL if category == FULL_MODERATION else MODERATION_DISPUTED
            await self.submit_moderation(msg, verdict, kind)
            return
        await self.db.set_decision(msg.chat_id, msg.msg_id, category, decided_by)
        if await self.should_forward(category, group):
            await self.forward(msg, silent=self.is_silent(category, group))

    @staticmethod
    def is_silent(category: str, group: MaxGroup) -> bool:
        return category == DIALOG and group.silent_dialogs

    async def classification_active(self, group: MaxGroup) -> bool:
        """Классификатор нужен, только если разделение включено и пересылается ровно один из типов."""
        if not await self.settings.classification():
            return False
        ann = group.forward_announcements and await self.settings.forward_announcements()
        dia = group.forward_dialogs and await self.settings.forward_dialogs()
        return ann != dia

    async def decide(self, msg: IncomingMessage, group: MaxGroup) -> tuple[str, str, Verdict | None]:
        """(категория, кто решил, вердикт правил). Кто решил == PENDING — сообщение идёт на модерацию."""
        stop = stop_reason(msg.text, msg.author.id, self.rules.rules)
        if stop is not None:
            return STOPPED, stop, None
        if group.full_moderation:
            hint = await self.hint(msg, group)
            return FULL_MODERATION, PENDING, hint

        media_flags = await self.settings.media()
        if any(media_flags.get(m.kind, False) for m in msg.media):
            return MEDIA, "media", None
        if not msg.text.strip() and not msg.forwarded_from and not msg.extras:
            return SKIP, "media-off", None

        if not await self.classification_active(group):
            if await self.settings.classification() and not await self.should_forward(ANNOUNCEMENT, group):
                # Разделение включено, но оба типа у группы выключены — пересылать нечего.
                return SKIP, "types-off", None
            return ALL, "no-classification", None

        verdict = await self._rules_verdict(msg)
        if verdict.category != DISPUTED:
            return verdict.category, "rules", verdict

        ai = await self.settings.ai()
        if ai is not None:
            try:
                label = await classify_with_ai(ai, msg.text, msg.chat_title, self.rules.rules.examples)
                return label, "ai", verdict
            except AiError as e:
                logger.warning("ИИ не ответил: %s", e)
                if time.monotonic() - self._last_ai_error_notice > AI_ERROR_NOTIFY_EVERY:
                    self._last_ai_error_notice = time.monotonic()
                    await self.notify_owner(
                        f"⚠️ ИИ не смог разобрать сообщение ({esc(str(e))}). "
                        "Спорные сообщения пока идут на модерацию вам. Проверить: /ai"
                    )
        return DISPUTED, PENDING, verdict

    async def _rules_verdict(self, msg: IncomingMessage) -> Verdict:
        surnames: set[str] = set()
        if self.max is not None:
            try:
                surnames = await self.max.surnames(msg.chat_id)
            except Exception:  # noqa: BLE001
                logger.debug("surnames failed", exc_info=True)
        return classify(
            ClassifyInput(
                text=msg.text,
                sender_id=msg.author.id,
                sender_is_admin=msg.author.role in (OWNER, ADMIN),
                is_reply=msg.is_reply,
                participant_surnames=surnames,
            ),
            self.rules.rules,
        )

    async def hint(self, msg: IncomingMessage, group: MaxGroup) -> Verdict | None:
        """Подсказка правил для карточки полной модерации — только если разделение используется."""
        if not msg.text.strip() or not await self.classification_active(group):
            return None
        return await self._rules_verdict(msg)

    async def should_forward(self, category: str, group: MaxGroup) -> bool:
        if category in (MEDIA, ALL, APPROVED):
            return True
        if category == ANNOUNCEMENT:
            return group.forward_announcements and await self.settings.forward_announcements()
        if category == DIALOG:
            return group.forward_dialogs and await self.settings.forward_dialogs()
        return False

    async def _handle_edit(self, msg: IncomingMessage) -> None:
        group = await self.db.get_group(msg.chat_id)
        if group is None:
            return
        decision = await self.db.get_decision(msg.chat_id, msg.msg_id)
        if decision is None:
            await self._handle_new(msg)
            return
        category, decided_by, deleted = decision

        if decided_by == PENDING:
            mod = await self.db.find_moderation(msg.chat_id, msg.msg_id)
            if mod is not None:
                await self.db.update_moderation_payload(mod.id, msg.to_dict())
                await self._refresh_moderation_message(mod.id, msg, deleted, note="✏️ Сообщение изменили в MAX")
            return

        refs = await self.db.get_forwarded(msg.chat_id, msg.msg_id)
        if refs:
            for ref in refs:
                if ref.text_holder:
                    await self._edit_holder(ref, msg, deleted)
            return

        # Раньше не переслали. Пересмотреть решение, если его принимал не владелец.
        if decided_by == "owner":
            return
        await self._decide_and_route(msg, group)

    async def _edit_holder(self, ref: ForwardedRef, msg: IncomingMessage, deleted: bool) -> None:
        show_title = len(await self.db.routes_to(ref.tg_chat_id)) > 1
        html = self._render_fitting(msg, deleted, show_title, CAPTION_LIMIT if ref.is_caption else TEXT_LIMIT)[0]
        await self._set_html(ref, html)

    async def _set_html(self, ref: ForwardedRef, html: str) -> None:
        try:
            if ref.is_caption:
                await tg_call(
                    self.bot.edit_message_caption,
                    chat_id=ref.tg_chat_id,
                    message_id=ref.tg_msg_id,
                    caption=html,
                    on_migrate=self._migrate,
                )
            else:
                await tg_call(
                    self.bot.edit_message_text,
                    chat_id=ref.tg_chat_id,
                    message_id=ref.tg_msg_id,
                    text=html,
                    on_migrate=self._migrate,
                )
        except TelegramBadRequest as e:
            if not not_modified(e):
                logger.warning("Не удалось изменить сообщение %s/%s: %s", ref.tg_chat_id, ref.tg_msg_id, e)
                return
        except TelegramAPIError:
            logger.warning("Не удалось изменить сообщение %s/%s", ref.tg_chat_id, ref.tg_msg_id, exc_info=True)
            return
        await self.db.update_forwarded_html(ref.tg_chat_id, ref.tg_msg_id, html)

    async def _handle_delete(self, chat_id: int, msg_id: int) -> None:
        decision = await self.db.get_decision(chat_id, msg_id)
        if decision is None or decision[2]:
            return
        await self.db.mark_deleted(chat_id, msg_id)
        for ref in await self.db.get_forwarded(chat_id, msg_id):
            if not ref.text_holder or DELETED_MARK in ref.html:
                continue
            html = f"{ref.html}\n\n{DELETED_MARK}"
            limit = CAPTION_LIMIT if ref.is_caption else TEXT_LIMIT
            if fits(html, limit):
                await self._set_html(ref, html)
            else:
                # Пометка не влезает в подпись — отдельным ответом на пересланное сообщение.
                try:
                    await tg_call(
                        self.bot.send_message,
                        chat_id=ref.tg_chat_id,
                        text=DELETED_MARK,
                        reply_parameters=ReplyParameters(message_id=ref.tg_msg_id, allow_sending_without_reply=True),
                        on_migrate=self._migrate,
                    )
                except TelegramAPIError:
                    logger.warning("Не удалось отметить удаление", exc_info=True)
        mod = await self.db.find_moderation(chat_id, msg_id)
        if mod is not None:
            msg = IncomingMessage.from_dict(mod.payload)
            await self._refresh_moderation_message(mod.id, msg, True, note="🗑️ Сообщение удалили в MAX")

    # --- отправка в Telegram --------------------------------------------------------------------

    async def forward(
        self, msg: IncomingMessage, deleted: bool = False, only_chat: int | None = None, silent: bool = False
    ) -> int:
        """Пересылает во все чаты-получатели (или в один), где его ещё нет. Возвращает число чатов.

        silent — без звука уведомления в Telegram."""
        targets = await self.db.routes_from(msg.chat_id)
        if only_chat is not None:
            targets = [t for t in targets if t == only_chat]
        already = await self.db.forwarded_chats(msg.chat_id, msg.msg_id)
        targets = [t for t in targets if t not in already]
        if not targets:
            return 0
        files = await self._download_all(msg)
        sent = 0
        for tg_chat in targets:
            try:
                await self._send_to(tg_chat, msg, files, deleted, silent)
                sent += 1
            except TelegramAPIError:
                logger.exception("Не удалось переслать в чат %s", tg_chat)
        return sent

    async def _download_all(self, msg: IncomingMessage) -> list[Downloaded]:
        from .max_client import DownloadError, TooLarge

        flags = await self.settings.media()
        result: list[Downloaded] = []
        for m in msg.media:
            if not flags.get(m.kind, False):
                continue
            if self.max is None:
                result.append(Downloaded(m, None, "MAX не подключён"))
                continue
            try:
                result.append(Downloaded(m, await self.max.download(msg, m)))
            except TooLarge as e:
                result.append(Downloaded(m, None, f"слишком большой для Telegram ({e})"))
            except (DownloadError, Exception) as e:  # noqa: BLE001
                logger.warning("Не скачалось вложение %s: %s", m.kind, e)
                result.append(Downloaded(m, None, "не удалось скачать из MAX"))
        return result

    def _render_fitting(
        self, msg: IncomingMessage, deleted: bool, show_title: bool, limit: int
    ) -> tuple[str, list[str]]:
        """HTML, влезающий в limit, и остаток текста, который придётся отправить отдельно."""
        html = render(msg, self.cfg.tz, deleted, show_title)
        if fits(html, limit):
            return html, []
        overhead = len(render(dataclasses.replace(msg, text=""), self.cfg.tz, deleted, show_title)) + 50
        room = max(limit - overhead, 200)
        chunks = split_text(msg.text, room)
        if not chunks:
            return html, []
        first = dataclasses.replace(msg, text=chunks[0] + " …")
        html = render(first, self.cfg.tz, deleted, show_title)
        rest = split_text(" ".join(chunks[1:]), TEXT_LIMIT - 100) if len(chunks) > 1 else []
        return html, rest

    async def _send_to(
        self, tg_chat: int, msg: IncomingMessage, files: list[Downloaded], deleted: bool, silent: bool = False
    ) -> None:
        quiet = {"disable_notification": True} if silent else {}
        show_title = len(await self.db.routes_to(tg_chat)) > 1
        notes = [f"⚠️ {MEDIA_NAMES[d.media.kind]}: {d.error}" for d in files if d.error]
        view = dataclasses.replace(msg, extras=[*msg.extras, *notes])
        ok = [d for d in files if d.data is not None]

        async def remember(message: Message, holder: bool, caption: bool, html: str = "") -> None:
            await self.db.add_forwarded(
                msg.chat_id, msg.msg_id, ForwardedRef(message.chat.id, message.message_id, holder, caption, html)
            )

        caption_html, caption_rest = self._render_fitting(view, deleted, show_title, CAPTION_LIMIT)
        if len(ok) == 1 and not caption_rest:
            sent = await self._send_media(tg_chat, ok[0], caption=caption_html, silent=silent)
            await remember(sent, True, sent.caption is not None, caption_html)
            return

        html, rest = self._render_fitting(view, deleted, show_title, TEXT_LIMIT)
        head = await tg_call(self.bot.send_message, chat_id=tg_chat, text=html, on_migrate=self._migrate, **quiet)
        tg_chat = head.chat.id
        await remember(head, True, False, html)
        reply = ReplyParameters(message_id=head.message_id, allow_sending_without_reply=True)
        for chunk in rest:
            part = await tg_call(
                self.bot.send_message, chat_id=tg_chat, text=esc(chunk), reply_parameters=reply, **quiet
            )
            await remember(part, False, False)

        visual = [d for d in ok if d.media.kind in ("photo", "video")]
        other = [d for d in ok if d.media.kind not in ("photo", "video")]
        for i in range(0, len(visual), 10):
            batch = visual[i : i + 10]
            if len(batch) == 1:
                other.insert(0, batch[0])
                continue
            album = [
                InputMediaPhoto(media=_file(d)) if d.media.kind == "photo" else InputMediaVideo(media=_file(d))
                for d in batch
            ]
            for m in await tg_call(
                self.bot.send_media_group, chat_id=tg_chat, media=album, reply_parameters=reply, **quiet
            ):
                await remember(m, False, False)
        for d in other:
            await remember(await self._send_media(tg_chat, d, reply=reply, silent=silent), False, False)

    async def _send_media(
        self,
        chat_id: int,
        d: Downloaded,
        caption: str | None = None,
        reply: ReplyParameters | None = None,
        silent: bool = False,
    ) -> Message:
        m = d.media
        common: dict = {"caption": caption, "reply_parameters": reply, "on_migrate": self._migrate}
        if silent:
            common["disable_notification"] = True
        attempts = {
            "photo": [(self.bot.send_photo, "photo")],
            "video": [(self.bot.send_video, "video")],
            "audio": [(self.bot.send_voice, "voice"), (self.bot.send_audio, "audio")],
            "sticker": [(self.bot.send_photo, "photo")],
            "file": [],
        }[m.kind]
        extra: dict = {}
        if m.kind == "video":
            extra = {"width": m.width, "height": m.height, "duration": m.duration, "supports_streaming": True}
        for fn, field in attempts:
            try:
                return await tg_call(fn, chat_id=chat_id, **{field: _file(d)}, **common, **extra)
            except TelegramBadRequest as e:
                logger.info("%s не принят как %s (%s) — пробую иначе", m.kind, field, e)
        return await tg_call(self.bot.send_document, chat_id=chat_id, document=_file(d), **common)

    # --- модерация ------------------------------------------------------------------------------

    def _moderation_html(
        self,
        mod_id: int,
        msg: IncomingMessage,
        deleted: bool,
        verdict: Verdict | None,
        note: str | None,
        kind: str = MODERATION_DISPUTED,
    ) -> str:
        if kind == MODERATION_FULL:
            top = [f"🛡 <b>На модерации №{mod_id}</b> · «{esc(msg.chat_title)}»"]
        else:
            top = [f"❓ <b>Спорное сообщение №{mod_id}</b> · «{esc(msg.chat_title)}»"]
        if verdict is not None:
            r = self.rules.rules
            guess = f"похоже на: {CATEGORY_NAMES[verdict.category]}. " if kind == MODERATION_FULL else ""
            top.append(
                f"<i>{guess}Баллы: {verdict.score} (объявление от {r.announcement_threshold}, "
                f"диалог до {r.dialog_threshold}). {esc('; '.join(verdict.reasons)) or 'признаков нет'}</i>"
            )
        if msg.media:
            kinds = ", ".join(sorted({MEDIA_NAMES[m.kind] for m in msg.media}))
            top.append(f"<i>Вложения: {kinds}</i>")
        if note:
            top.append(f"<b>{note}</b>")
        prefix = "\n".join(top) + "\n\n"
        body, _ = self._render_fitting(msg, deleted, False, TEXT_LIMIT - len(prefix) - 20)
        return prefix + body

    def _moderation_keyboard(
        self, mod_id: int, msg: IncomingMessage, kind: str = MODERATION_DISPUTED
    ) -> InlineKeyboardMarkup:
        kb = InlineKeyboardBuilder()
        if kind == MODERATION_FULL:
            kb.button(text="✅ Переслать", callback_data=f"mod:{mod_id}:f")
            kb.button(text="❌ Не пересылать", callback_data=f"mod:{mod_id}:s")
        else:
            kb.button(text=CATEGORY_NAMES[ANNOUNCEMENT], callback_data=f"mod:{mod_id}:a")
            kb.button(text=CATEGORY_NAMES[DIALOG], callback_data=f"mod:{mod_id}:d")
            if msg.author.id is not None:
                kb.button(text="📌 Автор — всегда пересылать", callback_data=f"mod:{mod_id}:author")
        if msg.author.id is not None:
            kb.button(text="🚫 Автор — никогда", callback_data=f"mod:{mod_id}:ban")
        kb.adjust(2, 1, 1)
        return kb.as_markup()

    async def submit_moderation(
        self, msg: IncomingMessage, verdict: Verdict | None, kind: str = MODERATION_DISPUTED
    ) -> None:
        mod_id = await self.db.add_moderation(msg.chat_id, msg.msg_id, msg.to_dict(), kind)
        sent = await self.notify_owner(
            self._moderation_html(mod_id, msg, False, verdict, None, kind),
            reply_markup=self._moderation_keyboard(mod_id, msg, kind),
        )
        if sent is not None:
            await self.db.set_moderation_msg(mod_id, sent.message_id)

    async def resend_moderation(self) -> int:
        """Заново присылает владельцу все нерешённые сообщения с модерации."""
        pending = await self.db.list_moderation()
        for mod in pending:
            msg = IncomingMessage.from_dict(mod.payload)
            decision = await self.db.get_decision(mod.max_chat_id, mod.max_msg_id)
            deleted = bool(decision and decision[2])
            sent = await self.notify_owner(
                self._moderation_html(mod.id, msg, deleted, None, None, mod.kind),
                reply_markup=self._moderation_keyboard(mod.id, msg, mod.kind),
            )
            if sent is not None:
                await self.db.set_moderation_msg(mod.id, sent.message_id)
        return len(pending)

    async def _refresh_moderation_message(self, mod_id: int, msg: IncomingMessage, deleted: bool, note: str) -> None:
        mod = await self.db.get_moderation(mod_id)
        if mod is None or mod.owner_msg_id is None:
            return
        try:
            await self.bot.edit_message_text(
                chat_id=self.cfg.owner_id,
                message_id=mod.owner_msg_id,
                text=self._moderation_html(mod_id, msg, deleted, None, note, mod.kind),
                reply_markup=self._moderation_keyboard(mod_id, msg, mod.kind),
            )
        except TelegramAPIError:
            logger.debug("moderation refresh failed", exc_info=True)

    async def resolve_moderation(
        self, mod_id: int, action: str
    ) -> tuple[str, InlineKeyboardMarkup | None] | None:
        """Решение владельца по карточке модерации.

        action: "a" — объявление, "d" — диалог, "author" — объявление и автора в «пересылать всё»,
        "f" — переслать, "s" — не пересылать, "ban" — не пересылать и автора в стоп-авторы.
        Возвращает текст для замены карточки и кнопки «обучения»; None — если уже решено."""
        mod = await self.db.get_moderation(mod_id)
        if mod is None:
            return None
        msg = IncomingMessage.from_dict(mod.payload)
        label = {"a": ANNOUNCEMENT, "author": ANNOUNCEMENT, "d": DIALOG, "f": APPROVED}.get(action, REJECTED)
        # Для обучения «переслать» — это объявление, «не пересылать» — диалог.
        example_label = {APPROVED: ANNOUNCEMENT, REJECTED: DIALOG}.get(label, label)
        extra = ""
        async with self._locks[msg.chat_id]:
            decision = await self.db.get_decision(msg.chat_id, msg.msg_id)
            deleted = bool(decision and decision[2])
            if action == "author" and msg.author.id is not None:
                await self.rules.add_author(msg.author.id, msg.author.name)
                extra = f"\n📌 {esc(msg.author.name)} добавлен в «пересылать всё»"
            if action == "ban" and msg.author.id is not None:
                await self.rules.add_stop_author(msg.author.id, msg.author.name)
                extra = f"\n🚫 {esc(msg.author.name)} добавлен в стоп-авторы"
            await self.db.set_decision(msg.chat_id, msg.msg_id, label, "owner")
            await self.db.remove_moderation(mod_id)
            group = await self.db.get_group(msg.chat_id)
            learning = msg.text.strip() and action != "ban" and group is not None
            learning = bool(learning and await self.settings.classification())
            if learning:
                await self.rules.add_example(msg.text, example_label)
            outcome = "источник отключён — не пересылаю"
            if group is not None:
                if await self.should_forward(label, group):
                    n = await self.forward(msg, deleted, silent=self.is_silent(label, group))
                    outcome = f"переслано в чатов: {n}"
                elif label == REJECTED:
                    outcome = "не пересылаю"
                else:
                    outcome = "этот тип в группе выключен — не пересылаю"
        title = CATEGORY_NAMES.get(label) or {APPROVED: "✅ Переслать", REJECTED: "❌ Не пересылать"}[label]
        head = f"✅ <b>№{mod_id}: {title}</b> — {outcome}{extra}"
        learn = self.learn_keyboard(msg.text, example_label) if learning else None
        if learn is not None:
            head += "\n\n🧠 Добавить слова из сообщения в правила? Нажмите нужные."
        body, _ = self._render_fitting(msg, deleted, False, TEXT_LIMIT - len(head) - 20)
        return f"{head}\n\n{body}", learn

    def learn_keyboard(self, text: str, label: str) -> InlineKeyboardMarkup | None:
        words = learnable_words(text, self.rules.rules)
        if not words:
            return None
        kind = "k" if label == ANNOUNCEMENT else "d"
        kb = InlineKeyboardBuilder()
        for w in words:
            data = f"learn:{kind}:{w}"
            if len(data.encode()) <= 64:
                kb.button(text=f"+ {w}", callback_data=data)
        kb.button(text="Готово", callback_data="learn:done")
        kb.adjust(3)
        return kb.as_markup()


def _file(d: Downloaded) -> BufferedInputFile:
    assert d.data is not None
    m = d.media
    default = {"photo": "photo.jpg", "video": "video.mp4", "audio": "voice.ogg", "sticker": "sticker.webp"}
    return BufferedInputFile(d.data, filename=m.file_name or default.get(m.kind, "file"))
