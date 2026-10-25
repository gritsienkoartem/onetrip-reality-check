"""Выбор провайдера данных через переменную окружения DATA_PROVIDER.

Продукт работает только с live-источниками. Вымышленные данные вынесены
в tests/fixtures и не выбираются этой функцией.
"""
from __future__ import annotations

import os

from providers.base import DataProvider, ProviderError
from providers.live import LiveProvider

__all__ = ["DataProvider", "LiveProvider", "ProviderError", "get_provider"]


def _live_notes() -> list[str]:
    notes = [
        "Источники: OpenStreetMap/Nominatim (координаты), Wikipedia (места), "
        "Open-Meteo (погода). Это реальные открытые данные без ключей."
    ]
    travel_search_on = os.getenv("TRAVEL_SEARCH", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    if travel_search_on:
        notes.append(
            "Авиабилеты, поезда и отели: сторонний сервис travel-search-ru "
            "(MIT, автор MissiaL) — кэш Aviasales, расписание Tutu.ru и "
            "предложения Level.Travel; это не поиск в реальном времени."
        )
    else:
        notes.append(
            "TRAVEL_SEARCH=0: travel-search-ru отключён; рейсы и отели "
            "берутся из Travelpayouts по токену или считаются оценкой проекта."
        )
    token = os.getenv("TRAVELPAYOUTS_TOKEN", "").strip()
    if token:
        notes.append(
            "Travelpayouts: резервный источник рейсов и отелей — цены из кэша "
            "Aviasales/Hotellook, а не поиск в реальном времени."
        )
    else:
        notes.append(
            "Токен Travelpayouts не задан: если внешний сервис недоступен, "
            "цены транспорта и проживания останутся оценкой проекта."
        )
    return notes


def get_provider(name: str | None = None) -> tuple[DataProvider, list[str]]:
    """Возвращает live-провайдер и пояснения к выбору.

    Единственный поддерживаемый режим — live. Неизвестное значение
    переменной безопасно откатывается на live с честной пометкой.
    """
    requested = name if name is not None else os.getenv("DATA_PROVIDER", "live")
    requested = requested.strip().lower() or "live"

    notes = _live_notes()
    if requested != "live":
        notes.insert(
            0,
            f"Режим «{requested}» не поддерживается: доступен только live. "
            "Используются открытые источники и оценки проекта.",
        )
    return LiveProvider(), notes
