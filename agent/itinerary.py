"""Сборка плана по дням из уже отсортированных мест.

Планировщик следит за временем посещения, перелётами и длиной дня.
Часы работы, погоду и перегрузку проверяет Reality Check; при
исправлении плана сюда можно передать дополнительные ограничения.
"""
from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from datetime import date, datetime, time, timedelta

from models import DayPlan, Flight, Hotel, ItineraryItem, Place, WeatherDay

DAY_START = time(9, 0)
DAY_END = time(19, 0)
MAX_PLACES_PER_DAY = 4
WALKING_SPEED_KMH = 4.5
TRANSFER_MINUTES = 60
AIRPORT_BUFFER_MINUTES = 120


def haversine_km(latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float) -> float:
    """Расстояние по прямой между двумя точками в километрах."""
    radius_km = 6371.0
    phi_a = math.radians(latitude_a)
    phi_b = math.radians(latitude_b)
    delta_phi = math.radians(latitude_b - latitude_a)
    delta_lambda = math.radians(longitude_b - longitude_a)
    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * radius_km * math.asin(math.sqrt(value))


def _travel_minutes(distance_km: float) -> int:
    if distance_km <= 0:
        return 0
    return max(1, math.ceil(distance_km / WALKING_SPEED_KMH * 60))


def _day_bounds(
    day_date: date | None,
    flight_out: Flight | None,
    flight_back: Flight | None,
) -> tuple[datetime, datetime]:
    """Границы активной части дня с учётом прилёта и вылета."""
    base_date = day_date or date(2000, 1, 1)
    start = datetime.combine(base_date, DAY_START)
    end = datetime.combine(base_date, DAY_END)

    if day_date is None:
        return start, end

    if flight_out is not None:
        arrival_date = flight_out.arrival_time.date()
        if day_date < arrival_date:
            return end, end  # до прилёта в этом дне активностей нет
        if day_date == arrival_date:
            start = max(start, flight_out.arrival_time + timedelta(minutes=TRANSFER_MINUTES))

    if flight_back is not None:
        departure_date = flight_back.departure_time.date()
        if day_date > departure_date:
            return end, end  # после вылета активностей нет
        if day_date == departure_date:
            end = min(end, flight_back.departure_time - timedelta(minutes=AIRPORT_BUFFER_MINUTES))

    return start, end


def build_days(
    places: Sequence[Place],
    start_date: date | None,
    duration_days: int,
    hotel: Hotel | None = None,
    flight_out: Flight | None = None,
    flight_back: Flight | None = None,
    weather: Sequence[WeatherDay] = (),
    *,
    max_places_per_day: int = MAX_PLACES_PER_DAY,
    prefer_indoor_dates: Collection[date] = (),
    max_walking_km_per_day: float | None = None,
) -> list[DayPlan]:
    """Распределяет места по дням, сохраняя порядок ранжирования.

    prefer_indoor_dates — даты, когда уличные места уступают место
    помещениям (используется при исправлении погодных конфликтов).
    max_walking_km_per_day — необязательный жёсткий лимит ходьбы.
    """
    weather_by_day: Mapping[date, WeatherDay] = {item.day: item for item in weather}
    prefer_indoor = set(prefer_indoor_dates)
    limit = max(1, int(max_places_per_day))
    used_ids: set[str] = set()
    days: list[DayPlan] = []

    for day_index in range(max(1, duration_days)):
        day_date = start_date + timedelta(days=day_index) if start_date else None
        day_start, day_end = _day_bounds(day_date, flight_out, flight_back)

        day_places: Sequence[Place] = places
        if day_date is not None and day_date in prefer_indoor:
            day_places = [place for place in places if place.indoor] + [
                place for place in places if not place.indoor
            ]

        items: list[ItineraryItem] = []
        current_time = day_start
        walking_total = 0.0
        current_point: tuple[float, float] | None = (
            (hotel.latitude, hotel.longitude) if hotel is not None else None
        )

        if day_start < day_end:
            for place in day_places:
                if len(items) >= limit:
                    break
                if place.id in used_ids:
                    continue

                distance = 0.0
                if current_point is not None:
                    distance = haversine_km(
                        current_point[0], current_point[1], place.latitude, place.longitude
                    )
                if (
                    max_walking_km_per_day is not None
                    and walking_total + distance > max_walking_km_per_day
                ):
                    continue

                arrival = current_time + timedelta(minutes=_travel_minutes(distance))
                finish = arrival + timedelta(minutes=place.visit_minutes)
                if finish > day_end:
                    continue

                items.append(
                    ItineraryItem(
                        place_id=place.id,
                        name=place.name,
                        start_time=arrival.time(),
                        end_time=finish.time(),
                        walking_km_from_previous=round(distance, 2),
                        cost_rub=place.entrance_fee_rub,
                    )
                )
                used_ids.add(place.id)
                walking_total += distance
                current_time = finish
                current_point = (place.latitude, place.longitude)

        days.append(
            DayPlan(
                day_number=day_index + 1,
                day=day_date,
                items=items,
                weather=weather_by_day.get(day_date) if day_date else None,
            )
        )

    return days
