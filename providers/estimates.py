"""Оценки транспорта и проживания, когда реальных цен нет.

Оценки считаются по расстоянию между городами и параметрам из
data/assumptions.json. Они всегда помечены price_kind="estimate" и
не выдаются за реальные тарифы или наличие мест.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from math import ceil

from models import (
    AccessLeg,
    Flight,
    Hotel,
    HotelTransfer,
    NO_DATA,
    TransferOption,
)
from providers.geo import GeoPoint, haversine_km
from providers.osm import LodgingArea, TransportNode, TransportNodes

_MODES = ("plane", "train", "bus")
_MODE_TO_KIND = {"plane": "flight", "train": "train", "bus": "bus"}
_MODE_TO_NODE = {"plane": "airport", "train": "train", "bus": "bus"}


def _round_price(value: float) -> float:
    return float(max(0, int(round(value / 50.0)) * 50))


def estimate_access_leg(
    center: GeoPoint, node: TransportNode, assumptions: dict
) -> AccessLeg | None:
    """Оценка дороги от центра места до узла: пешком или на такси."""
    distance = haversine_km(
        center.latitude,
        center.longitude,
        node.latitude,
        node.longitude,
    )
    if distance < 0.05:
        return None

    config = assumptions.get("access_estimates", {})
    source = str(config.get("source", "Оценка проекта: дорога до транспортного узла"))
    if distance <= float(config.get("walk_max_km", 2.0)):
        speed = max(1.0, float(config.get("walk_speed_kmh", 4.5)))
        return AccessLeg(
            mode="walk",
            distance_km=round(distance, 2),
            minutes=max(1, ceil(distance / speed * 60)),
            price_rub=0,
            source=source,
        )

    speed = max(1.0, float(config.get("taxi_speed_kmh", 40.0)))
    wait = max(0.0, float(config.get("taxi_wait_minutes", 5.0)))
    price = float(config.get("taxi_base_rub", 200.0)) + float(
        config.get("taxi_rub_per_km", 35.0)
    ) * distance
    return AccessLeg(
        mode="taxi",
        distance_km=round(distance, 2),
        minutes=max(1, ceil(distance / speed * 60 + wait)),
        price_rub=_round_price(price),
        source=source,
    )


def _osm_route_url(
    origin_latitude: float,
    origin_longitude: float,
    destination_latitude: float,
    destination_longitude: float,
    engine: str,
) -> str:
    """Ссылка на маршрут OpenStreetMap; координаты, а не выдуманный адрес."""
    return (
        "https://www.openstreetmap.org/directions"
        f"?engine={engine}"
        f"&route={origin_latitude:.5f}%2C{origin_longitude:.5f}"
        f"%3B{destination_latitude:.5f}%2C{destination_longitude:.5f}"
    )


def _yandex_route_url(
    origin_latitude: float,
    origin_longitude: float,
    destination_latitude: float,
    destination_longitude: float,
    mode: str,
) -> str:
    """Ссылка на маршрут в Яндекс Картах: пешком, транспортом или на машине.

    Это только карта с координатами, а не заказ такси: тариф и подачу
    нужно проверять в приложении Яндекс Go.
    """
    transport = {"walk": "pd", "public_transport": "mt"}.get(mode, "auto")
    return (
        "https://yandex.ru/maps/"
        f"?rtext={origin_latitude:.5f},{origin_longitude:.5f}"
        f"~{destination_latitude:.5f},{destination_longitude:.5f}"
        f"&rtt={transport}"
    )


def estimate_hotel_transfer(
    hotel: Hotel,
    *,
    node_latitude: float,
    node_longitude: float,
    from_name: str,
    to_name: str,
    title: str,
    assumptions: dict,
    transit_nearby: bool = False,
) -> HotelTransfer | None:
    """Оценка дороги между отелем и узлом: пешком, транспорт или такси.

    Цены и время — расчёт проекта по расстоянию. Это не тариф такси и не
    расписание общественного транспорта, поэтому все варианты помечены
    как оценка, а маршрут не выдаётся за проверенный.
    """
    distance = haversine_km(
        hotel.latitude,
        hotel.longitude,
        node_latitude,
        node_longitude,
    )
    if distance < 0.05:
        return None

    config = assumptions.get("access_estimates", {})
    source = str(
        config.get(
            "hotel_source",
            "Оценка проекта: дорога между отелем и транспортным узлом",
        )
    )
    walk_max = float(config.get("walk_max_km", 2.0))
    options: list[TransferOption] = []

    if distance <= walk_max:
        speed = max(1.0, float(config.get("walk_speed_kmh", 4.5)))
        options.append(
            TransferOption(
                mode="walk",
                distance_km=round(distance, 2),
                minutes=max(1, ceil(distance / speed * 60)),
                price_rub=0,
                source=source,
                note="Близко: можно дойти пешком, билет не нужен.",
                maps_url=_osm_route_url(
                    hotel.latitude,
                    hotel.longitude,
                    node_latitude,
                    node_longitude,
                    "fossgis_osrm_foot",
                ),
                yandex_url=_yandex_route_url(
                    hotel.latitude,
                    hotel.longitude,
                    node_latitude,
                    node_longitude,
                    "walk",
                ),
            )
        )

    if distance >= max(walk_max, float(config.get("public_transport_min_km", 1.0))):
        speed = max(1.0, float(config.get("public_transport_speed_kmh", 18.0)))
        wait = max(0.0, float(config.get("public_transport_wait_minutes", 12.0)))
        extra = max(0.0, float(config.get("public_transport_extra_minutes", 8.0)))
        price = float(config.get("public_transport_base_rub", 80.0)) + float(
            config.get("public_transport_rub_per_km", 6.0)
        ) * distance
        note = (
            "Рядом с отелем есть метро или вокзал по OpenStreetMap; "
            "конкретный маршрут и расписание не проверены."
            if transit_nearby
            else "Маршрут и расписание не проверены: остановки рядом с отелем "
            "не подтверждены."
        )
        options.append(
            TransferOption(
                mode="public_transport",
                distance_km=round(distance, 2),
                minutes=max(1, ceil(distance / speed * 60 + wait + extra)),
                price_rub=_round_price(price),
                source=source,
                note=note,
                yandex_url=_yandex_route_url(
                    hotel.latitude,
                    hotel.longitude,
                    node_latitude,
                    node_longitude,
                    "public_transport",
                ),
            )
        )

    speed = max(1.0, float(config.get("taxi_speed_kmh", 40.0)))
    wait = max(0.0, float(config.get("taxi_wait_minutes", 5.0)))
    price = float(config.get("taxi_base_rub", 200.0)) + float(
        config.get("taxi_rub_per_km", 35.0)
    ) * distance
    options.append(
        TransferOption(
            mode="taxi",
            distance_km=round(distance, 2),
            minutes=max(1, ceil(distance / speed * 60 + wait)),
            price_rub=_round_price(price),
            source=source,
            note="Оценка по расстоянию: тариф, подача и пробки не проверены.",
            maps_url=_osm_route_url(
                hotel.latitude,
                hotel.longitude,
                node_latitude,
                node_longitude,
                "fossgis_osrm_car",
            ),
            yandex_url=_yandex_route_url(
                hotel.latitude,
                hotel.longitude,
                node_latitude,
                node_longitude,
                "taxi",
            ),
        )
    )

    options.sort(key=lambda option: (option.price_rub, option.minutes))
    return HotelTransfer(
        title=title,
        from_name=from_name,
        to_name=to_name,
        distance_km=round(distance, 2),
        options=options,
        source=source,
    )


def estimate_transport(
    origin: GeoPoint,
    destination: GeoPoint,
    depart: date,
    return_date: date,
    assumptions: dict,
    origin_nodes: TransportNodes | None = None,
    destination_nodes: TransportNodes | None = None,
) -> list[Flight]:
    """Возвращает оценки поездки туда и обратно для доступных видов транспорта.

    Если переданы узлы OpenStreetMap, самолёт/поезд/автобус предлагаются
    только при наличии аэропорта/вокзала/автостанции рядом с обоими
    концами маршрута. Без узлов (источник недоступен) работает прежняя
    оценка по расстоянию между центрами с честной пометкой.
    """
    config = assumptions.get("transport_estimates", {})
    source = str(config.get("source", "Оценка проекта по расстоянию"))
    if origin_nodes is not None and destination_nodes is not None:
        return _estimate_between_nodes(
            origin,
            destination,
            depart,
            return_date,
            assumptions,
            config,
            source,
            origin_nodes,
            destination_nodes,
        )

    return _estimate_by_centers(
        origin,
        destination,
        depart,
        return_date,
        config,
        f"{source} · наличие узлов не проверено",
    )


def _estimate_by_centers(
    origin: GeoPoint,
    destination: GeoPoint,
    depart: date,
    return_date: date,
    config: dict,
    source: str,
) -> list[Flight]:
    distance = haversine_km(
        origin.latitude,
        origin.longitude,
        destination.latitude,
        destination.longitude,
    )
    if distance <= 0:
        return []

    options: list[Flight] = []
    for mode in _MODES:
        mode_config = config.get(mode) or {}
        if not mode_config or distance > float(mode_config.get("max_km", 0)):
            continue
        duration = timedelta(
            minutes=float(mode_config.get("ground_minutes", 0))
            + distance / float(mode_config.get("speed_kmh", 1)) * 60.0
        )
        price = _round_price(
            float(mode_config.get("base_rub", 0))
            + float(mode_config.get("rub_per_km", 0)) * distance
        )
        label = str(mode_config.get("label", f"{mode} (оценка)"))
        kind = _MODE_TO_KIND[mode]
        options.append(
            _option(
                f"est-{mode}-out",
                "outbound",
                origin.city,
                destination.city,
                label,
                datetime.combine(depart, time(8, 0)),
                duration,
                price,
                kind,
                source,
            )
        )
        options.append(
            _option(
                f"est-{mode}-back",
                "return",
                destination.city,
                origin.city,
                label,
                datetime.combine(return_date, time(17, 0)),
                duration,
                price,
                kind,
                source,
            )
        )
    return options


def _estimate_between_nodes(
    origin: GeoPoint,
    destination: GeoPoint,
    depart: date,
    return_date: date,
    assumptions: dict,
    config: dict,
    source: str,
    origin_nodes: TransportNodes,
    destination_nodes: TransportNodes,
) -> list[Flight]:
    node_source = f"{source} · узлы OpenStreetMap"
    options: list[Flight] = []
    for mode in _MODES:
        mode_config = config.get(mode) or {}
        if not mode_config:
            continue
        node_kind = _MODE_TO_NODE[mode]
        origin_node = origin_nodes.nearest(node_kind)
        destination_node = destination_nodes.nearest(node_kind)
        if origin_node is None or destination_node is None:
            continue

        distance = haversine_km(
            origin_node.latitude,
            origin_node.longitude,
            destination_node.latitude,
            destination_node.longitude,
        )
        if distance <= 0 or distance > float(mode_config.get("max_km", 0)):
            continue

        duration = timedelta(
            minutes=float(mode_config.get("ground_minutes", 0))
            + distance / float(mode_config.get("speed_kmh", 1)) * 60.0
        )
        price = _round_price(
            float(mode_config.get("base_rub", 0))
            + float(mode_config.get("rub_per_km", 0)) * distance
        )
        label = str(mode_config.get("label", f"{mode} (оценка)"))
        kind = _MODE_TO_KIND[mode]
        origin_access = estimate_access_leg(origin, origin_node, assumptions)
        destination_access = estimate_access_leg(destination, destination_node, assumptions)

        options.append(
            _option(
                f"est-{mode}-out",
                "outbound",
                origin.city,
                destination.city,
                label,
                datetime.combine(depart, time(8, 0)),
                duration,
                price,
                kind,
                node_source,
                departure_node=origin_node.name,
                arrival_node=destination_node.name,
                departure_node_latitude=origin_node.latitude,
                departure_node_longitude=origin_node.longitude,
                arrival_node_latitude=destination_node.latitude,
                arrival_node_longitude=destination_node.longitude,
                access_to_departure=origin_access,
                access_from_arrival=destination_access,
            )
        )
        options.append(
            _option(
                f"est-{mode}-back",
                "return",
                destination.city,
                origin.city,
                label,
                datetime.combine(return_date, time(17, 0)),
                duration,
                price,
                kind,
                node_source,
                departure_node=destination_node.name,
                arrival_node=origin_node.name,
                departure_node_latitude=destination_node.latitude,
                departure_node_longitude=destination_node.longitude,
                arrival_node_latitude=origin_node.latitude,
                arrival_node_longitude=origin_node.longitude,
                access_to_departure=destination_access,
                access_from_arrival=origin_access,
            )
        )
    return options


def _option(
    option_id: str,
    direction: str,
    origin: str,
    destination: str,
    label: str,
    departure: datetime,
    duration: timedelta,
    price: float,
    mode: str,
    source: str,
    *,
    departure_node: str = "",
    arrival_node: str = "",
    departure_node_latitude: float | None = None,
    departure_node_longitude: float | None = None,
    arrival_node_latitude: float | None = None,
    arrival_node_longitude: float | None = None,
    access_to_departure: AccessLeg | None = None,
    access_from_arrival: AccessLeg | None = None,
) -> Flight:
    return Flight(
        id=option_id,
        direction=direction,
        origin=origin,
        destination=destination,
        airline=label,
        departure_time=departure,
        arrival_time=departure + duration,
        price_rub=price,
        stops=0,
        layover_minutes=[],
        connection_type="none",
        baggage_included=False,
        baggage_fee_rub=0,
        mode=mode,
        price_kind="estimate",
        is_mock=False,
        source=source,
        departure_node=departure_node,
        arrival_node=arrival_node,
        departure_node_latitude=departure_node_latitude,
        departure_node_longitude=departure_node_longitude,
        arrival_node_latitude=arrival_node_latitude,
        arrival_node_longitude=arrival_node_longitude,
        access_to_departure=access_to_departure,
        access_from_arrival=access_from_arrival,
    )


def estimate_hotel(city: str, point: GeoPoint, assumptions: dict) -> Hotel:
    """Одна средняя оценка проживания: не конкретный отель и не его цена."""
    config = assumptions.get("hotel_estimates", {})
    return Hotel(
        id="est-hotel",
        name=f"Среднее проживание в городе {city} (оценка)",
        district="Центр (оценка)",
        city=city,
        price_per_night_rub=float(config.get("price_per_night_rub", 4500)),
        rating=float(config.get("rating", 5.0)),
        minutes_to_center=int(config.get("minutes_to_center", 25)),
        minutes_to_metro=int(config.get("minutes_to_metro", 15)),
        tourist_tax_per_night_rub=float(config.get("tourist_tax_per_night_rub", 0)),
        latitude=point.latitude,
        longitude=point.longitude,
        tags=[],
        rating_source=NO_DATA,
        is_mock=False,
        source=str(config.get("source", "Оценка проекта: средняя стоимость проживания")),
        price_kind="estimate",
    )


_QUIET_LODGING_KINDS = {
    "guest_house",
    "guesthouse",
    "chalet",
    "camp_site",
    "caravan_site",
    "alpine_hut",
}
_DEFAULT_LODGING_PRICES = {
    "hotel": 4500,
    "hostel": 1900,
    "guest_house": 3300,
    "guesthouse": 3300,
    "apartment": 4300,
    "motel": 3000,
    "chalet": 5200,
    "camp_site": 1300,
    "caravan_site": 1500,
    "alpine_hut": 2000,
}


def _travel_minutes(distance_km: float, config: dict) -> int:
    """Время от отеля до ориентира: пешком рядом, иначе на такси."""
    distance = max(0.0, distance_km)
    if distance <= 0:
        return 0
    if distance <= float(config.get("center_walk_max_km", 2.0)):
        speed = max(1.0, float(config.get("walk_speed_kmh", 4.5)))
        return max(1, ceil(distance / speed * 60))
    speed = max(1.0, float(config.get("taxi_speed_kmh", 30.0)))
    wait = max(0.0, float(config.get("taxi_wait_minutes", 5.0)))
    return max(1, ceil(distance / speed * 60 + wait))


def _stars(tags: dict[str, str]) -> int:
    raw = tags.get("stars") or tags.get("star_rating") or ""
    try:
        value = int(float(str(raw).replace(",", ".")))
    except (TypeError, ValueError):
        return 0
    return min(5, max(0, value))


def _lodging_district(tags: dict[str, str]) -> str:
    for key in ("addr:suburb", "addr:quarter", "addr:city_district", "addr:district"):
        value = str(tags.get(key, "")).strip()
        if value:
            return value
    return "Район не указан в OpenStreetMap"


def estimate_osm_hotels(
    city: str,
    point: GeoPoint,
    area: LodgingArea,
    assumptions: dict,
) -> list[Hotel]:
    """Превращает найденные в OSM отели в варианты проживания.

    Название и координаты — из OpenStreetMap. Цена, рейтинг, время до
    центра и метро — оценка проекта, потому что этих данных в OSM нет.
    """
    config = assumptions.get("hotel_search", {})
    base_prices = {**_DEFAULT_LODGING_PRICES, **config.get("base_price_rub", {})}
    star_price = float(config.get("star_price_rub", 700))
    center_max = float(config.get("center_max_km", 2.5))
    water_max = float(config.get("water_max_km", 0.8))
    station_max = float(config.get("station_max_km", 2.0))
    rating = float(assumptions.get("hotel_estimates", {}).get("rating", 5.0))
    tourist_tax = float(
        assumptions.get("hotel_estimates", {}).get("tourist_tax_per_night_rub", 0)
    )
    source = str(
        config.get(
            "source",
            "OpenStreetMap (Overpass API); цена и время — оценка проекта",
        )
    )

    hotels: list[Hotel] = []
    for index, node in enumerate(area.lodgings):
        price = float(base_prices.get(node.kind, 4500))
        stars = _stars(node.tags)
        if stars and node.kind in {"hotel", "motel", "apartment", "guest_house", "guesthouse"}:
            price += stars * star_price

        tags = [node.kind_label]
        if node.distance_to_center_km <= center_max:
            tags.append("в центре")
        if node.distance_to_water_km is not None and node.distance_to_water_km <= water_max:
            tags.append("у воды")
        if node.distance_to_station_km is not None and node.distance_to_station_km <= station_max:
            tags.append("у вокзала")
        if node.kind in _QUIET_LODGING_KINDS and "в центре" not in tags:
            tags.append("тихо")

        hotels.append(
            Hotel(
                id=f"{node.id}-hotel-{index}",
                name=node.name,
                district=_lodging_district(node.tags),
                city=city,
                price_per_night_rub=float(_round_price(price)),
                rating=rating,
                minutes_to_center=_travel_minutes(node.distance_to_center_km, config),
                minutes_to_metro=(
                    _travel_minutes(node.distance_to_metro_km, config)
                    if node.distance_to_metro_km is not None
                    else 0
                ),
                tourist_tax_per_night_rub=tourist_tax,
                latitude=node.latitude,
                longitude=node.longitude,
                tags=tags,
                rating_source=NO_DATA,
                is_mock=False,
                source=source,
                distance_to_center_km=node.distance_to_center_km,
                distance_to_metro_km=node.distance_to_metro_km,
                distance_to_station_km=node.distance_to_station_km,
                has_metro=node.distance_to_metro_km is not None,
                price_kind="estimate",
            )
        )
    return hotels
