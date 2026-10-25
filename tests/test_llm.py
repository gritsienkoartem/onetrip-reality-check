"""Тесты модуля llm.py. Сеть не используется: requests.post подменяется заглушкой."""
from pathlib import Path

import pytest
import requests

import llm
from llm import LLMError, ask_llm_json, extract_json_object, is_llm_configured

ROOT = Path(__file__).resolve().parent.parent
SECRET_KEYS = ("LLM_API_KEY", "FLIGHTS_API_KEY", "HOTELS_API_KEY", "PLACES_API_KEY", "WEATHER_API_KEY")


class FakeResponse:
    def __init__(self, status_code=200, body=None, bad_json=False):
        self.status_code = status_code
        self._body = body
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("не JSON")
        return self._body


def _ok_body(content):
    return {"choices": [{"message": {"content": content}}]}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://example.test/v1/")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_API_KEY", "secret-key")


# ---------------- extract_json_object ----------------
def test_extract_plain_json():
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_from_markdown_fence():
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_with_surrounding_text():
    assert extract_json_object('Вот результат: {"a": 1} Готово!') == {"a": 1}


@pytest.mark.parametrize("bad", ["", "просто текст", "[1, 2, 3]", "{не json}"])
def test_extract_invalid_json_raises(bad):
    with pytest.raises(LLMError):
        extract_json_object(bad)


# ---------------- настройки ----------------
def test_not_configured_without_env(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert is_llm_configured() is False
    with pytest.raises(LLMError):
        ask_llm_json("система", "запрос")


def test_configured_with_env(configured):
    assert is_llm_configured() is True


def test_env_example_contains_no_secrets():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        for key in SECRET_KEYS:
            if line.startswith(f"{key}="):
                assert line.strip() == f"{key}=", f"В .env.example остался секрет: {key}"


def test_env_file_is_gitignored():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert any(line.strip() in (".env", "/.env") for line in lines)


# ---------------- ask_llm_json ----------------
def test_ask_llm_json_success(configured, monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured.update(url=url, headers=headers, payload=json, timeout=timeout)
        return FakeResponse(200, _ok_body('```json\n{"destination_city": "Стамбул"}\n```'))

    monkeypatch.setattr(llm.requests, "post", fake_post)
    result = ask_llm_json("система", "запрос")

    assert result == {"destination_city": "Стамбул"}
    assert captured["url"] == "https://example.test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret-key"
    assert captured["payload"]["model"] == "test-model"
    assert captured["payload"]["messages"][1]["content"] == "запрос"


def test_no_authorization_header_without_key(configured, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "")
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured["headers"] = headers
        return FakeResponse(200, _ok_body("{}"))

    monkeypatch.setattr(llm.requests, "post", fake_post)
    ask_llm_json("с", "з")
    assert "Authorization" not in captured["headers"]


def test_http_error_raises_llm_error(configured, monkeypatch):
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: FakeResponse(500, {}))
    with pytest.raises(LLMError) as error:
        ask_llm_json("с", "з")
    assert "500" in str(error.value)


def test_timeout_raises_llm_error(configured, monkeypatch):
    def fake_post(*args, **kwargs):
        raise requests.Timeout()

    monkeypatch.setattr(llm.requests, "post", fake_post)
    with pytest.raises(LLMError) as error:
        ask_llm_json("с", "з")
    assert "время ожидания" in str(error.value)


def test_connection_error_raises_llm_error(configured, monkeypatch):
    def fake_post(*args, **kwargs):
        raise requests.ConnectionError()

    monkeypatch.setattr(llm.requests, "post", fake_post)
    with pytest.raises(LLMError):
        ask_llm_json("с", "з")


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(200, bad_json=True),
        FakeResponse(200, {"unexpected": True}),
        FakeResponse(200, _ok_body("")),
        FakeResponse(200, _ok_body("не JSON")),
    ],
)
def test_bad_response_raises_llm_error(configured, monkeypatch, response):
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: response)
    with pytest.raises(LLMError):
        ask_llm_json("с", "з")
