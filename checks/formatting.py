"""Небольшие помощники форматирования для текстов проверок."""
from __future__ import annotations

from datetime import date, time


def format_rub(value: float) -> str:
    """Сумма в рублях с пробелами между разрядами: 52 390 ₽."""
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def format_date(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value is not None else "дата не указана"


def format_time(value: time | None) -> str:
    return value.strftime("%H:%M") if value is not None else "—"


def format_minutes(value: int) -> str:
    return f"{value} мин"
