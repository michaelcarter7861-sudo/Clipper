"""Small unit tests for pipeline plumbing (no network, no ffmpeg)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clipper.config import Settings
from clipper.moments import ClipPlan
from clipper.pipeline import (
    ClipOutput,
    VideoResult,
    _clip_filename,
    _index_html,
    _post_text,
    _report_markdown,
)
from clipper.subtitles import Segment, Transcript, Word
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


def test_post_text_has_title_caption_hashtags_and_source(tmp_path: Path):
    plan = ClipPlan(
        65.0,
        95.0,
        "IT ACTUALLY WORKED",
        "It worked",
        reason="clean payoff",
        score=88,
        index=2,
        caption="Nobody saw this coming 😳 would you try it?",
        hashtags=["#GTA6", "#shorts"],
        layout="facecam",
    )
    source = VideoSource(url="https://youtu.be/abc", video_id="abc", title="A stream", duration=600)
    text = _post_text(ClipOutput(plan, Path("clip.mp4"), None, None, layout="facecam"), source)
    assert text.startswith("TITLE: It worked")
    assert "HOOK: IT ACTUALLY WORKED" in text
    assert "CAPTION:" in text and "#GTA6 #shorts" in text
    assert "SOURCE: A stream — https://youtu.be/abc" in text
    assert "FROM: 00:01:05 TO: 00:01:35 (30s)" in text
    assert "score 88" in text and "layout facecam" in text


def test_post_text_survives_a_bare_plan():
    plan = ClipPlan(0.0, 12.0, "hook", "title", index=1)
    text = _post_text(ClipOutput(plan, Path("c.mp4"), None, None), VideoSource("", "", "", 0))
    assert "CAPTION:" not in text
    assert text.endswith("\n")


def test_shift_plan_and_transcript_land_on_zero():
    from clipper.pipeline import _shift_plan, _shift_transcript

    plan = ClipPlan(65.0, 95.0, "hook", "title", index=1)
    shifted = _shift_plan(plan, 65.0)
    assert shifted.start == 0.0
    assert shifted.end == 30.0
    assert shifted.hook == "hook"

    transcript = Transcript(
        segments=[
            Segment(60.0, 70.0, "before", [Word(61.0, 62.0, "before")]),
            Segment(65.0, 75.0, "inside", [Word(66.0, 67.0, "inside"), Word(68.0, 69.0, "now")]),
        ],
        duration=120.0,
    )
    section = _shift_transcript(transcript, 65.0, 95.0)
    words = section.words()
    assert words[0].text == "inside"
    assert 0.0 <= words[0].start <= 2.5
    assert words[-1].text == "now"
    assert all(word.start >= 0 for word in words)


def test_media_for_clip_uses_the_whole_video_by_default(tmp_path: Path):
    from clipper.pipeline import _media_for_clip

    source = VideoSource(url="u", video_id="v", title="t", duration=100.0, path=tmp_path / "a.mp4")
    transcript = Transcript(duration=100.0)
    plan = ClipPlan(10.0, 30.0, "h", "t", index=1)
    media, render_plan, render_transcript, section = _media_for_clip(
        plan, source, transcript, tmp_path, tmp_path, Settings()
    )
    assert media == source.path
    assert render_plan is plan
    assert render_transcript is transcript
    assert section is None


def test_media_for_clip_downloads_only_the_window_in_audio_only_mode(tmp_path: Path, monkeypatch):
    from clipper import pipeline as pipeline_module

    section_file = tmp_path / "section.mp4"
    section_file.write_bytes(b"x" * 2048)
    calls: list[tuple[float, float]] = []

    def fake_section(url, start, end, work_dir, settings):
        calls.append((start, end))
        return VideoSource(
            url=url, video_id="v", title="t", duration=end - start, path=section_file
        )

    monkeypatch.setattr(pipeline_module, "download_section", fake_section)

    source = VideoSource(
        url="https://example.test/v", video_id="v", title="t", duration=900.0,
        path=tmp_path / "audio.m4a", audio_only=True,
    )
    transcript = Transcript(
        segments=[Segment(300.0, 320.0, "hello", [Word(301.0, 302.0, "hello")])],
        duration=900.0,
    )
    plan = ClipPlan(300.0, 320.0, "h", "t", index=2)
    media, render_plan, render_transcript, section = pipeline_module._media_for_clip(
        plan, source, transcript, tmp_path, tmp_path, Settings()
    )
    assert calls == [(300.0, 320.0)]
    assert media == section_file
    assert section == section_file
    assert render_plan.start == 0.0 and render_plan.end == 20.0
    assert render_transcript.words()[0].start < 3.0


def test_media_for_clip_falls_back_to_the_whole_video(tmp_path: Path, monkeypatch):
    from clipper import pipeline as pipeline_module
    from clipper.youtube import DownloadError

    def broken(url, start, end, work_dir, settings):
        raise DownloadError("nope")

    full = tmp_path / "full.mp4"
    full.write_bytes(b"y" * 1024)

    def fake_download(url, work_dir, settings):
        return VideoSource(url=url, video_id="v", title="t", duration=100.0, path=full)

    monkeypatch.setattr(pipeline_module, "download_section", broken)
    monkeypatch.setattr(pipeline_module, "download", fake_download)

    source = VideoSource(
        url="https://example.test/v", video_id="v", title="t", duration=900.0,
        path=tmp_path / "audio.m4a", audio_only=True,
    )
    plan = ClipPlan(10.0, 30.0, "h", "t", index=1)
    media, render_plan, _, section = pipeline_module._media_for_clip(
        plan, source, Transcript(duration=900.0), tmp_path, tmp_path, Settings()
    )
    assert media == full
    assert source.audio_only is False
    assert render_plan is plan and section is None


def test_chat_signals_never_break_a_run(tmp_path: Path, monkeypatch):
    from clipper import pipeline as pipeline_module

    def broken(url, work_dir, settings):
        raise RuntimeError("chat replay is on fire")

    monkeypatch.setattr(pipeline_module, "download_chat", broken)
    assert pipeline_module._chat_signals("https://example.test/v", tmp_path, Settings()) == []


def test_chat_signals_are_parsed_from_a_file(tmp_path: Path, monkeypatch):
    import json

    from clipper import pipeline as pipeline_module

    path = tmp_path / "chat.live_chat.json"
    entries = [
        {
            "replayChatItemAction": {
                "actions": [{"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
                    "message": {"runs": [{"text": "POG"}]}}}}}
                ],
                "videoOffsetTimeMsec": str(int(100.0 * 1000) + index),
            }
        }
        for index in range(10)
    ]
    path.write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(pipeline_module, "download_chat", lambda url, work_dir, settings: path)
    events = pipeline_module._chat_signals("https://example.test/v", tmp_path, Settings())
    assert len(events) == 1 and events[0].count == 10


@pytest.mark.ffmpeg
def test_a_video_without_audio_still_produces_a_transcript_object(tmp_path: Path):
    """No audio track means no words — not a failed run."""
    import os
    import shutil
    import subprocess

    from clipper.pipeline import get_transcript
    from clipper.youtube import VideoSource

    ffmpeg = os.environ.get("CLIP_TEST_FFMPEG") or shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    silent = tmp_path / "silent.mp4"
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=6",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(silent),
        ],
        check=True,
    )
    source = VideoSource(
        url="silent.mp4", video_id="silent", title="silent", duration=6.0, path=silent, local=True
    )
    settings = Settings(
        ffprobe=os.environ.get("CLIP_TEST_FFPROBE") or shutil.which("ffprobe") or "",
        work_dir=tmp_path,
    )
    transcript = get_transcript(source, tmp_path / "work", settings)
    assert transcript.is_empty
    assert transcript.source == "silent"
    assert transcript.duration == 6.0
