from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from clipper import browser
from clipper.browser import (
    BrowserPage,
    browser_available,
    browser_binary,
    fetch_page,
    harvest_cookies,
    http_download,
    netscape_cookies,
    pick_streams,
    playability,
    streams,
    video_details,
    write_cookies,
)
from clipper.config import Settings

PLAYER_RESPONSE = {
    "playabilityStatus": {"status": "OK"},
    "videoDetails": {
        "videoId": "DENbjnmSems",
        "title": "Test stream",
        "lengthSeconds": "3600",
        "author": "Someone",
    },
    "streamingData": {
        "formats": [
            {
                "itag": 18,
                "url": "https://example.test/v18",
                "mimeType": "video/mp4; codecs=\"avc1.42001E, mp4a.40.2\"",
                "height": 360,
                "width": 640,
            },
            {
                "itag": 22,
                "url": "https://example.test/v22",
                "mimeType": "video/mp4",
                "height": 720,
                "width": 1280,
            },
        ],
        "adaptiveFormats": [
            {
                "itag": 137,
                "url": "https://example.test/a137",
                "mimeType": "video/mp4",
                "height": 1080,
                "bitrate": 4000000,
            },
            {
                "itag": 140,
                "url": "https://example.test/a140",
                "mimeType": "audio/mp4",
                "bitrate": 128000,
            },
            {"itag": 999, "mimeType": "video/mp4", "height": 1080},  # no url: unusable
        ],
    },
}

COOKIES = [
    {
        "name": "VISITOR_INFO1_LIVE",
        "value": "abc123",
        "domain": ".youtube.com",
        "path": "/",
        "secure": True,
        "http_only": True,
        "expires": 1790000000,
    },
    {"name": "PREF", "value": "tz=UTC", "domain": "youtube.com", "path": "/", "expires": -1},
    {"name": "empty", "value": "", "domain": ".youtube.com"},
]


# --------------------------------------------------------------------------- #
# parsing helpers
# --------------------------------------------------------------------------- #
def test_playability_reads_the_status_and_reason():
    assert playability(PLAYER_RESPONSE) == ("OK", "")
    bot = {"playabilityStatus": {"status": "LOGIN_REQUIRED", "reason": "Sign in to confirm"}}
    assert playability(bot) == ("LOGIN_REQUIRED", "Sign in to confirm")
    assert playability(None) == ("", "")
    assert playability({"playabilityStatus": "nonsense"}) == ("", "")


def test_streams_ignores_formats_without_a_url():
    data = streams(PLAYER_RESPONSE)
    assert len(data["formats"]) == 2
    assert len(data["adaptive"]) == 2
    assert streams(None) == {"formats": [], "adaptive": []}


def test_video_details_survives_junk():
    assert video_details(PLAYER_RESPONSE)["videoId"] == "DENbjnmSems"
    assert video_details(None) == {}
    assert video_details({"videoDetails": "nope"}) == {}


def test_pick_streams_prefers_one_progressive_file():
    video, audio = pick_streams(PLAYER_RESPONSE, max_height=1080)
    assert video is not None and audio is None
    assert video["url"] == "https://example.test/v22"  # the taller progressive one


def test_pick_streams_respects_max_height():
    video, _ = pick_streams(PLAYER_RESPONSE, max_height=480)
    assert video is not None and video["height"] == 360


def test_pick_streams_falls_back_to_two_tracks():
    adaptive_only = {
        "streamingData": {
            "formats": [],
            "adaptiveFormats": PLAYER_RESPONSE["streamingData"]["adaptiveFormats"],
        }
    }
    video, audio = pick_streams(adaptive_only, max_height=1080)
    assert video is not None and video["itag"] == 137
    assert audio is not None and audio["itag"] == 140


def test_a_page_with_no_streams_is_not_downloadable():
    page = BrowserPage(url="u", ok=True, player_response={"streamingData": {}})
    assert page.downloadable is False
    assert "0 stream(s)" in page.describe()
    blocked = BrowserPage(
        url="u", ok=True, player_response={"playabilityStatus": {"status": "LOGIN_REQUIRED"}}
    )
    assert blocked.downloadable is False
    assert "LOGIN_REQUIRED" in blocked.describe()


# --------------------------------------------------------------------------- #
# cookies
# --------------------------------------------------------------------------- #
def test_netscape_cookies_are_valid_lines():
    text = netscape_cookies(COOKIES)
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert len(lines) == 2  # the empty value is dropped
    fields = lines[0].split("\t")
    assert len(fields) == 7
    assert fields[0] == ".youtube.com"
    assert fields[1] == "TRUE"
    assert fields[3] == "TRUE"  # secure
    assert fields[4] == "1790000000"
    assert fields[5] == "VISITOR_INFO1_LIVE"
    assert fields[6] == "abc123"
    # Domain without a dot gets one so subdomains match.
    assert lines[1].startswith(".youtube.com\t")


def test_write_cookies_makes_a_private_file(tmp_path):
    path = write_cookies(COOKIES, tmp_path / "cookies.txt")
    assert path is not None and path.exists()
    assert "VISITOR_INFO1_LIVE" in path.read_text()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert write_cookies([], tmp_path / "none.txt") is None


# --------------------------------------------------------------------------- #
# a fake browser binary: the CLI contract, without downloading obscura
# --------------------------------------------------------------------------- #
@pytest.fixture()
def fake_browser(tmp_path: Path) -> Path:
    """A stand-in for obscura that answers --eval and --dump cookies."""
    script = tmp_path / "obscura"
    payload = json.dumps(json.dumps(PLAYER_RESPONSE))  # a stringified player response
    script.write_text(
        f"""#!{sys.executable}
import json, sys
args = sys.argv[1:]
if "--version" in args:
    print("obscura 0.2.7")
    sys.exit(0)
if sys.argv[0].endswith("obscura-broken"):
    print("boom", file=sys.stderr)
    sys.exit(2)
if "--dump" in args and args[args.index("--dump") + 1] == "cookies":
    print(json.dumps(json.loads({json.dumps(json.dumps(COOKIES))})))
    sys.exit(0)
if "--eval" in args:
    print({payload})
    sys.exit(0)
print("")
"""
    )
    script.chmod(0o755)
    return script


def test_browser_available_reports_the_version(fake_browser: Path):
    ok, why = browser_available(Settings(browser=str(fake_browser)))
    assert ok is True
    assert "obscura" in why


def test_browser_available_is_honest_when_missing(tmp_path: Path):
    ok, why = browser_available(Settings(browser=str(tmp_path / "nope")))
    assert ok is False
    assert "not found" in why


def test_fetch_page_reads_the_player_response_and_cookies(fake_browser: Path, tmp_path: Path):
    settings = Settings(browser=str(fake_browser))
    page = fetch_page("https://youtu.be/x", settings, storage_dir=tmp_path / "profile")
    assert page.ok is True
    assert page.status == "OK"
    assert page.downloadable is True
    assert [cookie["name"] for cookie in page.cookies] == ["VISITOR_INFO1_LIVE", "PREF", "empty"]
    assert page.binary == str(fake_browser)
    assert (tmp_path / "profile").exists()


def test_fetch_page_reports_a_broken_browser(tmp_path: Path, fake_browser: Path):
    broken = tmp_path / "obscura-broken"
    broken.write_text(fake_browser.read_text())
    broken.chmod(0o755)
    page = fetch_page("https://youtu.be/x", Settings(browser=str(broken)))
    assert page.ok is False
    assert page.error


def test_harvest_cookies_accepts_a_wrapped_jar(tmp_path: Path):
    script = tmp_path / "obscura"
    script.write_text(
        f"""#!{sys.executable}
import json, sys
print(json.dumps({{"cookies": json.loads({json.dumps(json.dumps(COOKIES))})}}))
"""
    )
    script.chmod(0o755)
    cookies = harvest_cookies("https://youtu.be/x", Settings(browser=str(script)))
    assert len(cookies) == 3


def test_command_respects_stealth_and_proxy_flags(fake_browser: Path):
    plain = browser._command(Settings(browser=str(fake_browser)), "fetch", "u")
    assert "--stealth" not in plain
    loud = browser._command(
        Settings(browser=str(fake_browser), browser_stealth=True, proxy="http://p:1"), "fetch", "u"
    )
    assert "--stealth" in loud
    assert "--proxy" in loud and "http://p:1" in loud


def test_browser_binary_prefers_settings_then_path(fake_browser: Path):
    assert browser_binary(Settings(browser=str(fake_browser))) == str(fake_browser)
    assert browser_binary(Settings()) == "" or Path(browser_binary(Settings())).exists()


# --------------------------------------------------------------------------- #
# direct download
# --------------------------------------------------------------------------- #
def test_http_download_writes_the_bytes(tmp_path: Path, monkeypatch):
    class FakeResponse:
        def __init__(self) -> None:
            self.chunks = [b"abc", b"def", b""]

        def read(self, size: int) -> bytes:
            return self.chunks.pop(0)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(browser.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    dest = http_download("https://example.test/v", tmp_path / "out.bin")
    assert dest.read_bytes() == b"abcdef"


def test_http_download_rejects_an_empty_body(tmp_path: Path, monkeypatch):
    class Empty:
        def read(self, size: int) -> bytes:
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(browser.urllib.request, "urlopen", lambda *a, **k: Empty())
    with pytest.raises(RuntimeError):
        http_download("https://example.test/v", tmp_path / "out.bin")
