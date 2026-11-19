"""YouTube ingestion through yt-dlp."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

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
            "live_status": self.live_status,
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
FORMAT_ERROR_HINTS = (
    "requested format is not available",
    "no video formats found",
    "requested download is not available",
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


def _is_fatal(text: str) -> bool:
    lowered = (text or "").lower()
    return any(hint in lowered for hint in FATAL_HINTS)


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
) -> dict:
    opts: dict = {
        "format": format_selector or FORMAT_CHAIN,
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
    if settings.cookies and settings.cookies.exists():
        opts["cookiefile"] = str(settings.cookies)
    if settings.proxy:
        opts["proxy"] = settings.proxy
    if settings.ytdlp_extra_args:
        opts.update(_parse_extra_args(settings.ytdlp_extra_args))
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


def _attempts(live: bool) -> list[tuple[str, str]]:
    """Escalating format requests: the constrained pick first, then whatever exists."""
    if live:
        return [("best single file", "b"), ("best", "best"), ("best video+audio", "bv*+ba/b")]
    return [("best video+audio", "bv*+ba/b"), ("best", "best"), ("best single file", "b")]


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
        log.debug("could not list formats for %s: %s", url, text)
        return None
    if isinstance(info, dict) and info.get("entries"):
        entries = [entry for entry in info["entries"] if entry]
        info = entries[0] if entries else info
    return info if isinstance(info, dict) else None


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
    for line in format_table(_format_rows(info)):
        log.info("%s", line)
    return 0


def download(url: str, work_dir: Path, settings: Settings) -> VideoSource:
    """List the available formats, download the best one, and keep its subtitles."""
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise DownloadError("yt-dlp is not installed (pip install -r requirements.txt)") from exc

    probed = probe_formats(url, work_dir, settings)
    rows = _format_rows(probed)
    live_state = _live_state(probed)
    if live_state == "is_upcoming":
        raise DownloadError(
            f"{url}: this is an upcoming stream — there is nothing to clip yet. "
            "Try again once it has aired (or use a normal video link)."
        )
    live = live_state == "is_live"
    if probed is not None:
        label = str(probed.get("id") or video_id_from_url(url)) + (" (live)" if live else "")
        log.info("formats offered for %s — %d row(s):", label, len(rows))
        for line in format_table(rows):
            log.info("%s", line)
        if live:
            log.warning("%s", LIVE_WARNING)

    last_error = ""
    info: dict | None = None
    for label, selector in _attempts(live):
        try:
            with yt_dlp.YoutubeDL(
                _ydl_opts(settings, work_dir, format_selector=selector, live_from_start=live)
            ) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as exc:  # yt-dlp raises many exception types
            last_error = str(exc)
            if not _is_format_error(last_error):
                raise DownloadError(_friendly_error(last_error, rows, live=live)) from exc
            log.warning("%s failed (%s) — trying the next format", label, _one_line(last_error))
            continue
        log.info("downloading with format: %s", label)
        break
    if info is None:
        raise DownloadError(_friendly_error(last_error, rows, live=live))

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


def _friendly_error(raw: str, formats: list[FormatRow] | None = None, *, live: bool = False) -> str:
    lowered = raw.lower()
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
            f"Formats it listed: {listed or 'none'}.{hint}"
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
