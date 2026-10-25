"""Транспортные узлы из OpenStreetMap через Overpass API.

Модуль отвечает на вопрос «есть ли рядом аэропорт, вокзал или автостанция»,
а не «сколько стоит билет». Цены и время в пути по-прежнему оцениваются
проектом и помечаются как оценка. При недоступности Overpass клиент
выбрасывает ProviderError — вызывающая сторона сама решает, как откатиться.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import requests

from providers.base import ProviderError
from providers.geo import GeoPoint, haversine_km

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OSM_SOURCE = "OpenStreetMap (Overpass API)"

DEFAULT_RADII_KM = {
    "airport": 150.0,
    "train": 80.0,
    "bus": 60.0,
}

# Виды проживания, которые считаем реальными вариантами из OpenStreetMap.
LODGING_KINDS = {
    "hotel": "отель",
    "hostel": "хостел",
    "guest_house": "гостевой дом",
    "guesthouse": "гостевой дом",
    "apartment": "апартаменты",
    "motel": "мотель",
    "chalet": "шале",
    "camp_site": "кемпинг",
    "caravan_site": "кемпинг",
    "alpine_hut": "приют",
}
LODGING_PATTERN = "|".join(LODGING_KINDS)

NodeKind = Literal["airport", "train", "bus"]


@dataclass(frozen=True)
class TransportNode:
    """Аэропорт, железнодорожная станция или автостанция."""

    id: str
    name: str
    kind: NodeKind
    latitude: float
    longitude: float
    distance_km: float
    tags: dict[str, str] = field(default_factory=dict)
    source: str = OSM_SOURCE


@dataclass(frozen=True)
class TransportNodes:
    """Найденные узлы рядом с одним местом, отсортированные по расстоянию."""

    airports: list[TransportNode] = field(default_factory=list)
    train_stations: list[TransportNode] = field(default_factory=list)
    bus_stations: list[TransportNode] = field(default_factory=list)

    def by_kind(self, kind: NodeKind) -> list[TransportNode]:
        return {
            "airport": self.airports,
            "train": self.train_stations,
            "bus": self.bus_stations,
        }.get(kind, [])

    def nearest(self, kind: NodeKind) -> TransportNode | None:
        nodes = self.by_kind(kind)
        return nodes[0] if nodes else None


@dataclass(frozen=True)
class LodgingNode:
    """Отель, хостел, гостевой дом или кемпинг из OpenStreetMap."""

    id: str
    name: str
    kind: str
    latitude: float
    longitude: float
    distance_to_center_km: float
    distance_to_station_km: float | None = None
    distance_to_metro_km: float | None = None
    distance_to_water_km: float | None = None
    tags: dict[str, str] = field(default_factory=dict)
    source: str = OSM_SOURCE

    @property
    def kind_label(self) -> str:
        return LODGING_KINDS.get(self.kind, "жильё")


@dataclass(frozen=True)
class LodgingArea:
    """Найденное жильё и расстояния до ориентиров вокруг одного места."""

    lodgings: list[LodgingNode] = field(default_factory=list)
    station_count: int = 0
    metro_count: int = 0
    water_count: int = 0


def _normalize(value: str) -> str:
    return value.strip().casefold().replace("ё", "е")


def _kind(tags: dict[str, str]) -> NodeKind | None:
    if tags.get("aeroway") == "aerodrome":
        return "airport" if _airport_is_public(tags) else None
    if tags.get("railway") in {"station", "halt"}:
        if tags.get("station") == "subway" or tags.get("subway") == "yes":
            return None
        if tags.get("usage") in {"freight", "industrial"}:
            return None
        if tags.get("disused") in {"yes", "railway"}:
            return None
        return "train"
    if tags.get("amenity") == "bus_station":
        return "bus"
    return None


def _airport_is_public(tags: dict[str, str]) -> bool:
    """Отсекаем военные, частные и планерные площадки."""
    name = _normalize(tags.get("name") or tags.get("name:ru") or "")
    aerodrome_type = (tags.get("aerodrome:type") or "").strip().casefold()
    if aerodrome_type in {"private", "military", "ultralight", "gliding"}:
        return False
    if tags.get("military") in {"airfield", "yes"} or tags.get("access") == "private":
        return False
    if any(word in name for word in ("аэродром", "airfield", "gliding", "планерн", "досааф")):
        return False
    return bool(
        tags.get("iata")
        or tags.get("icao")
        or "аэропорт" in name
        or "airport" in name
        or aerodrome_type in {"international", "regional", "national", "civil", "public"}
    )


def _node_name(tags: dict[str, str], kind: NodeKind) -> str:
    for key in ("name:ru", "name", "official_name"):
        value = tags.get(key)
        if value and str(value).strip():
            return str(value).strip()
    if kind == "airport":
        code = tags.get("iata") or tags.get("icao")
        return f"Аэропорт {code}" if code else "Аэропорт"
    return {"train": "Ж/д станция", "bus": "Автостанция"}[kind]


def _query(latitude: float, longitude: float, radius_m: int) -> str:
    return (
        "[out:json][timeout:25];\n"
        "(\n"
        f'  nwr["aeroway"="aerodrome"](around:{radius_m},{latitude},{longitude});\n'
        f'  nwr["railway"="station"](around:{radius_m},{latitude},{longitude});\n'
        f'  nwr["railway"="halt"](around:{radius_m},{latitude},{longitude});\n'
        f'  nwr["amenity"="bus_station"](around:{radius_m},{latitude},{longitude});\n'
        ");\n"
        "out tags center 300;\n"
    )


def _lodging_query(
    latitude: float,
    longitude: float,
    radius_m: int,
    support_radius_m: int,
) -> str:
    """Жильё плюс ориентиры для тем: вокзал, метро и вода."""
    return (
        "[out:json][timeout:30];\n"
        f'nwr["tourism"~"^({LODGING_PATTERN})$"]'
        f"(around:{radius_m},{latitude},{longitude})->.lodging;\n"
        ".lodging out tags center 120;\n"
        f'nwr["railway"="station"]'
        f"(around:{support_radius_m},{latitude},{longitude})->.stations;\n"
        ".stations out tags center 40;\n"
        f'nwr["railway"="subway_entrance"]'
        f"(around:{support_radius_m},{latitude},{longitude})->.metro_entrances;\n"
        ".metro_entrances out tags center 40;\n"
        "(\n"
        f'  nwr["natural"~"^(water|bay)$"](around:{support_radius_m},{latitude},{longitude});\n'
        f'  nwr["waterway"~"^(river|riverbank|canal)$"](around:{support_radius_m},{latitude},{longitude});\n'
        ")->.water;\n"
        ".water out tags center 60;\n"
    )


def _element_tags(item: dict) -> dict[str, str]:
    tags = item.get("tags")
    return {str(key): str(value) for key, value in tags.items()} if isinstance(tags, dict) else {}


def _element_coordinates(item: dict) -> tuple[float, float] | None:
    latitude = item.get("lat")
    longitude = item.get("lon")
    if latitude is None or longitude is None:
        center = item.get("center")
        center = center if isinstance(center, dict) else {}
        latitude = center.get("lat")
        longitude = center.get("lon")
    try:
        return float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None


def _element_id(item: dict) -> str:
    return f"osm-{item.get('type') or 'node'}-{item.get('id') or ''}"


def _element_name(tags: dict[str, str]) -> str:
    for key in ("name:ru", "name", "official_name"):
        value = tags.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return ""


class OsmClient:
    """Клиент Overpass с кэшем по координатам."""

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout: int = 30,
    ) -> None:
        self._session = session or requests.Session()
        self._timeout = timeout
        self._cache: dict[tuple, list[TransportNode]] = {}
        self._lodging_cache: dict[tuple, LodgingArea] = {}

    def _get(self, query: str) -> dict:
        try:
            response = self._session.post(
                OVERPASS_URL,
                data={"data": query},
                headers={
                    "User-Agent": "OneTripRealityCheck/0.1 (hackathon demo)",
                    "Accept": "application/json",
                },
                timeout=self._timeout,
            )
        except requests.RequestException:
            raise ProviderError("не удалось подключиться к OpenStreetMap (Overpass)") from None
        if response.status_code != 200:
            raise ProviderError(
                f"OpenStreetMap вернул ошибку (код {response.status_code})"
            )
        try:
            data = response.json()
        except ValueError:
            raise ProviderError("OpenStreetMap вернул ответ неожиданного формата") from None
        if not isinstance(data, dict):
            raise ProviderError("OpenStreetMap вернул ответ неожиданного формата")
        return data

    def transport_nodes(
        self,
        point: GeoPoint,
        *,
        radii_km: dict[str, float] | None = None,
        limit: int = 3,
    ) -> TransportNodes:
        """Ищет ближайшие узлы в заданных радиусах вокруг точки."""
        radii = {**DEFAULT_RADII_KM, **(radii_km or {})}
        radii = {kind: max(0.0, float(value)) for kind, value in radii.items()}
        max_radius = max(radii.values(), default=0.0)
        if max_radius <= 0:
            return TransportNodes()

        key = (
            round(point.latitude, 2),
            round(point.longitude, 2),
            tuple(sorted((kind, round(value, 1)) for kind, value in radii.items())),
            max(1, int(limit)),
        )
        if key not in self._cache:
            radius_m = int(max_radius * 1000)
            data = self._get(_query(point.latitude, point.longitude, radius_m))
            elements = data.get("elements")
            if not isinstance(elements, list):
                elements = []
            nodes = [
                node
                for node in (_parse_element(item, point) for item in elements)
                if node is not None
            ]
            nodes.sort(key=lambda node: node.distance_km)
            self._cache[key] = _dedupe(nodes)

        grouped: dict[NodeKind, list[TransportNode]] = {
            "airport": [],
            "train": [],
            "bus": [],
        }
        for node in self._cache[key]:
            if node.distance_km <= radii.get(node.kind, 0.0):
                grouped[node.kind].append(node)
        return TransportNodes(
            airports=grouped["airport"][: max(1, int(limit))],
            train_stations=grouped["train"][: max(1, int(limit))],
            bus_stations=grouped["bus"][: max(1, int(limit))],
        )

    def lodging(
        self,
        point: GeoPoint,
        *,
        radius_km: float = 10.0,
        support_radius_km: float = 5.0,
        limit: int = 40,
    ) -> LodgingArea:
        """Ищет отели, хостелы, гостевые дома и кемпинги вокруг точки.

        Вместе с жильём запрашиваются вокзалы, входы в метро и вода:
        расстояния до них нужны для тем «у вокзала», «у воды» и для
        проверки «до метро», а не для выдуманных констант.
        """
        radius = max(0.0, float(radius_km))
        support = max(0.0, float(support_radius_km))
        if radius <= 0:
            return LodgingArea()
        key = (
            round(point.latitude, 2),
            round(point.longitude, 2),
            round(radius, 1),
            round(support, 1),
            max(1, int(limit)),
        )
        if key not in self._lodging_cache:
            data = self._get(
                _lodging_query(
                    point.latitude,
                    point.longitude,
                    int(radius * 1000),
                    int(max(radius, support) * 1000),
                )
            )
            elements = data.get("elements")
            if not isinstance(elements, list):
                elements = []
            self._lodging_cache[key] = _parse_lodging_area(elements, point, limit)
        return self._lodging_cache[key]


def _parse_lodging_area(elements: list, point: GeoPoint, limit: int) -> LodgingArea:
    stations: list[tuple[float, float]] = []
    metro: list[tuple[float, float]] = []
    water: list[tuple[float, float]] = []
    candidates: list[tuple[dict, str, str, float, float, dict[str, str]]] = []

    for item in elements:
        if not isinstance(item, dict):
            continue
        tags = _element_tags(item)
        coordinates = _element_coordinates(item)
        if coordinates is None:
            continue
        latitude, longitude = coordinates

        if tags.get("natural") in {"water", "bay"} or tags.get("waterway") in {
            "river",
            "riverbank",
            "canal",
        }:
            water.append((latitude, longitude))
            continue
        if tags.get("railway") == "subway_entrance" or (
            tags.get("railway") in {"station", "halt"}
            and (tags.get("station") == "subway" or tags.get("subway") == "yes")
        ):
            metro.append((latitude, longitude))
            continue
        if tags.get("railway") in {"station", "halt"}:
            stations.append((latitude, longitude))
            continue

        kind = tags.get("tourism", "")
        if kind not in LODGING_KINDS:
            continue
        if tags.get("abandoned") == "yes" or tags.get("disused") == "yes":
            continue
        name = _element_name(tags)
        if not name:
            continue
        candidates.append((item, name, kind, latitude, longitude, tags))

    lodgings: list[LodgingNode] = []
    for item, name, kind, latitude, longitude, tags in candidates:
        lodgings.append(
            LodgingNode(
                id=_element_id(item),
                name=name,
                kind=kind,
                latitude=latitude,
                longitude=longitude,
                distance_to_center_km=round(
                    haversine_km(point.latitude, point.longitude, latitude, longitude), 2
                ),
                distance_to_station_km=_nearest_km(latitude, longitude, stations),
                distance_to_metro_km=_nearest_km(latitude, longitude, metro),
                distance_to_water_km=_nearest_km(latitude, longitude, water),
                tags=tags,
            )
        )

    lodgings.sort(key=lambda node: node.distance_to_center_km)
    unique: list[LodgingNode] = []
    for node in lodgings:
        duplicate = any(
            _normalize(other.name) == _normalize(node.name)
            and haversine_km(
                other.latitude,
                other.longitude,
                node.latitude,
                node.longitude,
            )
            < 0.3
            for other in unique
        )
        if not duplicate:
            unique.append(node)

    return LodgingArea(
        lodgings=unique[: max(1, int(limit))],
        station_count=len(stations),
        metro_count=len(metro),
        water_count=len(water),
    )


def _nearest_km(latitude: float, longitude: float, targets: list[tuple[float, float]]) -> float | None:
    if not targets:
        return None
    return round(
        min(
            haversine_km(latitude, longitude, target_lat, target_lon)
            for target_lat, target_lon in targets
        ),
        2,
    )


def _parse_element(item, point: GeoPoint) -> TransportNode | None:
    if not isinstance(item, dict):
        return None
    tags = item.get("tags")
    tags = {str(key): str(value) for key, value in tags.items()} if isinstance(tags, dict) else {}
    kind = _kind(tags)
    if kind is None:
        return None

    latitude = item.get("lat")
    longitude = item.get("lon")
    if latitude is None or longitude is None:
        center = item.get("center")
        center = center if isinstance(center, dict) else {}
        latitude = center.get("lat")
        longitude = center.get("lon")
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return None

    distance = haversine_km(point.latitude, point.longitude, latitude, longitude)
    element_type = str(item.get("type") or "node")
    element_id = str(item.get("id") or "")
    return TransportNode(
        id=f"osm-{element_type}-{element_id}",
        name=_node_name(tags, kind),
        kind=kind,
        latitude=latitude,
        longitude=longitude,
        distance_km=round(distance, 2),
        tags=tags,
    )


def _dedupe(nodes: list[TransportNode]) -> list[TransportNode]:
    """Убирает дубли одного объекта (узел и полигон) рядом друг с другом."""
    unique: list[TransportNode] = []
    for node in nodes:
        duplicate = any(
            other.kind == node.kind
            and _normalize(other.name) == _normalize(node.name)
            and haversine_km(
                other.latitude,
                other.longitude,
                node.latitude,
                node.longitude,
            )
            < 2.0
            for other in unique
        )
        if not duplicate:
            unique.append(node)
    return unique
