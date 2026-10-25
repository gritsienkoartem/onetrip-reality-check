"""Бесплатные открытые источники без ключей: геокодинг, погода, места.

Nominatim даёт координаты города, Open-Meteo — прогноз или архивную
погоду за прошлый год, Wikipedia GeoSearch — названия и координаты
достопримечательностей. Метаданные, которых в этих источниках нет
(рейтинг, входной билет, часы работы), не выдумываются как факты:
они помечаются как отсутствующие или как оценка проекта.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from time import monotonic, sleep

import requests

from models import NO_DATA, Place, WeatherDay
from providers.base import ProviderError

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
WIKI_URL = "https://ru.wikipedia.org/w/api.php"

USER_AGENT = "OneTripRealityCheck/0.1 (hackathon demo)"
FORECAST_HORIZON_DAYS = 15  # Open-Meteo отдаёт прогноз максимум на 16 дней вперёд

PLACES_SOURCE = "Wikipedia GeoSearch (названия и координаты)"
FORECAST_SOURCE = "Open-Meteo (прогноз, реальные данные)"
ARCHIVE_SOURCE = "Open-Meteo (архив за прошлый год: типичная погода, не прогноз)"

_WEATHER_CODES = {
    range(0, 1): "ясно",
    range(1, 4): "переменная облачность",
    range(45, 49): "туман",
    range(51, 58): "морось",
    range(61, 68): "дождь",
    range(71, 78): "снег",
    range(80, 83): "ливни",
    range(85, 87): "снегопад",
    range(95, 100): "гроза",
}

# Слова в названии страницы → категории для ранжирования.
_PLACE_RULES: list[tuple[tuple[str, ...], list[str], bool, int]] = [
    (("музей", "музея", "музей-"), ["музей"], True, 90),
    (("театр",), ["театр"], True, 120),
    (("филармони",), ["музыка"], True, 120),
    (("галере", "выставочн"), ["галерея", "искусство"], True, 90),
    (("цирк", "планетари", "зоопарк"), ["развлечения"], True, 120),
    (("парк", "сад", "сквер", "ботаническ"), ["парк"], False, 60),
    (("памятник", "монумент", "стела", "бюст"), ["памятник"], False, 30),
    (("собор", "церков", "храм", "монастыр", "мечет", "костел", "синагог"), ["архитектура"], True, 60),
    (("усадьб", "дворец", "дом ", "башн"), ["архитектура", "история"], True, 60),
    (("набережн",), ["прогулка"], False, 60),
    (("площад",), ["площадь"], False, 30),
]

# Служебные и транспортные объекты, которые не стоит ставить в маршрут.
_PLACE_STOPWORDS = (
    "улица", "проспект", "переулок", "шоссе", "проезд", "правительство",
    "администраци", "прокуратур", "суд ", "банк", "вокзал", "станция",
    "депо", "завод", "больниц", "поликлиник", "школ", "лицей", "техникум",
)


# Типы OSM, которые считаются населёнными пунктами. Только среди них
# ищем неоднозначность: озеро «Байкал» не нужно сравнивать с деревней.
SETTLEMENT_TYPES = frozenset({
    "city", "town", "village", "hamlet", "municipality", "suburb",
    "quarter", "borough", "isolated_dwelling",
})

# Административные единицы с тем же названием не должны подменять город:
# Nominatim ставит регион «Москва» выше города «Москва».
_ADMIN_TYPES = frozenset({
    "state", "region", "province", "county", "administrative", "district",
})

_FEATURE_LABELS = {
    "city": "город",
    "town": "город",
    "village": "село",
    "hamlet": "деревня",
    "municipality": "муниципалитет",
    "suburb": "район города",
    "quarter": "квартал",
    "borough": "район",
    "isolated_dwelling": "хутор",
    "administrative": "административная единица",
    "state": "регион",
    "region": "регион",
    "county": "район",
    "city_district": "район города",
    "peak": "гора",
    "river": "река",
    "stream": "ручей",
    "forest": "лес",
    "park": "парк",
    "road": "дорога",
    "trunk": "дорога",
    "residential": "дорога",
    "lake": "озеро",
    "water": "водоём",
    "sea": "море",
    "bay": "залив",
    "island": "остров",
    "attraction": "достопримечательность",
    "museum": "музей",
    "monument": "памятник",
    "memorial": "мемориал",
    "hotel": "жильё",
    "hostel": "жильё",
    "guest_house": "жильё",
    "aerodrome": "аэропорт",
    "station": "станция",
}

# Порядок полей адреса, из которых берём короткое название места.
_NAME_KEYS = (
    "city", "town", "village", "hamlet", "municipality", "suburb",
    "county", "state", "region", "lake", "sea", "water", "bay",
    "island", "attraction", "monument", "aerodrome", "railway",
)


def _normalize(value: str) -> str:
    return value.strip().casefold().replace("ё", "е")


@dataclass(frozen=True)
class GeoPoint:
    """Найденное место с координатами и типом объекта OSM.

    Первые четыре поля остаются обязательными: старые вызовы и тесты
    создают точку только по названию и координатам.
    """

    city: str
    display_name: str
    latitude: float
    longitude: float
    feature_type: str = "city"
    country: str = ""
    region: str = ""
    osm_type: str = ""
    osm_id: int | None = None
    importance: float = 0.0

    @property
    def is_settlement(self) -> bool:
        """Населённый пункт, а не озеро, гора или достопримечательность."""
        return self.feature_type in SETTLEMENT_TYPES

    @property
    def feature_label(self) -> str:
        return _FEATURE_LABELS.get(self.feature_type, "место")

    @property
    def label_ru(self) -> str:
        """Короткая подпись для выбора: «Суздаль — Владимирская область, Россия»."""
        kind = ""
        if not self.is_settlement and _normalize(self.feature_label) not in _normalize(self.city):
            kind = f" ({self.feature_label})"
        label = f"{self.city}{kind}"
        if self.region and _normalize(self.region) not in _normalize(label):
            label += f" — {self.region}"
        if self.country and _normalize(self.country) not in _normalize(label):
            label += f", {self.country}"
        return label


def _region_key(point: GeoPoint) -> tuple[str, str]:
    return _normalize(point.country), _normalize(point.region)


def _name_tokens(value: str) -> set[str]:
    return {token for token in re.split(r"[^\w]+", _normalize(value)) if token}


def _name_matches_query(point: GeoPoint, query: str) -> bool:
    """Совпадает ли название места с запросом, а не просто стоит первым.

    Nominatim может поставить выше объект со старым или альтернативным
    названием (например, Уссурийск по запросу «Никольское»). Такой
    кандидат не должен молча становиться выбранным местом.
    """
    expected = _normalize(query)
    name = _normalize(point.city)
    if (
        name == expected
        or name.startswith(expected + " ")
        or name.startswith(expected + "-")
        or expected.startswith(name + " ")
    ):
        return True
    return expected in _name_tokens(point.city)


@dataclass(frozen=True)
class PlaceResolution:
    """Результат поиска места: кандидаты и признак неоднозначности."""

    query: str
    candidates: list[GeoPoint]

    @property
    def selected(self) -> GeoPoint | None:
        """Первое место, название которого действительно похоже на запрос.

        Если Nominatim поставил выше регион («Москва»), а ниже есть город
        с тем же названием, выбирается город. Озеро или достопримечательность
        остаются выбранными, если это первое совпадение.
        """
        relevant = [
            point for point in self.candidates if _name_matches_query(point, self.query)
        ]
        if not relevant:
            return self.candidates[0] if self.candidates else None
        first = relevant[0]
        if first.feature_type in _ADMIN_TYPES:
            settlement = next((point for point in relevant if point.is_settlement), None)
            if settlement is not None:
                return settlement
        return first

    @property
    def matching_settlements(self) -> list[GeoPoint]:
        """Населённые пункты, название которых действительно похоже на запрос."""
        return [
            point
            for point in self.candidates
            if point.is_settlement and _name_matches_query(point, self.query)
        ]

    @property
    def ambiguous(self) -> bool:
        """Один и тот же запрос указывает на разные города или страны."""
        selected = self.selected
        if selected is None or not selected.is_settlement:
            return False
        matches = self.matching_settlements
        if len(matches) < 2:
            return False
        selected_key = _region_key(selected)
        return any(
            _region_key(point) != selected_key
            for point in matches
            if point is not selected
        )

    @property
    def question(self) -> str:
        if not self.ambiguous:
            return ""
        return (
            f"Название «{self.query}» подходит к нескольким местам. "
            "Уточните, какое из них нужно:"
        )


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние между точками по большому кругу, км."""
    from math import asin, cos, radians, sin, sqrt

    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(a))


def _condition(code: int | None) -> str:
    if code is None:
        return NO_DATA
    for codes, label in _WEATHER_CODES.items():
        if code in codes:
            return label
    return "неизвестные условия"


class GeoClient:
    """Клиент открытых API с кэшем геокодинга.

    Все сетевые ошибки превращаются в ProviderError: планировщик уже
    умеет безопасно продолжать без данных.
    """

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout: int = 25,
        min_interval: float = 1.0,
    ) -> None:
        self._session = session or requests.Session()
        self._timeout = timeout
        self._geocache: dict[str, list[GeoPoint]] = {}
        # Публичный Nominatim просит не чаще одного запроса в секунду.
        self._min_interval = max(0.0, min_interval)
        self._last_nominatim = 0.0

    def _respect_nominatim_policy(self) -> None:
        if self._min_interval <= 0:
            return
        elapsed = monotonic() - self._last_nominatim
        if elapsed < self._min_interval:
            sleep(self._min_interval - elapsed)
        self._last_nominatim = monotonic()

    def _get(self, url: str, params: dict):
        try:
            response = self._session.get(
                url,
                params=params,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=self._timeout,
            )
        except requests.RequestException:
            raise ProviderError("не удалось подключиться к открытому источнику данных") from None
        if response.status_code != 200:
            raise ProviderError(f"открытый источник вернул ошибку (код {response.status_code})")
        try:
            return response.json()
        except ValueError:
            raise ProviderError("открытый источник вернул ответ неожиданного формата") from None

    def geocode_candidates(self, query: str, limit: int = 5) -> list[GeoPoint]:
        """Ищет место без навязанной страны и возвращает кандидатов.

        Nominatim отдаёт тип объекта (city, town, village, lake и т. п.),
        регион и страну — по ним планировщик понимает, что нашёл.
        """
        text = query.strip()
        if not text:
            return []
        key = _normalize(text)
        if key in self._geocache:
            return list(self._geocache[key])

        self._respect_nominatim_policy()
        data = self._get(
            NOMINATIM_URL,
            {
                "q": text,
                "format": "jsonv2",
                "limit": max(1, min(limit, 10)),
                "addressdetails": 1,
                "accept-language": "ru",
                "dedupe": 1,
            },
        )
        if not isinstance(data, list):
            data = []
        points = [
            point
            for point in (_point_from_nominatim(item, text) for item in data)
            if point is not None
        ]
        self._geocache[key] = points
        return list(points)

    def geocode(self, city: str) -> GeoPoint | None:
        candidates = self.geocode_candidates(city)
        return candidates[0] if candidates else None

    def resolve(self, query: str) -> PlaceResolution:
        """Возвращает кандидатов и признак неоднозначного названия."""
        return PlaceResolution(query=query, candidates=self.geocode_candidates(query))

    def weather(self, point: GeoPoint, start: date, end: date, today: date) -> list[WeatherDay]:
        if start > end:
            raise ProviderError("дата начала погодного периода позже даты окончания")

        forecast_last = today + timedelta(days=FORECAST_HORIZON_DAYS)
        if today <= start and end <= forecast_last:
            return self._forecast(point, start, end)
        return self._archive(point, start, end)

    def _forecast(self, point: GeoPoint, start: date, end: date) -> list[WeatherDay]:
        data = self._get(
            FORECAST_URL,
            {
                "latitude": point.latitude,
                "longitude": point.longitude,
                "daily": "weather_code,temperature_2m_max,precipitation_probability_max",
                "timezone": "auto",
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
        )
        daily = data.get("daily") or {}
        times = daily.get("time") or []
        days: list[WeatherDay] = []
        for index, raw_day in enumerate(times):
            try:
                day = date.fromisoformat(raw_day)
                code = _as_int(_at(daily.get("weather_code"), index))
                temperature = _as_float(_at(daily.get("temperature_2m_max"), index))
                probability = _as_int(_at(daily.get("precipitation_probability_max"), index))
            except (TypeError, ValueError):
                continue
            if temperature is None:
                continue
            days.append(
                WeatherDay(
                    day=day,
                    condition=_condition(code),
                    temperature_c=temperature,
                    precipitation_probability=probability,
                    is_mock=False,
                    source=FORECAST_SOURCE,
                )
            )
        return days

    def _archive(self, point: GeoPoint, start: date, end: date) -> list[WeatherDay]:
        archive_start = _last_year(start)
        archive_end = _last_year(end)
        data = self._get(
            ARCHIVE_URL,
            {
                "latitude": point.latitude,
                "longitude": point.longitude,
                "daily": "weather_code,temperature_2m_max",
                "timezone": "auto",
                "start_date": archive_start.isoformat(),
                "end_date": archive_end.isoformat(),
            },
        )
        daily = data.get("daily") or {}
        times = daily.get("time") or []
        days: list[WeatherDay] = []
        for index, raw_day in enumerate(times):
            try:
                archived = date.fromisoformat(raw_day)
                day = _same_day_next_year(archived)
                code = _as_int(_at(daily.get("weather_code"), index))
                temperature = _as_float(_at(daily.get("temperature_2m_max"), index))
            except (TypeError, ValueError):
                continue
            if temperature is None:
                continue
            days.append(
                WeatherDay(
                    day=day,
                    condition=_condition(code),
                    temperature_c=temperature,
                    precipitation_probability=None,
                    is_mock=False,
                    source=ARCHIVE_SOURCE,
                )
            )
        return days

    def places(self, point: GeoPoint, limit: int = 12) -> list[Place]:
        data = self._get(
            WIKI_URL,
            {
                "action": "query",
                "list": "geosearch",
                "gscoord": f"{point.latitude}|{point.longitude}",
                "gsradius": 10000,
                "gslimit": 50,
                "format": "json",
            },
        )
        pages = ((data.get("query") or {}).get("geosearch")) or []
        places: list[Place] = []
        seen: set[str] = set()
        for page in pages:
            title = str(page.get("title", "")).strip()
            key = title.casefold()
            if not title or key in seen:
                continue
            rule = _match_rule(title)
            if rule is None:
                continue
            seen.add(key)
            categories, indoor, visit_minutes = rule
            try:
                latitude = float(page["lat"])
                longitude = float(page["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            places.append(
                Place(
                    id=f"wiki-{page.get('pageid', key)}",
                    name=title,
                    city=point.city,
                    categories=categories,
                    latitude=latitude,
                    longitude=longitude,
                    indoor=indoor,
                    visit_minutes=visit_minutes,
                    entrance_fee_rub=0,
                    rating=5.0,
                    crowd_level="medium",
                    opening_hours=None,
                    hours_source=NO_DATA,
                    rating_source=NO_DATA,
                    is_mock=False,
                    source=PLACES_SOURCE,
                )
            )
            if len(places) >= limit:
                break
        return places


def _point_from_nominatim(item, query: str) -> GeoPoint | None:
    """Превращает ответ Nominatim в GeoPoint, не выдумывая недостающее."""
    if not isinstance(item, dict):
        return None
    try:
        latitude = float(item["lat"])
        longitude = float(item["lon"])
    except (KeyError, TypeError, ValueError):
        return None

    address = item.get("address")
    address = address if isinstance(address, dict) else {}
    name = str(item.get("name") or "").strip()
    if not name:
        for key in _NAME_KEYS:
            value = address.get(key)
            if value:
                name = str(value).strip()
                break
    if not name:
        name = str(item.get("display_name") or query).split(",")[0].strip() or query

    feature_type = str(item.get("addresstype") or item.get("type") or "").strip().casefold()
    raw_id = item.get("osm_id")
    try:
        osm_id = int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        osm_id = None
    try:
        importance = float(item.get("importance") or 0.0)
    except (TypeError, ValueError):
        importance = 0.0

    region = address.get("state") or address.get("region") or address.get("county") or ""
    return GeoPoint(
        city=name,
        display_name=str(item.get("display_name") or query),
        latitude=latitude,
        longitude=longitude,
        feature_type=feature_type or "place",
        country=str(address.get("country") or "").strip(),
        region=str(region).strip(),
        osm_type=str(item.get("osm_type") or "").strip(),
        osm_id=osm_id,
        importance=importance,
    )


def _match_rule(title: str) -> tuple[list[str], bool, int] | None:
    text = title.casefold().replace("ё", "е")
    if any(word in text for word in _PLACE_STOPWORDS):
        return None
    for words, categories, indoor, minutes in _PLACE_RULES:
        if any(word in text for word in words):
            return categories, indoor, minutes
    return None


def _at(values, index: int):
    return values[index] if isinstance(values, list) and index < len(values) else None


def _as_int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _last_year(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:  # 29 февраля
        return day.replace(year=day.year - 1, day=28)


def _same_day_next_year(day: date) -> date:
    try:
        return day.replace(year=day.year + 1)
    except ValueError:
        return day.replace(year=day.year + 1, day=28)
