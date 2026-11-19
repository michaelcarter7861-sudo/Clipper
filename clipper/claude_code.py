"""Headless Claude Code as the pipeline's editor-in-chief.

Claude Code gets a brief, the transcript file and a video file, and writes
``clips.json``. It talks to whatever ``ANTHROPIC_BASE_URL`` points at, so the
free OpenAI-compatible pool (via :mod:`clipper.bridge`) works too.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .llm import extract_json
from .log import get_logger, warning
from .moments import ClipPlan, validate_plans
from .prompts import DIRECTOR_BRIEF
from .subtitles import Transcript

log = get_logger()

BINARY = "claude"


@dataclass
class DirectorResult:
    clips: list[ClipPlan]
    brief_path: Path
    log_path: Path | None
    exit_code: int
    stdout: str = ""


def claude_binary() -> str | None:
    return shutil.which(BINARY)


def is_available() -> bool:
    return claude_binary() is not None


def build_env(base_url: str, model: str, token: str = "local") -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "ANTHROPIC_BASE_URL": base_url.rstrip("/"),
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_SMALL_FAST_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_UPDATE_CHECK": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "DISABLE_AUTOUPDATER": "1",
            "CI": "true",
            "TERM": "dumb",
            "NO_COLOR": "1",
        }
    )
    return env


def write_brief(
    path: Path,
    *,
    transcript_path: Path,
    clips_path: Path,
    video_path: Path,
    title: str,
    duration: float,
    language: str | None,
    settings: Settings,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        DIRECTOR_BRIEF.format(
            clips_path=clips_path,
            transcript_path=transcript_path,
            video_path=video_path,
            title=title,
            duration=duration,
            language=language or "unknown",
            count=settings.clips,
            min_duration=settings.min_duration,
            max_duration=settings.max_duration,
        ),
        encoding="utf-8",
    )
    return path


def build_command(brief: str, settings: Settings) -> list[str]:
    binary = claude_binary() or BINARY
    return [
        binary,
        "-p",
        brief,
        "--output-format",
        "text",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        "Read,Write",
        "--no-session-persistence",
        "--model",
        settings.claude_model,
    ]


def run_director(
    transcript: Transcript,
    *,
    transcript_path: Path,
    video_path: Path,
    work_dir: Path,
    title: str,
    duration: float,
    settings: Settings,
    base_url: str,
    token: str = "local",
) -> DirectorResult | None:
    """Ask Claude Code to pick the clips. Returns ``None`` if nothing usable came back."""
    if not is_available():
        warning(
            "Claude Code CLI (`claude`) was not found — install it with "
            "`npm install -g @anthropic-ai/claude-code`, or use LLM_BACKEND=api."
        )
        return None

    clips_path = work_dir / "clips.json"
    transcript_path = transcript_path
    brief_path = write_brief(
        work_dir / "assignment.md",
        transcript_path=transcript_path,
        clips_path=clips_path,
        video_path=video_path,
        title=title,
        duration=duration,
        language=transcript.language,
        settings=settings,
    )
    brief = brief_path.read_text(encoding="utf-8")
    command = build_command(brief, settings)
    log_path = work_dir / "claude-code.log"
    env = build_env(base_url, settings.claude_model, token)

    log.info("running Claude Code headless (model %s via %s)", settings.claude_model, base_url)
    log.info("brief: %s", brief_path)

    stdout_lines: list[str] = []
    exit_code = -1
    timed_out = False

    try:
        process = subprocess.Popen(
            command,
            cwd=work_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        warning(f"could not start Claude Code: {exc}")
        return None

    def _kill() -> None:
        nonlocal timed_out
        timed_out = True
        try:
            process.kill()
        except OSError:
            pass

    timer = threading.Timer(max(60, settings.claude_timeout), _kill)
    timer.start()
    try:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            if not line:
                continue
            stdout_lines.append(line)
            log.debug("claude-code ▸ %s", line[:400])
            print(f"[claude] {line[:400]}", flush=True)
        exit_code = process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        _kill()
        exit_code = -1
    finally:
        timer.cancel()

    log_path.write_text("\n".join(stdout_lines) + "\n", encoding="utf-8")

    if timed_out:
        warning(f"Claude Code hit the {settings.claude_timeout}s timeout")
        return None
    if exit_code != 0:
        warning(f"Claude Code exited with {exit_code}; see {log_path}")

    payload = None
    if clips_path.exists():
        try:
            payload = json.loads(clips_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = extract_json(clips_path.read_text(encoding="utf-8", errors="replace"))
    if payload is None:
        payload = extract_json("\n".join(stdout_lines))
    if payload is None:
        warning("Claude Code did not produce a usable clips.json")
        return None

    items = payload.get("clips") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        warning("clips.json did not contain a 'clips' list")
        return None

    clips = validate_plans(
        [item for item in items if isinstance(item, dict)],
        duration=duration,
        settings=settings,
        transcript=transcript,
        source="claude-code",
        limit=settings.clips,
    )
    if not clips:
        warning("Claude Code's clips failed validation (timings out of range?)")
        return None

    log.info("Claude Code picked %d clip(s)", len(clips))
    return DirectorResult(
        clips=clips,
        brief_path=brief_path,
        log_path=log_path,
        exit_code=exit_code,
        stdout="\n".join(stdout_lines),
    )
