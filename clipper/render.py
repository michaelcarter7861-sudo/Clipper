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
from .edit import EditPlan, build_edit_plan, retime_transcript, summarise
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
    edit: EditPlan | None = None
    output_duration: float = 0.0
    editing: str = ""


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


def _base_video_chain(settings: Settings, src: str = "[0:v]") -> str:
    """Reframe step as filter-graph statements, ending on an unlabelled node."""
    width, height = _even(settings.width), _even(settings.height)
    if settings.fit == "blur":
        return (
            f"{src}split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma=32,eq=brightness=-0.06[bgtmp];"
            f"[fgsrc]scale={width}:{height}:force_original_aspect_ratio=decrease[fgtmp];"
            "[bgtmp][fgtmp]overlay=(W-w)/2:(H-h)/2"
        )
    if settings.fit == "pad":
        return (
            f"{src}scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    return (
        f"{src}scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}"
    )


def _ff_color(value: str, alpha: float | None = None) -> str:
    raw = (value or "#FFFFFF").strip()
    if raw.startswith("#") and len(raw) == 7:
        raw = "0x" + raw[1:]
    return f"{raw}@{alpha}" if alpha is not None else raw


def look_filters(settings: Settings) -> str:
    """A cheap, tasteful grade per preset (no LUT files to install)."""
    presets = {
        "clean": "eq=contrast=1.04:saturation=1.06:brightness=0.008,unsharp=5:5:0.35",
        "punchy": "eq=contrast=1.12:saturation=1.22:brightness=0.010,unsharp=5:5:0.6,"
        "vignette=PI/5",
        "cinematic": "eq=contrast=1.10:saturation=0.95:gamma_b=1.04:gamma_r=0.98,"
        "unsharp=5:5:0.3,vignette=PI/4",
    }
    return presets.get(settings.look, "")


def _trim_graph(
    settings: Settings, edit: EditPlan, with_audio: bool
) -> tuple[str, str, str]:
    """Build the jump-cut concat graph. Returns ``(statements, video, audio)``."""
    parts: list[str] = []
    video_labels: list[str] = []
    audio_labels: list[str] = []

    for index, keep in enumerate(edit.keeps):
        parts.append(
            f"[0:v]trim=start={keep.start:.3f}:end={keep.end:.3f},"
            f"setpts=PTS-STARTPTS[v{index}]"
        )
        video_labels.append(f"[v{index}]")
        if with_audio:
            parts.append(
                f"[0:a]atrim=start={keep.start:.3f}:end={keep.end:.3f},"
                f"asetpts=PTS-STARTPTS[a{index}]"
            )
            audio_labels.append(f"[a{index}]")

    count = len(edit.keeps)
    if with_audio:
        inputs = "".join(
            f"{video_labels[i]}{audio_labels[i]}" for i in range(count)
        )
        parts.append(f"{inputs}concat=n={count}:v=1:a=1[cv][ca]")
        return ";".join(parts), "[cv]", "[ca]"

    parts.append(f"{''.join(video_labels)}concat=n={count}:v=1:a=0[cv]")
    return ";".join(parts), "[cv]", ""


def build_filter_complex(
    settings: Settings,
    *,
    duration: float,
    ass_filter: str | None,
    with_audio: bool = True,
    edit: EditPlan | None = None,
) -> str:
    """Full graph. ``duration`` is the *output* duration (fades, progress bar)."""
    statements: list[str] = []
    video_src, audio_src = "[0:v]", "[0:a]"

    if edit is not None and len(edit.keeps) > 1:
        trim_graph, video_src, audio_src = _trim_graph(settings, edit, with_audio)
        statements.append(trim_graph)

    video = _base_video_chain(settings, video_src)
    video += ",setsar=1"
    if edit is not None and abs(edit.speed - 1.0) > 1e-6:
        video += f",setpts=PTS/{edit.speed:.4f}"
    video += f",fps={settings.fps}"

    if settings.punch_in > 1.001:
        punch = settings.punch_in
        video += (
            f",scale=w='ceil(iw*{punch:.3f}/2)*2':h='ceil(ih*{punch:.3f}/2)*2':flags=bicubic"
            f",crop={_even(settings.width)}:{_even(settings.height)}"
        )

    grade = look_filters(settings)
    if grade:
        video += f",{grade}"

    video += ",fade=t=in:st=0:d=0.20"
    if duration > 0.8:
        video += f",fade=t=out:st={max(0.0, duration - 0.30):.2f}:d=0.30"

    if settings.progress_bar and duration > 1.0:
        bar = max(4, round(settings.height * 0.006))
        video += (
            f",drawbox=x=0:y=ih-{bar}:w='iw*t/{duration:.3f}':h={bar}"
            f":color={_ff_color('#FF3B30', 0.85)}:t=fill"
        )

    if ass_filter:
        video += f",{ass_filter}"
    video += "[v]"
    statements.append(video)

    if with_audio:
        audio = audio_src
        if edit is not None and abs(edit.speed - 1.0) > 1e-6:
            audio += f"atempo={edit.speed:.4f},"
        if settings.denoise:
            audio += "afftdn=nf=-25,"
        if settings.loudness:
            audio += "loudnorm=I=-16:TP=-1.5:LRA=11,"
        audio += "aresample=48000,"
        audio += "afade=t=in:st=0:d=0.15"
        if duration > 0.8:
            audio += f",afade=t=out:st={max(0.0, duration - 0.25):.2f}:d=0.25"
        audio += "[a]"
        statements.append(audio)

    return ";".join(statements)


def build_command(
    settings: Settings,
    *,
    source: Path,
    out_path: Path,
    start: float,
    end: float,
    ass_path: Path | None = None,
    with_audio: bool = True,
    edit: EditPlan | None = None,
) -> list[str]:
    source_duration = max(0.5, end - start)
    output_duration = edit.output_duration if edit is not None else source_duration
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
        "-t",
        f"{source_duration:.3f}",
        "-i",
        str(source),
        "-filter_complex",
        build_filter_complex(
            settings,
            duration=output_duration,
            ass_filter=ass_filter,
            with_audio=with_audio,
            edit=edit,
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

    edit_plan = build_edit_plan(transcript, plan.start, plan.end, settings)
    output_duration = edit_plan.output_duration
    edit_summary = summarise(edit_plan, settings)
    if edit_plan.keeps and (len(edit_plan.keeps) > 1 or edit_plan.removed):
        log.info("clip %d edit: %s", plan.index, edit_summary)

    ass_path: Path | None = None
    if settings.caption_style != "none" or settings.watermark:
        ass_path = (ass_dir or out_path.parent) / f"{out_path.stem}.ass"
        # Captions are rebuilt on the edited timeline so cuts never desync them.
        captions_transcript = (
            retime_transcript(transcript, edit_plan) if not edit_plan.is_simple else transcript
        )
        caption_start = 0.0 if not edit_plan.is_simple else plan.start
        write_ass(
            ass_path,
            captions_transcript,
            caption_start,
            caption_start + output_duration,
            settings,
            hook=plan.hook,
            clip_duration=output_duration,
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
        edit=edit_plan,
    )

    def result(thumb: Path | None = None, elapsed: float = 0.0) -> RenderResult:
        return RenderResult(
            path=out_path,
            start=plan.start,
            end=plan.end,
            command=command,
            thumbnail=thumb,
            elapsed=elapsed,
            edit=edit_plan,
            output_duration=output_duration,
            editing=edit_summary,
        )

    if dry_run:
        log.info("dry run: %s", " ".join(shlex.quote(part) for part in command))
        return result()

    log.info(
        "rendering clip %d (%.1fs → %.1fs, %.1fs in / %.1fs out)",
        plan.index,
        plan.start,
        plan.end,
        plan.duration,
        output_duration,
    )
    started = time.time()
    completed = _run(command, timeout=timeout)
    if completed.returncode != 0 and audio:
        log.warning("render failed with audio, retrying without an audio track")
        command = build_command(
            settings,
            source=source,
            out_path=out_path,
            start=plan.start,
            end=plan.end,
            ass_path=ass_path,
            with_audio=False,
            edit=edit_plan,
        )
        completed = _run(command, timeout=timeout)

    if completed.returncode != 0:
        raise RenderError(
            f"ffmpeg failed for clip {plan.index} (exit {completed.returncode}): "
            f"{completed.stderr.strip()[:600]}"
        )
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RenderError(f"ffmpeg produced no output for clip {plan.index}")

    elapsed = time.time() - started
    thumbnail = make_thumbnail(settings, out_path, out_path.with_name(f"{out_path.stem}.jpg"))
    log.info(
        "clip %d done in %.1fs (%.1f MB, %.1fs)",
        plan.index,
        elapsed,
        out_path.stat().st_size / 1e6,
        output_duration,
    )
    return result(thumb=thumbnail, elapsed=elapsed)


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
