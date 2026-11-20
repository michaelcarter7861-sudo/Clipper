"""Format listing, best-format choice and the download fallback ladder."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from clipper.config import Settings
from clipper.youtube import (
    PLAYER_CLIENTS,
    DownloadError,
    FormatRow,
    _attempts,
    _ffprobe_metadata,
    _format_rows,
    _friendly_error,
    _parse_extra_args,
    _ydl_opts,
    download,
    format_table,
    js_runtime_status,
    list_formats,
    youtube_reachable,
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
    fail_all: Exception | None = None
    on_download = None  # optional hook: (opts) -> info dict or Exception

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
        if FakeYoutubeDL.on_download is not None:
            item = FakeYoutubeDL.on_download(self.opts)
        elif FakeYoutubeDL.script:
            item = FakeYoutubeDL.script.pop(0)
        else:
            item = FakeYoutubeDL.fail_all or INFO
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture()
def fake_ytdlp(monkeypatch):
    FakeYoutubeDL.calls = []
    FakeYoutubeDL.probe_info = INFO
    FakeYoutubeDL.script = []
    FakeYoutubeDL.fail_all = None
    FakeYoutubeDL.on_download = None
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
    assert probe["format"] == "all"  # list every format, even when only images exist
    assert live["format"] == "b"
    assert live["live_from_start"] is True


def test_opts_can_pin_a_player_client_and_drop_cookies(tmp_path: Path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    settings = make_settings(cookies=cookies)

    pinned = _ydl_opts(settings, tmp_path, player_client="android")
    bare = _ydl_opts(settings, tmp_path, player_client="android", drop_cookies=True)

    assert pinned["extractor_args"]["youtube"]["player_client"] == ["android"]
    assert pinned["cookiefile"] == str(cookies)
    assert "cookiefile" not in bare


def test_opts_carry_proxy_and_cookies(tmp_path: Path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    opts = _ydl_opts(
        make_settings(proxy="http://127.0.0.1:8888", cookies=cookies), tmp_path
    )

    assert opts["proxy"] == "http://127.0.0.1:8888"
    assert opts["cookiefile"] == str(cookies)


def test_opts_hand_node_to_yt_dlp_as_a_js_runtime(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "clipper.youtube.shutil.which", lambda name: "/usr/local/bin/node" if name == "node" else None
    )

    opts = _ydl_opts(make_settings(), tmp_path)

    assert "node" in opts["js_runtimes"]
    assert "deno" in opts["js_runtimes"]  # yt-dlp's default stays available
    assert js_runtime_status() == {"node": "/usr/local/bin/node"}


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


def test_attempts_start_with_the_default_client_then_escalate():
    vod = _attempts(False)
    live = _attempts(True, has_cookies=True)

    assert vod[0].selector == "bv*+ba/b"
    assert vod[0].player_client is None
    assert [attempt.player_client for attempt in vod[1:1 + len(PLAYER_CLIENTS)]] == list(PLAYER_CLIENTS)
    assert live[0].selector == "b"  # live streams: ask for a single ready-made file first
    # cookie-free attempt only exists when there are cookies, and it comes last
    assert live[-1].drop_cookies is True and live[-1].player_client == "android"
    assert not any(attempt.drop_cookies for attempt in vod)


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #
def test_download_lists_formats_and_picks_the_first_working_one(fake_ytdlp, tmp_path: Path):
    (tmp_path / "source.mp4").write_bytes(b"video")
    fake_ytdlp.script = [
        RuntimeError("ERROR: [youtube] abc123def45: Requested format is not available"),
        RuntimeError("ERROR: [youtube] abc123def45: Requested format is not available"),
        INFO,
    ]

    source = download(URL, tmp_path, make_settings(out_dir=tmp_path / "out"))

    assert source.video_id == "abc123def45"
    assert source.chosen_format == "137"
    assert [row["id"] for row in source.formats] == ["137", "140"]
    # probe first (no download), then the ladder keeps the selector but switches player
    assert fake_ytdlp.calls[0]["download"] is False
    assert fake_ytdlp.calls[0]["opts"]["format"] == "all"
    attempts = [call["opts"] for call in fake_ytdlp.calls[1:]]
    assert [opts["format"] for opts in attempts] == ["bv*+ba/b", "bv*+ba/b", "bv*+ba/b"]
    assert "extractor_args" not in attempts[0]
    assert attempts[1]["extractor_args"]["youtube"]["player_client"] == ["android"]


def test_download_switches_player_client_when_the_first_one_has_no_stream(fake_ytdlp, tmp_path: Path):
    (tmp_path / "source.mp4").write_bytes(b"video")

    def on_download(opts):
        if "extractor_args" not in opts:
            return RuntimeError("ERROR: Requested format is not available")
        return INFO

    fake_ytdlp.on_download = on_download

    source = download(URL, tmp_path, make_settings(out_dir=tmp_path / "out"))

    assert source.video_id == "abc123def45"
    assert "extractor_args" not in fake_ytdlp.calls[1]["opts"]
    assert fake_ytdlp.calls[2]["opts"]["extractor_args"]["youtube"]["player_client"] == ["android"]


def test_download_mentions_the_formats_it_saw_when_everything_fails(fake_ytdlp, tmp_path: Path):
    fake_ytdlp.fail_all = RuntimeError("ERROR: Requested format is not available")
    out_dir = tmp_path / "out"

    with pytest.raises(DownloadError) as excinfo:
        download(URL, tmp_path, make_settings(out_dir=out_dir))

    message = str(excinfo.value)
    assert "did not offer a format" in message
    assert "137=mp4/1080p" in message
    assert "players tried" in message


def test_download_reports_storyboards_as_a_throttled_ip(fake_ytdlp, tmp_path: Path):
    fake_ytdlp.probe_info = {
        "id": "abc123def45",
        "title": "Test video",
        "duration": 0,
        "live_status": "was_live",
        "formats": [{"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "acodec": "none"}],
    }
    fake_ytdlp.fail_all = RuntimeError("ERROR: Requested format is not available")

    with pytest.raises(DownloadError) as excinfo:
        download(URL, tmp_path, make_settings(out_dir=tmp_path / "out"))

    message = str(excinfo.value)
    assert "storyboard images only" in message
    assert "YT_PROXY" in message


def test_page_reload_errors_are_retried_not_fatal(fake_ytdlp, tmp_path: Path):
    (tmp_path / "source.mp4").write_bytes(b"video")
    fake_ytdlp.script = [
        RuntimeError("ERROR: [youtube] abc123def45: The page needs to be reloaded."),
        RuntimeError("ERROR: [youtube] abc123def45: The page needs to be reloaded."),
        INFO,
    ]

    source = download(URL, tmp_path, make_settings(out_dir=tmp_path / "out"))

    assert source.video_id == "abc123def45"
    assert len([call for call in fake_ytdlp.calls if call.get("download")]) == 3


def test_unknown_fatal_errors_do_not_crash_the_message(fake_ytdlp, tmp_path: Path):
    fake_ytdlp.fail_all = RuntimeError("ERROR: something unexpected broke")

    with pytest.raises(DownloadError) as excinfo:
        download(URL, tmp_path, make_settings(out_dir=tmp_path / "out"))

    assert "something unexpected broke" in str(excinfo.value)


def test_formats_report_records_the_js_runtime(fake_ytdlp, tmp_path: Path):
    fake_ytdlp.fail_all = RuntimeError("ERROR: Requested format is not available")
    out_dir = tmp_path / "out"

    with pytest.raises(DownloadError):
        download(URL, tmp_path, make_settings(out_dir=out_dir))

    assert "js_runtime:" in (out_dir / "formats.txt").read_text(encoding="utf-8")


def test_storyboard_message_points_at_the_js_runtime(monkeypatch):
    monkeypatch.setattr("clipper.youtube.shutil.which", lambda name: None)

    message = _friendly_error(
        "ERROR: Requested format is not available", [], images_only=True, clients_tried=["client tv"]
    )

    assert "No JS runtime" in message
    assert "client tv" in message


def test_a_failed_download_leaves_a_formats_report(fake_ytdlp, tmp_path: Path):
    fake_ytdlp.fail_all = RuntimeError("ERROR: Requested format is not available")
    out_dir = tmp_path / "out"

    with pytest.raises(DownloadError):
        download(URL, tmp_path, make_settings(out_dir=out_dir))

    report = (out_dir / "formats.txt").read_text(encoding="utf-8")
    assert URL in report
    assert "formats listed by yt-dlp: 3" in report
    assert "default client" in report


def test_download_retries_a_live_stream_from_the_start(fake_ytdlp, tmp_path: Path):
    (tmp_path / "source.mp4").write_bytes(b"video")
    fake_ytdlp.probe_info = dict(INFO, live_status="is_live")

    source = download(URL, tmp_path, make_settings(out_dir=tmp_path / "out"))

    assert source.live_status == "is_live"
    assert fake_ytdlp.calls[1]["opts"]["format"] == "b"
    assert fake_ytdlp.calls[1]["opts"]["live_from_start"] is True


def test_upcoming_streams_are_rejected_with_a_clear_message(fake_ytdlp, tmp_path: Path):
    fake_ytdlp.probe_info = dict(INFO, live_status="is_upcoming")

    with pytest.raises(DownloadError, match="upcoming"):
        download(URL, tmp_path, make_settings(out_dir=tmp_path / "out"))

    assert len(fake_ytdlp.calls) == 1  # only the probe ran


def test_bot_check_is_fatal_and_points_at_cookies(fake_ytdlp, tmp_path: Path):
    fake_ytdlp.probe_info = RuntimeError("ERROR: Sign in to confirm you're not a bot")

    with pytest.raises(DownloadError, match="cookies.txt"):
        download(URL, tmp_path, make_settings(out_dir=tmp_path / "out"))


def test_a_flaky_probe_still_lets_the_download_run(fake_ytdlp, tmp_path: Path):
    (tmp_path / "source.mp4").write_bytes(b"video")
    fake_ytdlp.probe_info = RuntimeError("something odd happened")

    source = download(URL, tmp_path, make_settings(out_dir=tmp_path / "out"))

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


def test_friendly_error_explains_storyboard_only_answers():
    message = _friendly_error(
        "ERROR: Requested format is not available",
        [],
        images_only=True,
        clients_tried=["default client", "client android"],
    )

    assert "storyboard images only" in message
    assert "client android" in message
    assert "YT_PROXY" in message and "YT_COOKIES" in message


def test_list_formats_explains_storyboard_only_answers(fake_ytdlp):
    fake_ytdlp.probe_info = {
        "id": "abc123def45",
        "title": "Test video",
        "formats": [{"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "acodec": "none"}],
    }

    assert list_formats(URL, make_settings()) == 1


def test_local_files_skip_youtube_entirely(fake_ytdlp, tmp_path: Path, monkeypatch):
    video = tmp_path / "my talk.mp4"
    video.write_bytes(b"not really a video")
    (tmp_path / "my talk.vtt").write_text("WEBVTT\n", encoding="utf-8")
    monkeypatch.setattr("clipper.youtube.shutil.which", lambda name: None)

    source = download(str(video), tmp_path / "work", make_settings(out_dir=tmp_path / "out"))

    assert source.path == video
    assert source.title == "my talk"
    assert source.subtitle_files == [tmp_path / "my talk.vtt"]
    assert fake_ytdlp.calls == []  # yt-dlp was never touched


def test_ffprobe_metadata_reads_duration_and_size(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("clipper.youtube.shutil.which", lambda name: "/usr/bin/ffprobe")

    class Result:
        stdout = '{"streams": [{"width": 1920, "height": 1080}], "format": {"duration": "1295.4"}}'

    monkeypatch.setattr("clipper.youtube.subprocess.run", lambda *a, **k: Result())

    assert _ffprobe_metadata(tmp_path / "v.mp4", make_settings()) == (1295.4, 1920, 1080)


def test_ffprobe_metadata_is_optional(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("clipper.youtube.shutil.which", lambda name: None)

    assert _ffprobe_metadata(tmp_path / "v.mp4", make_settings()) == (0.0, 0, 0)


def test_file_urls_point_at_local_files(fake_ytdlp, tmp_path: Path, monkeypatch):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr("clipper.youtube.shutil.which", lambda name: None)

    source = download(f"file://{video}", tmp_path / "work", make_settings(out_dir=tmp_path / "out"))

    assert source.path == video
    assert fake_ytdlp.calls == []


def test_youtube_reachable_reports_storyboards_as_unreachable(fake_ytdlp):
    fake_ytdlp.probe_info = {"id": "x", "formats": [{"format_id": "sb0", "ext": "mhtml"}]}

    ok, reason = youtube_reachable(make_settings())

    assert ok is False
    assert "none downloadable" in reason


def test_youtube_reachable_true_with_a_real_format(fake_ytdlp):
    ok, reason = youtube_reachable(make_settings())

    assert ok is True
    assert "downloadable format(s)" in reason


def test_failure_says_the_ip_is_blocked_when_the_test_video_fails_too(fake_ytdlp, tmp_path: Path, monkeypatch):
    fake_ytdlp.fail_all = RuntimeError("ERROR: Requested format is not available")
    monkeypatch.setattr(
        "clipper.youtube.youtube_reachable", lambda settings, url=None: (False, "test video listed 4")
    )

    with pytest.raises(DownloadError) as excinfo:
        download(URL, tmp_path, make_settings(out_dir=tmp_path / "out"))

    message = str(excinfo.value)
    assert "blocking this machine's IP" in message
    assert "YT_PROXY" in message


def test_storyboard_diagnosis_beats_a_misleading_bot_check():
    # the cookie-free attempt ends in a bot check; the storyboard diagnosis must still win
    message = _friendly_error("Sign in to confirm you're not a bot", [], images_only=True)

    assert "storyboard images only" in message


def test_cli_check_youtube_exit_codes(monkeypatch):
    from clipper.cli import main

    monkeypatch.setattr("clipper.youtube.youtube_reachable", lambda settings, url=None: (True, "ok"))
    assert main(["--check-youtube"]) == 0

    monkeypatch.setattr("clipper.youtube.youtube_reachable", lambda settings, url=None: (False, "blocked"))
    assert main(["--check-youtube"]) == 1


def test_friendly_error_keeps_the_old_cases():
    assert "cookies.txt" in _friendly_error("Sign in to confirm you're not a bot")
    assert "private" in _friendly_error("ERROR: Private video. Sign in if you've been granted access")
    assert "members-only" in _friendly_error("ERROR: This video is members-only")
