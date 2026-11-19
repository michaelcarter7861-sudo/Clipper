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


def _ydl_opts(settings: Settings, work_dir: Path) -> dict:
    opts: dict = {
        "format": (
            f"bv*[height<={settings.max_video_height}][ext=mp4]+ba[ext=m4a]/"
            f"bv*[height<={settings.max_video_height}]+ba/"
            f"b[height<={settings.max_video_height}]/b"
        ),
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
    }
    if settings.cookies and settings.cookies.exists():
        opts["cookiefile"] = str(settings.cookies)
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
    """Let power users pass through raw yt-dlp CLI flags (e.g. ``--proxy``)."""
    try:
        from yt_dlp import parse_options
    except Exception:  # pragma: no cover - yt-dlp always present in practice
        return {}
    try:
        parsed = parse_options(extra)
        opts = dict(parsed.ydl_opts)
        for noisy in ("urls", "verbose", "quiet", "simulate", "forcejson"):
            opts.pop(noisy, None)
        return opts
    except Exception as exc:  # pragma: no cover
        log.warning("could not parse YTDLP_EXTRA_ARGS (%s): %s", " ".join(extra), exc)
        return {}


def download(url: str, work_dir: Path, settings: Settings) -> VideoSource:
    """Download the video (and its subtitles) into ``work_dir``."""
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise DownloadError("yt-dlp is not installed (pip install -r requirements.txt)") from exc

    opts = _ydl_opts(settings, work_dir)

    class _Logger:
        def debug(self, msg: str) -> None:
            log.debug(msg)

        def info(self, msg: str) -> None:
            log.debug(msg)

        def warning(self, msg: str) -> None:
            log.debug("yt-dlp: %s", msg)

        def error(self, msg: str) -> None:
            log.warning("yt-dlp: %s", msg)

    opts["logger"] = _Logger()

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:  # yt-dlp raises many exception types
        raise DownloadError(_friendly_error(str(exc))) from exc

    if info is None:
        raise DownloadError(f"yt-dlp returned no metadata for {url}")
    if "entries" in info and info["entries"]:
        info = info["entries"][0]

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


def _friendly_error(raw: str) -> str:
    lowered = raw.lower()
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
