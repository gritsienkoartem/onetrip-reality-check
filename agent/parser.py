"""Разбор запроса пользователя на естественном языке в модель TripRequest.

Как это работает (простыми словами):
1. Сначала правила (регулярные выражения) вытаскивают из текста всё, что
   можно найти надёжно: города из словаря, длительность, даты, бюджет.
2. Если языковая модель настроена, она разбирает запрос «умнее»;
   то, что она не нашла, добирается из результата правил.
3. Любой результат проверяется Pydantic. Если часть данных некорректна,
   она отбрасывается (с пояснением), а не ломает всё приложение.
4. Если не хватает ключевых данных, формируется ОДИН уточняющий вопрос.
5. Если модель недоступна или вернула мусор, работает разбор по правилам.

Ограничения разбора по правилам: города берутся только из словаря
CITY_PATTERNS, числовые даты вида 14.11 не распознаются.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Callable, Optional

from pydantic import BaseModel, Field, ValidationError

import llm
from models import TripRequest

# --------------------------------------------------------------------------
# Константы
# --------------------------------------------------------------------------
SCALAR_FIELDS = (
    "origin_city",
    "destination_city",
    "start_date",
    "end_date",
    "period_description",
    "duration_days",
    "budget_amount",
    "currency",
)
LIST_FIELDS = (
    "interests",
    "avoid",
    "accommodation_preferences",
    "transport_preferences",
    "constraints",
)

FIELD_LABELS_RU = {
    "origin_city": "город вылета",
    "destination_city": "город назначения",
    "start_date": "дата начала",
    "end_date": "дата окончания",
    "period_description": "период поездки",
    "duration_days": "длительность",
    "budget_amount": "бюджет",
    "currency": "валюта",
    "interests": "интересы",
    "avoid": "что избегать",
    "accommodation_preferences": "пожелания к жилью",
    "transport_preferences": "пожелания к транспорту",
    "constraints": "ограничения",
}

CLARIFICATION_QUESTIONS = {
    "origin_city": "Из какого города вы планируете вылетать?",
    "destination_city": "Куда вы хотите поехать?",
    "duration_days": "На сколько дней планируется поездка? (Можно указать даты.)",
}
EMPTY_REQUEST_QUESTION = (
    "Опишите, пожалуйста, поездку: куда, откуда, на сколько дней и с каким бюджетом."
)

# Словарь городов: название -> регулярное выражение для разных падежей.
CITY_PATTERNS: list[tuple[str, str]] = [
    ("Москва", r"москв\w*"),
    ("Санкт-Петербург", r"санкт-петербург\w*|петербург\w*|питер\w*"),
    ("Стамбул", r"стамбул\w*"),
    ("Казань", r"казан\w*"),
    ("Сочи", r"сочи"),
    ("Екатеринбург", r"екатеринбург\w*"),
    ("Новосибирск", r"новосибирск\w*"),
    ("Нижний Новгород", r"нижн\w+\s+новгород\w*"),
    ("Анталья", r"антали\w*"),
    ("Дубай", r"дуба[йяею]"),
    ("Тбилиси", r"тбилис\w*"),
    ("Ереван", r"ерева\w*"),
    ("Минск", r"минск\w*"),
    ("Баку", r"баку"),
    ("Белград", r"белград\w*"),
    ("Париж", r"париж\w*"),
    ("Рим", r"рим(?:а|е|у|ом)?"),
    ("Лондон", r"лондон\w*"),
    ("Берлин", r"берлин\w*"),
    ("Барселона", r"барселон\w*"),
    ("Прага", r"праг(?:а|и|е|у|ой)?"),
]

_PREP_ORIGIN = re.compile(r"(?:^|[\s,.;:—–-])(?:из|от|со?)\s+$", re.IGNORECASE)
_PREP_DEST = re.compile(r"(?:^|[\s,.;:—–-])(?:в|во|на|до|ко?)\s+$", re.IGNORECASE)

_MONTH = (
    r"(?<![а-яё])(январ(?:ь|я|е)|феврал(?:ь|я|е)|март(?:а|е)?|апрел(?:ь|я|е)|ма(?:я|е|й)"
    r"|июн(?:ь|я|е)|июл(?:ь|я|е)|август(?:а|е)?|сентябр(?:ь|я|е)|октябр(?:ь|я|е)"
    r"|ноябр(?:ь|я|е)|декабр(?:ь|я|е))(?![а-яё])"
)
_MONTH_PREFIXES = [
    ("январ", 1), ("феврал", 2), ("март", 3), ("апрел", 4), ("июн", 6), ("июл", 7),
    ("август", 8), ("сентябр", 9), ("октябр", 10), ("ноябр", 11), ("декабр", 12),
    ("ма", 5),  # «ма» проверяем последним, чтобы не спутать с «март»
]
_MONTH_NOM = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
              "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
_MONTH_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
_PART_NAMES = {"начал": "начало", "середин": "середина", "конц": "конец"}

_WORD_NUMS = {
    "один": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
}
_DAY_UNIT = r"(?:дн(?:я|ей|и)?|день|сут(?:ок|ки)?)(?![а-яё])"

_NUM = r"(?P<int>\d{1,3}(?:[ \u00a0.,]\d{3})+|\d+)(?:[.,](?P<frac>\d{1,2}))?(?!\d)"
_MULT = r"(?:\s*(?P<mult>тыс(?:\.|яч\w*)?|к|k|млн|миллион\w*)(?![а-яёa-z]))?"
_CUR_WORDS = r"руб\w*|₽|р(?![а-яё])|долл\w*|usd|евро|eur|€|лир\w*|try|\$"
_BUDGET_PATTERNS = [
    re.compile(rf"бюджет[^\d$€]{{0,25}}{_NUM}{_MULT}(?:\s*(?P<cur>{_CUR_WORDS}))?", re.IGNORECASE),
    re.compile(rf"{_NUM}{_MULT}\s*(?P<cur>{_CUR_WORDS})", re.IGNORECASE),
    re.compile(rf"(?P<cur>[$€])\s*{_NUM}{_MULT}", re.IGNORECASE),
]

# Интересы: (регулярное выражение, нормализованное название).
INTEREST_KEYWORDS: list[tuple[str, str]] = [
    (r"архитектур\w*", "архитектура"),
    (r"кофе\w*|кофейн\w*", "кофе"),
    (r"музе\w*", "музеи"),
    (r"истори\w*", "история"),
    (r"парк\w*", "парки"),
    (r"еда|еду|еды|кухн\w*|гастроном\w*|ресторан\w*|стритфуд", "еда"),
    (r"шопинг\w*|магазин\w*", "шопинг"),
    (r"природ\w*", "природа"),
    (r"пляж\w*", "пляж"),
    (r"ночн\w*\s+жизн\w*|клуб\w*", "ночная жизнь"),
]
AVOID_ONLY_KEYWORDS: list[tuple[str, str]] = [
    (r"толп\w*|многолюд\w*|очеред\w*", "толпы"),
]
_NEGATIVE_MARKER = re.compile(
    r"не\s+люблю|не\s+нрав\w*|не\s+хочу|не\s+перенош\w*|не\s+нужн\w*|избега\w*"
    r"|терпеть\s+не\s+могу|надоел\w*|без(?![а-яё])",
    re.IGNORECASE,
)

ACCOMMODATION_PATTERNS = [
    (r"рядом\s+с\s+метро|у\s+метро|близко\s+к\s+метро", "рядом с метро"),
    (r"в\s+центре", "в центре"),
    (r"у\s+воды|рядом\s+с\s+водой|на\s+берегу", "у воды"),
    (r"(?:рядом|около|у)\s+(?:с\s+)?вокзал\w*", "у вокзала"),
    (r"тих\w+\s+(?:район|отел\w*|место)|\bтихо\b", "тихий район"),
    (r"хостел\w*", "хостел"),
    (r"апартамент\w*", "апартаменты"),
    (r"гостев\w*\s+дом\w*", "гостевой дом"),
    (r"кемпинг\w*|глэмпинг\w*", "кемпинг"),
    (r"необычн\w+\s+(?:жиль|отел|дом)\w*|\bшале\b", "необычное жильё"),
]
TRANSPORT_PATTERNS = [
    (r"прям\w+\s+(?:рейс\w*|перел[её]т\w*)|без\s+пересадок", "прямой рейс"),
    (r"поезд\w*", "поезд"),
    (r"автобус\w*", "автобус"),
]
CONSTRAINT_PATTERNS = [
    (r"с\s+детьм\w*|с\s+ребён\w*|с\s+ребен\w*", "поездка с детьми"),
    (r"с\s+собак\w*|с\s+питомц\w*", "поездка с питомцем"),
    (r"без\s+виз\w*|безвизов\w*", "без визы"),
]


class ParseResult(BaseModel):
    """Результат разбора: запрос, уточняющий вопрос (если нужен) и пояснения."""

    request: TripRequest
    question: Optional[str] = None  # один уточняющий вопрос на русском
    asked_field: Optional[str] = None  # о каком поле задан вопрос
    used_llm: bool = False
    notes: list[str] = Field(default_factory=list)  # пояснения для пользователя


# --------------------------------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------------------------------
def _dedupe(items: list[str]) -> list[str]:
    """Убирает повторы (без учёта регистра), сохраняя порядок."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(item.strip())
    return result


def _month_number(word: str) -> int:
    word = word.lower()
    for prefix, number in _MONTH_PREFIXES:
        if word.startswith(prefix):
            return number
    raise ValueError(f"Неизвестный месяц: {word}")


def _upcoming(day: int, month: int, today: date) -> date:
    """Ближайшая будущая дата с таким днём и месяцем (может быть в следующем году)."""
    candidate = date(today.year, month, day)
    if candidate < today:
        candidate = date(today.year + 1, month, day)
    return candidate


# --------------------------------------------------------------------------
# Разбор по правилам
# --------------------------------------------------------------------------
def _known_cities(text: str) -> list[tuple[int, str]]:
    """Города из словаря, найденные в тексте: [(позиция, название)]."""
    found: dict[str, int] = {}
    for name, pattern in CITY_PATTERNS:
        for match in re.finditer(rf"(?<![а-яё])(?:{pattern})(?![а-яё])", text, re.IGNORECASE):
            if name not in found or match.start() < found[name]:
                found[name] = match.start()
    return sorted((pos, name) for name, pos in found.items())


def _extract_cities(text: str) -> dict:
    result: dict = {}
    unprepared: list[str] = []
    for position, name in _known_cities(text):
        before = text[:position]
        if _PREP_ORIGIN.search(before) and "origin_city" not in result:
            result["origin_city"] = name
        elif _PREP_DEST.search(before) and "destination_city" not in result:
            result["destination_city"] = name
        else:
            unprepared.append(name)
    if "destination_city" not in result:
        for name in unprepared:
            if name != result.get("origin_city"):
                result["destination_city"] = name  # «Хочу Стамбул» — считаем пунктом назначения
                break
    return result


def _extract_duration(text: str) -> dict:
    words = "|".join(_WORD_NUMS)
    # Диапазон «3-4 дня»: берём большее значение.
    match = re.search(rf"(\d{{1,2}})\s*[-–]\s*(\d{{1,2}})\s*{_DAY_UNIT}", text, re.IGNORECASE)
    if match:
        return {"duration_days": max(int(match.group(1)), int(match.group(2)))}
    match = re.search(rf"(\d{{1,2}})\s*{_DAY_UNIT}", text, re.IGNORECASE)
    if match:
        return {"duration_days": int(match.group(1))}
    match = re.search(rf"(?<![а-яё])({words})\s+{_DAY_UNIT}", text, re.IGNORECASE)
    if match:
        return {"duration_days": _WORD_NUMS[match.group(1).lower()]}
    # Ночи: допущение — N ночей = N + 1 день. Числа бывают цифрами и словами.
    match = re.search(
        rf"(?<![а-яё])(\d{{1,2}}|{words})\s+ноч(?:ь|и|ей)(?![а-яё])",
        text,
        re.IGNORECASE,
    )
    if match:
        raw = match.group(1).lower()
        nights = int(raw) if raw.isdigit() else _WORD_NUMS[raw]
        return {"duration_days": nights + 1}
    match = re.search(rf"(\d{{1,2}}|{words})\s+недел(?:ю|и|ь)(?![а-яё])", text, re.IGNORECASE)
    if match:
        raw = match.group(1).lower()
        count = int(raw) if raw.isdigit() else _WORD_NUMS[raw]
        return {"duration_days": count * 7}
    if re.search(r"на\s+неделю(?![а-яё])", text, re.IGNORECASE):
        return {"duration_days": 7}
    return {}


def _extract_dates(text: str, today: date) -> dict:
    try:
        match = re.search(rf"с\s+(\d{{1,2}})\s+по\s+(\d{{1,2}})\s+{_MONTH}", text, re.IGNORECASE)
        if match:
            month = _month_number(match.group(3))
            start = _upcoming(int(match.group(1)), month, today)
            return {"start_date": start, "end_date": date(start.year, month, int(match.group(2)))}

        match = re.search(
            rf"с\s+(\d{{1,2}})\s+{_MONTH}\s+по\s+(\d{{1,2}})\s+{_MONTH}", text, re.IGNORECASE
        )
        if match:
            start = _upcoming(int(match.group(1)), _month_number(match.group(2)), today)
            end = date(start.year, _month_number(match.group(4)), int(match.group(3)))
            if end < start:
                end = date(start.year + 1, end.month, end.day)
            return {"start_date": start, "end_date": end}

        match = re.search(rf"(\d{{1,2}})\s+{_MONTH}", text, re.IGNORECASE)
        if match:
            start = _upcoming(int(match.group(1)), _month_number(match.group(2)), today)
            return {"start_date": start}
    except ValueError:
        return {}  # несуществующая дата, например 31 ноября

    match = re.search(
        rf"(?:(начал\w*|середин\w*|конц\w*)\s+)?{_MONTH}", text, re.IGNORECASE
    )
    if match:
        month = _month_number(match.group(2))
        part = match.group(1)
        if part:
            part_name = next(name for stem, name in _PART_NAMES.items() if part.lower().startswith(stem))
            return {"period_description": f"{part_name} {_MONTH_GEN[month - 1]}"}
        return {"period_description": _MONTH_NOM[month - 1]}
    return {}


def _currency_code(word: str) -> str:
    word = word.lower()
    if word in ("$", "usd") or word.startswith("долл"):
        return "USD"
    if word in ("€", "eur") or word.startswith("евро"):
        return "EUR"
    if word.startswith("лир") or word == "try":
        return "TRY"
    return "RUB"


def _extract_budget(text: str) -> dict:
    for pattern in _BUDGET_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        digits = re.sub(r"[ \u00a0.,]", "", match.group("int"))
        value = float(f"{digits}.{match.group('frac')}") if match.group("frac") else float(digits)
        multiplier = (match.groupdict().get("mult") or "").lower()
        if multiplier.startswith("тыс") or multiplier in ("к", "k"):
            value *= 1000
        elif multiplier.startswith(("млн", "миллион")):
            value *= 1_000_000
        if value <= 0:
            continue
        return {"budget_amount": value, "currency": _currency_code(match.group("cur") or "руб")}
    return {}


def _find_keywords(text: str, include_avoid_only: bool) -> list[str]:
    keywords = INTEREST_KEYWORDS + (AVOID_ONLY_KEYWORDS if include_avoid_only else [])
    return [label for pattern, label in keywords if re.search(pattern, text, re.IGNORECASE)]


def _extract_interests_and_avoid(text: str) -> tuple[list[str], list[str]]:
    interests: list[str] = []
    avoid: list[str] = []
    for clause in re.split(r"[.!?;,\n]+", text):
        clause = clause.strip()
        if not clause:
            continue
        negative = _NEGATIVE_MARKER.search(clause)
        if not negative:
            interests += _find_keywords(clause, include_avoid_only=False)
            continue
        interests += _find_keywords(clause[: negative.start()], include_avoid_only=False)
        hits = _find_keywords(clause[negative.start():], include_avoid_only=True)
        if hits:
            avoid += hits
        elif not negative.group(0).lower().startswith("без"):
            rest = clause[negative.end():].strip(" —–-:")
            if rest and len(rest) <= 40:
                avoid.append(rest)
    return _dedupe(interests), _dedupe(avoid)


def _find_labels(text: str, patterns: list[tuple[str, str]]) -> list[str]:
    return _dedupe([label for pattern, label in patterns if re.search(pattern, text, re.IGNORECASE)])


def _short_answer_city(text: str) -> Optional[str]:
    """Ответ на вопрос о городе, которого нет в словаре: «Нижний Тагил»."""
    cleaned = re.sub(r"^(?:из|в|во|от)\s+", "", text.strip(), flags=re.IGNORECASE).strip(" .!?,;:")
    words = cleaned.split()
    if 1 <= len(words) <= 3 and re.fullmatch(r"[А-Яа-яЁёA-Za-z\- ]+", cleaned):
        return cleaned.title()
    return None


def _apply_asked_field(fields: dict, text: str, asked_field: str) -> None:
    """Учитывает, о каком поле мы только что спросили пользователя."""
    if asked_field in ("origin_city", "destination_city"):
        names = {name for _, name in _known_cities(text)}
        if len(names) == 1:
            city = next(iter(names))
        elif not names:
            city = _short_answer_city(text)
        else:
            return  # несколько городов — работают обычные правила с предлогами
        if city:
            fields[asked_field] = city
            other = "destination_city" if asked_field == "origin_city" else "origin_city"
            if fields.get(other) == city:
                fields.pop(other)
    elif asked_field == "duration_days" and "duration_days" not in fields:
        match = re.fullmatch(r"\s*(\d{1,2})\s*", text)
        if match:
            fields["duration_days"] = int(match.group(1))


def rules_extract(text: str, today: date, asked_field: Optional[str] = None) -> dict:
    """Разбор запроса по правилам. Возвращает только найденные поля."""
    fields: dict = {}
    fields.update(_extract_cities(text))
    fields.update(_extract_duration(text))
    fields.update(_extract_dates(text, today))
    fields.update(_extract_budget(text))

    interests, avoid = _extract_interests_and_avoid(text)
    found_lists = {
        "interests": interests,
        "avoid": avoid,
        "accommodation_preferences": _find_labels(text, ACCOMMODATION_PATTERNS),
        "transport_preferences": _find_labels(text, TRANSPORT_PATTERNS),
        "constraints": _find_labels(text, CONSTRAINT_PATTERNS),
    }
    fields.update({key: value for key, value in found_lists.items() if value})

    if asked_field:
        _apply_asked_field(fields, text, asked_field)
    return fields


# --------------------------------------------------------------------------
# Языковая модель
# --------------------------------------------------------------------------
def build_system_prompt(today: date) -> str:
    return f"""Ты — модуль разбора запросов на путешествие. Сегодня {today.isoformat()}.
Преврати сообщение пользователя в JSON-объект со следующими полями:
{{
  "origin_city": строка или null (город вылета, в именительном падеже, по-русски),
  "destination_city": строка или null (город назначения, в именительном падеже, по-русски),
  "start_date": "ГГГГ-ММ-ДД" или null,
  "end_date": "ГГГГ-ММ-ДД" или null,
  "period_description": строка или null (например, "середина ноября", если точных дат нет),
  "duration_days": целое число или null,
  "budget_amount": число или null,
  "currency": трёхбуквенный код валюты (RUB, USD, EUR...) или null,
  "interests": список строк (что нравится),
  "avoid": список строк (что не нравится или чего избегать),
  "accommodation_preferences": список строк,
  "transport_preferences": список строк,
  "constraints": список строк
}}
Правила:
- Используй только то, что явно сказано в сообщении. Ничего не выдумывай.
- Город может быть любым, даже редким. Верни его как есть, в именительном падеже
  и по-русски («в Иркутске» → «Иркутск»). Не заменяй город на похожий известный.
- Если город вылета не назван, origin_city = null. Не угадывай его.
- Конструкция «поехать в X» или «хочу X» без предлога вылета задаёт destination_city.
- Понимай опечатки и разговорные формы («в вторник», «вернутся», «на 4 дни»).
- Если назван день недели и дата, используй дату. Даты без года — ближайшие будущие.
- N ночей = N + 1 день. «Вернуться через три ночи» после даты выезда — это
  duration_days = 4, а не 3.
- Если известны только дата выезда и длительность, end_date можно не заполнять.
- Если значение неизвестно, пиши null (для списков — пустой список).
- Ответь ТОЛЬКО JSON-объектом, без пояснений и без Markdown."""


def build_user_message(text: str, asked_field: Optional[str]) -> str:
    if asked_field:
        return f'Ассистент только что спросил пользователя про поле "{asked_field}". Ответ пользователя: {text}'
    return text


def _clean_llm_fields(data: dict) -> dict:
    """Оставляет только известные поля и приводит списки к списку строк."""
    cleaned: dict = {}
    for key in SCALAR_FIELDS:
        value = data.get(key)
        if value is None or (isinstance(value, str) and value.strip().lower() in ("", "null", "none")):
            continue
        cleaned[key] = value
    for key in LIST_FIELDS:
        value = data.get(key)
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list):
            items = _dedupe([str(item) for item in value if item is not None])
            if items:
                cleaned[key] = items
    return cleaned


def _fill_gaps(base: dict, helper: dict) -> dict:
    """Добавляет в base то, чего там нет, из helper (результат правил)."""
    result = dict(base)
    for key in ("origin_city", "destination_city", "duration_days", "period_description"):
        if key not in result and key in helper:
            result[key] = helper[key]
    if "start_date" not in result and "end_date" not in result:
        for key in ("start_date", "end_date"):
            if key in helper:
                result[key] = helper[key]
    if "budget_amount" not in result and "budget_amount" in helper:
        result["budget_amount"] = helper["budget_amount"]
        result["currency"] = helper.get("currency", "RUB")
    for key in LIST_FIELDS:
        if key not in result and key in helper:
            result[key] = helper[key]
    return result


# --------------------------------------------------------------------------
# Сборка и объединение запросов
# --------------------------------------------------------------------------
def _build_request(fields: dict, raw_text: str, notes: list[str]) -> TripRequest:
    """Создаёт TripRequest; некорректные поля отбрасывает вместо падения."""
    fields = dict(fields)
    for _ in range(3):
        try:
            return TripRequest(raw_text=raw_text, **fields)
        except ValidationError as error:
            bad: set[str] = set()
            for item in error.errors():
                location = item.get("loc") or ()
                if location:
                    bad.add(str(location[0]))
                else:  # ошибка всей модели — почти всегда в датах
                    bad.update(("start_date", "end_date"))
            bad &= set(fields)
            if not bad:
                break
            for key in bad:
                fields.pop(key)
            labels = ", ".join(sorted(FIELD_LABELS_RU.get(key, key) for key in bad))
            notes.append(f"Часть данных не прошла проверку и была пропущена: {labels}.")
    return TripRequest(raw_text=raw_text)


def merge_requests(previous: TripRequest, new: TripRequest, notes: Optional[list[str]] = None) -> TripRequest:
    """Объединяет прежний запрос с ответом на уточняющий вопрос."""
    notes = notes if notes is not None else []
    data: dict = {}

    for key in ("origin_city", "destination_city", "period_description", "budget_amount"):
        value = getattr(new, key)
        if value is None:
            value = getattr(previous, key)
        if value is not None:
            data[key] = value
    data["currency"] = new.currency if new.budget_amount is not None else previous.currency

    if new.start_date or new.end_date:
        data["start_date"], data["end_date"] = new.start_date, new.end_date
        duration = new.duration_days if new.duration_days is not None else previous.duration_days
    else:
        data["start_date"], data["end_date"] = previous.start_date, previous.end_date
        duration = new.duration_days if new.duration_days is not None else previous.duration_days
    if duration is not None:
        data["duration_days"] = duration

    for key in LIST_FIELDS:
        merged = _dedupe(list(getattr(previous, key)) + list(getattr(new, key)))
        if merged:
            data[key] = merged

    raw_text = "\n".join(part for part in (previous.raw_text, new.raw_text) if part).strip()
    data = {key: value for key, value in data.items() if value is not None}
    return _build_request(data, raw_text, notes)


# --------------------------------------------------------------------------
# Уточняющие вопросы
# --------------------------------------------------------------------------
def next_missing_field(request: TripRequest) -> Optional[str]:
    """Первое недостающее ключевое поле (спрашиваем по одному)."""
    missing = request.missing_fields()
    return missing[0] if missing else None


def build_clarification_question(request: TripRequest) -> Optional[str]:
    """Один краткий вопрос на русском или None, если всё понятно."""
    field = next_missing_field(request)
    return CLARIFICATION_QUESTIONS[field] if field else None


# --------------------------------------------------------------------------
# Главная функция
# --------------------------------------------------------------------------
LLMCall = Callable[[str, str], dict]


def parse_trip_request(
    text: str,
    *,
    today: Optional[date] = None,
    previous: Optional[TripRequest] = None,
    asked_field: Optional[str] = None,
    llm_call: Optional[LLMCall] = None,
    use_llm: bool = True,
) -> ParseResult:
    """Разбирает текст пользователя в TripRequest.

    text        — сообщение пользователя.
    today       — «сегодняшняя» дата (для тестов можно задать вручную).
    previous    — прежний запрос, если пользователь отвечает на уточняющий вопрос.
    asked_field — поле, о котором мы спрашивали (помогает понять короткий ответ «Москва»).
    llm_call    — функция вызова модели (для тестов подставляется заглушка).
    use_llm     — False отключает модель, останутся только правила.
    """
    today = today or date.today()
    text = (text or "").strip()
    notes: list[str] = []

    if not text:
        request = previous or TripRequest()
        question = build_clarification_question(request) if previous else EMPTY_REQUEST_QUESTION
        return ParseResult(
            request=request,
            question=question,
            asked_field=next_missing_field(request),
            notes=["Пустое сообщение: нечего разбирать."],
        )

    rules_fields = rules_extract(text, today, asked_field)

    if not use_llm:
        llm_call = None  # use_llm=False полностью отключает модель
    elif llm_call is None and llm.is_llm_configured():
        llm_call = llm.ask_llm_json

    llm_fields: Optional[dict] = None
    if llm_call is not None:
        try:
            data = llm_call(build_system_prompt(today), build_user_message(text, asked_field))
            if not isinstance(data, dict):
                raise llm.LLMError("модель вернула не JSON-объект")
            llm_fields = _clean_llm_fields(data)
        except llm.LLMError as error:
            notes.append(f"Языковая модель недоступна ({error}). Запрос разобран по правилам.")
    elif use_llm:
        notes.append("Языковая модель не настроена, запрос разобран по правилам.")

    if llm_fields is not None:
        fields = _fill_gaps(llm_fields, rules_fields)
    else:
        fields = rules_fields

    request = _build_request(fields, text, notes)
    if previous is not None:
        request = merge_requests(previous, request, notes)

    return ParseResult(
        request=request,
        question=build_clarification_question(request),
        asked_field=next_missing_field(request),
        used_llm=llm_fields is not None,
        notes=notes,
    )
