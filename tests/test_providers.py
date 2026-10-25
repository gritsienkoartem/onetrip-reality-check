"""Тесты тестового провайдера фикстур и выбора live-провайдера."""
from datetime import date
from pathlib import Path

import pytest

from providers import LiveProvider, ProviderError, get_provider
from providers.fixtures import FixtureProvider


@pytest.fixture(scope="module")
def provider() -> FixtureProvider:
    return FixtureProvider()


def test_fixture_flights_are_filtered_by_route(provider):
    outbound = provider.search_flights("Москва", "Стамбул")
    inbound = provider.search_flights("Стамбул", "Москва")
    assert len(outbound) == 3
    assert len(inbound) == 3
    assert {flight.direction for flight in outbound} == {"outbound"}
    assert {flight.direction for flight in inbound} == {"return"}
    assert provider.search_flights("Казань", "Стамбул") == []


def test_city_filter_ignores_case_and_yo(provider):
    assert provider.search_hotels("стамбул")
    assert provider.get_places("СТАМБУЛ")


def test_fixture_entities_are_marked_as_fictional(provider):
    assert all(flight.is_mock for flight in provider.search_flights("Москва", "Стамбул"))
    assert all(hotel.is_mock for hotel in provider.search_hotels("Стамбул"))
    assert all(place.is_mock for place in provider.get_places("Стамбул"))
    assert all(day.is_mock for day in provider.get_weather("Стамбул", date(2026, 11, 14), date(2026, 11, 17)))


def test_weather_is_filtered_by_dates(provider):
    days = provider.get_weather("Стамбул", date(2026, 11, 14), date(2026, 11, 16))
    assert [day.day for day in days] == [date(2026, 11, 14), date(2026, 11, 15), date(2026, 11, 16)]
    assert provider.get_weather("Стамбул", date(2027, 1, 1), date(2027, 1, 2)) == []


def test_weather_is_empty_for_unknown_city(provider):
    assert provider.get_weather("Сочи", date(2026, 11, 14), date(2026, 11, 17)) == []


def test_weather_rejects_reversed_range(provider):
    with pytest.raises(ProviderError):
        provider.get_weather("Стамбул", date(2026, 11, 17), date(2026, 11, 14))


def test_currency_conversion(provider):
    assert provider.convert_currency(100, "EUR") == pytest.approx(10000)
    assert provider.convert_currency(10, "USD") == pytest.approx(950)
    assert provider.convert_currency(100, "RUB", "RUB") == pytest.approx(100)


def test_unknown_currency_is_reported(provider):
    with pytest.raises(ProviderError):
        provider.convert_currency(100, "GBP")


def test_negative_amount_is_rejected(provider):
    with pytest.raises(ProviderError):
        provider.convert_currency(-1, "RUB")


def test_get_provider_defaults_to_live(monkeypatch):
    monkeypatch.delenv("DATA_PROVIDER", raising=False)
    selected, notes = get_provider()
    assert isinstance(selected, LiveProvider)
    assert any("OpenStreetMap" in note for note in notes)
    assert not any("не поддерживается" in note for note in notes)


@pytest.mark.parametrize("requested", ["mock", "real", "unknown"])
def test_unknown_provider_falls_back_to_live(requested):
    selected, notes = get_provider(requested)
    assert isinstance(selected, LiveProvider)
    assert any("не поддерживается" in note for note in notes)
    assert any("live" in note for note in notes)


def test_broken_fixtures_directory_is_reported(tmp_path: Path):
    with pytest.raises(ProviderError):
        FixtureProvider(tmp_path)
