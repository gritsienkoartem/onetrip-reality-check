"""Тесты мультимодального транспорта и провайдера live без сети."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from agent.planner import build_trip_plan
from agent.ranker import rank_flight_pairs
from models import Flight, TripRequest
from providers import ProviderError
from providers.estimates import estimate_transport
from providers.geo import GeoClient, GeoPoint
from providers.live import LiveProvider, _merge_offers
from providers.osm import LodgingArea, LodgingNode, TransportNode, TransportNodes
from providers.travelpayouts import TravelpayoutsClient

MOSCOW = GeoPoint("Москва", "Москва, Россия", 55.7558, 37.6173)
PENZA = GeoPoint("Пенза", "Пенза, Россия", 53.1953, 45.0190)


class FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None, headers=None):
        params = params or {}
        self.calls.append((url, params))
        return self._handler(url, params)


def _flight(option_id: str, mode: str, direction: str, departure: datetime, hours: int) -> Flight:
    return Flight(
        id=option_id,
        direction=direction,
        origin="A",
        destination="B",
        airline=mode,
        departure_time=departure,
        arrival_time=departure + timedelta(hours=hours),
        price_rub=1000,
        mode=mode,
        price_kind="estimate",
        is_mock=False,
        source="тест",
    )


def _node(kind: str, point: GeoPoint) -> TransportNode:
    names = {"airport": "Аэропорт", "train": "Вокзал", "bus": "Автостанция"}
    return TransportNode(
        id=f"{kind}-{point.city}",
        name=f"{names[kind]} {point.city}",
        kind=kind,
        latitude=point.latitude + 0.1,
        longitude=point.longitude + 0.1,
        distance_km=12.0,
    )


def _nodes_for(
    point: GeoPoint,
    *,
    airport: bool = True,
    train: bool = True,
    bus: bool = True,
) -> TransportNodes:
    return TransportNodes(
        airports=[_node("airport", point)] if airport else [],
        train_stations=[_node("train", point)] if train else [],
        bus_stations=[_node("bus", point)] if bus else [],
    )


def _use_fake_nodes(monkeypatch, provider: LiveProvider, **flags) -> None:
    monkeypatch.setattr(
        provider._osm,
        "transport_nodes",
        lambda point, **kwargs: _nodes_for(point, **flags),
    )


def _lodging_area(point: GeoPoint) -> LodgingArea:
    return LodgingArea(
        lodgings=[
            LodgingNode(
                id="osm-node-h1",
                name="Отель Центральный",
                kind="hotel",
                latitude=point.latitude,
                longitude=point.longitude,
                distance_to_center_km=0.5,
                distance_to_station_km=0.6,
                distance_to_metro_km=0.4,
                distance_to_water_km=0.2,
                tags={"stars": "4"},
            ),
            LodgingNode(
                id="osm-node-h2",
                name="Гостевой дом",
                kind="guest_house",
                latitude=point.latitude + 0.02,
                longitude=point.longitude + 0.02,
                distance_to_center_km=3.0,
            ),
            LodgingNode(
                id="osm-node-h3",
                name="Хостел",
                kind="hostel",
                latitude=point.latitude + 0.03,
                longitude=point.longitude + 0.03,
                distance_to_center_km=4.0,
            ),
        ],
        station_count=1,
        metro_count=1,
        water_count=1,
    )


def _use_fake_lodging(monkeypatch, provider: LiveProvider) -> None:
    monkeypatch.setattr(
        provider._osm,
        "lodging",
        lambda point, **kwargs: _lodging_area(point),
    )


def test_estimates_cover_three_modes_and_chronological_dates():
    options = estimate_transport(MOSCOW, PENZA, date(2026, 11, 17), date(2026, 11, 26), {
        "transport_estimates": {
            "source": "Оценка",
            "plane": {"base_rub": 2500, "rub_per_km": 4.5, "speed_kmh": 750, "ground_minutes": 120, "max_km": 12000},
            "train": {"base_rub": 1200, "rub_per_km": 2.2, "speed_kmh": 70, "ground_minutes": 45, "max_km": 8000},
            "bus": {"base_rub": 500, "rub_per_km": 1.8, "speed_kmh": 55, "ground_minutes": 30, "max_km": 1500},
        }
    })
    assert {option.mode for option in options} == {"flight", "train", "bus"}
    outbound = [item for item in options if item.direction == "outbound"]
    inbound = [item for item in options if item.direction == "return"]
    assert len(outbound) == len(inbound) == 3
    assert all(item.price_kind == "estimate" for item in options)
    assert all(item.price_rub > 0 for item in options)
    assert {item.departure_time.date() for item in outbound} == {date(2026, 11, 17)}
    assert {item.departure_time.date() for item in inbound} == {date(2026, 11, 26)}
    assert all(item.arrival_time > item.departure_time for item in options)


def test_estimates_gate_modes_by_nodes():
    assumptions = {
        "transport_estimates": {
            "source": "Оценка",
            "plane": {"base_rub": 2500, "rub_per_km": 4.5, "speed_kmh": 750, "ground_minutes": 120, "max_km": 12000},
            "train": {"base_rub": 1200, "rub_per_km": 2.2, "speed_kmh": 70, "ground_minutes": 45, "max_km": 8000},
            "bus": {"base_rub": 500, "rub_per_km": 1.8, "speed_kmh": 55, "ground_minutes": 30, "max_km": 1500},
        },
        "access_estimates": {
            "source": "Оценка дороги",
            "walk_max_km": 2.0,
            "walk_speed_kmh": 4.5,
            "taxi_speed_kmh": 40,
            "taxi_base_rub": 200,
            "taxi_rub_per_km": 35,
            "taxi_wait_minutes": 5,
        },
    }
    origin_nodes = TransportNodes(
        airports=[_node("airport", MOSCOW)],
        train_stations=[],
        bus_stations=[_node("bus", MOSCOW)],
    )
    destination_nodes = TransportNodes(
        airports=[_node("airport", PENZA)],
        train_stations=[],
        bus_stations=[_node("bus", PENZA)],
    )

    options = estimate_transport(
        MOSCOW,
        PENZA,
        date(2026, 11, 17),
        date(2026, 11, 26),
        assumptions,
        origin_nodes=origin_nodes,
        destination_nodes=destination_nodes,
    )

    assert {option.mode for option in options} == {"flight", "bus"}
    assert all(option.departure_node and option.arrival_node for option in options)
    assert all(option.access_to_departure is not None for option in options)
    assert all(option.access_from_arrival is not None for option in options)
    return_legs = [option for option in options if option.direction == "return"]
    assert return_legs
    assert all(option.access_to_departure.distance_km > 0 for option in return_legs)


def test_ranker_never_mixes_transport_modes():
    day = datetime(2026, 11, 17, 8, 0)
    outbound = [
        _flight("plane-out", "flight", "outbound", day, 2),
        _flight("train-out", "train", "outbound", day, 12),
    ]
    inbound = [
        _flight("plane-back", "flight", "return", day + timedelta(days=5), 2),
        _flight("bus-back", "bus", "return", day + timedelta(days=5), 10),
    ]
    pairs = rank_flight_pairs(outbound, inbound)
    assert pairs
    assert all(pair.outbound.mode == pair.inbound.mode for pair in pairs)


def test_travelpayouts_maps_real_prices_and_times():
    def handler(url, params):
        if "autocomplete" in url:
            code = "MOW" if params["term"] == "Москва" else "PEZ"
            return FakeResponse([{"code": code, "name": params["term"]}])
        return FakeResponse({
            "data": [{
                "price": 5000,
                "departure_at": "2026-11-17T10:00:00+03:00",
                "duration_to": 120,
                "transfers": 0,
                "airline": "SU",
                "flight_number": "SU123",
            }]
        })

    client = TravelpayoutsClient("token", FakeSession(handler))
    flights = client.search_direction("Москва", "Пенза", date(2026, 11, 17), "outbound")
    assert len(flights) == 1
    flight = flights[0]
    assert flight.mode == "flight"
    assert flight.price_kind == "real"
    assert flight.is_mock is False
    assert flight.price_rub == 5000
    assert flight.departure_time == datetime(2026, 11, 17, 10, 0)
    assert flight.arrival_time == datetime(2026, 11, 17, 12, 0)
    assert "Travelpayouts" in flight.source


def test_live_provider_without_token_returns_estimates(monkeypatch):
    provider = LiveProvider(token="", geocode_min_interval=0)
    monkeypatch.setattr(provider._geo, "geocode", lambda city: MOSCOW if city == "Москва" else PENZA)
    _use_fake_nodes(monkeypatch, provider)

    flights = provider.search_flights("Москва", "Пенза", date(2026, 11, 17), date(2026, 11, 26))
    assert flights
    assert {flight.mode for flight in flights} == {"flight", "train", "bus"}
    assert all(flight.price_kind == "estimate" for flight in flights)
    assert all(flight.direction == "outbound" for flight in flights)
    assert all(flight.departure_node and flight.arrival_node for flight in flights)
    assert all(flight.access_to_departure is not None for flight in flights)
    assert all(flight.access_from_arrival is not None for flight in flights)

    back = provider.search_flights("Пенза", "Москва", date(2026, 11, 26), date(2026, 11, 17))
    assert back and all(flight.direction == "return" for flight in back)
    assert {flight.departure_time.date() for flight in back} == {date(2026, 11, 26)}

    _use_fake_lodging(monkeypatch, provider)
    hotels = provider.search_hotels("Пенза", date(2026, 11, 17), date(2026, 11, 26))
    assert [hotel.name for hotel in hotels] == [
        "Отель Центральный",
        "Гостевой дом",
        "Хостел",
    ]
    assert all(hotel.is_mock is False for hotel in hotels)
    assert all(hotel.price_kind == "estimate" for hotel in hotels)
    assert all(hotel.rating_source == "Нет данных" for hotel in hotels)
    assert hotels[0].has_metro is True
    assert hotels[0].minutes_to_metro < 15
    assert hotels[1].has_metro is False


def test_hotel_lookup_falls_back_to_average_estimate(monkeypatch):
    provider = LiveProvider(token="", geocode_min_interval=0)
    monkeypatch.setattr(provider._geo, "geocode", lambda city: PENZA)

    def fail(point, **kwargs):
        raise ProviderError("нет сети")

    monkeypatch.setattr(provider._osm, "lodging", fail)

    hotels = provider.search_hotels("Пенза", date(2026, 11, 17), date(2026, 11, 26))

    assert len(hotels) == 1
    assert hotels[0].name.startswith("Среднее проживание")
    assert hotels[0].price_kind == "estimate"
    assert hotels[0].is_mock is False


def test_village_without_airport_has_no_plane(monkeypatch):
    provider = LiveProvider(token="", geocode_min_interval=0)
    monkeypatch.setattr(provider._geo, "geocode", lambda city: MOSCOW if city == "Москва" else PENZA)
    _use_fake_nodes(monkeypatch, provider, airport=False)

    flights = provider.search_flights("Москва", "Пенза", date(2026, 11, 17), date(2026, 11, 26))

    assert flights
    assert {flight.mode for flight in flights} == {"train", "bus"}
    assert all(flight.departure_node for flight in flights)
    assert all(flight.arrival_node for flight in flights)
    leg = flights[0].access_to_departure
    assert leg is not None
    assert leg.mode == "taxi"
    assert leg.distance_km > 0
    assert leg.price_rub > 0


def test_osm_failure_falls_back_to_distance_estimates(monkeypatch):
    provider = LiveProvider(token="", geocode_min_interval=0)
    monkeypatch.setattr(provider._geo, "geocode", lambda city: MOSCOW if city == "Москва" else PENZA)

    def fail(point, **kwargs):
        raise ProviderError("нет сети")

    monkeypatch.setattr(provider._osm, "transport_nodes", fail)

    flights = provider.search_flights("Москва", "Пенза", date(2026, 11, 17), date(2026, 11, 26))

    assert {flight.mode for flight in flights} == {"flight", "train", "bus"}
    assert all(flight.departure_node == "" for flight in flights)
    assert all("наличие узлов не проверено" in flight.source for flight in flights)


def test_live_provider_keeps_train_and_bus_when_real_flight_exists(monkeypatch):
    class FakeTravelpayouts:
        def search_direction(self, origin, destination, depart_date, direction):
            return [
                Flight(
                    id="real-flight",
                    direction=direction,
                    origin=origin,
                    destination=destination,
                    airline="SU",
                    departure_time=datetime(2026, 11, 17, 10, 0),
                    arrival_time=datetime(2026, 11, 17, 12, 0),
                    price_rub=5000,
                    mode="flight",
                    price_kind="real",
                    is_mock=False,
                    source="Travelpayouts (кэш цен Aviasales)",
                )
            ]

    provider = LiveProvider(token="", geocode_min_interval=0)
    provider._travelpayouts = FakeTravelpayouts()
    monkeypatch.setattr(provider._geo, "geocode", lambda city: MOSCOW if city == "Москва" else PENZA)
    _use_fake_nodes(monkeypatch, provider)

    flights = provider.search_flights("Москва", "Пенза", date(2026, 11, 17), date(2026, 11, 26))
    assert {flight.mode for flight in flights} == {"flight", "train", "bus"}
    real = [flight for flight in flights if flight.price_kind == "real"]
    assert [flight.mode for flight in real] == ["flight"]
    assert all(flight.price_kind == "estimate" for flight in flights if flight.mode != "flight")


def test_plan_lists_transport_modes_compared(monkeypatch):
    provider = LiveProvider(token="", geocode_min_interval=0)
    monkeypatch.setattr(provider._geo, "geocode", lambda city: MOSCOW if city == "Москва" else PENZA)
    monkeypatch.setattr(provider._geo, "places", lambda point: [])
    monkeypatch.setattr(provider._geo, "weather", lambda point, start, end, today: [])
    _use_fake_nodes(monkeypatch, provider)

    request = TripRequest(
        origin_city="Москва",
        destination_city="Пенза",
        start_date=date(2026, 11, 17),
        end_date=date(2026, 11, 26),
        duration_days=10,
        budget_amount=30000,
    )
    plan = build_trip_plan(request, provider=provider, today=date(2026, 10, 25))

    assert plan.flight_out is not None and plan.flight_back is not None
    modes = {option.mode for option in plan.transport_options}
    assert modes == {"flight", "train", "bus"}
    for option in plan.transport_options:
        assert option.outbound.direction == "outbound"
        assert option.inbound.direction == "return"
        assert option.total_price_rub == option.outbound.price_rub + option.inbound.price_rub
    selected = [
        option
        for option in plan.transport_options
        if option.outbound.id == plan.flight_out.id and option.inbound.id == plan.flight_back.id
    ]
    assert len(selected) == 1
    assert any("Сравнены виды транспорта" in note for note in plan.data_notes)


def test_geo_places_skip_streets_and_mark_missing_metadata():
    def handler(url, params):
        return FakeResponse({
            "query": {
                "geosearch": [
                    {"pageid": 1, "title": "Пензенский областной драматический театр", "lat": 53.19, "lon": 45.02},
                    {"pageid": 2, "title": "Улица Кирова (Пенза)", "lat": 53.19, "lon": 45.01},
                    {"pageid": 3, "title": "Здание Правительства Пензенской области", "lat": 53.19, "lon": 45.0},
                    {"pageid": 4, "title": "Памятник Денису Давыдову (Пенза)", "lat": 53.2, "lon": 45.01},
                ]
            }
        })

    places = GeoClient(FakeSession(handler), min_interval=0).places(PENZA)
    names = [place.name for place in places]
    assert names == ["Пензенский областной драматический театр", "Памятник Денису Давыдову (Пенза)"]
    assert all(place.is_mock is False for place in places)
    assert all(place.rating_source == "Нет данных" for place in places)
    assert places[0].indoor is True
    assert places[1].indoor is False


def test_geo_weather_uses_archive_for_far_dates():
    def handler(url, params):
        assert "archive" in url
        return FakeResponse({
            "daily": {
                "time": ["2025-11-17", "2025-11-18"],
                "weather_code": [71, 0],
                "temperature_2m_max": [-3.0, 1.0],
            }
        })

    weather = GeoClient(FakeSession(handler), min_interval=0).weather(
        PENZA, date(2026, 11, 17), date(2026, 11, 18), today=date(2026, 10, 25)
    )
    assert [day.day for day in weather] == [date(2026, 11, 17), date(2026, 11, 18)]
    assert weather[0].condition == "снег"
    assert weather[0].precipitation_probability is None
    assert weather[0].source.startswith("Open-Meteo (архив")


MOSCOW_RU = GeoPoint("Москва", "Москва, Россия", 55.7558, 37.6173, country="Россия")
PENZA_RU = GeoPoint("Пенза", "Пенза, Россия", 53.1953, 45.0190, country="Россия")
ISTANBUL_TR = GeoPoint("Стамбул", "Стамбул, Турция", 41.0082, 28.9784, country="Турция")


class FakeTravelSearch:
    """Подменяет MCP-клиент, сохраняя форму реальных ответов."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def search_flights(self, origin, destination, depart_date, *, adults=1):
        self.calls.append(("flights", origin, destination, depart_date))
        return {
            "flights": [
                {
                    "airline": "SU",
                    "flight_number": "123",
                    "departure_at": f"{depart_date.isoformat()}T10:00:00+03:00",
                    "transfers": 0,
                    "duration_to_minutes": 120,
                    "price_per_adult": 5000,
                    "currency": "RUB",
                }
            ],
            "url": "https://aviasales.tpm.lv/test",
        }

    def search_trains(self, origin, destination, depart_date, *, limit=5):
        self.calls.append(("trains", origin, destination, depart_date))
        return {
            "origin": {"name": origin},
            "destination": {"name": destination},
            "trains": [
                {
                    "train_number": "001А",
                    "departure_time": "18:00:00",
                    "duration_minutes": 600,
                    "categories": [{"name": "купе", "price_rub": 2000}],
                    "min_price_rub": 2000,
                    "url": "https://tutu.tpm.lv/train",
                }
            ],
            "url": "https://tutu.tpm.lv/list",
        }

    def search_hotels(self, **kwargs):
        self.calls.append(("hotels", kwargs))
        return {
            "offers": [
                {
                    "source": "leveltravel",
                    "hotel": "Тестовый отель",
                    "stars": 4,
                    "rating": 8.5,
                    "center_distance_m": 1200,
                    "resort": "Центр",
                    "country": "Россия",
                    "meal": "BB",
                    "nights": 3,
                    "price": 12000,
                    "currency": "RUB",
                    "url": "https://level.tpm.lv/hotel",
                }
            ],
            "tripcom_url": "https://ru.trip.com/hotels/list?city=1",
            "notes": ["Цена — за всех туристов и всё проживание."],
        }


def test_live_provider_uses_travel_search_for_flights_trains_and_hotels(monkeypatch):
    fake = FakeTravelSearch()
    provider = LiveProvider(
        token="",
        geocode_min_interval=0,
        enable_travel_search=True,
        travel_search=fake,
    )
    monkeypatch.setattr(
        provider._geo,
        "geocode",
        lambda city: {"Москва": MOSCOW_RU, "Пенза": PENZA_RU}.get(city),
    )
    _use_fake_nodes(monkeypatch, provider)

    flights = provider.search_flights("Москва", "Пенза", date(2026, 11, 17), date(2026, 11, 20))
    by_mode = {flight.mode: flight for flight in flights}
    assert set(by_mode) == {"flight", "train", "bus"}
    assert by_mode["flight"].price_kind == "real"
    assert by_mode["flight"].source.startswith("Aviasales")
    assert by_mode["flight"].booking_url == "https://aviasales.tpm.lv/test"
    assert by_mode["train"].price_kind == "estimate"
    assert by_mode["train"].source.startswith("Tutu.ru")
    assert by_mode["bus"].price_kind == "estimate"
    assert [call[0] for call in fake.calls] == ["flights", "trains"]

    hotels = provider.search_hotels(
        "Пенза", date(2026, 11, 17), date(2026, 11, 20), origin_city="Москва"
    )
    assert len(hotels) == 1
    hotel = hotels[0]
    assert hotel.stay_total_rub == 12000
    assert hotel.price_per_night_rub == 4000
    assert hotel.meal_plan == "завтраки"
    assert hotel.center_known is True
    assert hotel.metro_known is False
    assert provider.last_hotel_search_url.startswith("https://ru.trip.com/")
    assert any(note.startswith("Сервис поиска:") for note in provider.last_notes)


def test_live_provider_does_not_ask_trains_for_foreign_route(monkeypatch):
    fake = FakeTravelSearch()
    provider = LiveProvider(
        token="",
        geocode_min_interval=0,
        enable_travel_search=True,
        travel_search=fake,
    )
    monkeypatch.setattr(
        provider._geo,
        "geocode",
        lambda city: {"Москва": MOSCOW_RU, "Стамбул": ISTANBUL_TR}.get(city),
    )
    _use_fake_nodes(monkeypatch, provider)

    flights = provider.search_flights(
        "Москва", "Стамбул", date(2026, 11, 17), date(2026, 11, 20)
    )

    assert any(flight.source.startswith("Aviasales") for flight in flights)
    assert not any(call[0] == "trains" for call in fake.calls)


def test_planner_uses_total_stay_price_and_tripcom_link(monkeypatch):
    fake = FakeTravelSearch()
    provider = LiveProvider(
        token="",
        geocode_min_interval=0,
        enable_travel_search=True,
        travel_search=fake,
    )
    monkeypatch.setattr(
        provider._geo,
        "geocode",
        lambda city: {"Москва": MOSCOW_RU, "Пенза": PENZA_RU}.get(city),
    )
    monkeypatch.setattr(provider._geo, "places", lambda point: [])
    monkeypatch.setattr(provider._geo, "weather", lambda point, start, end, today: [])
    _use_fake_nodes(monkeypatch, provider)
    _use_fake_lodging(monkeypatch, provider)

    request = TripRequest(
        origin_city="Москва",
        destination_city="Пенза",
        start_date=date(2026, 11, 17),
        end_date=date(2026, 11, 20),
        duration_days=4,
        budget_amount=30000,
    )
    plan = build_trip_plan(request, provider=provider, today=date(2026, 10, 25))

    assert plan.hotel is not None
    assert plan.hotel.stay_total_rub == 12000
    assert plan.cost is not None
    assert plan.cost.accommodation == 12000
    assert plan.hotel_search_url.startswith("https://ru.trip.com/")
    assert any("Level.Travel" in note for note in plan.data_notes)


def test_merge_offers_keeps_lowest_price_for_same_flight():
    day = datetime(2026, 11, 17, 10, 0)
    primary = _flight("avia", "flight", "outbound", day, 2).model_copy(
        update={
            "airline": "SU 123",
            "price_rub": 5200,
            "price_kind": "real",
            "source": "Aviasales (кэш)",
        }
    )
    backup = primary.model_copy(
        update={
            "id": "payouts",
            "airline": "SU123",
            "price_rub": 4800,
            "source": "Travelpayouts (кэш цен Aviasales)",
        }
    )
    later = primary.model_copy(
        update={
            "id": "later",
            "departure_time": day + timedelta(hours=2),
            "arrival_time": day + timedelta(hours=4),
            "price_rub": 4000,
        }
    )

    merged, duplicates = _merge_offers([primary], [backup, later])

    assert duplicates == 1
    assert [flight.id for flight in merged] == ["later", "payouts"]
    assert all(flight.id != "avia" for flight in merged)


def test_live_provider_compares_travelpayouts_and_keeps_cheaper(monkeypatch):
    day = datetime(2026, 11, 17, 10, 0)
    primary = _flight("avia", "flight", "outbound", day, 2).model_copy(
        update={
            "origin": "Москва",
            "destination": "Пенза",
            "airline": "SU 123",
            "price_rub": 5200,
            "price_kind": "real",
            "source": "Aviasales (кэш)",
        }
    )
    backup = primary.model_copy(
        update={
            "id": "payouts",
            "airline": "SU123",
            "price_rub": 4800,
            "source": "Travelpayouts (кэш цен Aviasales)",
        }
    )

    class FakeTravelpayouts:
        def search_direction(self, origin, destination, depart_date, direction):
            return [backup]

    provider = LiveProvider(token="", geocode_min_interval=0)
    provider._travelpayouts = FakeTravelpayouts()
    monkeypatch.setattr(
        provider, "_travel_search_flights", lambda *args, **kwargs: [primary]
    )
    monkeypatch.setattr(
        provider._geo, "geocode", lambda city: MOSCOW if city == "Москва" else PENZA
    )
    _use_fake_nodes(monkeypatch, provider)

    flights = provider.search_flights(
        "Москва", "Пенза", date(2026, 11, 17), date(2026, 11, 26)
    )

    real = [flight for flight in flights if flight.mode == "flight"]
    assert [flight.id for flight in real] == ["payouts"]
    assert real[0].price_rub == 4800
    assert any("совпадающих рейсов: 1" in note for note in provider.last_notes)
    assert any("Сравнены предложения" in note for note in provider.last_notes)
