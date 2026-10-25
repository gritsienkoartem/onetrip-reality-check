"""Текстовое и Markdown-представление плана для интерфейса и выгрузки."""
from __future__ import annotations

from datetime import date, datetime, time

from models import CostBreakdown, Issue, Severity, TripPlan


def format_money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def format_date_ru(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value is not None else "дата не указана"


def format_time_value(value: time | None) -> str:
    return value.strftime("%H:%M") if value is not None else "—"


def format_fetched_at(value: datetime | None) -> str:
    """Момент получения данных в местном времени («26.10.2026, 00:45»)."""
    if value is None:
        return "время получения не указано"
    return value.astimezone().strftime("%d.%m.%Y, %H:%M")


def format_duration(minutes: int) -> str:
    hours, minutes = divmod(max(0, minutes), 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"


def risk_label(plan: TripPlan) -> str:
    if plan.risk_level is None:
        return "не рассчитан"
    return f"{plan.risk_level.label_ru.lower()} ({plan.risk_score})"


def issue_to_text(issue: Issue) -> str:
    return (
        f"{issue.severity.label_ru} · {issue.code}\n"
        f"{issue.description}\n"
        f"Источник: {issue.source}\n"
        f"Рекомендация: {issue.recommendation}"
    )


def cost_rows(cost: CostBreakdown) -> list[tuple[str, float]]:
    return [
        ("Транспорт", cost.flights),
        ("Проживание", cost.accommodation),
        ("Трансферы", cost.transfers),
        ("Багаж", cost.baggage),
        ("Туристический налог", cost.tourist_tax),
        ("Местный транспорт", cost.local_transport),
        ("Билеты и места", cost.attractions),
    ]


MODE_WORDS = {"flight": "самолёт", "train": "поезд", "bus": "автобус"}
STYLE_WORDS = {
    "calm": "спокойный (до 2 мест в день)",
    "balanced": "сбалансированный",
    "intense": "насыщенный (до 5 мест в день)",
}
ACCESS_MODE_WORDS = {
    "walk": "пешком",
    "taxi": "такси",
    "public_transport": "общественный транспорт",
    "bus": "автобус",
    "transfer": "трансфер",
}
PRICE_KIND_WORDS = {
    "real": "реальная цена из кэша Travelpayouts",
    "estimate": "оценка проекта",
    "mock": "тестовые данные",
}
SOURCE_KIND_WORDS = {
    "real": "реальная цена из кэша",
    "estimate": "оценка проекта",
    "mock": "тестовые данные",
}
TUTU_PREFIX = "Tutu.ru"
AVIASALES_PREFIX = "Aviasales"
PACKAGE_PREFIXES = ("Level.Travel", "Travelata")


def price_kind_label(item) -> str:
    """Подпись цены с учётом источника: кэш, оценка или тестовые данные."""
    kind = getattr(item, "price_kind", "")
    source = getattr(item, "source", "") or ""
    if kind == "real":
        if source.startswith(AVIASALES_PREFIX):
            return "кэш цен Aviasales (до 48 ч)"
        if source.startswith("Level.Travel"):
            return "предложение Level.Travel за всё проживание"
        if source.startswith("Travelata"):
            return "предложение Travelata за всё проживание"
    if kind == "estimate" and source.startswith(TUTU_PREFIX):
        return "кэш расписания Tutu.ru, цена ориентировочная"
    return PRICE_KIND_WORDS.get(kind, kind)


def baggage_label(flight) -> str:
    if flight.baggage_included:
        return "включён"
    if not flight.baggage_known:
        return "источник не сообщил"
    return "не включён"


def _flight_line(title: str, flight) -> str:
    if flight is None:
        return f"- {title}: не выбран"
    mode = MODE_WORDS.get(flight.mode, "транспорт")
    price_kind = price_kind_label(flight)
    line = (
        f"- {title}: {mode}, {flight.id}, {flight.airline}, "
        f"{format_date_ru(flight.departure_time.date())} "
        f"{format_time_value(flight.departure_time.time())} → "
        f"{format_date_ru(flight.arrival_time.date())} "
        f"{format_time_value(flight.arrival_time.time())}, "
        f"{format_money(flight.price_rub)} ({price_kind}), "
        f"багаж: {baggage_label(flight)}"
    )
    if flight.booking_url:
        line += f", [ссылка на предложение]({flight.booking_url})"
    if flight.departure_node or flight.arrival_node:
        line += (
            f"; узлы: {flight.departure_node or '—'} → "
            f"{flight.arrival_node or '—'}"
        )
    for label, leg in (
        ("до узла", flight.access_to_departure),
        ("от узла", flight.access_from_arrival),
    ):
        if leg is None:
            continue
        line += (
            f"; {label}: {ACCESS_MODE_WORDS.get(leg.mode, leg.mode)}, "
            f"{leg.distance_km:g} км, ~{leg.minutes} мин, "
            f"{format_money(leg.price_rub)} (оценка)"
        )
    return line


def _sorted_issues(plan: TripPlan) -> list[Issue]:
    order = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}
    return sorted(plan.issues, key=lambda issue: (order[issue.severity], issue.code))


def plan_to_markdown(
    plan: TripPlan,
    *,
    initial_plan: TripPlan | None = None,
    applied: list[str] | None = None,
    correction_notes: list[str] | None = None,
) -> str:
    """Собирает подробный Markdown-отчёт о поездке."""
    request = plan.request
    origin = request.origin_city or "—"
    destination = request.destination_city or "—"
    lines: list[str] = [
        f"# Поездка: {origin} → {destination}",
        "",
        f"**Даты:** {format_date_ru(request.start_date)} — {format_date_ru(request.end_date)}"
        f" ({request.duration_days or '?'} дн.)",
        f"**Путешественники:** взрослых — {request.adults}, детей — {request.children}, "
        f"всего — {request.travelers}",
        f"**Темп поездки:** {STYLE_WORDS.get(request.trip_style, request.trip_style)}",
        f"**Уровень риска:** {risk_label(plan)}",
        f"**Данные получены:** {format_fetched_at(plan.generated_at)}",
    ]
    if request.accommodation_preferences:
        lines.append(
            "**Пожелания к жилью:** "
            + ", ".join(request.accommodation_preferences)
        )
    if initial_plan is not None and initial_plan.risk_score != plan.risk_score:
        lines.append(f"**Риск до исправлений:** {risk_label(initial_plan)}")
    if plan.cost is not None:
        lines.append(f"**Оценка стоимости:** {format_money(plan.cost.total)}")
    if request.budget_amount is not None:
        scope = "на человека" if request.budget_scope == "person" else "на всю группу"
        lines.append(
            f"**Бюджет:** {request.budget_amount:,.0f} {request.currency} ({scope})".replace(",", " ")
        )
    lines.append("")

    if plan.data_notes:
        lines.append("> В отчёте использованы данные провайдера; пометки о тестовых данных и допущениях — в конце.")
        lines.append("")

    lines.append("## Транспорт")
    lines.append(_flight_line("Туда", plan.flight_out))
    lines.append(_flight_line("Обратно", plan.flight_back))
    if len(plan.transport_options) > 1 or len(plan.transport_alternatives) > 1:
        comparison = []
        seen_options: set[tuple[str, str, str]] = set()
        for option in list(plan.transport_alternatives) + list(plan.transport_options):
            key = (option.mode, option.outbound.id, option.inbound.id)
            if key in seen_options:
                continue
            seen_options.add(key)
            comparison.append(option)
        lines.append("")
        lines.append("**Сравнение видов транспорта:**")
        for option in comparison:
            selected = (
                plan.flight_out is not None
                and option.outbound.id == plan.flight_out.id
                and plan.flight_back is not None
                and option.inbound.id == plan.flight_back.id
            )
            kind = price_kind_label(option.outbound)
            lines.append(
                f"- {MODE_WORDS.get(option.mode, option.mode)}: "
                f"{format_money(option.total_price_rub)} туда и обратно, "
                f"в пути {format_duration(option.total_duration_minutes)}, {kind}"
                + (" — выбран" if selected else "")
            )
    if plan.price_sources:
        lines.append("")
        lines.append("**Сравнение сервисов (минимальная цена за направление):**")
        for source in plan.price_sources:
            direction = "туда" if source.direction == "outbound" else "обратно"
            offers_word = "предложение" if source.offers_count == 1 else "предложений"
            line = (
                f"- {direction.capitalize()}, {MODE_WORDS.get(source.mode, source.mode)}: "
                f"{source.service} — от {format_money(source.cheapest_rub)} "
                f"({source.offers_count} {offers_word}, "
                f"{SOURCE_KIND_WORDS.get(source.price_kind, source.price_kind)})"
            )
            if source.booking_url:
                line += f", [ссылка на предложение]({source.booking_url})"
            lines.append(line)
    lines.append("")

    lines.append("## Отель")
    if plan.hotel is None:
        lines.append("Отель не выбран.")
    else:
        hotel = plan.hotel
        rating = "нет данных" if hotel.rating_source == "Нет данных" else f"{hotel.rating:g}"
        price_kind = price_kind_label(hotel)
        if not hotel.center_known:
            center_text = "расстояние до центра источник не сообщил"
        elif hotel.distance_to_center_km is not None:
            center_text = (
                f"до центра {hotel.distance_to_center_km:g} км "
                f"(~{hotel.minutes_to_center} мин)"
            )
        else:
            center_text = f"до центра {hotel.minutes_to_center} мин"
        if not hotel.metro_known:
            metro_text = "расстояние до метро источник не сообщил"
        elif not hotel.has_metro:
            metro_text = "метро рядом не найдено в OpenStreetMap"
        elif hotel.distance_to_metro_km is not None:
            metro_text = (
                f"до метро {hotel.distance_to_metro_km:g} км "
                f"(~{hotel.minutes_to_metro} мин)"
            )
        else:
            metro_text = f"до метро {hotel.minutes_to_metro} мин"
        total_text = (
            f", всего {format_money(hotel.stay_total_rub)} за проживание"
            if hotel.stay_total_rub is not None
            else ""
        )
        meal_text = f", питание: {hotel.meal_plan}" if hotel.meal_plan else ""
        lines.append(
            f"- {hotel.name}, {hotel.district}: "
            f"{format_money(hotel.price_per_night_rub)} за ночь ({price_kind})"
            f"{total_text}{meal_text}, "
            f"рейтинг {rating}, {center_text}, {metro_text}."
        )
        if hotel.booking_url:
            lines.append(f"  [Ссылка на предложение]({hotel.booking_url}).")
        if hotel.tags:
            lines.append(f"  Темы: {', '.join(hotel.tags)}.")
        if plan.hotel_search_url:
            lines.append("")
            lines.append(
                f"[Другие отели на те же даты на Trip.com]({plan.hotel_search_url})."
            )
        if len(plan.hotel_options) > 1:
            lines.append("")
            lines.append("Варианты проживания:")
            for option in plan.hotel_options:
                themes = f" ({', '.join(option.tags)})" if option.tags else ""
                selected = " — выбран" if option.id == hotel.id else ""
                lines.append(
                    f"- {option.name}: {format_money(option.price_per_night_rub)} "
                    f"за ночь{themes}{selected}"
                )
    lines.append("")

    if plan.hotel_transfers:
        lines.append("## Дорога до отеля")
        lines.append("")
        lines.append(
            "Оценки проекта по расстоянию; конкретный маршрут, тариф и расписание "
            "не подтверждены."
        )
        lines.append("")
        for transfer in plan.hotel_transfers:
            lines.append(
                f"**{transfer.title}:** {transfer.from_name} → {transfer.to_name}, "
                f"{transfer.distance_km:g} км."
            )
            for option in transfer.options:
                line = (
                    f"- {ACCESS_MODE_WORDS.get(option.mode, option.mode)}: "
                    f"~{option.minutes} мин, {format_money(option.price_rub)}"
                )
                if option.note:
                    line += f". {option.note}"
                if option.maps_url:
                    line += f" [Маршрут в OpenStreetMap]({option.maps_url})"
                if option.yandex_url:
                    line += f" [Маршрут в Яндекс Картах]({option.yandex_url})"
                lines.append(line)
            lines.append("")

    lines.append("## Маршрут по дням")
    if not plan.days:
        lines.append("Дни не сформированы.")
    for day in plan.days:
        title = f"### День {day.day_number}"
        if day.day is not None:
            title += f" — {format_date_ru(day.day)}"
        lines.append(title)
        if day.weather is not None:
            weather = day.weather
            probability = (
                f", осадки {weather.precipitation_probability}%"
                if weather.precipitation_probability is not None
                else ""
            )
            lines.append(
                f"Погода: {weather.condition}, {weather.temperature_c:g} °C{probability}."
            )
        if not day.items:
            lines.append("- Свободное время или день без запланированных мест.")
        for item in day.items:
            lines.append(
                f"- {format_time_value(item.start_time)}–{format_time_value(item.end_time)} — "
                f"{item.name}"
                + (f", {format_money(item.cost_rub)}" if item.cost_rub else "")
                + (f", пешком {item.walking_km_from_previous:.1f} км" if item.walking_km_from_previous else "")
            )
        lines.append("")

    if plan.cost is not None:
        lines.append("## Стоимость")
        lines.append("")
        lines.append("| Статья | Сумма |")
        lines.append("|---|---:|")
        for label, value in cost_rows(plan.cost):
            lines.append(f"| {label} | {format_money(value)} |")
        lines.append(f"| **Итого (оценка)** | **{format_money(plan.cost.total)}** |")
        lines.append("")
        for note in plan.cost.notes:
            lines.append(f"- {note}")
        lines.append("")

    lines.append("## Проверка реалистичности")
    if not plan.issues:
        lines.append("Проблем по заданным правилам не найдено.")
    for issue in _sorted_issues(plan):
        lines.append("")
        lines.append(f"### {issue.severity.label_ru}: {issue.code}")
        lines.append(issue.description)
        lines.append("")
        lines.append(f"Источник: {issue.source}")
        lines.append("")
        lines.append(f"Рекомендация: {issue.recommendation}")
    lines.append("")

    if applied or correction_notes:
        lines.append("## Автоматические исправления")
        if applied:
            for label in applied:
                lines.append(f"- {label}")
        for note in correction_notes or []:
            lines.append(f"- {note}")
        lines.append("")

    if plan.data_notes:
        lines.append("## Пояснения к данным")
        for note in dict.fromkeys(plan.data_notes):
            lines.append(f"- {note}")
        lines.append("")

    lines.append("---")
    lines.append(
        "Стоимость и риск — оценки проекта по заданным правилам, "
        "а не подтверждённые факты о реальной поездке."
    )
    return "\n".join(lines)
