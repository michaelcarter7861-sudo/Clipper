"""YouTube ingestion through yt-dlp."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .browser import (
    browser_available,
    fetch_page,
    http_download,
    pick_streams,
    video_details,
    write_cookies,
)
from .config import Settings
from .log import get_logger

log = get_logger()


class DownloadError(RuntimeError):
    pass


@dataclass
class VideoSource:
    url: str
    video_id: str
    title: str
    duration: float
    uploader: str = ""
    path: Path | None = None
    width: int = 0
    height: int = 0
    subtitle_files: list[Path] = field(default_factory=list)
    info: dict = field(default_factory=dict)
    formats: list[dict] = field(default_factory=list)
    chosen_format: str = ""
    live_status: str = ""
    local: bool = False  # a file the user pointed at: never delete it
    audio_only: bool = False  # analysis was done on audio; video comes per clip

    @property
    def slug(self) -> str:
        base = slugify(self.title) or "video"
        return f"{base[:60]}-{self.video_id}" if self.video_id else base[:60]

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "video_id": self.video_id,
            "title": self.title,
            "duration": self.duration,
            "uploader": self.uploader,
            "width": self.width,
            "height": self.height,
            "file": str(self.path) if self.path else None,
            "chosen_format": self.chosen_format,
            "audio_only": self.audio_only,
            "live_status": self.live_status,
            "local": self.local,
            "formats": self.formats,
        }


def slugify(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\u0600-\u06ff]+", "-", text)
    return text.strip("-")


def video_id_from_url(url: str) -> str:
    patterns = [
        r"(?:v=|/v/|youtu\.be/|/embed/|/shorts/|/live/)([A-Za-z0-9_-]{6,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return ""


FORMAT_CHAIN = "bv*+ba/b/best"
PROBE_FORMAT = "all"  # list every format, even when only images exist
PLAYER_CLIENTS = ("android", "tv", "web_safari", "ios", "mweb")
TEST_URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"  # Big Buck Bunny, public domain
FORMAT_ERROR_HINTS = (
    "requested format is not available",
    "no video formats found",
    "requested download is not available",
)
RETRYABLE_HINTS = (
    "the page needs to be reloaded",
    "player response",
    "nsig",
    "some formats may be missing",
)
FATAL_HINTS = (
    "sign in to confirm",
    "private video",
    "members-only",
    "video unavailable",
    "removed by the uploader",
    "this live event will begin",
)
LIVE_WARNING = (
    "live stream: yt-dlp fetches it from the beginning, so the download can be big and slow — "
    "clipping starts once the recording is on disk"
)


@dataclass
class FormatRow:
    """One row of ``yt-dlp -F``, so the run log shows what YouTube actually offered."""

    id: str
    ext: str
    height: int = 0
    fps: float | None = None
    vcodec: str = ""
    acodec: str = ""
    filesize: int | None = None
    tbr: float | None = None

    @property
    def res(self) -> str:
        return f"{self.height}p" if self.height else "audio"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ext": self.ext,
            "resolution": self.res,
            "fps": self.fps,
            "vcodec": self.vcodec,
            "acodec": self.acodec,
            "filesize": self.filesize,
            "tbr": self.tbr,
        }


class _YdlLogger:
    """Send yt-dlp chatter to our logger so failures keep their context."""

    def debug(self, msg: str) -> None:
        log.debug(msg)

    def info(self, msg: str) -> None:
        log.debug(msg)

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)


def _is_format_error(text: str) -> bool:
    lowered = (text or "").lower()
    return any(hint in lowered for hint in FORMAT_ERROR_HINTS)


def _is_retryable(text: str) -> bool:
    """Format problems *and* unusable player responses are worth retrying."""
    return _is_format_error(text) or any(hint in (text or "").lower() for hint in RETRYABLE_HINTS)


def _is_fatal(text: str) -> bool:
    lowered = (text or "").lower()
    return any(hint in lowered for hint in FATAL_HINTS)


def js_runtime_status() -> dict[str, str]:
    """Which JS runtimes yt-dlp can use for YouTube's signature challenges.

    Modern yt-dlp needs an external JS runtime; its default is Deno only, which is not
    installed on GitHub runners, so Node is added when available.
    """
    return {name: path for name in ("deno", "node") if (path := shutil.which(name))}


def _one_line(text: str, limit: int = 160) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return (lines[-1] if lines else "")[:limit]


def _format_sort(settings: Settings) -> list[str]:
    """Prefer the height ceiling, mp4/m4a and h264 — preferences, never filters."""
    return [f"res:{settings.max_video_height}", "ext:mp4:m4a", "codec:h264", "br"]


def _ydl_opts(
    settings: Settings,
    work_dir: Path,
    *,
    format_selector: str | None = None,
    live_from_start: bool = False,
    probe: bool = False,
    player_client: str | None = None,
    drop_cookies: bool = False,
    cookie_file: Path | None = None,
) -> dict:
    opts: dict = {
        "format": PROBE_FORMAT if probe else (format_selector or FORMAT_CHAIN),
        "format_sort": _format_sort(settings),
        "merge_output_format": "mp4",
        "outtmpl": str(work_dir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 40,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitlesformat": "vtt/best",
        "subtitleslangs": _subtitle_langs(settings),
        "concurrent_fragment_downloads": 4,
        "overwrites": False,
        "continuedl": True,
        "logger": _YdlLogger(),
    }
    if live_from_start:
        opts["live_from_start"] = True
    if probe:
        opts["skip_download"] = True
    cookies = cookie_file or settings.cookies
    if cookies and Path(cookies).exists() and not drop_cookies:
        opts["cookiefile"] = str(cookies)
    if settings.proxy:
        opts["proxy"] = settings.proxy
    if settings.ytdlp_extra_args:
        opts.update(_parse_extra_args(settings.ytdlp_extra_args))
    runtimes: dict = {"deno": {"path": None}}
    if shutil.which("node"):
        runtimes["node"] = {"path": None}
    opts["js_runtimes"] = runtimes
    if player_client:
        _set_player_client(opts, player_client)
    return opts


def _subtitle_langs(settings: Settings) -> list[str]:
    langs: list[str] = []
    if settings.language and settings.language.lower() not in {"auto", "none"}:
        langs.extend([settings.language, f"{settings.language}.*"])
    langs.extend(["en.*", "hi.*", "ur.*"])
    seen: list[str] = []
    for lang in langs:
        if lang not in seen:
            seen.append(lang)
    return seen


def _parse_extra_args(extra: list[str]) -> dict:
    """Turn raw yt-dlp flags (``--proxy``, ``--cookies-from-browser`` …) into options.

    yt-dlp's parser returns *every* default next to the flags we asked for, so only the
    values that differ from an empty command line are kept. Otherwise those defaults
    would wipe our own outtmpl/format/subtitle settings.
    """
    try:
        from yt_dlp import parse_options
    except Exception:  # pragma: no cover - yt-dlp always present in practice
        return {}
    try:
        baseline = dict(parse_options([]).ydl_opts)
        parsed = dict(parse_options(extra).ydl_opts)
    except Exception as exc:  # pragma: no cover
        log.warning("could not parse YTDLP_EXTRA_ARGS (%s): %s", " ".join(extra), exc)
        return {}
    changed: dict = {}
    for key, value in parsed.items():
        if key in {"urls", "verbose", "quiet", "simulate", "forcejson"}:
            continue
        if key not in baseline or baseline[key] != value:
            changed[key] = value
    return changed


def _format_rows(info: dict | None) -> list[FormatRow]:
    rows: list[FormatRow] = []
    for raw in (info or {}).get("formats") or []:
        ext = str(raw.get("ext") or "")
        vcodec = str(raw.get("vcodec") or "")
        acodec = str(raw.get("acodec") or "")
        if ext in {"mhtml", "none"} or (vcodec == "none" and acodec == "none"):
            continue
        if "storyboard" in str(raw.get("format_note") or "").lower():
            continue
        rows.append(
            FormatRow(
                id=str(raw.get("format_id") or ""),
                ext=ext,
                height=int(raw.get("height") or 0),
                fps=raw.get("fps"),
                vcodec=vcodec,
                acodec=acodec,
                filesize=raw.get("filesize") or raw.get("filesize_approx"),
                tbr=raw.get("tbr"),
            )
        )
    rows.sort(key=lambda row: (row.height, row.tbr or 0.0), reverse=True)
    return rows


def format_table(rows: list[FormatRow], limit: int = 15) -> list[str]:
    """Human-readable format listing (log, report and ``--list-formats``)."""
    if not rows:
        return ["  (YouTube listed no downloadable format)"]
    lines = [f"  {'id':<8}{'ext':<5}{'res':<9}{'fps':<5}{'vcodec':<9}{'acodec':<9}size"]
    for row in rows[:limit]:
        size = f"{row.filesize / 1048576:.1f}M" if row.filesize else "-"
        fps = f"{row.fps:g}" if row.fps else "-"
        vcodec = row.vcodec if row.vcodec not in {"", "none"} else "-"
        acodec = row.acodec if row.acodec not in {"", "none"} else "-"
        lines.append(f"  {row.id:<8}{row.ext:<5}{row.res:<9}{fps:<5}{vcodec:<9}{acodec:<9}{size}")
    if len(rows) > limit:
        lines.append(f"  … {len(rows) - limit} more format(s)")
    return lines


def _live_state(info: dict | None) -> str:
    if not info:
        return ""
    return str(info.get("live_status") or ("is_live" if info.get("is_live") else "not_live"))


@dataclass(frozen=True)
class FormatAttempt:
    """One download try: which selector, which player client, cookies or not."""

    label: str
    selector: str
    player_client: str | None = None
    drop_cookies: bool = False


def _attempts(live: bool, *, has_cookies: bool = False) -> list[FormatAttempt]:
    """Escalating tries: default client, then each player client, then cookie-free.

    YouTube sometimes answers with storyboard images only for one client/IP and a real
    stream for another, so the ladder keeps asking in a different way instead of dying.
    """
    primary = "b" if live else "bv*+ba/b"
    attempts = [FormatAttempt("default client", primary)]
    attempts.extend(FormatAttempt(f"client {client}", primary, player_client=client) for client in PLAYER_CLIENTS)
    attempts.append(FormatAttempt("best", "best"))
    attempts.append(FormatAttempt("best single file", "b"))
    if has_cookies:
        attempts.append(
            FormatAttempt("no cookies + android", primary, player_client="android", drop_cookies=True)
        )
    return attempts


def _set_player_client(opts: dict, client: str) -> None:
    """Pin one YouTube player client without dropping other extractor args."""
    args = dict(opts.get("extractor_args") or {})
    youtube = dict(args.get("youtube") or {})
    youtube["player_client"] = [client]
    args["youtube"] = youtube
    opts["extractor_args"] = args


def local_media_path(url: str) -> Path | None:
    """Return a path when ``url`` is really a file on disk (or a ``file://`` link)."""
    candidate = url[7:] if url.startswith("file://") else url
    if not candidate or "://" in candidate:
        return None
    path = Path(candidate).expanduser()
    return path if path.is_file() else None


def _ffprobe_metadata(path: Path, settings: Settings) -> tuple[float, int, int]:
    """duration, width, height — best effort, never fatal."""
    probe = shutil.which(settings.ffprobe) or (settings.ffprobe if Path(settings.ffprobe).is_file() else None)
    if not probe:
        return 0.0, 0, 0
    try:
        result = subprocess.run(
            [
                probe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        payload = json.loads(result.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0.0, 0, 0
    duration = 0.0
    try:
        duration = float((payload.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    streams = payload.get("streams") or [{}]
    width = int(streams[0].get("width") or 0)
    height = int(streams[0].get("height") or 0)
    return duration, width, height


def local_source(url: str, path: Path, settings: Settings) -> VideoSource:
    """Use a video that is already on disk — no YouTube involved at all."""
    duration, width, height = _ffprobe_metadata(path, settings)
    subtitles = sorted(
        sibling
        for sibling in path.parent.glob(f"{path.stem}.*")
        if sibling.suffix.lower() in {".vtt", ".srt"}
    )
    log.info("using the local file %s (%.1fs)", path.name, duration)
    return VideoSource(
        url=url,
        video_id=slugify(path.stem),
        title=path.stem,
        duration=duration,
        path=path,
        width=width,
        height=height,
        subtitle_files=subtitles,
        local=True,
        info={"id": slugify(path.stem), "title": path.stem, "duration": duration},
    )


def youtube_reachable(settings: Settings, url: str = TEST_URL) -> tuple[bool, str]:
    """Can this machine download from YouTube at all right now?

    Probes a public-domain video: if even that only yields storyboards, the block is on
    the machine's IP, not on the link the user pasted.
    """
    import tempfile

    try:
        with tempfile.TemporaryDirectory(prefix="clipper-check-") as tmp:
            info = probe_formats(url, Path(tmp), settings)
    except DownloadError as exc:
        return False, str(exc)
    rows = _format_rows(info)
    if rows:
        return True, f"{len(rows)} downloadable format(s) for a public test video"
    listed = len((info or {}).get("formats") or [])
    return False, f"the test video listed {listed} format(s) but none downloadable"


def probe_formats(url: str, work_dir: Path, settings: Settings) -> dict | None:
    """Ask yt-dlp what YouTube offers for ``url`` without downloading anything."""
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover - yt-dlp always present in practice
        raise DownloadError("yt-dlp is not installed (pip install -r requirements.txt)") from exc
    try:
        with yt_dlp.YoutubeDL(_ydl_opts(settings, work_dir, probe=True)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        text = str(exc)
        if _is_fatal(text):
            raise DownloadError(_friendly_error(text)) from exc
        log.warning("could not list formats for %s: %s", url, _one_line(text))
        return None
    if isinstance(info, dict) and info.get("entries"):
        entries = [entry for entry in info["entries"] if entry]
        info = entries[0] if entries else info
    return info if isinstance(info, dict) else None


def _write_formats_report(
    out_dir: Path,
    url: str,
    info: dict | None,
    errors: list[tuple[str, str]],
    live_state: str = "",
    fatal: str = "",
) -> Path:
    """Append what YouTube offered — and why every attempt failed — to ``out/formats.txt``.

    The file rides along in the artifact, so a failed run can be diagnosed later without
    digging through the Actions log.
    """
    path = out_dir / "formats.txt"
    lines = [
        "=" * 70,
        f"url: {url}",
        f"time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"live_status: {live_state or 'unknown'}",
        f"js_runtime: {js_runtime_status() or 'none found'}",
    ]
    if info:
        lines.append(f"title: {info.get('title')!r}  duration: {info.get('duration')}")
        raw = info.get("formats") or []
        lines.append(f"formats listed by yt-dlp: {len(raw)}")
        for fmt in raw[:40]:
            lines.append(
                "  "
                + "\t".join(
                    str(fmt.get(key) or "-")
                    for key in ("format_id", "ext", "height", "vcodec", "acodec", "format_note")
                )
            )
    if errors:
        lines.append("attempts:")
        lines.extend(f"  {label}: {reason}" for label, reason in errors)
    if fatal:
        lines.append(f"fatal: {fatal}")
    lines.append("")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:  # pragma: no cover - disk problems
        log.debug("could not write %s: %s", path, exc)
    return path


def list_formats(url: str, settings: Settings) -> int:
    """Print the formats YouTube offers for one URL (``--list-formats``)."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="clipper-formats-") as tmp:
        info = probe_formats(url, Path(tmp), settings)
    if info is None:
        log.error("could not read the format list for %s", url)
        return 1
    live = _live_state(info) == "is_live"
    log.info("%s — %s%s", url, info.get("title") or "unknown title", " (live)" if live else "")
    rows = _format_rows(info)
    if rows:
        for line in format_table(rows):
            log.info("%s", line)
        return 0
    raw = info.get("formats") or []
    log.warning(
        "YouTube listed %d format(s) but none are downloadable (storyboards/images only) — "
        "that is the throttled-IP / stale-cookie signature; try CLIP_PROXY or a fresh YT_COOKIES",
        len(raw),
    )
    for fmt in raw[:15]:
        log.info("  %-8s %-6s %s", fmt.get("format_id") or "-", fmt.get("ext") or "-", fmt.get("format_note") or "")
    return 1


def download(url: str, work_dir: Path, settings: Settings) -> VideoSource:
    """List the formats YouTube offers, then download the best one that answers."""
    work_dir.mkdir(parents=True, exist_ok=True)
    local = local_media_path(url)
    if local is not None:
        return local_source(url, local, settings)
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise DownloadError("yt-dlp is not installed (pip install -r requirements.txt)") from exc

    probed = probe_formats(url, work_dir, settings)
    raw_formats = (probed or {}).get("formats") or []
    rows = _format_rows(probed)
    live_state = _live_state(probed)
    if live_state == "is_upcoming":
        raise DownloadError(
            f"{url}: this is an upcoming stream — there is nothing to clip yet. "
            "Try again once it has aired (or use a normal video link)."
        )
    live = live_state == "is_live"
    images_only = bool(raw_formats) and not rows
    if probed is not None:
        label = str(probed.get("id") or video_id_from_url(url)) + (" (live)" if live else "")
        log.info(
            "formats offered for %s — %d downloadable of %d listed:",
            label,
            len(rows),
            len(raw_formats),
        )
        for line in format_table(rows):
            log.info("%s", line)
        if images_only:
            log.warning(
                "%s: YouTube listed only storyboards/images — no stream. Trying other players.",
                label,
            )
        if live:
            log.warning("%s", LIVE_WARNING)

    has_cookies = bool(settings.cookies and settings.cookies.exists())
    attempts = _attempts(live, has_cookies=has_cookies)
    errors: list[tuple[str, str]] = []
    last_error = ""
    info: dict | None = None
    for attempt in attempts:
        try:
            with yt_dlp.YoutubeDL(
                _ydl_opts(
                    settings,
                    work_dir,
                    format_selector=attempt.selector,
                    live_from_start=live,
                    player_client=attempt.player_client,
                    drop_cookies=attempt.drop_cookies,
                )
            ) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as exc:  # yt-dlp raises many exception types
            last_error = str(exc)
            reason = _one_line(last_error)
            errors.append((attempt.label, reason))
            if not _is_retryable(last_error):
                # Not worth another yt-dlp client, but the browser and the public
                # instances still get their turn in the block after this loop.
                log.warning("%s failed and is not worth retrying (%s)", attempt.label, reason)
                break
            log.warning("%s failed (%s) — trying the next attempt", attempt.label, reason)
            continue
        log.info("downloading with: %s (%s)", attempt.label, attempt.selector)
        break
    if info is None:
        direct = _direct_file_fallback(url, work_dir, settings)
        if direct is not None:
            return direct
        attempted_browser = _browser_fallback(url, work_dir, settings, probed=probed)
        if attempted_browser is not None:
            return attempted_browser
        borrowed = _upstream_fallback(url, work_dir, settings)
        if borrowed is not None:
            return borrowed
        _write_formats_report(settings.out_dir, url, probed, errors, live_state)
        message = _friendly_error(
            last_error,
            rows,
            live=live,
            images_only=images_only,
            clients_tried=[label for label, _ in errors],
        )
        reachable, why = youtube_reachable(settings)
        if not reachable:
            message += (
                " Diagnosis: a public test video fails the same way, so YouTube is blocking this "
                f"machine's IP, not this link ({why}). Fixes: set YT_PROXY to a residential proxy, "
                "or give a direct video file/link (e.g. a Dropbox ?dl=1 URL) instead of a YouTube URL."
            )
        raise DownloadError(message)

    return _source_from_info(info, url, work_dir, rows, live_state)


#: File extensions we will fetch ourselves when yt-dlp cannot handle a link.
DIRECT_SUFFIXES = (
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi",
    ".m4a", ".mp3", ".aac", ".ogg", ".opus", ".wav",
)


def _looks_like_a_file(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path.lower()
    return path.endswith(DIRECT_SUFFIXES)


def _direct_file_fallback(url: str, work_dir: Path, settings: Settings) -> VideoSource | None:
    """A file link that yt-dlp could not parse is still just bytes — fetch it ourselves.

    This is the route that works with no proxy and no cookies: a Release asset, a Dropbox
    ``?dl=1`` link, an S3 URL. Nothing on YouTube's side is involved.
    """
    if not _looks_like_a_file(url):
        return None
    from .browser import http_download

    suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower() or ".mp4"
    target = work_dir / f"direct{suffix}"
    log.info("this looks like a file, not a page — downloading it directly")
    try:
        http_download(url, target)
    except Exception as exc:  # noqa: BLE001 - fall through to the other fallbacks
        log.warning("direct download failed: %s", _one_line(str(exc)))
        return None
    if not target.exists() or target.stat().st_size < 1024:
        log.warning("direct download produced no usable file")
        return None

    duration, width, height = _ffprobe_metadata(target, settings)
    title = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).stem or "video"
    log.info(
        "direct download: %s (%.1f MB, %.0fs)",
        target.name,
        target.stat().st_size / 1e6,
        duration,
    )
    return VideoSource(
        url=url,
        video_id=slugify(title),
        title=title,
        duration=duration,
        path=target,
        width=width,
        height=height,
        info={"id": slugify(title), "title": title, "direct": True},
    )


def _upstream_fallback(url: str, work_dir: Path, settings: Settings) -> VideoSource | None:
    """Last resort: let a public Piped/Invidious instance do the YouTube request.

    This machine's IP is what YouTube refuses; an instance in another network is not
    refused (usually). Nothing is bypassed — the instance makes a normal request and
    hands back media URLs, and every clip is still cut locally.
    """
    if not getattr(settings, "upstream", False):
        return None
    video_id = video_id_from_url(url)
    if not video_id:
        return None
    try:
        from . import upstream
    except ImportError:  # pragma: no cover
        return None

    log.info("yt-dlp and the browser were both refused — trying a public instance for %s", video_id)
    try:
        video = upstream.resolve(video_id, settings)
    except Exception as exc:  # noqa: BLE001 - an instance must never break a run
        log.warning("upstream lookup failed: %s", str(exc)[:200])
        return None
    if video is None:
        log.warning("no public instance knew this link either")
        return None

    try:
        path, note = upstream.download(
            video, work_dir, settings, max_height=int(settings.max_video_height or 0)
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("upstream download failed: %s", str(exc)[:200])
        return None

    captions = None
    try:
        captions = upstream.fetch_captions(video, work_dir, settings)
    except Exception:  # noqa: BLE001 - captions are optional
        captions = None

    duration, width, height = _ffprobe_metadata(path, settings)
    log.info(
        "upstream: %s gave us the video (%s, %.0fs) — continuing without yt-dlp",
        video.instance,
        note,
        video.duration or duration,
    )
    return VideoSource(
        url=url,
        video_id=video_id,
        title=video.title or video_id,
        duration=video.duration or duration,
        uploader=video.uploader,
        path=path,
        width=width,
        height=height,
        subtitle_files=[captions] if captions else [],
        info={
            "id": video_id,
            "title": video.title,
            "instance": video.instance,
            "upstream": note,
        },
    )


def _browser_fallback(
    url: str, work_dir: Path, settings: Settings, *, probed: dict | None = None
) -> VideoSource | None:
    """Last resort: ask a real browser (obscura) instead of yt-dlp.

    Two tries, cheapest first: the browser's own cookies for yt-dlp, then the
    stream URLs the browser's player response carries. Returns ``None`` when the
    browser is not installed or YouTube refuses it too — the caller then reports
    the normal yt-dlp error, with whatever the browser said added to it.
    """
    ok, why = browser_available(settings)
    if not ok:
        log.info("browser fallback skipped: %s", why)
        return None

    log.info("yt-dlp was refused — asking the browser (%s) for this link", why)
    storage = work_dir / "browser-profile"
    page = fetch_page(url, settings, storage_dir=storage)
    log.info("browser: %s", page.describe())
    try:
        (work_dir / "browser.json").write_text(json.dumps(page.to_dict(), indent=2), encoding="utf-8")
    except OSError:  # pragma: no cover - diagnostics only
        pass

    cookie_file = write_cookies(page.cookies, work_dir / "browser-cookies.txt")
    if cookie_file is not None:
        log.info("browser holds %d cookie(s) — retrying yt-dlp with them", len(page.cookies))
        live = _live_state(probed) == "is_live"
        info = _download_with_cookie_file(url, work_dir, settings, cookie_file, live=live)
        if info is not None:
            return _source_from_info(info, url, work_dir, _format_rows(info), _live_state(info))

    if page.downloadable:
        log.info("browser's player response has stream URLs — downloading them directly")
        return _source_from_player_response(url, page, work_dir, settings)

    log.warning(
        "the browser got no stream either (%s) — this is the network, not the code",
        page.describe(),
    )
    return None


def _download_with_cookie_file(
    url: str, work_dir: Path, settings: Settings, cookie_file: Path, *, live: bool = False
) -> dict | None:
    """One yt-dlp attempt with a cookie jar the browser just wrote."""
    try:
        import yt_dlp
    except ImportError:  # pragma: no cover
        return None
    selector = "b" if live else "bv*+ba/b"
    try:
        with yt_dlp.YoutubeDL(
            _ydl_opts(
                settings,
                work_dir,
                format_selector=selector,
                live_from_start=live,
                cookie_file=cookie_file,
            )
        ) as ydl:
            return ydl.extract_info(url, download=True)
    except Exception as exc:  # noqa: BLE001 - yt-dlp raises many types
        log.warning("yt-dlp with browser cookies failed (%s)", _one_line(str(exc)))
        return None


def _source_from_player_response(
    url: str, page, work_dir: Path, settings: Settings
) -> VideoSource:
    """Download the video/audio URLs the browser's player response listed."""
    video, audio = pick_streams(page.player_response, settings.max_video_height)
    if video is None:
        raise DownloadError(
            f"{url}: the browser's player response has no downloadable stream "
            f"(playability={page.status or 'unknown'})"
        )
    details = video_details(page.player_response)
    video_path = http_download(video["url"], work_dir / "browser-video.bin")
    if audio is None:
        target = work_dir / "source.mp4"
        shutil.move(str(video_path), str(target))
    else:
        audio_path = http_download(audio["url"], work_dir / "browser-audio.bin")
        target = _mux_with_ffmpeg(video_path, audio_path, work_dir, settings)
    log.info("downloaded through the browser: %s", target.name)

    duration = 0.0
    try:
        duration = float(details.get("lengthSeconds") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return VideoSource(
        url=url,
        video_id=str(details.get("videoId") or video_id_from_url(url)),
        title=str(details.get("title") or "untitled"),
        duration=duration,
        uploader=str(details.get("author") or ""),
        path=target,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        info={
            "id": details.get("videoId"),
            "title": details.get("title"),
            "duration": duration,
            "uploader": details.get("author"),
            "webpage_url": url,
            "source": "obscura-browser",
        },
        chosen_format=str(video.get("itag") or video.get("formatId") or "browser"),
    )


def _mux_with_ffmpeg(video: Path, audio: Path, work_dir: Path, settings: Settings) -> Path:
    """Join separate video and audio tracks without re-encoding."""
    from .render import resolve_binary

    exe = resolve_binary(settings.ffmpeg) or settings.ffmpeg
    for suffix in (".mp4", ".mkv"):
        target = work_dir / f"source{suffix}"
        completed = subprocess.run(
            [
                exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-i", str(video), "-i", str(audio),
                "-c", "copy", "-movflags", "+faststart", str(target),
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if completed.returncode == 0 and target.exists() and target.stat().st_size:
            for leftover in (video, audio):
                leftover.unlink(missing_ok=True)
            return target
        log.debug("mux into %s failed: %s", suffix, _one_line(completed.stderr))
    raise DownloadError("could not join the browser's video and audio tracks with ffmpeg")


def _source_from_info(
    info: dict,
    url: str,
    work_dir: Path,
    rows: list[FormatRow],
    live_state: str,
) -> VideoSource:
    """Turn a yt-dlp info dict into a :class:`VideoSource`."""
    if "entries" in info and info["entries"]:
        info = info["entries"][0]

    chosen = str(info.get("format_id") or "")
    if chosen:
        note = info.get("format_note") or info.get("resolution") or ""
        log.info("picked format %s (%s)", chosen, note or "best available")

    path = _downloaded_path(info, work_dir)
    subtitles = sorted(
        [p for p in work_dir.glob("source*.*") if p.suffix.lower() in {".vtt", ".srt"}]
    )

    source = VideoSource(
        url=url,
        video_id=str(info.get("id") or video_id_from_url(url)),
        title=str(info.get("title") or "untitled"),
        duration=float(info.get("duration") or 0.0),
        uploader=str(info.get("uploader") or info.get("channel") or ""),
        path=path,
        width=int(info.get("width") or 0),
        height=int(info.get("height") or 0),
        subtitle_files=subtitles,
        info={k: info.get(k) for k in ("id", "title", "duration", "uploader", "webpage_url", "view_count")},
        formats=[row.to_dict() for row in rows],
        chosen_format=chosen,
        live_status=live_state,
    )
    (work_dir / "info.json").write_text(json.dumps(source.to_dict(), indent=2), encoding="utf-8")
    return source


def _downloaded_path(info: dict, work_dir: Path) -> Path | None:
    for download in info.get("requested_downloads") or []:
        filepath = download.get("filepath") or download.get("_filename")
        if filepath and Path(filepath).exists():
            return Path(filepath)
    candidates = [
        p
        for p in work_dir.glob("source.*")
        if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".wav"}
    ]
    candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    return candidates[0] if candidates else None


def _friendly_error(
    raw: str,
    formats: list[FormatRow] | None = None,
    *,
    live: bool = False,
    images_only: bool = False,
    clients_tried: list[str] | None = None,
) -> str:
    lowered = raw.lower()
    tried = ", ".join(clients_tried or ()) or "default client"
    runtimes = js_runtime_status()
    runtime_hint = (
        f"JS runtime(s) available: {', '.join(sorted(runtimes))}."
        if runtimes
        else "No JS runtime (deno/node) found — yt-dlp cannot solve YouTube's challenges."
    )
    if images_only:
        return (
            "YouTube answered with storyboard images only — no video/audio stream. GitHub-hosted "
            "runners hit this on their datacenter IPs even with valid cookies (measured on Ubuntu, "
            f"Windows and macOS). Players tried: {tried}. {runtime_hint} Fixes: set YT_PROXY to a "
            "residential proxy, or give a direct video file/link instead of a YouTube URL."
        )
    if _is_retryable(lowered):
        if _is_format_error(lowered):
            listed = ", ".join(f"{row.id}={row.ext}/{row.res}" for row in (formats or [])[:6])
            hint = (
                " This is a live stream, and its formats change while it runs — try again after "
                "it ends, or use a normal video link."
                if live
                else " Try a different link, or set CLIP_PROXY / YT_PROXY if YouTube is throttling this IP."
            )
            return (
                "YouTube did not offer a format this build can download. "
                f"Formats it listed: {listed or 'none'}; players tried: {tried}.{hint}"
            )
        return (
            "YouTube's player response was unusable (signature/JS challenge), so no format "
            f"could be opened. Players tried: {tried}. {runtime_hint} A residential proxy "
            "(YT_PROXY) also helps when this runner's IP is throttled."
        )
    if "sign in to confirm" in lowered or "bot" in lowered and "sign in" in lowered:
        return (
            "YouTube asked this runner to prove it is not a bot. Add a cookies.txt file "
            "as the YT_COOKIES secret (see docs/SETUP.md)."
        )
    if "private video" in lowered:
        return "That video is private, so it cannot be downloaded."
    if "video unavailable" in lowered:
        return "YouTube says the video is unavailable (region-locked, removed, or a live stream)."
    if "members-only" in lowered:
        return "That video is members-only; it needs cookies from an account with access."
    return raw


def extract_audio(ffmpeg: str, video_path: Path, wav_path: Path) -> Path:
    """16 kHz mono PCM — what speech models want."""
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    exe = shutil.which(ffmpeg) or ffmpeg
    command = [
        exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(wav_path),
    ]
    log.debug("extracting audio: %s", " ".join(command))
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise DownloadError(f"ffmpeg could not extract audio: {result.stderr.strip()[:400]}")
    return wav_path


# --------------------------------------------------------------------------- #
# chat replay
# --------------------------------------------------------------------------- #
def download_chat(url: str, work_dir: Path, settings: Settings) -> Path | None:
    """Fetch the live-chat replay if this video has one. Never raises."""
    from .config import Settings as _Settings  # noqa: F401  (typing only)

    if settings.chat and Path(settings.chat).exists():
        return Path(settings.chat)
    if not settings.chat_fetch:
        return None
    if local_media_path(url) is not None:
        return None
    try:
        import yt_dlp
    except ImportError:  # pragma: no cover
        return None

    work_dir.mkdir(parents=True, exist_ok=True)
    opts: dict = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["live_chat"],
        "subtitlesformat": "json",
        "outtmpl": str(work_dir / "chat.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 1,
        "socket_timeout": 30,
        "logger": _YdlLogger(),
    }
    if settings.cookies and Path(settings.cookies).exists():
        opts["cookiefile"] = str(settings.cookies)
    if settings.proxy:
        opts["proxy"] = settings.proxy
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as exc:  # noqa: BLE001 - a chat file is a bonus, never a blocker
        log.debug("no chat replay for %s: %s", url, _one_line(str(exc)))
        return None
    for candidate in sorted(work_dir.glob("chat*.json")):
        if candidate.stat().st_size:
            log.info("chat replay: %s", candidate.name)
            return candidate
    return None


# --------------------------------------------------------------------------- #
# audio-only analysis, then only the windows that became clips
# --------------------------------------------------------------------------- #
def download_audio(url: str, work_dir: Path, settings: Settings) -> VideoSource:
    """Download just the audio track — enough to transcribe and to listen for hype."""
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise DownloadError("yt-dlp is not installed (pip install -r requirements.txt)") from exc

    work_dir.mkdir(parents=True, exist_ok=True)
    opts = _ydl_opts(settings, work_dir, format_selector="ba[ext=m4a]/ba/b")
    opts["outtmpl"] = str(work_dir / "audio.%(ext)s")
    opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:  # noqa: BLE001
        raise DownloadError(f"audio-only download failed for {url}: {_one_line(str(exc))}") from exc

    files = [p for p in sorted(work_dir.glob("audio.*")) if p.suffix.lower() in {".m4a", ".mp3", ".opus", ".webm", ".aac", ".ogg", ".wav", ".mp4"}]
    if not files:
        raise DownloadError("audio-only download produced no file")
    audio = files[0]
    duration = float((info or {}).get("duration") or 0.0)
    if duration <= 0:
        duration, _, _ = _ffprobe_metadata(audio, settings)
    subs = sorted(
        candidate
        for candidate in work_dir.glob("audio*.*")
        if candidate.suffix.lower() in {".vtt", ".srt"}
    )
    log.info("audio-only: %s (%.0fs, %.1f MB)", audio.name, duration, audio.stat().st_size / 1e6)
    return VideoSource(
        url=url,
        video_id=str((info or {}).get("id") or video_id_from_url(url)),
        title=str((info or {}).get("title") or ""),
        duration=duration,
        uploader=str((info or {}).get("uploader") or ""),
        path=audio,
        subtitle_files=subs,
        info={k: v for k, v in (info or {}).items() if k in {"id", "title", "duration", "uploader", "view_count"}},
        audio_only=True,
    )


def download_section(
    url: str,
    start: float,
    end: float,
    work_dir: Path,
    settings: Settings,
) -> VideoSource:
    """Download only ``[start, end)`` of the video, cut on keyframes."""
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise DownloadError("yt-dlp is not installed (pip install -r requirements.txt)") from exc

    work_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{int(start):05d}-{int(end):05d}"
    opts = _ydl_opts(settings, work_dir, format_selector=FORMAT_CHAIN)
    opts["outtmpl"] = str(work_dir / f"section-{tag}.%(ext)s")
    opts["download_ranges"] = lambda info, ydl: [  # noqa: ARG005
        {"start_time": float(start), "end_time": float(end)}
    ]
    opts["force_keyframes_at_cuts"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:  # noqa: BLE001
        raise DownloadError(f"could not download {start:.0f}-{end:.0f}s: {_one_line(str(exc))}") from exc

    files = [
        p
        for p in sorted(work_dir.glob(f"section-{tag}.*"))
        if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
    ]
    if not files:
        raise DownloadError("the section download produced no video file")
    section = files[0]
    duration, width, height = _ffprobe_metadata(section, settings)
    log.info(
        "section %.0f-%.0fs downloaded (%.1fs, %.1f MB)",
        start,
        end,
        duration,
        section.stat().st_size / 1e6,
    )
    return VideoSource(
        url=url,
        video_id=str((info or {}).get("id") or video_id_from_url(url)),
        title=str((info or {}).get("title") or ""),
        duration=duration or (end - start),
        path=section,
        width=width,
        height=height,
        info={"id": str((info or {}).get("id") or ""), "section": [start, end]},
    )

