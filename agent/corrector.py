"""Цикл автоматических исправлений: собрать → проверить → исправить → проверить.

Исправления детерминированные и ограничены числом попыток из
assumptions.json. Если проблему нельзя устранить, план честно сообщает
об этом, а не объявляет поездку безопасной.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date

from agent.itinerary import MAX_PLACES_PER_DAY
from agent.planner import STYLE_PLACES_PER_DAY, build_trip_plan
from agent.ranker import FlightPair, rank_flight_pairs, rank_hotels, rank_places
from checks.itinerary import iter_opening_issues, weather_conflict_dates
from config import ConfigError, load_assumptions
from models import Hotel, Issue, Place, TransportOption, TripPlan, TripRequest
from providers import DataProvider, ProviderError, get_provider

FLIGHT_ISSUES = {"LATE_ARRIVAL", "SHORT_LAYOVER"}
HOTEL_ISSUES = {"HOTEL_FAR_FROM_CENTER", "HOTEL_FAR_FROM_METRO"}
OPENING_ISSUES = {"PLACE_CLOSED", "OPENING_HOURS_CONFLICT"}


@dataclass
class CorrectionResult:
    """Итог цикла исправлений: исходный и финальный план."""

    plan: TripPlan
    initial_plan: TripPlan
    attempts: int
    applied: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def unresolved(self) -> list[Issue]:
        """Проблемы, которые остались после всех попыток."""
        return list(self.plan.issues)

    @property
    def resolved_codes(self) -> list[str]:
        """Коды проблем, которых не стало в финальном плане."""
        final_codes = {issue.code for issue in self.plan.issues}
        return sorted(
            {issue.code for issue in self.initial_plan.issues if issue.code not in final_codes}
        )

    @property
    def improved(self) -> bool:
        return self.plan.risk_score < self.initial_plan.risk_score


@dataclass
class _State:
    flight_pair: FlightPair | None = None
    transport_options: list[TransportOption] = field(default_factory=list)
    transport_alternatives: list[TransportOption] = field(default_factory=list)
    hotel: Hotel | None = None
    hotel_options: list[Hotel] = field(default_factory=list)
    # Отель выбран пользователем: автокоррекция не должна его подменять.
    lock_hotel: bool = False
    # Транспорт выбран пользователем: автокоррекция не должна его подменять.
    lock_flight: bool = False
    excluded: set[str] = field(default_factory=set)
    max_places_per_day: int | None = None
    prefer_indoor_dates: set[date] = field(default_factory=set)
    max_walking_km_per_day: float | None = None


@dataclass
class _Candidate:
    label: str
    plan: TripPlan
    state: _State


def _state_key(state: _State) -> tuple:
    pair_ids = (
        (state.flight_pair.outbound.id, state.flight_pair.inbound.id)
        if state.flight_pair is not None
        else None
    )
    return (
        pair_ids,
        state.hotel.id if state.hotel is not None else None,
        tuple(sorted(state.excluded)),
        state.max_places_per_day,
        tuple(sorted(state.prefer_indoor_dates)),
        state.max_walking_km_per_day,
    )


def pair_from_plan(plan: TripPlan) -> FlightPair | None:
    outbound, inbound = plan.flight_out, plan.flight_back
    if outbound is None or inbound is None:
        return None
    duration = int(
        (
            (outbound.arrival_time - outbound.departure_time)
            + (inbound.arrival_time - inbound.departure_time)
        ).total_seconds()
        // 60
    )
    return FlightPair(
        outbound=outbound,
        inbound=inbound,
        score=0.0,
        total_price_rub=outbound.price_rub + inbound.price_rub,
        total_duration_minutes=duration,
        total_stops=outbound.stops + inbound.stops,
    )


def pair_from_option(option: TransportOption) -> FlightPair:
    """Пара рейсов из выбранного пользователем варианта транспорта."""
    return FlightPair(
        outbound=option.outbound,
        inbound=option.inbound,
        score=0.0,
        total_price_rub=option.total_price_rub,
        total_duration_minutes=option.total_duration_minutes,
        total_stops=option.outbound.stops + option.inbound.stops,
    )


def _item_count(plan: TripPlan) -> int:
    return sum(len(day.items) for day in plan.days)


def _score(plan: TripPlan) -> tuple[int, int, float]:
    """Меньше — лучше: риск, затем число проблем, затем стоимость."""
    return (
        plan.risk_score,
        len(plan.issues),
        plan.cost.total if plan.cost is not None else 0.0,
    )


def _build(
    request: TripRequest,
    *,
    provider: DataProvider,
    today: date,
    assumptions: dict,
    provider_notes: Sequence[str],
    state: _State,
) -> TripPlan:
    return build_trip_plan(
        request,
        provider=provider,
        today=today,
        assumptions=assumptions,
        provider_notes=list(provider_notes),
        flight_pair=state.flight_pair,
        transport_options=state.transport_options,
        transport_alternatives=state.transport_alternatives,
        hotel_choice=state.hotel,
        hotel_options=state.hotel_options,
        exclude_place_ids=state.excluded or None,
        max_places_per_day=state.max_places_per_day,
        prefer_indoor_dates=state.prefer_indoor_dates or None,
        max_walking_km_per_day=state.max_walking_km_per_day,
    )


def _load_candidates(
    request: TripRequest,
    *,
    provider: DataProvider,
    assumptions: dict,
    plan: TripPlan,
    notes: list[str],
) -> tuple[list[FlightPair], list[Hotel], list[Place]]:
    """Собирает альтернативы у того же провайдера для исправлений."""
    weights = assumptions.get("ranking_weights", {})
    pairs: list[FlightPair] = []
    hotels: list[Hotel] = []
    places: list[Place] = []

    if request.origin_city and request.destination_city:
        try:
            outbound = provider.search_flights(
                request.origin_city,
                request.destination_city,
                plan.request.start_date,
                plan.request.end_date,
            )
            inbound = provider.search_flights(
                request.destination_city,
                request.origin_city,
                plan.request.end_date,
                plan.request.start_date,
            )
            pairs = rank_flight_pairs(outbound, inbound, weights.get("flights"))
            if request.start_date and request.end_date:
                matching = [
                    pair
                    for pair in pairs
                    if pair.outbound.departure_time.date() == request.start_date
                    and pair.inbound.departure_time.date() == request.end_date
                ]
                if matching:
                    pairs = matching
        except ProviderError as error:
            notes.append(f"Не удалось получить альтернативные рейсы: {error}.")

    if request.destination_city:
        try:
            hotels = rank_hotels(
                provider.search_hotels(
                    request.destination_city,
                    plan.request.start_date,
                    plan.request.end_date,
                    origin_city=request.origin_city,
                    adults=request.adults,
                ),
                request,
                weights.get("hotels"),
            )
        except ProviderError as error:
            notes.append(f"Не удалось получить альтернативные отели: {error}.")
        try:
            places = rank_places(
                provider.get_places(request.destination_city),
                request,
                weights.get("places"),
            )
        except ProviderError as error:
            notes.append(f"Не удалось получить дополнительные места: {error}.")

    return pairs, hotels, places


def _opening_problem_ids(
    plan: TripPlan,
    places_by_id: dict[str, Place],
) -> set[str]:
    return {place_id for _, place_id in iter_opening_issues(plan, places_by_id)}


def _acceptable(current: TripPlan, candidate: TripPlan) -> bool:
    """Не даём «исправить» план его опустошением или потерей основы."""
    if _item_count(current) > 0 and _item_count(candidate) == 0:
        return False
    if current.hotel is not None and candidate.hotel is None:
        return False
    if current.flight_out is not None and candidate.flight_out is None:
        return False
    return True


def _make_candidates(
    current: TripPlan,
    *,
    state: _State,
    pairs: Sequence[FlightPair],
    hotels: Sequence[Hotel],
    places_by_id: dict[str, Place],
    request: TripRequest,
    provider: DataProvider,
    today: date,
    assumptions: dict,
    provider_notes: Sequence[str],
) -> list[_Candidate]:
    codes = {issue.code for issue in current.issues}
    budget_issue = "BUDGET_EXCEEDED" in codes
    incomplete = "COST_INCOMPLETE" in codes
    current_key = _state_key(state)
    seen: set[tuple] = {current_key}
    candidates: list[_Candidate] = []

    def add(label: str, new_state: _State) -> None:
        key = _state_key(new_state)
        if key in seen:
            return
        seen.add(key)
        candidate = _build(
            request,
            provider=provider,
            today=today,
            assumptions=assumptions,
            provider_notes=provider_notes,
            state=new_state,
        )
        if _acceptable(current, candidate):
            candidates.append(_Candidate(label=label, plan=candidate, state=new_state))

    if (codes & FLIGHT_ISSUES or budget_issue or incomplete) and not state.lock_flight:
        current_ids = (
            (state.flight_pair.outbound.id, state.flight_pair.inbound.id)
            if state.flight_pair is not None
            else None
        )
        for pair in pairs:
            pair_ids = (pair.outbound.id, pair.inbound.id)
            if pair_ids == current_ids:
                continue
            add(
                f"рейс {pair.outbound.id} + {pair.inbound.id}",
                replace(state, flight_pair=pair),
            )

    if (codes & HOTEL_ISSUES or budget_issue or incomplete) and not state.lock_hotel:
        for hotel in hotels:
            if state.hotel is not None and hotel.id == state.hotel.id:
                continue
            add(f"отель {hotel.name}", replace(state, hotel=hotel))

    if codes & OPENING_ISSUES:
        problem_ids = _opening_problem_ids(current, places_by_id)
        if problem_ids:
            add(
                "исключить места с нарушением часов работы",
                replace(state, excluded=state.excluded | problem_ids),
            )
            for place_id in sorted(problem_ids):
                add(
                    f"исключить место {place_id}",
                    replace(state, excluded=state.excluded | {place_id}),
                )

    if "WEATHER_CONFLICT" in codes:
        dates = weather_conflict_dates(current, places_by_id)
        if dates:
            add(
                "поставить помещения в приоритет в плохую погоду",
                replace(state, prefer_indoor_dates=state.prefer_indoor_dates | dates),
            )

    if "DAY_OVERLOADED" in codes:
        current_limit = state.max_places_per_day or MAX_PLACES_PER_DAY
        if current_limit > 1:
            add(
                f"ограничить день {current_limit - 1} местами",
                replace(state, max_places_per_day=current_limit - 1),
            )
        if state.max_walking_km_per_day is None:
            limit = float(assumptions.get("thresholds", {}).get("max_walking_km_per_day", 15))
            add(
                f"ограничить ходьбу {limit:g} км в день",
                replace(state, max_walking_km_per_day=limit),
            )

    return candidates


def _summary_notes(
    initial: TripPlan,
    final: TripPlan,
    *,
    applied: Sequence[str],
    attempts: int,
    max_attempts: int,
) -> list[str]:
    notes: list[str] = []
    if not initial.issues:
        notes.append(
            "Reality Check не нашёл проблем по заданным правилам; "
            "автоматические исправления не потребовались."
        )
        return notes

    if applied:
        notes.append(
            f"Применено автоматических исправлений: {len(applied)} "
            f"(попыток: {attempts} из {max_attempts})."
        )
    else:
        notes.append("Автоматические исправления не применялись: подходящих вариантов не найдено.")

    if final.issues:
        codes = "; ".join(
            f"{issue.code} ({issue.severity.label_ru.lower()})" for issue in final.issues
        )
        notes.append(
            f"После исправлений остались нерешённые проблемы: {codes}. "
            "Они требуют решения пользователя."
        )
        if attempts >= max_attempts:
            notes.append(f"Достигнут лимит попыток исправления: {max_attempts}.")
    else:
        notes.append("В пределах заданных правил проблем не осталось.")

    return notes


def build_corrected_plan(
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
    progress: Callable[[str], None] | None = None,
) -> CorrectionResult:
    """Собирает план, проверяет его и пытается исправить найденные проблемы."""
    today = today or date.today()
    provider_notes_list: list[str] = list(provider_notes or [])
    if provider is None:
        provider, selection_notes = get_provider()
        provider_notes_list.extend(selection_notes)

    if assumptions is None:
        try:
            assumptions = load_assumptions()
        except ConfigError as error:
            provider_notes_list.append(f"Не удалось прочитать допущения проекта: {error}")
            assumptions = {}

    thresholds = assumptions.get("thresholds", {})
    max_attempts = max(1, min(10, int(thresholds.get("max_correction_attempts", 3))))

    initial = build_trip_plan(
        request,
        provider=provider,
        today=today,
        assumptions=assumptions,
        provider_notes=provider_notes_list,
        flight_pair=flight_pair,
        transport_options=transport_options,
        transport_alternatives=transport_alternatives,
        hotel_choice=hotel_choice,
        hotel_options=hotel_options,
        progress=progress,
    )
    current = initial
    state = _State(
        flight_pair=pair_from_plan(initial),
        transport_options=initial.transport_options,
        transport_alternatives=initial.transport_alternatives,
        hotel=initial.hotel,
        hotel_options=initial.hotel_options,
        lock_hotel=hotel_choice is not None,
        lock_flight=flight_pair is not None,
        max_places_per_day=STYLE_PLACES_PER_DAY.get(request.trip_style),
    )
    applied: list[str] = []
    attempts = 0
    correction_notes: list[str] = []

    if progress is not None:
        progress("Ищу альтернативы для автоматических исправлений")
    pairs, hotels, places = _load_candidates(
        request,
        provider=provider,
        assumptions=assumptions,
        plan=initial,
        notes=correction_notes,
    )
    places_by_id = {place.id: place for place in places}

    while current.issues and attempts < max_attempts:
        if progress is not None:
            progress(
                f"Проверяю безопасные варианты: попытка {attempts + 1} "
                f"из {max_attempts}"
            )
        candidates = _make_candidates(
            current,
            state=state,
            pairs=pairs,
            hotels=hotels,
            places_by_id=places_by_id,
            request=request,
            provider=provider,
            today=today,
            assumptions=assumptions,
            provider_notes=provider_notes_list,
        )
        if not candidates:
            correction_notes.append(
                "Дальнейших автоматических исправлений не найдено: "
                "оставшиеся проблемы требуют решения пользователя."
            )
            break

        best = min(candidates, key=lambda candidate: _score(candidate.plan))
        if _score(best.plan) >= _score(current):
            correction_notes.append(
                "Очередной шаг не снизил риск: доступные варианты не лучше текущего плана."
            )
            break

        current = best.plan
        state = best.state
        applied.append(best.label)
        attempts += 1

    correction_notes.extend(
        _summary_notes(
            initial,
            current,
            applied=applied,
            attempts=attempts,
            max_attempts=max_attempts,
        )
    )
    final_plan = current.model_copy(
        update={"data_notes": list(current.data_notes) + correction_notes}
    )

    return CorrectionResult(
        plan=final_plan,
        initial_plan=initial,
        attempts=attempts,
        applied=applied,
        notes=correction_notes,
    )
