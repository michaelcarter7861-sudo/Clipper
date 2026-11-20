"""A real browser for the two things yt-dlp cannot do by itself.

Clipper asks YouTube through ``yt-dlp`` first. When that comes back with
"Sign in to confirm you're not a bot" or storyboards only, this module can ask a
**real browser** instead — `obscura <https://github.com/h4ckf0r0day/obscura>`_,
a headless browser engine in Rust (Apache-2.0) that speaks the Chrome DevTools
Protocol.

Two uses, both narrow and honest:

1. **Cookies.** Loading the watch page in the browser leaves a fresh visitor
   session in its jar (``--dump cookies`` returns HttpOnly cookies too). Those
   cookies go into a Netscape ``cookies.txt`` for yt-dlp, so nobody has to
   export cookies from a phone by hand.
2. **The page itself.** If the player response says ``OK``, its stream URLs can
   be downloaded directly and muxed with ffmpeg — no yt-dlp parsing involved.

What this does **not** do: it does not change the runner's IP. A datacenter IP
that YouTube refuses will still be refused; ``player_response`` will say so and
that answer is reported, not hidden. ``--stealth`` (fingerprint spoofing) is off
by default and never enabled implicitly.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .log import get_logger

log = get_logger()

#: Environment fallback for the binary, so a workflow can set it once.
BINARY_ENV = "CLIP_BROWSER"
DEFAULT_BINARY = "obscura"
PLAYABILITY_KEYS = ("playabilityStatus", "status", "reason")


@dataclass
class BrowserPage:
    """What the browser saw when it opened a watch page."""

    url: str
    ok: bool = False
    player_response: dict[str, Any] | None = None
    cookies: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    elapsed: float = 0.0
    binary: str = ""

    # -- player response helpers ------------------------------------------- #
    @property
    def status(self) -> str:
        return playability(self.player_response)[0]

    @property
    def reason(self) -> str:
        return playability(self.player_response)[1]

    @property
    def downloadable(self) -> bool:
        if not self.player_response:
            return False
        data = streams(self.player_response)
        return self.status in ("", "OK") and bool(data["formats"] or data["adaptive"])

    def describe(self) -> str:
        if not self.ok:
            return f"browser failed: {self.error or 'unknown error'}"
        if not self.player_response:
            return "browser loaded the page but found no player response"
        status = self.status or "OK"
        data = streams(self.player_response)
        count = len(data["formats"]) + len(data["adaptive"])
        extra = f" — {self.reason}" if self.reason else ""
        return f"browser says playability={status}, {count} stream(s){extra}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "ok": self.ok,
            "status": self.status,
            "reason": self.reason,
            "streams": (
                sum(len(v) for v in streams(self.player_response).values())
                if self.player_response
                else 0
            ),
            "cookies": len(self.cookies),
            "elapsed": round(self.elapsed, 1),
            "binary": self.binary,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# binary
# --------------------------------------------------------------------------- #
def browser_binary(settings: Settings) -> str:
    """Resolve the browser binary: settings, then ``CLIP_BROWSER``, then PATH."""
    wanted = (getattr(settings, "browser", "") or "").strip()
    if wanted:
        found = shutil.which(wanted)
        if found:
            return found
        return wanted if Path(wanted).exists() else ""
    return shutil.which(DEFAULT_BINARY) or ""


def browser_available(settings: Settings) -> tuple[bool, str]:
    """``(usable, why)`` — the workflow logs this before a run."""
    if not (getattr(settings, "browser", "") or "").strip() and not shutil.which(DEFAULT_BINARY):
        return False, "obscura is not installed (set CLIP_BROWSER to the binary)"
    binary = browser_binary(settings)
    if not binary:
        return False, f"browser binary not found: {settings.browser}"
    try:
        completed = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except Exception as exc:  # noqa: BLE001 - a broken binary must not break a run
        return False, f"could not run {binary}: {exc}"
    version = (completed.stdout or completed.stderr or "").strip().splitlines()
    return True, f"{Path(binary).name} {version[0] if version else ''}".strip()


def _command(settings: Settings, *args: str) -> list[str]:
    binary = browser_binary(settings)
    if not binary:
        raise RuntimeError("browser binary not found")
    command = [binary]
    if getattr(settings, "browser_stealth", False):
        command.append("--stealth")
    proxy = (getattr(settings, "proxy", "") or "").strip()
    if proxy:
        command += ["--proxy", proxy]
    command += ["--quiet"]
    return command + list(args)


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


# --------------------------------------------------------------------------- #
# player response
# --------------------------------------------------------------------------- #
def playability(player_response: dict[str, Any] | None) -> tuple[str, str]:
    """``(status, reason)`` from a player response; ``("", "")`` when unknown."""
    if not player_response:
        return "", ""
    status = player_response.get("playabilityStatus") or {}
    if not isinstance(status, dict):
        return "", ""
    return str(status.get("status") or ""), str(status.get("reason") or "")


def streams(player_response: dict[str, Any] | None) -> dict[str, list[dict]]:
    """``{"formats": [...], "adaptive": [...]}`` with a usable ``url`` on each."""
    streaming = (player_response or {}).get("streamingData") or {}
    if not isinstance(streaming, dict):
        return {"formats": [], "adaptive": []}
    return {
        "formats": [f for f in streaming.get("formats") or [] if f.get("url")],
        "adaptive": [f for f in streaming.get("adaptiveFormats") or [] if f.get("url")],
    }


def _height(fmt: dict[str, Any]) -> int:
    return int(fmt.get("height") or 0)


def _bitrate(fmt: dict[str, Any]) -> float:
    return float(fmt.get("bitrate") or fmt.get("averageBitrate") or 0)


def pick_streams(
    player_response: dict[str, Any] | None, max_height: int = 1080
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Best video and best audio URL a direct download can use.

    Progressive formats (video+audio in one file) win: one download, no mux. If
    YouTube only offers separate tracks, return the pair and let ffmpeg join them.
    """
    data = streams(player_response)
    progressive = [
        fmt
        for fmt in data["formats"]
        if fmt.get("url") and str(fmt.get("mimeType") or "").startswith("video")
    ]
    progressive = [fmt for fmt in progressive if 0 < _height(fmt) <= max_height] or progressive
    if progressive:
        return max(progressive, key=_height), None

    video = [
        fmt
        for fmt in data["adaptive"]
        if str(fmt.get("mimeType") or "").startswith("video")
        and (fmt.get("audioQuality") in (None, ""))
        and 0 < _height(fmt) <= max_height
    ]
    audio = [
        fmt
        for fmt in data["adaptive"]
        if str(fmt.get("mimeType") or "").startswith("audio")
    ]
    best_video = max(video, key=lambda f: (_height(f), _bitrate(f))) if video else None
    best_audio = max(audio, key=_bitrate) if audio else None
    return best_video, best_audio


def video_details(player_response: dict[str, Any] | None) -> dict[str, Any]:
    details = (player_response or {}).get("videoDetails") or {}
    return details if isinstance(details, dict) else {}


# --------------------------------------------------------------------------- #
# the browser itself
# --------------------------------------------------------------------------- #
def _parse_json_output(text: str) -> Any:
    """``--eval`` prints the value as JSON; a stringified object needs two passes."""
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def fetch_page(
    url: str,
    settings: Settings,
    *,
    storage_dir: Path | None = None,
    want_cookies: bool = True,
    timeout: int | None = None,
) -> BrowserPage:
    """Open ``url`` in the browser and report what YouTube answered."""
    started = time.time()
    binary = browser_binary(settings)
    page = BrowserPage(url=url, binary=binary)
    if not binary:
        page.error = "browser binary not found"
        return page
    limit = timeout or int(getattr(settings, "browser_timeout", 90) or 90)
    storage = Path(storage_dir) if storage_dir else None
    try:
        command = _command(
            settings,
            "fetch",
            url,
            "--wait-until",
            "domcontentloaded",
            "--timeout",
            str(limit),
            "--eval",
            "JSON.stringify(window.ytInitialPlayerResponse || null)",
        )
        if storage is not None:
            storage.mkdir(parents=True, exist_ok=True)
            command += ["--storage-dir", str(storage)]
        completed = _run(command, timeout=limit + 60)
        page.player_response = _parse_json_output(completed.stdout)
        if completed.returncode != 0 and not page.player_response:
            page.error = _one_line(completed.stderr or completed.stdout) or (
                f"exit {completed.returncode}"
            )
        else:
            page.ok = True
        if want_cookies and storage is not None:
            page.cookies = harvest_cookies(url, settings, storage_dir=storage, timeout=limit)
    except subprocess.TimeoutExpired:
        page.error = f"browser timed out after {limit}s"
    except Exception as exc:  # noqa: BLE001 - never let the browser break a run
        page.error = str(exc)
    page.elapsed = time.time() - started
    return page


def harvest_cookies(
    url: str,
    settings: Settings,
    *,
    storage_dir: Path | None = None,
    timeout: int | None = None,
) -> list[dict[str, Any]]:
    """Cookies the browser holds after visiting ``url`` (HttpOnly included)."""
    binary = browser_binary(settings)
    if not binary:
        return []
    limit = timeout or int(getattr(settings, "browser_timeout", 90) or 90)
    command = _command(settings, "fetch", url, "--dump", "cookies", "--timeout", str(limit))
    storage = Path(storage_dir) if storage_dir else None
    if storage is not None:
        storage.mkdir(parents=True, exist_ok=True)
        command += ["--storage-dir", str(storage)]
    try:
        completed = _run(command, timeout=limit + 60)
    except Exception as exc:  # noqa: BLE001
        log.debug("cookie harvest failed: %s", exc)
        return []
    payload = _parse_json_output(completed.stdout)
    if isinstance(payload, list):
        return [cookie for cookie in payload if isinstance(cookie, dict)]
    if isinstance(payload, dict):
        # the on-disk jar is a plain array, but accept a wrapped shape too
        for key in ("cookies", "items"):
            if isinstance(payload.get(key), list):
                return [cookie for cookie in payload[key] if isinstance(cookie, dict)]
    return []


def netscape_cookies(cookies: list[dict[str, Any]]) -> str:
    """Convert obscura's cookie jar to the cookies.txt format yt-dlp reads."""
    lines = [
        "# Netscape HTTP Cookie File",
        "# Written by Clipper from an obscura browser session",
        "",
    ]
    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "").strip()
        if not name or not value:
            continue
        domain = str(cookie.get("domain") or "youtube.com").strip() or "youtube.com"
        if not domain.startswith("."):
            domain = "." + domain.lstrip(".")
        include_subdomains = "TRUE"
        path = str(cookie.get("path") or "/").strip() or "/"
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        expires = cookie.get("expires") or cookie.get("expiry") or 0
        try:
            expires = int(float(expires))
        except (TypeError, ValueError):
            expires = 0
        if expires < 0:
            expires = 0
        lines.append(
            "\t".join([domain, include_subdomains, path, secure, str(expires), name, value])
        )
    return "\n".join(lines) + "\n"


def write_cookies(cookies: list[dict[str, Any]], path: Path) -> Path | None:
    """Write a cookies.txt for yt-dlp. ``None`` when there is nothing to write."""
    if not cookies:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(netscape_cookies(cookies), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - best effort on odd filesystems
        pass
    return path


# --------------------------------------------------------------------------- #
# direct download
# --------------------------------------------------------------------------- #
def http_download(
    url: str,
    dest: Path,
    *,
    timeout: int = 600,
    chunk: int = 1 << 20,
    user_agent: str = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
) -> Path:
    """Download one stream URL (a googlevideo link) straight to ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=timeout) as response, dest.open("wb") as handle:
        total = 0
        while True:
            block = response.read(chunk)
            if not block:
                break
            handle.write(block)
            total += len(block)
    if total == 0:
        raise RuntimeError(f"empty download from {url.split('?')[0]}")
    return dest


def _one_line(text: str, limit: int = 200) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    return lines[-1][:limit]
