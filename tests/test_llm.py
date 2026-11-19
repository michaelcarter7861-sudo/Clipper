from __future__ import annotations

import json

import pytest

from clipper.config import Settings
from clipper.llm import (
    ChatLLM,
    LLMError,
    Provider,
    _content_from_response,
    extract_json,
    resolve_provider,
)


def test_extract_json_plain():
    assert extract_json('{"clips": []}') == {"clips": []}
    assert extract_json("[1, 2, 3]") == [1, 2, 3]


def test_extract_json_from_code_fence():
    text = 'Sure! Here you go:\n```json\n{"clips": [{"start": 1, "end": 2}]}\n```\nEnjoy.'
    assert extract_json(text) == {"clips": [{"start": 1, "end": 2}]}


def test_extract_json_with_prose_and_trailing_comma():
    text = 'Here are the clips: {"clips": [{"start": 1, "end": 2},],} hope that helps'
    assert extract_json(text) == {"clips": [{"start": 1, "end": 2}]}


def test_extract_json_with_smart_quotes():
    text = "{“clips”: [{“start”: 3, “end”: 9}]}"
    assert extract_json(text) == {"clips": [{"start": 3, "end": 9}]}


def test_extract_json_handles_nested_and_numbers():
    text = 'blah {"a": {"b": [1, {"c": 2.5}]}} trailing'
    assert extract_json(text) == {"a": {"b": [1, {"c": 2.5}]}}


@pytest.mark.parametrize("text", ["", None, "no json here", "{broken", "<html>oops</html>"])
def test_extract_json_returns_none_on_garbage(text):
    assert extract_json(text) is None


def test_content_from_response_shapes():
    assert _content_from_response({"choices": [{"message": {"content": "hi"}}]}) == "hi"
    assert (
        _content_from_response(
            {"choices": [{"message": {"content": [{"type": "text", "text": "a"}, {"text": "b"}]}}]}
        )
        == "ab"
    )
    assert (
        _content_from_response({"choices": [{"message": {"reasoning_content": "thinking"}}]})
        == "thinking"
    )
    with pytest.raises(LLMError):
        _content_from_response({"nope": True})


def test_provider_url_join():
    provider = Provider("x", "https://api.example.com/v1/", "k", "m")
    assert provider.chat_url == "https://api.example.com/v1/chat/completions"
    assert "k" not in repr(provider)


def test_resolve_provider_precedence(monkeypatch):
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "GROQ_API_KEY",
        "GEMINI_API_KEY",
        "CEREBRAS_API_KEY",
        "OPENROUTER_API_KEY",
        "MISTRAL_API_KEY",
        "TOGETHER_API_KEY",
        "FREELLMAPI_API_KEY",
        "LLM_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    assert resolve_provider(Settings()) is None

    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    provider = resolve_provider(Settings())
    assert provider and provider.name == "groq"
    assert provider.base_url.startswith("https://api.groq.com")

    monkeypatch.setenv("OPENAI_API_KEY", "sk_test")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:3001/v1")
    provider = resolve_provider(Settings())
    assert provider and provider.name == "custom"
    assert provider.model == "auto"


def test_llm_model_override(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("LLM_MODEL", "llama-3.1-8b-instant")
    provider = resolve_provider(Settings())
    assert provider and provider.model == "llama-3.1-8b-instant"


def test_chat_posts_and_parses(monkeypatch):
    captured = {}

    def fake_post(url, payload, headers, timeout, retries=4):
        captured["url"] = url
        captured["payload"] = payload
        captured["headers"] = headers
        return {"choices": [{"message": {"content": "hello"}}]}

    monkeypatch.setattr("clipper.llm._post_json", fake_post)
    provider = Provider("groq", "https://api.groq.com/openai/v1", "gsk_x", "llama")
    llm = ChatLLM(provider)
    assert llm.complete("sys", "user") == "hello"
    assert captured["payload"]["messages"][0]["role"] == "system"
    assert captured["headers"]["Authorization"] == "Bearer gsk_x"
    assert captured["url"].endswith("/chat/completions")


def test_chat_retries_without_json_mode(monkeypatch):
    calls = []

    def fake_post(url, payload, headers, timeout, retries=4):
        calls.append(dict(payload))
        if "response_format" in payload:
            raise LLMError("HTTP 400: response_format is not supported")
        return {"choices": [{"message": {"content": '{"clips": []}'}}]}

    monkeypatch.setattr("clipper.llm._post_json", fake_post)
    llm = ChatLLM(Provider("x", "http://x/v1", "k", "m"))
    assert llm.complete_json("s", "u") == {"clips": []}
    assert len(calls) == 2


def test_complete_json_raises_after_failed_repair(monkeypatch):
    monkeypatch.setattr(
        "clipper.llm._post_json",
        lambda *a, **k: {"choices": [{"message": {"content": "totally not json"}}]},
    )
    llm = ChatLLM(Provider("x", "http://x/v1", "k", "m"))
    with pytest.raises(LLMError):
        llm.complete_json("s", "u")


def test_complete_json_repairs_once(monkeypatch):
    replies = iter(["not json at all", json.dumps({"clips": [{"start": 1, "end": 2}]})])
    monkeypatch.setattr(
        "clipper.llm._post_json",
        lambda *a, **k: {"choices": [{"message": {"content": next(replies)}}]},
    )
    llm = ChatLLM(Provider("x", "http://x/v1", "k", "m"))
    assert llm.complete_json("s", "u") == {"clips": [{"start": 1, "end": 2}]}
