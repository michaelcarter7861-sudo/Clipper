"""Cookie helpers: check, normalise and test a YouTube `cookies.txt`.

The supported way to get past YouTube's "confirm you're not a bot" wall is to use
your own logged-in browser session. This module makes that painless:

    python -m clipper.cookies --check cookies.txt
    python -m clipper.cookies --check cookies.txt --test
    YT_COOKIES=... python -m clipper.cookies --from-secret --out cookies.txt

It never bypasses anything: it validates the file you exported, tells you what is
wrong with it, and (optionally) asks YouTube whether that session works.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

NETSCAPE_HEADER = "# Netscape HTTP Cookie File"
YOUTUBE_DOMAINS = ("youtube.com", "youtu.be", "google.com", "googlevideo.com")
TEST_URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"  # Big Buck Bunny, public


@dataclass
class Cookie:
    domain: str
    path: str
    secure: bool
    expires: int
    name: str
    value: str

    @property
    def is_session(self) -> bool:
        return self.expires == 0

    @property
    def is_expired(self) -> bool:
        import time

        return self.expires not in (0, -1) and self.expires < time.time()

    def to_line(self) -> str:
        return "\t".join(
            [
                self.domain,
                "TRUE" if self.domain.startswith(".") else "FALSE",
                self.path or "/",
                "TRUE" if self.secure else "FALSE",
                str(self.expires),
                self.name,
                self.value,
            ]
        )


@dataclass
class CookieReport:
    ok: bool
    total: int = 0
    youtube: int = 0
    expired: int = 0
    logins: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "cookies": self.total,
            "youtube_cookies": self.youtube,
            "expired": self.expired,
            "accounts": self.logins,
            "domains": self.domains,
            "problems": self.problems,
            "notes": self.notes,
        }


def decode_secret(raw: str) -> str:
    """Accept raw cookies.txt text or its base64 form (what people paste into CI)."""
    text = (raw or "").strip()
    if not text:
        return ""
    if looks_like_netscape(text):
        return text.replace("\r\n", "\n")

    compact = re.sub(r"\s+", "", text)
    try:
        decoded = base64.b64decode(compact, validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return text
    return decoded if looks_like_netscape(decoded) else text


def looks_like_netscape(text: str) -> bool:
    if NETSCAPE_HEADER.lower() in (text or "").lower():
        return True
    for line in (text or "").splitlines():
        if line.count("\t") >= 6 and not line.startswith("#"):
            return True
    return False


def parse_cookies(text: str) -> list[Cookie]:
    cookies: list[Cookie] = []
    for raw_line in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw_line.strip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            # some exporters use spaces; recover the common case
            parts = re.split(r"\s{2,}|\t", line)
            if len(parts) < 7:
                continue
        domain, _flag, path, secure, expires, name, value = parts[:7]
        try:
            expires_int = int(float(expires))
        except ValueError:
            expires_int = 0
        if "." not in domain and not domain.startswith("localhost"):
            continue
        cookies.append(
            Cookie(
                domain=domain,
                path=path or "/",
                secure=secure.strip().upper() == "TRUE",
                expires=expires_int,
                name=name.strip(),
                value="\t".join(parts[6:]).strip(),
            )
        )
    return cookies


def analyse(cookies: list[Cookie]) -> CookieReport:
    report = CookieReport(ok=False, total=len(cookies))

    if not cookies:
        report.problems.append(
            "no cookie lines found — is this really a Netscape-format cookies.txt?"
        )
        return report

    report.youtube = sum(
        1 for cookie in cookies if any(d in cookie.domain for d in YOUTUBE_DOMAINS)
    )
    report.expired = sum(1 for cookie in cookies if cookie.is_expired)
    report.domains = sorted({cookie.domain.lstrip(".") for cookie in cookies})[:12]

    login_cookies = {"SID", "SSID", "HSID", "APISID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID"}
    report.logins = sorted({cookie.name for cookie in cookies if cookie.name in login_cookies})

    if report.youtube == 0:
        report.problems.append(
            "no YouTube/Google cookies in the file — export them while youtube.com is open "
            "and logged in"
        )
    if not report.logins:
        report.problems.append(
            "no Google login cookies (SID/HSID/__Secure-*PSID) — the session is anonymous, "
            "so it will not get past the bot check"
        )
    if report.total and report.expired == report.total:
        report.problems.append("every cookie has expired — export a fresh cookies.txt")

    report.ok = report.youtube > 0 and bool(report.logins) and report.expired < report.total
    if report.expired and report.expired < report.total:
        report.notes.append(f"{report.expired} of {report.total} cookies are already expired")
    report.notes.append("cookies are credentials: keep them in a secret, never in git")
    return report


def normalise(text: str) -> str:
    """Rewrite a cookie export into a clean Netscape file yt-dlp accepts."""
    cookies = parse_cookies(text)
    lines = [NETSCAPE_HEADER, "# Written by clipper.cookies", ""]
    lines.extend(cookie.to_line() for cookie in cookies)
    return "\n".join(lines) + "\n"


def read_secret() -> str:
    for name in ("YT_COOKIES", "CLIP_COOKIES_SECRET", "COOKIES"):
        raw = os.environ.get(name)
        if raw and raw.strip():
            return raw
    return ""


def test_against_youtube(path: Path, url: str = TEST_URL, *, timeout: int = 90) -> tuple[bool, str]:
    """Ask yt-dlp whether that cookie file can talk to YouTube right now."""
    try:
        import yt_dlp
    except ImportError:
        return False, "yt-dlp is not installed, cannot test the cookies"

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "simulate": True,
        "cookiefile": str(path),
        "socket_timeout": timeout,
        "retries": 0,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # yt-dlp raises broadly
        message = str(exc)
        if "sign in" in message.lower() or "bot" in message.lower():
            return False, (
                "YouTube still wants a sign-in: the cookies are stale, or they were exported "
                "from a browser window that was not logged in"
            )
        return False, f"YouTube rejected the request: {message[:300]}"
    title = (info or {}).get("title", "the video")
    return True, f"YouTube accepted the cookies (tested on: {title})"


def _print_report(report: CookieReport, source: str) -> None:
    status = "OK" if report.ok else "PROBLEM"
    print(f"{status} · {report.total} cookie(s) from {source}")
    if report.youtube:
        print(f"  youtube/google cookies : {report.youtube}")
    if report.logins:
        print(f"  login cookies          : {', '.join(report.logins)}")
    if report.domains:
        print(f"  domains                : {', '.join(report.domains)}")
    for note in report.notes:
        print(f"  note: {note}")
    for problem in report.problems:
        print(f"  problem: {problem}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m clipper.cookies",
        description="Check / normalise / test a YouTube cookies.txt for Clipper",
    )
    parser.add_argument("file", nargs="?", help="path to cookies.txt")
    parser.add_argument("--check", metavar="FILE", help="same as giving the file as an argument")
    parser.add_argument("--from-secret", action="store_true", help="read $YT_COOKIES (raw or base64)")
    parser.add_argument("--out", help="write a normalised cookies.txt here")
    parser.add_argument("--test", action="store_true", help="ask YouTube whether the cookies work")
    parser.add_argument("--test-url", default=TEST_URL, help="video used for the check")
    parser.add_argument("--strict", action="store_true", help="exit non-zero when the file is bad")
    args = parser.parse_args(argv)

    if args.check and not args.file:
        args.file = args.check

    source = "the file"
    if args.from_secret:
        raw = read_secret()
        source = "$YT_COOKIES"
        if not raw:
            print("No YT_COOKIES / CLIP_COOKIES_SECRET in the environment.")
            return 0
        text = decode_secret(raw)
    elif args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"error: {path} does not exist")
            return 2
        text = decode_secret(path.read_text(encoding="utf-8", errors="replace"))
        source = str(path)
    else:
        parser.error("give a cookies.txt path or --from-secret")

    report = analyse(parse_cookies(text))
    _print_report(report, source)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(normalise(text), encoding="utf-8")
        try:
            out.chmod(0o600)
        except OSError:
            pass
        print(f"  wrote normalised cookies → {out}")

    if args.test and report.total:
        target = Path(args.out) if args.out else Path(args.file or "cookies.txt")
        if not target.exists():
            target = Path("cookies.txt")
            target.write_text(normalise(text), encoding="utf-8")
        ok, message = test_against_youtube(target, args.test_url)
        print(f"  youtube test: {'PASS' if ok else 'FAIL'} — {message}")
        report.ok = report.ok and ok

    if not report.ok and args.strict:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
