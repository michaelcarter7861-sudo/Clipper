from __future__ import annotations

import pytest

from clipper.config import Settings, parse_urls, settings_from_env


def test_parse_urls_handles_separators_and_duplicates():
    raw = (
        "https://youtu.be/abc123, https://www.youtube.com/watch?v=def456\n"
        "https://youtu.be/abc123 not-a-url"
    )
    assert parse_urls(raw) == [
        "https://youtu.be/abc123",
        "https://www.youtube.com/watch?v=def456",
    ]


def test_parse_urls_keeps_local_files_and_file_links(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not really a video")
    assert parse_urls(str(clip)) == [str(clip)]
    assert parse_urls(f"file://{clip}") == [f"file://{clip}"]
    assert parse_urls("/nope/missing.mp4") == []


def test_parse_urls_accepts_lists_and_none():
    assert parse_urls(None) == []
    assert parse_urls(["https://a.test/1", "https://a.test/2"]) == [
        "https://a.test/1",
        "https://a.test/2",
    ]


def test_settings_clamp_values():
    settings = Settings(urls=["https://x.test"], clips=99, min_duration=40, max_duration=10)
    assert settings.clips == 12
    assert settings.max_duration == 45
    assert settings.min_duration == 40


def test_invalid_choices_fall_back_to_defaults():
    settings = Settings(caption_style="nope", fit="nope", llm_backend="nope")
    assert settings.caption_style == "karaoke"
    assert settings.fit == "crop"
    assert settings.llm_backend == "auto"


def test_caption_font_size_scales_with_width():
    assert Settings(width=1080, height=1920).caption_font_size == 78
    assert Settings(width=1080, height=1920, caption_size=120).caption_font_size == 120


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("CLIP_URLS", "https://youtu.be/one https://youtu.be/two")
    monkeypatch.setenv("CLIP_COUNT", "5")
    monkeypatch.setenv("CAPTION_STYLE", "plain")
    monkeypatch.setenv("CLIP_FIT", "blur")
    monkeypatch.setenv("YTDLP_EXTRA_ARGS", "--proxy http://127.0.0.1:7890 --no-check-certificates")

    settings = settings_from_env(out_dir="custom-out")
    assert settings.urls == ["https://youtu.be/one", "https://youtu.be/two"]
    assert settings.clips == 5
    assert settings.caption_style == "plain"
    assert settings.fit == "blur"
    assert settings.out_dir.name == "custom-out"
    assert settings.ytdlp_extra_args == ["--proxy", "http://127.0.0.1:7890", "--no-check-certificates"]


def test_bad_env_numbers_are_ignored(monkeypatch):
    monkeypatch.setenv("CLIP_COUNT", "not-a-number")
    settings = settings_from_env()
    assert settings.clips == 3


def test_overrides_win_over_env(monkeypatch):
    monkeypatch.setenv("CLIP_COUNT", "7")
    assert settings_from_env(clips=2).clips == 2


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_empty_urls(raw):
    assert parse_urls(raw) == []


def test_talking_mode_cuts_harder_and_punches_on_cuts():
    settings = settings_from_env(editing="talking")
    assert settings.tighten is True
    assert settings.max_pause == 0.30
    assert settings.pre_roll == 0.05
    assert settings.post_roll == 0.12
    assert settings.remove_fillers is True
    assert settings.retakes is True
    assert settings.punch_in == 1.10
    assert settings.punch_alternate is True


def test_talking_mode_leaves_explicit_knobs_alone():
    settings = settings_from_env(editing="talking", max_pause=0.8, punch_in=1.0)
    assert settings.max_pause == 0.8
    assert settings.punch_in == 1.0
    assert settings.punch_alternate is True


def test_chunk_span_and_signals_are_bounded():
    assert settings_from_env(chunk_span=5).chunk_span == 120.0
    assert settings_from_env(chunk_span=99999).chunk_span == 3600.0
    assert settings_from_env().chunk_span == 720.0
    assert settings_from_env(chat_min=0).chat_min == 2


def test_audio_only_and_chat_are_off_unless_asked():
    settings = settings_from_env()
    assert settings.audio_only is False
    assert settings.chat is None
    assert settings.chat_fetch is True
    assert settings_from_env(audio_only=True).audio_only is True
