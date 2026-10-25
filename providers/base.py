"""Контракт провайдера данных.

Провайдер отвечает только за получение фактов: рейсов, отелей, мест,
погоды и курсов валют. Он не строит маршрут и не проверяет план.
"""
from __future__ import annotations

from datetime import date
from typing import Protocol

from models import Flight, Hotel, Place, WeatherDay


class ProviderError(Exception):
    """Провайдер не смог получить данные. Текст ошибки — на русском."""


class DataProvider(Protocol):
    """Интерфейс источника данных (мок или реальные API)."""

    name: str

    def search_flights(
        self,
        origin: str,
        destination: str,
        depart_date: date | None = None,
        return_date: date | None = None,
    ) -> list[Flight]:
        """Рейсы туда и обратно. Даты могут быть не указаны на этапе подбора."""
        ...

    def search_hotels(
        self,
        city: str,
        check_in: date | None = None,
        check_out: date | None = None,
        *,
        origin_city: str | None = None,
        adults: int = 1,
    ) -> list[Hotel]:
        """Варианты проживания в городе.

        origin_city и adults нужны сервисам, которые ищут пакетные
        предложения: город вылета влияет на выдачу, а число взрослых —
        на цену. Мок-провайдер эти параметры принимает и игнорирует.
        """
        ...

    def get_places(self, city: str) -> list[Place]:
        """Достопримечательности, кофейни, парки и другие места."""
        ...

    def get_weather(self, city: str, start_date: date, end_date: date) -> list[WeatherDay]:
        """Прогноз погоды по дням поездки."""
        ...

    def convert_currency(self, amount: float, from_currency: str, to_currency: str = "RUB") -> float:
        """Пересчитывает сумму между валютами по курсам проекта."""
        ...
