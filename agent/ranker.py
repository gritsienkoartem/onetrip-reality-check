"""Детерминированное ранжирование вариантов по весам проекта.

Здесь нет языковой модели: одинаковые данные и веса всегда дают
одинаковый порядок. Веса берутся из data/assumptions.json.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from models import Flight, Hotel, Place, TripRequest

DEFAULT_FLIGHT_WEIGHTS = {"price": 0.6, "duration": 0.3, "stops": 0.1}
DEFAULT_HOTEL_WEIGHTS = {"price": 0.35, "rating": 0.25, "location": 0.25, "preferences": 0.15}
DEFAULT_PLACE_WEIGHTS = {"interest_match": 0.45, "rating": 0.25, "price": 0.15, "crowd": 0.15}


@dataclass(frozen=True)
class FlightPair:
    """Пара «туда + обратно» с оценкой и итоговыми метриками."""

    outbound: Flight
    inbound: Flight
    score: float
    total_price_rub: float
    total_duration_minutes: int
    total_stops: int


def _duration_minutes(flight: Flight) -> int:
    return int((flight.arrival_time - flight.departure_time).total_seconds() // 60)


def _normalize_lower(values: Sequence[float]) -> list[float]:
    """Чем меньше значение, тем выше оценка (от 0 до 1)."""
    if not values:
        return []
    low, high = min(values), max(values)
    if high == low:
        return [1.0] * len(values)
    return [(high - value) / (high - low) for value in values]


def _normalize_higher(values: Sequence[float]) -> list[float]:
    """Чем больше значение, тем выше оценка (от 0 до 1)."""
    if not values:
        return []
    low, high = min(values), max(values)
    if high == low:
        return [1.0] * len(values)
    return [(value - low) / (high - low) for value in values]


def _normalize_known(values: Sequence[float | None]) -> list[float]:
    """Как _normalize_lower, но неизвестное значение получает нейтральные 0.5.

    Ноль в модели означает «источник не сообщил расстояние», а не «рядом»,
    поэтому нельзя ставить такой отель первым только из-за нуля.
    """
    known = [value for value in values if value is not None]
    if not known:
        return [0.5] * len(values)
    low, high = min(known), max(known)
    if high == low:
        return [0.5 if value is None else 1.0 for value in values]
    return [
        0.5 if value is None else (high - value) / (high - low)
        for value in values
    ]


def _merge_weights(defaults: Mapping[str, float], weights: Mapping[str, float] | None) -> dict[str, float]:
    merged = dict(defaults)
    if weights:
        merged.update(weights)
    return merged


def rank_flight_pairs(
    outbound: Sequence[Flight],
    inbound: Sequence[Flight],
    weights: Mapping[str, float] | None = None,
) -> list[FlightPair]:
    """Складывает рейсы туда и обратно и сортирует пары по оценке.

    Учитываются цена, суммарное время в пути и число пересадок.
    Обратный рейс должен вылетать после прилёта прямого.
    """
    ordered = _merge_weights(DEFAULT_FLIGHT_WEIGHTS, weights)
    candidates: list[tuple[Flight, Flight]] = [
        (out, back)
        for out in outbound
        if out.direction == "outbound"
        for back in inbound
        # Смешивать виды транспорта нельзя: поезд туда и самолёт обратно —
        # это не единый вариант поездки.
        if back.direction == "return" and back.mode == out.mode and back.departure_time > out.arrival_time
    ]
    if not candidates:
        return []

    prices = [out.price_rub + back.price_rub for out, back in candidates]
    durations = [_duration_minutes(out) + _duration_minutes(back) for out, back in candidates]
    stops = [out.stops + back.stops for out, back in candidates]

    price_scores = _normalize_lower(prices)
    duration_scores = _normalize_lower(durations)
    stop_scores = _normalize_lower(stops)

    pairs: list[FlightPair] = []
    for index, (out, back) in enumerate(candidates):
        score = (
            ordered["price"] * price_scores[index]
            + ordered["duration"] * duration_scores[index]
            + ordered["stops"] * stop_scores[index]
        )
        pairs.append(
            FlightPair(
                outbound=out,
                inbound=back,
                score=round(score, 4),
                total_price_rub=prices[index],
                total_duration_minutes=durations[index],
                total_stops=stops[index],
            )
        )

    pairs.sort(key=lambda pair: (-pair.score, pair.total_price_rub, pair.total_duration_minutes,
                                 pair.outbound.id, pair.inbound.id))
    return pairs


def _normalize_text(value: str) -> str:
    return value.strip().casefold().replace("ё", "е")


def _text_match(left: str, right: str) -> bool:
    """Мягкое сравнение слов: учитывает разные окончания."""
    if not left or not right:
        return False
    if left == right or left in right or right in left:
        return True
    return len(left) >= 4 and len(right) >= 4 and left[:4] == right[:4]


_UNUSUAL_LODGING_WORDS = (
    "кемпинг",
    "глэмпинг",
    "шале",
    "приют",
    "лофт",
    "бутик",
    "маяк",
    "барнхаус",
    "капсул",
    "дерево",
    "лодк",
    "яхт",
)
_HOTEL_NAME_WORDS = ("отель", "гостиниц", "hotel", "бутик", "лофт")


def _preference_matches(hotel: Hotel, preference: str) -> bool:
    text = _normalize_text(preference)
    tags = [_normalize_text(tag) for tag in hotel.tags]
    name = _normalize_text(hotel.name)

    if text == "в центре":
        return hotel.center_known and hotel.minutes_to_center <= 15
    if text == "рядом с метро":
        return hotel.metro_known and (
            (hotel.has_metro and hotel.minutes_to_metro <= 10)
            or any("метро" in tag for tag in tags)
        )
    if text == "тихий район":
        return any("тих" in tag for tag in tags)
    if text == "у воды":
        return any("у воды" in tag for tag in tags)
    if text == "у вокзала":
        return any("вокзал" in tag for tag in tags)
    if text == "хостел":
        return "хостел" in name or any("хостел" in tag for tag in tags)
    if text == "апартаменты":
        return "апартамент" in name or any("апартамент" in tag for tag in tags)
    if text in {"кемпинг", "глэмпинг", "кемпинг / глэмпинг"}:
        return any(word in tag for tag in tags for word in ("кемпинг", "глэмпинг")) or any(
            word in name for word in ("кемпинг", "глэмпинг")
        )
    if text == "гостевой дом":
        return "гостевой дом" in name or any("гостевой дом" in tag for tag in tags)
    if text == "отель":
        if any(
            word in name
            for word in ("хостел", "гостевой дом", "апартамент", "кемпинг", "глэмпинг", "шале", "приют")
        ):
            return False
        return any(word in name for word in _HOTEL_NAME_WORDS) or any(
            "отель" in tag for tag in tags
        )
    if text == "необычное жильё":
        return any(
            word in value
            for value in tags + [name]
            for word in _UNUSUAL_LODGING_WORDS
        )

    return any(_text_match(text, tag) for tag in tags) or _text_match(text, name)


def _preference_score(hotel: Hotel, request: TripRequest) -> float:
    preferences = request.accommodation_preferences
    if not preferences:
        return 1.0  # пожеланий нет — этот критерий никого не штрафует
    matched = sum(1 for preference in preferences if _preference_matches(hotel, preference))
    return matched / len(preferences)


def unmatched_hotel_preferences(
    hotels: Sequence[Hotel], request: TripRequest
) -> list[str]:
    """Пожелания к жилью, которые не подтвердил ни один найденный вариант."""
    if not hotels or not request.accommodation_preferences:
        return []
    return [
        preference
        for preference in request.accommodation_preferences
        if not any(_preference_matches(hotel, preference) for hotel in hotels)
    ]


def rank_hotels(
    hotels: Sequence[Hotel],
    request: TripRequest,
    weights: Mapping[str, float] | None = None,
) -> list[Hotel]:
    """Сортирует отели по цене, рейтингу, расположению и пожеланиям."""
    if not hotels:
        return []
    ordered = _merge_weights(DEFAULT_HOTEL_WEIGHTS, weights)

    price_scores = _normalize_lower([hotel.price_per_night_rub for hotel in hotels])
    rating_scores = [hotel.rating / 10 for hotel in hotels]
    center_scores = _normalize_known(
        [hotel.minutes_to_center if hotel.center_known else None for hotel in hotels]
    )
    metro_scores = _normalize_known(
        [
            hotel.minutes_to_metro if hotel.metro_known and hotel.has_metro else None
            for hotel in hotels
        ]
    )
    preference_scores = [_preference_score(hotel, request) for hotel in hotels]

    scored: list[tuple[float, Hotel]] = []
    for index, hotel in enumerate(hotels):
        location = (center_scores[index] + metro_scores[index]) / 2
        score = (
            ordered["price"] * price_scores[index]
            + ordered["rating"] * rating_scores[index]
            + ordered["location"] * location
            + ordered["preferences"] * preference_scores[index]
        )
        scored.append((round(score, 4), hotel))

    scored.sort(key=lambda item: (-item[0], item[1].price_per_night_rub, -item[1].rating, item[1].id))
    return [hotel for _, hotel in scored]


def _interest_score(place: Place, request: TripRequest) -> float:
    interests = [_normalize_text(item) for item in request.interests]
    avoided = [_normalize_text(item) for item in request.avoid]
    categories = [_normalize_text(category) for category in place.categories]

    if interests:
        matched = sum(
            1 for interest in interests
            if any(_text_match(interest, category) for category in categories)
        )
        score = matched / len(interests)
    else:
        score = 1.0  # интересы не заданы — критерий никого не штрафует

    avoid_hits = sum(
        1 for item in avoided
        if any(_text_match(item, category) for category in categories)
    )
    return max(0.0, min(1.0, score - 0.25 * avoid_hits))


def rank_places(
    places: Sequence[Place],
    request: TripRequest,
    weights: Mapping[str, float] | None = None,
) -> list[Place]:
    """Сортирует места по совпадению с интересами, рейтингу, цене и толпам."""
    if not places:
        return []
    ordered = _merge_weights(DEFAULT_PLACE_WEIGHTS, weights)

    interest_scores = [_interest_score(place, request) for place in places]
    rating_scores = [place.rating / 10 for place in places]
    price_scores = _normalize_lower([place.entrance_fee_rub for place in places])
    crowd_scores = [{"low": 1.0, "medium": 0.5, "high": 0.0}[place.crowd_level] for place in places]

    scored: list[tuple[float, Place]] = []
    for index, place in enumerate(places):
        score = (
            ordered["interest_match"] * interest_scores[index]
            + ordered["rating"] * rating_scores[index]
            + ordered["price"] * price_scores[index]
            + ordered["crowd"] * crowd_scores[index]
        )
        scored.append((round(score, 4), place))

    scored.sort(key=lambda item: (-item[0], -item[1].rating, item[1].entrance_fee_rub, item[1].id))
    return [place for _, place in scored]
