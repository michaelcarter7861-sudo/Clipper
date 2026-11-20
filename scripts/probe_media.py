"""Which way of fetching a Piped instance's media URLs actually returns bytes?

Piped hands back googlevideo URLs that were signed for *the instance's* IP. Asking for them
from a runner gives 401, so the only way through is the instance's own proxy — and the proxy
URL shape differs between Piped versions. This script tries the shapes we know and prints
the HTTP code for a 2 KB range request on each.

    python scripts/probe_media.py https://youtu.be/DENbjnmSems
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.parse

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
API = "https://pipedapi.ducks.party"


def video_id(url: str) -> str:
    if "youtu.be/" in url:
        return url.split("youtu.be/")[1].split("?")[0].strip("/")
    match = re.search(r"[?&]v=([\w-]{6,})", url)
    return match.group(1) if match else url.strip()


def curl_json(url: str) -> dict:
    out = subprocess.run(
        ["curl", "-sS", "-m", "20", "-A", UA, "-H", "Accept: application/json", url],
        capture_output=True,
        text=True,
    ).stdout
    try:
        return json.loads(out)
    except ValueError:
        print(f"  (not json: {out[:120]})")
        return {}


def curl_code(url: str, extra: list[str] | None = None, *, agent: bool = True, rng: bool = True) -> str:
    command = ["curl", "-sS", "-o", "/tmp/probe-media.bin", "-w", "%{http_code}", "-m", "25"]
    if agent:
        command += ["-A", UA]
    if rng:
        command += ["-r", "0-2047"]
    command += [*(extra or []), url]
    result = subprocess.run(command, capture_output=True, text=True)
    size = 0
    try:
        with open("/tmp/probe-media.bin", "rb") as handle:
            size = len(handle.read())
    except OSError:
        pass
    return f"{result.stdout.strip() or 'ERR'} ({size}b)"


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://youtu.be/DENbjnmSems"
    vid = video_id(url)
    api = sys.argv[2] if len(sys.argv) > 2 else API
    print(f"== media probe · {vid} · {api} ==")

    payload = curl_json(f"{api}/streams/{vid}")
    if not payload:
        print("  instance gave no JSON")
        return 0
    proxy = str(payload.get("proxyUrl") or "")
    print(f"  title: {str(payload.get('title'))[:50]!r} proxyUrl: {proxy or '(none)'}")

    streams: list[tuple[str, str]] = []
    for key in ("videoStreams", "audioStreams"):
        for stream in payload.get(key) or []:
            if isinstance(stream, dict) and stream.get("url"):
                label = str(stream.get("quality") or stream.get("bitrate") or key)
                streams.append((label, str(stream["url"])))
    if not streams:
        print("  no streams listed")
        return 0

    # Some instances already hand back proxied URLs. Those must be used unchanged —
    # wrapping them again yields 404 — so test the header combinations on them first.
    proxied = [(label, media) for label, media in streams if proxy and proxy.split("//")[-1].split("/")[0] in media]
    for label, media in proxied[:2]:
        print(f"\n  -- already-proxied stream {label} — header variants --")
        for name, extra, agent, rng in [
            ("plain", None, False, False),
            ("ua-only", None, True, False),
            ("ua+range", None, True, True),
            ("ua+range+referer", ["-H", "Referer: https://www.youtube.com/"], True, True),
        ]:
            print(f"     {name:<18} {curl_code(media, extra, agent=agent, rng=rng)}")

    host_of_api = urllib.parse.urlsplit(api).netloc
    domain = ".".join(host_of_api.split(".")[-2:])  # ducks.party
    for label, media in streams[:3]:
        parts = urllib.parse.urlsplit(media)
        tail = parts.path + (f"?{parts.query}" if parts.query else "")
        print(f"\n  -- stream {label} ({parts.netloc}) --")
        candidates: list[tuple[str, str]] = [
            ("direct", media),
            ("direct-no-headers", media),
            ("proxy-path", f"{proxy.rstrip('/')}/{parts.netloc}{tail}" if proxy else ""),
            ("proxy-encoded", f"{proxy.rstrip('/')}/{urllib.parse.quote(media, safe='')}" if proxy else ""),
            ("proxy-noscheme", f"{proxy.rstrip('/')}/{media.split('://', 1)[-1]}" if proxy else ""),
            ("pipedproxy-host", f"https://pipedproxy.{domain}/{parts.netloc}{tail}"),
        ]
        for name, candidate in candidates:
            if not candidate:
                continue
            if name == "direct-no-headers":
                print(f"     {name:<18} {curl_code(candidate, agent=False, rng=False)}")
            else:
                print(f"     {name:<18} {curl_code(candidate)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
