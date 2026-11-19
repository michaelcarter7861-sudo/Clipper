"""Anthropic Messages API → OpenAI Chat Completions bridge.

Claude Code speaks Anthropic's wire format. Free LLM pools (Groq, Google AI
Studio, Cerebras, OpenRouter, a FreeLLMAPI router, …) speak OpenAI's. This
module is the translator, so `claude` can run headless in CI without an
Anthropic subscription.

Run it standalone:

    python -m clipper.bridge --port 8787
    ANTHROPIC_BASE_URL=http://127.0.0.1:8787 ANTHROPIC_AUTH_TOKEN=local claude
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .llm import Provider, resolve_provider
from .log import get_logger

log = get_logger()

STOP_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
    "stop_sequence": "stop_sequence",
}


# --------------------------------------------------------------------------- #
# request translation
# --------------------------------------------------------------------------- #
def _system_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in (None, "text"):
                parts.append(str(block.get("text", "")))
        return "\n\n".join(part for part in parts if part)
    return ""


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "image":
                    parts.append("[image omitted]")
                else:
                    parts.append(json.dumps(block)[:2000])
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return str(content)


def _translate_tools(payload: dict[str, Any]) -> list[dict[str, Any]]:
    tools = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name") or tool.get("type")
        if not name:
            continue
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", "") or "",
                    "parameters": tool.get("input_schema")
                    or {"type": "object", "properties": {}},
                },
            }
        )
    return tools


def _translate_tool_choice(payload: dict[str, Any]) -> Any | None:
    choice = payload.get("tool_choice")
    if not isinstance(choice, dict):
        return None
    kind = choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool" and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}
    return None


def translate_request(payload: dict[str, Any], default_model: str) -> dict[str, Any]:
    """Build an OpenAI chat-completions payload from an Anthropic request."""
    messages: list[dict[str, Any]] = []

    system = _system_text(payload.get("system"))
    if system:
        messages.append({"role": "system", "content": system})

    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")

        # Only the leading message may be a system message for strict providers
        # (Groq rejects anything else), so downgrade the mid-chat ones.
        if role == "system" and messages:
            role = "user"
            prefix = "<system-note>\n"
            if isinstance(content, str):
                content = prefix + content + "\n</system-note>"

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        texts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_messages: list[dict[str, Any]] = []
        images = 0

        for block in content or []:
            if isinstance(block, str):
                texts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                texts.append(str(block.get("text", "")))
            elif kind == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id") or f"toolu_{uuid.uuid4().hex[:12]}",
                        "type": "function",
                        "function": {
                            "name": block.get("name", "tool"),
                            "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                        },
                    }
                )
            elif kind == "tool_result":
                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id") or "unknown",
                        "content": _tool_result_text(block.get("content")),
                    }
                )
            elif kind == "image":
                images += 1
            # thinking / redacted_thinking blocks are intentionally dropped

        if role == "assistant":
            message_out: dict[str, Any] = {"role": "assistant"}
            joined = "\n".join(text for text in texts if text).strip()
            if joined:
                message_out["content"] = joined
            else:
                message_out["content"] = None
            if tool_calls:
                message_out["tool_calls"] = tool_calls
                message_out["content"] = joined or None
            messages.append(message_out)
        else:
            messages.extend(tool_messages)
            joined = "\n".join(text for text in texts if text).strip()
            if joined:
                messages.append({"role": "user", "content": joined})
            elif images and not tool_messages:
                messages.append({"role": "user", "content": "[image omitted]"})

    openai_payload: dict[str, Any] = {
        "model": payload.get("model") or default_model,
        "messages": messages,
        "stream": bool(payload.get("stream")),
    }

    max_tokens = payload.get("max_tokens")
    if isinstance(max_tokens, (int, float)) and max_tokens > 0:
        openai_payload["max_tokens"] = int(max_tokens)
    for key in ("temperature", "top_p"):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            openai_payload[key] = value
    stop = payload.get("stop_sequences")
    if stop:
        openai_payload["stop"] = stop

    tools = _translate_tools(payload)
    if tools:
        openai_payload["tools"] = tools
        choice = _translate_tool_choice(payload)
        if choice is not None:
            openai_payload["tool_choice"] = choice

    return openai_payload


# --------------------------------------------------------------------------- #
# response translation
# --------------------------------------------------------------------------- #
def _tool_input(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw[:4000]}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def translate_response(data: dict[str, Any], model: str) -> dict[str, Any]:
    """Build an Anthropic Messages response from an OpenAI chat completion."""
    choice = (data.get("choices") or [{}])[0] or {}
    message = choice.get("message") or {}
    text = message.get("content")

    content: list[dict[str, Any]] = []
    if isinstance(text, str) and text.strip():
        content.append({"type": "text", "text": text})
    elif isinstance(text, list):
        joined = "".join(
            str(block.get("text", "")) for block in text if isinstance(block, dict)
        )
        if joined.strip():
            content.append({"type": "text", "text": joined})

    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        content.append(
            {
                "type": "tool_use",
                "id": call.get("id") or f"toolu_{uuid.uuid4().hex[:12]}",
                "name": function.get("name", "tool"),
                "input": _tool_input(function.get("arguments")),
            }
        )

    if not content:
        content = [{"type": "text", "text": ""}]

    finish = choice.get("finish_reason") or "stop"
    stop_reason = "tool_use" if any(block["type"] == "tool_use" for block in content) else STOP_REASON_MAP.get(finish, "end_turn")

    usage = data.get("usage") or {}
    return {
        "id": f"msg_{uuid.uuid4().hex[:20]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }


def _event(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def sse_message_start(model: str) -> bytes:
    return _event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": f"msg_{uuid.uuid4().hex[:20]}",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )


class _StreamTranslator:
    """Turns OpenAI stream deltas into Anthropic SSE events."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.index = 0
        self.text_open = False
        self.tools: dict[int, dict[str, Any]] = {}
        self.text_seen = False
        self.output_tokens = 0
        self.finish_reason = "stop"

    def start(self) -> list[bytes]:
        return [sse_message_start(self.model)]

    def feed(self, chunk: dict[str, Any]) -> list[bytes]:
        out: list[bytes] = []
        if chunk.get("usage"):
            self.output_tokens = int(chunk["usage"].get("completion_tokens") or self.output_tokens)
        choices = chunk.get("choices") or []
        if not choices:
            return out
        choice = choices[0] or {}
        delta = choice.get("delta") or {}
        if choice.get("finish_reason"):
            self.finish_reason = choice["finish_reason"]

        text = delta.get("content")
        if isinstance(text, list):
            text = "".join(str(b.get("text", "")) for b in text if isinstance(b, dict))
        if text:
            if not self.text_open:
                out.append(
                    _event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": self.index,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                )
                self.text_open = True
            self.text_seen = True
            out.append(
                _event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self.index,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )
            )

        for call in delta.get("tool_calls") or []:
            key = int(call.get("index") or 0)
            slot = self.tools.setdefault(
                key,
                {"id": call.get("id") or f"toolu_{uuid.uuid4().hex[:12]}", "name": "", "args": ""},
            )
            if call.get("id"):
                slot["id"] = call["id"]
            function = call.get("function") or {}
            if function.get("name"):
                slot["name"] = function["name"]
            if function.get("arguments"):
                slot["args"] += function["arguments"]
        return out

    def finish(self) -> list[bytes]:
        out: list[bytes] = []
        if self.text_open:
            out.append(
                _event(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": self.index},
                )
            )
            self.text_open = False
            self.index += 1

        for key in sorted(self.tools):
            slot = self.tools[key]
            out.append(
                _event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": self.index,
                        "content_block": {
                            "type": "tool_use",
                            "id": slot["id"],
                            "name": slot["name"] or "tool",
                            "input": {},
                        },
                    },
                )
            )
            out.append(
                _event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self.index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": slot["args"] or "{}",
                        },
                    },
                )
            )
            out.append(
                _event("content_block_stop", {"type": "content_block_stop", "index": self.index})
            )
            self.index += 1

        stop_reason = (
            "tool_use"
            if self.tools
            else STOP_REASON_MAP.get(self.finish_reason, "end_turn")
        )
        out.append(
            _event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": self.output_tokens},
                },
            )
        )
        out.append(_event("message_stop", {"type": "message_stop"}))
        return out


# --------------------------------------------------------------------------- #
# server
# --------------------------------------------------------------------------- #
class Bridge:
    """A local HTTP server exposing the Anthropic Messages API."""

    def __init__(
        self,
        provider: Provider,
        port: int = 0,
        token: str | None = None,
        timeout: float = 600.0,
        host: str = "127.0.0.1",
    ) -> None:
        self.provider = provider
        self.token = token
        self.timeout = timeout
        self.model = provider.model
        handler = _make_handler(self)
        self.httpd = ThreadingHTTPServer((host, int(port)), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.started = False

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> Bridge:
        if not self.started:
            self.thread.start()
            self.started = True
            log.info("Claude bridge listening on %s → %s (%s)", self.base_url, self.provider.base_url, self.provider.model)
        return self

    def stop(self) -> None:
        if self.started:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.started = False

    # -- upstream calls ---------------------------------------------------- #
    def _upstream_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.provider.api_key}",
            "User-Agent": "clipper-bridge/1.0",
            **self.provider.extra_headers,
        }

    def call_upstream(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.provider.chat_url, data=body, headers=self._upstream_headers(), method="POST"
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))

    def stream_upstream(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.provider.chat_url, data=body, headers=self._upstream_headers(), method="POST"
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue


def _make_handler(bridge: Bridge):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "clipper-bridge/1.0"

        # -- plumbing ------------------------------------------------------ #
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            log.debug("bridge: " + fmt, *args)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                payload = {}
            return payload if isinstance(payload, dict) else {}

        def _authorised(self) -> bool:
            if not bridge.token:
                return True
            provided = (
                self.headers.get("x-api-key")
                or self.headers.get("authorization", "").removeprefix("Bearer ").strip()
            )
            return provided == bridge.token

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _send_error(self, message: str, status: int = 502) -> None:
            self._send_json(
                {"type": "error", "error": {"type": "api_error", "message": message}}, status
            )

        def _write_chunk(self, data: bytes) -> None:
            self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
            self.wfile.flush()

        # -- routes -------------------------------------------------------- #
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0].rstrip("/")
            if path in ("/healthz", "/health", ""):
                self._send_json(
                    {
                        "ok": True,
                        "provider": bridge.provider.name,
                        "model": bridge.model,
                        "upstream": bridge.provider.base_url,
                    }
                )
                return
            if path in ("/v1/models", "/models"):
                model = {
                    "id": bridge.model,
                    "type": "model",
                    "display_name": f"{bridge.provider.name}:{bridge.model}",
                    "created_at": "2026-01-01T00:00:00Z",
                }
                self._send_json(
                    {
                        "data": [model],
                        "has_more": False,
                        "first_id": model["id"],
                        "last_id": model["id"],
                    }
                )
                return
            self._send_error(f"unknown route GET {self.path}", 404)

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?")[0].rstrip("/")
            if not self._authorised():
                self._send_error("invalid api key", 401)
                return

            payload = self._read_json()

            if path.endswith("/count_tokens"):
                text = json.dumps(payload.get("messages") or []) + _system_text(payload.get("system"))
                self._send_json({"input_tokens": max(1, len(text) // 4)})
                return

            if not (path.endswith("/v1/messages") or path.endswith("/messages")):
                self._send_error(f"unknown route POST {self.path}", 404)
                return

            upstream_payload = translate_request(payload, bridge.model)
            try:
                if payload.get("stream"):
                    self._stream(upstream_payload)
                else:
                    data = bridge.call_upstream(upstream_payload)
                    self._send_json(translate_response(data, bridge.model))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:800]
                log.warning("upstream HTTP %s: %s", exc.code, detail)
                self._send_error(f"upstream provider returned {exc.code}: {detail}", 502)
            except Exception as exc:  # noqa: BLE001 - never crash the shim
                log.warning("bridge failure: %s", exc)
                self._send_error(f"bridge failure: {exc}", 502)

        # -- streaming ----------------------------------------------------- #
        def _stream(self, upstream_payload: dict[str, Any]) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            translator = _StreamTranslator(bridge.model)
            try:
                for event in translator.start():
                    self._write_chunk(event)
                for chunk in bridge.stream_upstream(upstream_payload):
                    for event in translator.feed(chunk):
                        self._write_chunk(event)
                for event in translator.finish():
                    self._write_chunk(event)
            except (BrokenPipeError, ConnectionResetError):
                log.debug("client disconnected mid-stream")
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("stream failed: %s", exc)
                try:
                    self._write_chunk(
                        _event(
                            "error",
                            {"type": "error", "error": {"type": "api_error", "message": str(exc)}},
                        )
                    )
                except OSError:
                    pass
            finally:
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:
                    pass

    return Handler


def start_bridge(
    provider: Provider | None = None,
    *,
    port: int = 0,
    token: str | None = None,
    timeout: float = 900.0,
) -> Bridge:
    provider = provider or resolve_provider()
    if provider is None:
        raise RuntimeError(
            "no LLM provider configured: set GROQ_API_KEY / GEMINI_API_KEY / "
            "OPENAI_API_KEY+OPENAI_BASE_URL / OPENROUTER_API_KEY"
        )
    return Bridge(provider, port=port, token=token, timeout=timeout).start()


def wait_until_ready(base_url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    url = base_url.rstrip("/") + "/healthz"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Anthropic→OpenAI bridge for Claude Code")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--token", default=None, help="optional bearer token to require")
    parser.add_argument("--model", default=None, help="override the upstream model id")
    args = parser.parse_args(argv)

    provider = resolve_provider()
    if provider is None:
        print(
            "No provider configured. Export one of GROQ_API_KEY, GEMINI_API_KEY, "
            "CEREBRAS_API_KEY, OPENROUTER_API_KEY, or OPENAI_API_KEY + OPENAI_BASE_URL.",
            flush=True,
        )
        return 2
    if args.model:
        provider.model = args.model

    bridge = start_bridge(provider, port=args.port, token=args.token)
    print(f"bridge: {bridge.base_url}  →  {provider.base_url}  ({provider.model})", flush=True)
    print(
        "use it with:\n"
        f"  export ANTHROPIC_BASE_URL={bridge.base_url}\n"
        f"  export ANTHROPIC_AUTH_TOKEN={args.token or 'local'}\n"
        f"  export ANTHROPIC_MODEL={provider.model}\n"
        "  claude",
        flush=True,
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        bridge.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
