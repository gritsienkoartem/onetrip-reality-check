"""Проверка уточнения мест до сборки плана."""
from __future__ import annotations

from models import TripRequest
from providers.geo import GeoPoint, PlaceResolution
from agent.place_resolver import resolve_request_places


def _point(
    name: str,
    *,
    region: str = "",
    country: str = "Россия",
    feature_type: str = "city",
    latitude: float = 55.0,
    longitude: float = 40.0,
) -> GeoPoint:
    return GeoPoint(
        city=name,
        display_name=", ".join(part for part in (name, region, country) if part),
        latitude=latitude,
        longitude=longitude,
        feature_type=feature_type,
        country=country,
        region=region,
    )


class FakeResolver:
    def __init__(self, resolutions: dict[str, PlaceResolution]) -> None:
        self._resolutions = resolutions
        self.calls: list[str] = []

    def resolve(self, query: str) -> PlaceResolution:
        self.calls.append(query)
        return self._resolutions.get(query, PlaceResolution(query, []))


def _request(origin: str | None, destination: str | None) -> TripRequest:
    return TripRequest(
        origin_city=origin,
        destination_city=destination,
        start_date=None,
        end_date=None,
        duration_days=3,
    )


def test_ambiguous_destination_is_reported_first():
    resolver = FakeResolver({
        "Ростов": PlaceResolution("Ростов", [
            _point("Ростов-на-Дону", region="Ростовская область", latitude=47.2, longitude=39.7),
            _point("Ростов", region="Ярославская область", latitude=57.1, longitude=39.4),
        ]),
    })

    request, ambiguous = resolve_request_places(_request("Москва", "Ростов"), resolver)

    assert resolver.calls == ["Ростов"]
    assert ambiguous is not None
    assert ambiguous.field == "destination_city"
    assert ambiguous.query == "Ростов"
    assert len(ambiguous.candidates) == 2
    assert "нескольким местам" in ambiguous.question


def test_ambiguous_origin_is_reported_after_clear_destination():
    resolver = FakeResolver({
        "Суздаль": PlaceResolution("Суздаль", [
            _point("Суздаль", region="Владимирская область", feature_type="town"),
        ]),
        "Ростов": PlaceResolution("Ростов", [
            _point("Ростов-на-Дону", region="Ростовская область", latitude=47.2, longitude=39.7),
            _point("Ростов", region="Ярославская область", latitude=57.1, longitude=39.4),
        ]),
    })

    _, ambiguous = resolve_request_places(_request("Ростов", "Суздаль"), resolver)

    assert resolver.calls == ["Суздаль", "Ростов"]
    assert ambiguous is not None
    assert ambiguous.field == "origin_city"


def test_selected_name_is_written_back_to_request():
    resolver = FakeResolver({
        "Питер": PlaceResolution("Питер", [
            _point("Санкт-Петербург", region="Санкт-Петербург"),
        ]),
    })

    request, ambiguous = resolve_request_places(_request("Питер", "Суздаль"), resolver)

    assert ambiguous is None
    assert request.origin_city == "Санкт-Петербург"
    assert request.destination_city == "Суздаль"


def test_non_settlement_keeps_the_user_query():
    resolver = FakeResolver({
        "Байкал": PlaceResolution("Байкал", [
            _point("озеро Байкал", region="Иркутская область", feature_type="lake"),
        ]),
    })

    request, ambiguous = resolve_request_places(_request("Москва", "Байкал"), resolver)

    assert ambiguous is None
    assert request.destination_city == "Байкал"


def test_skip_field_avoids_repeated_question():
    resolver = FakeResolver({
        "Ростов": PlaceResolution("Ростов", [
            _point("Ростов-на-Дону", region="Ростовская область"),
            _point("Ростов", region="Ярославская область"),
        ]),
    })

    request, ambiguous = resolve_request_places(
        _request("Москва", "Ростов"),
        resolver,
        skip_fields=("destination_city",),
    )

    assert resolver.calls == ["Москва"]
    assert ambiguous is None
    assert request.destination_city == "Ростов"


def test_missing_cities_are_not_queried():
    resolver = FakeResolver({
        "Суздаль": PlaceResolution("Суздаль", [
            _point("Суздаль", region="Владимирская область", feature_type="town"),
        ]),
    })

    request, ambiguous = resolve_request_places(_request(None, "Суздаль"), resolver)

    assert resolver.calls == ["Суздаль"]
    assert ambiguous is None
    assert request.origin_city is None


def test_duplicate_variant_labels_stay_distinct():
    resolver = FakeResolver({})
    from agent.place_resolver import AmbiguousPlace

    ambiguous = AmbiguousPlace(
        field="destination_city",
        query="Ростов",
        question="Уточните место",
        candidates=[
            _point("Ростов", region="Ростовская область"),
            _point("Ростов", region="Ростовская область", latitude=47.3, longitude=39.8),
        ],
    )

    options = ambiguous.options

    assert len(options) == 2
    assert len(set(options)) == 2
