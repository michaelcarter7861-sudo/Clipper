"""FFmpeg rendering: vertical reframe, captions burn-in, loudness, encode."""

from __future__ import annotations

import functools
import json
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .captions import caption_filters, write_ass
from .config import Settings
from .log import get_logger
from .moments import ClipPlan
from .subtitles import Transcript

log = get_logger()


class RenderError(RuntimeError):
    pass


@dataclass
class RenderResult:
    path: Path
    start: float
    end: float
    command: list[str]
    thumbnail: Path | None = None
    elapsed: float = 0.0


# --------------------------------------------------------------------------- #
# binaries
# --------------------------------------------------------------------------- #
def resolve_binary(name: str) -> str:
    if not name:
        return ""
    found = shutil.which(name)
    if found:
        return found
    if Path(name).exists():
        return str(Path(name))
    return ""


@functools.lru_cache(maxsize=4)
def ffmpeg_filters(exe: str) -> frozenset[str]:
    try:
        result = subprocess.run(
            [exe, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    names = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and not line.startswith("Filters:"):
            names.add(parts[1])
    return frozenset(names)


def doctor(settings: Settings) -> list[str]:
    """Return human-readable problems with the ffmpeg installation."""
    problems: list[str] = []
    exe = resolve_binary(settings.ffmpeg)
    if not exe:
        return [
            "ffmpeg was not found. Install it (macOS: brew install ffmpeg, "
            "Ubuntu: sudo apt-get install -y ffmpeg) or set FFMPEG_BIN."
        ]
    filters = ffmpeg_filters(exe)
    for needed in ("ass", "scale", "crop", "loudnorm", "fade"):
        if filters and needed not in filters:
            problems.append(f"this ffmpeg build lacks the '{needed}' filter")
    return problems


def has_audio(ffprobe: str, path: Path) -> bool:
    exe = resolve_binary(ffprobe)
    if not exe:
        return True
    try:
        result = subprocess.run(
            [
                exe,
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return True  # not really ffprobe: let ffmpeg decide
        payload = json.loads(result.stdout or "{}")
        return bool(payload.get("streams"))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return True


def probe_duration(ffprobe: str, path: Path) -> float | None:
    exe = resolve_binary(ffprobe)
    if not exe:
        return None
    try:
        result = subprocess.run(
            [
                exe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return float(result.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# filter graph
# --------------------------------------------------------------------------- #
def _even(value: float) -> int:
    return int(value) // 2 * 2


def _base_video_chain(settings: Settings) -> str:
    """Reframe step as filter-graph statements, ending on an unlabelled node."""
    width, height = _even(settings.width), _even(settings.height)
    if settings.fit == "blur":
        return (
            "[0:v]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma=32,eq=brightness=-0.06[bgtmp];"
            f"[fgsrc]scale={width}:{height}:force_original_aspect_ratio=decrease[fgtmp];"
            "[bgtmp][fgtmp]overlay=(W-w)/2:(H-h)/2"
        )
    if settings.fit == "pad":
        return (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    return (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}"
    )


def build_filter_complex(
    settings: Settings,
    *,
    duration: float,
    ass_filter: str | None,
    with_audio: bool = True,
) -> str:
    video = _base_video_chain(settings)
    video += ",setsar=1"
    video += f",fps={settings.fps}"
    video += ",fade=t=in:st=0:d=0.20"
    if duration > 0.8:
        video += f",fade=t=out:st={max(0.0, duration - 0.30):.2f}:d=0.30"
    if ass_filter:
        video += f",{ass_filter}"
    video += "[v]"

    graph = video

    if with_audio:
        audio = "[0:a]"
        if settings.denoise:
            audio += "afftdn=nf=-25,"
        if settings.loudness:
            audio += "loudnorm=I=-16:TP=-1.5:LRA=11,"
        audio += "aresample=48000,"
        audio += "afade=t=in:st=0:d=0.15"
        if duration > 0.8:
            audio += f",afade=t=out:st={max(0.0, duration - 0.25):.2f}:d=0.25"
        audio += "[a]"
        graph += ";" + audio

    return graph


def build_command(
    settings: Settings,
    *,
    source: Path,
    out_path: Path,
    start: float,
    end: float,
    ass_path: Path | None = None,
    with_audio: bool = True,
) -> list[str]:
    duration = max(0.5, end - start)
    exe = resolve_binary(settings.ffmpeg) or settings.ffmpeg
    ass_filter = caption_filters(ass_path) if ass_path else None

    command = [
        exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(source),
        "-t",
        f"{duration:.3f}",
        "-filter_complex",
        build_filter_complex(
            settings, duration=duration, ass_filter=ass_filter, with_audio=with_audio
        ),
        "-map",
        "[v]",
    ]
    if with_audio:
        command += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
    else:
        command += ["-an"]

    command += [
        "-c:v",
        "libx264",
        "-preset",
        settings.preset,
        "-crf",
        str(settings.crf),
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "high",
        "-r",
        str(settings.fps),
        "-movflags",
        "+faststart",
        "-max_muxing_queue_size",
        "1024",
        "-shortest",
        str(out_path),
    ]
    return command


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def render_clip(
    source: Path,
    out_path: Path,
    plan: ClipPlan,
    transcript: Transcript,
    settings: Settings,
    *,
    ass_dir: Path | None = None,
    dry_run: bool = False,
    timeout: int = 2400,
) -> RenderResult:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path: Path | None = None

    if settings.caption_style != "none":
        ass_path = (ass_dir or out_path.parent) / f"{out_path.stem}.ass"
        write_ass(
            ass_path,
            transcript,
            plan.start,
            plan.end,
            settings,
            hook=plan.hook,
            clip_duration=plan.duration,
        )

    audio = has_audio(settings.ffprobe, source)
    command = build_command(
        settings,
        source=source,
        out_path=out_path,
        start=plan.start,
        end=plan.end,
        ass_path=ass_path,
        with_audio=audio,
    )

    if dry_run:
        log.info("dry run: %s", " ".join(shlex.quote(part) for part in command))
        return RenderResult(path=out_path, start=plan.start, end=plan.end, command=command)

    log.info(
        "rendering clip %d (%.1fs → %.1fs, %.1fs long)",
        plan.index,
        plan.start,
        plan.end,
        plan.duration,
    )
    started = time.time()
    result = _run(command, timeout=timeout)
    if result.returncode != 0 and audio:
        log.warning("render failed with audio, retrying without an audio track")
        command = build_command(
            settings,
            source=source,
            out_path=out_path,
            start=plan.start,
            end=plan.end,
            ass_path=ass_path,
            with_audio=False,
        )
        result = _run(command, timeout=timeout)

    if result.returncode != 0:
        raise RenderError(
            f"ffmpeg failed for clip {plan.index} (exit {result.returncode}): "
            f"{result.stderr.strip()[:600]}"
        )
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RenderError(f"ffmpeg produced no output for clip {plan.index}")

    elapsed = time.time() - started
    thumbnail = make_thumbnail(settings, out_path, out_path.with_name(f"{out_path.stem}.jpg"))
    log.info(
        "clip %d done in %.1fs (%.1f MB)",
        plan.index,
        elapsed,
        out_path.stat().st_size / 1e6,
    )
    return RenderResult(
        path=out_path,
        start=plan.start,
        end=plan.end,
        command=command,
        thumbnail=thumbnail,
        elapsed=elapsed,
    )


def make_thumbnail(settings: Settings, clip: Path, out: Path) -> Path | None:
    exe = resolve_binary(settings.ffmpeg)
    if not exe:
        return None
    command = [
        exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        "1.0",
        "-i",
        str(clip),
        "-frames:v",
        "1",
        "-vf",
        "scale=540:-2",
        "-q:v",
        "3",
        str(out),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        return out if result.returncode == 0 and out.exists() else None
    except (OSError, subprocess.SubprocessError):
        return None


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    log.debug("ffmpeg: %s", " ".join(shlex.quote(part) for part in command))
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"ffmpeg timed out after {timeout}s") from exc
    except OSError as exc:
        raise RenderError(f"could not run ffmpeg: {exc}") from exc
