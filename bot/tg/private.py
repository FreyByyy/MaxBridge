"""Личка с ботом: команды владельца и доверенных, ввод телефона/кода MAX, добавление доверенных."""

from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    KeyboardButton,
    KeyboardButtonRequestUsers,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from ..ai import AiError, AiSettings, classify_with_ai
from ..app import App
from ..formatting import esc
from . import menus

logger = logging.getLogger(__name__)
router = Router(name="private")
router.message.filter(F.chat.type == "private")

PHONE_RE = re.compile(r"^\+?\d[\d\s()-]{8,}$")


class OwnerOnly:
    async def __call__(self, message: Message, app: App) -> bool:
        return app.is_owner(message.from_user.id if message.from_user else None)


owner = OwnerOnly()


async def _delete(message: Message) -> None:
    try:
        await message.delete()
    except TelegramAPIError:
        pass


# --- общие команды ------------------------------------------------------------------------------


@router.message(CommandStart())
async def start(message: Message, app: App) -> None:
    uid = message.from_user.id
    if app.is_owner(uid):
        text, kb = await menus.main_menu(app)
        await message.answer(text, reply_markup=kb)
    elif await app.db.is_trusted(uid):
        await app.db.add_trusted(uid, _name(message))
        text, kb = await menus.chats_menu(app, uid)
        await message.answer(
            "Вы — доверенный пользователь. Добавьте бота в свою группу Telegram, "
            "затем выберите здесь, какие чаты MAX в неё пересылать.\n\n" + text,
            reply_markup=kb,
        )
    else:
        await message.answer("⛔ Нет доступа. Этим ботом пользуется только его владелец.")


@router.message(Command("menu", "settings"), owner)
async def menu(message: Message, app: App) -> None:
    text, kb = await menus.main_menu(app)
    await message.answer(text, reply_markup=kb)


@router.message(Command("chats"))
async def chats(message: Message, app: App) -> None:
    if not await app.can_add_bot(message.from_user.id):
        return
    text, kb = await menus.chats_menu(app, message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.message(Command("help"))
async def help_(message: Message, app: App) -> None:
    if app.is_owner(message.from_user.id):
        await message.answer(
            "<b>Команды владельца</b>\n"
            "/menu — меню настроек\n"
            "/status — состояние\n"
            "/login [телефон], /logout — аккаунт MAX\n"
            "/chats — чаты Telegram и что в них пересылать\n"
            "/kw, /kw_add, /kw_del, /skw_add, /skw_del, /dkw_add, /dkw_del — слова\n"
            "/stop_add, /stop_del — стоп-слова (никогда не пересылать)\n"
            "/stats — статистика за 7 дней\n"
            "/threshold 4 1 — пороги баллов\n"
            "/reload_rules — перечитать rules.json\n"
            "/ai адрес модель ключ, /ai_off — ИИ\n"
            "/leave_message текст — сообщение при выходе из чужого чата\n"
            "/trust id, /untrust id — доверенные\n"
            "/pending — прислать спорные сообщения заново"
        )
    elif await app.db.is_trusted(message.from_user.id):
        await message.answer("/chats — ваши чаты Telegram и что в них пересылать")


@router.message(Command("stats"), owner)
async def stats(message: Message, app: App) -> None:
    text, kb = await menus.stats_menu(app)
    await message.answer(text, reply_markup=kb)


@router.message(Command("status"), owner)
async def status(message: Message, app: App) -> None:
    text, _ = await menus.main_menu(app)
    ai = await app.settings.ai()
    await message.answer(text + f"\nИИ: {'подключён' if ai else 'нет'}")


# --- аккаунт MAX --------------------------------------------------------------------------------


@router.message(Command("login"), owner)
async def login(message: Message, command: CommandObject, app: App) -> None:
    phone = (command.args or "").strip() or await app.settings.max_phone()
    if not phone:
        app.waiting[message.from_user.id] = "phone"
        await message.answer("Пришлите номер телефона аккаунта MAX в формате +79991234567.")
        return
    await _start_login(message, app, phone)


async def _start_login(message: Message, app: App, phone: str) -> None:
    phone = re.sub(r"[^\d+]", "", phone)
    if not phone.startswith("+"):
        phone = "+" + phone
    old = await app.settings.max_phone()
    if old and old != phone and app.max.has_session:
        await app.max.logout()
    await app.settings.set_max_phone(phone)
    await message.answer(f"Подключаюсь к MAX как {esc(phone)}…")
    await app.max.start(phone)


@router.message(Command("logout"), owner)
async def logout(message: Message, app: App) -> None:
    await app.max.logout()
    await message.answer("Сессия MAX завершена и удалена. Войти снова: /login")


# --- правила ------------------------------------------------------------------------------------

KW_COMMANDS = {
    "kw_add": ("keyword", True),
    "kw_del": ("keyword", False),
    "skw_add": ("strong", True),
    "skw_del": ("strong", False),
    "dkw_add": ("dialog", True),
    "dkw_del": ("dialog", False),
    "stop_add": ("stop", True),
    "stop_del": ("stop", False),
}


@router.message(Command(*KW_COMMANDS), owner)
async def keywords_edit(message: Message, command: CommandObject, app: App) -> None:
    kind, add = KW_COMMANDS[command.command]
    words = [w.strip() for w in (command.args or "").split(",") if w.strip()]
    if not words:
        await message.answer(f"Пример: /{command.command} слово, другое слово, начало")
        return
    done = []
    for w in words:
        ok = await (app.rules.add_keyword(kind, w) if add else app.rules.remove_keyword(kind, w))
        if ok:
            done.append(w)
    verb = "Добавлено" if add else "Удалено"
    await message.answer(f"{verb}: {esc(', '.join(done)) or 'ничего (уже было / не найдено)'}")


@router.message(Command("kw"), owner)
async def keywords_show(message: Message, app: App) -> None:
    r = app.rules.rules

    def block(title: str, words: list[str]) -> str:
        return f"<b>{title}</b> ({len(words)}):\n{esc(', '.join(words)) or '—'}"

    await message.answer(
        "\n\n".join(
            [
                block("Сильные слова (+3)", r.strong_keywords),
                block("Ключевые слова (+1)", r.keywords),
                block("Слова диалога (−2, целым словом)", r.dialog_keywords),
                block("Стоп-слова (никогда не пересылать)", r.stop_keywords),
                "Слово совпадает с началом слова в тексте: «прибы» найдёт «прибыть».",
            ]
        )
    )


@router.message(Command("threshold"), owner)
async def threshold(message: Message, command: CommandObject, app: App) -> None:
    parts = (command.args or "").split()
    if len(parts) != 2 or not all(p.lstrip("-").isdigit() for p in parts):
        r = app.rules.rules
        await message.answer(
            f"Сейчас: объявление от {r.announcement_threshold}, диалог до {r.dialog_threshold}.\n"
            "Изменить: /threshold 4 1"
        )
        return
    a, d = int(parts[0]), int(parts[1])
    if d >= a:
        await message.answer("Порог диалога должен быть меньше порога объявления.")
        return
    app.rules.rules.announcement_threshold, app.rules.rules.dialog_threshold = a, d
    await app.rules.save()
    await message.answer(f"Готово: объявление от {a}, диалог до {d}. Между ними — спорные.")


@router.message(Command("reload_rules"), owner)
async def reload_rules(message: Message, app: App) -> None:
    app.rules.reload()
    await message.answer("rules.json перечитан.")


# --- ИИ -----------------------------------------------------------------------------------------


@router.message(Command("ai"), owner)
async def ai_set(message: Message, command: CommandObject, app: App) -> None:
    parts = (command.args or "").split()
    if len(parts) < 2:
        text, kb = await menus.ai_menu(app)
        await message.answer(text, reply_markup=kb)
        return
    await _delete(message)  # в сообщении ключ API
    key = parts[2] if len(parts) > 2 and parts[2] != "-" else ""
    settings = AiSettings(base_url=parts[0], model=parts[1], api_key=key)
    try:
        label = await classify_with_ai(settings, "Внимание! Завтра всем прибыть к 9:00.", "проверка", [])
    except AiError as e:
        await message.answer(f"❌ ИИ не ответил: {esc(str(e))}\nНастройки не сохранены.")
        return
    await app.settings.set_ai(settings)
    await message.answer(f"✅ ИИ подключён ({esc(settings.model)}). Пробное сообщение он отнёс к: {label}.")


@router.message(Command("ai_off"), owner)
async def ai_off(message: Message, app: App) -> None:
    await app.settings.set_ai(None)
    await message.answer("ИИ отключён. Спорные сообщения будут приходить вам на модерацию.")


# --- прочее -------------------------------------------------------------------------------------


@router.message(Command("leave_message"), owner)
async def leave_message(message: Message, command: CommandObject, app: App) -> None:
    text = (command.args or "").strip()
    if not text:
        screen, kb = await menus.leave_menu(app)
        await message.answer(screen, reply_markup=kb)
        return
    await app.settings.set_leave_message(text)
    await message.answer("Сохранено.")


@router.message(Command("pending"), owner)
async def pending(message: Message, app: App) -> None:
    n = await app.forwarder.resend_moderation()
    if not n:
        await message.answer("Спорных сообщений нет.")


@router.message(Command("trust"), owner)
async def trust(message: Message, command: CommandObject, app: App) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await ask_trusted(message, app)
        return
    await add_trusted(message, app, int(arg), "")


@router.message(Command("untrust"), owner)
async def untrust(message: Message, command: CommandObject, app: App) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Пример: /untrust 123456789")
        return
    await remove_trusted(app, int(arg))
    await message.answer("Готово.")


async def ask_trusted(message: Message, app: App) -> None:
    app.waiting[app.cfg.owner_id] = "trusted"
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="👤 Выбрать пользователя",
                    request_users=KeyboardButtonRequestUsers(
                        request_id=1, user_is_bot=False, max_quantity=1, request_name=True, request_username=True
                    ),
                )
            ],
            [KeyboardButton(text="Отмена")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "Выберите пользователя кнопкой ниже или пришлите его числовой Telegram ID.", reply_markup=kb
    )


async def add_trusted(message: Message, app: App, uid: int, name: str) -> None:
    app.waiting.pop(app.cfg.owner_id, None)
    if app.is_owner(uid):
        await message.answer("Это вы 🙂", reply_markup=ReplyKeyboardRemove())
        return
    await app.db.add_trusted(uid, name)
    await message.answer(
        f"✅ {esc(name or str(uid))} теперь доверенный. Откройте ему чаты MAX ниже. "
        "Пусть он напишет боту /start.",
        reply_markup=ReplyKeyboardRemove(),
    )
    text, kb = await menus.trusted_user_menu(app, uid)
    await message.answer(text, reply_markup=kb)


async def remove_trusted(app: App, uid: int) -> None:
    """Убирает доверенного; бот выходит из чатов, в которые его добавил этот пользователь."""
    from .membership import leave_with_message

    for tg_id, _, _ in await app.db.list_tg_chats(uid):
        await leave_with_message(app, tg_id)
    await app.db.remove_trusted(uid)


@router.message(F.users_shared, owner)
async def users_shared(message: Message, app: App) -> None:
    user = message.users_shared.users[0]
    name = " ".join(p for p in (user.first_name, user.last_name) if p) or (user.username or "")
    await add_trusted(message, app, user.user_id, name)


# --- свободный текст: код MAX, телефон, ID доверенного ------------------------------------------


@router.message(F.text, owner)
async def owner_text(message: Message, app: App) -> None:
    text = message.text.strip()
    if app.max.prompter.waiting is not None:
        kind = app.max.prompter.waiting
        app.max.prompter.answer(text)
        await _delete(message)
        await message.answer("🔑 Пароль принят, проверяю…" if kind == "password" else "Код принят, проверяю…")
        return

    state = app.waiting.get(message.from_user.id)
    if text.lower() == "отмена":
        app.waiting.pop(message.from_user.id, None)
        await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
        return
    if state == "phone":
        if not PHONE_RE.match(text):
            await message.answer("Не похоже на номер. Пример: +79991234567")
            return
        app.waiting.pop(message.from_user.id, None)
        await _start_login(message, app, text)
        return
    if state == "trusted":
        if text.isdigit():
            await add_trusted(message, app, int(text), "")
        else:
            await message.answer("Нужен числовой ID или кнопка «Выбрать пользователя».")
        return

    await message.answer("Не понял. Меню: /menu, команды: /help")


def _name(message: Message) -> str:
    u = message.from_user
    return " ".join(p for p in (u.first_name, u.last_name) if p) or (u.username or str(u.id))
