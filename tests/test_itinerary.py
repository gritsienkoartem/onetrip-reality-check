"""Тесты сборки маршрута по дням."""
from datetime import date, datetime, time

from agent.itinerary import build_days
from models import Flight, Hotel, Place, WeatherDay


def make_hotel(latitude: float = 41.0, longitude: float = 29.0) -> Hotel:
    return Hotel(
        id="H1",
        name="Тестовый отель",
        district="Центр",
        city="Стамбул",
        price_per_night_rub=5000,
        rating=8.5,
        minutes_to_center=10,
        minutes_to_metro=5,
        tourist_tax_per_night_rub=0,
        latitude=latitude,
        longitude=longitude,
    )


def make_place(place_id: str, latitude: float = 41.0, longitude: float = 29.0, minutes: int = 60) -> Place:
    return Place(
        id=place_id,
        name=f"Место {place_id}",
        city="Стамбул",
        categories=["музей"],
        latitude=latitude,
        longitude=longitude,
        indoor=True,
        visit_minutes=minutes,
        entrance_fee_rub=100,
        rating=8.0,
        crowd_level="low",
    )


def make_flight(direction: str, departure: datetime, arrival: datetime) -> Flight:
    return Flight(
        id=f"F_{direction}",
        direction=direction,
        origin="Москва",
        destination="Стамбул",
        airline="Тест",
        departure_time=departure,
        arrival_time=arrival,
        price_rub=1000,
    )


def test_places_are_distributed_without_duplicates():
    places = [make_place(f"P{index}") for index in range(6)]

    days = build_days(places, date(2026, 11, 14), 2, hotel=make_hotel())

    assert len(days) == 2
    assert [day.day_number for day in days] == [1, 2]
    assert [len(day.items) for day in days] == [4, 2]
    ids = [item.place_id for day in days for item in day.items]
    assert len(ids) == len(set(ids))
    assert all(item.start_time <= item.end_time for day in days for item in day.items)


def test_first_day_starts_after_arrival_and_transfer():
    flight = make_flight(
        "outbound",
        datetime(2026, 11, 14, 8, 0),
        datetime(2026, 11, 14, 14, 0),
    )

    day = build_days(
        [make_place("P1")],
        date(2026, 11, 14),
        1,
        hotel=make_hotel(),
        flight_out=flight,
    )[0]

    assert day.items[0].start_time >= time(15, 0)


def test_return_flight_limits_last_day():
    flight = make_flight(
        "return",
        datetime(2026, 11, 16, 13, 0),
        datetime(2026, 11, 16, 17, 0),
    )

    day = build_days(
        [make_place("P1")],
        date(2026, 11, 16),
        1,
        hotel=make_hotel(),
        flight_back=flight,
    )[0]

    assert day.items
    assert day.items[0].end_time <= time(11, 0)


def test_weather_is_attached_to_matching_day():
    weather = WeatherDay(
        day=date(2026, 11, 14),
        condition="дождь",
        temperature_c=12,
        precipitation_probability=80,
    )

    day = build_days([], date(2026, 11, 14), 1, weather=[weather])[0]

    assert day.weather is not None
    assert day.weather.condition == "дождь"


def test_walking_distance_from_hotel_is_counted():
    day = build_days(
        [make_place("P1", latitude=41.02)],
        date(2026, 11, 14),
        1,
        hotel=make_hotel(),
    )[0]

    assert day.items[0].walking_km_from_previous > 0
    assert day.items[0].start_time > time(9, 0)
    assert day.total_walking_km == day.items[0].walking_km_from_previous


def test_empty_places_still_produce_days():
    days = build_days([], date(2026, 11, 14), 2, hotel=make_hotel())
    assert len(days) == 2
    assert all(day.items == [] for day in days)


def test_max_places_per_day_is_respected():
    places = [make_place(f"P{index}") for index in range(10)]
    day = build_days(places, date(2026, 11, 14), 1, hotel=make_hotel())[0]
    assert len(day.items) == 4
