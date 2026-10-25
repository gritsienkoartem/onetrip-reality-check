"""Главная точка входа Reality Check: запускает все правила и считает риск."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from checks.costs import check_costs
from checks.flights import check_flights
from checks.itinerary import check_itinerary
from checks.risk import calculate_risk, sort_issues
from checks.stay import check_hotel
from config import ConfigError, load_assumptions
from models import Issue, Place, RiskLevel, Severity, TripPlan

DESTINATION_UNSUPPORTED = "DESTINATION_UNSUPPORTED"


@dataclass(frozen=True)
class RealityCheckResult:
    """Результат проверки: проблемы, очки риска и пояснения."""

    issues: list[Issue]
    risk_score: int
    risk_level: RiskLevel
    notes: list[str]


def _destination_unsupported_issue(plan: TripPlan, provider_name: str | None) -> Issue:
    destination = plan.request.destination_city or "выбранный город"
    provider = provider_name or "провайдер данных"
    return Issue(
        code=DESTINATION_UNSUPPORTED,
        severity=Severity.HIGH,
        description=(
            f"Для города «{destination}» нет данных: не найдено ни рейсов, ни отелей, "
            f"ни мест, ни прогноза погоды (провайдер: {provider})."
        ),
        source=f"Провайдер {provider}",
        recommendation=(
            "Проверьте название города и наличие подключения к открытым источникам. "
            "Если город не находится через Nominatim, выберите другой населённый пункт. "
            "Пока данных нет, план и стоимость не строятся: иначе пришлось бы выдумывать цифры."
        ),
    )


def _dedupe(issues: Sequence[Issue]) -> list[Issue]:
    unique: list[Issue] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        key = (issue.code, issue.description)
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique


def run_reality_check(
    plan: TripPlan,
    *,
    places: Sequence[Place] = (),
    assumptions: dict | None = None,
    data_available: bool = True,
    provider_name: str | None = None,
) -> RealityCheckResult:
    """Проверяет план детерминированными правилами.

    Веса и пороги берутся из assumptions.json. Если файл недоступен,
    используются значения по умолчанию, а причина попадает в notes.
    Если для города нет данных, остальные правила не применяются:
    проверять нечего, а план и стоимость не должны выглядеть правдоподобно.
    """
    notes: list[str] = []
    if assumptions is None:
        try:
            assumptions = load_assumptions()
        except ConfigError as error:
            notes.append(f"Не удалось прочитать допущения проекта: {error}. Использованы значения по умолчанию.")
            assumptions = {}

    if not data_available:
        issues = sort_issues([_destination_unsupported_issue(plan, provider_name)])
        notes.append("План не собран: для города нет данных у провайдера.")
        score, level = calculate_risk(issues, assumptions.get("risk_scoring", {}))
        return RealityCheckResult(
            issues=issues,
            risk_score=score,
            risk_level=level,
            notes=notes,
        )

    places_by_id = {place.id: place for place in places}
    issues: list[Issue] = []
    issues.extend(check_flights(plan, assumptions))
    issues.extend(check_hotel(plan, assumptions))
    issues.extend(check_costs(plan, assumptions, notes))
    issues.extend(check_itinerary(plan, places_by_id, assumptions, notes))

    issues = sort_issues(_dedupe(issues))
    score, level = calculate_risk(issues, assumptions.get("risk_scoring", {}))
    return RealityCheckResult(
        issues=issues,
        risk_score=score,
        risk_level=level,
        notes=notes,
    )
