"""Travelpayouts (Aviasales/Hotellook): реальные кэш-цены при наличии токена.

Это не поиск в реальном времени: сервис отдаёт цены из кэша поисков
Aviasales. Поэтому цена помечается как реальная (price_kind="real"),
а при отсутствии токена или данных провайдер честно переходит на
оценки проекта.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import requests

from models import Flight, Hotel, NO_DATA
from providers.base import ProviderError
from providers.geo import GeoPoint, haversine_km

AUTOCOMPLETE_URL = "https://autocomplete.travelpayouts.com/places2"
PRICES_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
HOTELS_URL = "https://engine.hotellook.com/api/v2/cache.json"

FLIGHTS_SOURCE = "Travelpayouts (кэш цен Aviasales)"
HOTELS_SOURCE = "Travelpayouts / Hotellook (кэш цен)"


def _parse_datetime(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(value), time(8, 0))
        except ValueError:
            return None
    return parsed.replace(tzinfo=None)


def _number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class TravelpayoutsClient:
    """Клиент платных по лимитам, но бесплатных по тарифу кэш-API."""

    def __init__(self, token: str, session: requests.Session | None = None, timeout: int = 25) -> None:
        self._token = token.strip()
        self._session = session or requests.Session()
        self._timeout = timeout
        self._iata_cache: dict[str, str | None] = {}

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def _get(self, url: str, params: dict):
        try:
            response = self._session.get(url, params=params, timeout=self._timeout)
        except requests.RequestException:
            raise ProviderError("не удалось подключиться к Travelpayouts") from None
        if response.status_code == 401:
            raise ProviderError("Travelpayouts отклонил токен (код 401)")
        if response.status_code != 200:
            raise ProviderError(f"Travelpayouts вернул ошибку (код {response.status_code})")
        try:
            return response.json()
        except ValueError:
            raise ProviderError("Travelpayouts вернул ответ неожиданного формата") from None

    def iata(self, city: str) -> str | None:
        """Код города/аэропорта через открытый autocomplete Travelpayouts."""
        key = city.strip().casefold().replace("ё", "е")
        if key in self._iata_cache:
            return self._iata_cache[key]
        data = self._get(
            AUTOCOMPLETE_URL,
            {"term": city, "locale": "ru", "types[]": "city"},
        )
        code = None
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                candidate = str(item.get("code", "")).strip().upper()
                if len(candidate) == 3 and candidate.isalpha():
                    code = candidate
                    break
        self._iata_cache[key] = code
        return code

    def search_flights(
        self,
        origin: str,
        destination: str,
        depart_date: date,
        return_date: date | None = None,
    ) -> list[Flight]:
        """Односторонние кэш-цены на нужные даты, туда и обратно отдельно."""
        flights = self.search_direction(origin, destination, depart_date, "outbound")
        if return_date is not None:
            flights.extend(
                self.search_direction(destination, origin, return_date, "return")
            )
        return flights

    def search_direction(
        self,
        origin: str,
        destination: str,
        depart_date: date,
        direction: str,
    ) -> list[Flight]:
        """Кэш-цены только в одном направлении: outbound или return."""
        if not self.configured:
            return []
        origin_code = self.iata(origin)
        destination_code = self.iata(destination)
        if not origin_code or not destination_code:
            return []
        return self._one_way(
            origin,
            destination,
            origin_code,
            destination_code,
            depart_date,
            direction,
        )

    def _one_way(
        self,
        origin: str,
        destination: str,
        origin_code: str,
        destination_code: str,
        depart_date: date,
        direction: str,
    ) -> list[Flight]:
        data = self._get(
            PRICES_URL,
            {
                "origin": origin_code,
                "destination": destination_code,
                "departure_at": depart_date.isoformat(),
                "one_way": "true",
                "currency": "rub",
                "limit": 30,
                "sorting": "price",
                "token": self._token,
            },
        )
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return []

        flights: list[Flight] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            price = _number(row.get("price"))
            departure = _parse_datetime(row.get("departure_at"))
            duration_minutes = _number(row.get("duration_to"))
            if price is None or price <= 0 or departure is None or not duration_minutes:
                continue
            arrival = departure + timedelta(minutes=duration_minutes)
            transfers = int(_number(row.get("transfers")) or 0)
            number = str(row.get("flight_number") or "").strip()
            airline = str(row.get("airline") or "").strip().upper()
            carrier = f"Авиакомпания {airline}" if airline else "Авиакомпания (код не указан)"
            if number:
                carrier += f" {number}"
            flights.append(
                Flight(
                    id=f"tp-{number or airline or 'flight'}-{depart_date.isoformat()}-{direction}",
                    direction=direction,
                    origin=origin,
                    destination=destination,
                    airline=carrier,
                    departure_time=departure,
                    arrival_time=arrival,
                    price_rub=price,
                    stops=transfers,
                    # Длительность пересадок кэш-API не сообщает: None честнее нуля.
                    layover_minutes=None,
                    connection_type="domestic" if transfers else "none",
                    baggage_included=False,
                    baggage_known=False,
                    baggage_fee_rub=0,
                    mode="flight",
                    price_kind="real",
                    is_mock=False,
                    source=FLIGHTS_SOURCE,
                )
            )
        return flights

    def search_hotels(
        self,
        city: str,
        point: GeoPoint,
        check_in: date,
        check_out: date,
        limit: int = 8,
    ) -> list[Hotel]:
        """Кэш-цены отелей; расположение считается по координатам отеля."""
        if not self.configured or check_out <= check_in:
            return []
        data = self._get(
            HOTELS_URL,
            {
                "location": city,
                "checkIn": check_in.isoformat(),
                "checkOut": check_out.isoformat(),
                "limit": limit,
                "currency": "rub",
                "token": self._token,
            },
        )
        rows = data.get("results") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return []

        hotels: list[Hotel] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            price = _number(row.get("priceFrom"))
            location = row.get("location") if isinstance(row.get("location"), dict) else {}
            latitude = _number(location.get("lat"))
            longitude = _number(location.get("lon"))
            if price is None or price <= 0 or latitude is None or longitude is None:
                continue
            name = str(row.get("name") or row.get("label") or "").strip()
            if not name:
                continue
            stars = _number(row.get("stars"))
            rating = _number(row.get("rating"))
            rating_source = "Travelpayouts (рейтинг)"
            if rating is None:
                rating = min(10.0, max(0.0, stars * 2)) if stars else 0.0
                rating_source = "Оценка по числу звёзд" if stars else NO_DATA
            distance = haversine_km(point.latitude, point.longitude, latitude, longitude)
            hotels.append(
                Hotel(
                    id=f"tp-hotel-{row.get('id', index)}",
                    name=name,
                    district=str(row.get("district") or city),
                    city=city,
                    price_per_night_rub=price,
                    rating=rating,
                    minutes_to_center=min(120, max(5, int(distance / 0.08))),
                    # Hotellook не сообщает расстояние до метро: не выдумываем его.
                    minutes_to_metro=0,
                    metro_known=False,
                    tourist_tax_per_night_rub=0,
                    latitude=latitude,
                    longitude=longitude,
                    tags=[],
                    rating_source=rating_source,
                    is_mock=False,
                    source=HOTELS_SOURCE,
                    distance_to_center_km=round(distance, 2),
                    price_kind="real",
                )
            )
        return hotels
