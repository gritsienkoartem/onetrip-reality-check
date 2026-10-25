"""Тесты цикла «собрать → проверить → исправить → проверить»."""
from datetime import date, datetime, timedelta, time

from agent.corrector import build_corrected_plan
from models import Flight, Hotel, OpeningHours, Place, TripRequest, WeatherDay
from providers.fixtures import FixtureProvider

TODAY = date(2026, 10, 24)


def base_request(**changes) -> TripRequest:
    data = dict(
        origin_city="Москва",
        destination_city="Стамбул",
        duration_days=4,
        period_description="середина ноября",
        budget_amount=60000,
        interests=["архитектура", "кофе"],
        avoid=["толпы"],
    )
    data.update(changes)
    return TripRequest(**data)


class PerfectProvider:
    """Данные без проблем: дневной прилёт, отель в центре, помещения и ясно."""

    name = "perfect"

    def _flight(self, flight_id: str, direction: str, departure: datetime, arrival: datetime) -> Flight:
        return Flight(
            id=flight_id,
            direction=direction,
            origin="Москва" if direction == "outbound" else "Стамбул",
            destination="Стамбул" if direction == "outbound" else "Москва",
            airline="Тест-Авиа",
            departure_time=departure,
            arrival_time=arrival,
            price_rub=12000,
            baggage_included=True,
            is_mock=False,
            source="Тестовые данные",
        )

    def search_flights(self, origin, destination, depart_date=None, return_date=None):
        if origin == "Москва" and destination == "Стамбул":
            return [
                self._flight(
                    "OUT",
                    "outbound",
                    datetime(2026, 11, 14, 9, 0),
                    datetime(2026, 11, 14, 13, 0),
                )
            ]
        if origin == "Стамбул" and destination == "Москва":
            return [
                self._flight(
                    "BACK",
                    "return",
                    datetime(2026, 11, 17, 14, 0),
                    datetime(2026, 11, 17, 18, 0),
                )
            ]
        return []

    def search_hotels(self, city, check_in=None, check_out=None, *, origin_city=None, adults=1):
        return [
            Hotel(
                id="H_PERFECT",
                name="Отель в центре",
                district="Центр",
                city="Стамбул",
                price_per_night_rub=5000,
                rating=9.0,
                minutes_to_center=10,
                minutes_to_metro=5,
                tourist_tax_per_night_rub=0,
                latitude=41.0,
                longitude=29.0,
                is_mock=False,
                source="Тестовые данные",
            )
        ]

    def get_places(self, city):
        hours = OpeningHours(opens=time(9, 0), closes=time(19, 0))
        return [
            Place(
                id=f"P_PERFECT_{index}",
                name=f"Музей {index}",
                city="Стамбул",
                categories=["музей"],
                latitude=41.001 + index * 0.001,
                longitude=29.001 + index * 0.001,
                indoor=True,
                visit_minutes=60,
                entrance_fee_rub=0,
                rating=9.0,
                crowd_level="low",
                opening_hours=hours,
                hours_source="Тестовые часы работы",
                is_mock=False,
            )
            for index in range(6)
        ]

    def get_weather(self, city, start_date, end_date):
        days = []
        current = start_date
        while current <= end_date:
            days.append(
                WeatherDay(
                    day=current,
                    condition="ясно",
                    temperature_c=18,
                    precipitation_probability=5,
                    is_mock=False,
                    source="Тестовые данные",
                )
            )
            current += timedelta(days=1)
        return days

    def convert_currency(self, amount, from_currency, to_currency="RUB"):
        return amount


def test_perfect_plan_needs_no_corrections():
    result = build_corrected_plan(
        base_request(budget_amount=100000),
        provider=PerfectProvider(),
        today=TODAY,
    )

    assert result.initial_plan.issues == []
    assert result.plan.risk_score == 0
    assert result.attempts == 0
    assert result.applied == []
    assert any("не потребовались" in note for note in result.notes)


def test_mock_plan_risk_improves_after_corrections():
    result = build_corrected_plan(base_request(), provider=FixtureProvider(), today=TODAY)

    assert result.initial_plan.risk_score > result.plan.risk_score
    assert result.improved is True
    assert result.attempts >= 1
    assert result.attempts <= 3
    assert result.applied
    assert result.resolved_codes
    assert result.plan.risk_score <= result.initial_plan.risk_score
    assert result.plan.flight_out is not None and result.plan.flight_back is not None
    assert result.plan.hotel is not None
    assert result.plan.days


def test_impossible_budget_is_reported_honestly():
    result = build_corrected_plan(
        base_request(budget_amount=1000),
        provider=FixtureProvider(),
        today=TODAY,
    )

    initial_codes = {issue.code for issue in result.initial_plan.issues}
    final_codes = {issue.code for issue in result.plan.issues}
    assert "BUDGET_EXCEEDED" in initial_codes
    assert "BUDGET_EXCEEDED" in final_codes
    assert any("остались нерешённые" in note for note in result.notes)
    assert not any("проблем не осталось" in note for note in result.notes)


def test_attempts_are_bounded_by_assumptions():
    assumptions = {
        "thresholds": {"max_correction_attempts": 1},
        "ranking_weights": {},
        "cost_estimates": {},
        "risk_scoring": {},
    }
    result = build_corrected_plan(
        base_request(),
        provider=FixtureProvider(),
        today=TODAY,
        assumptions=assumptions,
    )
    assert result.attempts <= 1


def test_result_properties_are_consistent():
    result = build_corrected_plan(base_request(), provider=FixtureProvider(), today=TODAY)

    assert result.unresolved == result.plan.issues
    assert isinstance(result.applied, list)
    assert all(isinstance(label, str) and label for label in result.applied)
    assert all(isinstance(note, str) and note for note in result.notes)
