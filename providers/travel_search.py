"""Клиент открытого сервиса travel-search-ru (MCP) и маппинг его ответов.

Сервис — сторонний проект MissiaL (MIT): https://github.com/MissiaL/travel-search-ru.
Он объединяет кэш Aviasales, расписание Tutu.ru и предложения Level.Travel.
OneTrip использует его только как источник данных: поиск ничего не бронирует,
а все цены помечаются как кэшированные, а не подтверждённые.

Клиент говорит по MCP Streamable HTTP: initialize, notifications/initialized,
tools/call. Ответ приходит либо JSON, либо SSE-потоком. Сетевые ошибки
превращаются в ProviderError с русским текстом, без URL и секретов.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from time import monotonic, sleep
from typing import Any
from urllib.parse import urlparse

import requests

from models import NO_DATA, Flight, Hotel
from providers.base import ProviderError
from providers.geo import GeoPoint

MCP_ENDPOINT = "https://mcp.botclaw.ru/travel"
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
CLIENT_NAME = "OneTrip Reality Check"
CLIENT_VERSION = "0.1"
USER_AGENT = "OneTripRealityCheck/0.1 (hackathon demo)"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MIN_INTERVAL_SECONDS = 0.5
MAX_CACHE_ENTRIES = 64

AVIASALES_SOURCE = (
    "Aviasales через travel-search-ru (кэш цен до 48 ч; время прилёта рассчитано)"
)
TUTU_SOURCE = (
    "Tutu.ru через travel-search-ru (кэш расписания; цена ориентировочная)"
)
LEVELTRAVEL_SOURCE = "Level.Travel через travel-search-ru (цена за всё проживание)"
TRAVELATA_SOURCE = "Travelata через travel-search-ru (цена за всё проживание)"

_MEAL_LABELS = {
    "RO": "без питания",
    "BB": "завтраки",
    "HB": "полупансион",
    "FB": "полный пансион",
    "AI": "всё включено",
    "UAI": "ультра всё включено",
}

_TOOL_ERROR_PREFIX = re.compile(r"^Error executing tool [\w.-]+:\s*")
_URL_IN_TEXT = re.compile(r"(?i)\b(?:https?|ftp)://\S+|\bwww\.\S+")
_MAX_ERROR_CHARS = 300
_WALK_METERS_PER_MINUTE = 80  # 4.8 км/ч — допущение проекта


def _normalize_text(value: str) -> str:
    return value.strip().casefold().replace("ё", "е")


def _number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _safe_url(value) -> str:
    """Пропускаем только https-ссылки без логина и пароля."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""
    return text


def _parse_moment(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Время в выдаче местное; убираем смещение, чтобы не смешивать
    # aware- и naive-значения в остальных моделях проекта.
    return parsed.replace(tzinfo=None)


def _parse_clock(value) -> time | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        return None


def _slug(value: str) -> str:
    text = re.sub(r"[^\w]+", "-", _normalize_text(value), flags=re.UNICODE).strip("-")
    return text[:40] or "offer"


def _sanitize_error(text: str) -> str:
    cleaned = _TOOL_ERROR_PREFIX.sub("", text.strip())
    cleaned = " ".join(_URL_IN_TEXT.sub("", cleaned).split())
    return cleaned[:_MAX_ERROR_CHARS] or "запрос отклонён"


# --------------------------------------------------------------------------
# Клиент MCP
# --------------------------------------------------------------------------
class TravelSearchClient:
    """Минимальный MCP-клиент без сторонних библиотек и без CLI-обёртки."""

    def __init__(
        self,
        *,
        endpoint: str = MCP_ENDPOINT,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        session: requests.Session | None = None,
        min_interval: float = DEFAULT_MIN_INTERVAL_SECONDS,
    ) -> None:
        self._endpoint = endpoint
        self._timeout = max(5, int(timeout))
        self._session = session or requests.Session()
        self._min_interval = max(0.0, float(min_interval))
        self._last_call = 0.0
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        self._ready = False
        self._next_id = 1
        self._cache: dict[str, Any] = {}

    # -- служебное -----------------------------------------------------
    def _alloc_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    def _respect_interval(self) -> None:
        if self._min_interval <= 0:
            return
        elapsed = monotonic() - self._last_call
        if elapsed < self._min_interval:
            sleep(self._min_interval - elapsed)
        self._last_call = monotonic()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": USER_AGENT,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol_version:
            headers["MCP-Protocol-Version"] = self._protocol_version
        return headers

    def _post(self, payload: dict, *, expected_id: int | None, notification: bool = False):
        self._respect_interval()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            response = self._session.post(
                self._endpoint,
                data=body,
                headers=self._headers(),
                timeout=self._timeout,
                allow_redirects=False,
            )
        except requests.Timeout:
            raise ProviderError("сервис поиска не ответил вовремя") from None
        except requests.RequestException:
            raise ProviderError("не удалось подключиться к сервису поиска") from None

        status = int(getattr(response, "status_code", 0))
        if status == 429:
            raise ProviderError("сервис поиска перегружен: слишком много запросов")
        if 300 <= status < 400:
            raise ProviderError("сервис поиска перенаправил запрос; переход отключён")
        if status < 200 or status >= 300:
            raise ProviderError(f"сервис поиска вернул ошибку (код {status})")

        headers = getattr(response, "headers", {}) or {}
        session_id = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        if session_id:
            self._session_id = str(session_id)

        if notification:
            return None

        raw = getattr(response, "content", None)
        if isinstance(raw, (bytes, bytearray)) and raw:
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ProviderError("сервис поиска вернул слишком большой ответ")
            # Сервис отдаёт UTF-8 без charset в Content-Type; requests в этом
            # случае подставляет ISO-8859-1 и ломает кириллицу в SSE.
            try:
                text = bytes(raw).decode("utf-8")
            except UnicodeDecodeError:
                raise ProviderError(
                    "сервис поиска вернул ответ неожиданного формата"
                ) from None
        else:
            text = getattr(response, "text", None)
            if text is None:
                raise ProviderError("сервис поиска вернул пустой ответ")
            if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
                raise ProviderError("сервис поиска вернул слишком большой ответ")
        if not text.strip():
            raise ProviderError("сервис поиска вернул пустой ответ")

        return self._parse_text(text, expected_id)

    def _parse_text(self, text: str, expected_id: int | None):
        stripped = text.lstrip()
        if (
            stripped.startswith("data:")
            or stripped.startswith("event:")
            or "\ndata:" in text
        ):
            return self._parse_sse(text, expected_id)
        try:
            payload = json.loads(text)
        except ValueError:
            raise ProviderError("сервис поиска вернул ответ неожиданного формата") from None
        return self._select(payload, expected_id)

    def _parse_sse(self, text: str, expected_id: int | None):
        messages: list[Any] = []
        data_lines: list[str] = []

        def flush() -> None:
            if not data_lines:
                return
            blob = "\n".join(data_lines)
            data_lines.clear()
            try:
                messages.append(json.loads(blob))
            except ValueError:
                raise ProviderError(
                    "сервис поиска вернул ответ неожиданного формата"
                ) from None

        # Только настоящие переводы строк: splitlines() в Python считает
        # границами и служебные символы вроде U+0085, которые встречаются
        # внутри UTF-8, ошибочно прочитанного как Latin-1.
        for line in re.split(r"\r\n|\r|\n", text):
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif line.strip() == "":
                flush()
        flush()

        if not messages:
            raise ProviderError("сервис поиска вернул пустой поток событий")
        if expected_id is None:
            return messages[-1]
        for message in messages:
            if isinstance(message, dict) and message.get("id") == expected_id:
                return self._select(message, expected_id)
        raise ProviderError("сервис поиска не прислал ответ на запрос")

    def _select(self, payload, expected_id: int | None):
        if not isinstance(payload, dict):
            raise ProviderError("сервис поиска вернул ответ неожиданного формата")
        if expected_id is not None:
            if payload.get("id") != expected_id:
                raise ProviderError("сервис поиска вернул ответ на другой запрос")
        if payload.get("error") is not None:
            raise ProviderError("сервис поиска вернул ошибку протокола")
        if expected_id is not None and "result" not in payload:
            raise ProviderError("сервис поиска вернул неполный ответ")
        return payload.get("result")

    def _ensure_ready(self) -> None:
        if self._ready:
            return
        request_id = self._alloc_id()
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                },
            },
            expected_id=request_id,
        )
        if not isinstance(result, dict):
            raise ProviderError("сервис поиска не принял подключение")
        negotiated = result.get("protocolVersion")
        if negotiated not in SUPPORTED_PROTOCOL_VERSIONS:
            raise ProviderError("сервис поиска использует неподдерживаемую версию протокола")
        self._protocol_version = str(negotiated)
        self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            expected_id=None,
            notification=True,
        )
        self._ready = True

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Вызывает MCP-инструмент и возвращает нормализованный объект."""
        if not isinstance(arguments, dict):
            raise ProviderError("параметры запроса к сервису поиска должны быть объектом")
        key = f"{name}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"
        if key in self._cache:
            return self._cache[key]

        self._ensure_ready()
        request_id = self._alloc_id()
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            expected_id=request_id,
        )
        normalized = _normalize_tool_result(result)
        if len(self._cache) >= MAX_CACHE_ENTRIES:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = normalized
        return normalized

    # -- предметные вызовы ---------------------------------------------
    def search_flights(
        self,
        origin: str,
        destination: str,
        depart_date: date,
        *,
        adults: int = 1,
    ) -> dict:
        return self.call_tool(
            "search_flights",
            {
                "origin": origin,
                "destination": destination,
                "depart_date": depart_date.isoformat(),
                "adults": max(1, int(adults)),
            },
        )

    def search_trains(
        self,
        origin: str,
        destination: str,
        depart_date: date,
        *,
        limit: int = 5,
    ) -> dict:
        return self.call_tool(
            "search_train_tickets",
            {
                "origin": origin,
                "destination": destination,
                "depart_date": depart_date.isoformat(),
                "sort": "price",
                "limit": max(1, min(20, int(limit))),
            },
        )

    def search_hotels(
        self,
        *,
        departure_city: str,
        country: str,
        resort: str,
        check_in: date,
        check_out: date,
        adults: int = 1,
        limit: int = 10,
    ) -> dict:
        nights = (check_out - check_in).days
        if nights < 1:
            raise ProviderError("дата выезда должна быть позже даты заезда")
        if not country.strip():
            raise ProviderError("не удалось определить страну для поиска отелей")
        return self.call_tool(
            "search_hotels",
            {
                "departure_city": departure_city,
                "country": country,
                "resort": resort,
                "date_from": check_in.isoformat(),
                "date_to": check_in.isoformat(),
                "nights_min": nights,
                "nights_max": nights,
                "adults": max(1, int(adults)),
                "limit": max(1, min(30, int(limit))),
            },
        )


def _normalize_tool_result(result) -> dict:
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise ProviderError("сервис поиска вернул неожиданный результат")

    if result.get("isError") is True:
        message = "запрос отклонён"
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        message = _sanitize_error(text)
                        break
        raise ProviderError(f"сервис поиска: {message}")

    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            text = text.strip()
            if text.startswith("{") or text.startswith("["):
                try:
                    parsed = json.loads(text)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
    return result


# --------------------------------------------------------------------------
# Маппинг ответов в модели OneTrip
# --------------------------------------------------------------------------
def flights_from_payload(
    payload: dict,
    *,
    direction: str,
    origin: str,
    destination: str,
) -> list[Flight]:
    """Рейсы Aviasales: цена за взрослого, время прилёта из длительности."""
    rows = payload.get("flights") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    booking_url = _safe_url(payload.get("url"))
    flights: list[Flight] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        departure = _parse_moment(row.get("departure_at"))
        duration = _integer(row.get("duration_to_minutes") or row.get("duration_minutes"))
        price = _number(row.get("price_per_adult"))
        currency = str(row.get("currency") or "RUB").strip().upper()
        if departure is None or not duration or duration <= 0 or price is None or price <= 0:
            continue
        if currency != "RUB":
            # В проекте все суммы в рублях; пересчёт чужой валюты без курса
            # источника дал бы ложную стоимость.
            continue
        airline = str(row.get("airline") or "").strip().upper()
        number = str(row.get("flight_number") or "").strip()
        carrier = " ".join(part for part in (airline, number) if part) or "Авиакомпания не указана"
        flights.append(
            Flight(
                id=f"aviasales-{direction}-{departure.date().isoformat()}-{index}-{_slug(carrier)}",
                direction=direction,
                origin=origin,
                destination=destination,
                airline=carrier,
                departure_time=departure,
                arrival_time=departure + timedelta(minutes=duration),
                price_rub=float(price),
                stops=max(0, _integer(row.get("transfers")) or 0),
                layover_minutes=None,
                baggage_included=False,
                baggage_known=False,
                baggage_fee_rub=0,
                mode="flight",
                price_kind="real",
                is_mock=False,
                source=AVIASALES_SOURCE,
                booking_url=booking_url,
            )
        )
    return flights


def trains_from_payload(
    payload: dict,
    *,
    direction: str,
    depart_date: date,
) -> list[Flight]:
    """Поезда Tutu.ru: кэш расписания и ориентировочная цена, не наличие мест."""
    rows = payload.get("trains") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    origin_info = payload.get("origin") if isinstance(payload.get("origin"), dict) else {}
    destination_info = (
        payload.get("destination") if isinstance(payload.get("destination"), dict) else {}
    )
    origin = str(origin_info.get("name") or "").strip()
    destination = str(destination_info.get("name") or "").strip()
    if not origin or not destination:
        return []
    booking_url = _safe_url(payload.get("url"))
    trains: list[Flight] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        departure_clock = _parse_clock(row.get("departure_time"))
        duration = _integer(row.get("duration_minutes"))
        price = _number(row.get("min_price_rub"))
        if departure_clock is None or not duration or duration <= 0 or price is None or price <= 0:
            continue
        departure = datetime.combine(depart_date, departure_clock)
        number = str(row.get("train_number") or "").strip()
        categories = row.get("categories") if isinstance(row.get("categories"), list) else []
        cheapest = next(
            (
                item
                for item in categories
                if isinstance(item, dict) and _number(item.get("price_rub")) == price
            ),
            None,
        )
        category_name = str((cheapest or {}).get("name") or "").strip()
        carrier = "Поезд " + (number or "без номера")
        if category_name:
            carrier += f" · {category_name}"
        trains.append(
            Flight(
                id=f"tutu-{direction}-{depart_date.isoformat()}-{index}-{_slug(number)}",
                direction=direction,
                origin=origin,
                destination=destination,
                airline=carrier,
                departure_time=departure,
                arrival_time=departure + timedelta(minutes=duration),
                price_rub=float(price),
                stops=0,
                layover_minutes=None,
                baggage_included=False,
                baggage_known=False,
                baggage_fee_rub=0,
                mode="train",
                price_kind="estimate",
                is_mock=False,
                source=TUTU_SOURCE,
                booking_url=_safe_url(row.get("url")) or booking_url,
            )
        )
    trains.sort(key=lambda item: (item.price_rub, item.departure_time))
    return trains


def hotels_from_payload(
    payload: dict,
    *,
    city: str,
    point: GeoPoint,
    check_in: date,
    check_out: date,
) -> list[Hotel]:
    """Отели Level.Travel/Travelata: цена за всю поездку, без выдуманных расстояний."""
    rows = payload.get("offers") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    stay_nights = (check_out - check_in).days
    if stay_nights < 1:
        return []

    hotels: list[Hotel] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        name = str(row.get("hotel") or "").strip()
        total_price = _number(row.get("price"))
        currency = str(row.get("currency") or "RUB").strip().upper()
        nights = max(1, _integer(row.get("nights")) or stay_nights)
        if not name or total_price is None or total_price <= 0 or currency != "RUB":
            continue

        rating = _number(row.get("rating"))
        rating_source = NO_DATA
        if rating is not None:
            rating = min(10.0, max(0.0, rating))
            source_name = {
                "leveltravel": "Level.Travel",
                "travelata": "Travelata",
            }.get(str(row.get("source") or "").strip().lower(), "Сервис поиска")
            rating_source = f"{source_name} (рейтинг по шкале 0–10)"

        center_m = _number(row.get("center_distance_m"))
        center_km = round(center_m / 1000, 2) if center_m is not None else None
        minutes_to_center = (
            min(180, max(1, int(center_m / _WALK_METERS_PER_MINUTE)))
            if center_m is not None
            else 0
        )

        meal_code = str(row.get("meal") or "").strip().upper()
        meal_label = _MEAL_LABELS.get(meal_code, "")
        tags: list[str] = []
        stars = _integer(row.get("stars"))
        if stars:
            tags.append(f"{stars}★")
        if meal_label:
            tags.append(meal_label)

        provider = str(row.get("source") or "").strip().lower()
        source = LEVELTRAVEL_SOURCE if provider == "leveltravel" else TRAVELATA_SOURCE
        hotels.append(
            Hotel(
                id=f"ts-hotel-{index}-{_slug(name)}",
                name=name,
                district=str(row.get("resort") or row.get("country") or city),
                city=city,
                price_per_night_rub=round(total_price / nights, 2),
                rating=rating or 0.0,
                minutes_to_center=minutes_to_center,
                minutes_to_metro=0,
                tourist_tax_per_night_rub=0,
                latitude=point.latitude,
                longitude=point.longitude,
                tags=tags,
                rating_source=rating_source,
                is_mock=False,
                source=source,
                distance_to_center_km=center_km,
                center_known=center_m is not None,
                metro_known=False,
                price_kind="real",
                stay_total_rub=round(total_price, 2),
                booking_url=_safe_url(row.get("url")),
                meal_plan=meal_label,
            )
        )
    hotels.sort(key=lambda item: (item.price_per_night_rub, -item.rating, item.id))
    return hotels


def tripcom_url_from_payload(payload: dict) -> str:
    """Ссылка Trip.com принимается только с доверенного домена."""
    if not isinstance(payload, dict):
        return ""
    url = _safe_url(payload.get("tripcom_url"))
    if not url:
        return ""
    host = (urlparse(url).hostname or "").lower()
    if host == "trip.com" or host.endswith(".trip.com"):
        return url
    return ""
