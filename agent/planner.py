"""Сборка плана поездки и запуск Reality Check.

Планировщик получает данные провайдера, выбирает варианты, строит
маршрут и стоимость. После сборки план проверяется детерминированными
правилами; issues и risk_score заполняются в конце.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Collection
from datetime import date, timedelta

from agent.itinerary import MAX_PLACES_PER_DAY, build_days
from agent.ranker import (
    FlightPair,
    rank_flight_pairs,
    rank_hotels,
    rank_places,
    unmatched_hotel_preferences,
)
from checks import run_reality_check
from config import ConfigError, load_assumptions
from models import (
    CostBreakdown,
    Flight,
    Hotel,
    HotelTransfer,
    NO_DATA,
    PriceSource,
    TransportOption,
    TripPlan,
    TripRequest,
)
from providers import DataProvider, ProviderError, get_provider
from providers.estimates import estimate_hotel_transfer

_MONTH_PATTERNS = [
    (r"январ(?:ь|я|е|ю)?", 1),
    (r"феврал(?:ь|я|е|ю)?", 2),
    (r"март(?:а|е|у|ом)?", 3),
    (r"апрел(?:ь|я|е|ю)?", 4),
    (r"июн(?:ь|я|е|ю)?", 6),
    (r"июл(?:ь|я|е|ю)?", 7),
    (r"август(?:а|е|у|ом)?", 8),
    (r"сентябр(?:ь|я|е|ю)?", 9),
    (r"октябр(?:ь|я|е|ю)?", 10),
    (r"ноябр(?:ь|я|е|ю)?", 11),
    (r"декабр(?:ь|я|е|ю)?", 12),
    (r"ма(?:й|я|е|ю|ем)?", 5),  # «ма» проверяем последним, чтобы не спутать с «март»
]


def resolve_period_start(period: str | None, today: date) -> date | None:
    """Приблизительная дата начала по описанию вроде «середина ноября».

    Это допущение проекта: начало месяца — 3-е число, середина — 15-е,
    конец — 24-е. Если месяц уже прошёл, берётся следующий год.
    """
    text = (period or "").strip().casefold().replace("ё", "е")
    if not text:
        return None

    month = None
    for pattern, number in _MONTH_PATTERNS:
        if re.search(rf"(?<![а-яё]){pattern}(?![а-яё])", text):
            month = number
            break
    if month is None:
        return None

    if "начал" in text:
        day = 3
    elif "середин" in text:
        day = 15
    elif "конц" in text:
        day = 24
    else:
        day = 1

    candidate = date(today.year, month, day)
    if candidate < today:
        candidate = date(today.year + 1, month, day)
    return candidate


def _safe_call(label: str, function, notes: list[str], default: list):
    """Вызывает провайдер и не роняет приложение при его ошибке."""
    try:
        return function()
    except ProviderError as error:
        notes.append(f"Не удалось получить {label}: {error}.")
        return default


def _report_progress(progress: Callable[[str], None] | None, label: str) -> None:
    """Сообщает интерфейсу о реальном этапе поиска, без фиктивных процентов."""
    if progress is not None:
        progress(label)


def _filter_pairs_by_dates(
    pairs: list[FlightPair],
    start_date: date | None,
    end_date: date | None,
) -> list[FlightPair]:
    if not pairs or (start_date is None and end_date is None):
        return pairs
    matching = [
        pair for pair in pairs
        if (start_date is None or pair.outbound.departure_time.date() == start_date)
        and (end_date is None or pair.inbound.departure_time.date() == end_date)
    ]
    return matching


def _pair_price_kind(pair: FlightPair) -> str:
    kinds = {pair.outbound.price_kind, pair.inbound.price_kind}
    if kinds == {"real"}:
        return "real"
    if kinds == {"mock"}:
        return "mock"
    return "estimate"


def _pair_to_option(pair: FlightPair) -> TransportOption:
    return TransportOption(
        mode=pair.outbound.mode,
        outbound=pair.outbound,
        inbound=pair.inbound,
        total_price_rub=pair.total_price_rub,
        total_duration_minutes=pair.total_duration_minutes,
        price_kind=_pair_price_kind(pair),
    )


def _best_transport_options(pairs: list[FlightPair]) -> list[TransportOption]:
    """Лучшая пара для каждого вида транспорта, дешёвые варианты первыми."""
    best: dict[str, FlightPair] = {}
    for pair in pairs:
        best.setdefault(pair.outbound.mode, pair)
    options = [_pair_to_option(pair) for pair in best.values()]
    options.sort(key=lambda option: (option.total_price_rub, option.total_duration_minutes))
    return options


def _transport_alternatives(
    pairs: list[FlightPair],
    limit: int = 6,
) -> list[TransportOption]:
    """Несколько лучших пар для сравнения, включая более дорогие варианты."""
    return [_pair_to_option(pair) for pair in pairs[:limit]]


def _preferred_mode(request: TripRequest) -> str | None:
    """Вид транспорта, который пользователь выбрал в мастере."""
    text = " ".join(request.transport_preferences).casefold()
    if "самол" in text:
        return "flight"
    if "поезд" in text or "жд" in text:
        return "train"
    if "автобус" in text:
        return "bus"
    return None


STYLE_PLACES_PER_DAY = {"calm": 2, "balanced": None, "intense": 5}
STYLE_LABELS = {
    "calm": "спокойный",
    "balanced": "сбалансированный",
    "intense": "насыщенный",
}
TRANSPORT_MODE_LABELS = {"flight": "самолёт", "train": "поезд", "bus": "автобус"}
NODE_WORDS = {"flight": "аэропорт", "train": "вокзал", "bus": "автостанция"}

_SERVICE_PREFIXES = (
    "Aviasales",
    "Tutu.ru",
    "Travelpayouts",
    "Level.Travel",
    "Travelata",
    "OpenStreetMap",
)


def _service_name(source: str) -> str:
    """Короткое имя сервиса для сравнения цен без длинных пояснений."""
    text = (source or "").strip()
    for name in _SERVICE_PREFIXES:
        if text.startswith(name):
            return name
    if text.startswith("Оценка проекта"):
        return "Оценка проекта"
    if text.startswith("Мок"):
        return "Мок-данные"
    return text.split(" через")[0].split(" (")[0].strip() or "Источник"


def _price_sources(flights: list[Flight]) -> list[PriceSource]:
    """Минимальная цена каждого сервиса в каждом направлении.

    В сводку попадают все ответившие источники, включая оценки проекта:
    так пользователь видит и реальные предложения, и то, что оценено.
    """
    groups: dict[tuple[str, str, str], dict] = {}
    for flight in flights:
        service = _service_name(flight.source)
        key = (flight.direction, flight.mode, service)
        group = groups.get(key)
        if group is None:
            groups[key] = {
                "cheapest": flight,
                "count": 1,
                "kinds": {flight.price_kind},
            }
            continue
        group["count"] += 1
        group["kinds"].add(flight.price_kind)
        if flight.price_rub < group["cheapest"].price_rub:
            group["cheapest"] = flight

    sources: list[PriceSource] = []
    for (direction, mode, service), group in groups.items():
        cheapest: Flight = group["cheapest"]
        kinds = group["kinds"]
        price_kind = (
            "real" if "real" in kinds else ("mock" if "mock" in kinds else "estimate")
        )
        sources.append(
            PriceSource(
                service=service,
                mode=mode,
                direction=direction,
                cheapest_rub=cheapest.price_rub,
                offers_count=group["count"],
                price_kind=price_kind,
                booking_url=cheapest.booking_url,
            )
        )
    direction_order = {"outbound": 0, "return": 1}
    sources.sort(key=lambda item: (direction_order[item.direction], item.cheapest_rub, item.service))
    return sources


def _hotel_transfers(
    pair: FlightPair | None,
    hotel: Hotel | None,
    assumptions: dict,
) -> list[HotelTransfer]:
    """Дорога между узлом прилёта/вылета и выбранным отелем.

    Считается только если у рейса есть координаты узла из OpenStreetMap.
    Иначе честно ничего не показываем, а не придумываем адрес.
    """
    if pair is None or hotel is None:
        return []

    transit_nearby = bool(
        (hotel.metro_known and hotel.has_metro)
        or hotel.distance_to_station_km is not None
    )
    transfers: list[HotelTransfer] = []
    outbound = pair.outbound
    if (
        outbound.arrival_node_latitude is not None
        and outbound.arrival_node_longitude is not None
    ):
        word = NODE_WORDS.get(outbound.mode, "узел")
        transfer = estimate_hotel_transfer(
            hotel,
            node_latitude=outbound.arrival_node_latitude,
            node_longitude=outbound.arrival_node_longitude,
            from_name=outbound.arrival_node or word.capitalize(),
            to_name=hotel.name,
            title=f"{word.capitalize()} → отель",
            assumptions=assumptions,
            transit_nearby=transit_nearby,
        )
        if transfer is not None:
            transfers.append(transfer)

    inbound = pair.inbound
    if (
        inbound.departure_node_latitude is not None
        and inbound.departure_node_longitude is not None
    ):
        word = NODE_WORDS.get(inbound.mode, "узел")
        transfer = estimate_hotel_transfer(
            hotel,
            node_latitude=inbound.departure_node_latitude,
            node_longitude=inbound.departure_node_longitude,
            from_name=hotel.name,
            to_name=inbound.departure_node or word,
            title=f"Отель → {word}",
            assumptions=assumptions,
            transit_nearby=transit_nearby,
        )
        if transfer is not None:
            transfers.append(transfer)
    return transfers


def _build_cost(
    pair: FlightPair | None,
    hotel,
    days,
    duration_days: int,
    assumptions: dict,
    hotel_transfers: list[HotelTransfer] | None = None,
) -> CostBreakdown:
    estimates = assumptions.get("cost_estimates", {})
    transfer_one_way = float(estimates.get("airport_transfer_one_way_rub", 0))
    local_per_day = float(estimates.get("local_transport_per_day_rub", 0))
    source = estimates.get("source", "Оценка проекта")

    flights = [pair.outbound, pair.inbound] if pair else []
    nights = max(0, duration_days - 1)
    baggage = sum(
        0 if flight.baggage_included else flight.baggage_fee_rub
        for flight in flights
    )
    access_prices = [
        leg.price_rub
        for flight in flights
        for leg in (flight.access_to_departure, flight.access_from_arrival)
        if leg is not None
    ]
    hotel_legs = [
        option.price_rub
        for transfer in hotel_transfers or []
        if (option := (transfer.by_mode("taxi") or transfer.cheapest())) is not None
    ]
    if hotel_transfers and pair is not None:
        # Когда отель известен, дорога в городе назначения считается от
        # отеля, а не от центра. Ноги «центр → узел» в этом городе больше
        # не нужны, иначе смета посчитает одну дорогу дважды.
        origin_prices = [
            leg.price_rub
            for leg in (
                pair.outbound.access_to_departure,
                pair.inbound.access_from_arrival,
            )
            if leg is not None
        ]
        transfers = sum(origin_prices) + sum(hotel_legs)
    elif access_prices:
        transfers = sum(access_prices)
    else:
        transfers = transfer_one_way * len(flights)
    if hotel is not None and hotel.stay_total_rub is not None:
        accommodation = hotel.stay_total_rub
    else:
        accommodation = hotel.price_per_night_rub * nights if hotel else 0
    tourist_tax = hotel.tourist_tax_per_night_rub * nights if hotel else 0
    attractions = sum(item.cost_rub for day in days for item in day.items)

    notes = [f"Стоимость — оценка; допущения: {source}."]
    if pair is None:
        notes.append("Рейсы не выбраны, поэтому стоимость перелёта и багажа равна нулю.")
    if hotel is None:
        notes.append("Отель не выбран, поэтому проживание и туристический налог равны нулю.")
    elif hotel.stay_total_rub is not None:
        notes.append(
            "Проживание взято как цена предложения за всё проживание на выбранные даты, "
            "а не как цена за ночь, умноженная на число ночей."
        )
    if any(not flight.baggage_known for flight in flights):
        notes.append(
            "Условия багажа источник не сообщил: багаж не добавлен в стоимость, "
            "его нужно проверить при покупке."
        )
    if hotel_transfers:
        notes.append(
            "Трансферы включают дорогу до узла в городе вылета и поездку "
            "отель ↔ аэропорт/вокзал на такси по оценке проекта; это не тариф "
            "такси и не подтверждённое время в пути."
        )
        public_total = sum(
            option.price_rub
            for transfer in hotel_transfers
            if (option := transfer.by_mode("public_transport")) is not None
        )
        taxi_total = sum(hotel_legs)
        if public_total and public_total < taxi_total:
            saving = f"{taxi_total - public_total:,.0f} ₽".replace(",", " ")
            notes.append(
                f"Общественный транспорт по оценке дешевле такси на {saving} "
                "за две поездки; в смете оставлен такси как более надёжный вариант."
            )
    elif access_prices:
        notes.append(
            "Трансферы — оценка дороги от места до транспортного узла и от узла "
            "до места по координатам OpenStreetMap, а не тариф такси."
        )

    return CostBreakdown(
        flights=round(sum(flight.price_rub for flight in flights), 2),
        accommodation=round(accommodation, 2),
        transfers=round(transfers, 2),
        baggage=round(baggage, 2),
        tourist_tax=round(tourist_tax, 2),
        local_transport=round(local_per_day * max(1, duration_days), 2),
        attractions=round(attractions, 2),
        is_estimate=True,
        notes=notes,
    )


def build_trip_plan(
    request: TripRequest,
    *,
    provider: DataProvider | None = None,
    today: date | None = None,
    assumptions: dict | None = None,
    provider_notes: list[str] | None = None,
    flight_pair: FlightPair | None = None,
    transport_options: list[TransportOption] | None = None,
    transport_alternatives: list[TransportOption] | None = None,
    hotel_choice: Hotel | None = None,
    hotel_options: list[Hotel] | None = None,
    exclude_place_ids: Collection[str] | None = None,
    max_places_per_day: int | None = None,
    prefer_indoor_dates: Collection[date] | None = None,
    max_walking_km_per_day: float | None = None,
    progress: Callable[[str], None] | None = None,
) -> TripPlan:
    """Собирает план и сразу проверяет его Reality Check.

    Необязательные параметры используются циклом исправлений: можно
    зафиксировать другую пару рейсов или отель, исключить места и
    изменить ограничения маршрута.
    """
    today = today or date.today()
    notes: list[str] = list(provider_notes or [])
    options: list[TransportOption] = list(transport_options or [])
    alternatives: list[TransportOption] = list(transport_alternatives or [])

    if provider is None:
        provider, selection_notes = get_provider()
        notes.extend(selection_notes)
    reset_notes = getattr(provider, "reset_notes", None)
    if callable(reset_notes):
        reset_notes()
    if assumptions is None:
        try:
            assumptions = load_assumptions()
        except ConfigError as error:
            notes.append(f"Не удалось прочитать допущения проекта: {error}")
            assumptions = {}

    weights = assumptions.get("ranking_weights", {})
    origin = request.origin_city
    destination = request.destination_city

    start_date = request.start_date
    end_date = request.end_date
    duration_days = request.duration_days
    has_explicit_dates = start_date is not None or end_date is not None
    period_start = resolve_period_start(request.period_description or request.raw_text, today)

    if start_date and not end_date and duration_days:
        end_date = start_date + timedelta(days=duration_days - 1)
    if end_date and not start_date and duration_days:
        start_date = end_date - timedelta(days=duration_days - 1)

    if start_date and end_date:
        span = (end_date - start_date).days + 1
        if duration_days is not None and duration_days != span:
            notes.append("Длительность в запросе не совпала с датами; для плана взяты даты.")
        duration_days = span

    flights_out: list[Flight] = []
    flights_back: list[Flight] = []
    if flight_pair is not None:
        best_pair: FlightPair | None = flight_pair
    else:
        if origin and destination:
            _report_progress(progress, "Ищу билеты и расписание")
            search_start = start_date or period_start
            search_end = end_date or (
                search_start + timedelta(days=duration_days - 1) if search_start and duration_days else None
            )
            flights_out = _safe_call(
                "рейсы туда",
                lambda: provider.search_flights(origin, destination, search_start, search_end),
                notes,
                [],
            )
            flights_back = _safe_call(
                "обратные рейсы",
                lambda: provider.search_flights(destination, origin, search_end, search_start),
                notes,
                [],
            )
        else:
            notes.append("Не указаны город вылета и/или назначения — рейсы не подбирались.")

        pairs = rank_flight_pairs(flights_out, flights_back, weights.get("flights"))
        if has_explicit_dates and pairs:
            matching = _filter_pairs_by_dates(pairs, start_date, end_date)
            if matching:
                pairs = matching
            elif provider.name == "fixtures":
                notes.append(
                    "В тестовом наборе нет пары рейсов на указанные даты; "
                    "показаны вымышленные рейсы, даты плана взяты из запроса."
                )
            else:
                notes.append(
                    "На указанные даты нет подходящей пары билетов; "
                    "показан ближайший доступный вариант."
                )
        options = _best_transport_options(pairs)
        alternatives = _transport_alternatives(pairs)
        preferred_mode = _preferred_mode(request)
        if preferred_mode is not None:
            preferred_pairs = [
                pair for pair in pairs if pair.outbound.mode == preferred_mode
            ]
            if preferred_pairs:
                best_pair = preferred_pairs[0]
                notes.append(
                    "По пожеланию пользователя выбран "
                    f"{TRANSPORT_MODE_LABELS[preferred_mode]}; остальные найденные "
                    "варианты показаны для сравнения."
                )
            else:
                best_pair = pairs[0] if pairs else None
                notes.append(
                    "Пожелание «"
                    f"{TRANSPORT_MODE_LABELS[preferred_mode]}» учесть не удалось: "
                    "такого варианта на выбранные даты нет. Показан доступный транспорт."
                )
        else:
            best_pair = pairs[0] if pairs else None
        if alternatives:
            notes.append(
                f"Для сравнения сохранено вариантов транспорта: {len(alternatives)}; "
                "более дорогие не скрываются."
            )
        if best_pair is None and origin and destination:
            notes.append("Рейсы не найдены — план без перелёта.")

    if len(options) > 1:
        labels = {"flight": "самолёт", "train": "поезд", "bus": "автобус"}
        notes.append(
            "Сравнены виды транспорта: "
            + ", ".join(labels.get(option.mode, option.mode) for option in options)
            + "."
        )
    if any(
        flight.departure_node or flight.arrival_node
        for option in options
        for flight in (option.outbound, option.inbound)
    ):
        notes.append(
            "Наличие аэропортов, вокзалов и автостанций проверено по OpenStreetMap; "
            "дорога от узла до места — оценка проекта."
        )

    if not has_explicit_dates and best_pair is not None:
        start_date = best_pair.outbound.departure_time.date()
        end_date = best_pair.inbound.departure_time.date()
        notes.append(
            "Точные даты не были указаны: для плана взяты даты выбранных рейсов "
            f"({start_date.isoformat()} — {end_date.isoformat()})."
        )
    elif not has_explicit_dates and period_start is not None:
        start_date = period_start
        end_date = start_date + timedelta(days=(duration_days or 1) - 1)
        notes.append(
            f"Точные даты не были указаны: период уточнён как {start_date.isoformat()} "
            "(допущение проекта)."
        )
    elif start_date and not end_date:
        end_date = start_date + timedelta(days=(duration_days or 1) - 1)
    elif end_date and not start_date:
        start_date = end_date - timedelta(days=(duration_days or 1) - 1)

    if start_date and end_date:
        duration_days = (end_date - start_date).days + 1
    if duration_days is None:
        duration_days = 1
        notes.append("Длительность не указана; для плана принят один день.")
    duration_days = max(1, min(30, duration_days))

    max_hotel_options = max(
        1,
        min(10, int(assumptions.get("hotel_search", {}).get("max_options", 5))),
    )
    if hotel_choice is not None:
        hotel: Hotel | None = hotel_choice
        hotel_options_list = list(hotel_options or [])
        if all(option.id != hotel.id for option in hotel_options_list):
            hotel_options_list.insert(0, hotel)
    else:
        hotels = []
        if destination:
            _report_progress(progress, "Подбираю варианты проживания")
            hotels = _safe_call(
                "отели",
                lambda: provider.search_hotels(
                    destination,
                    start_date,
                    end_date,
                    origin_city=origin,
                    adults=request.adults,
                ),
                notes,
                [],
            )
        ranked_hotels = rank_hotels(hotels, request, weights.get("hotels"))
        hotel_options_list = ranked_hotels[:max_hotel_options]
        hotel = hotel_options_list[0] if hotel_options_list else None
        if hotel is None:
            notes.append("Отель не найден — план неполный.")
        elif provider.name == "live" and len(hotel_options_list) < 3:
            source_hint = (
                " OpenStreetMap"
                if hotel.source.startswith("OpenStreetMap")
                else ""
            )
            notes.append(
                f"В{source_hint} найдено вариантов проживания: {len(ranked_hotels)} — "
                "меньше трёх; показаны все найденные."
            )

    if len(hotel_options_list) > 1:
        notes.append(
            f"Показаны варианты проживания: {len(hotel_options_list)}; "
            "выбран лучший по весам проекта, остальные доступны в плане."
        )
    if any(
        option.source.startswith("OpenStreetMap")
        and {"в центре", "у воды", "у вокзала", "тихо"} & set(option.tags)
        for option in hotel_options_list
    ):
        notes.append(
            "Темы отелей («в центре», «у воды», «у вокзала», «тихо») рассчитаны "
            "по расстояниям OpenStreetMap и типу жилья — это допущение проекта."
        )

    unmatched_preferences = unmatched_hotel_preferences(hotel_options_list, request)
    if unmatched_preferences:
        notes.append(
            "Источники не подтвердили пожелания к жилью: "
            + ", ".join(unmatched_preferences)
            + ". Показаны остальные найденные варианты; пожелания не скрывают их."
        )

    hotel_transfers = _hotel_transfers(best_pair, hotel, assumptions)
    if hotel_transfers:
        notes.append(
            "Дорога отель ↔ аэропорт/вокзал — оценка проекта: пешком, "
            "общественный транспорт или такси; маршрут и расписание не проверены."
        )
        public_total = sum(
            option.price_rub
            for transfer in hotel_transfers
            if (option := transfer.by_mode("public_transport")) is not None
        )
        taxi_total = sum(
            option.price_rub
            for transfer in hotel_transfers
            if (option := transfer.by_mode("taxi")) is not None
        )
        if public_total and public_total < taxi_total:
            saving = f"{taxi_total - public_total:,.0f} ₽".replace(",", " ")
            notes.append(
                f"Общественный транспорт по оценке дешевле такси на {saving} "
                "за две поездки."
            )
    elif provider.name == "live" and hotel is not None and best_pair is not None:
        notes.append(
            "Дорога отель ↔ узел не рассчитана: у рейса нет координат "
            "аэропорта или вокзала из OpenStreetMap."
        )

    places = []
    if destination:
        _report_progress(progress, "Собираю места, погоду и маршрут")
        places = _safe_call(
            "места",
            lambda: provider.get_places(destination),
            notes,
            [],
        )
    ranked_places = rank_places(places, request, weights.get("places"))
    if not ranked_places:
        notes.append("Места не найдены — маршрут по дням пустой.")
    if exclude_place_ids:
        excluded = set(exclude_place_ids)
        ranked_places = [place for place in ranked_places if place.id not in excluded]
        if excluded and not ranked_places:
            notes.append("Все места были исключены при исправлении плана.")

    weather = []
    if destination and start_date and end_date:
        weather = _safe_call(
            "погоду",
            lambda: provider.get_weather(destination, start_date, end_date),
            notes,
            [],
        )
    if not weather:
        notes.append("Нет данных о погоде на выбранные даты.")
    elif any(day.source.startswith("Open-Meteo (архив") for day in weather):
        notes.append(
            "Даты дальше 16 дней: показана погода из архива за прошлый год "
            "(типичная, а не прогноз на эти дни)."
        )

    for note in getattr(provider, "last_notes", []) or []:
        if note not in notes:
            notes.append(note)

    has_provider_data = bool(best_pair or hotel or ranked_places or weather)
    destination_unsupported = bool(destination) and not has_provider_data

    used_flights = [item for item in ((best_pair.outbound, best_pair.inbound) if best_pair else ()) if item]
    if any(
        flight.price_kind == "real" and flight.source.startswith("Aviasales")
        for flight in used_flights
    ):
        notes.append(
            "Цены авиабилетов — кэш Aviasales до 48 часов, а не поиск в реальном "
            "времени; наличие мест не подтверждено."
        )
    if any(
        flight.mode == "train" and flight.source.startswith("Tutu.ru")
        for flight in used_flights
    ):
        notes.append(
            "Расписание поездов — кэш Tutu.ru; цена ориентировочная, наличие мест "
            "нужно проверить при покупке."
        )
    estimated_flights = [flight for flight in used_flights if flight.price_kind == "estimate"]
    if estimated_flights:
        notes.append(
            "Цена транспорта — оценка проекта по расстоянию, а не реальный тариф "
            "и не подтверждённое наличие мест."
        )
        modes = {"flight": "самолёт", "train": "поезд", "bus": "автобус"}
        chosen = ", ".join(sorted({modes.get(flight.mode, flight.mode) for flight in estimated_flights}))
        notes.append(f"Расписание и время в пути для «{chosen}» — оценка проекта.")
    if hotel is not None and hotel.source.startswith(("Level.Travel", "Travelata")):
        notes.append(
            "Отели — предложения Level.Travel/Travelata за всех туристов и всё "
            "проживание; цена и наличие мест не подтверждены. Предложение живёт "
            "около 20 минут, перед бронированием нужен повторный поиск."
        )
    hotel_search_url = getattr(provider, "last_hotel_search_url", "") if hotel is not None else ""
    if hotel_search_url:
        notes.append(
            "Ссылка Trip.com ведёт на общую выдачу отелей на те же даты; "
            "цены и доступность там могут отличаться."
        )
    if hotel is not None and hotel.source.startswith("OpenStreetMap"):
        notes.append(
            "Отели найдены в OpenStreetMap; цена за ночь и время до центра — "
            "оценка проекта, рейтинг в открытом источнике отсутствует."
        )
    elif hotel is not None and not hotel.is_mock and hotel.rating_source == NO_DATA:
        notes.append(
            "Проживание — средняя оценка стоимости, а не цена конкретного отеля; "
            "рейтинг отеля в источнике отсутствует."
        )
    if any(place.rating_source == "Нет данных" for place in ranked_places):
        notes.append(
            "У части мест нет рейтинга и цены входа в открытом источнике: показаны "
            "нейтральные значения, вход не учтён в стоимости."
        )

    if destination_unsupported:
        days = []
        cost = None
        notes.append(
            f"Для города «{destination}» у провайдера «{provider.name}» нет данных: "
            "не найдено рейсов, отелей, мест и прогноза погоды. План и стоимость "
            "не строятся, чтобы не показывать вымышленные значения."
        )
    else:
        style_limit = STYLE_PLACES_PER_DAY.get(request.trip_style)
        effective_places = (
            max_places_per_day
            if max_places_per_day is not None
            else (style_limit if style_limit is not None else MAX_PLACES_PER_DAY)
        )
        if style_limit is not None:
            notes.append(
                f"Стиль поездки «{STYLE_LABELS[request.trip_style]}» ограничивает день "
                f"{style_limit} местами; это настройка маршрута, а не цена."
            )
        days = build_days(
            ranked_places,
            start_date,
            duration_days,
            hotel=hotel,
            flight_out=best_pair.outbound if best_pair else None,
            flight_back=best_pair.inbound if best_pair else None,
            weather=weather,
            max_places_per_day=effective_places,
            prefer_indoor_dates=prefer_indoor_dates or (),
            max_walking_km_per_day=max_walking_km_per_day,
        )
        if ranked_places and not any(day.items for day in days):
            notes.append("Не удалось разместить места в маршруте: не хватило времени в днях.")

        used_place_ids = {item.place_id for day in days for item in day.items}
        used_mock_place = any(place.is_mock for place in ranked_places if place.id in used_place_ids)
        if (
            (best_pair and (best_pair.outbound.is_mock or best_pair.inbound.is_mock))
            or (hotel is not None and hotel.is_mock)
            or used_mock_place
            or any(weather_day.is_mock for weather_day in weather)
        ):
            notes.append("В плане использованы тестовые данные (вымышленные значения).")

        cost = _build_cost(
            best_pair,
            hotel,
            days,
            duration_days,
            assumptions,
            hotel_transfers=hotel_transfers,
        )
    resolved_request = request.model_copy(
        update={"start_date": start_date, "end_date": end_date, "duration_days": duration_days}
    )

    offer_flights: list[Flight] = list(flights_out) + list(flights_back)
    if not offer_flights:
        for option in list(alternatives) + list(options):
            offer_flights.extend((option.outbound, option.inbound))
    price_sources = _price_sources(offer_flights)
    real_services = sorted(
        {source.service for source in price_sources if source.price_kind == "real"}
    )
    if len(real_services) > 1:
        notes.append(
            "Сравнены минимальные цены сервисов: "
            + ", ".join(real_services)
            + "; цена и наличие мест не подтверждены."
        )

    plan = TripPlan(
        request=resolved_request,
        flight_out=best_pair.outbound if best_pair else None,
        flight_back=best_pair.inbound if best_pair else None,
        transport_options=options,
        transport_alternatives=alternatives,
        hotel=hotel,
        hotel_options=hotel_options_list,
        hotel_search_url=hotel_search_url,
        price_sources=price_sources,
        hotel_transfers=hotel_transfers,
        days=days,
        cost=cost,
        issues=[],
        risk_score=0,
        risk_level=None,
        data_notes=list(notes),
    )
    _report_progress(progress, "Проверяю план по правилам Reality Check")
    check = run_reality_check(
        plan,
        places=ranked_places,
        assumptions=assumptions,
        data_available=not destination_unsupported,
        provider_name=provider.name,
    )
    if destination_unsupported:
        summary = "Reality Check: план не собран — для города нет данных у провайдера."
    else:
        summary = (
            f"Reality Check: найдено проблем — {len(check.issues)}, "
            f"уровень риска — {check.risk_level.label_ru.lower()}."
        )
    return plan.model_copy(
        update={
            "issues": check.issues,
            "risk_score": check.risk_score,
            "risk_level": check.risk_level,
            "data_notes": notes + check.notes + [summary],
        }
    )
