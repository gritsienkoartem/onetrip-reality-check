"""Провайдер фикстур для тестов и evaluate.py.

Данные лежат в отдельной папке fixtures/ и не выбираются в продукте.
Провайдер нужен, чтобы тесты и оценочные сценарии работали без сети,
и всегда помечает вымышленные значения is_mock=True.
"""
from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from config import PROJECT_ROOT, ConfigError, load_assumptions, load_json
from models import Flight, Hotel, Place, WeatherDay
from providers.base import ProviderError

ModelT = TypeVar("ModelT", bound=BaseModel)
FIXTURES_DIR = PROJECT_ROOT / "fixtures"


def _same_city(left: str, right: str) -> bool:
    """Сравнивает названия городов без учёта регистра и буквы «ё»."""
    def normalize(value: str) -> str:
        return value.strip().casefold().replace("ё", "е")

    return normalize(left) == normalize(right)


class FixtureProvider:
    """Читает вымышленные данные из JSON-фикстур для тестов."""

    name = "fixtures"
    last_hotel_search_url = ""

    def __init__(
        self,
        fixtures_dir: Path | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self._fixtures_dir = fixtures_dir or FIXTURES_DIR
        self._flights = self._load_list("flights.json", Flight)
        self._hotels = self._load_list("hotels.json", Hotel)
        self._places = self._load_list("places.json", Place)
        self._weather = self._load_list("weather.json", WeatherDay)
        try:
            self._assumptions = load_assumptions(data_dir)
        except ConfigError as error:
            raise ProviderError(str(error)) from None

    def _load_list(self, filename: str, model: type[ModelT]) -> list[ModelT]:
        try:
            raw = load_json(filename, self._fixtures_dir)
        except ConfigError as error:
            raise ProviderError(str(error)) from None
        if not isinstance(raw, list):
            raise ProviderError(f"файл {filename} должен содержать список")
        try:
            return [model(**item) for item in raw]
        except (ValidationError, TypeError):
            raise ProviderError(f"данные в файле {filename} не соответствуют модели") from None

    def search_flights(
        self,
        origin: str,
        destination: str,
        depart_date: date | None = None,
        return_date: date | None = None,
    ) -> list[Flight]:
        """Возвращает рейсы между городами.

        Расписание фиксированное (14–17 ноября 2026). Параметры дат
        принимаются для совместимости с провайдером live, но фикстуры
        по ним не фильтруются — иначе сценарии не найдут рейсы.
        """
        flights = [
            flight.model_copy()
            for flight in self._flights
            if _same_city(flight.origin, origin) and _same_city(flight.destination, destination)
        ]
        return sorted(flights, key=lambda item: (item.departure_time, item.id))

    def search_hotels(
        self,
        city: str,
        check_in: date | None = None,
        check_out: date | None = None,
        *,
        origin_city: str | None = None,
        adults: int = 1,
    ) -> list[Hotel]:
        """Возвращает отели города. Даты и число гостей в фикстурах не учитываются."""
        hotels = [hotel.model_copy() for hotel in self._hotels if _same_city(hotel.city, city)]
        return sorted(hotels, key=lambda item: item.id)

    def get_places(self, city: str) -> list[Place]:
        """Возвращает места города."""
        places = [place.model_copy() for place in self._places if _same_city(place.city, city)]
        return sorted(places, key=lambda item: item.id)

    def get_weather(self, city: str, start_date: date, end_date: date) -> list[WeatherDay]:
        """Возвращает погоду за период включительно.

        В фикстурах один город, поэтому city принимается, но не фильтрует:
        в модели WeatherDay нет поля города.
        """
        if start_date > end_date:
            raise ProviderError("дата начала погодного периода позже даты окончания")
        known_cities = {hotel.city for hotel in self._hotels} | {place.city for place in self._places}
        if not any(_same_city(city, known) for known in known_cities):
            return []
        days = [
            day.model_copy()
            for day in self._weather
            if start_date <= day.day <= end_date
        ]
        return sorted(days, key=lambda item: item.day)

    def convert_currency(self, amount: float, from_currency: str, to_currency: str = "RUB") -> float:
        """Пересчитывает сумму по условным курсам из assumptions.json."""
        if not math.isfinite(amount) or amount < 0:
            raise ProviderError("сумма для пересчёта должна быть неотрицательным числом")

        rates = self._assumptions.get("fx_to_rub", {}).get("rates", {})
        source = from_currency.strip().upper()
        target = to_currency.strip().upper()
        for code in (source, target):
            if code not in rates:
                raise ProviderError(f"нет курса валюты {code or '—'} в assumptions.json")
        if rates[target] <= 0:
            raise ProviderError(f"некорректный курс валюты {target}")

        return round(amount * rates[source] / rates[target], 2)
