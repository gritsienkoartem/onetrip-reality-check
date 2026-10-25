"""Streamlit-интерфейс OneTrip Reality Check.

Запуск: streamlit run app.py
"""
from __future__ import annotations

import html
import os
from datetime import date, timedelta
from pathlib import Path

import streamlit as st

import llm
from agent.corrector import (
    CorrectionResult,
    build_corrected_plan,
    pair_from_option,
    pair_from_plan,
)
from agent.parser import parse_trip_request
from agent.place_resolver import AmbiguousPlace, resolve_request_places
from agent.ranker import unmatched_hotel_preferences
from agent.report import (
    baggage_label,
    format_date_ru,
    format_duration,
    format_money,
    format_time_value,
    plan_to_markdown,
    price_kind_label,
    risk_label,
)
from config import ConfigError, load_assumptions
from models import Severity, TripRequest
from providers.base import ProviderError
from providers.geo import GeoClient

SECRET_KEYS = (
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_API_KEY",
    "LLM_TIMEOUT_SECONDS",
    "TRAVELPAYOUTS_TOKEN",
    "TRAVEL_SEARCH",
    "DATA_PROVIDER",
)


def _apply_streamlit_secrets() -> None:
    """Переносит настройки Streamlit Cloud в переменные окружения."""
    try:
        secrets = st.secrets
        for key in SECRET_KEYS:
            if key in secrets and not os.getenv(key, "").strip():
                os.environ[key] = str(secrets[key])
    except Exception:
        return


_apply_streamlit_secrets()

EXAMPLE = (
    "Хочу поехать в Казань на 4 дня в середине ноября. Бюджет — 60 000 рублей. "
    "Люблю архитектуру и кофе, не люблю толпы"
)

DESTINATIONS_DIR = Path(__file__).resolve().parent / "assets" / "destinations"

DESTINATION_CARDS = [
    {
        "city": "Москва",
        "note": "Круглый год · музеи, метро и гастрономия",
        "image": DESTINATIONS_DIR / "moscow.jpg",
        "page": "https://ru.wikipedia.org/wiki/Красная_площадь",
    },
    {
        "city": "Санкт-Петербург",
        "note": "Май–сентябрь · каналы, дворцы и белые ночи",
        "image": DESTINATIONS_DIR / "spb.jpg",
        "page": "https://ru.wikipedia.org/wiki/Санкт-Петербург",
    },
    {
        "city": "Казань",
        "note": "Май–сентябрь · Кремль, набережная и кухня",
        "image": DESTINATIONS_DIR / "kazan.jpg",
        "page": "https://ru.wikipedia.org/wiki/Казань",
    },
    {
        "city": "Сочи",
        "note": "Круглый год · море летом, горы зимой",
        "image": DESTINATIONS_DIR / "sochi.jpg",
        "page": "https://ru.wikipedia.org/wiki/Сочи",
    },
    {
        "city": "Екатеринбург",
        "note": "Май–сентябрь · конструктивизм и Урал",
        "image": DESTINATIONS_DIR / "ekaterinburg.jpg",
        "page": "https://ru.wikipedia.org/wiki/Екатеринбург",
    },
]

WIZARD_STEP_TITLES = {
    1: "Маршрут",
    2: "Даты",
    3: "Путешественники",
    4: "Бюджет и стиль",
    5: "Проверка",
}
WIZARD_INTERESTS = {
    "архитектура": "Архитектура",
    "кофе": "Кофе и кафе",
    "музеи": "Музеи",
    "история": "История",
    "еда": "Еда и рынки",
    "парки": "Парки",
    "природа": "Природа",
    "пляж": "Пляжи",
    "шопинг": "Шопинг",
    "ночная жизнь": "Ночная жизнь",
}
WIZARD_AVOID = {
    "толпы": "Толпы",
    "шум": "Шум",
    "долгие прогулки": "Долгие прогулки",
    "ночная жизнь": "Ночная жизнь",
    "шопинг": "Шопинг",
}
TRANSPORT_OPTIONS = {
    "any": "Любой доступный",
    "flight": "Самолёт",
    "train": "Поезд",
}
TRANSPORT_PREFERENCES = {
    "any": [],
    "flight": ["самолёт"],
    "train": ["поезд"],
}
ACCOMMODATION_OPTIONS = {
    "any": "Без предпочтений",
    "budget": "Экономнее",
    "comfort": "Комфортнее",
}
ACCOMMODATION_PREFERENCES = {
    "any": [],
    "budget": ["хостел"],
    "comfort": ["в центре", "рядом с метро"],
}
ACCOMMODATION_FORMATS = {
    "hotel": "Отель",
    "guest_house": "Гостевой дом",
    "hostel": "Хостел",
    "apartment": "Апартаменты",
    "camping": "Кемпинг / глэмпинг",
    "unusual": "Необычное жильё",
}
ACCOMMODATION_FORMAT_PREFERENCES = {
    "hotel": "отель",
    "guest_house": "гостевой дом",
    "hostel": "хостел",
    "apartment": "апартаменты",
    "camping": "кемпинг",
    "unusual": "необычное жильё",
}
TRIP_STYLES = {
    "calm": "Спокойный",
    "balanced": "Сбалансированный",
    "intense": "Насыщенный",
}
CURRENCIES = ["RUB", "USD", "EUR"]

SEVERITY_STYLE = {
    Severity.HIGH: "error",
    Severity.MEDIUM: "warning",
    Severity.LOW: "info",
}

MODE_LABELS = {"flight": "Самолёт", "train": "Поезд", "bus": "Автобус"}
TRANSPORT_NOUNS = {"flight": "Рейс", "train": "Поезд", "bus": "Автобус"}
ACCESS_MODE_LABELS = {
    "walk": "пешком",
    "taxi": "такси",
    "public_transport": "общественный транспорт",
    "bus": "автобус",
    "transfer": "трансфер",
}
SOURCE_KIND_LABELS = {
    "real": "кэш или предложение сервиса",
    "estimate": "оценка проекта",
    "mock": "тестовые данные",
}

WIZARD_FIELDS = (
    "origin",
    "destination",
    "dates",
    "adults",
    "children",
    "budget",
    "currency",
    "scope",
    "transport",
    "accommodation",
    "accommodation_formats",
    "style",
    "interests",
    "avoid",
    "notes",
)

_CSS = """<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

:root {
    --ot-bg: #F8FAFC;
    --ot-surface: #FFFFFF;
    --ot-surface-2: #F1F5F9;
    --ot-ink: #0F172A;
    --ot-text: #334155;
    --ot-muted: #64748B;
    --ot-accent: #2563EB;
    --ot-accent-hover: #1D4ED8;
    --ot-accent-soft: rgba(37, 99, 235, 0.08);
    --ot-teal: #14B8A6;
    --ot-teal-ink: #0F766E;
    --ot-teal-soft: rgba(20, 184, 166, 0.12);
    --ot-border: #E2E8F0;
    --ot-border-strong: rgba(37, 99, 235, 0.38);
    --ot-warn-ink: #B45309;
    --ot-warn-soft: rgba(245, 158, 11, 0.14);
    --ot-shadow: 0 12px 32px rgba(15, 23, 42, 0.08);
    --ot-shadow-lg: 0 24px 60px rgba(15, 23, 42, 0.12);
    --ot-serif: "Inter", "Segoe UI", system-ui, -apple-system, sans-serif;
    --ot-sans: "Inter", "Segoe UI", system-ui, -apple-system, sans-serif;
}

html, body, .stApp {color-scheme: light;}
.stApp {
    background:
        radial-gradient(900px 480px at 88% -8%, rgba(37, 99, 235, 0.07), transparent 62%),
        radial-gradient(700px 420px at 4% 6%, rgba(20, 184, 166, 0.06), transparent 60%),
        var(--ot-bg);
    background-attachment: fixed;
    color: var(--ot-ink);
    font-family: var(--ot-sans);
}
header[data-testid="stHeader"] {
    background: transparent;
    border-bottom: none;
}
[data-testid="stToolbar"], [data-testid="stDecoration"] {display: none;}
.block-container {
    padding-top: 1.4rem;
    max-width: 1120px;
    position: relative;
    z-index: 1;
}
html, body, [data-testid="stMain"] {scroll-behavior: smooth;}
.ot-anchor {scroll-margin-top: 1.1rem;}

/* ---------- Типографика ---------- */
.stApp h1, .stApp h2, .stApp h3, .stApp h4,
.stApp .stMarkdown h1, .stApp .stMarkdown h2,
.stApp .stMarkdown h3, .stApp .stMarkdown h4,
h1, h2, h3, h4 {color: var(--ot-ink); font-family: var(--ot-serif); font-weight: 700; letter-spacing: -0.02em;}
.stApp h1 {font-size: clamp(2.2rem, 4.2vw, 3.4rem); line-height: 1.06; margin-bottom: 0.35rem;}
.stApp h2 {font-size: clamp(1.5rem, 2.4vw, 1.95rem);}
.stApp h3 {font-size: 1.4rem;}
.stApp h4, .stApp .stMarkdown h4 {font-size: 1.25rem; line-height: 1.25; padding-bottom: 0.35rem;}
p, li, label, .stMarkdown {color: var(--ot-text); font-family: var(--ot-sans);}
.stApp a, .stApp .stMarkdown a {color: var(--ot-accent); text-decoration-color: rgba(37, 99, 235, 0.4);}
.stApp a:hover, .stApp .stMarkdown a:hover {color: var(--ot-accent-hover);}
::selection {background: rgba(37, 99, 235, 0.2); color: var(--ot-ink);}
::-webkit-scrollbar {width: 10px; height: 10px;}
::-webkit-scrollbar-track {background: var(--ot-surface-2);}
::-webkit-scrollbar-thumb {
    background: #CBD5E1;
    border-radius: 8px;
    border: 2px solid var(--ot-surface-2);
}
::-webkit-scrollbar-thumb:hover {background: #94A3B8;}

div[data-testid="stCaptionContainer"] p, .stCaption {color: var(--ot-muted);}

/* ---------- Верхняя панель ---------- */
div[class*="st-key-ot_topbar"] {
    padding-bottom: 0.6rem;
    border-bottom: 1px solid var(--ot-border);
    margin-bottom: 0.9rem;
}
.ot-brand {display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap;}
.ot-logo {
    font-family: var(--ot-serif);
    font-size: 1.55rem;
    font-weight: 800;
    letter-spacing: -0.03em;
    color: var(--ot-ink);
    line-height: 1;
}
.ot-logo .ot-dot {color: var(--ot-teal);}
.ot-brand-note {
    color: var(--ot-muted);
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0.16em;
    text-transform: uppercase;
}
.ot-nav {
    display: flex;
    justify-content: flex-end;
    align-items: center;
    gap: 24px;
    flex-wrap: wrap;
}
.ot-nav a {
    position: relative;
    color: #475569 !important;
    font-size: 0.86rem;
    font-weight: 500;
    text-decoration: none !important;
    padding-bottom: 3px;
    transition: color 0.18s ease;
}
.ot-nav a::after {
    content: "";
    position: absolute;
    left: 0;
    right: 100%;
    bottom: 0;
    height: 2px;
    border-radius: 2px;
    background: var(--ot-accent);
    transition: right 0.22s ease;
}
.ot-nav a:hover {color: var(--ot-accent) !important;}
.ot-nav a:hover::after {right: 0;}
div[class*="st-key-ot_topbar"] .stButton > button {
    min-height: 36px;
    padding: 0.25rem 1rem;
    font-size: 0.84rem;
    border-radius: 999px;
}

/* ---------- Первый экран: иллюстрированная сцена ---------- */
div[class*="st-key-ot_hero"] {
    position: relative;
    margin: 0 0 1rem;
    padding: clamp(2.2rem, 4.5vw, 3.6rem) clamp(1.2rem, 3vw, 2.6rem) clamp(6rem, 10vw, 8.6rem);
    text-align: center;
    background:
        linear-gradient(180deg, rgba(15, 23, 42, 0.46) 0%, rgba(15, 23, 42, 0.16) 52%, rgba(15, 23, 42, 0) 100%),
        url("/app/static/hero_scene.svg") center bottom / cover no-repeat,
        #3B6FD8;
    border-radius: 28px;
    box-shadow: var(--ot-shadow-lg);
    overflow: hidden;
    isolation: isolate;
    animation: ot-fade 0.7s ease both;
}
div[class*="st-key-ot_hero"]::after,
.ot-flight {display: none;}
div[class*="st-key-ot_hero"] .ot-kicker {
    display: inline-block;
    color: #FFFFFF;
    background: rgba(15, 23, 42, 0.32);
    border: 1px solid rgba(255, 255, 255, 0.3);
    border-radius: 999px;
    padding: 4px 14px;
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    margin-bottom: 0.9rem;
    backdrop-filter: blur(6px);
}
div[class*="st-key-ot_hero"] h1 {
    color: #FFFFFF;
    font-family: var(--ot-serif);
    font-weight: 800;
    font-size: clamp(2.1rem, 4.2vw, 3.5rem);
    line-height: 1.06;
    letter-spacing: -0.03em;
    max-width: 18ch;
    margin: 0 auto 0.6rem;
    text-wrap: balance;
    text-shadow: 0 6px 30px rgba(15, 23, 42, 0.5);
}
.ot-hero-sub {
    max-width: 50ch;
    margin: 0 auto;
    color: #FFFFFF;
    font-size: 1.05rem;
    line-height: 1.55;
    text-shadow: 0 2px 16px rgba(15, 23, 42, 0.6);
}
.ot-hero-actions {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    flex-wrap: wrap;
    margin-top: 1.2rem;
}
.stApp .stMarkdown a.ot-cta, .stApp a.ot-cta {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    background: var(--ot-accent);
    color: #FFFFFF !important;
    font-weight: 600;
    font-size: 0.98rem;
    padding: 0.8rem 1.7rem;
    border-radius: 999px;
    text-decoration: none !important;
    box-shadow: 0 14px 34px rgba(37, 99, 235, 0.45);
    transition: background 0.18s ease, transform 0.18s ease, box-shadow 0.18s ease;
}
.stApp a.ot-cta span {transition: transform 0.18s ease;}
.stApp a.ot-cta:hover {
    background: var(--ot-accent-hover);
    transform: translateY(-2px);
    box-shadow: 0 18px 44px rgba(37, 99, 235, 0.55);
}
.stApp a.ot-cta:hover span {transform: translateY(3px);}
.stApp .stMarkdown a.ot-cta-ghost, .stApp a.ot-cta-ghost {
    background: rgba(255, 255, 255, 0.16);
    color: #FFFFFF !important;
    border: 1px solid rgba(255, 255, 255, 0.55);
    box-shadow: none;
    backdrop-filter: blur(6px);
}
.stApp .stMarkdown a.ot-cta-ghost:hover, .stApp a.ot-cta-ghost:hover {
    background: rgba(255, 255, 255, 0.28);
    border-color: #FFFFFF;
    box-shadow: none;
}
.ot-hero-actions .ot-trust {margin: 0;}
div[class*="st-key-ot_hero"] .ot-trust {margin-top: 1rem; justify-content: center;}

/* Форма планирования «парит» над нижним краем сцены */
div[class*="st-key-wizard_card"] {
    margin-top: calc(-1 * clamp(3.4rem, 6.4vw, 5.6rem));
    z-index: 3;
}

/* ---------- Чипы и бейджи ---------- */
.ot-trust {display: flex; flex-wrap: wrap; gap: 7px;}
.ot-trust span {
    background: rgba(15, 23, 42, 0.4);
    border: 1px solid rgba(255, 255, 255, 0.3);
    border-radius: 999px;
    padding: 4px 12px;
    color: #FFFFFF;
    font-size: 0.74rem;
    backdrop-filter: blur(6px);
    transition: border-color 0.2s ease, color 0.2s ease;
}
.ot-trust span:hover {border-color: #93C5FD; color: #FFFFFF;}

.ot-badge {
    display: inline-block;
    padding: 3px 10px;
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 600;
    background: var(--ot-teal-soft);
    color: var(--ot-teal-ink);
    margin-right: 6px;
}
.ot-badge.mock {background: var(--ot-warn-soft); color: var(--ot-warn-ink);}
.ot-badge.estimate {background: #E2E8F0; color: #334155;}
.ot-badge.real {background: var(--ot-teal-soft); color: var(--ot-teal-ink);}
.ot-muted {color: var(--ot-muted); font-size: 0.88rem;}

/* ---------- Раскрывающийся блок о проверках ---------- */
.ot-reveal {
    background: linear-gradient(120deg, #EFF6FF, #F0FDFA);
    border: 1px solid #DBEAFE;
    border-radius: 16px;
    margin: 0 0 1rem;
    overflow: hidden;
    animation: ot-rise 0.5s ease both;
}
.ot-reveal summary {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 16px;
    cursor: pointer;
    list-style: none;
    transition: background 0.2s ease;
}
.ot-reveal summary::-webkit-details-marker {display: none;}
.ot-reveal summary:hover {background: rgba(37, 99, 235, 0.05);}
.ot-reveal-badge {
    flex: none;
    color: var(--ot-accent);
    border: 1px solid rgba(37, 99, 235, 0.35);
    background: #FFFFFF;
    border-radius: 999px;
    padding: 3px 10px;
    font-size: 0.66rem;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
}
.ot-reveal-text {flex: 1; color: var(--ot-ink); font-size: 0.9rem; font-weight: 500;}
.ot-reveal-plus {
    flex: none;
    color: var(--ot-accent);
    font-size: 1.2rem;
    line-height: 1;
    transition: transform 0.25s ease;
}
.ot-reveal[open] .ot-reveal-plus {transform: rotate(45deg);}
.ot-reveal-body {padding: 0 16px 14px; color: var(--ot-text); font-size: 0.88rem; line-height: 1.55;}
.ot-reveal-body p {margin: 0; color: var(--ot-text);}
.ot-reveal-tags {display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px;}
.ot-reveal-tags span {
    background: #FFFFFF;
    border: 1px solid #BFDBFE;
    border-radius: 999px;
    padding: 2px 10px;
    font-size: 0.72rem;
    color: #1E40AF;
}

/* ---------- Секционные подписи ---------- */
.ot-section-kicker {
    color: var(--ot-accent);
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    margin: 0.4rem 0 0.2rem;
}

/* ---------- Карточки и панели ---------- */
.ot-card {
    background: var(--ot-surface);
    border: 1px solid var(--ot-border);
    border-radius: 18px;
    padding: 16px 18px;
    box-shadow: var(--ot-shadow);
}
div[class*="st-key-wizard_card"] {
    position: relative;
    background: var(--ot-surface);
    border: 1px solid var(--ot-border);
    border-radius: 24px;
    padding: 22px 26px 16px;
    box-shadow: var(--ot-shadow-lg);
    animation: ot-rise 0.5s ease both;
}
div[class*="st-key-wizard_card"]::before {
    content: "";
    position: absolute;
    top: 0;
    left: 28px;
    right: 28px;
    height: 3px;
    border-radius: 0 0 3px 3px;
    background: linear-gradient(90deg, var(--ot-accent), var(--ot-teal));
}
.ot-wizard-kicker {
    color: var(--ot-accent);
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    margin-bottom: 4px;
}
.ot-wizard-lead {
    color: var(--ot-ink);
    font-family: var(--ot-serif);
    font-size: 1.5rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    line-height: 1.2;
    margin: 0;
}
.ot-wizard-hint {color: var(--ot-muted); font-size: 0.9rem; margin: 4px 0 12px;}

/* ---------- Шаги мастера ---------- */
.ot-steps {
    display: flex;
    gap: 8px;
    margin: 2px 0 12px;
    overflow-x: auto;
    padding-bottom: 2px;
}
.ot-step-item {
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 5px 12px;
    border-radius: 999px;
    color: var(--ot-muted);
    font-size: 0.8rem;
    font-weight: 500;
    white-space: nowrap;
    border: 1px solid transparent;
    transition: color 0.2s ease, background 0.2s ease, border-color 0.2s ease;
}
.ot-step-item.done {
    color: var(--ot-teal-ink);
    background: var(--ot-teal-soft);
    border-color: rgba(20, 184, 166, 0.3);
}
.ot-step-item.active {
    color: #FFFFFF;
    font-weight: 700;
    background: var(--ot-accent);
    box-shadow: 0 8px 22px rgba(37, 99, 235, 0.28);
}
.ot-step-num {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    border-radius: 999px;
    border: 1px solid currentColor;
    font-size: 0.72rem;
    font-weight: 700;
}
.ot-step-item.active .ot-step-num {
    border-color: rgba(255, 255, 255, 0.7);
    animation: ot-pulse 2.4s ease-in-out infinite;
}
.ot-summary {display: flex; flex-wrap: wrap; gap: 8px; margin: 4px 0 16px;}
.ot-chip {
    display: inline-block;
    padding: 5px 12px;
    border-radius: 999px;
    background: var(--ot-surface-2);
    border: 1px solid var(--ot-border);
    color: #1E293B;
    font-size: 0.84rem;
    white-space: nowrap;
}

/* ---------- Карточки направлений ---------- */
.ot-dest-card {
    background: var(--ot-surface);
    border: 1px solid var(--ot-border);
    border-radius: 18px;
    overflow: hidden;
    box-shadow: var(--ot-shadow);
    margin-bottom: 8px;
}
.ot-dest-card a {color: inherit; text-decoration: none;}
.ot-dest-image {
    width: 100%;
    height: 116px;
    object-fit: cover;
    display: block;
    background: linear-gradient(135deg, #DBEAFE, #CCFBF1);
}
div[data-testid="stImage"] img {
    height: 132px;
    object-fit: cover;
    border-radius: 14px;
    border: 1px solid var(--ot-border);
    transition: transform 0.5s ease, filter 0.5s ease;
}
div[data-testid="stVerticalBlockBorderWrapper"]:hover div[data-testid="stImage"] img {
    transform: scale(1.04);
    filter: saturate(1.06);
}
.ot-dest-body {padding: 11px 13px 9px;}
.ot-dest-title {color: var(--ot-ink); font-weight: 600; font-size: 0.96rem;}
.ot-dest-note {color: var(--ot-muted); font-size: 0.78rem; line-height: 1.4;}
div[class*="st-key-dest_box_"] {
    border-radius: 18px;
    transition: background 0.22s ease, box-shadow 0.22s ease;
}
div[class*="st-key-dest_box_"]:hover {
    background: rgba(37, 99, 235, 0.04);
    box-shadow: 0 14px 34px rgba(15, 23, 42, 0.1);
}

/* ---------- Проверка и результаты ---------- */
.ot-review {
    background: var(--ot-bg);
    border: 1px solid var(--ot-border);
    border-radius: 14px;
    padding: 11px 14px;
    min-height: 3.4rem;
}
.ot-review span {display: block; color: var(--ot-muted); font-size: 0.74rem; letter-spacing: 0.03em; margin-bottom: 2px;}
.ot-review strong {color: var(--ot-ink); font-size: 0.95rem; font-weight: 600;}
.ot-result-summary {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 10px;
    margin: 8px 0 18px;
}
.ot-result-summary > div {
    background: var(--ot-surface);
    border: 1px solid var(--ot-border);
    border-radius: 14px;
    padding: 10px 14px;
    box-shadow: 0 4px 14px rgba(15, 23, 42, 0.05);
    animation: ot-rise 0.5s ease both;
}
.ot-result-summary span {display: block; color: var(--ot-muted); font-size: 0.74rem; margin-bottom: 2px;}
.ot-result-summary strong {color: var(--ot-ink); font-size: 0.96rem;}
.ot-source-note {
    background: #EFF6FF;
    border: 1px solid #BFDBFE;
    border-radius: 14px;
    padding: 12px 14px;
    color: #1E293B;
    font-size: 0.88rem;
    line-height: 1.5;
}

/* ---------- Сравнение сервисов и дорога до отеля ---------- */
.ot-price-board, .ot-transfer-board {
    display: grid;
    gap: 12px;
    margin: 8px 0 14px;
}
@media (min-width: 760px) {
    .ot-price-board {grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));}
}
.ot-price-card, .ot-transfer-card {
    background: var(--ot-surface);
    border: 1px solid var(--ot-border);
    border-radius: 16px;
    padding: 13px 15px;
    box-shadow: 0 6px 18px rgba(15, 23, 42, 0.05);
    animation: ot-rise 0.4s ease both;
}
.ot-price-head, .ot-transfer-head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: 4px 12px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--ot-border);
}
.ot-price-head span {
    color: var(--ot-muted);
    font-size: 0.76rem;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: uppercase;
}
.ot-price-head strong, .ot-transfer-head strong {color: var(--ot-ink); font-size: 1.02rem;}
.ot-price-best {color: var(--ot-teal-ink); font-size: 0.8rem; font-weight: 600; margin: 7px 0 2px;}
.ot-price-row {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 8px;
    padding: 8px 0;
    border-bottom: 1px dashed var(--ot-border);
}
.ot-price-row:last-child {border-bottom: none;}
.ot-price-service {color: var(--ot-ink); font-size: 0.9rem; font-weight: 600;}
.ot-price-row strong {margin-left: auto; color: var(--ot-ink); font-size: 0.92rem;}
.ot-price-kind {flex-basis: 100%; color: var(--ot-muted); font-size: 0.74rem;}
.ot-price-tag {
    display: inline-block;
    padding: 1px 8px;
    border-radius: 999px;
    background: var(--ot-teal-soft);
    color: var(--ot-teal-ink);
    font-size: 0.7rem;
    font-weight: 700;
}
.ot-price-gap {color: var(--ot-muted); font-size: 0.74rem;}
.ot-price-row a, .ot-transfer-links a {
    color: var(--ot-accent);
    font-size: 0.76rem;
    font-weight: 600;
    text-decoration: none;
}
.ot-price-row a:hover, .ot-transfer-links a:hover {text-decoration: underline;}
.ot-transfer-head span {color: var(--ot-muted); font-size: 0.78rem;}
.ot-transfer-row {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 10px;
    padding: 9px 0;
    border-bottom: 1px dashed var(--ot-border);
}
.ot-transfer-row:last-child {border-bottom: none;}
.ot-transfer-mode {color: var(--ot-ink); font-size: 0.9rem; font-weight: 600; min-width: 150px;}
.ot-transfer-time {color: var(--ot-muted); font-size: 0.78rem;}
.ot-transfer-row strong {margin-left: auto; color: var(--ot-ink); font-size: 0.92rem;}
.ot-transfer-links {flex-basis: 100%; font-size: 0.78rem;}
.ot-transfer-note {flex-basis: 100%; color: var(--ot-muted); font-size: 0.76rem; line-height: 1.45;}

/* ---------- Метрики ---------- */
div[data-testid="stMetric"] {
    background: var(--ot-surface);
    border: 1px solid var(--ot-border);
    border-radius: 18px;
    padding: 15px 17px;
    box-shadow: var(--ot-shadow);
    animation: ot-rise 0.45s ease both;
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
}
div[data-testid="stMetric"]:hover {
    border-color: var(--ot-border-strong);
    box-shadow: 0 18px 44px rgba(37, 99, 235, 0.12);
}
div[data-testid="stMetricLabel"] p, div[data-testid="stMetric"] label p {
    color: var(--ot-muted);
    font-size: 0.74rem;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    white-space: normal;
    overflow: visible;
    text-overflow: clip;
    line-height: 1.3;
}
div[data-testid="stMetricValue"] {color: var(--ot-ink); font-size: 1.5rem; font-weight: 700; letter-spacing: -0.02em;}

/* ---------- Вкладки ---------- */
.stTabs [role="tablist"] {
    display: flex;
    gap: 4px;
    width: fit-content;
    max-width: 100%;
    background: var(--ot-surface);
    padding: 6px;
    border-radius: 999px;
    border: 1px solid var(--ot-border);
    box-shadow: 0 4px 14px rgba(15, 23, 42, 0.05);
    overflow-x: auto;
    animation: ot-rise 0.45s ease both;
}
.stTabs [role="tab"] {
    border-radius: 999px;
    padding: 8px 18px;
    color: var(--ot-muted);
    white-space: nowrap;
    cursor: pointer;
    transition: color 0.18s ease, background 0.18s ease;
}
.stTabs [role="tab"] p, .stTabs [role="tab"] div {color: inherit;}
.stTabs [role="tab"]:hover {color: var(--ot-ink); background: var(--ot-accent-soft);}
.stTabs [role="tab"][aria-selected="true"] {
    background: var(--ot-accent);
    color: #FFFFFF;
    font-weight: 600;
}
.stTabs [role="tab"][aria-selected="true"] p,
.stTabs [role="tab"][aria-selected="true"] div {color: #FFFFFF;}
.stTabs .react-aria-SelectionIndicator {display: none;}

/* ---------- Кнопки ---------- */
button[data-testid="stBaseButton-secondary"], button[kind="secondary"] {
    background: var(--ot-surface);
    color: var(--ot-ink);
    border: 1px solid #CBD5E1;
    border-radius: 999px;
    transition: border-color 0.18s ease, color 0.18s ease, background 0.18s ease, transform 0.18s ease;
}
button[data-testid="stBaseButton-secondary"]:hover, button[kind="secondary"]:hover {
    border-color: var(--ot-accent);
    color: var(--ot-accent);
    background: var(--ot-accent-soft);
    transform: translateY(-1px);
}
button[data-testid="stBaseButton-primary"], button[kind="primary"], .stFormSubmitButton > button {
    background: var(--ot-accent);
    color: #FFFFFF;
    border: none;
    border-radius: 999px;
    box-shadow: 0 10px 26px rgba(37, 99, 235, 0.28);
    transition: background 0.18s ease, transform 0.18s ease, box-shadow 0.18s ease;
}
button[data-testid="stBaseButton-primary"] p, button[kind="primary"] p,
button[data-testid="stBaseButton-primary"] span, button[kind="primary"] span {
    color: #FFFFFF;
    font-weight: 600;
}
/* Подпись кнопки всегда наследует её цвет: иначе глобальный стиль p
   оставляет тёмный текст на синей заливке. */
.stApp button p, .stApp button span {color: inherit;}
button[data-testid="stBaseButton-primary"]:hover, button[kind="primary"]:hover,
.stFormSubmitButton > button:hover {
    background: var(--ot-accent-hover);
    color: #FFFFFF;
    transform: translateY(-1px);
    box-shadow: 0 14px 32px rgba(37, 99, 235, 0.36);
}
button[data-testid="stBaseButton-primary"]:disabled, button[kind="primary"]:disabled {
    background: #E2E8F0;
    color: #94A3B8;
    border: 1px dashed #CBD5E1;
    box-shadow: none;
}
div[data-testid="stDownloadButton"] > button {
    background: var(--ot-surface);
    color: var(--ot-accent);
    border: 1px solid rgba(37, 99, 235, 0.45);
    border-radius: 999px;
}
div[data-testid="stDownloadButton"] > button:hover {
    background: var(--ot-accent-soft);
    color: var(--ot-accent-hover);
}

/* ---------- Поля ввода ---------- */
input, textarea {color: var(--ot-ink); caret-color: var(--ot-accent);}
.stTextInput input, .stNumberInput input, .stDateInput input, .stTextArea textarea,
.stTextInput div[data-baseweb="input"], .stNumberInput div[data-baseweb="input"],
div[data-baseweb="input"], div[data-baseweb="base-input"],
div[data-baseweb="select"] > div, div[data-baseweb="textarea"] {
    background: #FFFFFF;
    border: 1px solid #CBD5E1;
    color: var(--ot-ink);
    border-radius: 12px;
}
.stTextInput input:hover, .stNumberInput input:hover, .stDateInput input:hover,
.stTextArea textarea:hover, div[data-baseweb="select"] > div:hover {
    border-color: rgba(37, 99, 235, 0.55);
}
.stTextInput div[data-baseweb="input"]:focus-within,
.stNumberInput div[data-baseweb="input"]:focus-within,
div[data-baseweb="input"]:focus-within,
div[data-baseweb="select"] > div:focus-within,
div[data-baseweb="textarea"]:focus-within {
    border-color: var(--ot-accent);
    box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.16);
}
input::placeholder, textarea::placeholder {color: #94A3B8; opacity: 1;}
div[data-baseweb="select"] svg, .stSelectbox svg, .stDateInput svg {fill: #64748B; color: #64748B;}
div[data-baseweb="popover"] div[role="listbox"], ul[role="listbox"] {
    background: #FFFFFF;
    border: 1px solid var(--ot-border);
    border-radius: 12px;
    box-shadow: 0 18px 44px rgba(15, 23, 42, 0.16);
}
li[role="option"], div[role="option"] {color: var(--ot-ink);}
li[role="option"]:hover, div[role="option"]:hover,
li[role="option"][aria-selected="true"], div[role="option"][aria-selected="true"] {
    background: rgba(37, 99, 235, 0.1);
    color: var(--ot-ink);
}
div[data-baseweb="calendar"] {
    background: #FFFFFF;
    color: var(--ot-ink);
    border: 1px solid var(--ot-border);
}
div[data-baseweb="calendar"] button {color: var(--ot-ink); background: transparent;}
div[data-baseweb="calendar"] button:hover {background: rgba(37, 99, 235, 0.1);}
div[data-baseweb="tag"] {
    background: #DBEAFE;
    color: #1E3A8A;
    border-radius: 8px;
}
div[data-baseweb="tag"] span {color: #1E3A8A;}
div[role="radiogroup"] label, div[role="radiogroup"] p {color: var(--ot-text);}
.stRadio div[role="radiogroup"] label:hover {color: var(--ot-ink);}
.stNumberInput button {
    background: #FFFFFF;
    color: var(--ot-ink);
    border-color: #CBD5E1;
}

/* ---------- Экспандеры и блоки ---------- */
div[data-testid="stExpander"] {
    background: var(--ot-surface);
    border: 1px solid var(--ot-border);
    border-radius: 14px;
    overflow: hidden;
}
div[data-testid="stExpander"] summary {color: var(--ot-ink);}
div[data-testid="stExpander"] summary:hover {color: var(--ot-accent);}
div[data-testid="stVerticalBlockBorderWrapper"] {
    border-radius: 18px;
    background: var(--ot-surface);
    border: 1px solid var(--ot-border);
    box-shadow: 0 4px 14px rgba(15, 23, 42, 0.04);
    transition: transform 0.25s ease, border-color 0.25s ease, box-shadow 0.25s ease;
}
div[data-testid="stVerticalBlockBorderWrapper"]:hover {
    border-color: var(--ot-border-strong);
    box-shadow: 0 16px 38px rgba(37, 99, 235, 0.1);
    transform: translateY(-2px);
}
div[data-testid="stAlert"] {
    border-radius: 14px;
    border: none;
    background: transparent;
    padding: 0;
}
div[data-testid="stAlertContainer"] {
    border-radius: 14px;
    border: 1px solid rgba(20, 184, 166, 0.4);
    background: rgba(20, 184, 166, 0.08);
    color: var(--ot-ink);
}
div[data-testid="stAlertContainer"] p {color: var(--ot-ink);}
div[data-testid="stAlert"]:has([data-testid="stAlertContentWarning"]) div[data-testid="stAlertContainer"] {
    border-color: rgba(245, 158, 11, 0.5);
    background: rgba(245, 158, 11, 0.1);
}
div[data-testid="stAlert"]:has([data-testid="stAlertContentError"]) div[data-testid="stAlertContainer"] {
    border-color: rgba(239, 68, 68, 0.45);
    background: rgba(239, 68, 68, 0.08);
}
div[data-testid="stAlert"]:has([data-testid="stAlertContentInfo"]) div[data-testid="stAlertContainer"] {
    border-color: rgba(37, 99, 235, 0.35);
    background: rgba(37, 99, 235, 0.07);
}

/* ---------- Подвал ---------- */
div[class*="st-key-ot_footer"] {
    margin-top: 2.4rem;
    padding-top: 1.2rem;
    border-top: 1px solid var(--ot-border);
}
div[class*="st-key-ot_footer"] p, div[class*="st-key-ot_footer"] .stCaption {
    font-size: 0.78rem;
    color: var(--ot-muted);
}

/* ---------- Прогресс ---------- */
div[data-testid="stProgress"] > div:first-child {
    background: transparent;
    padding-left: 2px;
}
div[data-testid="stProgress"] > div:first-child > div {
    background: transparent;
    animation: none;
    box-shadow: none;
}
div[data-testid="stProgress"] > div[role="progressbar"] > div {
    background: #E2E8F0;
    border-radius: 999px;
    overflow: hidden;
}
div[data-testid="stProgress"] > div[role="progressbar"] > div > div {
    background: linear-gradient(90deg, #2563EB, #14B8A6, #2563EB);
    background-size: 200% 100%;
    border-radius: 999px;
    animation: ot-shimmer 2.8s linear infinite;
}
div[data-testid="stProgress"] p {
    color: var(--ot-text);
    font-size: 0.78rem;
    font-weight: 600;
    letter-spacing: 0.02em;
}

/* ---------- Кастомные абзацы ----------
   Контейнер markdown в Streamlit задаёт p {font-size: inherit} с большей
   специфичностью, поэтому размеры своих абзацев фиксируем явно. */
.stApp .stMarkdown p.ot-wizard-lead {font-size: 1.5rem; line-height: 1.2; color: var(--ot-ink);}
.stApp .stMarkdown p.ot-wizard-hint {font-size: 0.9rem;}
.stApp .stMarkdown p.ot-hero-sub {font-size: 1.05rem; line-height: 1.55; color: #FFFFFF;}
.stApp .stMarkdown .ot-reveal p {font-size: 0.88rem; line-height: 1.55;}

@keyframes ot-rise {
    from {opacity: 0; transform: translateY(10px);}
    to {opacity: 1; transform: translateY(0);}
}
@keyframes ot-fade {
    from {opacity: 0;}
    to {opacity: 1;}
}
@keyframes ot-shimmer {
    from {background-position: 0% 50%;}
    to {background-position: 200% 50%;}
}
@keyframes ot-fly {
    from {offset-distance: 0%;}
    to {offset-distance: 100%;}
}
@keyframes ot-pulse {
    0%, 100% {box-shadow: 0 0 0 0 rgba(255, 255, 255, 0.5);}
    50% {box-shadow: 0 0 0 5px rgba(255, 255, 255, 0);}
}

@media (max-width: 700px) {
    .block-container {padding-left: 0.8rem; padding-right: 0.8rem;}
    div[class*="st-key-ot_hero"] {
        margin: 0 0 0.9rem;
        padding: 1.6rem 1rem 4.6rem;
        border-radius: 22px;
    }
    div[class*="st-key-wizard_card"] {margin-top: -2.6rem;}
    div[class*="st-key-ot_hero"] h1 {font-size: 1.95rem;}
    .ot-hero-actions {margin-top: 0.9rem;}
    .stApp a.ot-cta {padding: 0.62rem 1.1rem; font-size: 0.9rem;}
    div[class*="st-key-wizard_card"] {padding: 18px 16px 12px; border-radius: 18px;}
    .ot-wizard-lead {font-size: 1.35rem;}
    .stApp .stMarkdown p.ot-wizard-lead {font-size: 1.35rem;}
    .ot-steps {flex-wrap: nowrap; overflow-x: auto; padding-bottom: 4px; scrollbar-width: none;}
    .ot-steps::-webkit-scrollbar {display: none;}
    .ot-step-item {font-size: 0.74rem; padding: 4px 10px; gap: 5px;}
    .ot-nav {justify-content: flex-start; gap: 14px;}
    .ot-nav a {font-size: 0.8rem;}
    .ot-nav a[href="#ot-tools"] {display: none;}
    .ot-reveal summary {align-items: flex-start; gap: 8px; padding: 10px 13px;}
    .ot-reveal-badge {display: none;}
    .ot-reveal-text {font-size: 0.84rem; line-height: 1.4;}
    div[class*="st-key-ot_topbar"] [data-testid="stHorizontalBlock"]:has(button) {
        flex-wrap: wrap;
        row-gap: 6px;
    }
    div[class*="st-key-ot_topbar"] [data-testid="stHorizontalBlock"]:has(button) [data-testid="stColumn"]:first-child {
        flex: 1 1 100% !important;
        min-width: 0 !important;
    }
    div[class*="st-key-ot_topbar"] [data-testid="stHorizontalBlock"]:has(button) [data-testid="stColumn"]:last-child {
        flex: 0 0 auto !important;
        width: auto !important;
        margin-left: auto;
    }
    div[class*="st-key-ot_topbar"] [data-testid="stHorizontalBlock"]:has(button) .stButton > button {
        width: auto;
        padding: 0.28rem 1rem;
    }
    div[class*="st-key-ot_topbar"] .ot-logo {font-size: 1.45rem;}
    .ot-steps {flex-wrap: wrap; overflow: visible;}
    div[data-testid="stImage"] img {height: 104px;}
    .ot-dest-image {height: 96px;}
    .ot-result-summary {grid-template-columns: 1fr 1fr;}
    .ot-price-head, .ot-transfer-head {flex-direction: column; gap: 2px;}
    .ot-transfer-mode {min-width: 0;}
    .stButton > button, .stDownloadButton > button {width: 100%;}
    .ot-brand {gap: 6px;}
    .ot-brand-note {font-size: 0.62rem;}
}

@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
        animation: none !important;
        transition: none !important;
    }
}
</style>
"""


def _badge(kind: str, label: str) -> str:
    css = kind if kind in {"mock", "estimate", "real"} else ""
    return f'<span class="ot-badge {css}">{label}</span>'


def _default_trip_dates() -> tuple[date, date]:
    """Ближайший удобный период: примерно три недели вперёд."""
    start = date.today() + timedelta(days=20)
    return start, start + timedelta(days=3)


def _travel_search_enabled() -> bool:
    """Включён ли сторонний сервис travel-search-ru."""
    return os.getenv("TRAVEL_SEARCH", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _format_budget(amount: float, currency: str) -> str:
    return f"{amount:,.0f} {currency}".replace(",", " ")


def _remember_wizard() -> None:
    """Копирует ответы виджетов в постоянные ключи.

    Streamlit удаляет состояние виджета, когда шаг перестаёт отображаться,
    поэтому ответы нельзя хранить только в ключах самих виджетов.
    """
    for name in WIZARD_FIELDS:
        widget_key = f"wizard_{name}_input"
        if widget_key in st.session_state:
            st.session_state[f"wizard_{name}"] = st.session_state[widget_key]
    st.session_state.wizard_error = None


def _restore_wizard(name: str) -> None:
    """Возвращает ответ в виджет при повторном открытии шага."""
    widget_key = f"wizard_{name}_input"
    if widget_key not in st.session_state:
        value = st.session_state.get(f"wizard_{name}")
        if value is None and name in {"origin", "destination"}:
            value = ""
        st.session_state[widget_key] = value


def _wizard_route() -> tuple[str, str]:
    origin = (st.session_state.get("wizard_origin") or "").strip()
    destination = (st.session_state.get("wizard_destination") or "").strip()
    return origin, destination


def _wizard_dates() -> tuple[date | None, date | None]:
    value = st.session_state.get("wizard_dates")
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return value[0], value[1]
    if isinstance(value, date):
        return value, None
    return None, None


def _wizard_days() -> int | None:
    start, end = _wizard_dates()
    if start is not None and end is not None and end >= start:
        return (end - start).days + 1
    return None


def _wizard_travelers() -> int:
    adults = int(st.session_state.get("wizard_adults") or 1)
    children = int(st.session_state.get("wizard_children") or 0)
    return max(1, adults) + max(0, children)


def _wizard_step_error(step: int) -> str | None:
    """Что мешает перейти к следующему шагу (или запустить поиск)."""
    origin, destination = _wizard_route()
    if step >= 1:
        if not origin or not destination:
            return "Выберите город отправления и город назначения."
        if origin.casefold() == destination.casefold():
            return "Города отправления и назначения должны различаться."
    if step >= 2:
        start, end = _wizard_dates()
        if start is None or end is None:
            return "Выберите дату начала и дату окончания поездки."
        if end < start:
            return "Дата окончания не может быть раньше даты начала."
        if (end - start).days + 1 > 30:
            return "Поездки длиннее 30 дней пока не поддерживаются."
    if step >= 3:
        if int(st.session_state.get("wizard_adults") or 0) < 1:
            return "Нужен хотя бы один взрослый путешественник."
    if step >= 4:
        budget = float(st.session_state.get("wizard_budget") or 0)
        if budget <= 0:
            return "Укажите бюджет больше нуля."
        if st.session_state.get("wizard_currency") not in CURRENCIES:
            return "Выберите валюту бюджета."
    return None


def _wizard_summary_html() -> str:
    origin, destination = _wizard_route()
    if not origin or not destination:
        return ""
    step = int(st.session_state.get("wizard_step", 1))
    chips: list[str] = [f"{html.escape(origin)} → {html.escape(destination)}"]
    start, end = _wizard_dates()
    days = _wizard_days()
    if step >= 2 and start is not None and end is not None:
        chips.append(f"{start:%d.%m.%Y} — {end:%d.%m.%Y}")
        if days is not None:
            chips.append(f"{days} дн. · {days - 1} ноч.")
    if step >= 3:
        adults = int(st.session_state.get("wizard_adults") or 1)
        children = int(st.session_state.get("wizard_children") or 0)
        travelers = f"{adults} взр." + (f" + {children} дет." if children else "")
        chips.append(f"Путешественники: {travelers}")
    if step >= 4:
        budget = float(st.session_state.get("wizard_budget") or 0)
        currency = st.session_state.get("wizard_currency") or "RUB"
        if budget > 0:
            scope = (
                "на человека"
                if st.session_state.get("wizard_scope") == "person"
                else "на всю группу"
            )
            chips.append(f"{_format_budget(budget, currency)} · {scope}")
        transport = TRANSPORT_OPTIONS.get(st.session_state.get("wizard_transport"), "")
        if transport and st.session_state.get("wizard_transport") != "any":
            chips.append(transport)
        selected = st.session_state.get("wizard_interests") or []
        interests = [
            WIZARD_INTERESTS[value]
            for value in selected
            if value in WIZARD_INTERESTS
        ]
        if interests:
            chips.append("Интересы: " + html.escape(", ".join(interests)))
    body = "".join(f'<span class="ot-chip">{chip}</span>' for chip in chips)
    return f'<div class="ot-summary">{body}</div>'


def _wizard_request_text() -> str:
    origin, destination = _wizard_route()
    start, end = _wizard_dates()
    budget = float(st.session_state.get("wizard_budget") or 0)
    currency = st.session_state.get("wizard_currency") or "RUB"
    parts = [f"Поездка {origin} → {destination}"]
    if start is not None and end is not None:
        parts.append(f"{start:%d.%m.%Y} — {end:%d.%m.%Y}")
    adults = int(st.session_state.get("wizard_adults") or 1)
    children = int(st.session_state.get("wizard_children") or 0)
    parts.append(f"взрослых {adults}, детей {children}")
    if budget > 0:
        scope = (
            "на человека"
            if st.session_state.get("wizard_scope") == "person"
            else "на всю группу"
        )
        parts.append(f"бюджет {_format_budget(budget, currency)} {scope}")
    selected = st.session_state.get("wizard_interests") or []
    interests = [WIZARD_INTERESTS[value] for value in selected if value in WIZARD_INTERESTS]
    if interests:
        parts.append("интересы: " + ", ".join(interests))
    format_keys = st.session_state.get("wizard_accommodation_formats") or []
    formats = [
        ACCOMMODATION_FORMATS[value].lower()
        for value in format_keys
        if value in ACCOMMODATION_FORMATS
    ]
    if formats:
        parts.append("жильё: " + ", ".join(formats))
    notes = (st.session_state.get("wizard_notes") or "").strip()
    if notes:
        parts.append("пожелания: " + notes)
    return "; ".join(parts) + "."


def _pick_wizard_city(city: str) -> None:
    st.session_state.wizard_destination = city
    st.session_state.wizard_destination_input = city
    st.session_state.wizard_error = None


def _clear_destination() -> None:
    st.session_state.wizard_destination = None
    st.session_state.wizard_destination_input = ""
    st.session_state.wizard_error = None


def _go_step(step: int) -> None:
    st.session_state.wizard_step = step
    st.session_state.wizard_error = None


def _load_wizard_from_request(request: TripRequest) -> None:
    """Переносит параметры готового плана в мастер для редактирования."""
    st.session_state.wizard_origin = request.origin_city
    st.session_state.wizard_destination = request.destination_city
    if request.start_date is not None and request.end_date is not None:
        st.session_state.wizard_dates = (request.start_date, request.end_date)
    else:
        st.session_state.wizard_dates = _default_trip_dates()
    st.session_state.wizard_adults = request.adults
    st.session_state.wizard_children = request.children
    st.session_state.wizard_budget = request.budget_amount or 60_000
    st.session_state.wizard_currency = request.currency
    st.session_state.wizard_scope = request.budget_scope

    preferences = " ".join(request.transport_preferences).casefold()
    if "самол" in preferences:
        transport = "flight"
    elif "поезд" in preferences or "жд" in preferences:
        transport = "train"
    else:
        transport = "any"
    st.session_state.wizard_transport = transport

    accommodation_text = " ".join(request.accommodation_preferences).casefold()
    if "хостел" in accommodation_text:
        accommodation = "budget"
    elif "центр" in accommodation_text or "метро" in accommodation_text:
        accommodation = "comfort"
    else:
        accommodation = "any"
    st.session_state.wizard_accommodation = accommodation

    format_keys: list[str] = []
    if "гостевой дом" in accommodation_text:
        format_keys.append("guest_house")
    if "кемпинг" in accommodation_text or "глэмпинг" in accommodation_text:
        format_keys.append("camping")
    if "необычное жильё" in accommodation_text:
        format_keys.append("unusual")
    if "апартамент" in accommodation_text:
        format_keys.append("apartment")
    if "хостел" in accommodation_text:
        format_keys.append("hostel")
    if "отель" in accommodation_text and "гостевой дом" not in accommodation_text:
        format_keys.append("hotel")
    st.session_state.wizard_accommodation_formats = format_keys

    st.session_state.wizard_style = request.trip_style
    st.session_state.wizard_interests = list(request.interests)
    st.session_state.wizard_avoid = list(request.avoid)
    marker = "пожелания: "
    if marker in request.raw_text:
        notes = request.raw_text.split(marker, 1)[1].rstrip(".").strip()
    else:
        notes = st.session_state.get("wizard_notes") or ""
    st.session_state.wizard_notes = notes
    for name in WIZARD_FIELDS:
        st.session_state.pop(f"wizard_{name}_input", None)
    st.session_state.wizard_step = 5
    st.session_state.wizard_error = None
    st.session_state.show_results = False


def _edit_current_request() -> None:
    result = st.session_state.get("correction_result")
    if result is not None:
        _load_wizard_from_request(result.plan.request)


def _clear_result() -> None:
    st.session_state.update(
        correction_result=None,
        pending_request=None,
        pending_question=None,
        pending_field=None,
        pending_place=None,
        pending_place_request=None,
        app_error=None,
        parse_notes=[],
        wizard_error=None,
        show_results=False,
    )


def _reset_new_request() -> None:
    for name in WIZARD_FIELDS:
        st.session_state.pop(f"wizard_{name}_input", None)
    st.session_state.wizard_step = 1
    st.session_state.wizard_origin = None
    st.session_state.wizard_destination = None
    st.session_state.wizard_dates = _default_trip_dates()
    st.session_state.wizard_adults = 1
    st.session_state.wizard_children = 0
    st.session_state.wizard_budget = 60_000
    st.session_state.wizard_currency = "RUB"
    st.session_state.wizard_scope = "group"
    st.session_state.wizard_transport = "any"
    st.session_state.wizard_accommodation = "any"
    st.session_state.wizard_accommodation_formats = []
    st.session_state.wizard_style = "balanced"
    st.session_state.wizard_interests = []
    st.session_state.wizard_avoid = []
    st.session_state.wizard_notes = ""
    st.session_state.wizard_error = None
    st.session_state.show_results = False
    st.session_state.correction_result = None
    st.session_state.pending_place = None
    st.session_state.pending_place_request = None
    st.session_state.pending_question = None
    st.session_state.pending_request = None
    st.session_state.parse_notes = []
    st.session_state.app_error = None


def _submit_wizard() -> None:
    error = _wizard_step_error(5)
    if error:
        st.session_state.wizard_error = error
        return
    origin, destination = _wizard_route()
    start, end = _wizard_dates()
    budget = float(st.session_state.get("wizard_budget") or 0)
    interest_keys = list(st.session_state.get("wizard_interests") or [])
    avoid_keys = list(st.session_state.get("wizard_avoid") or [])
    transport = st.session_state.get("wizard_transport") or "any"
    accommodation = st.session_state.get("wizard_accommodation") or "any"
    format_keys = list(st.session_state.get("wizard_accommodation_formats") or [])
    accommodation_preferences = list(ACCOMMODATION_PREFERENCES.get(accommodation, []))
    accommodation_preferences += [
        ACCOMMODATION_FORMAT_PREFERENCES[key]
        for key in format_keys
        if key in ACCOMMODATION_FORMAT_PREFERENCES
    ]
    accommodation_preferences = list(dict.fromkeys(accommodation_preferences))
    request = TripRequest(
        raw_text=_wizard_request_text(),
        origin_city=origin,
        destination_city=destination,
        start_date=start,
        end_date=end,
        budget_amount=budget or None,
        currency=st.session_state.get("wizard_currency") or "RUB",
        budget_scope=st.session_state.get("wizard_scope") or "group",
        adults=int(st.session_state.get("wizard_adults") or 1),
        children=int(st.session_state.get("wizard_children") or 0),
        interests=interest_keys,
        avoid=avoid_keys,
        transport_preferences=list(TRANSPORT_PREFERENCES.get(transport, [])),
        accommodation_preferences=accommodation_preferences,
        trip_style=st.session_state.get("wizard_style") or "balanced",
    )
    st.session_state.wizard_error = None
    st.session_state.parse_notes = [
        "Запрос собран в мастере: маршрут, даты, путешественники, бюджет, "
        "предпочтения и интересы взяты из формы."
    ]
    st.session_state.pending_question = None
    st.session_state.pending_field = None
    st.session_state.pending_request = None
    _finalize_request(request)


def _wizard_rail(step: int) -> str:
    items = []
    for number in range(1, 6):
        state = "active" if number == step else ("done" if number < step else "")
        items.append(
            f'<div class="ot-step-item {state}">'
            f'<span class="ot-step-num">{number}</span>'
            f"<span>{WIZARD_STEP_TITLES[number]}</span></div>"
        )
    return f'<div class="ot-steps">{"".join(items)}</div>'


def _render_destination_card(column, card: dict) -> None:
    with column, st.container(border=True, key=f"dest_box_{card['city']}"):
        image = card["image"]
        if image.is_file():
            st.image(str(image), use_container_width=True)
        else:
            st.markdown(
                '<div class="ot-dest-image"></div>',
                unsafe_allow_html=True,
            )
        st.markdown(f"**{html.escape(card['city'])}**")
        st.caption(card["note"])
        st.markdown(f"[О городе]({card['page']})")
        st.button(
            f"Выбрать {card['city']}",
            key=f"destination_card_{card['city']}",
            on_click=_pick_wizard_city,
            args=(card["city"],),
            use_container_width=True,
        )


def _render_destination_cards() -> None:
    st.markdown(
        '<div id="ot-destinations" class="ot-anchor"></div>',
        unsafe_allow_html=True,
    )
    st.markdown("#### Куда поехать в России")
    st.caption(
        "Пять городов для короткой поездки. Поезд или самолёт — сравните "
        "оба варианта в плане."
    )
    for row_start in range(0, len(DESTINATION_CARDS), 3):
        columns = st.columns(3)
        for column, card in zip(columns, DESTINATION_CARDS[row_start:row_start + 3]):
            _render_destination_card(column, card)


def _render_wizard() -> None:
    step = int(st.session_state.get("wizard_step", 1))
    with st.container(key="wizard_card"):
        st.markdown(
            '<div class="ot-wizard-kicker">Планировщик поездки</div>'
            '<p class="ot-wizard-lead">Соберём поездку за пять шагов</p>'
            '<p class="ot-wizard-hint">Маршрут, даты, путешественники, бюджет и '
            "предпочтения. Затем агент найдёт варианты и проверит план.</p>",
            unsafe_allow_html=True,
        )
        st.markdown(_wizard_rail(step), unsafe_allow_html=True)
        st.progress(step / 5, text=f"Шаг {step} из 5 · {WIZARD_STEP_TITLES[step]}")
        summary = _wizard_summary_html()
        if summary:
            st.markdown(summary, unsafe_allow_html=True)

        if step == 1:
            _restore_wizard("origin")
            _restore_wizard("destination")
            left, right = st.columns(2)
            with left:
                st.text_input(
                    "Откуда",
                    placeholder="Например, Москва",
                    key="wizard_origin_input",
                    on_change=_remember_wizard,
                )
            with right:
                st.text_input(
                    "Куда",
                    placeholder="Например, Казань",
                    key="wizard_destination_input",
                    on_change=_remember_wizard,
                )
            if st.session_state.wizard_destination:
                st.caption(
                    "Выбранное направление можно убрать, чтобы выбрать другое."
                )
                st.button(
                    "Убрать направление",
                    key="wizard_clear_destination",
                    on_click=_clear_destination,
                )
            st.caption(
                "Можно ввести любой город России. Пока строим маршрут с одним "
                "городом назначения: поездка в одну сторону и несколько городов "
                "движком пока не поддерживаются."
            )
            _render_destination_cards()

        elif step == 2:
            _restore_wizard("dates")
            st.date_input(
                "Даты поездки",
                key="wizard_dates_input",
                min_value=date.today(),
                max_value=date.today() + timedelta(days=365),
                format="DD/MM/YYYY",
                on_change=_remember_wizard,
            )
            days = _wizard_days()
            if days is not None:
                st.caption(f"{days} дн. · {days - 1} ноч. · поездки до 30 дней")
            else:
                st.caption("Выберите обе даты. Поездки до 30 дней.")
            st.caption(
                "Гибкие даты и «плюс-минус несколько дней» пока не поддерживаются: "
                "поиску нужны точные даты, чтобы не показывать вымышленные варианты."
            )

        elif step == 3:
            _restore_wizard("adults")
            _restore_wizard("children")
            left, right = st.columns(2)
            with left:
                st.number_input(
                    "Взрослые",
                    min_value=1,
                    max_value=9,
                    step=1,
                    key="wizard_adults_input",
                    on_change=_remember_wizard,
                )
            with right:
                st.number_input(
                    "Дети",
                    min_value=0,
                    max_value=9,
                    step=1,
                    key="wizard_children_input",
                    on_change=_remember_wizard,
                )
            st.caption(
                f"Всего путешественников: {_wizard_travelers()}. "
                "Взрослых передаём в поиск отелей. Возраст детей и детские тарифы "
                "источники пока не принимают, поэтому цены не умножаются на число людей."
            )

        elif step == 4:
            for name in (
                "budget",
                "currency",
                "scope",
                "transport",
                "accommodation",
                "accommodation_formats",
                "style",
                "interests",
                "avoid",
                "notes",
            ):
                _restore_wizard(name)
            currency = st.session_state.get("wizard_currency") or "RUB"
            left, right = st.columns([2, 1])
            with left:
                st.number_input(
                    f"Бюджет поездки, {currency}",
                    min_value=1_000,
                    max_value=10_000_000,
                    step=1_000,
                    key="wizard_budget_input",
                    on_change=_remember_wizard,
                )
            with right:
                st.selectbox(
                    "Валюта",
                    CURRENCIES,
                    key="wizard_currency_input",
                    on_change=_remember_wizard,
                )
            st.radio(
                "Бюджет указан",
                ["group", "person"],
                format_func=lambda value: (
                    "На всю группу" if value == "group" else "На одного путешественника"
                ),
                horizontal=True,
                key="wizard_scope_input",
                on_change=_remember_wizard,
            )
            st.radio(
                "Транспорт",
                list(TRANSPORT_OPTIONS),
                format_func=lambda value: TRANSPORT_OPTIONS[value],
                horizontal=True,
                key="wizard_transport_input",
                on_change=_remember_wizard,
            )
            st.caption(
                "Для поездок по России на 300–1200 км сравните поезд и самолёт: "
                "поезд обычно приходит в центр, но едет дольше. В плане будут "
                "оба варианта, если источник их вернул."
            )
            st.radio(
                "Проживание",
                list(ACCOMMODATION_OPTIONS),
                format_func=lambda value: ACCOMMODATION_OPTIONS[value],
                horizontal=True,
                key="wizard_accommodation_input",
                on_change=_remember_wizard,
            )
            st.caption(
                "«Экономнее» и «Комфортнее» — это пожелания к ранжированию отелей "
                "(хостел; центр и метро), а не фильтр, который скрывает варианты."
            )
            st.multiselect(
                "Формат жилья",
                options=list(ACCOMMODATION_FORMATS),
                format_func=lambda value: ACCOMMODATION_FORMATS[value],
                placeholder="Любой: отель, гостевой дом, кемпинг…",
                key="wizard_accommodation_formats_input",
                on_change=_remember_wizard,
            )
            st.caption(
                "Формат — пожелание к ранжированию, а не скрытый фильтр. Если "
                "источники не подтвердят формат, план честно об этом скажет. "
                "«Необычное жильё» определяется по типу и названию — это эвристика."
            )
            st.radio(
                "Темп поездки",
                list(TRIP_STYLES),
                format_func=lambda value: TRIP_STYLES[value],
                horizontal=True,
                key="wizard_style_input",
                on_change=_remember_wizard,
            )
            st.multiselect(
                "Интересы",
                options=list(WIZARD_INTERESTS),
                format_func=lambda value: WIZARD_INTERESTS[value],
                placeholder="Выберите или введите своё",
                key="wizard_interests_input",
                on_change=_remember_wizard,
            )
            st.multiselect(
                "Чего избегать",
                options=list(WIZARD_AVOID),
                format_func=lambda value: WIZARD_AVOID[value],
                placeholder="Можно оставить пустым",
                key="wizard_avoid_input",
                on_change=_remember_wizard,
            )
            st.text_area(
                "Расскажи, какой ты представляешь свою поездку",
                key="wizard_notes_input",
                height=90,
                placeholder=(
                    "Например: хочу гулять по историческому центру, пробовать "
                    "местную кухню и не спешить"
                ),
                on_change=_remember_wizard,
            )
            st.caption(
                "Интересы влияют на подбор мест, темп — на число мест в дне. "
                "Свободное описание сохраняется в запросе, но поисковые источники "
                "его не получают."
            )

        else:
            _render_wizard_review()

        if st.session_state.get("wizard_error"):
            st.warning(st.session_state.wizard_error)

        error = _wizard_step_error(step)
        if error and step < 5:
            st.caption(f"Чтобы продолжить: {error}")

        back_column, next_column, _ = st.columns([0.9, 2, 0.9])
        with back_column:
            if step > 1 and st.button(
                "Назад", key="wizard_back", use_container_width=True
            ):
                _go_step(step - 1)
                st.rerun()
        with next_column:
            if step < 5:
                if st.button(
                    "Продолжить",
                    key="wizard_next",
                    type="primary",
                    use_container_width=True,
                ):
                    issue = _wizard_step_error(step)
                    if issue:
                        st.session_state.wizard_error = issue
                        st.rerun()
                    _go_step(step + 1)
                    st.rerun()
            elif st.button(
                "Найти варианты и составить план",
                key="wizard_submit",
                type="primary",
                use_container_width=True,
            ):
                issue = _wizard_step_error(5)
                if issue:
                    st.session_state.wizard_error = issue
                    st.rerun()
                _submit_wizard()


def _render_wizard_review() -> None:
    origin, destination = _wizard_route()
    start, end = _wizard_dates()
    days = _wizard_days() or 0
    adults = int(st.session_state.get("wizard_adults") or 1)
    children = int(st.session_state.get("wizard_children") or 0)
    budget = float(st.session_state.get("wizard_budget") or 0)
    currency = st.session_state.get("wizard_currency") or "RUB"
    scope = "на человека" if st.session_state.get("wizard_scope") == "person" else "на всю группу"
    interest_keys = st.session_state.get("wizard_interests") or []
    interests = [WIZARD_INTERESTS[value] for value in interest_keys if value in WIZARD_INTERESTS]
    format_keys = st.session_state.get("wizard_accommodation_formats") or []
    formats = [ACCOMMODATION_FORMATS[key] for key in format_keys if key in ACCOMMODATION_FORMATS]
    sections = [
        (
            "Маршрут",
            f"{origin} → {destination}",
            1,
            "route",
        ),
        (
            "Даты",
            (
                f"{start:%d.%m.%Y} — {end:%d.%m.%Y} · {days} дн. / {days - 1} ноч."
                if start and end
                else "даты не выбраны"
            ),
            2,
            "dates",
        ),
        (
            "Путешественники",
            f"{adults} взр." + (f" + {children} дет." if children else "")
            + f" · всего {adults + children}",
            3,
            "travelers",
        ),
        (
            "Бюджет и стиль",
            (
                f"{_format_budget(budget, currency)} · {scope} · "
                f"{TRANSPORT_OPTIONS.get(st.session_state.get('wizard_transport'), 'любой транспорт')} · "
                f"{TRIP_STYLES.get(st.session_state.get('wizard_style'), 'сбалансированный')}"
                + (f" · {', '.join(interests)}" if interests else "")
                + (f" · жильё: {', '.join(formats)}" if formats else "")
            ),
            4,
            "preferences",
        ),
    ]
    for title, value, target_step, key in sections:
        columns = st.columns([4, 2])
        with columns[0]:
            st.markdown(
                f'<div class="ot-review"><span>{title}</span>'
                f"<strong>{html.escape(value)}</strong></div>",
                unsafe_allow_html=True,
            )
        with columns[1]:
            st.button(
                f"Изменить: {title.lower()}",
                key=f"review_edit_{key}",
                on_click=_go_step,
                args=(target_step,),
                use_container_width=True,
            )
    st.markdown(
        '<div class="ot-source-note">Поиск выполняется только после нажатия '
        "кнопки. Показываем доступные данные источников; кэш, оценки и "
        "ограничения подписаны в результатах.</div>",
        unsafe_allow_html=True,
    )


def _init_state() -> None:
    defaults = {
        "request_text": "",
        "pending_request": None,
        "pending_question": None,
        "pending_field": None,
        "pending_place": None,
        "pending_place_request": None,
        "parse_notes": [],
        "correction_result": None,
        "app_error": None,
        "clarify_round": 0,
        "show_results": False,
        "wizard_step": 1,
        "wizard_origin": None,
        "wizard_destination": None,
        "wizard_dates": _default_trip_dates(),
        "wizard_adults": 1,
        "wizard_children": 0,
        "wizard_budget": 60_000,
        "wizard_currency": "RUB",
        "wizard_scope": "group",
        "wizard_transport": "any",
        "wizard_accommodation": "any",
        "wizard_accommodation_formats": [],
        "wizard_style": "balanced",
        "wizard_interests": [],
        "wizard_avoid": [],
        "wizard_notes": "",
        "wizard_error": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


@st.cache_resource(show_spinner=False)
def _geo_client() -> GeoClient:
    """Один геокодер на сессию: он хранит кэш запросов к Nominatim."""
    return GeoClient()


@st.cache_data(show_spinner=False)
def _fx_rates() -> dict[str, float]:
    try:
        rates = load_assumptions().get("fx_to_rub", {}).get("rates", {})
    except ConfigError:
        return {"RUB": 1.0}
    return {str(key): float(value) for key, value in rates.items() if isinstance(value, (int, float))}


def _budget_in_rub(request: TripRequest) -> float | None:
    amount = request.budget_amount
    if amount is None:
        return None
    rate = _fx_rates().get(request.currency)
    if not rate:
        return None
    total = float(amount) * rate
    if request.budget_scope == "person":
        total *= max(1, request.travelers)
    return total


def _resolve_places(request, *, skip_place_fields=()):
    """Уточняет места по live-источникам; сбой сети не мешает плану."""
    try:
        return resolve_request_places(
            request,
            _geo_client(),
            skip_fields=skip_place_fields,
        )
    except ProviderError as error:
        st.session_state.parse_notes = list(st.session_state.parse_notes) + [
            f"Не удалось уточнить место в геокодере: {error}. "
            "Использую название из запроса."
        ]
        return request, None


def _finalize_request(
    request,
    *,
    skip_place_fields=(),
    hotel_choice=None,
    hotel_options=None,
) -> None:
    """Проверяет неоднозначные названия и собирает план с реальными статусами."""
    st.session_state.app_error = None
    try:
        with st.status("Проверяю параметры поездки", expanded=True) as status:
            status.update(label="Уточняю названия городов")
            request, ambiguous = _resolve_places(
                request, skip_place_fields=skip_place_fields
            )
            if ambiguous is not None:
                st.session_state.pending_place = ambiguous
                st.session_state.pending_place_request = request
                st.session_state.correction_result = None
                st.session_state.show_results = False
                status.update(
                    label="Нужно уточнить место",
                    state="complete",
                    expanded=False,
                )
                st.rerun()

            st.session_state.pending_place = None
            st.session_state.pending_place_request = None
            status.update(label="Ищу варианты и собираю план")
            result = build_corrected_plan(
                request,
                today=date.today(),
                hotel_choice=hotel_choice,
                hotel_options=hotel_options,
                progress=lambda label: status.update(label=label),
            )
            status.update(label="План готов", state="complete", expanded=False)
    except Exception as error:
        st.session_state.app_error = f"Не удалось собрать план: {error}"
        st.session_state.show_results = False
        return

    st.session_state.correction_result = result
    st.session_state.show_results = True
    st.session_state.pending_place = None
    st.session_state.pending_place_request = None
    st.rerun()


def _run_parse(
    text: str,
    *,
    previous=None,
    asked_field: str | None = None,
) -> None:
    """Разбирает запрос и, если данных достаточно, собирает план."""
    st.session_state.app_error = None
    st.session_state.pending_place = None
    st.session_state.pending_place_request = None
    try:
        parsed = parse_trip_request(
            text,
            today=date.today(),
            previous=previous,
            asked_field=asked_field,
            use_llm=True,
        )
    except Exception as error:  # интерфейс не должен падать из-за неожиданной ошибки
        st.session_state.app_error = f"Не удалось разобрать запрос: {error}"
        return

    st.session_state.parse_notes = parsed.notes
    if parsed.question:
        st.session_state.clarify_round += 1
        st.session_state.pending_request = parsed.request
        st.session_state.pending_question = parsed.question
        st.session_state.pending_field = parsed.asked_field
        st.session_state.correction_result = None
        st.session_state.show_results = False
        return

    st.session_state.pending_request = None
    st.session_state.pending_question = None
    st.session_state.pending_field = None
    _finalize_request(parsed.request)


def _render_issue(issue) -> None:
    renderer = getattr(st, SEVERITY_STYLE.get(issue.severity, "info"))
    renderer(
        f"**{issue.severity.label_ru} · {issue.code}**\n\n"
        f"{issue.description}\n\n"
        f"Источник: {issue.source}\n\n"
        f"Рекомендация: {issue.recommendation}"
    )


def _render_free_form() -> bool:
    st.markdown("#### Или опишите поездку словами")
    st.caption(
        "Свободный запрос понимает любые формулировки, а мастер помогает "
        "не забыть маршрут, даты, бюджет и предпочтения."
    )
    with st.form("request_form"):
        st.text_area(
            "Опишите поездку",
            key="request_text",
            height=110,
            placeholder=EXAMPLE,
        )
        return st.form_submit_button("Проверить поездку", type="primary")


def _render_pending_place() -> None:
    if not st.session_state.pending_place:
        return
    ambiguous: AmbiguousPlace = st.session_state.pending_place
    st.info(ambiguous.question)
    options = ambiguous.options
    choice = st.radio(
        f"{ambiguous.field_label}: выберите место",
        options=list(range(len(options))),
        format_func=lambda index: options[index],
        key=f"place_choice_{ambiguous.field}_{ambiguous.query}",
    )
    if st.button("Выбрать место", type="primary"):
        selected = ambiguous.candidates[choice]
        updated = st.session_state.pending_place_request.model_copy(
            update={ambiguous.field: selected.city}
        )
        st.session_state.pending_place = None
        st.session_state.pending_place_request = None
        _finalize_request(updated, skip_place_fields=(ambiguous.field,))
        st.rerun()


def _render_clarification() -> None:
    if not st.session_state.pending_question:
        return
    st.info(st.session_state.pending_question)
    answer_key = f"clarification_answer_{st.session_state.clarify_round}"
    with st.form("clarification_form"):
        st.text_input("Ваш ответ", key=answer_key)
        answered = st.form_submit_button("Ответить")
    if answered:
        answer = (st.session_state.get(answer_key) or "").strip()
        if not answer:
            st.warning("Введите ответ.")
        else:
            _run_parse(
                answer,
                previous=st.session_state.pending_request,
                asked_field=st.session_state.pending_field,
            )
            st.rerun()


def _flight_option_is_selected(plan, option) -> bool:
    return (
        plan.flight_out is not None
        and plan.flight_back is not None
        and option.outbound.id == plan.flight_out.id
        and option.inbound.id == plan.flight_back.id
    )


def _render_flight_card(plan, title: str, flight) -> None:
    with st.container(border=True):
        st.markdown(f"**{title}**")
        if flight is None:
            st.caption("Транспорт не выбран: источник не вернул подходящий вариант.")
            return
        st.markdown(
            f"{TRANSPORT_NOUNS.get(flight.mode, 'Транспорт')} · {flight.airline}"
        )
        st.caption(
            f"{format_date_ru(flight.departure_time.date())} "
            f"{format_time_value(flight.departure_time.time())} → "
            f"{format_date_ru(flight.arrival_time.date())} "
            f"{format_time_value(flight.arrival_time.time())} · "
            f"в пути {format_duration(int((flight.arrival_time - flight.departure_time).total_seconds() // 60))}"
        )
        st.markdown(
            _badge(flight.price_kind, price_kind_label(flight)),
            unsafe_allow_html=True,
        )
        stops_text = "без пересадок" if flight.stops == 0 else f"пересадок: {flight.stops}"
        st.caption(
            f"{format_money(flight.price_rub)} · {stops_text} · "
            f"багаж: {baggage_label(flight)}"
        )
        if flight.source:
            st.caption(f"Источник: {flight.source}")
        if flight.booking_url:
            st.markdown(f"[Открыть предложение]({flight.booking_url})")
        if flight.departure_node or flight.arrival_node:
            st.caption(
                f"Узлы: {flight.departure_node or '—'} → "
                f"{flight.arrival_node or '—'}"
            )
        for label, leg in (
            ("Дорога до узла", flight.access_to_departure),
            ("Дорога от узла", flight.access_from_arrival),
        ):
            if leg is None:
                continue
            st.caption(
                f"{label}: {ACCESS_MODE_LABELS.get(leg.mode, leg.mode)}, "
                f"{leg.distance_km:g} км, ~{leg.minutes} мин, "
                f"{format_money(leg.price_rub)} · оценка"
            )


def _offers_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "предложение"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return "предложения"
    return "предложений"


def _render_price_sources(plan) -> None:
    """Сводка минимальных цен по сервисам и направлениям."""
    if not plan.price_sources:
        return
    st.markdown("**Сравнение сервисов**")
    st.caption(
        "Минимальные цены, которые вернул каждый сервис. Это кэш или оценка, "
        "а не подтверждённое наличие мест: перед покупкой откройте предложение."
    )
    groups: dict[tuple[str, str], list] = {}
    for source in plan.price_sources:
        groups.setdefault((source.direction, source.mode), []).append(source)

    cards: list[str] = []
    for (direction, mode), sources in groups.items():
        cheapest = min(sources, key=lambda item: item.cheapest_rub)
        rows: list[str] = []
        for source in sorted(sources, key=lambda item: item.cheapest_rub):
            if source is cheapest:
                badge = '<span class="ot-price-tag">минимум</span>'
            elif source.cheapest_rub > cheapest.cheapest_rub:
                gap = source.cheapest_rub - cheapest.cheapest_rub
                badge = f'<span class="ot-price-gap">+{format_money(gap)}</span>'
            else:
                badge = ""
            link = ""
            if source.booking_url:
                link = (
                    f'<a href="{html.escape(source.booking_url, quote=True)}" '
                    'target="_blank" rel="noopener">открыть</a>'
                )
            kind = SOURCE_KIND_LABELS.get(source.price_kind, source.price_kind)
            rows.append(
                '<div class="ot-price-row">'
                f'<span class="ot-price-service">{html.escape(source.service)}</span>'
                f"<strong>{format_money(source.cheapest_rub)}</strong>"
                f"{badge}{link}"
                '<span class="ot-price-kind">'
                f"{html.escape(kind)} · {source.offers_count} "
                f"{_offers_word(source.offers_count)}</span>"
                "</div>"
            )
        direction_label = "Туда" if direction == "outbound" else "Обратно"
        cards.append(
            '<div class="ot-price-card">'
            '<div class="ot-price-head">'
            f"<span>{direction_label} · {MODE_LABELS.get(mode, mode)}</span>"
            f"<strong>{format_money(cheapest.cheapest_rub)}</strong>"
            "</div>"
            f'<div class="ot-price-best">Дешевле всего: {html.escape(cheapest.service)}</div>'
            + "".join(rows)
            + "</div>"
        )
    st.markdown(
        '<div class="ot-price-board">' + "".join(cards) + "</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Минимум считается по ответившим сервисам без учёта багажа и условий "
        "возврата: перед покупкой сравните тариф на сайте."
    )


def _apply_transport(plan, option) -> None:
    try:
        with st.status(
            "Пересобираю план с выбранным транспортом", expanded=True
        ) as status:
            result = build_corrected_plan(
                plan.request,
                today=date.today(),
                flight_pair=pair_from_option(option),
                transport_options=list(plan.transport_options),
                transport_alternatives=list(plan.transport_alternatives),
                hotel_choice=plan.hotel,
                hotel_options=list(plan.hotel_options),
                progress=lambda label: status.update(label=label),
            )
            status.update(label="План обновлён", state="complete", expanded=False)
    except Exception as error:
        st.error(f"Не удалось применить транспорт: {error}")
        return
    st.session_state.correction_result = result
    st.session_state.show_results = True
    st.rerun()


def _render_transport_tab(plan) -> None:
    left, right = st.columns(2)
    with left:
        _render_flight_card(plan, "Туда", plan.flight_out)
    with right:
        _render_flight_card(plan, "Обратно", plan.flight_back)

    _render_price_sources(plan)

    comparison = list(plan.transport_alternatives) or list(plan.transport_options)
    if comparison:
        st.markdown("**Найденные варианты транспорта**")
        st.caption(
            "Показаны варианты, которые вернули источники, включая более дорогие. "
            "Сравнение не является рекомендацией купить билет."
        )
        for index, option in enumerate(comparison):
            selected = _flight_option_is_selected(plan, option)
            with st.container(border=True):
                header = (
                    f"**{MODE_LABELS.get(option.mode, option.mode)}** · "
                    f"{option.outbound.airline}"
                )
                if selected:
                    header += " · выбран"
                st.markdown(header)
                st.caption(
                    f"{format_date_ru(option.outbound.departure_time.date())} "
                    f"{format_time_value(option.outbound.departure_time.time())} → "
                    f"{format_date_ru(option.inbound.arrival_time.date())} "
                    f"{format_time_value(option.inbound.arrival_time.time())} · "
                    f"в пути {format_duration(option.total_duration_minutes)}"
                )
                st.caption(
                    f"{format_money(option.total_price_rub)} туда и обратно · "
                    f"{price_kind_label(option.outbound)}"
                )
                if option.outbound.source:
                    st.caption(f"Источник: {option.outbound.source}")
                links = [
                    link
                    for link in (
                        option.outbound.booking_url,
                        option.inbound.booking_url,
                    )
                    if link
                ]
                for link_index, link in enumerate(links, start=1):
                    st.markdown(
                        f"[Открыть предложение {link_index}]({link})"
                    )
                if selected:
                    st.caption("Этот вариант уже выбран в плане.")
                elif st.button(
                    "Выбрать этот вариант",
                    key=f"transport_pick_{index}",
                    use_container_width=True,
                ):
                    _apply_transport(plan, option)
            if index >= 5:
                break
        if _travel_search_enabled():
            st.caption(
                "Авиабилеты Aviasales и расписание Tutu.ru приходят из кэша "
                "стороннего сервиса travel-search-ru, а не из поиска в реальном "
                "времени. Наличие мест не подтверждено."
            )
        else:
            st.caption(
                "travel-search-ru отключён: рейсы и отели берутся из Travelpayouts "
                "по токену или считаются оценкой проекта."
            )
    elif plan.flight_out is None:
        st.info(
            "Транспорт не найден: источник не вернул варианты на эти даты. "
            "План ниже не скрывает это и не подставляет вымышленный рейс."
        )


def _hotel_limitations(hotel) -> list[str]:
    limits: list[str] = []
    if hotel.price_kind == "mock":
        limits.append("Цена и рейтинг — тестовые данные, не предложение отеля.")
    elif hotel.price_kind == "estimate":
        limits.append(
            "Цена за ночь — оценка проекта, а не тариф отеля; наличие мест "
            "не подтверждено."
        )
    else:
        limits.append(
            "Цена из предложения сервиса; оно может измениться, наличие мест "
            "нужно проверить перед бронированием."
        )
    if hotel.rating_source == "Нет данных":
        limits.append("Рейтинг в источнике отсутствует: показано нейтральное значение.")
    if not hotel.center_known:
        limits.append("Расстояние до центра источник не сообщил.")
    if not hotel.metro_known:
        limits.append("Расстояние до метро источник не сообщил.")
    if hotel.source.startswith("OpenStreetMap"):
        limits.append("Название и координаты — OpenStreetMap; цена и время — оценка проекта.")
    return limits


def _render_hotel_details(plan, hotel) -> None:
    nights = max(0, (plan.request.duration_days or 1) - 1)
    rating = "нет данных" if hotel.rating_source == "Нет данных" else f"{hotel.rating:g}"
    st.markdown(f"**{hotel.name}** · {hotel.district}")
    st.markdown(
        _badge(hotel.price_kind, price_kind_label(hotel)),
        unsafe_allow_html=True,
    )
    if hotel.stay_total_rub is not None:
        st.caption(
            f"Всего за проживание: {format_money(hotel.stay_total_rub)} · "
            f"{nights} ноч. · {format_money(hotel.price_per_night_rub)} за ночь"
        )
    elif hotel.price_per_night_rub is not None and nights > 0:
        estimate = hotel.price_per_night_rub * nights
        st.caption(
            f"Всего за проживание: {format_money(estimate)} · {nights} ноч. · "
            f"{format_money(hotel.price_per_night_rub)} за ночь"
        )
        st.caption(
            "Итог источником не сообщён: это расчёт проекта (цена за ночь × "
            "число ночей), а не подтверждённый тариф."
        )
    else:
        st.caption("Стоимость проживания источником не сообщена.")
    st.caption(f"Рейтинг: {rating}" + (f" · питание: {hotel.meal_plan}" if hotel.meal_plan else ""))
    if hotel.distance_to_center_km is not None:
        st.caption(
            f"До центра {hotel.distance_to_center_km:g} км "
            f"(~{format_duration(hotel.minutes_to_center)})"
        )
    elif hotel.center_known:
        st.caption(f"До центра ~{format_duration(hotel.minutes_to_center)}")
    if hotel.distance_to_metro_km is not None:
        st.caption(
            f"До метро {hotel.distance_to_metro_km:g} км "
            f"(~{format_duration(hotel.minutes_to_metro)})"
        )
    elif hotel.metro_known:
        st.caption(f"До метро ~{format_duration(hotel.minutes_to_metro)}")
    if hotel.source:
        st.caption(f"Источник: {hotel.source}")
    if hotel.booking_url:
        st.markdown(f"[Открыть предложение отеля]({hotel.booking_url})")
    for limit in _hotel_limitations(hotel):
        st.caption(f"Ограничение: {limit}")


def _apply_hotel(plan, hotel, options) -> None:
    try:
        with st.status("Пересобираю план с выбранным отелем", expanded=True) as status:
            result = build_corrected_plan(
                plan.request,
                today=date.today(),
                flight_pair=pair_from_plan(plan),
                transport_options=list(plan.transport_options),
                transport_alternatives=list(plan.transport_alternatives),
                hotel_choice=hotel,
                hotel_options=list(options),
                progress=lambda label: status.update(label=label),
            )
            status.update(label="План обновлён", state="complete", expanded=False)
    except Exception as error:
        st.error(f"Не удалось применить отель: {error}")
        return
    st.session_state.correction_result = result
    st.session_state.show_results = True
    st.rerun()


def _render_transfers(plan) -> None:
    """Дорога между узлом прилёта/вылета и отелем: пешком, транспорт, такси."""
    if not plan.hotel_transfers:
        return
    st.markdown("**Дорога до отеля**")
    st.caption(
        "Оценки проекта по расстоянию, а не тарифы и расписание. "
        "Маршруты открываются по координатам отеля и узла."
    )
    cards: list[str] = []
    for transfer in plan.hotel_transfers:
        cheapest = transfer.cheapest()
        rows: list[str] = []
        for option in transfer.options:
            price = (
                "бесплатно"
                if option.price_rub <= 0
                else format_money(option.price_rub)
            )
            badge = (
                '<span class="ot-price-tag">дешевле всего</span>'
                if cheapest is not None and option.mode == cheapest.mode
                else ""
            )
            links: list[str] = []
            if option.maps_url:
                links.append(
                    f'<a href="{html.escape(option.maps_url, quote=True)}" '
                    'target="_blank" rel="noopener">OpenStreetMap</a>'
                )
            if option.yandex_url:
                links.append(
                    f'<a href="{html.escape(option.yandex_url, quote=True)}" '
                    'target="_blank" rel="noopener">Яндекс Карты</a>'
                )
            rows.append(
                '<div class="ot-transfer-row">'
                f'<span class="ot-transfer-mode">'
                f"{html.escape(ACCESS_MODE_LABELS.get(option.mode, option.mode))}</span>"
                f'<span class="ot-transfer-time">~{option.minutes} мин</span>'
                f"<strong>{price}</strong>{badge}"
                + (
                    f'<span class="ot-transfer-links">{" · ".join(links)}</span>'
                    if links
                    else ""
                )
                + (
                    f'<span class="ot-transfer-note">{html.escape(option.note)}</span>'
                    if option.note
                    else ""
                )
                + "</div>"
            )
        cards.append(
            '<div class="ot-transfer-card">'
            '<div class="ot-transfer-head">'
            f"<strong>{html.escape(transfer.title)}</strong>"
            f"<span>{html.escape(transfer.from_name)} → "
            f"{html.escape(transfer.to_name)} · {transfer.distance_km:g} км</span>"
            "</div>"
            + "".join(rows)
            + "</div>"
        )
    st.markdown(
        '<div class="ot-transfer-board">' + "".join(cards) + "</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Такси: тариф, подача и пробки не проверены. Маршрут в Яндекс Картах "
        "помогает открыть точку, а поездку нужно заказывать отдельно в приложении "
        "Яндекс Go. Общественный транспорт: расписание не проверено."
    )


def _render_hotels_tab(plan) -> None:
    if plan.hotel is None:
        st.info(
            "Отель не выбран: источник не вернул варианты проживания. "
            "Стоимость проживания не подставляется."
        )
    else:
        st.markdown("**Выбранный вариант**")
        with st.container(border=True):
            _render_hotel_details(plan, plan.hotel)
        unmatched = unmatched_hotel_preferences(plan.hotel_options, plan.request)
        if unmatched:
            st.info(
                "Источники не подтвердили пожелания к жилью: "
                + ", ".join(unmatched)
                + ". Показаны остальные найденные варианты; пожелания не скрывают их."
            )
        if plan.request.accommodation_preferences:
            st.caption(
                "Пожелания к жилью: "
                + ", ".join(plan.request.accommodation_preferences)
                + "."
            )
        _render_transfers(plan)
        if plan.hotel_search_url:
            st.markdown(
                f"[Другие отели на те же даты на Trip.com]({plan.hotel_search_url})"
            )

    options = list(plan.hotel_options)
    if len(options) > 1:
        st.markdown(f"**Найденные варианты проживания ({len(options)})**")
        st.caption(
            "Показаны все найденные варианты в пределах лимита источника, "
            "включая те, что дороже выбранного."
        )
        for hotel in options:
            with st.container(border=True):
                _render_hotel_details(plan, hotel)
                if plan.hotel is not None and hotel.id == plan.hotel.id:
                    st.caption("Этот вариант уже выбран.")
                elif st.button(
                    "Выбрать этот отель",
                    key=f"hotel_pick_{hotel.id}",
                    use_container_width=True,
                ):
                    _apply_hotel(plan, hotel, options)
    elif plan.hotel is not None:
        st.caption("Источник вернул один вариант проживания.")


def _known_cost_rows(plan) -> tuple[list[tuple[str, float]], list[str]]:
    cost = plan.cost
    if cost is None:
        return [], ["вся смета"]
    rows: list[tuple[str, float]] = []
    unknown: list[str] = []
    if plan.flight_out is not None and plan.flight_back is not None:
        rows.append(("Транспорт", cost.flights))
    else:
        unknown.append("транспорт")
    if plan.hotel is not None:
        rows.append(("Проживание", cost.accommodation))
    else:
        unknown.append("проживание")
    rows.extend(
        [
            ("Трансферы", cost.transfers),
            ("Багаж", cost.baggage),
            ("Туристический налог", cost.tourist_tax),
            ("Местный транспорт", cost.local_transport),
            ("Билеты и места", cost.attractions),
        ]
    )
    return rows, unknown


def _render_budget_tab(plan) -> None:
    if plan.cost is None:
        st.warning("Стоимость не рассчитана: в плане нет данных для сметы.")
        return
    rows, unknown = _known_cost_rows(plan)
    st.table(
        [
            {"Статья": label, "Сумма": format_money(value)}
            for label, value in rows
        ]
        + [{"Статья": "Итого известных расходов", "Сумма": format_money(plan.cost.total)}]
    )
    budget_rub = _budget_in_rub(plan.request)
    if plan.request.budget_amount is not None and budget_rub is not None:
        remaining = budget_rub - plan.cost.total
        if remaining >= 0:
            st.success(
                f"Бюджет {_format_budget(plan.request.budget_amount, plan.request.currency)} "
                f"укладывается: остаток {format_money(remaining)}."
            )
        else:
            st.warning(
                f"Бюджет превышен на {format_money(-remaining)}. "
                "Смета — оценка, а не подтверждённая цена."
            )
        ratio = plan.cost.total / budget_rub if budget_rub > 0 else 0
        st.progress(min(1.0, ratio), text=f"Бюджет использован на {ratio:.0%}")
        if plan.request.budget_scope == "person":
            st.caption(
                f"Бюджет указан на человека; для {plan.request.travelers} чел. "
                f"в проверке используется {format_money(budget_rub)}."
            )
    elif plan.request.budget_amount is not None:
        st.warning(
            f"Бюджет в валюте {plan.request.currency} не удалось пересчитать: "
            "в допущениях проекта нет курса. Проверка превышения не выполнялась."
        )
    if unknown:
        st.caption(
            "Не оценено: " + ", ".join(unknown) + ". Эти статьи не считаются нулевыми."
        )
    st.caption(
        "В смету не входят питание, страховка, визы и сувениры: обоснованных "
        "данных для их оценки нет."
    )
    for note in plan.cost.notes:
        st.caption(note)


def _render_sources_tab(plan) -> None:
    if any("тестовые данные" in note.lower() for note in plan.data_notes):
        st.warning(
            "В плане использованы тестовые данные: это вымышленные цены, "
            "расписание, часы работы и погода."
        )
    st.markdown("**Пояснения к данным**")
    for note in dict.fromkeys(plan.data_notes):
        st.markdown(f"- {note}")
    if st.session_state.parse_notes:
        st.markdown("**Пояснения парсера**")
        for note in st.session_state.parse_notes:
            st.markdown(f"- {note}")

    st.markdown("**Источники**")
    st.caption(
        "Ниже перечислены источники, которые проект использует. "
        "В текущем плане часть из них могла не запрашиваться."
    )
    st.markdown(
        "- **travel-search-ru** — сторонний проект MissiaL (MIT): "
        "[github.com/MissiaL/travel-search-ru](https://github.com/MissiaL/travel-search-ru). "
        "Через него приходят кэш Aviasales, расписание Tutu.ru и предложения "
        "Level.Travel / Travelata. OneTrip не бронирует билеты и отели.\n"
        "- **OpenStreetMap** — места, отели, транспортные узлы и расстояния: "
        "[openstreetmap.org/copyright](https://www.openstreetmap.org/copyright).\n"
        "- **Open-Meteo** — погода и архив типичной погоды: "
        "[open-meteo.com](https://open-meteo.com/).\n"
        "- **Wikipedia / Wikimedia Commons** — справочные описания и фотографии "
        "направлений. Ссылки на карточках ведут на статьи Википедии.\n"
        "- **Travelpayouts** — необязательный резервный источник кэша цен, "
        "если задан `TRAVELPAYOUTS_TOKEN`."
    )
    if _travel_search_enabled():
        st.caption(
            "Цены и наличие мест не подтверждены. Кэш Aviasales может отставать до "
            "48 часов, расписание Tutu.ru ориентировочное, предложения отелей живут "
            "около 20 минут."
        )
    else:
        st.caption(
            "Цены и наличие мест не подтверждены. travel-search-ru отключён: "
            "цены — кэш Travelpayouts или оценка проекта."
        )
    st.markdown("**Настройки проверок**")
    st.caption(
        "Пороги, веса ранжирования, очки риска и условные курсы валют находятся "
        "в `data/assumptions.json`. Это допущения проекта, а не подтверждённые факты."
    )
    st.caption(
        "Источники: открытые данные, travel-search-ru и оценки проекта. "
        "`TRAVEL_SEARCH=0` отключает внешний сервис поиска."
    )


def _render_day_plan(plan) -> None:
    st.markdown("**Маршрут по дням**")
    for day in plan.days:
        title = f"День {day.day_number}"
        if day.day is not None:
            title += f" · {format_date_ru(day.day)}"
        with st.expander(title, expanded=day.day_number <= 2):
            if day.weather is not None:
                probability = (
                    f", осадки {day.weather.precipitation_probability}%"
                    if day.weather.precipitation_probability is not None
                    else ""
                )
                st.caption(
                    f"Погода: {day.weather.condition}, {day.weather.temperature_c:g} °C"
                    f"{probability}"
                )
            if not day.items:
                st.caption("Свободное время или день без запланированных мест.")
            for item in day.items:
                st.markdown(
                    f"- **{format_time_value(item.start_time)}–"
                    f"{format_time_value(item.end_time)}** — {item.name}"
                    + (f" · {format_money(item.cost_rub)}" if item.cost_rub else "")
                    + (
                        f" · пешком {item.walking_km_from_previous:.1f} км"
                        if item.walking_km_from_previous
                        else ""
                    )
                )
            if day.items:
                st.caption(f"Пешая нагрузка за день: {day.total_walking_km:.1f} км")


def _render_overview_tab(result: CorrectionResult) -> None:
    plan = result.plan
    request = plan.request
    summary = (
        f'<div class="ot-result-summary">'
        f"<div><span>Маршрут</span><strong>{html.escape(request.origin_city or '—')} → "
        f"{html.escape(request.destination_city or '—')}</strong></div>"
        f"<div><span>Даты</span><strong>{format_date_ru(request.start_date)} — "
        f"{format_date_ru(request.end_date)}</strong></div>"
        f"<div><span>Путешественники</span><strong>{request.travelers} чел.</strong></div>"
        f"<div><span>Темп</span><strong>{TRIP_STYLES.get(request.trip_style, request.trip_style)}</strong></div>"
        "</div>"
    )
    st.markdown(summary, unsafe_allow_html=True)

    st.markdown("**Reality Check**")
    if not plan.issues:
        st.success("Проблем по заданным правилам не найдено.")
    else:
        st.caption(
            "Проверки детерминированные: одинаковый план и настройки всегда дают "
            "одинаковый результат. Это не гарантия реальной поездки."
        )
        for issue in plan.issues:
            _render_issue(issue)

    if result.applied or result.notes:
        st.markdown("**Журнал исправлений**")
        for label in result.applied:
            st.markdown(f"- {label}")
        for note in result.notes:
            st.markdown(f"- {note}")

    if result.initial_plan.issues:
        with st.expander("Исходный план до исправлений"):
            st.caption(f"Риск до исправлений: {risk_label(result.initial_plan)}")
            for issue in result.initial_plan.issues:
                st.markdown(
                    f"- {issue.severity.label_ru} · `{issue.code}` — {issue.description}"
                )

    st.markdown("**Проверка и источники**")
    failed = [
        note
        for note in plan.data_notes
        if note.startswith("Не удалось получить")
    ]
    if failed:
        for note in failed:
            st.warning(note)
    else:
        st.caption(
            "Все запрошенные источники ответили без ошибок. Это не значит, что "
            "цены и наличие мест подтверждены."
        )
    _render_day_plan(plan)


def _render_plan(result: CorrectionResult) -> None:
    plan = result.plan
    initial = result.initial_plan
    request = plan.request

    origin = request.origin_city or "—"
    destination = request.destination_city or "—"
    st.subheader(f"{origin} → {destination}")
    st.caption(
        f"{format_date_ru(request.start_date)} — {format_date_ru(request.end_date)} · "
        f"{request.duration_days or '?'} дн. · "
        f"путешественников: {request.travelers} · "
        f"темп: {TRIP_STYLES.get(request.trip_style, request.trip_style).lower()}"
    )

    unsupported = next(
        (issue for issue in plan.issues if issue.code == "DESTINATION_UNSUPPORTED"),
        None,
    )
    if unsupported is not None:
        st.error(f"План не собран. {unsupported.description}")
        st.markdown(f"**Что делать:** {unsupported.recommendation}")
        st.caption(
            "Стоимость и маршрут не показаны: без данных о городе это были бы "
            "выдуманные значения."
        )
        with st.expander("Пояснения к данным"):
            for note in dict.fromkeys(plan.data_notes + result.notes):
                st.markdown(f"- {note}")
        st.button(
            "Изменить параметры",
            on_click=_edit_current_request,
            type="primary",
        )
        return

    risk_text = f"Риск: {risk_label(plan)}"
    if initial.risk_score != plan.risk_score:
        risk_text += f" · до исправлений: {risk_label(initial)}"
    if plan.risk_level is not None and plan.risk_level.value == "High":
        st.error(risk_text)
    elif plan.risk_level is not None and plan.risk_level.value == "Medium":
        st.warning(risk_text)
    else:
        st.success(risk_text)

    budget_rub = _budget_in_rub(request)
    if request.budget_amount is not None and budget_rub is not None and plan.cost is not None:
        remaining = budget_rub - plan.cost.total
        if remaining >= 0:
            st.success(
                f"Бюджет: остаток {format_money(remaining)} из "
                f"{format_money(budget_rub)}."
            )
        else:
            st.warning(f"Бюджет: превышение на {format_money(-remaining)}.")
    elif request.budget_amount is not None:
        st.warning("Бюджет не удалось пересчитать в рубли: проверка не выполнялась.")

    columns = st.columns(4)
    if plan.cost is not None:
        columns[0].metric("Оценка стоимости", format_money(plan.cost.total))
    if request.budget_amount is not None:
        columns[1].metric(
            "Бюджет",
            _format_budget(request.budget_amount, request.currency),
        )
    columns[2].metric("Проблем после проверки", len(plan.issues))
    columns[3].metric("Автоматических исправлений", result.attempts)

    markdown = plan_to_markdown(
        plan,
        initial_plan=initial if initial.risk_score != plan.risk_score else None,
        applied=result.applied,
        correction_notes=result.notes,
    )
    actions = st.columns([1.2, 1.1, 1.5, 1.2])
    with actions[0]:
        st.button(
            "Изменить параметры",
            on_click=_edit_current_request,
            use_container_width=True,
        )
    with actions[1]:
        if st.button("Повторить поиск", use_container_width=True):
            _finalize_request(plan.request)
    with actions[2]:
        st.download_button(
            "Скачать план в Markdown",
            data=markdown.encode("utf-8"),
            file_name="onetrip_plan.md",
            mime="text/markdown",
            use_container_width=True,
        )

    overview_tab, transport_tab, hotels_tab, budget_tab, sources_tab = st.tabs(
        ["Обзор", "Транспорт", "Отели", "Бюджет", "Об источниках"]
    )
    with overview_tab:
        _render_overview_tab(result)
    with transport_tab:
        _render_transport_tab(plan)
    with hotels_tab:
        _render_hotels_tab(plan)
    with budget_tab:
        _render_budget_tab(plan)
    with sources_tab:
        _render_sources_tab(plan)


def _render_top_bar() -> None:
    result = st.session_state.correction_result
    show_results = st.session_state.show_results and result is not None
    with st.container(key="ot_topbar"):
        if show_results:
            brand_column, action_column = st.columns(
                [2.6, 1.4], vertical_alignment="center"
            )
        else:
            brand_column, nav_column = st.columns(
                [1.4, 2.6], vertical_alignment="center"
            )
        with brand_column:
            st.markdown(
                '<div class="ot-brand">'
                '<span class="ot-logo">OneTrip<span class="ot-dot">.</span></span>'
                '<span class="ot-brand-note">Reality Check</span>'
                "</div>",
                unsafe_allow_html=True,
            )
        if show_results:
            with action_column:
                st.button(
                    "Новый запрос",
                    on_click=_reset_new_request,
                    use_container_width=True,
                )
        else:
            with nav_column:
                st.markdown(
                    '<nav class="ot-nav">'
                    '<a href="#ot-plan">Планировщик</a>'
                    '<a href="#ot-reality">Reality Check</a>'
                    '<a href="#ot-destinations">Направления</a>'
                    '<a href="#ot-tools">Быстрые действия</a>'
                    "</nav>",
                    unsafe_allow_html=True,
                )


def _render_hero() -> None:
    with st.container(key="ot_hero"):
        st.markdown(
            '<div class="ot-flight" aria-hidden="true">'
            '<span class="ot-route"></span>'
            '<span class="ot-plane"></span>'
            '<span class="ot-plane two"></span>'
            "</div>"
            '<div class="ot-kicker">Поездки по России · проверка на реальность</div>',
            unsafe_allow_html=True,
        )
        st.title("Путешествуй умнее. Трать меньше.")
        st.markdown(
            '<p class="ot-hero-sub">Поездки по России без сюрпризов: сравните билеты '
            "и жильё, посчитайте дорогу до отеля и проверьте, что план не развалится "
            "на месте.</p>"
            '<div class="ot-hero-actions">'
            '<a class="ot-cta" href="#ot-plan">Спланировать поездку'
            '<span aria-hidden="true">→</span></a>'
            '<a class="ot-cta ot-cta-ghost" href="#ot-reality">Как проверяем план</a>'
            "</div>"
            '<div class="ot-trust"><span>Города России</span>'
            "<span>Поезда и самолёты</span>"
            "<span>Жильё на ваш формат</span>"
            "<span>Честные оценки</span></div>",
            unsafe_allow_html=True,
        )


def _render_footer() -> None:
    with st.container(key="ot_footer"):
        travel_search_on = _travel_search_enabled()
        data_column, status_column = st.columns([2.2, 1.4])
        with data_column:
            if travel_search_on:
                st.caption(
                    "Данные: travel-search-ru — кэш Aviasales, расписание Tutu.ru "
                    "и предложения Level.Travel."
                )
            else:
                st.caption("Данные: travel-search-ru отключён (TRAVEL_SEARCH=0).")
            if os.getenv("TRAVELPAYOUTS_TOKEN", "").strip():
                st.caption("Резерв: Travelpayouts (кэш цен).")
            st.caption(
                "Погода, места, координаты: Open-Meteo, Wikipedia, OpenStreetMap."
            )
            st.caption("Пороги и веса: data/assumptions.json")
        with status_column:
            if llm.is_llm_configured():
                st.caption(f"LLM: настроена ({os.getenv('LLM_MODEL', '').strip()})")
            else:
                st.caption("LLM: не настроена — разбор по правилам. Инструкция в README.")
        st.markdown('<div id="ot-tools" class="ot-anchor"></div>', unsafe_allow_html=True)
        with st.expander("Быстрые действия"):
            st.caption(
                "Подставить пример запроса или очистить текущий результат. "
                "В обычном сценарии пользователь начинает с кнопки «Спланировать поездку»."
            )
            example_column, clear_column = st.columns(2)
            with example_column:
                st.button(
                    "Подставить пример",
                    on_click=lambda: st.session_state.update(request_text=EXAMPLE),
                    use_container_width=True,
                )
            with clear_column:
                st.button(
                    "Очистить результат",
                    on_click=_clear_result,
                    use_container_width=True,
                )
        with st.expander("Об источниках"):
            st.markdown(
                "- **travel-search-ru** — сторонний проект MissiaL (MIT). "
                "OneTrip использует его как источник данных и не бронирует билеты.\n"
                "- Цены Aviasales и Level.Travel — кэш, а не поиск в реальном времени; "
                "наличие мест нужно проверять на сайте сервиса.\n"
                "- Расписание Tutu.ru — кэш; цена поезда ориентировочная.\n"
                "- Погода, места и координаты — открытые данные без ключей."
            )


def main() -> None:
    st.set_page_config(
        page_title="OneTrip Reality Check",
        page_icon=None,
        layout="wide",
    )
    st.markdown(_CSS, unsafe_allow_html=True)
    _init_state()
    _render_top_bar()

    result = st.session_state.correction_result
    if st.session_state.show_results and result is not None:
        _render_plan(result)
        _render_footer()
        return

    _render_hero()

    st.markdown('<div id="ot-plan" class="ot-anchor"></div>', unsafe_allow_html=True)
    _render_wizard()

    st.markdown(
        '<details class="ot-reveal" id="ot-reality">'
        '<summary><span class="ot-reveal-badge">Reality Check</span>'
        '<span class="ot-reveal-text">Не просто подборка вариантов: план '
        "проверяется на реальность</span>"
        '<span class="ot-reveal-plus" aria-hidden="true">+</span></summary>'
        '<div class="ot-reveal-body"><p>Детерминированные проверки ловят ночной '
        "прилёт, короткую пересадку, далёкий отель, перегруженный день, закрытые "
        "места, дождь и бюджет. Всё, что является оценкой или кэшем, помечено."
        "</p>"
        '<div class="ot-reveal-tags"><span>ночной прилёт</span>'
        "<span>пересадки</span><span>дорога до отеля</span>"
        "<span>погода</span><span>бюджет</span></div></div></details>",
        unsafe_allow_html=True,
    )
    submitted = _render_free_form()
    if submitted:
        text = (st.session_state.request_text or "").strip()
        if not text:
            st.warning("Введите запрос: куда, откуда, на сколько дней и с каким бюджетом.")
        else:
            _run_parse(text)

    if st.session_state.app_error:
        st.error(st.session_state.app_error)

    _render_pending_place()
    _render_clarification()

    if (
        st.session_state.correction_result is None
        and not st.session_state.pending_question
        and not st.session_state.pending_place
        and not st.session_state.app_error
    ):
        st.caption("План появится здесь после проверки запроса.")

    _render_footer()


if __name__ == "__main__":
    main()
