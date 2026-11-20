"""Классификация спорных сообщений любой ИИ через OpenAI-совместимый API.

Такой формат понимают OpenAI, Anthropic (Claude), DeepSeek, OpenRouter, Groq, Mistral,
локальные Ollama / LM Studio и др. — нужны только адрес, ключ и имя модели.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import aiohttp

from .classifier import ANNOUNCEMENT, DIALOG
from .rules import Example

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты сортируешь сообщения из рабочих групповых чатов. "
    "«announcement» — объявление: информация или указание для всех или для перечисленных людей "
    "(сбор, время, дежурство, задание, ссылка, которую надо пройти, изменения расписания). "
    "«dialog» — обычная переписка: вопросы, ответы, благодарности, обсуждение, шутки. "
    'Ответь строго JSON без пояснений: {"label": "announcement"} или {"label": "dialog"}.'
)


@dataclass
class AiSettings:
    base_url: str
    api_key: str
    model: str

    @property
    def endpoint(self) -> str:
        url = self.base_url.rstrip("/")
        return url if url.endswith("/chat/completions") else url + "/chat/completions"


class AiError(Exception):
    pass


def build_messages(text: str, chat_title: str, examples: list[Example]) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for ex in examples[-20:]:
        messages.append({"role": "user", "content": ex.text})
        messages.append({"role": "assistant", "content": json.dumps({"label": ex.label})})
    messages.append({"role": "user", "content": f"[Группа: {chat_title}]\n{text}"})
    return messages


def parse_label(content: str) -> str:
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if match:
        try:
            label = json.loads(match.group(0)).get("label")
            if label in {ANNOUNCEMENT, DIALOG}:
                return label
        except (json.JSONDecodeError, AttributeError):
            pass
    lowered = content.lower()
    if ANNOUNCEMENT in lowered and DIALOG not in lowered:
        return ANNOUNCEMENT
    if DIALOG in lowered and ANNOUNCEMENT not in lowered:
        return DIALOG
    raise AiError(f"непонятный ответ модели: {content[:200]!r}")


async def classify_with_ai(
    settings: AiSettings, text: str, chat_title: str, examples: list[Example], timeout: float = 30
) -> str:
    payload = {
        "model": settings.model,
        "messages": build_messages(text, chat_title, examples),
        "temperature": 0,
        "max_tokens": 20,
    }
    headers = {"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(settings.endpoint, json=payload, headers=headers) as resp:
                body = await resp.text()
                if resp.status != 200:
                    raise AiError(f"HTTP {resp.status}: {body[:200]}")
    except aiohttp.ClientError as e:
        raise AiError(f"сеть: {e}") from e
    except TimeoutError as e:
        raise AiError("таймаут") from e
    try:
        content = json.loads(body)["choices"][0]["message"]["content"] or ""
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        raise AiError(f"неожиданный формат ответа: {body[:200]}") from e
    return parse_label(content)
