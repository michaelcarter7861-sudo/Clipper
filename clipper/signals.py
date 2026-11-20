"""Cheap, honest signals that help the LLM find the hype: loudness and chat.

Ported from the GTA 6 Clipper skill (``skills/gta6-clipper``, upstream
``vossashtonai/gta6-clipper`` @ 11cee2b, MIT): a streamer screaming is a real
marker, and it costs one ffmpeg pass with no model at all.

The markers are *evidence for the picker*, never a decision: a loud line with
nothing interesting said is not a clip, and a great line with no marker can be.
"""

from __future__ import annotations

import re
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .log import get_logger

log = get_logger()

#: How far above the local baseline (in dB) counts as "loud".
LOUD_DB = 9.0
#: Below this RMS the frame is effectively digital silence; the real test is the
#: *relative* jump, because a quiet recording and a loud one must both work.
FLOOR_DB = -60.0
#: Seconds either side of a frame that define "normal" for this video.
BASELINE_WINDOW = 90
#: Longest stretch of audio to analyse (2 h); longer streams still get signals
#: for their first two hours and are logged as truncated.
MAX_ANALYSIS_SEC = 7200.0


@dataclass
class LoudEvent:
    start: float
    end: float
    db: float

    def to_dict(self) -> dict:
        return {"start": round(self.start, 2), "end": round(self.end, 2), "db": round(self.db, 1)}


def _rms_levels(source: Path, settings: Settings, *, start: float, span: float, timeout: int) -> list[float]:
    """Per-second RMS level in dBFS, straight out of ffmpeg's astats."""
    exe = settings.ffmpeg
    command = [
        exe, "-hide_banner", "-nostats", "-nostdin", "-loglevel", "info",
        "-ss", f"{max(0.0, start):.3f}",
        "-t", f"{span:.3f}",
        "-i", str(source),
        "-vn", "-ac", "1", "-ar", "16000",
        "-af",
        "asetnsamples=n=16000,astats=metadata=1:reset=1,"
        "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
        "-f", "null", "-",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception as exc:  # noqa: BLE001 - signals are a bonus, never a blocker
        log.debug("loudness analysis failed: %s", exc)
        return []
    levels: list[float] = []
    for match in re.finditer(r"RMS_level=(-?[\d.]+|-inf|nan)", completed.stdout + completed.stderr):
        raw = match.group(1)
        levels.append(-90.0 if raw in ("-inf", "nan") else float(raw))
    return levels


def loud_moments(
    source: Path,
    settings: Settings,
    *,
    start: float = 0.0,
    end: float | None = None,
    timeout: int = 1800,
) -> list[LoudEvent]:
    """Seconds where the audio jumps well above this video's own normal."""
    if not Path(source).exists():
        return []
    span = (end - start) if end and end > start else MAX_ANALYSIS_SEC
    span = min(span, MAX_ANALYSIS_SEC)
    levels = _rms_levels(Path(source), settings, start=start, span=span, timeout=timeout)
    if not levels:
        return []

    events: list[LoudEvent] = []
    for second, level in enumerate(levels):
        if level < FLOOR_DB:
            continue
        low, high = max(0, second - BASELINE_WINDOW), min(len(levels), second + BASELINE_WINDOW)
        baseline = statistics.median(levels[low:high])
        jump = level - baseline
        if jump >= LOUD_DB:
            events.append(LoudEvent(start=start + second, end=start + second, db=jump))

    merged: list[LoudEvent] = []
    for event in events:
        if merged and event.start - merged[-1].end <= 2:
            merged[-1].end = event.end
            merged[-1].db = max(merged[-1].db, event.db)
        else:
            merged.append(LoudEvent(event.start, event.end, event.db))
    return merged


def marker_for(start: float, end: float, loud: list[LoudEvent]) -> str:
    """``"<<LOUD(+12dB)>>"`` when this span overlaps a loud moment, else ``""``."""
    for event in loud:
        if event.start <= end and event.end + 1 >= start:
            return f"<<LOUD(+{event.db:.0f}dB)>>"
    return ""


def annotate_lines(segments, loud: list[LoudEvent], chat=None) -> list[str]:
    """Transcript lines for a prompt, tagged when a signal lands inside them.

    ``chat`` is an optional list of :class:`clipper.chat.ChatEvent`. Both markers are
    evidence for the picker, never an instruction to cut there.
    """
    from .chat import at as chat_at
    from .chat import marker_for as chat_marker

    lines: list[str] = []
    for segment in segments:
        markers: list[str] = []
        if loud:
            marker = marker_for(segment.start, segment.end, loud)
            if marker:
                markers.append(marker)
        if chat:
            event = chat_at(chat, (segment.start + segment.end) / 2)
            if event is not None:
                markers.append(chat_marker(event))
        line = f"[{segment.start:6.1f} - {segment.end:6.1f}] {segment.text}"
        lines.append(f"{line}   {' '.join(markers)}" if markers else line)
    return lines


def summarise(loud: list[LoudEvent]) -> str:
    if not loud:
        return "no loud moments found"
    peak = max(event.db for event in loud)
    return f"{len(loud)} loud moment(s), peak +{peak:.0f}dB"
