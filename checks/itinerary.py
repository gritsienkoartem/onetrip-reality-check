"""Проверки маршрута: перегрузка дня, часы работы и погода."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import date

from models import DayPlan, Issue, ItineraryItem, NO_DATA, Place, Severity, TripPlan, WeatherDay

from checks.formatting import format_date, format_time

WEEKDAY_NAMES = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
WEEKDAY_CLOSED = (
    "по понедельникам",
    "по вторникам",
    "по средам",
    "по четвергам",
    "по пятницам",
    "по субботам",
    "по воскресеньям",
)
BAD_WEATHER_MARKERS = ("дождь", "ливень", "снег", "гроза", "град", "шторм", "метель")


def is_bad_weather(weather: WeatherDay) -> bool:
    """Дождь, снег, гроза или высокая вероятность осадков."""
    condition = weather.condition.casefold().replace("ё", "е")
    if any(marker in condition for marker in BAD_WEATHER_MARKERS):
        return True
    probability = weather.precipitation_probability
    return probability is not None and probability >= 60


def _day_title(day: DayPlan) -> str:
    if day.day is not None:
        return f"День {day.day_number} ({format_date(day.day)})"
    return f"День {day.day_number}"


def _outdoor_items(day: DayPlan, places_by_id: Mapping[str, Place]) -> list[ItineraryItem]:
    result: list[ItineraryItem] = []
    for item in day.items:
        place = places_by_id.get(item.place_id)
        if place is not None and not place.indoor:
            result.append(item)
    return result


def iter_opening_issues(
    plan: TripPlan,
    places_by_id: Mapping[str, Place],
) -> Iterator[tuple[Issue, str]]:
    """Возвращает проблемы часов работы вместе с id места."""
    for day in plan.days:
        for item in day.items:
            place = places_by_id.get(item.place_id)
            if place is None:
                continue
            hours = place.opening_hours
            if hours is None:
                continue  # «Нет данных» — это не «закрыто»

            if day.day is not None and day.day.weekday() in hours.closed_weekdays:
                weekday_index = day.day.weekday()
                weekday = WEEKDAY_NAMES[weekday_index]
                yield (
                    Issue(
                        code="PLACE_CLOSED",
                        severity=Severity.HIGH,
                        description=(
                            f"«{item.name}» не работает {WEEKDAY_CLOSED[weekday_index]}, "
                            f"а посещение запланировано на {format_date(day.day)} ({weekday})."
                        ),
                        source=place.hours_source or NO_DATA,
                        recommendation=(
                            "Перенесите посещение на другой день или замените место "
                            "на доступное в этот день."
                        ),
                    ),
                    place.id,
                )
                continue

            if item.start_time is None or item.end_time is None:
                continue
            if item.start_time < hours.opens or item.end_time > hours.closes:
                yield (
                    Issue(
                        code="OPENING_HOURS_CONFLICT",
                        severity=Severity.MEDIUM,
                        description=(
                            f"«{item.name}» работает {format_time(hours.opens)}–"
                            f"{format_time(hours.closes)}, а посещение запланировано на "
                            f"{format_time(item.start_time)}–{format_time(item.end_time)}"
                            + (f" ({format_date(day.day)})" if day.day is not None else "")
                            + "."
                        ),
                        source=place.hours_source or NO_DATA,
                        recommendation=(
                            "Сдвиньте посещение в часы работы или выберите другое место."
                        ),
                    ),
                    place.id,
                )


def check_opening_hours(
    plan: TripPlan,
    places_by_id: Mapping[str, Place],
    notes: list[str],
) -> list[Issue]:
    issues = [issue for issue, _ in iter_opening_issues(plan, places_by_id)]

    unknown: list[str] = []
    missing: list[str] = []
    for day in plan.days:
        for item in day.items:
            place = places_by_id.get(item.place_id)
            if place is None:
                missing.append(item.name)
            elif place.opening_hours is None:
                unknown.append(item.name)

    unknown_names = list(dict.fromkeys(unknown))
    if unknown_names:
        preview = ", ".join(f"«{name}»" for name in unknown_names[:3])
        if len(unknown_names) > 3:
            preview += f" и ещё {len(unknown_names) - 3}"
        notes.append(
            f"Часы работы неизвестны для некоторых мест: {preview}. "
            "Это не считается нарушением, но их стоит проверить перед поездкой."
        )

    if missing:
        notes.append("Не удалось проверить часы работы: для части пунктов нет данных о самом месте.")

    return issues


def check_day_load(plan: TripPlan, assumptions: dict) -> list[Issue]:
    """Проверяет пешую нагрузку и число мест в каждом дне."""
    thresholds = assumptions.get("thresholds", {})
    max_walking = float(thresholds.get("max_walking_km_per_day", 15))
    max_attractions = int(thresholds.get("max_attractions_per_day", 5))
    issues: list[Issue] = []

    for day in plan.days:
        walking = day.total_walking_km
        count = len(day.items)
        if walking <= max_walking and count <= max_attractions:
            continue

        parts: list[str] = []
        if walking > max_walking:
            parts.append(f"пешком {walking:.1f} км при лимите {max_walking:g} км")
        if count > max_attractions:
            parts.append(f"{count} мест при лимите {max_attractions}")
        issues.append(
            Issue(
                code="DAY_OVERLOADED",
                severity=Severity.MEDIUM,
                description=f"{_day_title(day)}: " + " и ".join(parts) + ".",
                source="Расчёт по плану (оценка проекта)",
                recommendation=(
                    "Сократите число мест или перенесите часть на другой день; "
                    "для длинных переездов используйте транспорт вместо ходьбы."
                ),
            )
        )

    return issues


def iter_weather_issues(
    plan: TripPlan,
    places_by_id: Mapping[str, Place],
) -> Iterator[Issue]:
    for day in plan.days:
        weather = day.weather
        if day.day is None or weather is None or not is_bad_weather(weather):
            continue

        outdoor = _outdoor_items(day, places_by_id)
        if not outdoor:
            continue

        names = ", ".join(f"«{item.name}»" for item in outdoor[:3])
        if len(outdoor) > 3:
            names += f" и ещё {len(outdoor) - 3}"
        probability = weather.precipitation_probability
        probability_text = (
            f", вероятность осадков {probability}%" if probability is not None else ""
        )
        yield Issue(
            code="WEATHER_CONFLICT",
            severity=Severity.MEDIUM,
            description=(
                f"На {format_date(day.day)} прогноз «{weather.condition}»{probability_text}, "
                f"а в плане уличные места: {names}."
            ),
            source=weather.source or NO_DATA,
            recommendation=(
                "Замените уличные места на помещения или перенесите их на день "
                "с лучшей погодой."
            ),
        )


def check_weather(plan: TripPlan, places_by_id: Mapping[str, Place]) -> list[Issue]:
    return list(iter_weather_issues(plan, places_by_id))


def weather_conflict_dates(plan: TripPlan, places_by_id: Mapping[str, Place]) -> set[date]:
    """Даты, где плохая погода совпала с уличными местами."""
    dates: set[date] = set()
    for day in plan.days:
        if day.day is None or day.weather is None:
            continue
        if is_bad_weather(day.weather) and _outdoor_items(day, places_by_id):
            dates.add(day.day)
    return dates


def check_itinerary(
    plan: TripPlan,
    places_by_id: Mapping[str, Place],
    assumptions: dict,
    notes: list[str],
) -> list[Issue]:
    issues = check_day_load(plan, assumptions)
    if places_by_id:
        issues.extend(check_opening_hours(plan, places_by_id, notes))
        issues.extend(check_weather(plan, places_by_id))
    elif any(day.items for day in plan.days):
        notes.append("Нет данных о местах: проверки часов работы и погоды пропущены.")
    return issues
