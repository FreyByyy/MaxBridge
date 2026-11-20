"""Классификация сообщения MAX: объявление / диалог / спорное — по баллам из правил."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .rules import Rules

ANNOUNCEMENT = "announcement"
DIALOG = "dialog"
DISPUTED = "disputed"

CATEGORY_NAMES = {ANNOUNCEMENT: "📢 Объявление", DIALOG: "💬 Диалог", DISPUTED: "❓ Спорное"}

TIME_RE = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3])[:.][0-5]\d(?!\d)")
DATE_RE = re.compile(r"(?<!\d)(?:0?[1-9]|[12]\d|3[01])[./](?:0?[1-9]|1[0-2])(?:[./](?:\d{2}|\d{4}))?(?!\d)")
LINK_RE = re.compile(r"(?:https?://|www\.|max\.ru/|t\.me/)\S+", re.IGNORECASE)
WEEKDAY_RE = re.compile(
    r"(?<!\w)(?:понедельник|вторник|сред[ауые](?!\w)|четверг|пятниц|суббот|воскресень|(?:пн|вт|ср|чт|пт|сб|вс)(?!\w))",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[a-zа-я]+(?:-[a-zа-я]+)?")
# Окончания русских фамилий — для списков фамилий, когда список участников группы недоступен.
SURNAME_RE = re.compile(
    r"(?<![\w-])[А-ЯЁ][а-яё]+(?:ов|ова|ев|ева|ёв|ёва|ин|ина|ын|ына|ский|ская|цкий|цкая|ко|ук|юк|ич|ян|дзе|швили|их|ых)(?![\w])"
)


@dataclass
class ClassifyInput:
    text: str
    sender_id: int | None = None
    sender_is_admin: bool = False
    is_reply: bool = False
    # Фамилии участников группы в нижнем регистре.
    participant_surnames: set[str] = field(default_factory=set)


@dataclass
class Verdict:
    category: str
    score: int
    reasons: list[str]


def normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


def keyword_hits(text_norm: str, keywords: list[str]) -> list[str]:
    """Ключевые слова, с которых начинается хотя бы одно слово текста (фразы — тоже с начала слова)."""
    hits = []
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        if re.search(r"(?<!\w)" + re.escape(kw), text_norm):
            hits.append(kw)
    return hits


def whole_word_hits(text_norm: str, words: list[str]) -> list[str]:
    hits = []
    for w in words:
        w = w.strip()
        if not w:
            continue
        if re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", text_norm):
            hits.append(w)
    return hits


def surname_mentions(text: str, surnames: set[str]) -> int:
    """Сколько разных участников группы упомянуто по фамилии (с учётом падежей: Иванов → Иванову)."""
    if not surnames:
        return 0
    tokens = set(WORD_RE.findall(normalize(text)))
    found = set()
    for s in surnames:
        if len(s) < 3:
            continue
        stem = s[:-1] if len(s) >= 5 else s
        if any(t == s or t.startswith(stem) and len(t) - len(s) <= 2 for t in tokens):
            found.add(s)
    return len(found)


def stop_reason(text: str, sender_id: int | None, rules: Rules) -> str | None:
    """Почему сообщение нельзя пересылать ни при каких условиях (или None)."""
    if sender_id is not None and sender_id in rules.stop_author_ids():
        return "стоп-автор"
    hits = keyword_hits(normalize(text), rules.stop_keywords)
    if hits:
        return "стоп-слова: " + ", ".join(hits)
    return None


def classify(inp: ClassifyInput, rules: Rules) -> Verdict:
    if inp.sender_id is not None and inp.sender_id in rules.author_ids():
        return Verdict(ANNOUNCEMENT, 100, ["автор из списка «пересылать всё»"])

    text = inp.text.strip()
    norm = normalize(text)
    score = 0
    reasons: list[str] = []

    def add(points: int, why: str) -> None:
        nonlocal score
        score += points
        reasons.append(f"{points:+d} {why}")

    strong = keyword_hits(norm, rules.strong_keywords)
    if strong:
        add(min(3 * len(strong), 6), "сильные слова: " + ", ".join(strong))
    weak = keyword_hits(norm, rules.keywords)
    if weak:
        add(min(len(weak), 3), "ключевые слова: " + ", ".join(weak))
    if TIME_RE.search(text):
        add(2, "время")
    if DATE_RE.search(text):
        add(1, "дата")
    if WEEKDAY_RE.search(norm):
        add(1, "день недели")
    if LINK_RE.search(text):
        add(1, "ссылка")

    mentioned = surname_mentions(text, inp.participant_surnames)
    if mentioned >= 2:
        add(3, f"фамилии участников ({mentioned})")
    elif mentioned == 1:
        add(1, "фамилия участника")
    else:
        surname_like = len(set(SURNAME_RE.findall(text)))
        if surname_like >= 2:
            add(2, f"похоже на список фамилий ({surname_like})")

    if inp.sender_is_admin:
        add(2, "автор — админ/владелец группы")
    if text.count("\n") >= 2:
        add(1, "несколько строк")
    if text.endswith("!"):
        add(1, "восклицание")

    dialog = whole_word_hits(norm, rules.dialog_keywords)
    if dialog:
        add(-2, "слова диалога: " + ", ".join(dialog))
    if inp.is_reply:
        add(-2, "ответ на сообщение")
    words = WORD_RE.findall(norm)
    if len(words) <= 3 and not strong and not weak:
        add(-2, "очень короткое")
    if text.endswith("?") and not strong:
        add(-1, "вопрос")

    if score >= rules.announcement_threshold:
        category = ANNOUNCEMENT
    elif score <= rules.dialog_threshold:
        category = DIALOG
    else:
        category = DISPUTED
    return Verdict(category, score, reasons)


def learnable_words(text: str, rules: Rules, limit: int = 6) -> list[str]:
    """Слова сообщения, которых ещё нет в правилах, — кандидаты для «обучения» на решении владельца."""
    known = set(rules.strong_keywords) | set(rules.keywords) | set(rules.dialog_keywords)
    out: list[str] = []
    for w in WORD_RE.findall(normalize(text)):
        if len(w) < 4 or w in out:
            continue
        if any(w.startswith(k.strip()) for k in known if k.strip()):
            continue
        out.append(w)
        if len(out) >= limit:
            break
    return out
