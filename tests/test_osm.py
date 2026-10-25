"""Транспортные узлы OpenStreetMap без сети."""
from __future__ import annotations

import pytest

from providers.base import ProviderError
from providers.geo import GeoPoint
from providers.osm import OsmClient, TransportNodes

PENZA = GeoPoint("Пенза", "Пенза, Россия", 53.1953, 45.0190)


class FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self._status_code = status_code
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, data=None, timeout=None, headers=None):
        self.calls.append((url, data or {}))
        return FakeResponse(self._payload, self._status_code)


def _element(
    *,
    osm_type: str = "node",
    osm_id: int,
    tags: dict,
    latitude: float | None = None,
    longitude: float | None = None,
    center: tuple[float, float] | None = None,
) -> dict:
    item = {"type": osm_type, "id": osm_id, "tags": tags}
    if latitude is not None:
        item["lat"] = latitude
        item["lon"] = longitude
    if center is not None:
        item["center"] = {"lat": center[0], "lon": center[1]}
    return item


PAYLOAD = {
    "elements": [
        _element(
            osm_id=1,
            tags={"aeroway": "aerodrome", "name": "Аэропорт Пенза", "iata": "PEZ"},
            latitude=53.118,
            longitude=45.023,
        ),
        _element(
            osm_type="way",
            osm_id=2,
            tags={"aeroway": "aerodrome", "name": "Аэропорт Пенза", "icao": "UWPP"},
            center=(53.12, 45.02),
        ),
        _element(
            osm_id=3,
            tags={"aeroway": "aerodrome", "name": "Военный аэродром", "aerodrome:type": "military"},
            latitude=53.3,
            longitude=45.1,
        ),
        _element(
            osm_id=4,
            tags={"aeroway": "aerodrome", "name": "Аэродром ДОСААФ"},
            latitude=53.25,
            longitude=45.05,
        ),
        _element(
            osm_id=5,
            tags={"railway": "station", "name": "Пенза I"},
            latitude=53.19,
            longitude=45.01,
        ),
        _element(
            osm_id=6,
            tags={"railway": "station", "name": "Сортировочная", "usage": "freight"},
            latitude=53.2,
            longitude=45.0,
        ),
        _element(
            osm_id=7,
            tags={"railway": "station", "name": "Метро", "station": "subway"},
            latitude=53.21,
            longitude=45.02,
        ),
        _element(
            osm_id=8,
            tags={"amenity": "bus_station", "name": "Автовокзал Пенза"},
            latitude=53.18,
            longitude=45.0,
        ),
        _element(
            osm_id=9,
            tags={"amenity": "bus_station", "name": "Дальний автовокзал"},
            latitude=54.8,
            longitude=45.0,
        ),
        _element(
            osm_id=10,
            tags={"amenity": "bus_station", "name": "Без координат"},
        ),
    ]
}


def _client(payload=PAYLOAD, status_code: int = 200):
    session = FakeSession(payload, status_code)
    return OsmClient(session, timeout=5), session


def test_transport_nodes_are_grouped_filtered_and_deduped():
    client, session = _client()

    nodes = client.transport_nodes(
        PENZA,
        radii_km={"airport": 150, "train": 80, "bus": 60},
        limit=3,
    )

    assert [node.name for node in nodes.airports] == ["Аэропорт Пенза"]
    assert [node.name for node in nodes.train_stations] == ["Пенза I"]
    assert [node.name for node in nodes.bus_stations] == ["Автовокзал Пенза"]
    assert nodes.airports[0].kind == "airport"
    assert nodes.airports[0].distance_km > 0
    assert nodes.nearest("train") is not None
    assert nodes.nearest("bus") is not None
    assert all(node.source.startswith("OpenStreetMap") for node in nodes.airports)

    _, data = session.calls[0]
    query = data["data"]
    assert "around:150000" in query
    assert '"aeroway"="aerodrome"' in query
    assert '"railway"="station"' in query
    assert '"amenity"="bus_station"' in query


def test_radius_hides_nodes_that_are_too_far():
    client, _ = _client()

    nodes = client.transport_nodes(
        PENZA,
        radii_km={"airport": 0.1, "train": 0.1, "bus": 0.1},
        limit=3,
    )

    assert nodes.airports == []
    assert nodes.train_stations == []
    assert nodes.bus_stations == []
    assert isinstance(nodes, TransportNodes)


def test_transport_nodes_are_cached_per_point():
    client, session = _client()

    first = client.transport_nodes(PENZA, radii_km={"airport": 150, "train": 80, "bus": 60})
    second = client.transport_nodes(PENZA, radii_km={"airport": 150, "train": 80, "bus": 60})

    assert len(session.calls) == 1
    assert [node.id for node in first.airports] == [node.id for node in second.airports]


def test_overpass_error_is_reported():
    client, _ = _client(status_code=503)

    with pytest.raises(ProviderError):
        client.transport_nodes(PENZA)


def test_malformed_elements_are_skipped():
    client, _ = _client({"elements": [None, {"type": "node", "id": 1, "tags": {}}]})

    nodes = client.transport_nodes(PENZA)

    assert nodes.airports == []
    assert nodes.train_stations == []
    assert nodes.bus_stations == []


LODGING_PAYLOAD = {
    "elements": [
        _element(
            osm_id=101,
            tags={
                "tourism": "hotel",
                "name": "Отель Центральный",
                "stars": "4",
                "addr:suburb": "Центр",
            },
            latitude=53.1953,
            longitude=45.0190,
        ),
        _element(
            osm_type="way",
            osm_id=102,
            tags={"tourism": "hotel", "name": "Отель Центральный"},
            center=(53.1954, 45.0191),
        ),
        _element(
            osm_id=103,
            tags={"tourism": "hostel", "name": "Хостел У воды"},
            latitude=53.2000,
            longitude=45.0200,
        ),
        _element(
            osm_id=104,
            tags={"tourism": "guest_house", "name": "Гостевой дом Тихий"},
            latitude=53.2500,
            longitude=45.0500,
        ),
        _element(
            osm_id=105,
            tags={"tourism": "hotel", "name": "Закрытый отель", "disused": "yes"},
            latitude=53.1960,
            longitude=45.0190,
        ),
        _element(
            osm_id=106,
            tags={"tourism": "camp_site", "name": ""},
            latitude=53.1970,
            longitude=45.0190,
        ),
        _element(
            osm_id=201,
            tags={"railway": "station", "name": "Пенза I"},
            latitude=53.1980,
            longitude=45.0200,
        ),
        _element(
            osm_id=202,
            tags={"railway": "subway_entrance"},
            latitude=53.1960,
            longitude=45.0195,
        ),
        _element(
            osm_id=203,
            tags={"natural": "water", "name": "Пруд"},
            latitude=53.1965,
            longitude=45.0195,
        ),
    ]
}


def _lodging_client(payload=LODGING_PAYLOAD):
    session = FakeSession(payload)
    return OsmClient(session, timeout=5), session


def test_lodging_is_parsed_with_distances_and_deduped():
    client, session = _lodging_client()

    area = client.lodging(PENZA, radius_km=12, support_radius_km=6, limit=10)

    assert [node.name for node in area.lodgings] == [
        "Отель Центральный",
        "Хостел У воды",
        "Гостевой дом Тихий",
    ]
    hotel = area.lodgings[0]
    assert hotel.id == "osm-node-101"
    assert hotel.kind == "hotel"
    assert hotel.kind_label == "отель"
    assert hotel.distance_to_center_km == 0
    assert hotel.distance_to_station_km is not None
    assert hotel.distance_to_station_km < 1
    assert hotel.distance_to_metro_km is not None
    assert hotel.distance_to_metro_km < 1
    assert hotel.distance_to_water_km is not None
    assert hotel.distance_to_water_km < 1
    assert hotel.tags["stars"] == "4"
    assert area.station_count == 1
    assert area.metro_count == 1
    assert area.water_count == 1

    query = session.calls[0][1]["data"]
    assert '"tourism"' in query
    assert '"subway_entrance"' in query
    assert '"natural"' in query
    assert "around:12000" in query


def test_lodging_is_cached_and_respects_limit():
    client, session = _lodging_client()

    first = client.lodging(PENZA, radius_km=12, support_radius_km=6, limit=2)
    second = client.lodging(PENZA, radius_km=12, support_radius_km=6, limit=2)

    assert len(session.calls) == 1
    assert len(first.lodgings) == 2
    assert [node.id for node in first.lodgings] == [node.id for node in second.lodgings]


def test_lodging_skips_objects_without_required_data():
    client, _ = _lodging_client(
        {
            "elements": [
                _element(
                    osm_id=301,
                    tags={"tourism": "hotel", "name": "Без координат"},
                ),
                _element(
                    osm_id=302,
                    tags={"tourism": "hotel", "name": "Снесён", "abandoned": "yes"},
                    latitude=53.19,
                    longitude=45.02,
                ),
                _element(
                    osm_id=303,
                    tags={"tourism": "hotel", "name": "Живой"},
                    latitude=53.19,
                    longitude=45.02,
                ),
            ]
        }
    )

    area = client.lodging(PENZA)

    assert [node.name for node in area.lodgings] == ["Живой"]


def test_lodging_overpass_error_is_reported():
    client, _ = _client(status_code=503)

    with pytest.raises(ProviderError):
        client.lodging(PENZA)
