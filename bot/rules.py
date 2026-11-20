"""Правила классификации в data/rules.json — их можно править руками или через бота."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Слова и начала слов. Совпадение ищется с начала слова: «прибы» найдёт «прибыть» и «прибыли»,
# «все» — «все», «всем», «всех». Слова диалога (dialog_keywords) сравниваются целиком.
DEFAULT_STRONG = [
    "внимание",
    "все",
    "объявлени",
    "важно",
    "срочно",
    "обязательно",
    "напоминаю",
    "напоминание",
    "к сведению",
    "информирую",
    "сообщаю",
]
DEFAULT_KEYWORDS = [
    "прибы",
    "прийти",
    "приходить",
    "явиться",
    "явка",
    "дежур",
    "собрание",
    "сбор",
    "построение",
    "сдать",
    "сдаём",
    "пройти",
    "заполни",
    "зарегистрир",
    "расписани",
    "перенос",
    "отмен",
    "завтра",
    "сегодня",
    "не позже",
    "не позднее",
    "до конца",
    "пишите",
    "просьба",
    "необходимо",
    "нужно всем",
    "ссылк",
    "по ссылке",
]
DEFAULT_DIALOG = [
    "спасибо",
    "спс",
    "ок",
    "окей",
    "хорошо",
    "понял",
    "поняла",
    "ага",
    "угу",
    "да",
    "нет",
    "привет",
    "ахах",
    "хах",
    "лол",
    "+",
]


@dataclass
class Author:
    id: int
    name: str = ""


@dataclass
class Example:
    text: str
    label: str  # "announcement" | "dialog"


@dataclass
class Rules:
    strong_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_STRONG))
    keywords: list[str] = field(default_factory=lambda: list(DEFAULT_KEYWORDS))
    dialog_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_DIALOG))
    # Авторы MAX, все сообщения которых пересылаются как объявления.
    always_forward_authors: list[Author] = field(default_factory=list)
    # Балл ≥ announcement — объявление, ≤ dialog — диалог, между — спорное.
    announcement_threshold: int = 4
    dialog_threshold: int = 1
    # Решения владельца по спорным сообщениям: подсказки для ИИ.
    examples: list[Example] = field(default_factory=list)
    max_examples: int = 40
    # «Никогда не пересылать»: слова (с начала слова, как ключевые) и авторы MAX. Проверяются первыми.
    stop_keywords: list[str] = field(default_factory=list)
    stop_authors: list[Author] = field(default_factory=list)

    def author_ids(self) -> set[int]:
        return {a.id for a in self.always_forward_authors}

    def stop_author_ids(self) -> set[int]:
        return {a.id for a in self.stop_authors}


class RulesStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self.rules = self._load()

    def _load(self) -> Rules:
        if not self.path.exists():
            rules = Rules()
            self._write(rules)
            return rules
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            # Не затираем файл с ошибкой — владелец мог править его руками.
            logger.error("rules.json не читается (%s) — работаю с правилами по умолчанию", e)
            return Rules()
        return _from_dict(raw)

    def reload(self) -> None:
        self.rules = self._load()

    def _write(self, rules: Rules) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(rules), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    async def save(self) -> None:
        async with self._lock:
            self._write(self.rules)

    async def add_keyword(self, kind: str, word: str) -> bool:
        target = self._list(kind)
        word = word.lower().replace("ё", "е")
        if not word or word in target:
            return False
        target.append(word)
        await self.save()
        return True

    async def remove_keyword(self, kind: str, word: str) -> bool:
        target = self._list(kind)
        word = word.lower().replace("ё", "е")
        if word not in target:
            return False
        target.remove(word)
        await self.save()
        return True

    async def add_author(self, author_id: int, name: str) -> bool:
        if author_id in self.rules.author_ids():
            return False
        self.rules.always_forward_authors.append(Author(author_id, name))
        await self.save()
        return True

    async def remove_author(self, author_id: int) -> bool:
        before = len(self.rules.always_forward_authors)
        self.rules.always_forward_authors = [a for a in self.rules.always_forward_authors if a.id != author_id]
        if len(self.rules.always_forward_authors) == before:
            return False
        await self.save()
        return True

    async def add_stop_author(self, author_id: int, name: str) -> bool:
        if author_id in self.rules.stop_author_ids():
            return False
        self.rules.stop_authors.append(Author(author_id, name))
        # Один и тот же человек не может быть и в «пересылать всё», и в «никогда».
        self.rules.always_forward_authors = [a for a in self.rules.always_forward_authors if a.id != author_id]
        await self.save()
        return True

    async def remove_stop_author(self, author_id: int) -> bool:
        before = len(self.rules.stop_authors)
        self.rules.stop_authors = [a for a in self.rules.stop_authors if a.id != author_id]
        if len(self.rules.stop_authors) == before:
            return False
        await self.save()
        return True

    async def add_example(self, text: str, label: str) -> None:
        text = text.strip()
        if not text:
            return
        self.rules.examples.append(Example(text[:500], label))
        self.rules.examples = self.rules.examples[-self.rules.max_examples :]
        await self.save()

    def _list(self, kind: str) -> list[str]:
        if kind == "strong":
            return self.rules.strong_keywords
        if kind == "keyword":
            return self.rules.keywords
        if kind == "dialog":
            return self.rules.dialog_keywords
        if kind == "stop":
            return self.rules.stop_keywords
        raise ValueError(kind)


def _authors(raw: list) -> list[Author]:
    authors = []
    for a in raw if isinstance(raw, list) else []:
        if isinstance(a, dict) and "id" in a:
            authors.append(Author(int(a["id"]), str(a.get("name", ""))))
        elif isinstance(a, (int, str)) and str(a).lstrip("-").isdigit():
            authors.append(Author(int(a)))
    return authors


def _from_dict(raw: dict) -> Rules:
    base = Rules()
    examples = [
        Example(str(e["text"]), str(e["label"]))
        for e in raw.get("examples", [])
        if isinstance(e, dict) and "text" in e and e.get("label") in {"announcement", "dialog"}
    ]

    def words(key: str, default: list[str]) -> list[str]:
        value = raw.get(key)
        if not isinstance(value, list):
            return default
        return [str(w).lower().replace("ё", "е") for w in value if str(w)]

    return Rules(
        strong_keywords=words("strong_keywords", base.strong_keywords),
        keywords=words("keywords", base.keywords),
        dialog_keywords=words("dialog_keywords", base.dialog_keywords),
        always_forward_authors=_authors(raw.get("always_forward_authors", [])),
        announcement_threshold=int(raw.get("announcement_threshold", base.announcement_threshold)),
        dialog_threshold=int(raw.get("dialog_threshold", base.dialog_threshold)),
        examples=examples,
        max_examples=int(raw.get("max_examples", base.max_examples)),
        stop_keywords=words("stop_keywords", []),
        stop_authors=_authors(raw.get("stop_authors", [])),
    )
