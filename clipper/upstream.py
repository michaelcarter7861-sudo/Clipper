"""Last resort without a proxy: borrow a public Piped/Invidious instance.

When YouTube answers this machine's IP with "Sign in to confirm you're not a bot", every
client of *this* machine fails — yt-dlp's android/ios/tv/mweb/web players and the obscura
browser alike. What still works is somebody else's machine asking: a public Piped or
Invidious instance keeps its own player session, and hands back plain media URLs (or a
proxied URL through itself) that need no bot check.

This is not a bypass and it is not a guarantee:

- nothing decrypts or circumvents anything; the instance just did the normal request,
- instances are volunteer-run, they rate-limit, they die, and a few are blocked too —
  Clipper tries a short list and gives up quietly with a clear message,
- for the user's own account/subscription content, cookies remain the right answer.

Only a link that is already public works here, and the clips are made locally as usual.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .log import get_logger

log = get_logger()

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

#: ``kind`` prefix tells the parser which API shape to expect.
DEFAULT_INSTANCES: tuple[str, ...] = (
    "invidious:https://inv.nadeko.net",
    "piped:https://pipedapi.kavin.rocks",
    "invidious:https://yewtu.be",
    "piped:https://pipedapi.adminforge.de",
    "invidious:https://invidious.nerdvpn.de",
    "piped:https://api.piped.private.coffee",
    "invidious:https://invidious.privacyredirect.com",
    "invidious:https://iv.melmac.space",
)

TIMEOUT = 25


class UpstreamError(RuntimeError):
    pass


@dataclass
class Stream:
    url: str
    mime: str = ""
    height: int = 0
    has_audio: bool = True

    @property
    def is_video(self) -> bool:
        return self.mime.startswith("video/") or self.height > 0

    @property
    def is_audio(self) -> bool:
        return self.mime.startswith("audio/")

    def describe(self) -> str:
        kind = "audio" if self.is_audio and not self.height else f"{self.height or '?'}p"
        return f"{kind} {self.mime or 'unknown'}"


@dataclass
class UpstreamVideo:
    instance: str
    video_id: str
    title: str = ""
    duration: float = 0.0
    uploader: str = ""
    streams: list[Stream] = field(default_factory=list)
    captions: list[tuple[str, str]] = field(default_factory=list)

    def muxed(self, max_height: int = 0) -> list[Stream]:
        streams = [s for s in self.streams if s.has_audio and s.height and not s.is_audio]
        streams.sort(key=lambda s: s.height, reverse=True)
        if max_height:
            capped = [s for s in streams if s.height <= max_height]
            streams = capped or streams[-1:]
        return streams

    def video_only(self, max_height: int = 0) -> Stream | None:
        streams = [s for s in self.streams if not s.has_audio and s.height and not s.is_audio]
        streams.sort(key=lambda s: s.height, reverse=True)
        if max_height:
            capped = [s for s in streams if s.height <= max_height]
            streams = capped or streams[-1:]
        return streams[0] if streams else None

    def audio_only(self) -> Stream | None:
        streams = [s for s in self.streams if s.is_audio]
        if not streams:
            return None
        return streams[0]


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #
def _opener(settings: Settings) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = []
    if settings.proxy:
        handlers.append(urllib.request.ProxyHandler({"http": settings.proxy, "https": settings.proxy}))
    return urllib.request.build_opener(*handlers)


def _http_json(url: str, settings: Settings, timeout: int = TIMEOUT) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with _opener(settings).open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def fetch_stream(url: str, dest: Path, settings: Settings, timeout: int = 60) -> Path:
    """Download one media URL to ``dest`` (no yt-dlp, no player logic)."""
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _opener(settings).open(request, timeout=timeout) as response, dest.open("wb") as handle:
        shutil.copyfileobj(response, handle, 1 << 20)
    if not dest.exists() or dest.stat().st_size < 1024:
        raise UpstreamError(f"the instance returned {dest.stat().st_size if dest.exists() else 0} bytes")
    return dest


# --------------------------------------------------------------------------- #
# API shapes
# --------------------------------------------------------------------------- #
def _height(text: str) -> int:
    match = re.search(r"(\d{3,4})", str(text or ""))
    return int(match.group(1)) if match else 0


def _proxied(proxy_base: str, url: str) -> str:
    """Turn a googlevideo URL into the instance's proxy URL for it.

    Some instances already hand back proxied URLs (their host *is* the proxy host).
    Wrapping those again points at a path that does not exist, so leave them alone.
    """
    if not proxy_base:
        return url
    parts = urllib.parse.urlsplit(url)
    proxy_host = urllib.parse.urlsplit(proxy_base).netloc
    if proxy_host and parts.netloc == proxy_host:
        return url
    tail = parts.path + (f"?{parts.query}" if parts.query else "")
    return f"{proxy_base.rstrip('/')}/{parts.netloc}{tail}"


def parse_piped(payload: object, base: str, video_id: str) -> UpstreamVideo:
    data = payload if isinstance(payload, dict) else {}
    proxy_base = str(data.get("proxyUrl") or "")
    streams: list[Stream] = []
    for raw in data.get("videoStreams") or []:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "")
        if not url:
            continue
        mime = str(raw.get("mimeType") or "")
        height = int(raw.get("height") or 0) or _height(raw.get("quality"))
        streams.append(
            Stream(
                url=_proxied(proxy_base, url),
                mime=mime,
                height=height,
                has_audio=not bool(raw.get("videoOnly")),
            )
        )
    for raw in data.get("audioStreams") or []:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "")
        if url:
            streams.append(
                Stream(url=_proxied(proxy_base, url), mime=str(raw.get("mimeType") or "audio/mp4"), height=0)
            )
    return UpstreamVideo(
        instance=base,
        video_id=video_id,
        title=str(data.get("title") or "").strip(),
        duration=float(data.get("duration") or 0.0),
        uploader=str(data.get("uploader") or ""),
        streams=streams,
    )


def parse_invidious(payload: object, base: str, video_id: str) -> UpstreamVideo:
    data = payload if isinstance(payload, dict) else {}
    streams: list[Stream] = []

    def full(url: str) -> str:
        url = str(url or "")
        if url.startswith("/"):
            return base.rstrip("/") + url
        return url

    for raw in data.get("formatStreams") or []:
        if not isinstance(raw, dict):
            continue
        url = full(raw.get("url", ""))
        if url:
            streams.append(
                Stream(
                    url=url,
                    mime=str(raw.get("type") or "video/mp4").split(";")[0].strip(),
                    height=_height(raw.get("resolution") or raw.get("qualityLabel")),
                    has_audio=True,
                )
            )
    for raw in data.get("adaptiveFormats") or []:
        if not isinstance(raw, dict):
            continue
        url = full(raw.get("url", ""))
        if not url:
            continue
        mime = str(raw.get("type") or "").split(";")[0].strip()
        if mime.startswith("audio/"):
            streams.append(Stream(url=url, mime=mime, height=0))
        elif mime.startswith("video/"):
            streams.append(
                Stream(
                    url=url,
                    mime=mime,
                    height=_height(raw.get("resolution") or raw.get("qualityLabel")),
                    has_audio=False,
                )
            )
    captions: list[tuple[str, str]] = []
    for raw in data.get("captions") or []:
        if isinstance(raw, dict) and raw.get("url"):
            captions.append((str(raw.get("label") or "unknown"), full(raw["url"])))

    return UpstreamVideo(
        instance=base,
        video_id=video_id,
        title=str(data.get("title") or "").strip(),
        duration=float(data.get("lengthSeconds") or 0.0),
        uploader=str(data.get("author") or ""),
        streams=streams,
        captions=captions,
    )


def instances(settings: Settings | None = None) -> list[tuple[str, str]]:
    """``[(kind, base_url), …]`` — overridable with ``CLIP_UPSTREAM_INSTANCES``."""
    raw = ""
    if settings is not None:
        raw = str(getattr(settings, "upstream_instances", "") or "")
    configured: list[tuple[str, str]] = []
    for chunk in raw.replace("\n", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk.startswith(("http://", "https://")):
            kind, base = "invidious", chunk
        else:
            kind, _, base = chunk.partition(":")
            if not base:
                kind, base = "invidious", kind
        configured.append((kind.strip().lower(), base.strip()))
    return configured or [(item.split(":", 1)[0], item.split(":", 1)[1]) for item in DEFAULT_INSTANCES]


def resolve(video_id: str, settings: Settings, *, timeout: int = TIMEOUT) -> UpstreamVideo | None:
    """Ask the first instance that answers for this video's streams."""
    if not video_id:
        return None
    for kind, base in instances(settings):
        try:
            if kind == "piped":
                payload = _http_json(f"{base.rstrip('/')}/streams/{video_id}", settings, timeout)
                video = parse_piped(payload, base, video_id)
            else:
                payload = _http_json(
                    f"{base.rstrip('/')}/api/v1/videos/{video_id}?fields=title,lengthSeconds,author,formatStreams,adaptiveFormats,captions",
                    settings,
                    timeout,
                )
                video = parse_invidious(payload, base, video_id)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            log.info("upstream %s did not answer (%s)", base, str(exc)[:120])
            continue
        except Exception as exc:  # noqa: BLE001 - an instance must never break a run
            log.info("upstream %s failed (%s)", base, str(exc)[:120])
            continue
        if video.streams:
            log.info(
                "upstream: %s answered for %s (%d stream(s), %s)",
                base,
                video_id,
                len(video.streams),
                "captions" if video.captions else "no captions",
            )
            return video
        log.info("upstream %s listed no stream for %s", base, video_id)
    return None


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #
def _mux(video: Path, audio: Path, target: Path, settings: Settings) -> Path:
    command = [
        settings.ffmpeg or "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(target),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise UpstreamError(f"ffmpeg could not mux the streams: {result.stderr.strip()[:300]}")
    return target


def download(
    video: UpstreamVideo, work_dir: Path, settings: Settings, *, max_height: int = 0
) -> tuple[Path, str]:
    """Download the best muxed stream, or video+audio and mux them. Returns (path, note)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    stem = work_dir / "upstream"

    for stream in video.muxed(max_height):
        try:
            target = fetch_stream(stream.url, stem.with_suffix(".mp4"), settings)
            log.info("upstream: downloaded a muxed %s stream", stream.describe())
            return target, f"muxed {stream.describe()}"
        except Exception as exc:  # noqa: BLE001 - try the next quality
            log.info("upstream muxed %s failed (%s)", stream.describe(), str(exc)[:120])

    video_stream = video.video_only(max_height)
    audio_stream = video.audio_only()
    if video_stream is not None and audio_stream is not None:
        video_path = fetch_stream(video_stream.url, work_dir / "upstream-video.mp4", settings)
        audio_path = fetch_stream(audio_stream.url, work_dir / "upstream-audio.m4a", settings)
        merged = _mux(video_path, audio_path, work_dir / "upstream-full.mp4", settings)
        log.info("upstream: downloaded %s + audio and muxed them", video_stream.describe())
        return merged, f"{video_stream.describe()} + audio"
    if video_stream is not None:
        path = fetch_stream(video_stream.url, work_dir / "upstream.mp4", settings)
        log.warning("upstream: this video came without audio")
        return path, f"{video_stream.describe()} (no audio)"
    raise UpstreamError("the instance listed no usable stream")


def fetch_captions(video: UpstreamVideo, work_dir: Path, settings: Settings) -> Path | None:
    """Grab the first caption track as a VTT file, if the instance offers one."""
    if not video.captions:
        return None
    label, url = video.captions[0]
    for candidate in (url, url + ("&fmt=vtt" if "?" in url else "?fmt=vtt")):
        try:
            path = fetch_stream(candidate, work_dir / "upstream-captions.vtt", settings)
            log.info("upstream: captions %r downloaded", label)
            return path
        except Exception as exc:  # noqa: BLE001
            log.debug("caption %r failed: %s", label, str(exc)[:120])
    return None


__all__ = [
    "DEFAULT_INSTANCES",
    "Stream",
    "UpstreamError",
    "UpstreamVideo",
    "download",
    "fetch_captions",
    "fetch_stream",
    "instances",
    "parse_invidious",
    "parse_piped",
    "resolve",
]
