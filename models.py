"""Модели данных OneTrip Reality Check (Pydantic v2).

Здесь описано, как выглядят запрос пользователя, перелёты, отели, места,
найденные проблемы и итоговый план поездки. Вся остальная логика
(парсер, проверки, планировщик) будет опираться на эти модели.
"""
from __future__ import annotations

from datetime import date, datetime, time
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Подпись для источника, когда данных нет. Мы не придумываем ответ.
NO_DATA = "Нет данных"

# Подпись источника для тестовых фикстур (в продукте не используется).
MOCK_SOURCE = "Тестовые данные (вымышленные значения)"


# --------------------------------------------------------------------------
# Уровни серьёзности и риска
# --------------------------------------------------------------------------
class Severity(str, Enum):
    """Серьёзность одной найденной проблемы."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def label_ru(self) -> str:
        return {"low": "Низкая", "medium": "Средняя", "high": "Высокая"}[self.value]


class RiskLevel(str, Enum):
    """Общий уровень риска поездки (эвристика проекта)."""

    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"

    @property
    def label_ru(self) -> str:
        return {"Low": "Низкий", "Medium": "Средний", "High": "Высокий"}[self.value]


# --------------------------------------------------------------------------
# Запрос пользователя
# --------------------------------------------------------------------------
class TripRequest(BaseModel):
    """Структурированный запрос на поездку (результат работы парсера)."""

    raw_text: str = ""  # исходный текст пользователя

    origin_city: Optional[str] = None  # город вылета
    destination_city: Optional[str] = None  # город назначения

    start_date: Optional[date] = None
    end_date: Optional[date] = None
    period_description: Optional[str] = None  # например, «середина ноября»
    duration_days: Optional[int] = Field(default=None, ge=1, le=30)

    budget_amount: Optional[float] = Field(default=None, gt=0)
    currency: str = "RUB"
    # Бюджет может быть указан на всю группу или на одного путешественника.
    budget_scope: Literal["group", "person"] = "group"

    # Число взрослых передаётся в поиск отелей; дети пока только фиксируются
    # в запросе, потому что источники не принимают возраст и детские тарифы.
    adults: int = Field(default=1, ge=1, le=9)
    children: int = Field(default=0, ge=0, le=9)

    interests: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)  # что пользователь не любит
    accommodation_preferences: list[str] = Field(default_factory=list)
    transport_preferences: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    # Темп поездки влияет на число мест в дне, а не на цены.
    trip_style: Literal["calm", "balanced", "intense"] = "balanced"

    @property
    def travelers(self) -> int:
        """Всего путешественников: взрослые и дети."""
        return self.adults + self.children

    @field_validator("origin_city", "destination_city")
    @classmethod
    def _clean_city(cls, value: Optional[str]) -> Optional[str]:
        """Убираем пробелы; пустая строка превращается в None."""
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("currency")
    @classmethod
    def _normalize_currency(cls, value: str) -> str:
        """Код валюты — три латинские буквы, например RUB."""
        value = value.strip().upper()
        if len(value) != 3 or not value.isascii() or not value.isalpha():
            raise ValueError("Код валюты должен состоять из трёх латинских букв, например RUB")
        return value

    @model_validator(mode="after")
    def _check_dates(self) -> "TripRequest":
        """Проверяем даты и при необходимости считаем длительность."""
        if self.start_date and self.end_date:
            if self.end_date < self.start_date:
                raise ValueError("Дата окончания поездки раньше даты начала")
            days = (self.end_date - self.start_date).days + 1
            if days > 30:
                raise ValueError("Поездки длиннее 30 дней пока не поддерживаются")
            if self.duration_days is None:
                self.duration_days = days
        return self

    def missing_fields(self) -> list[str]:
        """Какие ключевые данные не удалось определить.

        Допущение MVP: обязательны город вылета, город назначения и
        длительность (или даты). Бюджет необязателен: без него просто
        не выполняется проверка превышения бюджета.
        """
        missing: list[str] = []
        if not self.origin_city:
            missing.append("origin_city")
        if not self.destination_city:
            missing.append("destination_city")
        if self.duration_days is None and not (self.start_date and self.end_date):
            missing.append("duration_days")
        return missing


# --------------------------------------------------------------------------
# Данные провайдеров: перелёты, отели, места, погода
# --------------------------------------------------------------------------
class AccessLeg(BaseModel):
    """Дорога от центра места до транспортного узла (оценка проекта)."""

    mode: Literal["walk", "taxi", "bus", "transfer"] = "taxi"
    distance_km: float = Field(ge=0)
    minutes: int = Field(ge=0)
    price_rub: float = Field(ge=0)
    source: str = NO_DATA
    is_estimate: bool = True


class Flight(BaseModel):
    """Билет туда или обратно: самолёт, поезд или автобус.

    Имя класса сохранено ради совместимости; вид транспорта задаёт mode.
    """

    id: str
    direction: Literal["outbound", "return"]  # туда / обратно
    origin: str
    destination: str
    airline: str  # перевозчик (авиакомпания, РЖД, автобусный оператор)
    departure_time: datetime
    arrival_time: datetime
    price_rub: float = Field(ge=0)
    stops: int = Field(default=0, ge=0)
    # None — длительность пересадок источник не сообщил; это не «0 минут».
    layover_minutes: Optional[list[int]] = None
    connection_type: Literal["none", "domestic", "international"] = "none"
    baggage_included: bool = False
    # False — источник не сообщил условия багажа; это не «багаж не включён».
    baggage_known: bool = True
    baggage_fee_rub: float = Field(default=0, ge=0)  # цена багажа, если он не включён
    mode: Literal["flight", "train", "bus"] = "flight"
    # mock — данные мока, estimate — расчёт проекта, real — цена из внешнего источника.
    price_kind: Literal["mock", "estimate", "real"] = "mock"
    is_mock: bool = True
    source: str = NO_DATA
    booking_url: str = ""  # короткая ссылка на выдачу сервиса, если она есть
    # Узлы OpenStreetMap, между которыми идёт поездка, и дорога до них.
    departure_node: str = ""
    arrival_node: str = ""
    # Координаты узлов нужны, чтобы посчитать дорогу уже до отеля, а не
    # только до центра города.
    departure_node_latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    departure_node_longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    arrival_node_latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    arrival_node_longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    access_to_departure: Optional[AccessLeg] = None
    access_from_arrival: Optional[AccessLeg] = None

    @model_validator(mode="after")
    def _check_times_and_layovers(self) -> "Flight":
        if self.arrival_time <= self.departure_time:
            raise ValueError("Время прилёта должно быть позже времени вылета")
        if self.layover_minutes is not None:
            if len(self.layover_minutes) != self.stops:
                raise ValueError("Число пересадок не совпадает с числом значений layover_minutes")
            if any(m < 0 for m in self.layover_minutes):
                raise ValueError("Время пересадки не может быть отрицательным")
        return self


class TransportOption(BaseModel):
    """Лучшая пара «туда + обратно» для одного вида транспорта.

    Хранится в плане, чтобы показать, какие виды транспорта сравнивались,
    а не только выбранный вариант.
    """

    mode: Literal["flight", "train", "bus"]
    outbound: Flight
    inbound: Flight
    total_price_rub: float = Field(ge=0)
    total_duration_minutes: int = Field(ge=0)
    price_kind: Literal["mock", "estimate", "real"] = "mock"


class TransferOption(BaseModel):
    """Один способ добраться между отелем и транспортным узлом.

    Все цены и время здесь — оценка проекта по расстоянию, если источник
    явно не указал иное. Это не тариф такси и не расписание.
    """

    mode: Literal["walk", "public_transport", "taxi"]
    distance_km: float = Field(ge=0)
    minutes: int = Field(ge=0)
    price_rub: float = Field(ge=0)
    source: str = "Оценка проекта"
    is_estimate: bool = True
    note: str = ""
    maps_url: str = ""
    yandex_url: str = ""


class HotelTransfer(BaseModel):
    """Дорога между транспортным узлом и выбранным отелем."""

    title: str
    from_name: str
    to_name: str
    distance_km: float = Field(ge=0)
    options: list[TransferOption] = Field(default_factory=list)
    source: str = "Оценка проекта"
    is_estimate: bool = True

    def cheapest(self) -> Optional[TransferOption]:
        return min(self.options, key=lambda option: option.price_rub) if self.options else None

    def by_mode(self, mode: str) -> Optional[TransferOption]:
        return next((option for option in self.options if option.mode == mode), None)


class PriceSource(BaseModel):
    """Минимальная цена одного сервиса в одном направлении.

    Сводка нужна, чтобы честно показать, какие сервисы ответили и где
    билет дешевле. Она не заменяет выдачу сервиса и не подтверждает места.
    """

    service: str
    mode: Literal["flight", "train", "bus"]
    direction: Literal["outbound", "return"]
    cheapest_rub: float = Field(ge=0)
    offers_count: int = Field(ge=1)
    price_kind: Literal["mock", "estimate", "real"] = "mock"
    booking_url: str = ""


class Hotel(BaseModel):
    """Один вариант проживания."""

    id: str
    name: str
    district: str
    city: str
    price_per_night_rub: float = Field(ge=0)
    rating: float = Field(ge=0, le=10)
    minutes_to_center: int = Field(ge=0)
    minutes_to_metro: int = Field(ge=0)
    tourist_tax_per_night_rub: float = Field(default=0, ge=0)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    tags: list[str] = Field(default_factory=list)
    # Откуда взяты рейтинг и цена: мок-данные, оценка проекта или реальный сервис.
    rating_source: str = MOCK_SOURCE
    is_mock: bool = True
    source: str = NO_DATA
    # Настоящие расстояния из OpenStreetMap: нужны для проверок и честного
    # отображения вместо постоянных «25 минут до центра» и «15 до метро».
    distance_to_center_km: Optional[float] = Field(default=None, ge=0)
    distance_to_metro_km: Optional[float] = Field(default=None, ge=0)
    distance_to_station_km: Optional[float] = Field(default=None, ge=0)
    # False означает «метро рядом не найдено в OpenStreetMap», а не «его нет».
    has_metro: bool = True
    # False — источник не сообщил расстояние; проверка и подписи это учитывают.
    center_known: bool = True
    metro_known: bool = True
    # mock — вымышленная цена, estimate — расчёт проекта, real — цена сервиса.
    price_kind: Literal["mock", "estimate", "real"] = "mock"
    # Полная цена проживания на выбранные даты и ссылка на предложение.
    stay_total_rub: Optional[float] = Field(default=None, ge=0)
    booking_url: str = ""
    meal_plan: str = ""


class OpeningHours(BaseModel):
    """Часы работы. Работа через полночь в MVP не поддерживается."""

    opens: time
    closes: time
    closed_weekdays: list[int] = Field(default_factory=list)  # 0 = понедельник ... 6 = воскресенье

    @field_validator("closed_weekdays")
    @classmethod
    def _check_weekdays(cls, value: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in value):
            raise ValueError("День недели должен быть числом от 0 (понедельник) до 6 (воскресенье)")
        return value

    @model_validator(mode="after")
    def _check_order(self) -> "OpeningHours":
        if self.closes <= self.opens:
            raise ValueError("Время закрытия должно быть позже времени открытия")
        return self


class Place(BaseModel):
    """Достопримечательность, кофейня, парк и т. п."""

    id: str
    name: str
    city: str
    categories: list[str] = Field(default_factory=list)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    indoor: bool
    visit_minutes: int = Field(gt=0)
    entrance_fee_rub: float = Field(default=0, ge=0)
    rating: float = Field(ge=0, le=10)
    crowd_level: Literal["low", "medium", "high"]
    # None означает «Нет данных»: это НЕ значит, что место закрыто.
    opening_hours: Optional[OpeningHours] = None
    hours_source: str = NO_DATA
    # Рейтинг и входной билет у открытых источников часто отсутствуют:
    # тогда rating_source = NO_DATA, а rating — нейтральная оценка проекта.
    rating_source: str = MOCK_SOURCE
    is_mock: bool = True
    source: str = NO_DATA


class WeatherDay(BaseModel):
    """Прогноз погоды на один день (источник обязателен)."""

    day: date
    condition: str
    temperature_c: float
    precipitation_probability: Optional[int] = Field(default=None, ge=0, le=100)
    is_mock: bool = True
    source: str = NO_DATA


# --------------------------------------------------------------------------
# Стоимость, проблемы, итоговый план
# --------------------------------------------------------------------------
class CostBreakdown(BaseModel):
    """Разбивка стоимости поездки в рублях."""

    flights: float = Field(default=0, ge=0)
    accommodation: float = Field(default=0, ge=0)
    transfers: float = Field(default=0, ge=0)
    baggage: float = Field(default=0, ge=0)
    tourist_tax: float = Field(default=0, ge=0)
    local_transport: float = Field(default=0, ge=0)
    attractions: float = Field(default=0, ge=0)
    is_estimate: bool = True  # оценка, а не подтверждённая цена
    notes: list[str] = Field(default_factory=list)

    @property
    def total(self) -> float:
        return (
            self.flights
            + self.accommodation
            + self.transfers
            + self.baggage
            + self.tourist_tax
            + self.local_transport
            + self.attractions
        )


class Issue(BaseModel):
    """Проблема, найденная проверкой Reality Check."""

    code: str  # уникальный идентификатор, например "LATE_ARRIVAL"
    severity: Severity
    description: str  # объяснение на русском
    source: str  # источник данных или пометка «Оценка»
    recommendation: str  # что предлагаем сделать, на русском


class ItineraryItem(BaseModel):
    """Один пункт дневного плана."""

    place_id: str
    name: str
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    walking_km_from_previous: float = Field(default=0, ge=0)
    cost_rub: float = Field(default=0, ge=0)
    note: str = ""


class DayPlan(BaseModel):
    """План одного дня."""

    day_number: int = Field(ge=1)
    day: Optional[date] = None
    items: list[ItineraryItem] = Field(default_factory=list)
    weather: Optional[WeatherDay] = None

    @property
    def total_walking_km(self) -> float:
        return sum(item.walking_km_from_previous for item in self.items)


class TripPlan(BaseModel):
    """Полный план поездки."""

    request: TripRequest
    flight_out: Optional[Flight] = None
    flight_back: Optional[Flight] = None
    transport_options: list[TransportOption] = Field(default_factory=list)
    hotel: Optional[Hotel] = None
    # Несколько найденных вариантов проживания; hotel — выбранный из них.
    hotel_options: list[Hotel] = Field(default_factory=list)
    # До нескольких пар «туда + обратно» для честного сравнения, включая
    # варианты дороже выбранного.
    transport_alternatives: list[TransportOption] = Field(default_factory=list)
    # Общая выдача отелей на те же даты (например, Trip.com), если сервис её вернул.
    hotel_search_url: str = ""
    # Минимальные цены по каждому ответившему сервису, без скрытия дорогих.
    price_sources: list[PriceSource] = Field(default_factory=list)
    # Дорога между узлом прилёта/вылета и выбранным отелем.
    hotel_transfers: list[HotelTransfer] = Field(default_factory=list)
    days: list[DayPlan] = Field(default_factory=list)
    cost: Optional[CostBreakdown] = None
    issues: list[Issue] = Field(default_factory=list)
    risk_score: int = Field(default=0, ge=0)
    risk_level: Optional[RiskLevel] = None
    data_notes: list[str] = Field(default_factory=list)  # например, «Использованы мок-данные»
    # Момент, когда план собран из ответов источников: по нему интерфейс
    # показывает свежесть данных, не выдавая кэш поставщика за реальное время.
    generated_at: Optional[datetime] = None
