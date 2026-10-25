"""Базовые тесты моделей и мок-данных (этап 1)."""
from datetime import date, datetime, time

import pytest
from pydantic import ValidationError

from models import (
    Flight,
    Hotel,
    Issue,
    OpeningHours,
    Place,
    RiskLevel,
    Severity,
    TripRequest,
    WeatherDay,
)


# ---------------- TripRequest ----------------
def test_trip_request_defaults():
    request = TripRequest()
    assert request.currency == "RUB"
    assert request.interests == []


def test_currency_is_normalized():
    assert TripRequest(currency=" rub ").currency == "RUB"


@pytest.mark.parametrize("bad_currency", ["рубль", "RU", "R2B", ""])
def test_invalid_currency_rejected(bad_currency):
    with pytest.raises(ValidationError):
        TripRequest(currency=bad_currency)


def test_budget_must_be_positive():
    with pytest.raises(ValidationError):
        TripRequest(budget_amount=-100)


def test_end_date_before_start_date_rejected():
    with pytest.raises(ValidationError):
        TripRequest(start_date=date(2026, 11, 17), end_date=date(2026, 11, 14))


def test_duration_is_computed_from_dates():
    request = TripRequest(start_date=date(2026, 11, 14), end_date=date(2026, 11, 17))
    assert request.duration_days == 4


def test_missing_fields_are_reported():
    request = TripRequest(destination_city="Стамбул")
    assert request.missing_fields() == ["origin_city", "duration_days"]


def test_complete_request_has_no_missing_fields():
    request = TripRequest(origin_city="Москва", destination_city="Стамбул", duration_days=4)
    assert request.missing_fields() == []


# ---------------- Issue и уровни ----------------
def test_issue_requires_valid_severity():
    with pytest.raises(ValidationError):
        Issue(code="X", severity="critical", description="д", source="с", recommendation="р")


def test_issue_is_created():
    issue = Issue(code="X", severity="high", description="д", source="Оценка", recommendation="р")
    assert issue.severity is Severity.HIGH


def test_russian_labels():
    assert RiskLevel.LOW.label_ru == "Низкий"
    assert RiskLevel.MEDIUM.label_ru == "Средний"
    assert RiskLevel.HIGH.label_ru == "Высокий"
    assert Severity.HIGH.label_ru == "Высокая"


# ---------------- Flight / OpeningHours ----------------
def _flight_kwargs(**changes):
    data = dict(
        id="T1", direction="outbound", origin="Москва", destination="Стамбул", airline="Тест",
        departure_time=datetime(2026, 11, 14, 8, 0), arrival_time=datetime(2026, 11, 14, 12, 0),
        price_rub=1000,
    )
    data.update(changes)
    return data


def test_flight_arrival_must_be_after_departure():
    with pytest.raises(ValidationError):
        Flight(**_flight_kwargs(arrival_time=datetime(2026, 11, 14, 7, 0)))


def test_flight_layover_count_must_match_stops():
    with pytest.raises(ValidationError):
        Flight(**_flight_kwargs(stops=1, layover_minutes=[]))


def test_opening_hours_order_checked():
    with pytest.raises(ValidationError):
        OpeningHours(opens=time(18, 0), closes=time(9, 0))


def test_opening_hours_weekday_range_checked():
    with pytest.raises(ValidationError):
        OpeningHours(opens=time(9, 0), closes=time(18, 0), closed_weekdays=[7])


# ---------------- Тестовые фикстуры ----------------
def test_fixture_flights_are_valid(load_fixture):
    flights = [Flight(**item) for item in load_fixture("flights.json")]
    assert len({f.id for f in flights}) == len(flights)
    assert all(f.is_mock for f in flights)
    assert {f.direction for f in flights} == {"outbound", "return"}


def test_fixture_hotels_are_valid(load_fixture):
    hotels = [Hotel(**item) for item in load_fixture("hotels.json")]
    assert len({h.id for h in hotels}) == len(hotels)
    assert all(h.is_mock for h in hotels)


def test_fixture_places_are_valid(load_fixture):
    places = [Place(**item) for item in load_fixture("places.json")]
    assert len({p.id for p in places}) == len(places)
    assert all(p.is_mock for p in places)
    # В данных должны быть места без часов работы — для проверки сценария «Нет данных».
    assert any(p.opening_hours is None for p in places)


def test_fixture_weather_are_valid(load_fixture):
    days = [WeatherDay(**item) for item in load_fixture("weather.json")]
    assert len(days) == 4
    assert all(day.is_mock for day in days)
    # В прогнозе есть дождливый день — для проверки погодного правила.
    assert any("дождь" in day.condition for day in days)


def test_fixtures_cover_evaluation_scenarios(load_fixture):
    """В фикстурах есть и ночной прилёт, и короткая пересадка, и далёкий отель."""
    flights = [Flight(**item) for item in load_fixture("flights.json")]
    hotels = [Hotel(**item) for item in load_fixture("hotels.json")]
    assert any(f.arrival_time.hour < 5 for f in flights)
    assert any(f.layover_minutes and min(f.layover_minutes) < 60 for f in flights)
    assert any(h.minutes_to_center > 60 for h in hotels)


def test_assumptions_have_required_sections(load_json):
    data = load_json("assumptions.json")
    for key in ("thresholds", "risk_scoring", "cost_estimates", "fx_to_rub", "ranking_weights"):
        assert key in data
    for key in ("max_walking_km_per_day", "max_attractions_per_day", "min_layover_minutes"):
        assert key in data["thresholds"]
    assert data["fx_to_rub"]["rates"]["RUB"] == 1.0


@pytest.mark.parametrize("group", ["flights", "hotels", "places"])
def test_ranking_weights_sum_to_one(load_json, group):
    weights = load_json("assumptions.json")["ranking_weights"][group]
    assert sum(weights.values()) == pytest.approx(1.0)
