"""Оценка сценариев OneTrip Reality Check на тестовых фикстурах.

Запуск:
    python evaluate.py
    python evaluate.py --json
    python evaluate.py --scenario baseline

Скрипт прогоняет запросы через полный цикл «план → проверка → исправление»
на вымышленных фикстурах и сверяет результат с ожидаемыми кодами проблем.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date

from agent.corrector import CorrectionResult, build_corrected_plan
from config import ConfigError, load_assumptions
from models import Severity, TripPlan, TripRequest
from providers.fixtures import FixtureProvider

# Фиксированная дата делает прогон воспроизводимым: фикстуры расписания
# и погоды привязаны к 14–17 ноября 2026 года.
EVALUATION_TODAY = date(2026, 10, 24)


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    description: str
    request: TripRequest
    expected_initial_codes: frozenset[str]
    expected_final_codes: frozenset[str]
    expect_improved: bool


def _request(**overrides) -> TripRequest:
    data = dict(
        origin_city="Москва",
        destination_city="Стамбул",
        start_date=date(2026, 11, 14),
        end_date=date(2026, 11, 17),
        duration_days=4,
        interests=["архитектура", "кофе"],
    )
    data.update(overrides)
    return TripRequest(**data)


SCENARIOS = (
    Scenario(
        key="baseline",
        title="Базовый план",
        description="Стамбул на 4 дня без бюджета: агент сам выбирает рейсы и отель.",
        request=_request(),
        expected_initial_codes=frozenset(
            {
                "LATE_ARRIVAL",
                "PLACE_CLOSED",
                "DAY_OVERLOADED",
                "OPENING_HOURS_CONFLICT",
                "WEATHER_CONFLICT",
            }
        ),
        expected_final_codes=frozenset({"WEATHER_CONFLICT"}),
        expect_improved=True,
    ),
    Scenario(
        key="low_budget",
        title="Жёсткий бюджет",
        description="Бюджет 30 000 ₽: перерасход остаётся, но показан честно.",
        request=_request(budget_amount=30000),
        expected_initial_codes=frozenset(
            {
                "BUDGET_EXCEEDED",
                "LATE_ARRIVAL",
                "PLACE_CLOSED",
                "DAY_OVERLOADED",
                "OPENING_HOURS_CONFLICT",
                "WEATHER_CONFLICT",
            }
        ),
        expected_final_codes=frozenset({"BUDGET_EXCEEDED", "WEATHER_CONFLICT"}),
        expect_improved=True,
    ),
    Scenario(
        key="far_hotel",
        title="Далёкий хостел",
        description="Пожелание «хостел» приводит к отелю далеко от центра и метро.",
        request=_request(accommodation_preferences=["хостел"]),
        expected_initial_codes=frozenset(
            {
                "LATE_ARRIVAL",
                "DAY_OVERLOADED",
                "HOTEL_FAR_FROM_CENTER",
                "HOTEL_FAR_FROM_METRO",
                "WEATHER_CONFLICT",
            }
        ),
        expected_final_codes=frozenset({"LATE_ARRIVAL"}),
        expect_improved=True,
    ),
    Scenario(
        key="relative_dates",
        title="Примерные даты",
        description="«Середина ноября» без точных дат: план поясняет допущение.",
        request=TripRequest(
            origin_city="Москва",
            destination_city="Стамбул",
            period_description="середина ноября",
            duration_days=4,
            budget_amount=60000,
            interests=["архитектура", "кофе"],
        ),
        expected_initial_codes=frozenset(
            {
                "LATE_ARRIVAL",
                "PLACE_CLOSED",
                "DAY_OVERLOADED",
                "OPENING_HOURS_CONFLICT",
                "WEATHER_CONFLICT",
            }
        ),
        expected_final_codes=frozenset({"WEATHER_CONFLICT"}),
        expect_improved=True,
    ),
    Scenario(
        key="history",
        title="Честный отказ от исправления",
        description="Интересы «история»: доступные замены не улучшают план одним шагом.",
        request=_request(interests=["история"]),
        expected_initial_codes=frozenset({"LATE_ARRIVAL", "WEATHER_CONFLICT"}),
        expected_final_codes=frozenset({"LATE_ARRIVAL", "WEATHER_CONFLICT"}),
        expect_improved=False,
    ),
)


@dataclass
class ScenarioResult:
    scenario: Scenario
    correction: CorrectionResult
    max_attempts: int
    checks: dict[str, bool]

    @property
    def passed(self) -> bool:
        return all(self.checks.values())

    @property
    def initial_codes(self) -> set[str]:
        return _codes(self.correction.initial_plan)

    @property
    def final_codes(self) -> set[str]:
        return _codes(self.correction.plan)

    @property
    def failed_checks(self) -> list[str]:
        return [name for name, passed in self.checks.items() if not passed]

    def to_dict(self) -> dict:
        correction = self.correction
        return {
            "key": self.scenario.key,
            "title": self.scenario.title,
            "description": self.scenario.description,
            "initial": _plan_payload(correction.initial_plan),
            "final": _plan_payload(correction.plan),
            "attempts": correction.attempts,
            "max_attempts": self.max_attempts,
            "applied": list(correction.applied),
            "resolved_codes": correction.resolved_codes,
            "remaining_codes": sorted(self.final_codes),
            "expected_initial_codes": sorted(self.scenario.expected_initial_codes),
            "expected_final_codes": sorted(self.scenario.expected_final_codes),
            "checks": dict(self.checks),
            "failed_checks": self.failed_checks,
            "passed": self.passed,
        }


def _codes(plan: TripPlan) -> set[str]:
    return {issue.code for issue in plan.issues}


def _issue_payload(plan: TripPlan) -> list[dict]:
    return [
        {
            "code": issue.code,
            "severity": issue.severity.value,
            "severity_label_ru": issue.severity.label_ru,
            "description": issue.description,
            "source": issue.source,
            "recommendation": issue.recommendation,
        }
        for issue in plan.issues
    ]


def _plan_payload(plan: TripPlan) -> dict:
    return {
        "risk_score": plan.risk_score,
        "risk_level": plan.risk_level.name.lower() if plan.risk_level else None,
        "risk_label_ru": plan.risk_level.label_ru if plan.risk_level else None,
        "cost_rub": plan.cost.total if plan.cost is not None else None,
        "flights": [
            flight.id for flight in (plan.flight_out, plan.flight_back) if flight is not None
        ],
        "hotel": plan.hotel.id if plan.hotel is not None else None,
        "issues": _issue_payload(plan),
    }


def _honest_notes(correction: CorrectionResult) -> bool:
    """Проверяет, что итог не выдаёт оставшиеся проблемы за отсутствие проблем."""
    notes = correction.notes
    if not correction.plan.issues:
        return not any("остались нерешённые проблемы" in note for note in notes)
    mentions_remaining = any("остались нерешённые проблемы" in note for note in notes)
    false_success = any("проблем не осталось" in note for note in notes)
    return mentions_remaining and not false_success


def _complete_plan(plan: TripPlan) -> bool:
    return (
        plan.flight_out is not None
        and plan.flight_back is not None
        and plan.hotel is not None
        and any(day.items for day in plan.days)
    )


def _max_attempts(assumptions: dict) -> int:
    return max(
        1,
        min(10, int(assumptions.get("thresholds", {}).get("max_correction_attempts", 3))),
    )


def run_scenarios(keys: list[str] | None = None) -> list[ScenarioResult]:
    assumptions = load_assumptions()
    max_attempts = _max_attempts(assumptions)
    provider = FixtureProvider()
    selected = set(keys) if keys else None
    results: list[ScenarioResult] = []

    for scenario in SCENARIOS:
        if selected is not None and scenario.key not in selected:
            continue

        correction = build_corrected_plan(
            scenario.request,
            provider=provider,
            today=EVALUATION_TODAY,
            assumptions=assumptions,
        )
        initial = correction.initial_plan
        final = correction.plan
        improved = final.risk_score < initial.risk_score
        checks = {
            "initial_codes": _codes(initial) == scenario.expected_initial_codes,
            "final_codes": _codes(final) == scenario.expected_final_codes,
            "improvement": improved if scenario.expect_improved else not improved,
            "bounded_attempts": correction.attempts <= max_attempts,
            "complete_plan": _complete_plan(final),
            "honest_notes": _honest_notes(correction),
        }
        results.append(
            ScenarioResult(
                scenario=scenario,
                correction=correction,
                max_attempts=max_attempts,
                checks=checks,
            )
        )
    return results


def _risk_text(plan: TripPlan) -> str:
    if plan.risk_level is None:
        return f"не рассчитан ({plan.risk_score})"
    return f"{plan.risk_level.label_ru.lower()} ({plan.risk_score})"


def _issue_text(plan: TripPlan) -> str:
    seen: set[tuple[str, Severity]] = set()
    parts: list[str] = []
    for issue in plan.issues:
        key = (issue.code, issue.severity)
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"{issue.code} ({issue.severity.label_ru.lower()})")
    return ", ".join(parts) if parts else "нет"


_CHECK_LABELS = {
    "initial_codes": "ожидаемые начальные проблемы",
    "final_codes": "ожидаемые оставшиеся проблемы",
    "improvement": "ожидаемая динамика риска",
    "bounded_attempts": "лимит попыток исправления",
    "complete_plan": "полнота плана",
    "honest_notes": "честная формулировка итога",
}


def format_human(results: list[ScenarioResult]) -> str:
    lines = [
        "OneTrip Reality Check — оценка сценариев",
        f"Дата оценки: {EVALUATION_TODAY:%d.%m.%Y} · "
        "Провайдер: fixtures · Данные: тестовые фикстуры (вымышленные)",
        "",
        f"{'Сценарий':<34} {'Риск до':<18} {'Риск после':<18} "
        f"{'Проблемы':<12} {'Испр.':<7} Статус",
        "-" * 104,
    ]

    for item in results:
        correction = item.correction
        initial, final = correction.initial_plan, correction.plan
        problems = f"{len(initial.issues)} → {len(final.issues)}"
        lines.append(
            f"{item.scenario.title:<34} {_risk_text(initial):<18} {_risk_text(final):<18} "
            f"{problems:<12} {correction.attempts:<7} {'OK' if item.passed else 'ОШИБКА'}"
        )

    for index, item in enumerate(results, 1):
        correction = item.correction
        initial, final = correction.initial_plan, correction.plan
        status = "OK" if item.passed else "ОШИБКА"
        lines.extend(
            [
                "",
                f"[{index}] {item.scenario.title} — {status}",
                f"    Суть: {item.scenario.description}",
                f"    Риск: {_risk_text(initial)} → {_risk_text(final)}",
                f"    Проблемы до: {_issue_text(initial)}",
                f"    Проблемы после: {_issue_text(final)}",
                f"    Исправлено: {', '.join(correction.resolved_codes) or '—'}",
                f"    Применено: {'; '.join(correction.applied) or '—'}",
                f"    Попыток: {correction.attempts} из {item.max_attempts}",
            ]
        )
        if item.failed_checks:
            labels = ", ".join(_CHECK_LABELS[name] for name in item.failed_checks)
            lines.append(f"    Проваленные проверки: {labels}")
        else:
            lines.append("    Проверки: все ожидания выполнены")

    passed = sum(1 for item in results if item.passed)
    lines.extend(
        [
            "",
            f"Итог: {passed}/{len(results)} сценариев прошли проверку ожиданий.",
        ]
    )
    return "\n".join(lines)


def _payload(results: list[ScenarioResult]) -> dict:
    passed = sum(1 for item in results if item.passed)
    return {
        "evaluation_date": EVALUATION_TODAY.isoformat(),
        "provider": "fixtures",
        "data_kind": "fixture",
        "scenarios": [item.to_dict() for item in results],
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "all_passed": passed == len(results),
        },
    }


def main(argv: list[str] | None = None) -> int:
    # Консоль Windows по умолчанию может быть в cp1251/cp866 и падать на «→».
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    parser = argparse.ArgumentParser(
        description="Прогон сценариев OneTrip Reality Check на тестовых фикстурах."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="вывести машинно-читаемый JSON вместо таблицы",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=[scenario.key for scenario in SCENARIOS],
        metavar="KEY",
        help="прогнать только выбранный сценарий (можно указывать несколько раз)",
    )
    args = parser.parse_args(argv)

    try:
        results = run_scenarios(args.scenario)
    except ConfigError as error:
        print(f"Ошибка конфигурации: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(_payload(results), ensure_ascii=False, indent=2))
    else:
        print(format_human(results))
    return 0 if all(item.passed for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
