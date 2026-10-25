"""Оценки отелей, найденных в OpenStreetMap, без сети."""
from __future__ import annotations

from config import load_assumptions
from models import NO_DATA, Hotel
from providers.estimates import estimate_hotel_transfer, estimate_osm_hotels
from providers.geo import GeoPoint
from providers.osm import LodgingArea, LodgingNode

PENZA = GeoPoint("Пенза", "Пенза, Россия", 53.1953, 45.0190)


def _node(node_id: str, name: str, kind: str, **changes) -> LodgingNode:
    data = dict(
        id=node_id,
        name=name,
        kind=kind,
        latitude=53.1953,
        longitude=45.0190,
        distance_to_center_km=3.0,
    )
    data.update(changes)
    return LodgingNode(**data)


def _area() -> LodgingArea:
    return LodgingArea(
        lodgings=[
            _node(
                "osm-node-1",
                "Отель у набережной",
                "hotel",
                distance_to_center_km=0.5,
                distance_to_station_km=0.6,
                distance_to_metro_km=0.4,
                distance_to_water_km=0.2,
                tags={"stars": "5", "addr:suburb": "Центр"},
            ),
            _node(
                "osm-node-2",
                "Гостевой дом на окраине",
                "guest_house",
                distance_to_center_km=4.0,
                distance_to_water_km=3.0,
            ),
            _node("osm-node-3", "Хостел", "hostel", distance_to_center_km=3.0),
        ],
        station_count=1,
        metro_count=1,
        water_count=1,
    )


def test_osm_hotels_become_labelled_estimates():
    hotels = estimate_osm_hotels("Пенза", PENZA, _area(), load_assumptions())

    assert [hotel.name for hotel in hotels] == [
        "Отель у набережной",
        "Гостевой дом на окраине",
        "Хостел",
    ]
    assert all(hotel.price_kind == "estimate" for hotel in hotels)
    assert all(hotel.is_mock is False for hotel in hotels)
    assert all(hotel.rating_source == NO_DATA for hotel in hotels)
    assert all(hotel.source.startswith("OpenStreetMap") for hotel in hotels)
    assert all(hotel.distance_to_center_km is not None for hotel in hotels)
    assert hotels[0].district == "Центр"


def test_osm_hotels_get_theme_tags_from_distances():
    central, quiet, hostel = estimate_osm_hotels(
        "Пенза", PENZA, _area(), load_assumptions()
    )

    assert "отель" in central.tags
    assert "в центре" in central.tags
    assert "у воды" in central.tags
    assert "у вокзала" in central.tags
    assert "тихо" not in central.tags

    assert "гостевой дом" in quiet.tags
    assert "тихо" in quiet.tags
    assert "в центре" not in quiet.tags
    assert "у воды" not in quiet.tags

    assert hostel.tags == ["хостел"]


def test_osm_hotel_time_is_calculated_not_constant():
    central, quiet, _ = estimate_osm_hotels(
        "Пенза", PENZA, _area(), load_assumptions()
    )

    assert central.minutes_to_center == 7
    assert central.minutes_to_center != 25
    assert central.distance_to_center_km == 0.5
    assert central.minutes_to_metro == 6
    assert central.has_metro is True
    assert central.distance_to_metro_km == 0.4

    assert quiet.minutes_to_metro == 0
    assert quiet.has_metro is False
    assert quiet.distance_to_metro_km is None
    assert quiet.minutes_to_center == 13


def test_osm_hotel_prices_depend_on_kind_and_stars():
    central, quiet, hostel = estimate_osm_hotels(
        "Пенза", PENZA, _area(), load_assumptions()
    )

    assert central.price_per_night_rub == 8000
    assert quiet.price_per_night_rub == 3300
    assert hostel.price_per_night_rub == 1900


def _hotel() -> Hotel:
    return Hotel(
        id="hotel-1",
        name="Отель у вокзала",
        district="Центр",
        city="Пенза",
        price_per_night_rub=4000,
        rating=8.0,
        minutes_to_center=5,
        minutes_to_metro=4,
        latitude=PENZA.latitude,
        longitude=PENZA.longitude,
        price_kind="estimate",
        is_mock=False,
        source="тест",
    )


def test_hotel_transfer_offers_walk_and_taxi_when_close():
    transfer = estimate_hotel_transfer(
        _hotel(),
        node_latitude=PENZA.latitude + 0.006,
        node_longitude=PENZA.longitude,
        from_name="Вокзал",
        to_name="Отель у вокзала",
        title="Вокзал → отель",
        assumptions=load_assumptions(),
    )

    assert transfer is not None
    assert transfer.from_name == "Вокзал"
    assert {option.mode for option in transfer.options} == {"walk", "taxi"}
    cheapest = transfer.cheapest()
    assert cheapest is not None
    assert cheapest.mode == "walk"
    assert cheapest.price_rub == 0
    assert all(option.is_estimate for option in transfer.options)
    assert "rtt=pd" in transfer.by_mode("walk").yandex_url
    assert "rtt=auto" in transfer.by_mode("taxi").yandex_url
    assert all("openstreetmap.org/directions" in option.maps_url for option in transfer.options)


def test_hotel_transfer_adds_public_transport_when_far():
    transfer = estimate_hotel_transfer(
        _hotel(),
        node_latitude=PENZA.latitude + 0.12,
        node_longitude=PENZA.longitude,
        from_name="Аэропорт",
        to_name="Отель у вокзала",
        title="Аэропорт → отель",
        assumptions=load_assumptions(),
    )

    assert transfer is not None
    assert {option.mode for option in transfer.options} == {
        "public_transport",
        "taxi",
    }
    bus = transfer.by_mode("public_transport")
    taxi = transfer.by_mode("taxi")
    assert bus is not None and taxi is not None
    assert bus.price_rub < taxi.price_rub
    assert bus.minutes > taxi.minutes
    assert "rtt=mt" in bus.yandex_url
    assert "Маршрут и расписание не проверены" in bus.note


def test_hotel_transfer_skips_same_point():
    transfer = estimate_hotel_transfer(
        _hotel(),
        node_latitude=PENZA.latitude,
        node_longitude=PENZA.longitude,
        from_name="Вокзал",
        to_name="Отель у вокзала",
        title="Вокзал → отель",
        assumptions=load_assumptions(),
    )

    assert transfer is None
