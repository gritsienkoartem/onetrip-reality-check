"""Тесты пошагового мастера поездки в Streamlit-интерфейсе."""
from datetime import date, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"
TRIP_DATES = (date.today() + timedelta(days=20), date.today() + timedelta(days=23))


@pytest.fixture(autouse=True)
def disable_llm(monkeypatch):
    """Мастер не должен ходить в сеть через LLM во время тестов."""
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("LLM_API_KEY", "")


@pytest.fixture
def app() -> AppTest:
    at = AppTest.from_file(str(APP_PATH), default_timeout=120)
    at.run()
    assert not at.exception
    return at


def _click(app: AppTest, label: str) -> None:
    for button in app.button:
        if button.label == label:
            button.click()
            return
    raise AssertionError(f"Кнопка {label!r} не найдена: {[b.label for b in app.button]}")


def _click_and_run(app: AppTest, label: str) -> None:
    _click(app, label)
    app.run()
    assert not app.exception, app.exception


def _selectbox(app: AppTest, label: str):
    for box in app.selectbox:
        if box.label == label:
            return box
    raise AssertionError(f"Список {label!r} не найден: {[b.label for b in app.selectbox]}")


def _text_input(app: AppTest, label: str):
    for field in app.text_input:
        if field.label == label:
            return field
    raise AssertionError(f"Поле {label!r} не найдено: {[f.label for f in app.text_input]}")


def _number_input(app: AppTest, label: str):
    for field in app.number_input:
        if field.label == label:
            return field
    raise AssertionError(f"Поле {label!r} не найдено: {[f.label for f in app.number_input]}")


def _radio(app: AppTest, label: str):
    for field in app.radio:
        if field.label == label:
            return field
    raise AssertionError(f"Переключатель {label!r} не найден: {[f.label for f in app.radio]}")


def _multiselect(app: AppTest, label: str):
    for field in app.multiselect:
        if field.label == label:
            return field
    raise AssertionError(f"Мультивыбор {label!r} не найден: {[f.label for f in app.multiselect]}")


def _fill_route(app: AppTest) -> None:
    _text_input(app, "Откуда").set_value("Москва")
    _text_input(app, "Куда").set_value("Стамбул")
    app.run()
    assert not app.exception


def _fill_dates(app: AppTest, value=TRIP_DATES) -> None:
    assert [field.label for field in app.date_input] == ["Даты поездки"]
    app.date_input[0].set_value(value)
    app.run()
    assert not app.exception


def _open_review(app: AppTest, *, budget: int = 75_000) -> None:
    _fill_route(app)
    _click_and_run(app, "Продолжить")
    _fill_dates(app)
    _click_and_run(app, "Продолжить")
    _number_input(app, "Взрослые").set_value(2)
    _number_input(app, "Дети").set_value(1)
    app.run()
    assert not app.exception
    _click_and_run(app, "Продолжить")
    _number_input(app, "Бюджет поездки, RUB").set_value(budget)
    _multiselect(app, "Интересы").select("архитектура")
    _multiselect(app, "Чего избегать").select("толпы")
    app.text_area[0].set_value("Хочу без спешки")
    app.run()
    assert not app.exception
    _click_and_run(app, "Продолжить")


def test_wizard_is_visible_on_first_screen(app):
    assert [field.label for field in app.text_input] == ["Откуда", "Куда"]
    assert [field.value for field in app.text_input] == ["", ""]
    assert any("Шаг 1 из 5" in item.text for item in app.progress)
    assert any(button.label == "Продолжить" for button in app.button)
    assert any(button.label == "Выбрать Казань" for button in app.button)
    assert any("Чтобы продолжить" in caption.value for caption in app.caption)
    assert any("не поддерживаются" in caption.value for caption in app.caption)


def test_city_chip_fills_destination(app):
    _click_and_run(app, "Выбрать Казань")
    assert _text_input(app, "Куда").value == "Казань"


def test_wizard_back_keeps_route(app):
    _fill_route(app)
    _click_and_run(app, "Продолжить")
    assert any("Шаг 2 из 5" in item.text for item in app.progress)
    _click_and_run(app, "Назад")
    assert _text_input(app, "Откуда").value == "Москва"
    assert _text_input(app, "Куда").value == "Стамбул"


def test_wizard_builds_plan_from_answers(app):
    _open_review(app, budget=75_000)
    assert any("Шаг 5 из 5" in item.text for item in app.progress)
    assert any("Москва → Стамбул" in item.value for item in app.markdown)
    assert app.session_state["correction_result"] is None

    _click_and_run(app, "Найти варианты и составить план")

    result = app.session_state["correction_result"]
    assert result is not None
    request = result.plan.request
    assert request.origin_city == "Москва"
    assert request.destination_city == "Стамбул"
    assert request.start_date == TRIP_DATES[0]
    assert request.end_date == TRIP_DATES[1]
    assert request.duration_days == 4
    assert request.budget_amount == 75_000
    assert request.currency == "RUB"
    assert request.budget_scope == "group"
    assert request.adults == 2
    assert request.children == 1
    assert request.travelers == 3
    assert request.interests == ["архитектура"]
    assert request.avoid == ["толпы"]
    assert request.trip_style == "balanced"
    assert "Хочу без спешки" in request.raw_text
    assert "мастер" in " ".join(app.session_state["parse_notes"]).lower()

    assert any(subheader.value == "Москва → Стамбул" for subheader in app.subheader)
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Бюджет"] == "75 000 RUB"
    assert metrics["Проблем после проверки"].isdigit()
    assert [tab.label for tab in app.tabs] == [
        "Обзор",
        "Транспорт",
        "Отели",
        "Бюджет",
        "Об источниках",
    ]
    labels = [button.label for button in app.button]
    assert "Изменить параметры" in labels
    assert "Повторить поиск" in labels
    assert [button.label for button in app.get("download_button")] == [
        "Скачать план в Markdown"
    ]


def test_wizard_saves_hotel_format_choices(app):
    _open_review(app)
    _click_and_run(app, "Изменить: бюджет и стиль")
    formats = _multiselect(app, "Формат жилья")
    formats.select("Кемпинг / глэмпинг")
    formats.select("Гостевой дом")
    app.run()
    assert not app.exception

    _click_and_run(app, "Продолжить")
    markup = "\n".join(item.value for item in app.markdown)
    assert "жильё: " in markup
    assert "Кемпинг / глэмпинг" in markup
    assert "Гостевой дом" in markup
    _click_and_run(app, "Найти варианты и составить план")

    request = app.session_state["correction_result"].plan.request
    assert "кемпинг" in request.accommodation_preferences
    assert "гостевой дом" in request.accommodation_preferences
    assert any("не подтвердили" in info.value for info in app.info)

    _click_and_run(app, "Изменить параметры")
    _click_and_run(app, "Изменить: бюджет и стиль")
    assert set(_multiselect(app, "Формат жилья").value) == {"guest_house", "camping"}


def test_wizard_rejects_more_than_thirty_days(app):
    _fill_route(app)
    _click_and_run(app, "Продолжить")

    _fill_dates(app, (TRIP_DATES[0], TRIP_DATES[0] + timedelta(days=30)))
    assert any("длиннее 30 дней" in caption.value for caption in app.caption)
    _click_and_run(app, "Продолжить")
    assert any("Шаг 2 из 5" in item.text for item in app.progress)
    assert any("длиннее 30 дней" in warning.value for warning in app.warning)


def test_wizard_rejects_same_city(app):
    _text_input(app, "Откуда").set_value("Москва")
    _text_input(app, "Куда").set_value("Москва")
    app.run()
    assert not app.exception
    assert any("должны различаться" in caption.value for caption in app.caption)
    _click_and_run(app, "Продолжить")
    assert any("Шаг 1 из 5" in item.text for item in app.progress)
    assert any("должны различаться" in warning.value for warning in app.warning)

def test_review_cards_allow_editing_without_losing_answers(app):
    _open_review(app)
    labels = [button.label for button in app.button]
    assert "Изменить: маршрут" in labels
    assert "Изменить: даты" in labels
    assert "Изменить: путешественники" in labels
    assert "Изменить: бюджет и стиль" in labels

    _click_and_run(app, "Изменить: бюджет и стиль")
    assert any("Шаг 4 из 5" in item.text for item in app.progress)
    assert _number_input(app, "Бюджет поездки, RUB").value == 75_000
    assert _radio(app, "Бюджет указан").value == "group"
    assert _multiselect(app, "Интересы").value == ["архитектура"]
    assert app.session_state["correction_result"] is None

    _click_and_run(app, "Продолжить")
    assert any("Шаг 5 из 5" in item.text for item in app.progress)
    assert app.session_state["correction_result"] is None


def test_wizard_saves_currency_scope_and_free_text(app):
    _fill_route(app)
    _click_and_run(app, "Продолжить")
    _fill_dates(app)
    _click_and_run(app, "Продолжить")
    _click_and_run(app, "Продолжить")

    _selectbox(app, "Валюта").select("EUR")
    _radio(app, "Бюджет указан").set_value("person")
    app.text_area[0].set_value("Хочу спокойно гулять по центру")
    app.run()
    assert not app.exception
    _click_and_run(app, "Продолжить")
    _click_and_run(app, "Найти варианты и составить план")

    request = app.session_state["correction_result"].plan.request
    assert request.currency == "EUR"
    assert request.budget_scope == "person"
    assert request.travelers == 1
    assert "спокойно гулять" in request.raw_text


def test_results_show_hotel_total_and_test_data_labels(app):
    _open_review(app)
    _click_and_run(app, "Найти варианты и составить план")

    captions = [caption.value for caption in app.caption]
    assert any("Всего за проживание" in value for value in captions)
    assert any("расчёт проекта" in value for value in captions)
    assert any("тестовые данные" in value for value in captions)
    assert not any("Aviasales" in value for value in captions)


def test_results_edit_preserves_other_answers(app):
    _open_review(app)
    _click_and_run(app, "Найти варианты и составить план")
    _click_and_run(app, "Изменить параметры")

    assert app.session_state["show_results"] is False
    assert any("Шаг 5 из 5" in item.text for item in app.progress)
    assert app.session_state["wizard_adults"] == 2
    assert app.session_state["wizard_children"] == 1
    assert app.session_state["wizard_budget"] == 75_000
    assert app.session_state["wizard_interests"] == ["архитектура"]
    assert app.session_state["wizard_notes"] == "Хочу без спешки"


def test_results_repeat_search_keeps_plan(app):
    _open_review(app)
    _click_and_run(app, "Найти варианты и составить план")
    first_total = app.session_state["correction_result"].plan.cost.total
    _click_and_run(app, "Повторить поиск")

    result = app.session_state["correction_result"]
    assert result is not None
    assert result.plan.cost.total == first_total
    assert app.session_state["show_results"] is True


def test_wizard_does_not_search_when_switching_steps(app):
    _open_review(app)
    _click_and_run(app, "Назад")
    _click_and_run(app, "Назад")
    assert any("Шаг 3 из 5" in item.text for item in app.progress)
    assert app.session_state["correction_result"] is None
