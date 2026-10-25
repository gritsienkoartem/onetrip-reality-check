"""Дымовые тесты Streamlit-интерфейса через AppTest."""
from pathlib import Path

import pytest

from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"
REQUEST = (
    "Хочу поехать в Стамбул с 14 по 17 ноября 2026 года. "
    "Бюджет 60000 рублей. Люблю архитектуру и кофе, не люблю толпы"
)


def _click(at: AppTest, label: str) -> None:
    for button in at.button:
        if button.label == label:
            button.click()
            return
    raise AssertionError(f"Кнопка {label!r} не найдена: {[b.label for b in at.button]}")


@pytest.fixture(autouse=True)
def disable_llm(monkeypatch):
    """UI-тесты не должны ходить в сеть, даже если у разработчика заполнен .env."""
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("LLM_API_KEY", "")


@pytest.fixture
def app() -> AppTest:
    at = AppTest.from_file(str(APP_PATH), default_timeout=120)
    at.run()
    assert not at.exception
    return at


def test_initial_screen(app):
    assert [title.value for title in app.title] == ["Путешествуй умнее. Трать меньше."]
    assert [area.label for area in app.text_area] == ["Опишите поездку"]
    assert any("План появится здесь" in caption.value for caption in app.caption)


def test_landing_cta_and_quick_actions_placement(app):
    markup = "\n".join(item.value for item in app.markdown)
    assert 'href="#ot-plan"' in markup
    assert "Спланировать поездку" in markup
    assert 'id="ot-reality"' in markup

    labels = [button.label for button in app.button]
    assert labels.count("Подставить пример") == 1
    assert labels.count("Очистить результат") == 1
    assert "Новый запрос" not in labels
    assert any(expander.label == "Быстрые действия" for expander in app.expander)


def test_full_clarification_and_plan_flow(app):
    app.text_area[0].set_value(REQUEST)
    _click(app, "Проверить поездку")
    app.run()
    assert not app.exception
    assert any("Из какого города" in info.value for info in app.info)
    assert [field.label for field in app.text_input] == ["Откуда", "Куда", "Ваш ответ"]

    app.text_input[-1].set_value("Москва")
    _click(app, "Ответить")
    app.run()
    assert not app.exception
    assert [subheader.value for subheader in app.subheader] == ["Москва → Стамбул"]

    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Оценка стоимости"].endswith("₽")
    assert metrics["Бюджет"] == "60 000 RUB"
    assert metrics["Проблем после проверки"].isdigit()
    assert metrics["Автоматических исправлений"].isdigit()

    risk_messages = (
        [item.value for item in app.warning]
        + [item.value for item in app.success]
        + [item.value for item in app.error]
    )
    assert any(value.startswith("Риск:") for value in risk_messages)
    assert [tab.label for tab in app.tabs] == [
        "Обзор",
        "Транспорт",
        "Отели",
        "Бюджет",
        "Об источниках",
    ]
    markup = "\n".join(item.value for item in app.markdown)
    assert "Сравнение сервисов" in markup
    assert "ot-price-board" in markup
    assert [button.label for button in app.button].count("Выбрать этот вариант") >= 1
    assert [button.label for button in app.get("download_button")] == [
        "Скачать план в Markdown"
    ]


def test_transport_choice_rebuilds_plan(app):
    app.text_area[0].set_value(REQUEST)
    _click(app, "Проверить поездку")
    app.run()
    app.text_input[-1].set_value("Москва")
    _click(app, "Ответить")
    app.run()
    assert not app.exception

    result = app.session_state["correction_result"]
    before_ids = (result.plan.flight_out.id, result.plan.flight_back.id)

    _click(app, "Выбрать этот вариант")
    app.run()
    assert not app.exception

    updated = app.session_state["correction_result"]
    after_ids = (updated.plan.flight_out.id, updated.plan.flight_back.id)
    assert after_ids != before_ids
    assert "Этот вариант уже выбран в плане." in [
        caption.value for caption in app.caption
    ]
    markup = "\n".join(item.value for item in app.markdown)
    assert "· выбран" in markup


def test_hotel_choice_rebuilds_plan(app):
    app.text_area[0].set_value(REQUEST)
    _click(app, "Проверить поездку")
    app.run()
    app.text_input[-1].set_value("Москва")
    _click(app, "Ответить")
    app.run()
    assert not app.exception

    result = app.session_state["correction_result"]
    before_id = result.plan.hotel.id

    _click(app, "Выбрать этот отель")
    app.run()
    assert not app.exception

    updated = app.session_state["correction_result"]
    assert updated.plan.hotel.id != before_id
    assert "Этот вариант уже выбран." in [caption.value for caption in app.caption]


def test_unconfirmed_hotel_format_is_explained_in_hotels_tab(app):
    request = REQUEST.replace(
        "Люблю архитектуру и кофе, не люблю толпы", "Хочу кемпинг"
    )
    app.text_area[0].set_value(request)
    _click(app, "Проверить поездку")
    app.run()
    app.text_input[-1].set_value("Москва")
    _click(app, "Ответить")
    app.run()
    assert not app.exception

    result = app.session_state["correction_result"]
    assert result.plan.request.accommodation_preferences == ["кемпинг"]
    assert any(
        "Источники не подтвердили пожелания к жилью: кемпинг" in info.value
        for info in app.info
    )
    assert any(
        caption.value == "Пожелания к жилью: кемпинг."
        for caption in app.caption
    )


def test_unsupported_destination_shows_no_fake_plan(app):
    app.text_area[0].set_value("Из Минска в Иркутск с 27 по 30 октября 2026")
    _click(app, "Проверить поездку")
    app.run()
    assert any("Куда вы хотите поехать" in info.value for info in app.info)

    app.text_input[-1].set_value("Иркутск")
    _click(app, "Ответить")
    app.run()
    assert not app.exception
    assert any("План не собран" in error.value for error in app.error)
    assert any("нет данных" in error.value for error in app.error)
    assert not app.metric
    assert not app.tabs
    assert not app.get("download_button")


def test_clear_result_resets_plan(app):
    app.text_area[0].set_value(REQUEST)
    _click(app, "Проверить поездку")
    app.run()
    app.text_input[-1].set_value("Москва")
    _click(app, "Ответить")
    app.run()
    assert app.subheader

    _click(app, "Очистить результат")
    app.run()
    assert not app.subheader
    assert not app.get("download_button")
