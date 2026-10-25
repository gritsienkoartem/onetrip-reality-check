"""Прозрачная эвристика риска: очки за проблемы и уровни из assumptions.json."""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from models import Issue, RiskLevel

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
DEFAULT_POINTS = {"low": 1, "medium": 3, "high": 6}

# Если данных о городе нет, поездку нельзя оценить по остальным правилам.
# Такой план всегда получает высокий риск, а не «средний» из-за одного issue.
BLOCKING_CODES = {"DESTINATION_UNSUPPORTED"}


def sort_issues(issues: Iterable[Issue]) -> list[Issue]:
    """Сначала высокие, затем средние и низкие; внутри — по коду и тексту."""
    return sorted(
        issues,
        key=lambda issue: (
            SEVERITY_ORDER.get(issue.severity.value, 9),
            issue.code,
            issue.description,
        ),
    )


def calculate_risk(
    issues: Sequence[Issue],
    scoring: dict | None = None,
) -> tuple[int, RiskLevel]:
    """Суммирует очки за проблемы и возвращает (risk_score, risk_level).

    Это эвристика проекта, а не научно проверенная вероятность.
    Блокирующая проблема (нет данных о городе) всегда даёт высокий уровень.
    """
    scoring = scoring or {}
    raw_points = scoring.get("points_by_severity")
    points = raw_points if isinstance(raw_points, dict) else DEFAULT_POINTS
    score = sum(int(points.get(issue.severity.value, 0)) for issue in issues)

    medium_from = int(scoring.get("medium_level_from", 3))
    high_from = int(scoring.get("high_level_from", 7))
    if any(issue.code in BLOCKING_CODES for issue in issues):
        level = RiskLevel.HIGH
    elif score >= high_from:
        level = RiskLevel.HIGH
    elif score >= medium_from:
        level = RiskLevel.MEDIUM
    else:
        level = RiskLevel.LOW
    return score, level
