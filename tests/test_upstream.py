"""The no-proxy fallback: read Piped/Invidious answers, never trust one instance."""

from __future__ import annotations

from pathlib import Path

import pytest

from clipper import upstream
from clipper.config import Settings
from clipper.youtube import VideoSource, _upstream_fallback

PIPED = {
    "title": "a public video",
    "duration": 123,
    "uploader": "someone",
    "proxyUrl": "https://proxy.example.test",
    "videoStreams": [
        {
            "url": "https://rr1---sn-x.googlevideo.com/videoplayback?itag=18",
            "mimeType": "video/mp4",
            "quality": "360p",
            "height": 360,
            "videoOnly": False,
        },
        {
            "url": "https://rr1---sn-x.googlevideo.com/videoplayback?itag=137",
            "mimeType": "video/mp4",
            "quality": "1080p",
            "height": 1080,
            "videoOnly": True,
        },
    ],
    "audioStreams": [
        {"url": "https://rr1---sn-x.googlevideo.com/videoplayback?itag=140", "mimeType": "audio/mp4"}
    ],
}

INVIDIOUS = {
    "title": "another public video",
    "lengthSeconds": 90,
    "author": "somebody",
    "formatStreams": [
        {"url": "/videoplayback?itag=18", "type": 'video/mp4; codecs="avc1"', "resolution": "360p"}
    ],
    "adaptiveFormats": [
        {"url": "/videoplayback?itag=136", "type": 'video/mp4; codecs="avc1"', "resolution": "720p"},
        {"url": "/videoplayback?itag=140", "type": 'audio/mp4; codecs="mp4a"'},
    ],
    "captions": [{"label": "English", "url": "/api/v1/captions/abc?label=English"}],
}


def test_parse_piped_marks_muxed_video_and_proxies_urls():
    video = upstream.parse_piped(PIPED, "https://piped.example.test", "abc123def45")
    assert video.title == "a public video"
    assert video.duration == 123
    muxed = video.muxed()
    assert muxed and muxed[0].height == 360 and muxed[0].has_audio
    # The instance's proxy URL is what actually works from a datacenter IP.
    assert muxed[0].url.startswith("https://proxy.example.test/rr1---sn-x.googlevideo.com/videoplayback?")
    assert video.video_only().height == 1080
    assert video.audio_only().is_audio


def test_parse_invidious_makes_relative_urls_absolute():
    video = upstream.parse_invidious(INVIDIOUS, "https://inv.example.test", "abc123def45")
    assert video.duration == 90
    assert video.muxed()[0].url == "https://inv.example.test/videoplayback?itag=18"
    assert video.video_only().height == 720
    assert video.audio_only().mime == "audio/mp4"
    assert video.captions and video.captions[0][0] == "English"
    assert video.captions[0][1].startswith("https://inv.example.test/api/v1/captions/")


def test_max_height_prefers_something_that_fits():
    video = upstream.parse_piped(PIPED, "https://piped.example.test", "abc123def45")
    assert [stream.height for stream in video.muxed(max_height=720)] == [360]
    # Nothing fits an impossible cap: take the smallest available, never the biggest.
    assert video.video_only(max_height=200).height == 1080
    assert video.audio_only().height == 0


def test_instances_are_overridable(monkeypatch):
    settings = Settings(upstream_instances="piped:https://one.test,invidious:https://two.test")
    assert upstream.instances(settings) == [
        ("piped", "https://one.test"),
        ("invidious", "https://two.test"),
    ]
    # A bare URL is treated as an Invidious instance.
    assert upstream.instances(Settings(upstream_instances="https://three.test")) == [
        ("invidious", "https://three.test")
    ]
    assert upstream.instances(Settings())  # built-in list


def test_resolve_skips_an_instance_that_fails(monkeypatch):
    seen: list[str] = []

    def fake_json(url, settings, timeout=25):
        seen.append(url)
        if "dead" in url:
            raise OSError("connection refused")
        return INVIDIOUS

    monkeypatch.setattr(upstream, "_http_json", fake_json)
    monkeypatch.setattr(
        upstream, "instances", lambda settings=None: [("invidious", "https://dead.test"), ("invidious", "https://ok.test")]
    )
    video = upstream.resolve("abc123def45", Settings())
    assert video is not None and video.instance == "https://ok.test"
    assert len(seen) == 2


def test_resolve_returns_none_when_nobody_answers(monkeypatch):
    def boom(url, settings, timeout=25):
        raise OSError("nope")

    monkeypatch.setattr(upstream, "_http_json", boom)
    monkeypatch.setattr(upstream, "instances", lambda settings=None: [("piped", "https://x.test")])
    assert upstream.resolve("abc123def45", Settings()) is None


def test_download_prefers_a_muxed_stream(tmp_path: Path, monkeypatch):
    def fake_fetch(url, dest, settings, timeout=60):
        Path(dest).write_bytes(b"x" * 4096)
        return Path(dest)

    monkeypatch.setattr(upstream, "fetch_stream", fake_fetch)
    video = upstream.parse_piped(PIPED, "https://piped.example.test", "abc123def45")
    path, note = upstream.download(video, tmp_path, Settings())
    assert path.exists() and "muxed" in note


def test_download_muxes_video_and_audio_when_needed(tmp_path: Path, monkeypatch):
    video = upstream.parse_invidious(INVIDIOUS, "https://inv.example.test", "abc123def45")
    # No muxed stream survives: pretend the first download failed.
    calls: list[str] = []

    def fake_fetch(url, dest, settings, timeout=60):
        calls.append(url)
        Path(dest).write_bytes(b"y" * 4096)
        return Path(dest)

    monkeypatch.setattr(upstream, "fetch_stream", fake_fetch)
    video.streams = [s for s in video.streams if not (s.has_audio and s.height)]
    monkeypatch.setattr(
        upstream,
        "_mux",
        lambda v, a, target, settings: (Path(target).write_bytes(b"z"), Path(target))[1],
    )
    path, note = upstream.download(video, tmp_path, Settings(), max_height=0)
    assert "audio" in note
    assert path.exists()
    assert any("itag=136" in url for url in calls)


def test_upstream_fallback_builds_a_source(tmp_path: Path, monkeypatch):
    video = tmp_path / "upstream.mp4"
    video.write_bytes(b"v" * 4096)
    parsed = upstream.parse_piped(PIPED, "https://piped.example.test", "abc123def45")

    monkeypatch.setattr(upstream, "resolve", lambda video_id, settings, timeout=25: parsed)
    monkeypatch.setattr(
        upstream,
        "download",
        lambda video, work_dir, settings, max_height=0: (video, "muxed 360p"),
    )
    monkeypatch.setattr(upstream, "fetch_captions", lambda video, work_dir, settings: None)
    monkeypatch.setattr("clipper.youtube._ffprobe_metadata", lambda path, settings: (123.0, 1280, 720))

    source = _upstream_fallback("https://youtu.be/abc123def45", tmp_path, Settings())
    assert isinstance(source, VideoSource)
    assert source.title == "a public video"
    assert source.duration == 123
    assert source.info["instance"] == "https://piped.example.test"
    assert source.path is not None


def test_upstream_fallback_is_off_when_asked(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(upstream, "resolve", lambda *a, **k: pytest.fail("should not be called"))
    assert _upstream_fallback("https://youtu.be/abc123def45", tmp_path, Settings(upstream=False)) is None


def test_upstream_fallback_survives_a_broken_instance(tmp_path: Path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("instance exploded")

    monkeypatch.setattr(upstream, "resolve", boom)
    assert _upstream_fallback("https://youtu.be/abc123def45", tmp_path, Settings()) is None


def test_already_proxied_urls_are_not_wrapped_twice():
    payload = {
        "title": "x",
        "proxyUrl": "https://piped-proxy.example.test",
        "videoStreams": [
            {
                "url": "https://piped-proxy.example.test/videoplayback?itag=18",
                "mimeType": "video/mp4",
                "quality": "360p",
                "height": 360,
                "videoOnly": False,
            }
        ],
    }
    video = upstream.parse_piped(payload, "https://pipedapi.example.test", "abc")
    assert video.muxed()[0].url == "https://piped-proxy.example.test/videoplayback?itag=18"
