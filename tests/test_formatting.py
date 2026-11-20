from zoneinfo import ZoneInfo

from bot.formatting import DELETED_MARK, SEPARATOR, render, split_text, visible_len
from bot.models import ADMIN, OWNER, Author, IncomingMessage, Quote

MSK = ZoneInfo("Europe/Moscow")
# 2026-11-02 09:30 по Москве
TS = 1793601000


def msg(**kw) -> IncomingMessage:
    base = dict(
        chat_id=1,
        msg_id=2,
        chat_title="Группа",
        author=Author(id=5, name="Иван Петров", username="ivanp", role=ADMIN),
        time=TS,
        text="Всем привет <b>",
    )
    base.update(kw)
    return IncomingMessage(**base)


def test_header_like_template():
    html = render(msg(), MSK)
    lines = html.split("\n")
    assert lines[0] == "👤 <b>Иван Петров</b> - <b>@ivanp</b> | 👔<b>Админ</b>"
    assert lines[1] == "🕗 <b>09:30</b> - 📅 <b>02.11.2026</b>"
    assert html.count(SEPARATOR) == 2
    assert "Всем привет &lt;b&gt;" in html


def test_owner_and_member_roles():
    owner = render(msg(author=Author(1, "А", None, OWNER)), MSK)
    assert "⭕<b>Владелец</b>" in owner and "@" not in owner.split("\n")[0]
    member = render(msg(author=Author(1, "А")), MSK)
    assert "|" not in member.split("\n")[0]


def test_deleted_mark_and_reply():
    html = render(msg(reply_to=Quote("Мария", "во сколько?")), MSK, deleted=True)
    assert html.endswith(DELETED_MARK)
    assert "↩️ в ответ на <b>Мария</b>" in html


def test_chat_title_line():
    assert render(msg(), MSK, show_chat_title=True).startswith("💬 <b>Группа</b>\n👤")


def test_media_only_has_no_separators():
    assert SEPARATOR not in render(msg(text=""), MSK)


def test_split_and_visible_len():
    text = ("слово " * 2000).strip()
    parts = split_text(text, 1000)
    assert all(len(p) <= 1000 for p in parts)
    assert " ".join(parts) == text
    assert visible_len("<b>a&amp;b</b>") == 3


def test_roundtrip_dict():
    m = msg(reply_to=Quote("X", "y"))
    assert IncomingMessage.from_dict(m.to_dict()) == m
