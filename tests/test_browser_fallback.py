from __future__ import annotations

from pathlib import Path

from clipper import youtube
from clipper.browser import BrowserPage
from clipper.config import Settings

PR_OK = {
    "playabilityStatus": {"status": "OK"},
    "videoDetails": {"videoId": "abc123", "title": "Stream", "lengthSeconds": "90", "author": "x"},
    "streamingData": {
        "formats": [
            {
                "itag": 18,
                "url": "https://example.test/v18",
                "mimeType": "video/mp4",
                "height": 360,
                "width": 640,
            }
        ]
    },
}

PR_BLOCKED = {
    "playabilityStatus": {"status": "LOGIN_REQUIRED", "reason": "Sign in to confirm you're not a bot"},
    "videoDetails": {"videoId": "abc123", "title": "Stream"},
}


def test_no_browser_means_no_fallback(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(youtube, "browser_available", lambda settings: (False, "not installed"))
    assert youtube._browser_fallback("https://youtu.be/abc", tmp_path, Settings()) is None


def test_browser_cookies_unlock_yt_dlp(monkeypatch, tmp_path: Path):
    called: dict = {}

    def fake_download_with_cookies(url, work_dir, settings, cookie_file, *, live=False):
        called["cookie_file"] = cookie_file
        called["live"] = live
        video = work_dir / "source.mp4"
        video.write_bytes(b"video")
        return {"id": "abc123", "title": "Stream", "duration": 90,
                "requested_downloads": [{"filepath": str(video)}]}

    page = BrowserPage(
        url="https://youtu.be/abc",
        ok=True,
        player_response=PR_BLOCKED,
        cookies=[{"name": "VISITOR_INFO1_LIVE", "value": "v", "domain": ".youtube.com"}],
    )
    monkeypatch.setattr(youtube, "browser_available", lambda settings: (True, "obscura 0.2.7"))
    monkeypatch.setattr(youtube, "fetch_page", lambda *a, **k: page)
    monkeypatch.setattr(youtube, "_download_with_cookie_file", fake_download_with_cookies)

    source = youtube._browser_fallback("https://youtu.be/abc", tmp_path, Settings())
    assert source is not None
    assert source.video_id == "abc123"
    assert source.path is not None and source.path.exists()
    assert called["cookie_file"].exists()
    assert "VISITOR_INFO1_LIVE" in called["cookie_file"].read_text()
    assert (tmp_path / "browser.json").exists()


def test_player_response_streams_are_downloaded_directly(monkeypatch, tmp_path: Path):
    page = BrowserPage(url="https://youtu.be/abc", ok=True, player_response=PR_OK, cookies=[])

    def fake_http(url, dest, **kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"stream")
        return dest

    monkeypatch.setattr(youtube, "browser_available", lambda settings: (True, "obscura 0.2.7"))
    monkeypatch.setattr(youtube, "fetch_page", lambda *a, **k: page)
    monkeypatch.setattr(youtube, "http_download", fake_http)

    source = youtube._browser_fallback("https://youtu.be/abc", tmp_path, Settings())
    assert source is not None
    assert source.title == "Stream"
    assert source.duration == 90.0
    assert source.chosen_format == "18"
    assert source.path is not None and source.path.read_bytes() == b"stream"
    assert source.to_dict()["file"].endswith("source.mp4")


def test_browser_refusal_returns_none(monkeypatch, tmp_path: Path):
    page = BrowserPage(url="https://youtu.be/abc", ok=True, player_response=PR_BLOCKED)
    monkeypatch.setattr(youtube, "browser_available", lambda settings: (True, "obscura 0.2.7"))
    monkeypatch.setattr(youtube, "fetch_page", lambda *a, **k: page)
    assert youtube._browser_fallback("https://youtu.be/abc", tmp_path, Settings()) is None


def test_two_tracks_are_muxed(monkeypatch, tmp_path: Path):
    page = BrowserPage(url="https://youtu.be/abc", ok=True, player_response=PR_OK)

    def fake_http(url, dest, **kwargs):
        dest.write_bytes(b"data")
        return dest

    def fake_mux(video, audio, work_dir, settings):
        target = work_dir / "source.mp4"
        target.write_bytes(b"muxed")
        return target

    monkeypatch.setattr(youtube, "browser_available", lambda settings: (True, "obscura 0.2.7"))
    monkeypatch.setattr(youtube, "fetch_page", lambda *a, **k: page)
    monkeypatch.setattr(youtube, "http_download", fake_http)
    monkeypatch.setattr(youtube, "_mux_with_ffmpeg", fake_mux)

    # progressive only -> no audio track, so the mux is not needed
    source = youtube._browser_fallback("https://youtu.be/abc", tmp_path, Settings())
    assert source is not None and source.path is not None
