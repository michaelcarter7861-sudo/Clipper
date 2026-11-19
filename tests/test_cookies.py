from __future__ import annotations

import base64
from pathlib import Path

from clipper.cookies import (
    analyse,
    decode_secret,
    looks_like_netscape,
    main,
    normalise,
    parse_cookies,
)

COOKIES = """# Netscape HTTP Cookie File
# https://curl.se/docs/http-cookies.html
.youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\tsecret-sid-value
.youtube.com\tTRUE\t/\tTRUE\t1893456000\tHSID\thash-value
.youtube.com\tFALSE\t/\tFALSE\t0\tPREF\ttz=Asia/Karachi
.google.com\tTRUE\t/\tTRUE\t1893456000\t__Secure-3PSID\tpsid-value
"""


def test_parse_netscape_file():
    cookies = parse_cookies(COOKIES)
    assert len(cookies) == 4
    sid = next(cookie for cookie in cookies if cookie.name == "SID")
    assert sid.domain == ".youtube.com"
    assert sid.secure is True
    assert sid.is_session is False


def test_analyse_good_file():
    report = analyse(parse_cookies(COOKIES))
    assert report.ok
    assert report.youtube == 4
    assert "SID" in report.logins
    assert not report.problems


def test_analyse_anonymous_export():
    text = "youtube.com\tFALSE\t/\tFALSE\t0\tPREF\ttz=UTC\n"
    report = analyse(parse_cookies(text))
    assert not report.ok
    assert any("login cookies" in problem for problem in report.problems)


def test_analyse_empty_or_garbage():
    for text in ("", "hello world", "<html>nope</html>"):
        report = analyse(parse_cookies(text))
        assert not report.ok
        assert report.problems


def test_expired_cookies_are_flagged():
    text = ".youtube.com\tTRUE\t/\tTRUE\t1000000000\tSID\told\n"
    report = analyse(parse_cookies(text))
    assert report.expired == 1
    assert not report.ok


def test_decode_secret_handles_base64_and_raw():
    encoded = base64.b64encode(COOKIES.encode()).decode()
    assert decode_secret(encoded).strip() == COOKIES.strip()
    assert decode_secret(COOKIES).strip() == COOKIES.strip()
    assert decode_secret("") == ""


def test_looks_like_netscape():
    assert looks_like_netscape(COOKIES)
    assert looks_like_netscape("youtube.com\tFALSE\t/\tFALSE\t0\tA\tb")
    assert not looks_like_netscape("just some text")


def test_normalise_roundtrip():
    text = normalise(COOKIES)
    assert text.startswith("# Netscape HTTP Cookie File")
    assert parse_cookies(text) == parse_cookies(COOKIES)


def test_cli_writes_normalised_file(tmp_path: Path):
    source = tmp_path / "cookies.txt"
    source.write_text(COOKIES, encoding="utf-8")
    target = tmp_path / "clean.txt"
    assert main([str(source), "--out", str(target)]) == 0
    assert "SID" in target.read_text()


def test_cli_accepts_check_flag(tmp_path: Path):
    source = tmp_path / "cookies.txt"
    source.write_text(COOKIES, encoding="utf-8")
    assert main(["--check", str(source)]) == 0


def test_cli_reports_missing_file(tmp_path: Path):
    assert main([str(tmp_path / "nope.txt")]) == 2


def test_cli_from_secret(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("YT_COOKIES", base64.b64encode(COOKIES.encode()).decode())
    target = tmp_path / "from-env.txt"
    assert main(["--from-secret", "--out", str(target)]) == 0
    assert "secret-sid-value" in target.read_text()


def test_cli_from_secret_absent(monkeypatch):
    monkeypatch.delenv("YT_COOKIES", raising=False)
    monkeypatch.delenv("COOKIES", raising=False)
    monkeypatch.delenv("CLIP_COOKIES_SECRET", raising=False)
    assert main(["--from-secret"]) == 0


def test_cli_strict_mode_fails_on_bad_cookies(tmp_path: Path):
    bad = tmp_path / "bad.txt"
    bad.write_text("youtube.com\tFALSE\t/\tFALSE\t0\tPREF\ttz=UTC\n", encoding="utf-8")
    assert main([str(bad), "--strict"]) == 1
