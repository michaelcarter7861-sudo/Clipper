from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from clipper.bridge import Bridge, _StreamTranslator, translate_request, translate_response
from clipper.llm import Provider


# --------------------------------------------------------------------------- #
# pure translation
# --------------------------------------------------------------------------- #
def test_translate_request_basics():
    payload = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 1024,
        "temperature": 0.2,
        "stop_sequences": ["</done>"],
        "system": [{"type": "text", "text": "be brief", "cache_control": {"type": "ephemeral"}}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "calling a tool"},
                    {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"path": "a.txt"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "file body"}]},
                    {"type": "text", "text": "now summarise"},
                ],
            },
        ],
    }
    result = translate_request(payload, "fallback-model")

    assert result["model"] == "claude-sonnet-4-5"
    assert result["max_tokens"] == 1024
    assert result["stop"] == ["</done>"]
    assert result["messages"][0] == {"role": "system", "content": "be brief"}
    assert result["messages"][1]["content"] == "hello"

    assistant = result["messages"][2]
    assert assistant["tool_calls"][0]["function"]["name"] == "Read"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"path": "a.txt"}

    tool_message = result["messages"][3]
    assert tool_message == {"role": "tool", "tool_call_id": "toolu_1", "content": "file body"}
    assert result["messages"][4]["content"] == "now summarise"


def test_translate_request_tools_and_choice():
    payload = {
        "system": "sys",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"name": "Read", "description": "read file", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
            {"name": "Write", "description": "write", "input_schema": {"type": "object"}},
        ],
        "tool_choice": {"type": "any"},
    }
    result = translate_request(payload, "m")
    assert [tool["function"]["name"] for tool in result["tools"]] == ["Read", "Write"]
    assert result["tools"][0]["type"] == "function"
    assert result["tool_choice"] == "required"
    assert result["messages"][0] == {"role": "system", "content": "sys"}


def test_translate_request_defaults_model():
    assert translate_request({"messages": []}, "default")["model"] == "default"


def test_mid_conversation_system_messages_are_downgraded():
    payload = {
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "system", "content": "reminder: stay on task"},
        ]
    }
    result = translate_request(payload, "m")
    assert result["messages"][2]["role"] == "user"
    assert "<system-note>" in result["messages"][2]["content"]


def test_leading_system_message_is_kept():
    payload = {"messages": [{"role": "system", "content": "you are helpful"}, {"role": "user", "content": "hi"}]}
    result = translate_request(payload, "m")
    assert result["messages"][0] == {"role": "system", "content": "you are helpful"}


def test_translate_response_text():
    data = {
        "choices": [{"message": {"content": "hello world"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }
    result = translate_response(data, "model-x")
    assert result["type"] == "message"
    assert result["content"] == [{"type": "text", "text": "hello world"}]
    assert result["stop_reason"] == "end_turn"
    assert result["usage"] == {"input_tokens": 11, "output_tokens": 7}


def test_translate_response_tool_use():
    data = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "Read", "arguments": '{"path":"a"}'}}
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    result = translate_response(data, "m")
    block = result["content"][0]
    assert block["type"] == "tool_use"
    assert block["input"] == {"path": "a"}
    assert result["stop_reason"] == "tool_use"


def test_translate_response_bad_json_arguments():
    data = {
        "choices": [
            {
                "message": {"tool_calls": [{"id": "c", "function": {"name": "X", "arguments": "{oops"}}]},
                "finish_reason": "tool_calls",
            }
        ]
    }
    assert translate_response(data, "m")["content"][0]["input"]["_raw"] == "{oops"


# --------------------------------------------------------------------------- #
# streaming translator
# --------------------------------------------------------------------------- #
def test_stream_translator_text_then_tools():
    translator = _StreamTranslator("m")
    events = b"".join(translator.start()).decode()
    assert "message_start" in events

    events = b"".join(
        translator.feed({"choices": [{"delta": {"content": "Hel"}}]})
    ).decode()
    assert "content_block_start" in events
    assert '"text": "Hel"' in events

    events = b"".join(translator.feed({"choices": [{"delta": {"content": "lo"}}]})).decode()
    assert "content_block_delta" in events

    translator.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "call_9", "function": {"name": "Write", "arguments": '{"a"'}}
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )
    translator.feed(
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ":1}"}}]}}]}
    )

    final = b"".join(translator.finish()).decode()
    assert "tool_use" in final
    assert "input_json_delta" in final
    assert '"partial_json": "{\\"a\\":1}"' in final
    assert '"stop_reason": "tool_use"' in final
    assert "message_stop" in final
    assert final.count("event: content_block_stop") == 2


# --------------------------------------------------------------------------- #
# end-to-end against a fake upstream
# --------------------------------------------------------------------------- #
class FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    mode = "text"
    received: list[dict] = []

    def log_message(self, *args):  # noqa: A003
        pass

    def do_POST(self):  # noqa: N802
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        FakeUpstream.received.append(payload)

        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in self._chunks():
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        body = json.dumps(self._response()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _chunks(self):
        if FakeUpstream.mode == "tool":
            return [
                {"choices": [{"delta": {"content": "reading"}}]},
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {"name": "Read", "arguments": '{"path":"t.txt"}'},
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            ]
        return [
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"content": " from upstream"}, "finish_reason": "stop"}]},
        ]

    def _response(self):
        if FakeUpstream.mode == "tool":
            return {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {"id": "call_1", "function": {"name": "Read", "arguments": '{"path":"t.txt"}'}}
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        return {
            "choices": [{"message": {"content": "Hello from upstream"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }


@pytest.fixture()
def upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    FakeUpstream.received = []
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


@pytest.fixture()
def bridge(upstream):
    provider = Provider("fake", upstream, "secret", "fake-model")
    instance = Bridge(provider, port=0, token="local-token").start()
    yield instance
    instance.stop()


def post(url: str, payload: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-api-key": "local-token"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.status, response.read()


def test_bridge_health_and_models(bridge):
    with urllib.request.urlopen(bridge.base_url + "/healthz", timeout=10) as response:
        assert json.loads(response.read())["ok"] is True
    with urllib.request.urlopen(bridge.base_url + "/v1/models", timeout=10) as response:
        payload = json.loads(response.read())
        assert payload["data"][0]["id"] == "fake-model"


def test_bridge_non_stream(bridge, upstream):
    FakeUpstream.mode = "text"
    status, body = post(
        bridge.base_url + "/v1/messages",
        {
            "model": "claude-sonnet-4-5",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["content"][0]["text"] == "Hello from upstream"
    assert payload["stop_reason"] == "end_turn"
    assert FakeUpstream.received[-1]["messages"][0]["content"] == "hi"
    assert FakeUpstream.received[-1]["stream"] is False


def test_bridge_stream(bridge):
    FakeUpstream.mode = "text"
    request = urllib.request.Request(
        bridge.base_url + "/v1/messages",
        data=json.dumps(
            {
                "model": "claude-sonnet-4-5",
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            }
        ).encode(),
        headers={"Content-Type": "application/json", "x-api-key": "local-token"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read().decode()

    assert "event: message_start" in raw
    assert "event: content_block_delta" in raw
    assert '"text": "Hello"' in raw
    assert '"text": " from upstream"' in raw
    assert "event: message_stop" in raw
    assert FakeUpstream.received[-1]["stream"] is True


def test_bridge_stream_tool_use(bridge):
    FakeUpstream.mode = "tool"
    request = urllib.request.Request(
        bridge.base_url + "/v1/messages",
        data=json.dumps(
            {
                "model": "m",
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "read t.txt"}],
                "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
            }
        ).encode(),
        headers={"Content-Type": "application/json", "x-api-key": "local-token"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read().decode()

    assert '"type": "tool_use"' in raw
    assert '"name": "Read"' in raw
    assert '"stop_reason": "tool_use"' in raw
    assert FakeUpstream.received[-1]["tools"][0]["function"]["name"] == "Read"


def test_bridge_rejects_bad_token(bridge):
    request = urllib.request.Request(
        bridge.base_url + "/v1/messages",
        data=b"{}",
        headers={"Content-Type": "application/json", "x-api-key": "wrong"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=10)
    assert excinfo.value.code == 401


def test_bridge_count_tokens(bridge):
    status, body = post(
        bridge.base_url + "/v1/messages/count_tokens",
        {"messages": [{"role": "user", "content": "a" * 40}]},
    )
    assert status == 200
    assert json.loads(body)["input_tokens"] >= 10


def test_bridge_upstream_error_is_502(bridge, monkeypatch):
    def boom(payload):
        raise RuntimeError("upstream is down")

    monkeypatch.setattr(bridge, "call_upstream", boom)
    request = urllib.request.Request(
        bridge.base_url + "/v1/messages",
        data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
        headers={"Content-Type": "application/json", "x-api-key": "local-token"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=10)
    assert excinfo.value.code == 502
