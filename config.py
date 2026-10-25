"""Общие пути проекта и чтение JSON-настроек."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"


class ConfigError(Exception):
    """Ошибка чтения или формата файла настроек."""


def load_json(filename: str, data_dir: Path | None = None) -> Any:
    """Читает JSON из папки data (или из указанной папки)."""
    path = (data_dir or DATA_DIR) / filename
    try:
        with path.open(encoding="utf-8") as file:
            return json.load(file)
    except OSError:
        raise ConfigError(f"не удалось прочитать файл {filename}") from None
    except json.JSONDecodeError:
        raise ConfigError(f"файл {filename} содержит некорректный JSON") from None


def load_assumptions(data_dir: Path | None = None) -> dict:
    """Загружает assumptions.json и проверяет, что это объект."""
    data = load_json("assumptions.json", data_dir)
    if not isinstance(data, dict):
        raise ConfigError("assumptions.json должен содержать JSON-объект")
    return data
