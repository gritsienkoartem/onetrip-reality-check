"""Тесты сборки полного плана поездки."""
from datetime import date, datetime

import pytest

from agent.planner import build_trip_plan, resolve_period_start
from agent.ranker import FlightPair
from models import Flight, Hotel, RiskLevel, TripRequest
from providers.fixtures import FixtureProvider

TODAY = date(2026, 10, 24)


@pytest.fixture
def trip_request() -> TripRequest:
    return TripRequest(
        origin_city="Москва",
        destination_city="Стамбул",
        duration_days=4,
        period_description="середина ноября",
        budget_amount=60000,
        interests=["архитектура", "кофе"],
        avoid=["толпы"],
    )


@pytest.fixture
def plan(trip_request):
    return build_trip_plan(trip_request, provider=FixtureProvider(), today=TODAY)


def test_plan_has_flights_hotel_and_days(plan):
    assert plan.flight_out is not None
    assert plan.flight_out.direction == "outbound"
    assert plan.flight_back is not None
    assert plan.flight_back.direction == "return"
    assert plan.hotel is not None
    assert len(plan.days) == 4
    assert [day.day for day in plan.days] == [
        date(2026, 11, 14),
        date(2026, 11, 15),
        date(2026, 11, 16),
        date(2026, 11, 17),
    ]
    assert plan.request.start_date == date(2026, 11, 14)
    assert plan.request.end_date == date(2026, 11, 17)
    assert plan.request.duration_days == 4


def test_plan_contains_ranked_places_without_duplicates(plan):
    items = [item for day in plan.days for item in day.items]
    assert items
    assert len({item.place_id for item in items}) == len(items)
    assert all(item.start_time <= item.end_time for item in items)
    assert all(len(day.items) <= 4 for day in plan.days)


def test_cost_breakdown_matches_selected_entities(plan):
    assert plan.cost is not None
    assert plan.cost.flights == plan.flight_out.price_rub + plan.flight_back.price_rub
    assert plan.cost.accommodation == plan.hotel.price_per_night_rub * 3
    assert plan.cost.transfers == 3000
    assert plan.cost.local_transport == 2400
    assert plan.cost.baggage == sum(
        0 if flight.baggage_included else flight.baggage_fee_rub
        for flight in (plan.flight_out, plan.flight_back)
    )
    assert plan.cost.attractions == sum(item.cost_rub for day in plan.days for item in day.items)
    assert plan.cost.total == pytest.approx(
        plan.cost.flights
        + plan.cost.accommodation
        + plan.cost.transfers
        + plan.cost.baggage
        + plan.cost.tourist_tax
        + plan.cost.local_transport
        + plan.cost.attractions
    )
    assert plan.cost.is_estimate is True


def test_reality_check_runs_and_marks_risk(plan):
    assert plan.issues
    assert plan.risk_score > 0
    assert plan.risk_level is not None
    assert any(issue.code == "LATE_ARRIVAL" for issue in plan.issues)
    assert all(issue.description and issue.recommendation for issue in plan.issues)


def test_fixture_data_is_disclosed(plan):
    assert any("тестовые данные" in note.lower() for note in plan.data_notes)
    assert any("Точные даты не были указаны" in note for note in plan.data_notes)


def test_explicit_dates_are_preserved(trip_request):
    trip_request = trip_request.model_copy(update={"start_date": date(2026, 11, 14), "end_date": date(2026, 11, 17)})
    plan = build_trip_plan(trip_request, provider=FixtureProvider(), today=TODAY)
    assert plan.request.start_date == date(2026, 11, 14)
    assert plan.request.end_date == date(2026, 11, 17)


def test_missing_origin_does_not_crash(trip_request):
    trip_request = trip_request.model_copy(update={"origin_city": None})
    plan = build_trip_plan(trip_request, provider=FixtureProvider(), today=TODAY)
    assert plan.flight_out is None
    assert plan.flight_back is None
    assert any("город вылета" in note for note in plan.data_notes)
    assert len(plan.days) == 4


def test_unknown_destination_is_reported_without_fake_plan(trip_request):
    trip_request = trip_request.model_copy(
        update={
            "origin_city": "Минск",
            "destination_city": "Иркутск",
            "start_date": date(2026, 10, 27),
            "end_date": date(2026, 10, 30),
            "duration_days": 4,
        }
    )
    plan = build_trip_plan(trip_request, provider=FixtureProvider(), today=TODAY)

    assert plan.flight_out is None
    assert plan.flight_back is None
    assert plan.hotel is None
    assert plan.days == []
    assert plan.cost is None
    assert [issue.code for issue in plan.issues] == ["DESTINATION_UNSUPPORTED"]
    assert plan.issues[0].severity.value == "high"
    assert plan.risk_level == RiskLevel.HIGH
    assert any("нет данных" in note for note in plan.data_notes)
    assert any("не строятся" in note for note in plan.data_notes)


def test_period_resolution():
    assert resolve_period_start("середина ноября", date(2026, 10, 24)) == date(2026, 11, 15)
    assert resolve_period_start("в конце декабря", date(2026, 12, 25)) == date(2027, 12, 24)
    assert resolve_period_start("", date(2026, 10, 24)) is None
    assert resolve_period_start("маленький отель", date(2026, 10, 24)) is None


class RecordingProvider(FixtureProvider):
    """Мок-провайдер, который запоминает число взрослых для отелей."""

    def __init__(self) -> None:
        super().__init__()
        self.requested_adults: list[int] = []

    def search_hotels(self, city, check_in=None, check_out=None, *, origin_city=None, adults=1):
        self.requested_adults.append(adults)
        return super().search_hotels(
            city,
            check_in,
            check_out,
            origin_city=origin_city,
            adults=adults,
        )


def test_adults_are_passed_to_hotel_search(trip_request):
    provider = RecordingProvider()
    request = trip_request.model_copy(
        update={"adults": 2, "children": 1, "start_date": date(2026, 11, 14), "end_date": date(2026, 11, 17)}
    )
    plan = build_trip_plan(request, provider=provider, today=TODAY)

    assert request.travelers == 3
    assert provider.requested_adults
    assert set(provider.requested_adults) == {2}
    assert plan.request.adults == 2
    assert plan.request.children == 1


def test_calm_style_limits_places_per_day(trip_request):
    request = trip_request.model_copy(
        update={"trip_style": "calm", "start_date": date(2026, 11, 14), "end_date": date(2026, 11, 17)}
    )
    plan = build_trip_plan(request, provider=FixtureProvider(), today=TODAY)

    assert all(len(day.items) <= 2 for day in plan.days)
    assert any("Стиль поездки" in note for note in plan.data_notes)


def test_transport_alternative_is_kept_for_comparison(trip_request):
    request = trip_request.model_copy(
        update={
            "transport_preferences": ["самолёт"],
            "start_date": date(2026, 11, 14),
            "end_date": date(2026, 11, 17),
        }
    )
    plan = build_trip_plan(request, provider=FixtureProvider(), today=TODAY)

    assert plan.flight_out is not None and plan.flight_out.mode == "flight"
    assert plan.transport_alternatives
    assert any("вариантов транспорта" in note for note in plan.data_notes)
    assert any("выбран самолёт" in note for note in plan.data_notes)


def test_unavailable_transport_preference_is_reported(trip_request):
    request = trip_request.model_copy(
        update={
            "transport_preferences": ["поезд"],
            "start_date": date(2026, 11, 14),
            "end_date": date(2026, 11, 17),
        }
    )
    plan = build_trip_plan(request, provider=FixtureProvider(), today=TODAY)

    assert plan.flight_out is not None
    assert any("учесть не удалось" in note for note in plan.data_notes)


def test_unconfirmed_hotel_format_is_reported_honestly(trip_request):
    request = trip_request.model_copy(
        update={
            "accommodation_preferences": ["кемпинг", "отель"],
            "start_date": date(2026, 11, 14),
            "end_date": date(2026, 11, 17),
        }
    )
    plan = build_trip_plan(request, provider=FixtureProvider(), today=TODAY)

    assert plan.hotel is not None
    assert plan.request.accommodation_preferences == ["кемпинг", "отель"]
    assert any(
        "не подтвердили пожелания к жилью: кемпинг" in note for note in plan.data_notes
    )
    assert not any(
        "не подтвердили пожелания к жилью: кемпинг, отель" in note
        for note in plan.data_notes
    )


def test_progress_callback_reports_real_stages(trip_request):
    labels: list[str] = []
    build_trip_plan(
        trip_request,
        provider=FixtureProvider(),
        today=TODAY,
        progress=labels.append,
    )

    assert labels[0] == "Ищу билеты и расписание"
    assert any("проживания" in label for label in labels)
    assert any("места" in label for label in labels)
    assert labels[-1] == "Проверяю план по правилам Reality Check"


def test_plan_summarises_prices_by_direction(plan):
    assert plan.price_sources
    assert {source.direction for source in plan.price_sources} == {
        "outbound",
        "return",
    }
    assert all(source.offers_count >= 1 for source in plan.price_sources)
    assert all(source.cheapest_rub > 0 for source in plan.price_sources)


def _flight_with_node(
    flight_id: str,
    direction: str,
    departure: datetime,
    *,
    node_name: str,
    node_latitude: float,
    node_longitude: float,
    departure_node: bool,
) -> Flight:
    data = dict(
        id=flight_id,
        direction=direction,
        origin="Москва" if direction == "outbound" else "Стамбул",
        destination="Стамбул" if direction == "outbound" else "Москва",
        airline="Тестовая авиакомпания",
        departure_time=departure,
        arrival_time=departure.replace(hour=departure.hour + 3),
        price_rub=10000,
        mode="flight",
        price_kind="estimate",
        is_mock=False,
        source="тест",
    )
    if departure_node:
        data.update(
            departure_node=node_name,
            departure_node_latitude=node_latitude,
            departure_node_longitude=node_longitude,
        )
    else:
        data.update(
            arrival_node=node_name,
            arrival_node_latitude=node_latitude,
            arrival_node_longitude=node_longitude,
        )
    return Flight(**data)


def test_plan_builds_hotel_transfers_from_node_coordinates(trip_request):
    hotel = Hotel(
        id="hotel-1",
        name="Отель у вокзала",
        district="Центр",
        city="Стамбул",
        price_per_night_rub=5000,
        rating=8.5,
        minutes_to_center=8,
        minutes_to_metro=5,
        latitude=41.0082,
        longitude=28.9784,
        price_kind="estimate",
        is_mock=False,
        source="тест",
    )
    outbound = _flight_with_node(
        "out-1",
        "outbound",
        datetime(2026, 11, 14, 9, 0),
        node_name="Аэропорт Стамбул",
        node_latitude=41.2753,
        node_longitude=28.7519,
        departure_node=False,
    )
    inbound = _flight_with_node(
        "in-1",
        "return",
        datetime(2026, 11, 17, 18, 0),
        node_name="Аэропорт Стамбул",
        node_latitude=41.2753,
        node_longitude=28.7519,
        departure_node=True,
    )
    pair = FlightPair(
        outbound=outbound,
        inbound=inbound,
        score=0.0,
        total_price_rub=20000,
        total_duration_minutes=360,
        total_stops=0,
    )

    plan = build_trip_plan(
        trip_request,
        provider=FixtureProvider(),
        today=TODAY,
        flight_pair=pair,
        hotel_choice=hotel,
    )

    assert [transfer.title for transfer in plan.hotel_transfers] == [
        "Аэропорт → отель",
        "Отель → аэропорт",
    ]
    arrival = plan.hotel_transfers[0]
    assert arrival.from_name == "Аэропорт Стамбул"
    assert arrival.to_name == "Отель у вокзала"
    assert {option.mode for option in arrival.options} == {
        "public_transport",
        "taxi",
    }
    assert arrival.by_mode("taxi").price_rub > 0
    assert any("Трансферы включают" in note for note in plan.cost.notes)
    assert plan.cost.transfers > 2000
