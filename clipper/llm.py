"""LLM plumbing: provider discovery, chat completions and JSON extraction.

The pipeline only needs "given a prompt, get JSON back". That is deliberately
provider-agnostic so it can run on any free OpenAI-compatible endpoint
(Groq, Google AI Studio, Cerebras, OpenRouter, a self-hosted FreeLLMAPI
router, …) with a single key.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .config import Settings, env_str
from .log import get_logger

log = get_logger()

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524}


class LLMError(RuntimeError):
    """Raised when no provider can answer a prompt."""


# --------------------------------------------------------------------------- #
# providers
# --------------------------------------------------------------------------- #
@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str
    model: str
    supports_json_mode: bool = True
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def chat_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    def __repr__(self) -> str:  # never leak the key into logs
        return f"Provider(name={self.name!r}, base_url={self.base_url!r}, model={self.model!r})"


# name -> (env var, default base url, default model)
_PROVIDER_TABLE: dict[str, tuple[str, str, str]] = {
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-4o-mini"),
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "cerebras": ("CEREBRAS_API_KEY", "https://api.cerebras.ai/v1", "llama-3.3-70b"),
    "gemini": (
        "GEMINI_API_KEY",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini-2.5-flash",
    ),
    "openrouter": (
        "OPENROUTER_API_KEY",
        "https://openrouter.ai/api/v1",
        "meta-llama/llama-3.3-70b-instruct:free",
    ),
    "mistral": ("MISTRAL_API_KEY", "https://api.mistral.ai/v1", "mistral-small-latest"),
    "together": (
        "TOGETHER_API_KEY",
        "https://api.together.xyz/v1",
        "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
    ),
    "freellmapi": ("FREELLMAPI_API_KEY", "http://127.0.0.1:3001/v1", "auto"),
}


def _provider_from_env_name(name: str) -> Provider | None:
    env_key, default_base, default_model = _PROVIDER_TABLE[name]
    api_key = env_str(env_key)
    if not api_key:
        return None
    base = env_str(f"{name.upper()}_BASE_URL", default_base) or default_base
    model = env_str(f"{name.upper()}_MODEL", default_model) or default_model
    extra: dict[str, str] = {}
    if name == "openrouter":
        extra = {
            "HTTP-Referer": "https://github.com/",
            "X-Title": "Clipper",
        }
    return Provider(name=name, base_url=base, api_key=api_key, model=model, extra_headers=extra)


def resolve_provider(settings: Settings | None = None) -> Provider | None:
    """Find the first configured LLM endpoint.

    Precedence: explicit ``OPENAI_BASE_URL`` + key (any compatible endpoint,
    including a FreeLLMAPI router), then the named free providers in order.
    ``LLM_MODEL`` always wins over the provider default.
    """
    settings = settings or Settings()
    explicit_model = (settings.llm_model or env_str("LLM_MODEL") or "").strip() or None

    api_key = env_str("OPENAI_API_KEY")
    base_url = env_str("OPENAI_BASE_URL")
    if api_key and base_url:
        provider = Provider(
            name="custom",
            base_url=base_url,
            api_key=api_key,
            model=explicit_model or env_str("OPENAI_MODEL", "auto"),
        )
        return provider

    for name in ("groq", "gemini", "cerebras", "openrouter", "mistral", "together", "openai", "freellmapi"):
        provider = _provider_from_env_name(name)
        if provider is not None:
            if explicit_model:
                provider.model = explicit_model
            return provider

    if api_key:  # OPENAI_API_KEY without a base url -> official endpoint
        return Provider(
            name="openai",
            base_url="https://api.openai.com/v1",
            api_key=api_key,
            model=explicit_model or "gpt-4o-mini",
        )
    return None


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
    retries: int = 4,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    last_error: Exception | None = None

    for attempt in range(retries):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
            return json.loads(raw)
        except urllib.error.HTTPError as exc:  # noqa: PERF203 - retry loop
            detail = exc.read().decode("utf-8", "replace")[:800]
            last_error = LLMError(f"HTTP {exc.code} from {url}: {detail}")
            if exc.code not in RETRYABLE_STATUS or attempt == retries - 1:
                raise last_error from exc
            wait = _retry_after(exc.headers.get("Retry-After"), attempt)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = LLMError(f"request to {url} failed: {exc}")
            if attempt == retries - 1:
                raise last_error from exc
            wait = _retry_after(None, attempt)

        log.info("LLM retry in %.1fs (%s)", wait, last_error)
        time.sleep(wait)

    raise last_error or LLMError("unknown LLM failure")


def _retry_after(header: str | None, attempt: int) -> float:
    if header:
        try:
            return min(60.0, max(1.0, float(header)))
        except ValueError:
            pass
    return min(45.0, (2 ** attempt) + random.random())


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #
class ChatLLM:
    """Thin OpenAI-compatible chat client with JSON helpers."""

    def __init__(self, provider: Provider, timeout: float = 180.0, retries: int = 4) -> None:
        self.provider = provider
        self.timeout = timeout
        self.retries = retries

    # -- low level --------------------------------------------------------- #
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 4000,
        json_mode: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> str:
        provider = self.provider
        payload: dict[str, Any] = {
            "model": provider.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode and provider.supports_json_mode:
            payload["response_format"] = {"type": "json_object"}
        if extra:
            payload.update(extra)

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {provider.api_key}",
            "User-Agent": "clipper/1.0",
            **provider.extra_headers,
        }

        try:
            data = _post_json(provider.chat_url, payload, headers, self.timeout, self.retries)
        except LLMError as exc:
            # Some providers/models reject response_format; retry once without it.
            if json_mode and "response_format" in str(exc):
                log.info("provider rejected response_format, retrying in plain mode")
                payload.pop("response_format", None)
                data = _post_json(provider.chat_url, payload, headers, self.timeout, self.retries)
            else:
                raise

        return _content_from_response(data)

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 4000,
        json_mode: bool = False,
    ) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return self.chat(
            messages, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode
        )

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 4000,
    ) -> Any:
        text = self.complete(
            system, user, temperature=temperature, max_tokens=max_tokens, json_mode=True
        )
        parsed = extract_json(text)
        if parsed is None:
            # One repair attempt: ask the model to re-emit pure JSON.
            log.info("could not parse JSON from model output, asking for a repair")
            repair = self.complete(
                "You fix malformed JSON. Reply with valid JSON only, no prose, no code fences.",
                "Return the same data as strict JSON:\n\n" + text[:12000],
                temperature=0.0,
                max_tokens=max_tokens,
            )
            parsed = extract_json(repair)
        if parsed is None:
            raise LLMError("model did not return parseable JSON")
        return parsed


def _content_from_response(data: dict[str, Any]) -> str:
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"unexpected LLM response shape: {str(data)[:400]}") from exc

    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in (None, "text"):
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    if content is None:
        # Some reasoning models put everything in `reasoning_content` only.
        return str(message.get("reasoning_content") or "")
    return str(content)


# --------------------------------------------------------------------------- #
# JSON extraction
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str | None) -> Any | None:
    """Pull the first JSON object/array out of a model reply.

    Tolerates code fences, prose before/after, trailing commas and smart quotes.
    Returns ``None`` when nothing parses.
    """
    if not text:
        return None

    base: list[str] = [text.strip()]
    base.extend(match.strip() for match in _FENCE_RE.findall(text))

    candidates: list[str] = []
    for candidate in base:
        candidates.append(candidate)
        repaired = _repair_json(candidate)
        if repaired != candidate:
            candidates.append(repaired)

    decoder = json.JSONDecoder()
    best: Any | None = None
    best_end = -1

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            pass
        for index, char in enumerate(candidate):
            if char not in "[{":
                continue
            try:
                value, end = decoder.raw_decode(candidate, index)
            except json.JSONDecodeError:
                continue
            # Prefer the widest span: the outer object beats a nested one when
            # the reply wrapped the JSON in prose.
            if isinstance(value, (dict, list)) and end > best_end:
                best, best_end = value, end
    return best


def _repair_json(text: str) -> str:
    fixed = (
        text.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u00a0", " ")
    )
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    return fixed


def summarise_provider(provider: Provider | None) -> str:
    if provider is None:
        return "no LLM provider configured (heuristic mode)"
    return f"{provider.name} · {provider.model} · {provider.base_url}"


def env_has_llm_key() -> bool:
    """True when any supported provider key is present in the environment."""
    if env_str("OPENAI_API_KEY") and env_str("OPENAI_BASE_URL"):
        return True
    if env_str("OPENAI_API_KEY"):
        return True
    return any(env_str(env_key) for env_key, _, _ in _PROVIDER_TABLE.values())


def redact(text: str) -> str:
    """Best-effort secret scrubbing for logs."""
    for name in os.environ:
        if not name.endswith(("_API_KEY", "_TOKEN", "_KEY")):
            continue
        value = os.environ.get(name) or ""
        if len(value) > 8:
            text = text.replace(value, "***")
    return text
