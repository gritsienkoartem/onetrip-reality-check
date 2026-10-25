"""Проверки отеля: удалённость от центра и метро."""
from __future__ import annotations

from models import Hotel, Issue, NO_DATA, Severity, TripPlan

from checks.formatting import format_minutes


def _source(hotel: Hotel) -> str:
    return hotel.source or NO_DATA


def check_hotel(plan: TripPlan, assumptions: dict) -> list[Issue]:
    """Сравнивает расположение отеля с порогами из assumptions.json."""
    hotel = plan.hotel
    if hotel is None:
        return []

    thresholds = assumptions.get("thresholds", {})
    max_center = int(thresholds.get("max_minutes_to_center", 40))
    max_metro = int(thresholds.get("max_minutes_to_metro", 15))
    issues: list[Issue] = []

    if hotel.center_known and hotel.minutes_to_center > max_center:
        issues.append(
            Issue(
                code="HOTEL_FAR_FROM_CENTER",
                severity=Severity.MEDIUM,
                description=(
                    f"Отель «{hotel.name}» находится в {format_minutes(hotel.minutes_to_center)} "
                    f"от центра, а рекомендуемый максимум — {format_minutes(max_center)}. "
                    "Дорога будет съедать время каждый день."
                ),
                source=_source(hotel),
                recommendation=(
                    f"Выберите отель не дальше {format_minutes(max_center)} от центра "
                    "или заранее учтите ежедневные поездки в плане."
                ),
            )
        )

    if hotel.metro_known and hotel.has_metro and hotel.minutes_to_metro > max_metro:
        issues.append(
            Issue(
                code="HOTEL_FAR_FROM_METRO",
                severity=Severity.MEDIUM,
                description=(
                    f"От отеля «{hotel.name}» до метро {format_minutes(hotel.minutes_to_metro)}, "
                    f"а рекомендуемый максимум — {format_minutes(max_metro)}."
                ),
                source=_source(hotel),
                recommendation=(
                    f"Выберите отель не дальше {format_minutes(max_metro)} от метро "
                    "или проверьте наличие удобного наземного транспорта."
                ),
            )
        )

    return issues
