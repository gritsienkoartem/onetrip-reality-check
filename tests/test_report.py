"""Тесты Markdown-отчёта и форматирования."""
from datetime import date, datetime

from agent.corrector import build_corrected_plan
from agent.report import (
    format_date_ru,
    format_duration,
    format_fetched_at,
    format_money,
    format_time_value,
    plan_to_markdown,
)
from models import HotelTransfer, TransferOption, TransportOption, TripRequest


def make_request() -> TripRequest:
    return TripRequest(
        origin_city="Москва",
        destination_city="Стамбул",
        start_date=date(2026, 11, 14),
        end_date=date(2026, 11, 17),
        duration_days=4,
        budget_amount=60000,
    )


def build_result():
    return build_corrected_plan(make_request(), today=date(2026, 10, 24))


def test_format_helpers():
    assert format_money(58390) == "58 390 ₽"
    assert format_duration(135) == "2 ч 15 мин"
    assert format_duration(120) == "2 ч"
    assert format_duration(45) == "45 мин"
    assert format_date_ru(date(2026, 11, 14)) == "14.11.2026"
    assert format_date_ru(None) == "дата не указана"
    assert format_time_value(None) == "—"


def test_format_fetched_at_shows_local_time():
    assert format_fetched_at(None) == "время получения не указано"
    stamp = datetime.now().astimezone().replace(
        year=2026, month=10, day=26, hour=0, minute=45, second=0, microsecond=0
    )
    assert format_fetched_at(stamp) == "26.10.2026, 00:45"


def test_markdown_contains_required_sections():
    result = build_result()
    markdown = plan_to_markdown(
        result.plan,
        initial_plan=result.initial_plan,
        applied=result.applied,
        correction_notes=result.notes,
    )

    assert markdown.startswith("# Поездка: Москва → Стамбул")
    for heading in ("## Транспорт", "## Отель", "## Маршрут по дням", "## Стоимость"):
        assert heading in markdown
    assert "## Проверка реалистичности" in markdown
    assert "**Оценка стоимости:**" in markdown
    assert "**Бюджет:** 60 000 RUB" in markdown
    assert "**Данные получены:**" in markdown
    assert "| **Итого (оценка)** |" in markdown
    assert "Стоимость и риск — оценки проекта по заданным правилам" in markdown


def test_markdown_reports_risk_change_and_corrections():
    result = build_result()
    markdown = plan_to_markdown(
        result.plan,
        initial_plan=result.initial_plan,
        applied=result.applied,
        correction_notes=result.notes,
    )

    assert "**Риск до исправлений:**" in markdown
    if result.applied:
        assert "## Автоматические исправления" in markdown
        for label in result.applied:
            assert label in markdown


def test_markdown_compares_transport_modes():
    result = build_result()
    plan = result.plan
    train_out = plan.flight_out.model_copy(
        update={"id": "train-out", "mode": "train", "price_kind": "estimate", "price_rub": 2400}
    )
    train_back = plan.flight_back.model_copy(
        update={"id": "train-back", "mode": "train", "price_kind": "estimate", "price_rub": 2400}
    )
    options = [
        TransportOption(
            mode=plan.flight_out.mode,
            outbound=plan.flight_out,
            inbound=plan.flight_back,
            total_price_rub=plan.flight_out.price_rub + plan.flight_back.price_rub,
            total_duration_minutes=600,
            price_kind=plan.flight_out.price_kind,
        ),
        TransportOption(
            mode="train",
            outbound=train_out,
            inbound=train_back,
            total_price_rub=4800,
            total_duration_minutes=1200,
            price_kind="estimate",
        ),
    ]
    markdown = plan_to_markdown(plan.model_copy(update={"transport_options": options}))

    assert "**Сравнение видов транспорта:**" in markdown
    assert "поезд: 4 800 ₽ туда и обратно" in markdown
    assert "— выбран" in markdown


def test_markdown_marks_test_data_and_issues():
    result = build_result()
    markdown = plan_to_markdown(result.plan)

    assert "тестовые данные" in markdown
    assert "## Пояснения к данным" in markdown
    for issue in result.plan.issues:
        assert issue.code in markdown
        assert issue.recommendation in markdown


def test_markdown_lists_lodging_preferences():
    result = build_result()
    request = result.plan.request.model_copy(
        update={"accommodation_preferences": ["кемпинг", "рядом с метро"]}
    )
    markdown = plan_to_markdown(result.plan.model_copy(update={"request": request}))

    assert "**Пожелания к жилью:** кемпинг, рядом с метро" in markdown

    without_preferences = plan_to_markdown(result.plan)
    assert "**Пожелания к жилью:**" not in without_preferences


def test_markdown_lists_price_sources_and_hotel_transfers():
    result = build_result()
    transfer = HotelTransfer(
        title="Аэропорт → отель",
        from_name="Аэропорт Стамбул",
        to_name=result.plan.hotel.name,
        distance_km=12.5,
        options=[
            TransferOption(
                mode="public_transport",
                distance_km=12.5,
                minutes=48,
                price_rub=155,
                note="Расписание не проверено.",
            ),
            TransferOption(
                mode="taxi",
                distance_km=12.5,
                minutes=24,
                price_rub=638,
                note="Тариф не проверен.",
                maps_url="https://www.openstreetmap.org/directions?engine=test",
                yandex_url="https://yandex.ru/maps/?rtext=1,2~3,4&rtt=auto",
            ),
        ],
    )
    markdown = plan_to_markdown(
        result.plan.model_copy(update={"hotel_transfers": [transfer]})
    )

    assert "**Сравнение сервисов (минимальная цена за направление):**" in markdown
    assert "## Дорога до отеля" in markdown
    assert "**Аэропорт → отель:** Аэропорт Стамбул →" in markdown
    assert "общественный транспорт: ~48 мин, 155 ₽" in markdown
    assert "[Маршрут в Яндекс Картах]" in markdown
