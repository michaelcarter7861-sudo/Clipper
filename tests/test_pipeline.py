"""Small unit tests for pipeline plumbing (no network, no ffmpeg)."""

from __future__ import annotations

import json
from pathlib import Path

from clipper.config import Settings
from clipper.moments import ClipPlan
from clipper.pipeline import ClipOutput, VideoResult, _clip_filename, _index_html, _report_markdown
from clipper.subtitles import Transcript
from clipper.youtube import VideoSource


def make_output() -> ClipOutput:
    plan = ClipPlan(1.0, 21.0, "the money secret", "money", score=88, index=1)
    return ClipOutput(
        plan=plan,
        path=Path("clip-01-the-money-secret.mp4"),
        thumbnail=Path("clip-01-the-money-secret.jpg"),
        ass=Path("clip-01-the-money-secret.ass"),
        output_duration=12.5,
        editing="1 jump cut(s), -8.0s dead air",
        edit={"keeps": [{"start": 0, "end": 1.5}]},
    )


def test_clip_output_serialises():
    payload = make_output().to_dict()
    assert payload["hook"] == "the money secret"
    assert payload["file"] == "clip-01-the-money-secret.mp4"
    assert payload["output_duration"] == 12.5
    assert payload["editing"].startswith("1 jump cut")
    assert payload["edit"]["keeps"]
    assert json.loads(json.dumps(payload))  # JSON-safe


def test_clip_output_without_render():
    payload = ClipOutput(ClipPlan(0, 10, "h", "t", index=1), None, None, None).to_dict()
    assert payload["file"] is None
    assert payload["thumbnail"] is None


def test_clip_filename_is_safe():
    plan = ClipPlan(0, 10, "Hello/World: the *secret*?", "t", index=3)
    name = _clip_filename(plan)
    assert name.startswith("clip-03-hello-world")
    assert name.endswith(".mp4")
    assert "/" not in name and ":" not in name


def test_report_and_index_handle_results(tmp_path: Path):
    result = VideoResult(
        source=VideoSource(url="https://youtu.be/x", video_id="x", title="A video", duration=60),
        transcript=Transcript(segments=[], duration=60),
        clips=[make_output()],
        method="claude-code",
        dir=tmp_path / "a-video-x",
    )
    (tmp_path / "a-video-x").mkdir()
    report = _report_markdown([result], Settings())
    assert "A video" in report
    assert "1 jump cut" in report
    assert "claude-code" in report

    index = _index_html([result])
    assert "A video" in index
    assert 'href="a-video-x/index.html"' in index


def test_report_marks_failures(tmp_path: Path):
    result = VideoResult(
        source=VideoSource(url="u", video_id="", title="bad", duration=0),
        transcript=Transcript(),
        method="failed",
        error="bot check",
    )
    report = _report_markdown([result], Settings())
    assert "bot check" in report
