"""Тесты парсера запросов (этап 2). Сеть и реальная модель не используются."""
from datetime import date

import pytest

from agent.parser import (
    CLARIFICATION_QUESTIONS,
    build_clarification_question,
    build_system_prompt,
    parse_trip_request,
)
from llm import LLMError
from models import TripRequest

TODAY = date(2026, 10, 24)
EXAMPLE = (
    "Хочу поехать в Стамбул на 4 дня в середине ноября. Бюджет — 60 000 рублей. "
    "Люблю архитектуру и кофе, не люблю толпы"
)


def parse(text, **kwargs):
    """Разбор только по правилам, без модели."""
    kwargs.setdefault("today", TODAY)
    return parse_trip_request(text, use_llm=False, **kwargs)


# ---------------- Разбор по правилам ----------------
def test_example_request_from_spec():
    result = parse(EXAMPLE)
    request = result.request
    assert request.destination_city == "Стамбул"
    assert request.origin_city is None
    assert request.duration_days == 4
    assert request.budget_amount == 60000
    assert request.currency == "RUB"
    assert request.period_description == "середина ноября"
    assert "архитектура" in request.interests
    assert "кофе" in request.interests
    assert request.avoid == ["толпы"]
    assert "толпы" not in request.interests
    # Город вылета не указан, значит, нужен один вопрос именно о нём.
    assert result.question == CLARIFICATION_QUESTIONS["origin_city"]
    assert result.asked_field == "origin_city"


def test_cities_by_prepositions():
    request = parse("Хочу из Казани в Стамбул на 3 дня").request
    assert (request.origin_city, request.destination_city) == ("Казань", "Стамбул")
    request = parse("В Стамбул из Москвы на 5 дней").request
    assert (request.origin_city, request.destination_city) == ("Москва", "Стамбул")


def test_single_city_without_preposition_is_destination():
    assert parse("Хочу Стамбул на 3 дня").request.destination_city == "Стамбул"


def test_explicit_date_range():
    result = parse("Из Москвы в Стамбул с 14 по 17 ноября")
    request = result.request
    assert request.start_date == date(2026, 11, 14)
    assert request.end_date == date(2026, 11, 17)
    assert request.duration_days == 4
    assert result.question is None


def test_dates_roll_over_to_next_year():
    request = parse("Из Москвы в Рим с 5 по 8 января", today=date(2026, 12, 20)).request
    assert request.start_date == date(2027, 1, 5)
    assert request.end_date == date(2027, 1, 8)


def test_impossible_date_is_ignored():
    request = parse("Из Москвы в Стамбул 31 ноября на 3 дня").request
    assert request.start_date is None
    assert request.duration_days == 3


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("на 5 дней", 5),
        ("на четыре дня", 4),
        ("на неделю", 7),
        ("на две недели", 14),
        ("на 3 ночи", 4),
        ("на 3-4 дня", 4),
        ("на 10 суток", 10),
    ],
)
def test_duration_variants(phrase, expected):
    assert parse(f"Хочу в Стамбул {phrase}").request.duration_days == expected


@pytest.mark.parametrize(
    "phrase, amount, currency",
    [
        ("Бюджет 60 000 рублей", 60000, "RUB"),
        ("бюджет 60к", 60000, "RUB"),
        ("Бюджет — 1,5 млн руб.", 1500000, "RUB"),
        ("Бюджет 45 тыс. рублей", 45000, "RUB"),
        ("Бюджет 60.000 ₽", 60000, "RUB"),
        ("Бюджет 800 долларов", 800, "USD"),
        ("Хочу уложиться в $1200", 1200, "USD"),
        ("Бюджет 500 евро", 500, "EUR"),
    ],
)
def test_budget_variants(phrase, amount, currency):
    request = parse(f"Хочу в Стамбул на 4 дня. {phrase}").request
    assert request.budget_amount == amount
    assert request.currency == currency


def test_days_are_not_mistaken_for_budget():
    request = parse("Хочу в Стамбул на 4 дня").request
    assert request.budget_amount is None
    assert request.currency == "RUB"


def test_word_number_nights_are_parsed_as_days():
    request = parse("Хочу в Иркутск 27 октября и вернуться через три ночи").request
    assert request.start_date == date(2026, 10, 27)
    assert request.duration_days == 4


def test_interests_and_avoid():
    request = parse("Люблю музеи и парки, не люблю жару").request
    assert request.interests == ["музеи", "парки"]
    assert request.avoid == ["жару"]
    assert parse("Хочу в Рим без толп").request.avoid == ["толпы"]


def test_accommodation_and_transport_preferences():
    request = parse("Нужен отель рядом с метро и прямой рейс").request
    assert "рядом с метро" in request.accommodation_preferences
    assert "прямой рейс" in request.transport_preferences


def test_lodging_formats_are_recognized():
    request = parse("Хочу гостевой дом или глэмпинг, а лучше шале").request
    assert "гостевой дом" in request.accommodation_preferences
    assert "кемпинг" in request.accommodation_preferences
    assert "необычное жильё" in request.accommodation_preferences


# ---------------- Уточняющие вопросы ----------------
def test_question_asks_for_origin_first():
    result = parse("Хочу отдохнуть на 4 дня")
    assert result.question == CLARIFICATION_QUESTIONS["origin_city"]


def test_known_information_is_not_asked_again():
    result = parse("Из Москвы в Стамбул")
    assert result.question == CLARIFICATION_QUESTIONS["duration_days"]
    assert result.asked_field == "duration_days"


def test_only_one_question_is_returned():
    assert build_clarification_question(TripRequest()).count("?") == 1


def test_complete_request_has_no_question():
    assert parse("Из Москвы в Стамбул на 4 дня").question is None


def test_answer_to_clarification_is_merged():
    first = parse("Хочу в Стамбул на 4 дня, бюджет 60 000 рублей")
    assert first.asked_field == "origin_city"
    second = parse("Москва", previous=first.request, asked_field=first.asked_field)
    request = second.request
    assert request.origin_city == "Москва"
    assert request.destination_city == "Стамбул"
    assert request.duration_days == 4
    assert request.budget_amount == 60000
    assert second.question is None


def test_answer_with_city_missing_from_dictionary():
    first = parse("Хочу в Стамбул на 4 дня")
    second = parse("нижний тагил", previous=first.request, asked_field="origin_city")
    assert second.request.origin_city == "Нижний Тагил"


def test_answer_with_number_of_days():
    first = parse("Из Москвы в Стамбул")
    second = parse("5", previous=first.request, asked_field="duration_days")
    assert second.request.duration_days == 5
    assert second.question is None


def test_empty_text_asks_to_describe_trip():
    result = parse("   ")
    assert result.question
    assert result.request.destination_city is None


# ---------------- Языковая модель ----------------
def test_llm_result_is_used():
    def fake_llm(system_prompt, user_message):
        return {
            "origin_city": "Москва", "destination_city": "Стамбул", "duration_days": 4,
            "budget_amount": 60000, "currency": "rub",
            "interests": ["архитектура", "кофе"], "avoid": ["толпы"],
        }

    result = parse_trip_request("любой текст", today=TODAY, llm_call=fake_llm)
    assert result.used_llm is True
    assert result.request.currency == "RUB"  # приведено к верхнему регистру моделью TripRequest
    assert result.request.interests == ["архитектура", "кофе"]
    assert result.question is None


def test_llm_parses_rare_city_without_substitution():
    def fake_llm(system_prompt, user_message):
        return {
            "destination_city": "Иркутск",
            "start_date": "2026-10-27",
            "duration_days": 4,
            "origin_city": None,
        }

    result = parse_trip_request(
        "хочу поехать в иркутск в вторник 27 октября и вернуться через три ночи",
        today=TODAY,
        llm_call=fake_llm,
    )
    request = result.request
    assert result.used_llm is True
    assert request.destination_city == "Иркутск"
    assert request.origin_city is None
    assert request.start_date == date(2026, 10, 27)
    assert request.duration_days == 4
    assert result.question == CLARIFICATION_QUESTIONS["origin_city"]


def test_llm_prompt_explains_any_city_and_nights():
    prompt = build_system_prompt(TODAY)
    assert "любым" in prompt
    assert "N + 1" in prompt
    assert "origin_city = null" in prompt


def test_llm_prompt_contains_today_and_context():
    captured = {}

    def fake_llm(system_prompt, user_message):
        captured.update(system=system_prompt, user=user_message)
        return {}

    parse_trip_request("Москва", today=TODAY, llm_call=fake_llm, asked_field="origin_city")
    assert "2026-10-24" in captured["system"]
    assert "origin_city" in captured["user"]


def test_llm_error_falls_back_to_rules():
    def broken_llm(system_prompt, user_message):
        raise LLMError("превышено время ожидания ответа модели")

    result = parse_trip_request(EXAMPLE, today=TODAY, llm_call=broken_llm)
    assert result.used_llm is False
    assert result.request.destination_city == "Стамбул"
    assert any("Языковая модель недоступна" in note for note in result.notes)


def test_llm_non_dict_answer_falls_back_to_rules():
    result = parse_trip_request(EXAMPLE, today=TODAY, llm_call=lambda s, u: ["не", "словарь"])
    assert result.used_llm is False
    assert result.request.duration_days == 4


def test_invalid_llm_fields_are_dropped_not_fatal():
    def fake_llm(system_prompt, user_message):
        return {"destination_city": "Стамбул", "duration_days": 4, "currency": "рубли", "budget_amount": -5}

    result = parse_trip_request("текст", today=TODAY, llm_call=fake_llm)
    request = result.request
    assert request.destination_city == "Стамбул"
    assert request.duration_days == 4
    assert request.budget_amount is None
    assert request.currency == "RUB"
    assert any("не прошла проверку" in note for note in result.notes)


def test_inconsistent_llm_dates_are_dropped():
    def fake_llm(system_prompt, user_message):
        return {
            "origin_city": "Москва", "destination_city": "Стамбул", "duration_days": 4,
            "start_date": "2026-11-17", "end_date": "2026-11-14",
        }

    request = parse_trip_request("текст", today=TODAY, llm_call=fake_llm).request
    assert request.start_date is None and request.end_date is None
    assert request.duration_days == 4


def test_llm_gaps_are_filled_by_rules():
    def fake_llm(system_prompt, user_message):
        return {"destination_city": "Стамбул"}

    text = "Из Москвы на 4 дня, бюджет 60 000 рублей"
    request = parse_trip_request(text, today=TODAY, llm_call=fake_llm).request
    assert request.origin_city == "Москва"
    assert request.duration_days == 4
    assert request.budget_amount == 60000


def test_null_strings_from_llm_are_ignored():
    def fake_llm(system_prompt, user_message):
        return {"origin_city": "null", "destination_city": "Стамбул"}

    assert parse_trip_request("Стамбул", today=TODAY, llm_call=fake_llm).request.origin_city is None


def test_use_llm_false_ignores_llm_call():
    def fake_llm(system_prompt, user_message):
        raise AssertionError("модель не должна вызываться")

    result = parse_trip_request(EXAMPLE, today=TODAY, llm_call=fake_llm, use_llm=False)
    assert result.used_llm is False


def test_unconfigured_llm_is_reported(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    result = parse_trip_request(EXAMPLE, today=TODAY)
    assert result.used_llm is False
    assert any("не настроена" in note for note in result.notes)
    assert result.request.destination_city == "Стамбул"
