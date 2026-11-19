"""Claude Code headless, end to end, against a mock LLM behind the bridge.

This is the integration test for the risky part of the pipeline: a real
`claude` process talking Anthropic's wire format to our OpenAI bridge, using
Read/Write tools to produce clips.json. It skips when the CLI is absent.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from clipper.bridge import Bridge
from clipper.claude_code import build_command, build_env, is_available, run_director, write_brief
from clipper.config import Settings
from clipper.llm import Provider
from clipper.subtitles import Segment, Transcript, Word

pytestmark = pytest.mark.claude

CLIPS_PAYLOAD = {
    "clips": [
        {
            "start": 2.0,
            "end": 22.0,
            "hook": "the money secret",
            "title": "money",
            "reason": "specific and hooky",
            "score": 88,
        }
    ]
}


class MockLLM(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    requests: list[dict] = []

    def log_message(self, *args):  # noqa: A003
        pass

    def do_POST(self):  # noqa: N802
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        MockLLM.requests.append(payload)
        tool_results = [m for m in payload.get("messages", []) if m.get("role") == "tool"]

        call = None
        if len(tool_results) == 0:
            call = {"id": "call_read", "name": "Read", "args": {"file_path": "transcript.json"}}
        elif len(tool_results) == 1:
            call = {
                "id": "call_write",
                "name": "Write",
                "args": {"file_path": "clips.json", "content": json.dumps(CLIPS_PAYLOAD)},
            }

        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def emit(chunk: dict) -> None:
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                self.wfile.flush()

            if call is None:
                emit({"choices": [{"delta": {"content": "done"}}]})
                emit({"choices": [{"delta": {}, "finish_reason": "stop"}]})
            else:
                emit({"choices": [{"delta": {"content": f"using {call['name']}"}}]})
                emit(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": call["id"],
                                            "function": {
                                                "name": call["name"],
                                                "arguments": json.dumps(call["args"]),
                                            },
                                        }
                                    ]
                                },
                                "finish_reason": "tool_calls",
                            }
                        ]
                    }
                )
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        if call is None:
            body = {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}
        else:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": call["id"],
                                    "type": "function",
                                    "function": {
                                        "name": call["name"],
                                        "arguments": json.dumps(call["args"]),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def mock_llm_url():
    MockLLM.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockLLM)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


@pytest.fixture()
def bridge(mock_llm_url):
    instance = Bridge(Provider("mock", mock_llm_url, "k", "mock-model"), port=0).start()
    yield instance
    instance.stop()


def make_transcript() -> Transcript:
    words = [Word(t, t + 1.0, f"word{int(t)}") for t in range(0, 30, 2)]
    return Transcript(
        segments=[Segment(start=0, end=30, text="the money secret is patience, honestly", words=words)],
        language="en",
        duration=30,
    )


def test_build_env_points_at_the_local_bridge():
    env = build_env("http://127.0.0.1:8787", "my-model", "tok")
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8787"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "tok"
    assert env["ANTHROPIC_MODEL"] == "my-model"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["CI"] == "true"


def test_build_command_flags():
    command = build_command("brief text", Settings(claude_model="sonnet"))
    assert command[0].endswith("claude") or command[0] == "claude"
    assert "-p" in command and "brief text" in command
    assert "--allowedTools" in command
    assert command[command.index("--allowedTools") + 1] == "Read,Write"
    assert command[command.index("--model") + 1] == "sonnet"
    assert "--no-session-persistence" in command


def test_write_brief_mentions_every_input(tmp_path: Path):
    path = write_brief(
        tmp_path / "assignment.md",
        transcript_path=tmp_path / "transcript.json",
        clips_path=tmp_path / "clips.json",
        video_path=tmp_path / "source.mp4",
        title="a video",
        duration=120.0,
        language="en",
        settings=Settings(clips=4, min_duration=15, max_duration=45),
    )
    brief = path.read_text()
    assert "transcript.json" in brief and "clips.json" in brief
    assert "a video" in brief
    assert "Exactly 4 clips" in brief
    assert "15s" in brief and "45s" in brief


def test_run_director_end_to_end(tmp_path: Path, bridge):
    if not is_available():
        pytest.skip("Claude Code CLI is not installed")

    work = tmp_path / "work"
    work.mkdir()
    transcript = make_transcript()
    transcript_path = work / "transcript.json"
    transcript.save(transcript_path)
    (work / "source.mp4").write_bytes(b"not really a video")

    settings = Settings(
        clips=1,
        min_duration=10,
        max_duration=40,
        claude_timeout=240,
        claude_model="mock-model",
        llm_backend="claude-code",
    )
    result = run_director(
        transcript,
        transcript_path=transcript_path,
        video_path=work / "source.mp4",
        work_dir=work,
        title="a video",
        duration=30.0,
        settings=settings,
        base_url=bridge.base_url,
    )

    assert result is not None, "Claude Code run produced nothing"
    assert (work / "clips.json").exists()
    assert result.exit_code == 0
    assert len(result.clips) == 1
    clip = result.clips[0]
    assert clip.hook == "the money secret"
    assert clip.score == 88
    assert clip.source == "claude-code"
    assert clip.start >= 0 and clip.end <= 30

    # The agent used Read then Write, and the bridge translated both.
    assert len(MockLLM.requests) >= 3
    roles = [[message["role"] for message in request["messages"]] for request in MockLLM.requests]
    assert any("tool" in role_list for role_list in roles)
    assert any(request.get("stream") for request in MockLLM.requests)
    tool_names = [tool["function"]["name"] for tool in MockLLM.requests[0]["tools"]]
    assert "Read" in tool_names and "Write" in tool_names
