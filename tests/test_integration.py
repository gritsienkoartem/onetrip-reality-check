"""Сквозной тест: запрос из задания превращается в проверенный план."""
from datetime import date

from agent.corrector import build_corrected_plan
from agent.parser import parse_trip_request
from providers.fixtures import FixtureProvider

TODAY = date(2026, 10, 24)
EXAMPLE = (
    "Хочу поехать в Стамбул на 4 дня в середине ноября. Бюджет — 60 000 рублей. "
    "Люблю архитектуру и кофе, не люблю толпы"
)


def test_example_request_becomes_a_checked_and_improved_plan():
    first = parse_trip_request(EXAMPLE, today=TODAY, use_llm=False)
    assert first.asked_field == "origin_city"

    second = parse_trip_request(
        "Москва",
        previous=first.request,
        asked_field=first.asked_field,
        today=TODAY,
        use_llm=False,
    )
    result = build_corrected_plan(second.request, provider=FixtureProvider(), today=TODAY)
    plan = result.plan

    assert plan.request.origin_city == "Москва"
    assert plan.request.destination_city == "Стамбул"
    assert plan.flight_out is not None
    assert plan.flight_back is not None
    assert plan.hotel is not None
    assert len(plan.days) == 4
    assert plan.cost is not None and plan.cost.total > 0
    assert result.initial_plan.issues
    assert plan.risk_score <= result.initial_plan.risk_score
    assert result.applied
