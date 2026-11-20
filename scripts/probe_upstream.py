"""Which public instances can still serve a YouTube video from a CI machine?

Run on a GitHub runner (that is where the block exists), never locally:

    python scripts/probe_upstream.py https://youtu.be/DENbjnmSems

It asks every instance we know about for the same video, prints one line per instance, and
probes the first media URL with a 1 KB range request — an API that answers is worthless if
the media URL behind it is refused.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
TIMEOUT = 12

PIPED = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://api.piped.private.coffee",
    "https://pipedapi.reallyaweso.me",
    "https://pipedapi.drgns.space",
    "https://pipedapi.leptons.xyz",
    "https://piped-api.lunar.icu",
    "https://api.piped.projectsegfau.lt",
    "https://pipedapi.ducks.party",
    "https://pipedapi.nosebs.ru",
    "https://pipedapi.orangenet.cc",
    "https://pipedapi.astartes.nl",
]

INVIDIOUS = [
    "https://inv.nadeko.net",
    "https://yewtu.be",
    "https://invidious.nerdvpn.de",
    "https://invidious.f5.si",
    "https://invidious.jing.rocks",
    "https://iv.duti.dev",
    "https://invidious.reallyaweso.me",
    "https://invidious.perennialte.ch",
    "https://yt.artemislena.eu",
    "https://invidious.dhusch.de",
    "https://inv.tux.pizza",
    "https://inv.ggtyler.dev",
    "https://invidious.materialio.us",
    "https://invidious.privacyredirect.com",
]

COBALT = [
    "https://api.cobalt.tools",
    "https://co.eepy.today",
    "https://cobalt-api.kwiatekmiki.com",
    "https://cobalt.api.timelessnesses.me",
]


def video_id(url: str) -> str:
    if "youtu.be/" in url:
        return url.split("youtu.be/")[1].split("?")[0].strip("/")
    match = re.search(r"[?&]v=([\w-]{6,})", url)
    return match.group(1) if match else url.strip()


def get(url: str, headers: dict | None = None, data: bytes | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read(400_000)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(2000) if hasattr(exc, "read") else b""
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(str(exc)[:120]) from exc


def probe_media(url: str) -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": UA, "Range": "bytes=0-1023"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            size = len(response.read(2048))
            return f"range={response.status} bytes={size}"
    except urllib.error.HTTPError as exc:
        return f"range={exc.code}"
    except Exception as exc:  # noqa: BLE001
        return f"range=fail {str(exc)[:60]}"


def first_media(kind: str, payload: dict) -> str:
    if kind == "piped":
        for key in ("videoStreams", "audioStreams"):
            for stream in payload.get(key) or []:
                if isinstance(stream, dict) and stream.get("url"):
                    proxy = str(payload.get("proxyUrl") or "")
                    if proxy:
                        parts = urllib.parse.urlsplit(stream["url"])
                        tail = parts.path + (f"?{parts.query}" if parts.query else "")
                        return f"{proxy.rstrip('/')}/{parts.netloc}{tail}"
                    return str(stream["url"])
        return ""
    for key in ("formatStreams", "adaptiveFormats"):
        for stream in payload.get(key) or []:
            if isinstance(stream, dict) and stream.get("url"):
                return str(stream["url"])
    return ""


def check(kind: str, base: str, vid: str) -> str:
    try:
        if kind == "piped":
            status, body = get(f"{base}/streams/{vid}")
        elif kind == "invidious":
            status, body = get(
                f"{base}/api/v1/videos/{vid}?fields=title,lengthSeconds,formatStreams,adaptiveFormats,captions"
            )
        else:
            payload = json.dumps({"url": f"https://youtu.be/{vid}", "videoQuality": "720"}).encode()
            status, body = get(
                f"{base}/",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                data=payload,
            )
    except RuntimeError as exc:
        return f"fail  {base:<45} {exc}"
    if status != 200:
        return f"{status}   {base:<45} {body[:80].decode('utf-8', 'replace').replace(chr(10), ' ')}"

    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return f"json? {base:<45} {body[:80].decode('utf-8', 'replace')}"

    if kind == "cobalt":
        return f"OK    {base:<45} cobalt status={payload.get('status')} url={str(payload.get('url'))[:60]}"

    streams = len(payload.get("videoStreams") or []) + len(payload.get("audioStreams") or []) if kind == "piped" else (
        len(payload.get("formatStreams") or []) + len(payload.get("adaptiveFormats") or [])
    )
    if not streams:
        return f"empty {base:<45} streams=0"
    media = first_media(kind, payload)
    title = str(payload.get("title") or payload.get("lengthSeconds") or "")[:24]
    return f"OK    {base:<45} streams={streams:<3} media={probe_media(media) if media else 'none'} title={title}"


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://youtu.be/DENbjnmSems"
    vid = video_id(url)
    print(f"== upstream probe · {vid} · {url} ==", flush=True)

    groups: list[tuple[str, list[str]]] = [
        ("invidious", INVIDIOUS),
        ("piped", PIPED),
        ("cobalt", COBALT),
    ]
    working: list[str] = []
    for kind, bases in groups:
        print(f"\n-- {kind} --", flush=True)
        with ThreadPoolExecutor(max_workers=8) as pool:
            lines = list(pool.map(lambda base, k=kind: check(k, base, vid), bases))
        for base, line in zip(bases, lines, strict=True):
            print(line, flush=True)
            if line.startswith("OK"):
                working.append(f"{kind}:{base}")

    print("\n== worked ==")
    for item in working or ["(nothing)"]:
        print(item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
