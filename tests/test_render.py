from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from clipper.captions import write_ass
from clipper.config import Settings
from clipper.moments import ClipPlan
from clipper.render import (
    build_command,
    build_filter_complex,
    doctor,
    has_audio,
    render_clip,
    resolve_binary,
)
from clipper.subtitles import Segment, Transcript, Word


def test_crop_filter_graph():
    graph = build_filter_complex(
        Settings(fit="crop", width=1080, height=1920, fps=30),
        duration=30,
        ass_filter=None,
    )
    assert "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920" in graph
    assert "fps=30" in graph
    assert "fade=t=in" in graph and "fade=t=out" in graph
    assert graph.endswith("[a]")
    assert "loudnorm=I=-16" in graph


def test_blur_filter_graph_splits_input():
    graph = build_filter_complex(
        Settings(fit="blur", width=1080, height=1920), duration=20, ass_filter=None
    )
    assert "split=2" in graph
    assert "gblur" in graph
    assert "overlay=(W-w)/2:(H-h)/2" in graph


def test_pad_filter_graph():
    graph = build_filter_complex(
        Settings(fit="pad", width=1080, height=1920), duration=20, ass_filter=None
    )
    assert "pad=1080:1920" in graph


def test_ass_and_loudness_toggles(tmp_path: Path):
    graph = build_filter_complex(
        Settings(loudness=False, denoise=True),
        duration=10,
        ass_filter="ass=filename=/tmp/x.ass",
    )
    assert "loudnorm" not in graph
    assert "afftdn" in graph
    assert graph.index("ass=filename") < graph.index("[v]")

    no_audio = build_filter_complex(Settings(), duration=10, ass_filter=None, with_audio=False)
    assert "[a]" not in no_audio


def test_dimensions_are_even():
    graph = build_filter_complex(Settings(width=1081, height=1921), duration=5, ass_filter=None)
    assert "scale=1080:1920" in graph


def test_build_command_order_and_flags(tmp_path: Path):
    settings = Settings(crf=23, fps=25, preset="fast")
    command = build_command(
        settings,
        source=tmp_path / "in.mp4",
        out_path=tmp_path / "out.mp4",
        start=12.5,
        end=40.0,
        ass_path=tmp_path / "cap.ass",
        with_audio=True,
    )
    joined = " ".join(command)
    assert command[command.index("-ss") + 1] == "12.500"
    assert command.index("-ss") < command.index("-i")
    assert command[command.index("-t") + 1] == "27.500"
    assert "-crf" in command and "23" in command
    assert "-c:a" in command and "aac" in command
    assert "-movflags" in command and "+faststart" in command
    assert "ass=filename=" in joined
    assert command[-1].endswith("out.mp4")


def test_no_audio_flag(tmp_path: Path):
    command = build_command(
        Settings(), source=tmp_path / "a.mp4", out_path=tmp_path / "b.mp4", start=0, end=5,
        with_audio=False,
    )
    assert "-an" in command
    assert "[a]" not in command


def test_missing_ffmpeg_is_reported():
    problems = doctor(Settings(ffmpeg="definitely-not-a-real-binary-xyz"))
    assert problems and "ffmpeg was not found" in problems[0]


def test_resolve_binary():
    assert resolve_binary("definitely-not-a-real-binary-xyz") == ""
    assert resolve_binary("/bin/sh").endswith("sh")


def ffmpeg_path() -> str | None:
    candidate = os.environ.get("CLIP_TEST_FFMPEG") or shutil.which("ffmpeg")
    return candidate if candidate and Path(candidate).exists() else None


def make_source(path: Path, ffmpeg: str, seconds: int = 4) -> Path:
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=25:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )
    return path


@pytest.mark.ffmpeg
def test_render_burns_captions_end_to_end(tmp_path: Path):
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        pytest.skip("ffmpeg not available")

    source = make_source(tmp_path / "source.mp4", ffmpeg)
    probe = shutil.which("ffprobe")
    if probe:
        assert has_audio(probe, source) is True

    transcript = Transcript(
        segments=[
            Segment(
                start=0.0,
                end=2.0,
                text="this is a test",
                words=[Word(0.0, 0.6, "this"), Word(0.6, 1.2, "is"), Word(1.2, 1.8, "a"), Word(1.8, 2.4, "test")],
            )
        ],
        duration=4,
    )
    settings = Settings(ffmpeg=ffmpeg, ffprobe=shutil.which("ffprobe") or "", width=540, height=960)
    plan = ClipPlan(0.0, 3.0, "the hook", "test clip", score=80, index=1)
    out = tmp_path / "clip.mp4"

    result = render_clip(source, out, plan, transcript, settings, ass_dir=tmp_path)
    assert result.path.exists() and result.path.stat().st_size > 10_000
    assert result.thumbnail and result.thumbnail.exists()
    assert (tmp_path / "clip.ass").exists()

    probe = shutil.which("ffprobe")
    if probe:
        duration = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(out)],
            capture_output=True, text=True,
        ).stdout.strip()
        assert 2.5 < float(duration) < 3.6


@pytest.mark.ffmpeg
def test_dry_run_writes_ass_but_not_video(tmp_path: Path):
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    source = make_source(tmp_path / "source.mp4", ffmpeg)
    transcript = Transcript(
        segments=[Segment(start=0, end=4, text="hello world", words=[Word(0, 1, "hello"), Word(1, 2, "world")])],
        duration=4,
    )
    out = tmp_path / "clip.mp4"
    result = render_clip(
        source, out, ClipPlan(0, 3, "hook", "t", index=1), transcript,
        Settings(ffmpeg=ffmpeg), ass_dir=tmp_path, dry_run=True,
    )
    assert not out.exists()
    assert result.command and "-filter_complex" in result.command
    assert (tmp_path / "clip.ass").exists()


def test_write_ass_handles_missing_hook(tmp_path: Path):
    transcript = Transcript(segments=[], duration=1)
    path = write_ass(tmp_path / "x.ass", transcript, 0, 1, Settings(caption_style="none"))
    assert path.exists()
    assert "Dialogue" not in path.read_text()
