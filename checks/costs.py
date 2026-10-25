"""Проверки стоимости: полнота итоговой суммы и превышение бюджета."""
from __future__ import annotations

from models import Issue, Severity, TripPlan

from checks.formatting import format_rub


def _budget_in_rub(plan: TripPlan, assumptions: dict, notes: list[str]) -> float | None:
    """Пересчитывает бюджет пользователя в рубли по условным курсам проекта."""
    budget = plan.request.budget_amount
    if budget is None:
        notes.append("Бюджет не указан — проверка превышения бюджета не выполнялась.")
        return None

    currency = plan.request.currency
    if currency == "RUB":
        converted = float(budget)
    else:
        fx = assumptions.get("fx_to_rub") or {}
        rates = fx.get("rates") or {}
        rate = rates.get(currency)
        if not isinstance(rate, (int, float)) or rate <= 0:
            notes.append(
                f"Бюджет в валюте {currency} не проверен: в assumptions.json нет корректного курса."
            )
            return None

        converted = float(budget) * float(rate)
        notes.append(
            f"Бюджет {budget:,.0f} {currency} пересчитан в рубли по условному курсу "
            f"{rate:g} ₽ за 1 {currency}: {format_rub(converted)}."
        )

    if plan.request.budget_scope == "person":
        travelers = max(1, plan.request.travelers)
        total = converted * travelers
        notes.append(
            f"Бюджет указан на одного путешественника: для группы из {travelers} чел. "
            f"в расчёте {format_rub(total)}. Это арифметика бюджета, а не цена за человека."
        )
        converted = total
    return converted


def check_costs(plan: TripPlan, assumptions: dict, notes: list[str]) -> list[Issue]:
    """Проверяет, что сумма полная и укладывается в бюджет."""
    issues: list[Issue] = []
    cost = plan.cost

    if cost is None:
        issues.append(
            Issue(
                code="COST_MISSING",
                severity=Severity.HIGH,
                description="Итоговая стоимость поездки не рассчитана.",
                source="Оценка проекта",
                recommendation="Пересоберите план: без стоимости нельзя проверить бюджет.",
            )
        )
        return issues

    if cost.is_estimate:
        notes.append("Итоговая стоимость — оценка проекта, а не подтверждённые цены.")

    missing: list[str] = []
    if (
        plan.request.origin_city
        and plan.request.destination_city
        and not (plan.flight_out and plan.flight_back)
    ):
        missing.append("перелёт туда и обратно")
    if plan.request.destination_city and plan.hotel is None:
        missing.append("проживание")

    if missing:
        issues.append(
            Issue(
                code="COST_INCOMPLETE",
                severity=Severity.MEDIUM,
                description=(
                    "В итоговую стоимость не входят: "
                    + ", ".join(missing)
                    + ". Итог будет занижен, поэтому бюджету нельзя доверять."
                ),
                source="Оценка проекта",
                recommendation=(
                    "Добавьте недостающие варианты перелёта и проживания "
                    "или явно считайте план неполным."
                ),
            )
        )

    if cost.total <= 0:
        issues.append(
            Issue(
                code="COST_MISSING",
                severity=Severity.HIGH,
                description="Итоговая стоимость равна нулю: в плане нет ни одного расхода.",
                source="Оценка проекта",
                recommendation="Проверьте, что выбраны рейсы, отель и места.",
            )
        )

    budget_rub = _budget_in_rub(plan, assumptions, notes)
    if budget_rub is None:
        return issues

    total = cost.total
    if total <= budget_rub:
        return issues

    overrun = total - budget_rub
    percent = overrun / budget_rub * 100 if budget_rub > 0 else 0.0
    severity = Severity.HIGH if percent > 20 else Severity.MEDIUM
    issues.append(
        Issue(
            code="BUDGET_EXCEEDED",
            severity=severity,
            description=(
                f"Оценка итоговой стоимости {format_rub(total)} превышает бюджет "
                f"{format_rub(budget_rub)} на {format_rub(overrun)} ({percent:.0f}%)."
            ),
            source="Оценка проекта (не подтверждённая цена)",
            recommendation=(
                "Сократите расходы: выберите более дешёвый отель или рейс, "
                "уменьшите число платных мест. Если бюджет нельзя увеличить, "
                "рассмотрите другие даты."
            ),
        )
    )
    return issues
