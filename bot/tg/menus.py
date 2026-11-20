"""Экраны меню с кнопками. Каждый экран возвращает (текст, клавиатура)."""

from __future__ import annotations

import time

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..app import App
from ..formatting import esc
from ..models import MEDIA_KINDS, MEDIA_NAMES

PAGE = 8
Screen = tuple[str, InlineKeyboardMarkup]


def onoff(value: bool) -> str:
    return "✅" if value else "❌"


def _back(kb: InlineKeyboardBuilder, to: str = "m:main") -> None:
    kb.button(text="⬅️ Назад", callback_data=to)


async def main_menu(app: App) -> Screen:
    pending = len(await app.db.list_moderation())
    max_state = "🟢 подключён" if app.max.connected else "🔴 не подключён"
    text = (
        "<b>Настройки бота</b>\n\n"
        f"MAX: {max_state}\n"
        f"Источников MAX: {len(await app.db.list_groups())}\n"
        f"Чатов Telegram: {len(await app.db.list_tg_chats())}\n"
        f"Ждут модерации: {pending}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="📥 Источники MAX", callback_data="m:src:0")
    kb.button(text="💬 Чаты Telegram", callback_data="m:chats")
    kb.button(text="👥 Доверенные", callback_data="m:tu")
    kb.button(text="🏷 Классификация", callback_data="m:cls")
    kb.button(text="📎 Вложения", callback_data="m:media")
    kb.button(text="🤖 ИИ", callback_data="m:ai")
    kb.button(text="🔑 Аккаунт MAX", callback_data="m:max")
    kb.button(text="🚪 Текст при выходе", callback_data="m:leave")
    kb.button(text=f"❓ Модерация ({pending})", callback_data="m:mod")
    kb.button(text="📊 Статистика", callback_data="m:stats")
    kb.adjust(2)
    return text, kb.as_markup()


async def sources_menu(app: App, page: int) -> Screen:
    enabled = {g.max_chat_id: g for g in await app.db.list_groups()}
    if app.max.connected:
        chats = [(cid, title) for cid, title, _ in await app.max.list_chats()]
        note = "Нажмите на чат, чтобы настроить. ✅ — бот читает этот чат."
    else:
        chats = [(g.max_chat_id, g.title) for g in enabled.values()]
        note = "MAX не подключён — показаны только уже выбранные источники."
    pages = max(1, (len(chats) + PAGE - 1) // PAGE)
    page = min(max(page, 0), pages - 1)
    kb = InlineKeyboardBuilder()
    for cid, title in chats[page * PAGE : (page + 1) * PAGE]:
        mark = "✅" if cid in enabled else "▫️"
        kb.button(text=f"{mark} {title[:40]}", callback_data=f"g:{cid}:show")
    kb.adjust(1)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️", callback_data=f"m:src:{page - 1}")
    if page < pages - 1:
        nav.button(text="▶️", callback_data=f"m:src:{page + 1}")
    _back(nav)
    kb.attach(nav)
    text = f"<b>📥 Источники MAX</b> (стр. {page + 1}/{pages})\n\n{note}"
    return text, kb.as_markup()


async def group_menu(app: App, max_chat_id: int) -> Screen:
    group = await app.db.get_group(max_chat_id)
    kb = InlineKeyboardBuilder()
    if group is None:
        title = await _max_title(app, max_chat_id)
        text = f"<b>{esc(title)}</b>\n\nНе источник: бот не читает этот чат."
        kb.button(text="✅ Сделать источником", callback_data=f"g:{max_chat_id}:on")
    else:
        routes = [t for t, *_ in await app.db.list_tg_chats() if max_chat_id in await app.db.routes_to(t)]
        lines = [f"<b>{esc(group.title)}</b>\n", "Источник: ✅"]
        layout = []
        if await app.settings.classification():
            a, d = group.forward_announcements, group.forward_dialogs
            lines += [f"📢 Объявления: {onoff(a)}", f"💬 Диалоги: {onoff(d)}"]
            if a and d:
                lines.append("<i>Включены оба типа — пересылается всё, без классификатора.</i>")
            elif d:
                lines.append(f"🔕 Диалоги без звука: {onoff(group.silent_dialogs)}")
            kb.button(text=f"📢 Объявления {onoff(a)}", callback_data=f"g:{max_chat_id}:a")
            kb.button(text=f"💬 Диалоги {onoff(d)}", callback_data=f"g:{max_chat_id}:d")
            layout.append(2)
            if d and not a:
                kb.button(text=f"🔕 Диалоги без звука {onoff(group.silent_dialogs)}", callback_data=f"g:{max_chat_id}:q")
                layout.append(1)
        else:
            lines.append("Разделение на объявления/диалоги выключено — пересылается всё.")
        lines.append(f"🛡 Всё на модерацию: {onoff(group.full_moderation)}")
        lines.append(f"Пересылается в чатов Telegram: {len(routes)}")
        text = "\n".join(lines)
        kb.button(text=f"🛡 Всё на модерацию {onoff(group.full_moderation)}", callback_data=f"g:{max_chat_id}:m")
        kb.button(text="🚫 Перестать читать", callback_data=f"g:{max_chat_id}:off")
        layout += [1, 1]
    _back(kb, "m:src:0")
    kb.adjust(*(layout if group is not None else [2]), 1)
    return text, kb.as_markup()


async def _max_title(app: App, max_chat_id: int) -> str:
    group = await app.db.get_group(max_chat_id)
    if group:
        return group.title
    if app.max.connected:
        chat = await app.max.get_chat(max_chat_id)
        if chat is not None:
            return await app.max.chat_title(chat)
    return f"Чат {max_chat_id}"


async def chats_menu(app: App, user_id: int) -> Screen:
    owner = app.is_owner(user_id)
    chats = await app.db.list_tg_chats(None if owner else user_id)
    kb = InlineKeyboardBuilder()
    for tg_id, title, _ in chats:
        kb.button(text=title[:45] or str(tg_id), callback_data=f"c:{tg_id}:show")
    if owner:
        _back(kb)
    kb.adjust(1)
    if chats:
        text = "<b>💬 Чаты Telegram</b>\n\nВыберите чат, чтобы указать, какие чаты MAX в него пересылать."
    else:
        text = "<b>💬 Чаты Telegram</b>\n\nБот пока ни в один чат не добавлен. Добавьте его в группу Telegram."
    return text, kb.as_markup()


async def available_sources(app: App, user_id: int) -> list[tuple[int, str]]:
    groups = await app.db.list_groups()
    if app.is_owner(user_id):
        return [(g.max_chat_id, g.title) for g in groups]
    allowed = await app.db.allowed_groups(user_id)
    return [(g.max_chat_id, g.title) for g in groups if g.max_chat_id in allowed]


async def chat_menu(app: App, user_id: int, tg_chat_id: int) -> Screen:
    info = await app.db.get_tg_chat(tg_chat_id)
    kb = InlineKeyboardBuilder()
    if info is None:
        return "Чат не найден — возможно, бота из него удалили.", _only_back(user_id, app)
    _, title, added_by = info
    routes = await app.db.routes_to(tg_chat_id)
    sources = await available_sources(app, user_id)
    for cid, name in sources:
        kb.button(text=f"{onoff(cid in routes)} {name[:40]}", callback_data=f"r:{tg_chat_id}:{cid}")
    kb.button(text="🚪 Убрать бота из чата", callback_data=f"c:{tg_chat_id}:leave")
    kb.button(text="⬅️ Назад", callback_data="m:chats")
    kb.adjust(1)
    who = "вы" if added_by == user_id else (await app.db.trusted_name(added_by) or str(added_by))
    if sources:
        hint = "Отметьте чаты MAX, сообщения из которых пересылать сюда."
    elif app.is_owner(user_id):
        hint = "Сначала выберите источники: меню → 📥 Источники MAX."
    else:
        hint = "Владелец ещё не открыл вам ни одного чата MAX."
    text = f"<b>{esc(title)}</b>\nДобавил: {esc(who)}\n\n{hint}"
    return text, kb.as_markup()


def _only_back(user_id: int, app: App) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="m:chats")
    return kb.as_markup()


async def trusted_menu(app: App) -> Screen:
    users = await app.db.list_trusted()
    kb = InlineKeyboardBuilder()
    for uid, name in users:
        kb.button(text=f"{name or uid}", callback_data=f"t:{uid}:show")
    kb.button(text="➕ Добавить", callback_data="t:0:add")
    _back(kb)
    kb.adjust(1)
    text = (
        "<b>👥 Доверенные пользователи</b>\n\n"
        "Могут добавлять бота в свои чаты Telegram и выбирать для них чаты MAX — "
        "только из тех, что вы им открыли."
    )
    return text, kb.as_markup()


async def trusted_user_menu(app: App, uid: int) -> Screen:
    name = await app.db.trusted_name(uid)
    kb = InlineKeyboardBuilder()
    if name is None:
        _back(kb, "m:tu")
        return "Пользователь уже не в списке доверенных.", kb.as_markup()
    allowed = await app.db.allowed_groups(uid)
    for g in await app.db.list_groups():
        kb.button(text=f"{onoff(g.max_chat_id in allowed)} {g.title[:40]}", callback_data=f"a:{uid}:{g.max_chat_id}")
    kb.button(text="🗑 Убрать из доверенных", callback_data=f"t:{uid}:del")
    _back(kb, "m:tu")
    kb.adjust(1)
    chats = await app.db.list_tg_chats(uid)
    text = (
        f"<b>{esc(name or str(uid))}</b> (id <code>{uid}</code>)\n"
        f"Добавил бота в чатов: {len(chats)}\n\n"
        "Отметьте чаты MAX, которые этот пользователь может пересылать в свои чаты."
    )
    return text, kb.as_markup()


async def classification_menu(app: App) -> Screen:
    r = app.rules.rules
    enabled = await app.settings.classification()
    stop = (
        f"🚫 Стоп-слова: {len(r.stop_keywords)}, стоп-авторы: {len(r.stop_authors)} — "
        "такие сообщения не пересылаются никогда, при любых настройках.\n"
        "/stop_add, /stop_del — стоп-слова\n\n"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🏷 Разделение {onoff(enabled)}", callback_data="s:cls")
    if not enabled:
        text = (
            "<b>🏷 Разделение на объявления и диалоги</b>\n\n"
            "Выключено: пересылаются все сообщения выбранных чатов MAX.\n\n"
            "Включите, чтобы у каждого источника появились переключатели «📢 Объявления» и «💬 Диалоги», "
            "а бот сам сортировал сообщения (правила, ИИ, спорные — вам на модерацию).\n\n" + stop
        )
        layout = [1]
    else:
        ann = await app.settings.forward_announcements()
        dia = await app.settings.forward_dialogs()
        text = (
            "<b>🏷 Разделение на объявления и диалоги</b>: ✅\n\n"
            f"Пересылать объявления (везде): {onoff(ann)}\n"
            f"Пересылать диалоги (везде): {onoff(dia)}\n"
            "<i>У каждого источника свои переключатели в «📥 Источники MAX». "
            "Если у источника включены оба типа — классификатор для него не нужен, пересылается всё.</i>\n\n"
            f"Сильные слова: {len(r.strong_keywords)}, ключевые: {len(r.keywords)}, "
            f"слова диалога: {len(r.dialog_keywords)}\n"
            f"Авторы «пересылать всё»: {len(r.always_forward_authors)}\n"
            f"Пороги: объявление от {r.announcement_threshold} баллов, диалог до {r.dialog_threshold}\n"
            f"Примеры для обучения: {len(r.examples)}\n\n" + stop + "Команды:\n"
            "/kw — показать все слова\n"
            "/kw_add, /kw_del — ключевые слова\n"
            "/skw_add, /skw_del — сильные слова\n"
            "/dkw_add, /dkw_del — слова диалога\n"
            "/threshold 4 1 — пороги\n"
            "/reload_rules — перечитать rules.json после ручной правки"
        )
        kb.button(text=f"📢 Объявления {onoff(ann)}", callback_data="s:ann")
        kb.button(text=f"💬 Диалоги {onoff(dia)}", callback_data="s:dia")
        kb.button(text="📌 Авторы «пересылать всё»", callback_data="m:authors")
        layout = [1, 2, 1]
    kb.button(text="🚫 Стоп-авторы", callback_data="m:stopauthors")
    _back(kb)
    kb.adjust(*layout, 1, 1)
    return text, kb.as_markup()


async def authors_menu(app: App) -> Screen:
    authors = app.rules.rules.always_forward_authors
    kb = InlineKeyboardBuilder()
    for a in authors:
        kb.button(text=f"❌ {a.name or a.id}", callback_data=f"au:{a.id}:del")
    _back(kb, "m:cls")
    kb.adjust(1)
    if authors:
        text = "<b>📌 Авторы «пересылать всё»</b>\n\nНажмите, чтобы убрать из списка."
    else:
        text = "<b>📌 Авторы «пересылать всё»</b>\n\nСписок пуст."
    text += (
        "\n\nДобавить автора: кнопка «📌 Автор — всегда пересылать» на спорном сообщении "
        "или вручную в rules.json (always_forward_authors)."
    )
    return text, kb.as_markup()


async def stop_authors_menu(app: App) -> Screen:
    authors = app.rules.rules.stop_authors
    kb = InlineKeyboardBuilder()
    for a in authors:
        kb.button(text=f"❌ {a.name or a.id}", callback_data=f"sa:{a.id}:del")
    _back(kb, "m:cls")
    kb.adjust(1)
    text = "<b>🚫 Стоп-авторы</b>\n\n"
    text += "Нажмите, чтобы убрать из списка." if authors else "Список пуст."
    text += (
        "\n\nДобавить: кнопка «🚫 Автор — никогда» на карточке модерации "
        "или вручную в rules.json (stop_authors)."
    )
    return text, kb.as_markup()


STAT_DAYS = 7


async def stats_menu(app: App) -> Screen:
    since = int(time.time()) - STAT_DAYS * 86400
    data = await app.db.stats(since)
    titles = {g.max_chat_id: g.title for g in await app.db.list_groups()}
    lines = [f"<b>📊 Статистика за {STAT_DAYS} дней</b>"]
    for chat_id, s in sorted(data.items(), key=lambda x: -x[1].get("total", 0)):
        total = s.get("total", 0)
        forwarded = s.get("forwarded", 0)
        pending = s.get("pending", 0)
        stopped = s.get("stopped", 0)
        rest = max(total - forwarded - pending - stopped, 0)
        title = titles.get(chat_id) or f"Чат {chat_id} (больше не источник)"
        lines.append(
            f"<b>{esc(title)}</b>\nвсего {total} · переслано {forwarded} · не переслано {rest} · "
            f"стоп {stopped} · ждут {pending}"
        )
    if len(lines) == 1:
        lines.append("Сообщений пока не было.")
    kb = InlineKeyboardBuilder()
    _back(kb)
    return "\n\n".join(lines), kb.as_markup()


async def media_menu(app: App) -> Screen:
    flags = await app.settings.media()
    kb = InlineKeyboardBuilder()
    for kind in MEDIA_KINDS:
        kb.button(text=f"{onoff(flags[kind])} {MEDIA_NAMES[kind]}", callback_data=f"md:{kind}")
    _back(kb)
    kb.adjust(1)
    text = (
        "<b>📎 Вложения</b>\n\n"
        "Сообщения с включёнными вложениями пересылаются всегда, без классификации. "
        "Выключенные вложения не пересылаются; текст такого сообщения проходит обычную проверку."
    )
    return text, kb.as_markup()


async def ai_menu(app: App) -> Screen:
    ai = await app.settings.ai()
    if ai is None:
        state = "не подключён — спорные сообщения идут вам на модерацию"
    else:
        state = f"подключён\nАдрес: <code>{esc(ai.base_url)}</code>\nМодель: <code>{esc(ai.model)}</code>"
    text = (
        f"<b>🤖 ИИ для спорных сообщений</b>\n\n{state}\n\n"
        "Подключить (любой OpenAI-совместимый API):\n"
        "<code>/ai адрес модель ключ</code>\n"
        "Примеры:\n"
        "<code>/ai https://api.openai.com/v1 gpt-4o-mini sk-...</code>\n"
        "<code>/ai https://api.anthropic.com/v1 claude-haiku-5-5 sk-ant-...</code>\n"
        "<code>/ai https://api.deepseek.com deepseek-chat sk-...</code>\n"
        "<code>/ai https://openrouter.ai/api/v1 модель sk-or-...</code>\n"
        "<code>/ai http://localhost:11434/v1 llama3.1 -</code> (Ollama, без ключа)\n\n"
        "Сообщение с ключом бот сразу удалит из чата."
    )
    kb = InlineKeyboardBuilder()
    if ai is not None:
        kb.button(text="🧪 Проверить", callback_data="ai:test")
        kb.button(text="⏹ Отключить", callback_data="ai:off")
    _back(kb)
    kb.adjust(2, 1)
    return text, kb.as_markup()


async def max_menu(app: App) -> Screen:
    phone = await app.settings.max_phone()
    if app.max.connected:
        state = "🟢 подключён"
    elif app.max.running:
        state = "🟡 подключается…"
    else:
        state = "🔴 не подключён"
    text = (
        "<b>🔑 Аккаунт MAX</b>\n\n"
        f"Состояние: {state}\n"
        f"Телефон: {esc(phone) if phone else '—'}\n"
        f"Сессия сохранена: {'да' if app.max.has_session else 'нет'}\n\n"
        "/login [телефон] — войти (код придёт в MAX/SMS, пришлите его сюда)\n"
        "/logout — выйти и удалить сессию"
    )
    kb = InlineKeyboardBuilder()
    _back(kb)
    return text, kb.as_markup()


async def leave_menu(app: App) -> Screen:
    text = (
        "<b>🚪 Сообщение при выходе</b>\n\n"
        "Его бот пишет в чат, куда его добавил посторонний, и сразу выходит.\n\n"
        f"Сейчас:\n<blockquote>{esc(await app.settings.leave_message())}</blockquote>\n\n"
        "Изменить: <code>/leave_message новый текст</code>"
    )
    kb = InlineKeyboardBuilder()
    _back(kb)
    return text, kb.as_markup()


async def moderation_menu(app: App) -> Screen:
    pending = await app.db.list_moderation()
    text = (
        f"<b>❓ Модерация</b>\n\nЖдут решения: {len(pending)}\n\n"
        "Сообщения висят, пока вы не нажмёте кнопку решения. "
        "Если сообщения с кнопками потерялись — пришлю их заново."
    )
    kb = InlineKeyboardBuilder()
    if pending:
        kb.button(text="🔁 Прислать заново", callback_data="mod:resend")
    _back(kb)
    kb.adjust(1)
    return text, kb.as_markup()
