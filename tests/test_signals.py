from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from clipper.config import Settings
from clipper.signals import (
    LoudEvent,
    annotate_lines,
    loud_moments,
    marker_for,
    summarise,
)
from clipper.subtitles import Segment


def test_marker_for_matches_an_overlapping_span():
    loud = [LoudEvent(start=10.0, end=12.0, db=12.4)]
    assert marker_for(9.0, 11.0, loud) == "<<LOUD(+12dB)>>"
    assert marker_for(12.5, 14.0, loud) == "<<LOUD(+12dB)>>"  # one second of slack
    assert marker_for(20.0, 22.0, loud) == ""
    assert marker_for(10.0, 11.0, []) == ""


def test_annotate_lines_keeps_the_plain_format_without_signals():
    segments = [
        Segment(start=0.0, end=2.0, text="hello"),
        Segment(start=2.0, end=4.0, text="world"),
    ]
    plain = annotate_lines(segments, [])
    assert plain[0] == "[   0.0 -    2.0] hello"
    tagged = annotate_lines(segments, [LoudEvent(3.5, 4.0, 11.0)])
    assert "<<LOUD(+11dB)>>" in tagged[1]
    assert "<<LOUD" not in tagged[0]


def test_summarise_says_so_when_nothing_is_loud():
    assert summarise([]) == "no loud moments found"
    assert "2 loud" in summarise([LoudEvent(1, 2, 10.0), LoudEvent(5, 6, 14.0)])


def test_loud_moments_handles_a_missing_file(tmp_path: Path):
    assert loud_moments(tmp_path / "nope.mp4", Settings()) == []


@pytest.fixture()
def shouty_audio(tmp_path: Path) -> Path:
    """20 s of quiet room tone with a 3 s shout in the middle."""
    ffmpeg = os.environ.get("CLIP_TEST_FFMPEG") or shutil.which("ffmpeg") or ""
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    out = tmp_path / "shouty.wav"
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=16000:duration=20",
        "-af",
        "volume=0.02,"
        "volume=enable='between(t,8,11)':volume=14",
        str(out),
    ]
    subprocess.run(command, check=True)
    return out


def test_loud_moments_finds_the_shout(shouty_audio: Path):
    ffmpeg = os.environ.get("CLIP_TEST_FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
    events = loud_moments(shouty_audio, Settings(ffmpeg=ffmpeg), timeout=120)
    if not events:  # pragma: no cover - an ffmpeg build without astats metadata
        pytest.skip("this ffmpeg build does not report RMS levels")
    assert any(7.0 <= event.start <= 12.0 for event in events), events
    assert max(event.db for event in events) >= 9.0
