"""Добавление бота в чаты: разрешено только владельцу и доверенным, остальным — сообщение и выход."""

from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import ChatMemberUpdated, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..app import App
from ..formatting import esc

logger = logging.getLogger(__name__)
router = Router(name="membership")

IN = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
OUT = {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}

MIGRATION_WAIT = 15.0
MIGRATION_POLL = 0.5
REMOVE_DELAY = 60.0
_background: set[asyncio.Task] = set()


def user_label(user) -> str:  # noqa: ANN001
    name = " ".join(p for p in (user.first_name, user.last_name) if p) or str(user.id)
    return f"{name} (@{user.username})" if user.username else name


async def leave_with_message(app: App, chat_id: int) -> None:
    try:
        await app.bot.send_message(chat_id, esc(await app.settings.leave_message()))
    except TelegramAPIError:
        logger.debug("leave message failed", exc_info=True)
    try:
        await app.bot.leave_chat(chat_id)
    except TelegramAPIError:
        logger.warning("Не удалось выйти из чата %s", chat_id, exc_info=True)
    await app.db.remove_tg_chat(chat_id)


@router.my_chat_member(F.chat.type != "private")
async def on_bot_membership(update: ChatMemberUpdated, app: App) -> None:
    chat = update.chat
    old, new = update.old_chat_member.status, update.new_chat_member.status
    title = chat.title or str(chat.id)

    if new in OUT:
        if chat.type == "group":
            # Обычная группа могла переехать в супергруппу: настройки забирает on_migrate,
            # поэтому удаляем их не сразу.
            task = asyncio.create_task(_remove_later(app, chat.id))
            _background.add(task)
            task.add_done_callback(_background.discard)
        else:
            await app.db.remove_tg_chat(chat.id)
        logger.info("Бота убрали из чата %s «%s»", chat.id, title)
        return
    if not (new in IN and old in OUT):
        return

    adder = update.from_user
    if not await app.can_add_bot(adder.id):
        if chat.type == "supergroup" and await _became_supergroup(app, chat.id):
            # Telegram сообщает о переезде группы как о «добавлении» бота тем, кто поменял настройки.
            logger.info("«%s» стала супергруппой %s — настройки перенесены", title, chat.id)
            await app.forwarder.notify_owner(
                f"ℹ️ «{esc(title)}» стала супергруппой (её новый ID — <code>{chat.id}</code>). "
                "Настройки пересылки перенесены, бот остался в чате."
            )
            return
        logger.warning("Посторонний %s добавил бота в %s «%s» — выхожу", adder.id, chat.id, title)
        await leave_with_message(app, chat.id)
        await app.forwarder.notify_owner(
            f"🚫 {esc(user_label(adder))} (id <code>{adder.id}</code>) добавил бота в «{esc(title)}». "
            "Бот написал сообщение и вышел."
        )
        return

    await app.db.add_tg_chat(chat.id, title, adder.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="⚙️ Выбрать чаты MAX", callback_data=f"c:{chat.id}:show")
    text = f"✅ Бот добавлен в «{esc(title)}». Выберите, какие чаты MAX сюда пересылать."
    try:
        await app.bot.send_message(adder.id, text, reply_markup=kb.as_markup())
    except TelegramForbiddenError:
        await app.bot.send_message(
            chat.id, "Бот добавлен. Чтобы настроить пересылку, откройте бота в личных сообщениях и нажмите /start."
        )
    if not app.is_owner(adder.id):
        await app.forwarder.notify_owner(
            f"ℹ️ Доверенный {esc(user_label(adder))} добавил бота в «{esc(title)}»."
        )


async def _became_supergroup(app: App, chat_id: int) -> bool:
    """Ждёт служебное сообщение о переезде группы: оно приходит отдельно и может опоздать."""
    for _ in range(int(MIGRATION_WAIT / MIGRATION_POLL)):
        if await app.db.get_tg_chat(chat_id) is not None:
            return True
        await asyncio.sleep(MIGRATION_POLL)
    return await app.db.get_tg_chat(chat_id) is not None


async def _remove_later(app: App, chat_id: int) -> None:
    await asyncio.sleep(REMOVE_DELAY)
    # Если группа переехала, её записи уже под новым ID и тут удалять нечего.
    await app.db.remove_tg_chat(chat_id)


async def _migrate(app: App, old_id: int, new_id: int) -> None:
    if await app.db.get_tg_chat(old_id) is None:
        return
    await app.db.migrate_tg_chat(old_id, new_id)
    logger.info("Чат %s переехал в супергруппу %s — настройки перенесены", old_id, new_id)


# Telegram присылает два служебных сообщения: в старую группу и в новую супергруппу.
@router.message(F.migrate_to_chat_id)
async def on_migrate_to(message: Message, app: App) -> None:
    await _migrate(app, message.chat.id, message.migrate_to_chat_id)


@router.message(F.migrate_from_chat_id)
async def on_migrate_from(message: Message, app: App) -> None:
    await _migrate(app, message.migrate_from_chat_id, message.chat.id)
