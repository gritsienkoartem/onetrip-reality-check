"""Тесты клиента travel-search-ru и маппинга ответов без сети."""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from models import NO_DATA
from providers.base import ProviderError
from providers.geo import GeoPoint
from providers.travel_search import (
    TravelSearchClient,
    flights_from_payload,
    hotels_from_payload,
    trains_from_payload,
    tripcom_url_from_payload,
)

ISTANBUL = GeoPoint("Стамбул", "Стамбул, Турция", 41.0082, 28.9784, country="Турция")


class FakeResponse:
    def __init__(
        self,
        text: str = "",
        status_code: int = 200,
        headers: dict | None = None,
        content: bytes | None = None,
    ) -> None:
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content if content is not None else text.encode("utf-8")


class FakeMcpSession:
    """Отвечает заранее заданной очередью и записывает запросы."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict, dict]] = []

    def post(self, url, data=None, headers=None, timeout=None, allow_redirects=None):
        payload = json.loads(data.decode("utf-8"))
        self.calls.append((url, payload, dict(headers or {})))
        if not self._responses:
            raise AssertionError("неожиданный запрос к сервису поиска")
        return self._responses.pop(0)


def _sse(payload: dict) -> str:
    return "event: message\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _init_response() -> FakeResponse:
    return FakeResponse(
        _sse(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "travel-search", "version": "2.6.0"},
                },
            }
        ),
        headers={"Mcp-Session-Id": "session-123"},
    )


def _tool_response(tool_id: int, structured: dict | None = None, content: list | None = None) -> FakeResponse:
    result: dict = {"isError": False}
    if structured is not None:
        result["structuredContent"] = structured
    if content is not None:
        result["content"] = content
    return FakeResponse(_sse({"jsonrpc": "2.0", "id": tool_id, "result": result}))


def test_client_initializes_and_parses_sse():
    session = FakeMcpSession(
        [
            _init_response(),
            FakeResponse("", status_code=202),
            _tool_response(2, structured={"flights": [], "notes": ["тест"]}),
        ]
    )
    client = TravelSearchClient(session=session, min_interval=0)

    result = client.search_flights("Москва", "Стамбул", date(2026, 11, 14))

    assert result["notes"] == ["тест"]
    assert len(session.calls) == 3
    methods = [payload.get("method") for _, payload, _ in session.calls]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    tool_request = session.calls[-1][1]
    assert tool_request["params"]["name"] == "search_flights"
    assert tool_request["params"]["arguments"]["depart_date"] == "2026-11-14"
    assert session.calls[-1][2]["Mcp-Session-Id"] == "session-123"
    assert session.calls[-1][2]["MCP-Protocol-Version"] == "2025-06-18"


def test_client_decodes_utf8_sse_without_charset():
    body = _sse(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "structuredContent": {
                    "notes": ["Цена — кэш, наличие мест не подтверждено"],
                }
            },
        }
    ).encode("utf-8")
    session = FakeMcpSession(
        [
            _init_response(),
            FakeResponse("", status_code=202),
            FakeResponse(content=body),
        ]
    )
    client = TravelSearchClient(session=session, min_interval=0)

    result = client.search_flights("Москва", "Стамбул", date(2026, 11, 14))

    assert result["notes"] == ["Цена — кэш, наличие мест не подтверждено"]


def test_client_parses_json_text_content():
    session = FakeMcpSession(
        [
            _init_response(),
            FakeResponse("", status_code=202),
            FakeResponse(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {
                            "content": [
                                {"type": "text", "text": json.dumps({"trains": []})}
                            ]
                        },
                    }
                )
            ),
        ]
    )
    client = TravelSearchClient(session=session, min_interval=0)

    result = client.call_tool("search_train_tickets", {"origin": "Москва"})

    assert result == {"trains": []}


def test_tool_error_hides_urls_and_prefix():
    session = FakeMcpSession(
        [
            _init_response(),
            FakeResponse("", status_code=202),
            FakeResponse(
                _sse(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {
                            "isError": True,
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Error executing tool search_flights: "
                                    "Источник отклонил параметры https://example.com/secret",
                                }
                            ],
                        },
                    }
                )
            ),
        ]
    )
    client = TravelSearchClient(session=session, min_interval=0)

    with pytest.raises(ProviderError) as error:
        client.search_flights("Москва", "Стамбул", date(2026, 11, 14))

    message = str(error.value)
    assert "Источник отклонил параметры" in message
    assert "http" not in message
    assert "Error executing tool" not in message


def test_rate_limit_is_reported_in_russian():
    session = FakeMcpSession([FakeResponse("", status_code=429)])
    client = TravelSearchClient(session=session, min_interval=0)

    with pytest.raises(ProviderError) as error:
        client.search_flights("Москва", "Стамбул", date(2026, 11, 14))

    assert "слишком много запросов" in str(error.value)


def test_tool_result_is_cached():
    session = FakeMcpSession(
        [
            _init_response(),
            FakeResponse("", status_code=202),
            _tool_response(2, structured={"flights": []}),
        ]
    )
    client = TravelSearchClient(session=session, min_interval=0)

    first = client.search_flights("Москва", "Стамбул", date(2026, 11, 14))
    second = client.search_flights("Москва", "Стамбул", date(2026, 11, 14))

    assert first == second == {"flights": []}
    assert len(session.calls) == 3


def test_hotel_request_uses_nights_and_dates():
    session = FakeMcpSession(
        [
            _init_response(),
            FakeResponse("", status_code=202),
            _tool_response(2, structured={"offers": []}),
        ]
    )
    client = TravelSearchClient(session=session, min_interval=0)

    client.search_hotels(
        departure_city="Москва",
        country="Турция",
        resort="Стамбул",
        check_in=date(2026, 11, 14),
        check_out=date(2026, 11, 17),
    )

    arguments = session.calls[-1][1]["params"]["arguments"]
    assert arguments["nights_min"] == arguments["nights_max"] == 3
    assert arguments["date_from"] == arguments["date_to"] == "2026-11-14"
    assert arguments["country"] == "Турция"


def test_flights_mapping():
    payload = {
        "origin": "MOW",
        "destination": "IST",
        "flights": [
            {
                "airline": "2S",
                "flight_number": "086",
                "departure_at": "2026-11-14T00:50:00+03:00",
                "transfers": 0,
                "duration_to_minutes": 225,
                "price_per_adult": 8359,
                "currency": "RUB",
            }
        ],
        "url": "https://aviasales.tpm.lv/XNDXtSim",
        "notes": [],
    }

    flights = flights_from_payload(
        payload, direction="outbound", origin="Москва", destination="Стамбул"
    )

    assert len(flights) == 1
    flight = flights[0]
    assert flight.mode == "flight"
    assert flight.price_kind == "real"
    assert flight.is_mock is False
    assert flight.price_rub == 8359
    assert flight.airline == "2S 086"
    assert flight.departure_time == datetime(2026, 11, 14, 0, 50)
    assert flight.arrival_time == datetime(2026, 11, 14, 4, 35)
    assert flight.layover_minutes is None
    assert flight.baggage_known is False
    assert flight.booking_url == "https://aviasales.tpm.lv/XNDXtSim"
    assert "Aviasales" in flight.source


def test_trains_mapping_is_marked_as_cached_estimate():
    payload = {
        "provider": "Tutu.ru",
        "origin": {"code": "2000000", "name": "Москва"},
        "destination": {"code": "2064130", "name": "Сочи"},
        "depart_date": "2026-11-14",
        "trains": [
            {
                "train_number": "083М",
                "departure_time": "19:45:00",
                "duration_minutes": 2400,
                "categories": [
                    {"type": "coupe", "name": "купе", "price_rub": 2463},
                ],
                "min_price_rub": 2463,
                "url": "https://tutu.tpm.lv/21u8Bi8s",
            }
        ],
        "url": "https://tutu.tpm.lv/objPm6fw",
        "notes": [],
    }

    trains = trains_from_payload(payload, direction="outbound", depart_date=date(2026, 11, 14))

    assert len(trains) == 1
    train = trains[0]
    assert train.mode == "train"
    assert train.price_kind == "estimate"
    assert train.is_mock is False
    assert train.airline == "Поезд 083М · купе"
    assert train.departure_time == datetime(2026, 11, 14, 19, 45)
    assert train.arrival_time > train.departure_time
    assert "Tutu.ru" in train.source
    assert train.booking_url == "https://tutu.tpm.lv/21u8Bi8s"


def test_hotels_mapping_keeps_total_price_and_unknown_distances():
    payload = {
        "offers": [
            {
                "source": "leveltravel",
                "hotel": "Nostalji Hostel",
                "stars": None,
                "rating": 5.3,
                "center_distance_m": None,
                "resort": "Бейоглу",
                "country": "Турция",
                "meal": "HB",
                "nights": 3,
                "price": 3143,
                "currency": "RUB",
                "url": "https://level.tpm.lv/5PzJg7L9",
            },
            {
                "source": "leveltravel",
                "hotel": "Kalkedon Hostel",
                "stars": None,
                "rating": None,
                "center_distance_m": 6000,
                "resort": "Кадыкёй",
                "country": "Турция",
                "meal": "RO",
                "nights": 3,
                "price": 4700,
                "currency": "RUB",
                "url": "https://level.tpm.lv/e5eQ2pE2",
            },
        ]
    }

    hotels = hotels_from_payload(
        payload,
        city="Стамбул",
        point=ISTANBUL,
        check_in=date(2026, 11, 14),
        check_out=date(2026, 11, 17),
    )

    assert [hotel.name for hotel in hotels] == ["Nostalji Hostel", "Kalkedon Hostel"]
    first, second = hotels
    assert first.stay_total_rub == 3143
    assert first.price_per_night_rub == pytest.approx(1047.67)
    assert first.center_known is False
    assert first.metro_known is False
    assert first.meal_plan == "полупансион"
    assert first.rating_source == "Level.Travel (рейтинг по шкале 0–10)"
    assert first.booking_url == "https://level.tpm.lv/5PzJg7L9"
    assert second.center_known is True
    assert second.distance_to_center_km == 6.0
    assert second.minutes_to_center == 75
    assert second.rating_source == NO_DATA


def test_tripcom_and_booking_urls_must_be_safe():
    assert tripcom_url_from_payload(
        {"tripcom_url": "https://ru.trip.com/hotels/list?city=532"}
    ).startswith("https://ru.trip.com/")
    assert tripcom_url_from_payload({"tripcom_url": "https://evil.example/trip.com"}) == ""
    assert tripcom_url_from_payload({"tripcom_url": "javascript:alert(1)"}) == ""

    payload = {
        "flights": [
            {
                "airline": "SU",
                "flight_number": "1",
                "departure_at": "2026-11-14T10:00:00",
                "duration_to_minutes": 60,
                "price_per_adult": 1000,
                "currency": "RUB",
            }
        ],
        "url": "http://unsafe.example/ticket",
    }
    flight = flights_from_payload(
        payload, direction="outbound", origin="A", destination="B"
    )[0]
    assert flight.booking_url == ""
