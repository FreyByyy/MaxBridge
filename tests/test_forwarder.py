"""Сценарии ядра с поддельным Telegram-ботом: пересылка, правка, удаление, модерация."""

from __future__ import annotations

import dataclasses
import itertools
import time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from bot.classifier import ANNOUNCEMENT, DIALOG
from bot.config import Config
from bot.db import Database
from bot.formatting import DELETED_MARK
from bot.forwarder import Forwarder
from bot.models import Author, IncomingMessage
from bot.rules import RulesStore
from bot.settings import Settings

OWNER = 1000
TG_CHAT = -100500
MAX_CHAT = 77


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._ids = itertools.count(1)

    def _sent(self, chat_id: int, **kw):  # noqa: ANN003, ANN202
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=next(self._ids), caption=kw.get("caption"))

    async def send_message(self, chat_id: int, text: str, **kw):  # noqa: ANN003, ANN201
        self.calls.append(("send_message", {"chat_id": chat_id, "text": text, **kw}))
        return self._sent(chat_id)

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, **kw):  # noqa: ANN003, ANN201
        self.calls.append(("edit_message_text", {"chat_id": chat_id, "message_id": message_id, "text": text, **kw}))
        return True

    async def edit_message_caption(self, chat_id: int, message_id: int, caption: str, **kw):  # noqa: ANN003, ANN201
        self.calls.append(("edit_message_caption", {"chat_id": chat_id, "message_id": message_id, "caption": caption}))
        return True

    def of(self, name: str) -> list[dict]:
        return [kw for n, kw in self.calls if n == name]


@pytest.fixture
async def env(tmp_path: Path):
    cfg = Config(
        bot_token="x",
        owner_id=OWNER,
        data_dir=tmp_path,
        tz=ZoneInfo("Europe/Moscow"),
        max_phone=None,
        max_show_online=False,
        ai_base_url=None,
        ai_api_key=None,
        ai_model=None,
        log_level="INFO",
    )
    db = Database(cfg.db_path)
    await db.open()
    bot = FakeBot()
    rules = RulesStore(cfg.rules_path)
    settings = Settings(db, cfg)
    await settings.init_defaults()
    fw = Forwarder(bot, db, rules, settings, cfg)  # type: ignore[arg-type]
    await db.enable_group(MAX_CHAT, "Работа")
    await db.add_tg_chat(TG_CHAT, "Объявления", OWNER)
    await db.set_route(TG_CHAT, MAX_CHAT, True)
    yield SimpleNamespace(db=db, bot=bot, fw=fw, rules=rules, settings=settings)
    await db.close()


@pytest.fixture
async def classified(env):
    """Разделение на объявления/диалоги включено (по умолчанию в новой установке — выключено)."""
    await env.settings.set_classification(True)
    return env


def incoming(msg_id: int, text: str, **kw) -> IncomingMessage:
    return IncomingMessage(
        chat_id=MAX_CHAT,
        msg_id=msg_id,
        chat_title="Работа",
        author=Author(id=5, name="Пётр"),
        time=int(time.time()),
        text=text,
        **kw,
    )


async def test_announcement_forwarded_edited_deleted(classified):
    env = classified
    await env.fw.on_new(incoming(1, "Внимание! Всем прибыть в 9:00"))
    sent = env.bot.of("send_message")
    assert len(sent) == 1 and sent[0]["chat_id"] == TG_CHAT
    assert "прибыть в 9:00" in sent[0]["text"]

    await env.fw.on_edit(incoming(1, "Внимание! Всем прибыть в 10:00"))
    edits = env.bot.of("edit_message_text")
    assert len(edits) == 1 and "10:00" in edits[0]["text"]

    await env.fw.on_delete(MAX_CHAT, [1])
    edits = env.bot.of("edit_message_text")
    assert len(edits) == 2 and edits[1]["text"].endswith(DELETED_MARK) and "10:00" in edits[1]["text"]

    # Повторное удаление не дублирует пометку.
    await env.fw.on_delete(MAX_CHAT, [1])
    assert len(env.bot.of("edit_message_text")) == 2


async def test_dialog_not_forwarded_and_duplicates_ignored(classified):
    env = classified
    await env.fw.on_new(incoming(2, "спасибо"))
    await env.fw.on_new(incoming(2, "спасибо"))
    assert env.bot.of("send_message") == []
    assert (await env.db.get_decision(MAX_CHAT, 2))[0] == DIALOG


async def test_dialog_forwarded_when_enabled(classified):
    env = classified
    await env.db.set_group_flag(MAX_CHAT, "forward_dialogs", True)
    await env.fw.settings.set_global("forward_dialogs", True)
    await env.fw.on_new(incoming(3, "спасибо"))
    assert len(env.bot.of("send_message")) == 1


async def test_old_messages_and_unknown_groups_ignored(classified):
    env = classified
    await env.fw.on_new(dataclasses.replace(incoming(4, "Внимание всем!"), time=1))
    await env.fw.on_new(dataclasses.replace(incoming(5, "Внимание всем!"), chat_id=999))
    assert env.bot.of("send_message") == []


async def test_disputed_goes_to_owner_and_resolves(classified):
    env = classified
    await env.fw.on_new(incoming(6, "Завтра собрание"))
    dm = env.bot.of("send_message")
    assert len(dm) == 1 and dm[0]["chat_id"] == OWNER and "Спорное" in dm[0]["text"]
    pending = await env.db.list_moderation()
    assert len(pending) == 1

    # Пока висит на модерации, сообщение в MAX изменили — карточка обновилась.
    await env.fw.on_edit(incoming(6, "Завтра собрание в 8:00"))
    assert "8:00" in env.bot.of("edit_message_text")[-1]["text"]

    result = await env.fw.resolve_moderation(pending[0].id, "a")
    assert result is not None and "переслано в чатов: 1" in result[0]
    forwarded = [c for c in env.bot.of("send_message") if c["chat_id"] == TG_CHAT]
    assert len(forwarded) == 1 and "8:00" in forwarded[0]["text"]
    assert await env.db.list_moderation() == []
    assert env.rules.rules.examples[-1].label == ANNOUNCEMENT
    # Второе нажатие — уже решено.
    assert await env.fw.resolve_moderation(pending[0].id, "a") is None


async def test_edit_turns_dialog_into_announcement(classified):
    env = classified
    await env.fw.on_new(incoming(7, "ок"))
    assert env.bot.of("send_message") == []
    await env.fw.on_edit(incoming(7, "Внимание! Всем прибыть завтра в 9:00"))
    assert len(env.bot.of("send_message")) == 1


async def test_media_disabled_text_classified(classified):
    env = classified
    from bot.models import Media

    await env.fw.settings.set_media("photo", False)
    await env.fw.on_new(incoming(8, "", media=[Media("photo", url="http://x")]))
    assert env.bot.of("send_message") == []
    assert (await env.db.get_decision(MAX_CHAT, 8))[0] == "skip"


# --- режимы: разделение выключено, оба типа, полная модерация, стоп-фильтры, без звука ----------


async def test_classification_off_by_default_forwards_everything(env):
    assert await env.settings.classification() is False
    await env.fw.on_new(incoming(20, "спасибо"))
    await env.fw.on_new(incoming(21, "Завтра собрание"))
    sent = env.bot.of("send_message")
    assert [c["chat_id"] for c in sent] == [TG_CHAT, TG_CHAT]
    assert await env.db.list_moderation() == []


async def test_existing_install_keeps_classification(tmp_path):
    db = Database(tmp_path / "bot.db")
    await db.open()
    await db.enable_group(MAX_CHAT, "Работа")
    settings = Settings(db, None)  # type: ignore[arg-type]
    await settings.init_defaults()
    assert await settings.classification() is True
    await db.close()


async def test_both_types_skip_classifier_and_moderation(classified):
    env = classified
    await env.db.set_group_flag(MAX_CHAT, "forward_dialogs", True)
    await env.settings.set_global("forward_dialogs", True)
    await env.fw.on_new(incoming(22, "Завтра собрание"))  # иначе было бы спорным
    assert await env.db.list_moderation() == []
    assert len(env.bot.of("send_message")) == 1
    assert (await env.db.get_decision(MAX_CHAT, 22))[1] == "no-classification"


async def test_both_types_off_forwards_nothing(classified):
    env = classified
    await env.db.set_group_flag(MAX_CHAT, "forward_announcements", False)
    await env.fw.on_new(incoming(23, "Внимание! Всем прибыть в 9:00"))
    assert env.bot.of("send_message") == []
    assert await env.db.list_moderation() == []


async def test_full_moderation_waits_for_owner(env):
    await env.db.set_group_flag(MAX_CHAT, "full_moderation", True)
    await env.fw.on_new(incoming(24, "Внимание! Всем прибыть в 9:00"))
    await env.fw.on_new(incoming(25, "спасибо"))
    cards = env.bot.of("send_message")
    assert [c["chat_id"] for c in cards] == [OWNER, OWNER]
    assert "На модерации" in cards[0]["text"]
    buttons = [b.callback_data for row in cards[0]["reply_markup"].inline_keyboard for b in row]
    assert any(b.endswith(":f") for b in buttons) and any(b.endswith(":s") for b in buttons)

    first, second = await env.db.list_moderation()
    assert first.kind == "full"
    text, _ = await env.fw.resolve_moderation(first.id, "f")
    assert "переслано в чатов: 1" in text
    text, _ = await env.fw.resolve_moderation(second.id, "s")
    assert "не пересылаю" in text
    forwarded = [c for c in env.bot.of("send_message") if c["chat_id"] == TG_CHAT]
    assert len(forwarded) == 1 and "9:00" in forwarded[0]["text"]
    # Разделение выключено — на решениях полной модерации ничему не учимся.
    assert env.rules.rules.examples == []


async def test_full_moderation_shows_hint_when_classifying(classified):
    env = classified
    await env.db.set_group_flag(MAX_CHAT, "full_moderation", True)
    await env.fw.on_new(incoming(26, "Внимание! Всем прибыть в 9:00"))
    card = env.bot.of("send_message")[0]["text"]
    assert "похоже на: 📢 Объявление" in card
    mod = (await env.db.list_moderation())[0]
    await env.fw.resolve_moderation(mod.id, "s")
    assert env.rules.rules.examples[-1].label == "dialog"


async def test_stop_words_and_authors_beat_everything(env):
    await env.rules.add_keyword("stop", "реклам")
    await env.db.set_group_flag(MAX_CHAT, "full_moderation", True)
    await env.fw.on_new(incoming(27, "Рекламная акция, всем скидки!"))
    assert env.bot.of("send_message") == []
    assert (await env.db.get_decision(MAX_CHAT, 27))[0] == "stopped"

    await env.db.set_group_flag(MAX_CHAT, "full_moderation", False)
    await env.rules.add_stop_author(5, "Пётр")
    await env.fw.on_new(incoming(28, "Внимание всем!"))
    assert env.bot.of("send_message") == []


async def test_ban_button_adds_stop_author(env):
    await env.db.set_group_flag(MAX_CHAT, "full_moderation", True)
    await env.rules.add_author(5, "Пётр")
    await env.fw.on_new(incoming(29, "купите слона"))
    mod = (await env.db.list_moderation())[0]
    text, _ = await env.fw.resolve_moderation(mod.id, "ban")
    assert "стоп-авторы" in text
    assert 5 in env.rules.rules.stop_author_ids() and 5 not in env.rules.rules.author_ids()


async def test_silent_dialogs(classified):
    env = classified
    await env.db.set_group_flag(MAX_CHAT, "forward_announcements", False)
    await env.db.set_group_flag(MAX_CHAT, "forward_dialogs", True)
    await env.db.set_group_flag(MAX_CHAT, "silent_dialogs", True)
    await env.settings.set_global("forward_dialogs", True)
    await env.fw.on_new(incoming(30, "спасибо"))
    sent = env.bot.of("send_message")
    assert len(sent) == 1 and sent[0].get("disable_notification") is True


async def test_stats_counts(env):
    await env.fw.on_new(incoming(31, "привет"))
    await env.rules.add_keyword("stop", "спам")
    await env.fw.on_new(incoming(32, "спам"))
    s = (await env.db.stats(0))[MAX_CHAT]
    assert s["total"] == 2 and s["forwarded"] == 1 and s["stopped"] == 1
