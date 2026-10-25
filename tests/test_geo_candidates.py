"""Кандидаты геокодера: тип места, страна и неоднозначные названия."""
from __future__ import annotations

import pytest

from providers.base import ProviderError
from providers.geo import GeoClient


class FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None, headers=None):
        params = params or {}
        self.calls.append((url, params))
        return self._handler(url, params)


def _candidate(
    name: str,
    *,
    feature_type: str,
    latitude: float,
    longitude: float,
    country: str,
    region: str = "",
    osm_id: int = 1,
) -> dict:
    address = {"country": country}
    if region:
        address["state"] = region
    address_key = {
        "city": "city",
        "town": "town",
        "village": "village",
        "administrative": "county",
        "lake": "lake",
    }.get(feature_type, "place")
    address[address_key] = name
    return {
        "osm_type": "relation",
        "osm_id": osm_id,
        "lat": str(latitude),
        "lon": str(longitude),
        "class": "place" if feature_type != "lake" else "natural",
        "type": feature_type,
        "addresstype": feature_type,
        "importance": 0.5,
        "name": name,
        "display_name": ", ".join(part for part in (name, region, country) if part),
        "address": address,
    }


SUZDAL = _candidate(
    "Суздаль",
    feature_type="town",
    latitude=56.42,
    longitude=40.44,
    country="Россия",
    region="Владимирская область",
    osm_id=101,
)

BAIKAL = _candidate(
    "озеро Байкал",
    feature_type="lake",
    latitude=53.5,
    longitude=108.0,
    country="Россия",
    region="Иркутская область",
    osm_id=102,
)

KOTOR = _candidate(
    "Котор",
    feature_type="town",
    latitude=42.42,
    longitude=18.77,
    country="Черногория",
    region="Општина Котор",
    osm_id=103,
)

ROSTOV_ON_DON = _candidate(
    "Ростов-на-Дону",
    feature_type="city",
    latitude=47.23,
    longitude=39.72,
    country="Россия",
    region="Ростовская область",
    osm_id=104,
)

ROSTOV_VELIKY = _candidate(
    "Ростов",
    feature_type="town",
    latitude=57.18,
    longitude=39.41,
    country="Россия",
    region="Ярославская область",
    osm_id=105,
)


def _client(payload):
    session = FakeSession(lambda url, params: FakeResponse(payload))
    return GeoClient(session, min_interval=0), session


def test_candidates_do_not_force_russia_and_keep_type():
    client, session = _client([SUZDAL])

    candidates = client.geocode_candidates("Суздаль")

    assert len(candidates) == 1
    point = candidates[0]
    assert point.city == "Суздаль"
    assert point.feature_type == "town"
    assert point.is_settlement is True
    assert point.country == "Россия"
    assert point.region == "Владимирская область"
    assert point.osm_id == 101
    assert "Владимирская область" in point.label_ru

    url, params = session.calls[0]
    assert "nominatim" in url
    assert params["q"] == "Суздаль"
    assert "Россия" not in params["q"]
    assert params["addressdetails"] == 1
    assert params["limit"] == 5


def test_lake_is_resolved_as_a_place_not_a_city():
    client, _ = _client([BAIKAL])

    resolution = client.resolve("Байкал")

    assert resolution.selected is not None
    assert resolution.selected.feature_type == "lake"
    assert resolution.selected.is_settlement is False
    assert resolution.selected.feature_label == "озеро"
    assert "озеро" in resolution.selected.label_ru
    assert resolution.ambiguous is False
    assert resolution.question == ""


def test_small_foreign_city_keeps_its_country():
    client, session = _client([KOTOR])

    resolution = client.resolve("Котор")

    assert resolution.selected is not None
    assert resolution.selected.city == "Котор"
    assert resolution.selected.country == "Черногория"
    assert resolution.ambiguous is False
    assert "Черногория" in resolution.selected.label_ru
    assert session.calls[0][1]["q"] == "Котор"


def test_ambiguous_name_gives_one_question_with_variants():
    unrelated = _candidate(
        "Ростовский",
        feature_type="village",
        latitude=46.5,
        longitude=41.0,
        country="Россия",
        region="Ростовская область",
        osm_id=106,
    )
    client, _ = _client([ROSTOV_ON_DON, ROSTOV_VELIKY, unrelated])

    resolution = client.resolve("Ростов")

    assert resolution.ambiguous is True
    assert len(resolution.matching_settlements) == 2
    assert "нескольким местам" in resolution.question
    assert "Ростов" in resolution.question
    labels = [point.label_ru for point in resolution.matching_settlements]
    assert len(set(labels)) == 2
    assert any("Ростовская область" in label for label in labels)
    assert any("Ярославская область" in label for label in labels)


def test_region_with_same_name_yields_to_city():
    region = _candidate(
        "Москва",
        feature_type="state",
        latitude=55.7,
        longitude=37.6,
        country="Россия",
        osm_id=113,
    )
    city = _candidate(
        "Москва",
        feature_type="city",
        latitude=55.7558,
        longitude=37.6173,
        country="Россия",
        region="Москва",
        osm_id=114,
    )
    client, _ = _client([region, city])

    resolution = client.resolve("Москва")

    assert resolution.selected is not None
    assert resolution.selected.feature_type == "city"
    assert resolution.selected.latitude == pytest.approx(55.7558)
    assert resolution.ambiguous is False


def test_selection_skips_an_irrelevant_top_result():
    ussuriysk = _candidate(
        "Уссурийск",
        feature_type="city",
        latitude=43.8,
        longitude=131.95,
        country="Россия",
        region="Приморский край",
        osm_id=108,
    )
    nikolskoye_len = _candidate(
        "Никольское",
        feature_type="town",
        latitude=59.7,
        longitude=30.8,
        country="Россия",
        region="Ленинградская область",
        osm_id=109,
    )
    nikolskoye_tver = _candidate(
        "Никольское",
        feature_type="village",
        latitude=56.8,
        longitude=35.0,
        country="Россия",
        region="Тверская область",
        osm_id=110,
    )
    client, _ = _client([ussuriysk, nikolskoye_len, nikolskoye_tver])

    resolution = client.resolve("Никольское")

    assert resolution.selected is not None
    assert resolution.selected.city == "Никольское"
    assert resolution.selected.region == "Ленинградская область"
    assert resolution.ambiguous is True
    assert len(resolution.matching_settlements) == 2


def test_paris_and_its_suburb_are_not_ambiguous():
    paris = _candidate(
        "Париж",
        feature_type="city",
        latitude=48.85,
        longitude=2.35,
        country="Франция",
        region="Иль-де-Франс",
        osm_id=111,
    )
    paris_suburb = _candidate(
        "Париж",
        feature_type="suburb",
        latitude=48.86,
        longitude=2.34,
        country="Франция",
        region="Иль-де-Франс",
        osm_id=112,
    )
    client, _ = _client([paris, paris_suburb])

    resolution = client.resolve("Париж")

    assert resolution.selected is not None
    assert resolution.selected.feature_type == "city"
    assert resolution.selected.country == "Франция"
    assert resolution.ambiguous is False


def test_related_names_do_not_trigger_a_question():
    district = _candidate(
        "Суздальский район",
        feature_type="administrative",
        latitude=56.4,
        longitude=40.4,
        country="Россия",
        region="Владимирская область",
        osm_id=107,
    )
    client, _ = _client([SUZDAL, district])

    resolution = client.resolve("Суздаль")

    assert resolution.ambiguous is False
    assert resolution.question == ""


def test_empty_result_is_not_ambiguous():
    client, _ = _client([])

    resolution = client.resolve("Несуществующее")

    assert resolution.candidates == []
    assert resolution.selected is None
    assert resolution.ambiguous is False
    assert client.geocode("Несуществующее") is None


def test_nominatim_error_is_reported():
    session = FakeSession(lambda url, params: FakeResponse({}, status_code=503))
    client = GeoClient(session, min_interval=0)

    with pytest.raises(ProviderError):
        client.geocode_candidates("Суздаль")
