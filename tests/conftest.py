"""Общие настройки и фикстуры для тестов."""
import json
import sys
from pathlib import Path

import pytest

# Корень проекта нужен в sys.path, чтобы работал `import models`.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent.corrector
import agent.planner
from providers.fixtures import FixtureProvider

DATA_DIR = ROOT / "data"
FIXTURES_DIR = ROOT / "fixtures"


@pytest.fixture(scope="session")
def fixture_provider() -> FixtureProvider:
    """Один набор вымышленных данных на весь прогон."""
    return FixtureProvider()


@pytest.fixture(autouse=True)
def isolate_local_env(monkeypatch, fixture_provider):
    """Тесты не должны зависеть от сети и локального .env разработчика.

    `llm.py` загружает .env при импорте (в том числе во время UI-тестов
    через AppTest), поэтому окружение фиксируется явно. Планировщик и
    цикл исправлений получают провайдера фикстур вместо live-источников.
    """
    monkeypatch.setenv("TRAVEL_SEARCH", "0")
    monkeypatch.delenv("TRAVELPAYOUTS_TOKEN", raising=False)
    monkeypatch.delenv("DATA_PROVIDER", raising=False)
    monkeypatch.setattr(agent.planner, "get_provider", lambda: (fixture_provider, []))
    monkeypatch.setattr(agent.corrector, "get_provider", lambda: (fixture_provider, []))


@pytest.fixture
def load_json():
    """Возвращает функцию, которая читает JSON-файл из папки data/."""

    def _load(filename: str):
        with open(DATA_DIR / filename, encoding="utf-8") as file:
            return json.load(file)

    return _load


@pytest.fixture
def load_fixture():
    """Возвращает функцию, которая читает JSON-фикстуру из папки fixtures/."""

    def _load(filename: str):
        with open(FIXTURES_DIR / filename, encoding="utf-8") as file:
            return json.load(file)

    return _load
