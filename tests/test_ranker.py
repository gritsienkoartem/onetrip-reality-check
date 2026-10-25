"""Тесты детерминированного ранжирования."""
from datetime import datetime, timedelta

from agent.ranker import (
    rank_flight_pairs,
    rank_hotels,
    rank_places,
    unmatched_hotel_preferences,
)
from models import Flight, Hotel, Place, TripRequest


def make_flight(
    flight_id: str,
    direction: str,
    price: float,
    departure: datetime,
    duration_hours: float,
    stops: int = 0,
) -> Flight:
    return Flight(
        id=flight_id,
        direction=direction,
        origin="Москва" if direction == "outbound" else "Стамбул",
        destination="Стамбул" if direction == "outbound" else "Москва",
        airline="Тест",
        departure_time=departure,
        arrival_time=departure + timedelta(hours=duration_hours),
        price_rub=price,
        stops=stops,
        layover_minutes=[60] * stops,
        connection_type="international" if stops else "none",
        baggage_included=True,
    )


def make_hotel(
    hotel_id: str,
    price: float,
    center: int,
    metro: int,
    tags=None,
    name: str | None = None,
) -> Hotel:
    return Hotel(
        id=hotel_id,
        name=name or f"Отель {hotel_id}",
        district="Тест",
        city="Стамбул",
        price_per_night_rub=price,
        rating=8.0,
        minutes_to_center=center,
        minutes_to_metro=metro,
        tourist_tax_per_night_rub=0,
        latitude=41.0,
        longitude=29.0,
        tags=tags or [],
    )


def make_place(
    place_id: str,
    categories: list[str],
    fee: float = 0,
    crowd: str = "low",
    rating: float = 8.0,
) -> Place:
    return Place(
        id=place_id,
        name=f"Место {place_id}",
        city="Стамбул",
        categories=categories,
        latitude=41.0,
        longitude=29.0,
        indoor=True,
        visit_minutes=60,
        entrance_fee_rub=fee,
        rating=rating,
        crowd_level=crowd,
    )


def test_flight_pairs_are_sorted_and_have_metrics():
    start = datetime(2026, 11, 14, 8, 0)
    outbound = [
        make_flight("O_CHEAP", "outbound", 100, start, 10),
        make_flight("O_FAST", "outbound", 200, start, 2),
    ]
    inbound = [
        make_flight("I_CHEAP", "return", 100, datetime(2026, 11, 17, 8, 0), 10),
        make_flight("I_FAST", "return", 200, datetime(2026, 11, 17, 8, 0), 2),
    ]

    pairs = rank_flight_pairs(outbound, inbound)

    assert len(pairs) == 4
    assert pairs[0].total_price_rub == 200
    assert pairs[0].total_duration_minutes == 20 * 60
    assert pairs[0].total_stops == 0
    assert [pair.score for pair in pairs] == sorted((pair.score for pair in pairs), reverse=True)


def test_return_must_depart_after_arrival():
    outbound = [make_flight("O1", "outbound", 100, datetime(2026, 11, 14, 8, 0), 4)]
    too_early = [make_flight("I_EARLY", "return", 100, datetime(2026, 11, 14, 10, 0), 3)]
    normal = [make_flight("I_OK", "return", 100, datetime(2026, 11, 17, 8, 0), 3)]

    assert rank_flight_pairs(outbound, too_early) == []
    assert len(rank_flight_pairs(outbound, normal)) == 1


def test_flight_weights_can_prioritize_duration():
    start = datetime(2026, 11, 14, 8, 0)
    outbound = [
        make_flight("O_CHEAP", "outbound", 100, start, 10),
        make_flight("O_FAST", "outbound", 200, start, 2),
    ]
    inbound = [
        make_flight("I_CHEAP", "return", 100, datetime(2026, 11, 17, 8, 0), 10),
        make_flight("I_FAST", "return", 200, datetime(2026, 11, 17, 8, 0), 2),
    ]

    pairs = rank_flight_pairs(outbound, inbound, {"price": 0, "duration": 1, "stops": 0})

    assert pairs[0].total_duration_minutes == 4 * 60


def test_hotel_weights_can_prioritize_price_or_location():
    hotels = [
        make_hotel("H_CHEAP", 2000, center=60, metro=20),
        make_hotel("H_CENTRAL", 8000, center=5, metro=3),
    ]
    request = TripRequest()

    cheap_first = rank_hotels(hotels, request, {"price": 1, "rating": 0, "location": 0, "preferences": 0})
    central_first = rank_hotels(hotels, request, {"price": 0, "rating": 0, "location": 1, "preferences": 0})

    assert cheap_first[0].id == "H_CHEAP"
    assert central_first[0].id == "H_CENTRAL"


def test_hotel_preference_affects_ranking():
    hotels = [
        make_hotel("H_FAR", 3000, center=10, metro=25),
        make_hotel("H_NEAR", 5000, center=15, metro=5, tags=["рядом с метро"]),
    ]
    request = TripRequest(accommodation_preferences=["рядом с метро"])

    ranked = rank_hotels(hotels, request, {"price": 0, "rating": 0, "location": 0, "preferences": 1})

    assert ranked[0].id == "H_NEAR"


def test_place_interest_match_and_avoid_penalty():
    places = [
        make_place("P_ARCH", ["архитектура"]),
        make_place("P_FOOD", ["еда"]),
        make_place("P_ARCH_SHOP", ["архитектура", "шопинг"]),
    ]
    request = TripRequest(interests=["архитектура"], avoid=["шопинг"])

    ranked = rank_places(places, request, {"interest_match": 1, "rating": 0, "price": 0, "crowd": 0})

    assert ranked[0].id == "P_ARCH"
    assert ranked[-1].id == "P_FOOD"
    assert [place.id for place in ranked].index("P_ARCH_SHOP") < [place.id for place in ranked].index("P_FOOD")


def test_place_weights_can_prioritize_crowd():
    places = [
        make_place("P_HIGH", ["музей"], crowd="high"),
        make_place("P_LOW", ["музей"], crowd="low"),
    ]
    ranked = rank_places(
        places,
        TripRequest(),
        {"interest_match": 0, "rating": 0, "price": 0, "crowd": 1},
    )
    assert ranked[0].id == "P_LOW"


def test_empty_collections_return_empty_lists():
    assert rank_flight_pairs([], []) == []
    assert rank_hotels([], TripRequest()) == []
    assert rank_places([], TripRequest()) == []


def test_unknown_hotel_distances_are_neutral_not_best():
    unknown = make_hotel("H_UNKNOWN", 3000, center=0, metro=0).model_copy(
        update={"center_known": False, "metro_known": False}
    )
    known = make_hotel("H_KNOWN", 3000, center=10, metro=5)

    ranked = rank_hotels(
        [unknown, known],
        TripRequest(),
        {"price": 0, "rating": 0, "location": 1, "preferences": 0},
    )
    assert ranked[0].id == "H_KNOWN"

    preference = TripRequest(accommodation_preferences=["в центре"])
    ranked_by_preference = rank_hotels(
        [unknown, known],
        preference,
        {"price": 0, "rating": 0, "location": 0, "preferences": 1},
    )
    assert ranked_by_preference[0].id == "H_KNOWN"


def test_hotel_format_preferences_match_expected_lodging():
    hotels = [
        make_hotel("H_HOTEL", 3000, center=10, metro=5),
        make_hotel("H_GUEST", 3000, center=10, metro=5, name="Гостевой дом у реки"),
        make_hotel("H_CAMP", 3000, center=10, metro=5, name="Глэмпинг Лесная поляна"),
        make_hotel("H_CHALET", 3000, center=10, metro=5, name="Шале на склоне"),
    ]

    guest = rank_hotels(
        hotels,
        TripRequest(accommodation_preferences=["гостевой дом"]),
        {"price": 0, "rating": 0, "location": 0, "preferences": 1},
    )
    camping = rank_hotels(
        hotels,
        TripRequest(accommodation_preferences=["кемпинг"]),
        {"price": 0, "rating": 0, "location": 0, "preferences": 1},
    )
    hotel = rank_hotels(
        hotels,
        TripRequest(accommodation_preferences=["отель"]),
        {"price": 0, "rating": 0, "location": 0, "preferences": 1},
    )
    unusual = rank_hotels(
        hotels,
        TripRequest(accommodation_preferences=["необычное жильё"]),
        {"price": 0, "rating": 0, "location": 0, "preferences": 1},
    )

    assert guest[0].id == "H_GUEST"
    assert camping[0].id == "H_CAMP"
    assert hotel[0].id == "H_HOTEL"
    assert {item.id for item in unusual[:2]} == {"H_CAMP", "H_CHALET"}


def test_unmatched_hotel_preferences_reports_missing_formats():
    hotels = [make_hotel("H_HOTEL", 3000, center=10, metro=5)]

    assert unmatched_hotel_preferences(
        hotels, TripRequest(accommodation_preferences=["кемпинг", "тихий район"])
    ) == ["кемпинг", "тихий район"]
    assert unmatched_hotel_preferences(
        hotels, TripRequest(accommodation_preferences=["отель"])
    ) == []
    assert unmatched_hotel_preferences([], TripRequest(accommodation_preferences=["кемпинг"])) == []
    assert unmatched_hotel_preferences(hotels, TripRequest()) == []
