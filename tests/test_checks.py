"""Тесты детерминированных проверок Reality Check."""
from datetime import date, datetime, time

import pytest

from checks import run_reality_check
from checks.risk import calculate_risk
from models import (
    CostBreakdown,
    DayPlan,
    Flight,
    Hotel,
    Issue,
    ItineraryItem,
    OpeningHours,
    Place,
    RiskLevel,
    Severity,
    TripPlan,
    TripRequest,
    WeatherDay,
)

TODAY = date(2026, 10, 24)
_DEFAULT_HOURS = object()


def make_request(**changes) -> TripRequest:
    data = dict(
        origin_city="Москва",
        destination_city="Стамбул",
        duration_days=3,
        budget_amount=100000,
        currency="RUB",
    )
    data.update(changes)
    return TripRequest(**data)


def make_flight(
    flight_id: str = "F1",
    direction: str = "outbound",
    departure: datetime = datetime(2026, 11, 14, 8, 0),
    arrival: datetime = datetime(2026, 11, 14, 12, 0),
    **changes,
) -> Flight:
    data = dict(
        id=flight_id,
        direction=direction,
        origin="Москва",
        destination="Стамбул",
        airline="Тест-Авиа",
        departure_time=departure,
        arrival_time=arrival,
        price_rub=10000,
        source="Тестовые данные",
    )
    data.update(changes)
    return Flight(**data)


def make_hotel(**changes) -> Hotel:
    data = dict(
        id="H1",
        name="Тестовый отель",
        district="Центр",
        city="Стамбул",
        price_per_night_rub=5000,
        rating=8.5,
        minutes_to_center=10,
        minutes_to_metro=5,
        tourist_tax_per_night_rub=0,
        latitude=41.0,
        longitude=29.0,
        tags=[],
        source="Тестовые данные",
    )
    data.update(changes)
    return Hotel(**data)


def make_place(
    place_id: str = "P1",
    *,
    indoor: bool = True,
    opening_hours=_DEFAULT_HOURS,
    **changes,
) -> Place:
    if opening_hours is _DEFAULT_HOURS:
        opening_hours = OpeningHours(opens=time(9, 0), closes=time(19, 0))
    data = dict(
        id=place_id,
        name=f"Место {place_id}",
        city="Стамбул",
        categories=["музей"],
        latitude=41.0,
        longitude=29.0,
        indoor=indoor,
        visit_minutes=60,
        entrance_fee_rub=0,
        rating=8.0,
        crowd_level="low",
        opening_hours=opening_hours,
        hours_source="Тестовые часы работы",
    )
    data.update(changes)
    return Place(**data)


def make_item(
    place: Place,
    *,
    start: time = time(10, 0),
    end: time = time(11, 0),
    walking: float = 0.0,
) -> ItineraryItem:
    return ItineraryItem(
        place_id=place.id,
        name=place.name,
        start_time=start,
        end_time=end,
        walking_km_from_previous=walking,
        cost_rub=place.entrance_fee_rub,
    )


def make_plan(
    *,
    request: TripRequest | None = None,
    flight_out: Flight | None = None,
    flight_back: Flight | None = None,
    hotel: Hotel | None = None,
    days: list[DayPlan] | None = None,
    cost: CostBreakdown | None = None,
    with_base: bool = True,
) -> TripPlan:
    request = request or make_request()
    if with_base:
        flight_out = flight_out if flight_out is not None else make_flight()
        flight_back = flight_back if flight_back is not None else make_flight(
            "F2", "return", datetime(2026, 11, 16, 14, 0), datetime(2026, 11, 16, 18, 0)
        )
        hotel = hotel if hotel is not None else make_hotel()
    if days is None:
        days = [DayPlan(day_number=1, day=date(2026, 11, 14), items=[])]
    if cost is None:
        cost = CostBreakdown(flights=20000, accommodation=10000, local_transport=1800)
    return TripPlan(
        request=request,
        flight_out=flight_out,
        flight_back=flight_back,
        hotel=hotel,
        days=days,
        cost=cost,
    )


def codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


# ---------------- Перелёты ----------------
def test_late_arrival_is_high_severity():
    flight = make_flight(
        departure=datetime(2026, 11, 14, 23, 10),
        arrival=datetime(2026, 11, 15, 3, 0),
    )
    result = run_reality_check(make_plan(flight_out=flight))

    assert "LATE_ARRIVAL" in codes(result)
    issue = next(issue for issue in result.issues if issue.code == "LATE_ARRIVAL")
    assert issue.severity is Severity.HIGH
    assert "03:00" in issue.description
    assert "ночное время" in issue.description
    assert issue.recommendation


def test_late_arrival_boundary():
    exactly_five = make_flight(
        departure=datetime(2026, 11, 15, 1, 0),
        arrival=datetime(2026, 11, 15, 5, 0),
    )
    assert "LATE_ARRIVAL" not in codes(run_reality_check(make_plan(flight_out=exactly_five)))

    almost_five = make_flight(
        departure=datetime(2026, 11, 15, 1, 0),
        arrival=datetime(2026, 11, 15, 4, 59),
    )
    assert "LATE_ARRIVAL" in codes(run_reality_check(make_plan(flight_out=almost_five)))


def test_short_international_layover():
    flight = make_flight(
        departure=datetime(2026, 11, 14, 6, 0),
        arrival=datetime(2026, 11, 14, 15, 30),
        stops=1,
        layover_minutes=[50],
        connection_type="international",
    )
    result = run_reality_check(make_plan(flight_out=flight))

    assert "SHORT_LAYOVER" in codes(result)
    issue = next(issue for issue in result.issues if issue.code == "SHORT_LAYOVER")
    assert issue.severity is Severity.HIGH
    assert "50 мин" in issue.description
    assert "120 мин" in issue.description


@pytest.mark.parametrize(
    ("connection_type", "minutes", "expected"),
    [
        ("domestic", 60, False),
        ("domestic", 59, True),
        ("international", 90, True),
        ("international", 120, False),
    ],
)
def test_layover_thresholds_depend_on_connection_type(connection_type, minutes, expected):
    flight = make_flight(
        stops=1,
        layover_minutes=[minutes],
        connection_type=connection_type,
    )
    assert ("SHORT_LAYOVER" in codes(run_reality_check(make_plan(flight_out=flight)))) is expected


# ---------------- Отель ----------------
def test_hotel_far_from_center_and_metro():
    hotel = make_hotel(minutes_to_center=70, minutes_to_metro=25)
    result = run_reality_check(make_plan(hotel=hotel))

    assert {"HOTEL_FAR_FROM_CENTER", "HOTEL_FAR_FROM_METRO"} <= codes(result)
    for issue in result.issues:
        if issue.code.startswith("HOTEL_"):
            assert issue.severity is Severity.MEDIUM


def test_hotel_within_limits_has_no_issues():
    hotel = make_hotel(minutes_to_center=40, minutes_to_metro=15)
    result = run_reality_check(make_plan(hotel=hotel))
    assert not {"HOTEL_FAR_FROM_CENTER", "HOTEL_FAR_FROM_METRO"} & codes(result)


# ---------------- Стоимость и бюджет ----------------
def test_budget_overrun_severity_depends_on_percent():
    high = run_reality_check(
        make_plan(
            request=make_request(budget_amount=70000),
            cost=CostBreakdown(flights=100000),
        )
    )
    high_issue = next(issue for issue in high.issues if issue.code == "BUDGET_EXCEEDED")
    assert high_issue.severity is Severity.HIGH

    medium = run_reality_check(
        make_plan(
            request=make_request(budget_amount=90000),
            cost=CostBreakdown(flights=100000),
        )
    )
    medium_issue = next(issue for issue in medium.issues if issue.code == "BUDGET_EXCEEDED")
    assert medium_issue.severity is Severity.MEDIUM


def test_budget_currency_conversion():
    assumptions = {"fx_to_rub": {"rates": {"RUB": 1.0, "USD": 95.0}}}
    fit = run_reality_check(
        make_plan(
            request=make_request(budget_amount=1000, currency="USD"),
            cost=CostBreakdown(flights=90000),
        ),
        assumptions=assumptions,
    )
    assert "BUDGET_EXCEEDED" not in codes(fit)
    assert any("пересчитан" in note for note in fit.notes)

    over = run_reality_check(
        make_plan(
            request=make_request(budget_amount=1000, currency="USD"),
            cost=CostBreakdown(flights=100000),
        ),
        assumptions=assumptions,
    )
    assert "BUDGET_EXCEEDED" in codes(over)


def test_budget_per_person_is_multiplied_by_travelers():
    request = make_request(
        budget_amount=80000,
        budget_scope="person",
        adults=2,
        children=0,
    )
    fit = run_reality_check(
        make_plan(request=request, cost=CostBreakdown(flights=150000))
    )
    assert "BUDGET_EXCEEDED" not in codes(fit)
    assert any("на одного путешественника" in note for note in fit.notes)
    assert any("160 000" in note for note in fit.notes)

    over = run_reality_check(
        make_plan(request=request, cost=CostBreakdown(flights=170000))
    )
    assert "BUDGET_EXCEEDED" in codes(over)


def test_unknown_currency_skips_budget_with_note():
    result = run_reality_check(
        make_plan(
            request=make_request(budget_amount=1000, currency="GBP"),
            cost=CostBreakdown(flights=100000),
        ),
        assumptions={"fx_to_rub": {"rates": {"RUB": 1.0}}},
    )
    assert "BUDGET_EXCEEDED" not in codes(result)
    assert any("GBP" in note for note in result.notes)


def test_incomplete_cost_is_reported():
    plan = make_plan(with_base=False, cost=CostBreakdown(local_transport=1000))
    result = run_reality_check(plan)
    assert "COST_INCOMPLETE" in codes(result)


# ---------------- Нагрузка дня ----------------
def test_day_walking_overload():
    place = make_place()
    items = [make_item(place, walking=6.0) for _ in range(3)]
    plan = make_plan(days=[DayPlan(day_number=1, day=date(2026, 11, 14), items=items)])
    result = run_reality_check(plan, places=[place])

    assert "DAY_OVERLOADED" in codes(result)
    issue = next(issue for issue in result.issues if issue.code == "DAY_OVERLOADED")
    assert "18.0 км" in issue.description


def test_day_walking_at_limit_is_not_overloaded():
    place = make_place()
    items = [make_item(place, walking=3.0) for _ in range(5)]
    plan = make_plan(days=[DayPlan(day_number=1, day=date(2026, 11, 14), items=items)])
    assert "DAY_OVERLOADED" not in codes(run_reality_check(plan, places=[place]))


def test_day_attractions_overload():
    places = [make_place(f"P{index}") for index in range(6)]
    items = [make_item(place) for place in places]
    plan = make_plan(days=[DayPlan(day_number=1, day=date(2026, 11, 14), items=items)])
    result = run_reality_check(plan, places=places)
    assert "DAY_OVERLOADED" in codes(result)


# ---------------- Часы работы ----------------
def test_place_closed_on_weekday():
    # 17.11.2026 — вторник.
    place = make_place(
        "P_CLOSED",
        opening_hours=OpeningHours(
            opens=time(9, 0), closes=time(18, 0), closed_weekdays=[1]
        ),
    )
    item = make_item(place)
    plan = make_plan(days=[DayPlan(day_number=1, day=date(2026, 11, 17), items=[item])])
    result = run_reality_check(plan, places=[place])

    assert "PLACE_CLOSED" in codes(result)
    issue = next(issue for issue in result.issues if issue.code == "PLACE_CLOSED")
    assert issue.severity is Severity.HIGH
    assert "по вторникам" in issue.description


def test_opening_hours_conflict():
    place = make_place(
        "P_HOURS",
        opening_hours=OpeningHours(opens=time(9, 0), closes=time(18, 0)),
    )
    item = make_item(place, start=time(17, 30), end=time(19, 0))
    plan = make_plan(days=[DayPlan(day_number=1, day=date(2026, 11, 16), items=[item])])
    result = run_reality_check(plan, places=[place])

    assert "OPENING_HOURS_CONFLICT" in codes(result)
    issue = next(issue for issue in result.issues if issue.code == "OPENING_HOURS_CONFLICT")
    assert issue.severity is Severity.MEDIUM


def test_unknown_opening_hours_is_not_a_violation():
    place = make_place("P_UNKNOWN", opening_hours=None)
    item = make_item(place, start=time(22, 0), end=time(23, 0))
    plan = make_plan(days=[DayPlan(day_number=1, day=date(2026, 11, 16), items=[item])])
    result = run_reality_check(plan, places=[place])

    assert not {"PLACE_CLOSED", "OPENING_HOURS_CONFLICT"} & codes(result)
    assert any("Часы работы неизвестны" in note for note in result.notes)


# ---------------- Погода ----------------
def _rainy_day(place: Place) -> DayPlan:
    return DayPlan(
        day_number=1,
        day=date(2026, 11, 15),
        items=[make_item(place)],
        weather=WeatherDay(
            day=date(2026, 11, 15),
            condition="дождь",
            temperature_c=13,
            precipitation_probability=80,
            source="Тестовый прогноз",
        ),
    )


def test_weather_conflict_for_outdoor_place():
    place = make_place("P_OUT", indoor=False)
    result = run_reality_check(make_plan(days=[_rainy_day(place)]), places=[place])

    assert "WEATHER_CONFLICT" in codes(result)
    issue = next(issue for issue in result.issues if issue.code == "WEATHER_CONFLICT")
    assert issue.severity is Severity.MEDIUM
    assert "дождь" in issue.description


def test_weather_is_fine_for_indoor_place():
    place = make_place("P_IN", indoor=True)
    result = run_reality_check(make_plan(days=[_rainy_day(place)]), places=[place])
    assert "WEATHER_CONFLICT" not in codes(result)


# ---------------- Риск и устойчивость ----------------
def _issue(severity: Severity) -> Issue:
    return Issue(
        code=f"TEST_{severity.value}",
        severity=severity,
        description="тест",
        source="тест",
        recommendation="тест",
    )


def test_risk_scoring_levels():
    scoring = {
        "points_by_severity": {"low": 1, "medium": 3, "high": 6},
        "medium_level_from": 3,
        "high_level_from": 7,
    }
    assert calculate_risk([], scoring) == (0, RiskLevel.LOW)
    assert calculate_risk([_issue(Severity.LOW)], scoring) == (1, RiskLevel.LOW)
    assert calculate_risk([_issue(Severity.MEDIUM)], scoring) == (3, RiskLevel.MEDIUM)
    assert calculate_risk([_issue(Severity.HIGH)], scoring) == (6, RiskLevel.MEDIUM)
    assert calculate_risk([_issue(Severity.HIGH), _issue(Severity.LOW)], scoring) == (
        7,
        RiskLevel.HIGH,
    )


def test_issues_are_sorted_high_first():
    hotel = make_hotel(minutes_to_center=70)
    flight = make_flight(
        departure=datetime(2026, 11, 14, 23, 10),
        arrival=datetime(2026, 11, 15, 3, 0),
    )
    result = run_reality_check(make_plan(flight_out=flight, hotel=hotel))
    assert result.issues[0].severity is Severity.HIGH


def test_missing_place_data_does_not_crash():
    place = make_place()
    plan = make_plan(days=[DayPlan(day_number=1, day=date(2026, 11, 14), items=[make_item(place)])])
    result = run_reality_check(plan, places=[])
    assert any("Нет данных о местах" in note for note in result.notes)


def test_unknown_hotel_distances_do_not_create_false_issues():
    hotel = make_hotel(
        minutes_to_center=999,
        minutes_to_metro=999,
        center_known=False,
        metro_known=False,
    )
    result = run_reality_check(make_plan(hotel=hotel))
    assert not {"HOTEL_FAR_FROM_CENTER", "HOTEL_FAR_FROM_METRO"} & codes(result)
