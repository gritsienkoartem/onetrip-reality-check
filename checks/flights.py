"""Проверки транспорта: ночное прибытие и короткие пересадки."""
from __future__ import annotations

from models import Flight, Issue, NO_DATA, Severity, TripPlan

from checks.formatting import format_date, format_minutes, format_time

_MODE_WORDS = {
    "flight": ("Рейс", "прилетает"),
    "train": ("Поезд", "прибывает"),
    "bus": ("Автобус", "прибывает"),
}


def _source(flight: Flight) -> str:
    return flight.source or NO_DATA


def _mode_words(flight: Flight) -> tuple[str, str]:
    return _MODE_WORDS.get(flight.mode, _MODE_WORDS["flight"])


def _late_arrival_issue(flight: Flight, thresholds: dict) -> Issue | None:
    start_hour = int(thresholds.get("late_arrival_start_hour", 0))
    end_hour = int(thresholds.get("late_arrival_end_hour", 5))
    arrival_hour = flight.arrival_time.hour

    if start_hour <= end_hour:
        is_late = start_hour <= arrival_hour < end_hour
    else:  # интервал переходит через полночь
        is_late = arrival_hour >= start_hour or arrival_hour < end_hour
    if not is_late:
        return None

    kind, verb = _mode_words(flight)
    return Issue(
        code="LATE_ARRIVAL",
        severity=Severity.HIGH,
        description=(
            f"{kind} {flight.id} ({flight.airline}) {verb} в {flight.destination} "
            f"{format_date(flight.arrival_time.date())} в {format_time(flight.arrival_time.time())}. "
            f"Это ночное время ({start_hour:02d}:00–{end_hour:02d}:00): заселение в отель, "
            "трансфер и первый день будут сложнее."
        ),
        source=_source(flight),
        recommendation=(
            f"Выберите {kind.lower()} с прибытием днём (после ночного интервала) или заранее "
            "договоритесь о позднем заселении и ночном трансфере."
        ),
    )


def _layover_threshold(flight: Flight, thresholds: dict) -> tuple[int, str]:
    limits = thresholds.get("min_layover_minutes") or {}
    domestic = int(limits.get("domestic", 60))
    international = int(limits.get("international", 120))

    if flight.connection_type == "domestic":
        return domestic, "внутреннего перелёта"
    if flight.connection_type == "international":
        return international, "международного перелёта"
    # Если тип пересадки не указан, берём более строгий порог.
    return max(domestic, international), "перелёта с неизвестным типом пересадки"


def _short_layover_issue(flight: Flight, thresholds: dict) -> Issue | None:
    if flight.mode != "flight" or not flight.layover_minutes:
        return None

    threshold, connection_label = _layover_threshold(flight, thresholds)
    short = [minutes for minutes in flight.layover_minutes if minutes < threshold]
    if not short:
        return None

    minutes_text = ", ".join(format_minutes(value) for value in short)
    return Issue(
        code="SHORT_LAYOVER",
        severity=Severity.HIGH,
        description=(
            f"В рейсе {flight.id} ({flight.airline}) пересадка {minutes_text} — меньше "
            f"минимума {format_minutes(threshold)} для {connection_label}. "
            "Есть риск опоздать на стыковочный рейс."
        ),
        source=_source(flight),
        recommendation=(
            f"Выберите рейс с пересадкой не короче {format_minutes(threshold)} "
            "или другой маршрут без рискованной стыковки."
        ),
    )


def check_flights(plan: TripPlan, assumptions: dict) -> list[Issue]:
    """Проверяет выбранные рейсы туда и обратно."""
    thresholds = assumptions.get("thresholds", {})
    issues: list[Issue] = []

    if plan.flight_out is not None:
        late = _late_arrival_issue(plan.flight_out, thresholds)
        if late is not None:
            issues.append(late)

    for flight in (plan.flight_out, plan.flight_back):
        if flight is None:
            continue
        layover = _short_layover_issue(flight, thresholds)
        if layover is not None:
            issues.append(layover)

    return issues
