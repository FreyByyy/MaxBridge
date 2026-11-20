"""Нажатия кнопок меню и модерации."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from ..ai import AiError, classify_with_ai
from ..app import App
from ..models import MEDIA_KINDS
from . import menus
from .private import ask_trusted, remove_trusted

logger = logging.getLogger(__name__)
router = Router(name="callbacks")

GROUP_TOGGLES = {
    "a": "forward_announcements",
    "d": "forward_dialogs",
    "m": "full_moderation",
    "q": "silent_dialogs",
}


async def show(call: CallbackQuery, screen: menus.Screen) -> None:
    text, kb = screen
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            await call.message.answer(text, reply_markup=kb)
    try:
        await call.answer()
    except TelegramAPIError:
        pass  # на кнопку уже ответили


def _is_owner(call: CallbackQuery, app: App) -> bool:
    return app.is_owner(call.from_user.id)


# --- навигация владельца ------------------------------------------------------------------------


@router.callback_query(F.data.startswith("m:"))
async def navigate(call: CallbackQuery, app: App) -> None:
    parts = call.data.split(":")
    screen = parts[1]
    if screen == "chats":
        if not await app.can_add_bot(call.from_user.id):
            return await call.answer("Нет доступа", show_alert=True)
        return await show(call, await menus.chats_menu(app, call.from_user.id))
    if not _is_owner(call, app):
        return await call.answer("Нет доступа", show_alert=True)
    if screen == "main":
        await show(call, await menus.main_menu(app))
    elif screen == "src":
        await show(call, await menus.sources_menu(app, int(parts[2])))
    elif screen == "tu":
        await show(call, await menus.trusted_menu(app))
    elif screen == "cls":
        await show(call, await menus.classification_menu(app))
    elif screen == "authors":
        await show(call, await menus.authors_menu(app))
    elif screen == "stopauthors":
        await show(call, await menus.stop_authors_menu(app))
    elif screen == "stats":
        await show(call, await menus.stats_menu(app))
    elif screen == "media":
        await show(call, await menus.media_menu(app))
    elif screen == "ai":
        await show(call, await menus.ai_menu(app))
    elif screen == "max":
        await show(call, await menus.max_menu(app))
    elif screen == "leave":
        await show(call, await menus.leave_menu(app))
    elif screen == "mod":
        await show(call, await menus.moderation_menu(app))
    else:
        await call.answer()


# --- источники MAX ------------------------------------------------------------------------------


@router.callback_query(F.data.startswith("g:"))
async def group_action(call: CallbackQuery, app: App) -> None:
    if not _is_owner(call, app):
        return await call.answer("Нет доступа", show_alert=True)
    _, raw_id, action = call.data.split(":")
    chat_id = int(raw_id)
    if action == "on":
        title = await menus._max_title(app, chat_id)
        await app.db.enable_group(chat_id, title)
    elif action == "off":
        await app.db.disable_group(chat_id)
    elif action in GROUP_TOGGLES:
        group = await app.db.get_group(chat_id)
        if group is not None:
            flag = GROUP_TOGGLES[action]
            await app.db.set_group_flag(chat_id, flag, not getattr(group, flag))
    await show(call, await menus.group_menu(app, chat_id))


# --- чаты Telegram и маршруты -------------------------------------------------------------------


async def _can_manage_chat(app: App, user_id: int, tg_chat_id: int) -> bool:
    if app.is_owner(user_id):
        return True
    info = await app.db.get_tg_chat(tg_chat_id)
    return info is not None and info[2] == user_id and await app.db.is_trusted(user_id)


@router.callback_query(F.data.startswith("c:"))
async def chat_action(call: CallbackQuery, app: App) -> None:
    _, raw_id, action = call.data.split(":")
    tg_chat_id = int(raw_id)
    if not await _can_manage_chat(app, call.from_user.id, tg_chat_id):
        return await call.answer("Нет доступа", show_alert=True)
    if action == "leave":
        try:
            await app.bot.leave_chat(tg_chat_id)
        except TelegramAPIError:
            logger.debug("leave failed", exc_info=True)
        await app.db.remove_tg_chat(tg_chat_id)
        return await show(call, await menus.chats_menu(app, call.from_user.id))
    await show(call, await menus.chat_menu(app, call.from_user.id, tg_chat_id))


@router.callback_query(F.data.startswith("r:"))
async def route_toggle(call: CallbackQuery, app: App) -> None:
    _, raw_tg, raw_max = call.data.split(":")
    tg_chat_id, max_chat_id = int(raw_tg), int(raw_max)
    uid = call.from_user.id
    if not await _can_manage_chat(app, uid, tg_chat_id):
        return await call.answer("Нет доступа", show_alert=True)
    allowed = {cid for cid, _ in await menus.available_sources(app, uid)}
    if max_chat_id not in allowed:
        return await call.answer("Этот чат MAX вам недоступен", show_alert=True)
    current = await app.db.routes_to(tg_chat_id)
    await app.db.set_route(tg_chat_id, max_chat_id, max_chat_id not in current)
    await show(call, await menus.chat_menu(app, uid, tg_chat_id))


# --- доверенные ---------------------------------------------------------------------------------


@router.callback_query(F.data.startswith("t:"))
async def trusted_action(call: CallbackQuery, app: App) -> None:
    if not _is_owner(call, app):
        return await call.answer("Нет доступа", show_alert=True)
    _, raw_id, action = call.data.split(":")
    uid = int(raw_id)
    if action == "add":
        await call.answer()
        await ask_trusted(call.message, app)
        return
    if action == "del":
        await remove_trusted(app, uid)
        return await show(call, await menus.trusted_menu(app))
    await show(call, await menus.trusted_user_menu(app, uid))


@router.callback_query(F.data.startswith("a:"))
async def access_toggle(call: CallbackQuery, app: App) -> None:
    if not _is_owner(call, app):
        return await call.answer("Нет доступа", show_alert=True)
    _, raw_uid, raw_max = call.data.split(":")
    uid, max_chat_id = int(raw_uid), int(raw_max)
    if max_chat_id in await app.db.allowed_groups(uid):
        await app.db.revoke_access(uid, max_chat_id)
    else:
        await app.db.grant_access(uid, max_chat_id)
    await show(call, await menus.trusted_user_menu(app, uid))


# --- классификация, вложения, ИИ ----------------------------------------------------------------


@router.callback_query(F.data.startswith("s:"))
async def global_toggle(call: CallbackQuery, app: App) -> None:
    if not _is_owner(call, app):
        return await call.answer("Нет доступа", show_alert=True)
    if call.data == "s:cls":
        await app.settings.set_classification(not await app.settings.classification())
    elif call.data == "s:ann":
        await app.settings.set_global("forward_announcements", not await app.settings.forward_announcements())
    elif call.data == "s:dia":
        await app.settings.set_global("forward_dialogs", not await app.settings.forward_dialogs())
    await show(call, await menus.classification_menu(app))


@router.callback_query(F.data.startswith("au:"))
async def author_remove(call: CallbackQuery, app: App) -> None:
    if not _is_owner(call, app):
        return await call.answer("Нет доступа", show_alert=True)
    await app.rules.remove_author(int(call.data.split(":")[1]))
    await show(call, await menus.authors_menu(app))


@router.callback_query(F.data.startswith("sa:"))
async def stop_author_remove(call: CallbackQuery, app: App) -> None:
    if not _is_owner(call, app):
        return await call.answer("Нет доступа", show_alert=True)
    await app.rules.remove_stop_author(int(call.data.split(":")[1]))
    await show(call, await menus.stop_authors_menu(app))


@router.callback_query(F.data.startswith("md:"))
async def media_toggle(call: CallbackQuery, app: App) -> None:
    if not _is_owner(call, app):
        return await call.answer("Нет доступа", show_alert=True)
    kind = call.data.split(":")[1]
    if kind in MEDIA_KINDS:
        flags = await app.settings.media()
        await app.settings.set_media(kind, not flags[kind])
    await show(call, await menus.media_menu(app))


@router.callback_query(F.data.startswith("ai:"))
async def ai_action(call: CallbackQuery, app: App) -> None:
    if not _is_owner(call, app):
        return await call.answer("Нет доступа", show_alert=True)
    if call.data == "ai:off":
        await app.settings.set_ai(None)
        return await show(call, await menus.ai_menu(app))
    ai = await app.settings.ai()
    if ai is None:
        return await call.answer("ИИ не подключён", show_alert=True)
    try:
        label = await classify_with_ai(ai, "Спасибо, понял!", "проверка", [])
        await call.answer(f"✅ Работает. «Спасибо, понял!» → {label}", show_alert=True)
    except AiError as e:
        await call.answer(f"❌ {e}"[:190], show_alert=True)


# --- модерация ----------------------------------------------------------------------------------


@router.callback_query(F.data == "mod:resend")
async def moderation_resend(call: CallbackQuery, app: App) -> None:
    if not _is_owner(call, app):
        return await call.answer("Нет доступа", show_alert=True)
    n = await app.forwarder.resend_moderation()
    await call.answer(f"Прислал: {n}")


@router.callback_query(F.data.startswith("mod:"))
async def moderation_decide(call: CallbackQuery, app: App) -> None:
    if not _is_owner(call, app):
        return await call.answer("Нет доступа", show_alert=True)
    _, raw_id, action = call.data.split(":")
    if action not in {"a", "d", "author", "f", "s", "ban"}:
        return await call.answer()
    await call.answer("Принято")
    result = await app.forwarder.resolve_moderation(int(raw_id), action)
    if result is None:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError:
            pass
        return
    text, learn = result
    try:
        await call.message.edit_text(text, reply_markup=learn)
    except TelegramAPIError:
        await call.message.answer(text, reply_markup=learn)


@router.callback_query(F.data.startswith("learn:"))
async def learn(call: CallbackQuery, app: App) -> None:
    if not _is_owner(call, app):
        return await call.answer("Нет доступа", show_alert=True)
    parts = call.data.split(":", 2)
    if parts[1] == "done":
        await call.message.edit_reply_markup(reply_markup=None)
        return await call.answer("Готово")
    kind = "keyword" if parts[1] == "k" else "dialog"
    word = parts[2]
    added = await app.rules.add_keyword(kind, word)
    where = "ключевые слова" if kind == "keyword" else "слова диалога"
    await call.answer(f"«{word}» → {where}" if added else "Уже есть")
    markup = call.message.reply_markup
    if markup is not None:
        rows = [[b for b in row if b.callback_data != call.data] for row in markup.inline_keyboard]
        rows = [r for r in rows if r]
        try:
            await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except TelegramAPIError:
            pass
