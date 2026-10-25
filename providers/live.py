"""Провайдер «live»: открытые данные, travel-search-ru и оценки.

Схема источников:
- координаты города — Nominatim (OpenStreetMap), без ключа;
- погода — Open-Meteo, без ключа (прогноз или архив прошлого года);
- места — Wikipedia GeoSearch, без ключа;
- рейсы и отели — travel-search-ru (кэш Aviasales и Level.Travel) без ключа;
- поезда — Tutu.ru через travel-search-ru только для маршрутов внутри страны;
- Travelpayouts — резерв по токену, если travel-search недоступен;
- если данных нет — оценки проекта по расстоянию.

Провайдер никогда не подменяет оценку реальной ценой: у билетов есть
price_kind, у отелей — source и rating_source.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from datetime import date

import requests

from config import ConfigError, load_assumptions
from models import Flight, Hotel, Place, WeatherDay
from providers.base import ProviderError
from providers.estimates import (
    estimate_access_leg,
    estimate_hotel,
    estimate_osm_hotels,
    estimate_transport,
)
from providers.geo import GeoClient, GeoPoint
from providers.osm import OsmClient, TransportNode
from providers.travel_search import (
    TravelSearchClient,
    flights_from_payload,
    hotels_from_payload,
    trains_from_payload,
    tripcom_url_from_payload,
)
from providers.travelpayouts import TravelpayoutsClient

DEFAULT_TIMEOUT_SECONDS = 25
_MODE_TO_NODE = {"flight": "airport", "train": "train", "bus": "bus"}
_FLIGHT_NUMBER = re.compile(r"\b([A-Z]{2})\s*(\d{1,4})\b")


def _offer_key(flight: Flight) -> tuple:
    """Ключ одного и того же рейса из разных сервисов.

    Номер рейса надёжнее названия перевозчика: Aviasales пишет «SU 123»,
    Travelpayouts — «Авиакомпания SU 123». Если номера нет, сравниваем
    перевозчика, время и число пересадок.
    """
    airline = flight.airline.strip().upper()
    match = _FLIGHT_NUMBER.search(airline)
    carrier = f"{match.group(1)}{match.group(2)}" if match else re.sub(r"\s+", "", airline)
    return (flight.mode, carrier, flight.departure_time, flight.stops)


def _merge_offers(*groups: list[Flight]) -> tuple[list[Flight], int]:
    """Объединяет выдачу сервисов и оставляет минимальную цену совпадений."""
    merged: dict[tuple, Flight] = {}
    duplicates = 0
    for group in groups:
        for flight in group:
            key = _offer_key(flight)
            existing = merged.get(key)
            if existing is None:
                merged[key] = flight
                continue
            duplicates += 1
            if flight.price_rub < existing.price_rub:
                merged[key] = flight
    return sorted(merged.values(), key=lambda item: (item.price_rub, item.departure_time)), duplicates


def _same_country(left: GeoPoint | None, right: GeoPoint | None) -> bool:
    """Маршрут внутри одной страны: только там имеет смысл искать поезд."""
    if left is None or right is None:
        return False
    first = left.country.strip().casefold().replace("ё", "е")
    second = right.country.strip().casefold().replace("ё", "е")
    return bool(first and second and first == second)


class LiveProvider:
    """Реальные открытые данные плюс помеченные оценки там, где цен нет."""

    name = "live"

    def __init__(
        self,
        *,
        token: str | None = None,
        session: requests.Session | None = None,
        data_dir=None,
        today: date | None = None,
        geocode_min_interval: float = 1.0,
        enable_travel_search: bool | None = None,
        travel_search: TravelSearchClient | None = None,
    ) -> None:
        self._session = session or requests.Session()
        self._geo = GeoClient(self._session, min_interval=geocode_min_interval)
        self._osm = OsmClient(self._session)
        token = token if token is not None else os.getenv("TRAVELPAYOUTS_TOKEN", "")
        self._travelpayouts = TravelpayoutsClient(token, self._session) if token.strip() else None
        if enable_travel_search is None:
            flag = os.getenv("TRAVEL_SEARCH", "1").strip().lower()
            enable_travel_search = flag not in {"0", "false", "no", "off"}
        self._travel_search = (
            travel_search
            if travel_search is not None
            else (TravelSearchClient(session=self._session) if enable_travel_search else None)
        )
        self.last_hotel_search_url = ""
        self.last_notes: list[str] = []
        self._today = today
        self._fx_rates: dict[str, float] | None = None
        try:
            self._assumptions = load_assumptions(data_dir)
        except ConfigError as error:
            raise ProviderError(str(error)) from None

    def reset_notes(self) -> None:
        """Начинает новый сбор плана: старые предупреждения не переносятся."""
        self.last_notes.clear()

    def _note(self, text: str) -> None:
        if text not in self.last_notes:
            self.last_notes.append(text)

    # ------------------------------------------------------------------
    # Транспорт
    # ------------------------------------------------------------------
    def search_flights(
        self,
        origin: str,
        destination: str,
        depart_date: date | None = None,
        return_date: date | None = None,
    ) -> list[Flight]:
        if not depart_date:
            return []
        direction = "return" if return_date and return_date < depart_date else "outbound"
        origin_point = self._geo.geocode(origin)
        destination_point = self._geo.geocode(destination)

        primary = self._travel_search_flights(origin, destination, depart_date, direction)
        backup: list[Flight] = []
        if self._travelpayouts is not None:
            try:
                backup = self._travelpayouts.search_direction(
                    origin, destination, depart_date, direction
                )
            except ProviderError as error:
                self._note(f"Travelpayouts недоступен: {error}.")
                backup = []
        real, duplicates = _merge_offers(primary, backup)
        if primary and backup:
            comparison = (
                "Сравнены предложения Aviasales через travel-search-ru и Travelpayouts"
            )
            if duplicates:
                comparison += (
                    f"; совпадающих рейсов: {duplicates} — оставлена минимальная цена"
                )
            self._note(comparison + ".")
        real = real + self._travel_search_trains(
            origin,
            destination,
            depart_date,
            direction,
            origin_point,
            destination_point,
        )

        if origin_point is None or destination_point is None:
            return sorted(real, key=lambda item: (item.price_rub, item.departure_time))

        # Узлы OSM определяют, какие виды транспорта вообще возможны.
        # Если Overpass недоступен, оценка откатывается на расстояние
        # между центрами и честно помечает это в источнике.
        radii, limit = self._node_settings()
        origin_nodes = destination_nodes = None
        try:
            origin_nodes = self._osm.transport_nodes(
                origin_point,
                radii_km=radii,
                limit=limit,
            )
            destination_nodes = self._osm.transport_nodes(
                destination_point,
                radii_km=radii,
                limit=limit,
            )
        except ProviderError:
            origin_nodes = destination_nodes = None

        if origin_nodes is not None and destination_nodes is not None:
            real = self._attach_nodes(
                real,
                origin_point,
                destination_point,
                origin_nodes,
                destination_nodes,
            )

        # Планировщик запрашивает обратное направление с переставленными
        # датами, поэтому для оценки берём хронологический порядок.
        if direction == "return":
            out_date = return_date or depart_date
            ret_date = depart_date
        else:
            out_date = depart_date
            ret_date = return_date or depart_date

        estimated = estimate_transport(
            origin_point,
            destination_point,
            out_date,
            ret_date,
            self._assumptions,
            origin_nodes=origin_nodes,
            destination_nodes=destination_nodes,
        )
        # Если источник дал реальные билеты на этот вид транспорта, оценка
        # того же вида не нужна; остальные виды остаются для сравнения.
        real_modes = {flight.mode for flight in real}
        estimated = [
            flight
            for flight in estimated
            if flight.direction == direction and flight.mode not in real_modes
        ]
        return sorted(real + estimated, key=lambda item: (item.price_rub, item.departure_time))

    def _travel_search_flights(
        self,
        origin: str,
        destination: str,
        depart_date: date,
        direction: str,
    ) -> list[Flight]:
        if self._travel_search is None:
            return []
        try:
            payload = self._travel_search.search_flights(origin, destination, depart_date)
        except ProviderError as error:
            self._note(f"Aviasales через travel-search-ru недоступен: {error}.")
            return []
        flights = flights_from_payload(
            payload,
            direction=direction,
            origin=origin,
            destination=destination,
        )
        if not flights and payload.get("flights"):
            self._note(
                "Aviasales вернул рейсы без цены в рублях; они не добавлены в план."
            )
        return flights

    def _travel_search_trains(
        self,
        origin: str,
        destination: str,
        depart_date: date,
        direction: str,
        origin_point: GeoPoint | None,
        destination_point: GeoPoint | None,
    ) -> list[Flight]:
        if self._travel_search is None:
            return []
        if not _same_country(origin_point, destination_point):
            return []
        try:
            payload = self._travel_search.search_trains(origin, destination, depart_date)
        except ProviderError as error:
            self._note(f"Tutu.ru через travel-search-ru недоступен: {error}.")
            return []
        return trains_from_payload(payload, direction=direction, depart_date=depart_date)

    def _attach_nodes(
        self,
        flights: list[Flight],
        origin: GeoPoint,
        destination: GeoPoint,
        origin_nodes,
        destination_nodes,
    ) -> list[Flight]:
        """Добавляет реальным рейсам ближайшие узлы OSM и дорогу до них.

        Источники цен не сообщают аэропорт и вокзал, поэтому узел выбирается
        по координатам ближайшего объекта OpenStreetMap. Это допущение
        проекта, а не подтверждённый терминал рейса.
        """
        prepared: list[Flight] = []
        attached = False
        for flight in flights:
            if flight.departure_node and flight.arrival_node:
                prepared.append(flight)
                continue
            kind = _MODE_TO_NODE.get(flight.mode)
            if kind is None:
                prepared.append(flight)
                continue
            origin_node: TransportNode | None = origin_nodes.nearest(kind)
            destination_node: TransportNode | None = destination_nodes.nearest(kind)
            if origin_node is None or destination_node is None:
                prepared.append(flight)
                continue

            updates: dict = {
                "departure_node": origin_node.name,
                "arrival_node": destination_node.name,
                "departure_node_latitude": origin_node.latitude,
                "departure_node_longitude": origin_node.longitude,
                "arrival_node_latitude": destination_node.latitude,
                "arrival_node_longitude": destination_node.longitude,
            }
            if flight.access_to_departure is None:
                leg = estimate_access_leg(origin, origin_node, self._assumptions)
                if leg is not None:
                    updates["access_to_departure"] = leg
            if flight.access_from_arrival is None:
                leg = estimate_access_leg(destination, destination_node, self._assumptions)
                if leg is not None:
                    updates["access_from_arrival"] = leg
            prepared.append(flight.model_copy(update=updates))
            attached = True

        if attached:
            self._note(
                "Для найденных рейсов указаны ближайшие аэропорты, вокзалы и "
                "автостанции по OpenStreetMap; дорога до них — оценка проекта."
            )
        return prepared

    # ------------------------------------------------------------------
    # Отели
    # ------------------------------------------------------------------
    def search_hotels(
        self,
        city: str,
        check_in: date | None = None,
        check_out: date | None = None,
        *,
        origin_city: str | None = None,
        adults: int = 1,
    ) -> list[Hotel]:
        self.last_hotel_search_url = ""
        point = self._geo.geocode(city)
        if point is None:
            return []

        config = self._assumptions.get("hotel_search", {})
        max_options = max(1, min(10, int(config.get("max_options", 5))))

        package_hotels = self._travel_search_hotels(
            city, point, check_in, check_out, origin_city, adults
        )
        if package_hotels:
            return sorted(
                package_hotels, key=lambda item: item.price_per_night_rub
            )[:max_options]

        if self._travelpayouts is not None and check_in and check_out:
            try:
                real = self._travelpayouts.search_hotels(city, point, check_in, check_out)
            except ProviderError:
                real = []
            if real:
                return sorted(real, key=lambda item: item.price_per_night_rub)[:max_options]

        radius = max(0.0, float(config.get("radius_km", 12)))
        support = max(0.0, float(config.get("support_radius_km", 6)))
        limit = max(1, min(100, int(config.get("max_candidates", 40))))
        try:
            area = self._osm.lodging(
                point,
                radius_km=radius,
                support_radius_km=support,
                limit=limit,
            )
        except ProviderError:
            area = None

        hotels = (
            estimate_osm_hotels(city, point, area, self._assumptions)
            if area is not None
            else []
        )
        if hotels:
            return hotels

        # В OSM не нашлось ни одного объекта или сервис недоступен:
        # показываем одну честную среднюю оценку вместо выдуманных отелей.
        return [estimate_hotel(city, point, self._assumptions)]

    def _travel_search_hotels(
        self,
        city: str,
        point: GeoPoint,
        check_in: date | None,
        check_out: date | None,
        origin_city: str | None,
        adults: int,
    ) -> list[Hotel]:
        """Пакетные отели Level.Travel/Travelata через travel-search-ru."""
        if self._travel_search is None or check_in is None or check_out is None:
            return []
        if check_out <= check_in:
            return []
        if not point.country:
            self._note(
                "Страна города не определена: поиск пакетных отелей пропущен."
            )
            return []
        try:
            payload = self._travel_search.search_hotels(
                departure_city=origin_city or city,
                country=point.country,
                resort=city,
                check_in=check_in,
                check_out=check_out,
                adults=adults,
            )
        except ProviderError as error:
            self._note(f"Level.Travel через travel-search-ru недоступен: {error}.")
            return []

        self.last_hotel_search_url = tripcom_url_from_payload(payload)
        for note in (payload.get("notes") or [])[:4]:
            if isinstance(note, str) and note.strip():
                self._note(f"Сервис поиска: {note.strip()}")
        hotels = hotels_from_payload(
            payload,
            city=city,
            point=point,
            check_in=check_in,
            check_out=check_out,
        )
        if not hotels:
            self._note(
                "Level.Travel не вернул предложений отелей на эти даты; "
                "использованы другие источники."
            )
        return hotels

    # ------------------------------------------------------------------
    # Места и погода
    # ------------------------------------------------------------------
    def get_places(self, city: str) -> list[Place]:
        point = self._geo.geocode(city)
        if point is None:
            return []
        return self._geo.places(point)

    def get_weather(self, city: str, start_date: date, end_date: date) -> list[WeatherDay]:
        point = self._geo.geocode(city)
        if point is None:
            return []
        return self._geo.weather(point, start_date, end_date, self._today or date.today())

    # ------------------------------------------------------------------
    # Валюты
    # ------------------------------------------------------------------
    def _node_settings(self) -> tuple[dict[str, float], int]:
        config = self._assumptions.get("transport_nodes", {})
        radii = {
            "airport": max(0.0, float(config.get("airport_radius_km", 150))),
            "train": max(0.0, float(config.get("train_radius_km", 80))),
            "bus": max(0.0, float(config.get("bus_radius_km", 60))),
        }
        limit = max(1, min(10, int(config.get("max_per_kind", 3))))
        return radii, limit

    def convert_currency(self, amount: float, from_currency: str, to_currency: str = "RUB") -> float:
        source = from_currency.strip().upper()
        target = to_currency.strip().upper()
        if source == target:
            return round(amount, 2)

        rates = self._rates()
        if source not in rates or target not in rates or rates[target] <= 0:
            raise ProviderError(f"нет курса валюты {source if source not in rates else target}")
        return round(amount * rates[source] / rates[target], 2)

    def _rates(self) -> dict[str, float]:
        if self._fx_rates is not None:
            return self._fx_rates
        try:
            response = self._session.get(
                "https://www.cbr.ru/scripts/XML_daily.asp",
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)
            rates = {"RUB": 1.0}
            for currency in root.findall("Valute"):
                code = (currency.findtext("CharCode") or "").strip().upper()
                nominal = float((currency.findtext("Nominal") or "1").replace(",", "."))
                value = float((currency.findtext("Value") or "0").replace(",", "."))
                if code and nominal > 0 and value > 0:
                    rates[code] = value / nominal
            if len(rates) > 1:
                self._fx_rates = rates
                return rates
        except (requests.RequestException, ET.ParseError, ValueError, ZeroDivisionError):
            pass

        # Резерв: условные курсы проекта из assumptions.json.
        fallback = self._assumptions.get("fx_to_rub", {}).get("rates", {})
        self._fx_rates = {str(code).upper(): float(value) for code, value in fallback.items()}
        return self._fx_rates
