"""Format listing, best-format choice and the download fallback ladder."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from clipper.config import Settings
from clipper.youtube import (
    DownloadError,
    FormatRow,
    _attempts,
    _format_rows,
    _friendly_error,
    _parse_extra_args,
    _ydl_opts,
    download,
    format_table,
    list_formats,
)

URL = "https://www.youtube.com/live/abc123def45"

INFO = {
    "id": "abc123def45",
    "title": "Test video",
    "duration": 125.0,
    "uploader": "tester",
    "webpage_url": URL,
    "live_status": "not_live",
    "format_id": "137",
    "formats": [
        {
            "format_id": "137",
            "ext": "mp4",
            "height": 1080,
            "fps": 30,
            "vcodec": "avc1.640028",
            "acodec": "none",
            "filesize": 45_000_000,
            "tbr": 3000,
        },
        {
            "format_id": "140",
            "ext": "m4a",
            "height": 0,
            "vcodec": "none",
            "acodec": "mp4a.40.2",
            "filesize": 2_000_000,
            "tbr": 128,
        },
        {
            "format_id": "sb0",
            "ext": "mhtml",
            "vcodec": "none",
            "acodec": "none",
            "format_note": "storyboard",
        },
    ],
}


def make_settings(**kwargs) -> Settings:
    kwargs.setdefault("urls", [URL])
    return Settings(**kwargs)


class FakeYoutubeDL:
    """Stand-in for ``yt_dlp.YoutubeDL`` that records every call."""

    probe_info: dict | None = None
    script: list = []

    def __init__(self, opts: dict) -> None:
        self.opts = opts
        FakeYoutubeDL.calls.append({"opts": dict(opts)})

    def __enter__(self) -> FakeYoutubeDL:
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def extract_info(self, url: str, download: bool = False):
        FakeYoutubeDL.calls[-1].update({"url": url, "download": download})
        if not download:
            if isinstance(FakeYoutubeDL.probe_info, Exception):
                raise FakeYoutubeDL.probe_info
            return FakeYoutubeDL.probe_info
        item = FakeYoutubeDL.script.pop(0) if FakeYoutubeDL.script else INFO
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture()
def fake_ytdlp(monkeypatch):
    FakeYoutubeDL.calls = []
    FakeYoutubeDL.probe_info = INFO
    FakeYoutubeDL.script = []
    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = FakeYoutubeDL
    module.version = types.SimpleNamespace(__version__="test")
    monkeypatch.setitem(sys.modules, "yt_dlp", module)
    return FakeYoutubeDL


# --------------------------------------------------------------------------- #
# options
# --------------------------------------------------------------------------- #
def test_opts_ask_for_the_best_and_sort_by_height(tmp_path: Path):
    opts = _ydl_opts(make_settings(), tmp_path)

    assert opts["format"] == "bv*+ba/b/best"
    assert opts["format_sort"][0] == "res:1080"
    assert opts["merge_output_format"] == "mp4"
    assert "skip_download" not in opts
    assert "live_from_start" not in opts


def test_opts_for_probe_and_live(tmp_path: Path):
    probe = _ydl_opts(make_settings(), tmp_path, probe=True)
    live = _ydl_opts(make_settings(), tmp_path, format_selector="b", live_from_start=True)

    assert probe["skip_download"] is True
    assert live["format"] == "b"
    assert live["live_from_start"] is True


def test_opts_carry_proxy_and_cookies(tmp_path: Path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    opts = _ydl_opts(
        make_settings(proxy="http://127.0.0.1:8888", cookies=cookies), tmp_path
    )

    assert opts["proxy"] == "http://127.0.0.1:8888"
    assert opts["cookiefile"] == str(cookies)


def test_extra_args_only_override_what_the_user_asked_for():
    pytest.importorskip("yt_dlp")

    changed = _parse_extra_args(["--proxy", "http://127.0.0.1:8888"])

    assert changed["proxy"] == "http://127.0.0.1:8888"
    # defaults must not leak in and clobber our own outtmpl/format/subtitle settings
    assert "outtmpl" not in changed
    assert "format" not in changed
    assert "writesubtitles" not in changed


# --------------------------------------------------------------------------- #
# format listing
# --------------------------------------------------------------------------- #
def test_format_rows_keep_video_and_audio_but_drop_storyboards():
    rows = _format_rows(INFO)

    assert [row.id for row in rows] == ["137", "140"]
    assert rows[0].res == "1080p"
    assert rows[1].res == "audio"


def test_format_table_is_readable():
    table = "\n".join(format_table(_format_rows(INFO)))

    assert "id" in table and "res" in table
    assert "137" in table and "1080p" in table
    assert "audio" in table
    assert "sb0" not in table


def test_format_table_survives_an_empty_list():
    assert "no downloadable format" in format_table([])[0]


def test_attempts_start_with_the_constrained_pick_for_vod_and_loose_for_live():
    assert [selector for _, selector in _attempts(False)] == ["bv*+ba/b", "best", "b"]
    assert [selector for _, selector in _attempts(True)] == ["b", "best", "bv*+ba/b"]


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #
def test_download_lists_formats_and_picks_the_first_working_one(fake_ytdlp, tmp_path: Path):
    (tmp_path / "source.mp4").write_bytes(b"video")
    fake_ytdlp.script = [
        RuntimeError("ERROR: [youtube] abc123def45: Requested format is not available"),
        INFO,
    ]

    source = download(URL, tmp_path, make_settings())

    assert source.video_id == "abc123def45"
    assert source.chosen_format == "137"
    assert [row["id"] for row in source.formats] == ["137", "140"]
    # probe first (no download), then two attempts: constrained pick, then "best"
    assert fake_ytdlp.calls[0]["download"] is False
    attempts = [call["opts"]["format"] for call in fake_ytdlp.calls[1:]]
    assert attempts == ["bv*+ba/b", "best"]


def test_download_mentions_the_formats_it_saw_when_everything_fails(fake_ytdlp, tmp_path: Path):
    fake_ytdlp.script = [
        RuntimeError("ERROR: Requested format is not available"),
        RuntimeError("ERROR: Requested format is not available"),
        RuntimeError("ERROR: Requested format is not available"),
    ]

    with pytest.raises(DownloadError) as excinfo:
        download(URL, tmp_path, make_settings())

    message = str(excinfo.value)
    assert "did not offer a format" in message
    assert "137=mp4/1080p" in message


def test_download_retries_a_live_stream_from_the_start(fake_ytdlp, tmp_path: Path):
    (tmp_path / "source.mp4").write_bytes(b"video")
    fake_ytdlp.probe_info = dict(INFO, live_status="is_live")

    source = download(URL, tmp_path, make_settings())

    assert source.live_status == "is_live"
    assert fake_ytdlp.calls[1]["opts"]["format"] == "b"
    assert fake_ytdlp.calls[1]["opts"]["live_from_start"] is True


def test_upcoming_streams_are_rejected_with_a_clear_message(fake_ytdlp, tmp_path: Path):
    fake_ytdlp.probe_info = dict(INFO, live_status="is_upcoming")

    with pytest.raises(DownloadError, match="upcoming"):
        download(URL, tmp_path, make_settings())

    assert len(fake_ytdlp.calls) == 1  # only the probe ran


def test_bot_check_is_fatal_and_points_at_cookies(fake_ytdlp, tmp_path: Path):
    fake_ytdlp.probe_info = RuntimeError("ERROR: Sign in to confirm you're not a bot")

    with pytest.raises(DownloadError, match="cookies.txt"):
        download(URL, tmp_path, make_settings())


def test_a_flaky_probe_still_lets_the_download_run(fake_ytdlp, tmp_path: Path):
    (tmp_path / "source.mp4").write_bytes(b"video")
    fake_ytdlp.probe_info = RuntimeError("something odd happened")

    source = download(URL, tmp_path, make_settings())

    assert source.video_id == "abc123def45"
    assert fake_ytdlp.calls[1]["download"] is True


def test_list_formats_prints_without_downloading(fake_ytdlp):
    assert list_formats(URL, make_settings()) == 0
    assert fake_ytdlp.calls and fake_ytdlp.calls[0]["download"] is False


def test_cli_list_formats_flag(fake_ytdlp):
    from clipper.cli import main

    assert main(["--list-formats", "--url", URL]) == 0
    assert fake_ytdlp.calls[0]["download"] is False
    assert main(["--list-formats"]) == 2


# --------------------------------------------------------------------------- #
# error messages
# --------------------------------------------------------------------------- #
def test_friendly_error_for_format_problems_mentions_live_and_formats():
    rows = [FormatRow(id="137", ext="mp4", height=1080)]

    vod = _friendly_error("ERROR: Requested format is not available", rows)
    live = _friendly_error("ERROR: Requested format is not available", rows, live=True)

    assert "137=mp4/1080p" in vod
    assert "proxy" in vod.lower()
    assert "live stream" in live
    assert "did not offer a format" in _friendly_error("Requested format is not available")


def test_friendly_error_keeps_the_old_cases():
    assert "cookies.txt" in _friendly_error("Sign in to confirm you're not a bot")
    assert "private" in _friendly_error("ERROR: Private video. Sign in if you've been granted access")
    assert "members-only" in _friendly_error("ERROR: This video is members-only")
