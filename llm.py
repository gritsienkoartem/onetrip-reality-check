"""Интеграция с языковой моделью (любой OpenAI-совместимый API).

Настройки берутся из переменных окружения (файл .env):
    LLM_BASE_URL         — адрес API, например https://api.openai.com/v1
    LLM_MODEL            — название модели
    LLM_API_KEY          — ключ (для локальных моделей может быть пустым)
    LLM_TIMEOUT_SECONDS  — таймаут запроса в секундах (по умолчанию 30)

Модуль ничего не придумывает сам: при любой проблеме (нет настроек,
таймаут, ошибка сервиса, неверный JSON) он вызывает LLMError, а вызывающий
код решает, что делать дальше (например, перейти на разбор по правилам).
"""
from __future__ import annotations

import json
import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()

DEFAULT_TIMEOUT_SECONDS = 30


class LLMError(Exception):
    """Любая ошибка при работе с языковой моделью. Текст — на русском."""


def is_llm_configured() -> bool:
    """Модель считается настроенной, если заданы адрес API и название модели."""
    return bool(os.getenv("LLM_BASE_URL", "").strip() and os.getenv("LLM_MODEL", "").strip())


def _timeout_seconds() -> int:
    try:
        value = int(os.getenv("LLM_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS


def extract_json_object(text: str) -> dict:
    """Достаёт JSON-объект из ответа модели.

    Модели часто оборачивают JSON в ```json ... ``` или добавляют пояснения,
    поэтому сначала пробуем разобрать текст целиком, потом — фрагмент
    от первой «{» до последней «}».
    """
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE).strip()

    candidates = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        candidates.append(cleaned[start : end + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise LLMError("модель вернула ответ, который не удалось разобрать как JSON")


def ask_llm_json(system_prompt: str, user_message: str) -> dict:
    """Отправляет запрос модели и возвращает JSON-объект из её ответа."""
    if not is_llm_configured():
        raise LLMError("языковая модель не настроена (заполните LLM_BASE_URL и LLM_MODEL в .env)")

    base_url = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
    api_key = os.getenv("LLM_API_KEY", "").strip()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": os.getenv("LLM_MODEL", "").strip(),
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    }

    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=_timeout_seconds(),
        )
    except requests.Timeout:
        raise LLMError("превышено время ожидания ответа модели") from None
    except requests.RequestException:
        raise LLMError("не удалось подключиться к сервису модели") from None

    if response.status_code != 200:
        # Тело ответа не показываем: в нём могут быть служебные данные.
        raise LLMError(f"сервис модели вернул ошибку (код {response.status_code})")

    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise LLMError("сервис модели вернул ответ неожиданного формата") from None

    if not isinstance(content, str) or not content.strip():
        raise LLMError("модель вернула пустой ответ")

    return extract_json_object(content)
