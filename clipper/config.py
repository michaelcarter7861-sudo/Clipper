"""Settings for the clipping pipeline.

Everything is configurable through CLI flags; every flag also has an environment
fallback so a GitHub Actions workflow can drive the pipeline with `env:` only.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

CAPTION_STYLES = ("karaoke", "plain", "none")
FIT_MODES = ("crop", "blur", "pad")
LLM_BACKENDS = ("auto", "claude-code", "api", "heuristic")

_TRUTHY = {"1", "true", "t", "yes", "y", "on"}
_FALSY = {"0", "false", "f", "no", "n", "off", ""}


# --------------------------------------------------------------------------- #
# environment helpers
# --------------------------------------------------------------------------- #
def env_str(name: str, default: str | None = None) -> str | None:
    """Return a stripped environment value, treating blanks as missing."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw or default


def env_int(name: str, default: int) -> int:
    try:
        return int(str(env_str(name, str(default))).strip())
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(str(env_str(name, str(default))).strip())
    except (TypeError, ValueError):
        return default


def env_bool(name: str, default: bool) -> bool:
    raw = env_str(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUTHY:
        return True
    if lowered in _FALSY:
        return False
    return default


def env_list(name: str) -> list[str]:
    raw = env_str(name, "") or ""
    return [part for part in re.split(r"[\s,;]+", raw) if part]


def parse_urls(raw: str | Iterable[str] | None) -> list[str]:
    """Split a raw input string into a de-duplicated list of http(s) URLs."""
    if raw is None:
        return []
    if isinstance(raw, str):
        candidates = re.split(r"[\s,;]+", raw)
    else:
        candidates = []
        for item in raw:
            candidates.extend(re.split(r"[\s,;]+", str(item)))

    urls: list[str] = []
    for candidate in candidates:
        candidate = candidate.strip().strip("'\"")
        if candidate and candidate.lower().startswith(("http://", "https://")):
            if candidate not in urls:
                urls.append(candidate)
    return urls


def _path(value: str | os.PathLike[str] | None) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    return Path(text).expanduser() if text else None


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    """All knobs for one run."""

    urls: list[str] = field(default_factory=list)
    out_dir: Path = Path("out")
    work_dir: Path | None = None

    # clip selection
    clips: int = 3
    min_duration: float = 18.0
    max_duration: float = 60.0

    # transcript
    language: str | None = None
    whisper_model: str = "base"
    prefer_subtitles: bool = True

    # captions
    caption_style: str = "karaoke"
    caption_font: str = "DejaVu Sans"
    caption_size: int = 0  # 0 = auto
    caption_primary: str = "#FFFFFF"
    caption_highlight: str = "#FFE100"
    caption_outline: str = "#000000"
    uppercase_captions: bool = True
    words_per_caption: int = 3
    hook_titles: bool = True

    # render
    fit: str = "crop"
    width: int = 1080
    height: int = 1920
    fps: int = 30
    crf: int = 20
    preset: str = "veryfast"
    loudness: bool = True
    denoise: bool = False
    max_video_height: int = 1080
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"

    # llm
    llm_backend: str = "auto"
    llm_model: str | None = None
    llm_temperature: float = 0.3
    llm_max_tokens: int = 4000
    claude_model: str = "claude-sonnet-4-5"
    claude_timeout: int = 900
    bridge_port: int = 0  # 0 = pick a free port

    # youtube
    cookies: Path | None = None
    ytdlp_extra_args: list[str] = field(default_factory=list)

    # misc
    dry_run: bool = False
    keep_source: bool = False
    fail_fast: bool = False
    verbose: bool = False
    seed: int | None = None

    def __post_init__(self) -> None:
        self.urls = parse_urls(self.urls)
        self.out_dir = Path(self.out_dir)
        self.work_dir = Path(self.work_dir) if self.work_dir else None
        self.cookies = _path(self.cookies)

        self.clips = max(1, min(int(self.clips), 12))
        self.min_duration = max(4.0, float(self.min_duration))
        if self.max_duration <= self.min_duration:
            self.max_duration = self.min_duration + 5.0
        self.max_duration = min(float(self.max_duration), 180.0)

        self.caption_style = self.caption_style if self.caption_style in CAPTION_STYLES else "karaoke"
        self.fit = self.fit if self.fit in FIT_MODES else "crop"
        self.llm_backend = self.llm_backend if self.llm_backend in LLM_BACKENDS else "auto"
        self.words_per_caption = max(1, min(int(self.words_per_caption), 6))
        self.width = max(240, int(self.width))
        self.height = max(240, int(self.height))
        self.fps = max(15, min(int(self.fps), 60))
        self.crf = max(14, min(int(self.crf), 30))
        self.caption_size = max(0, int(self.caption_size))

    # -- derived ----------------------------------------------------------- #
    @property
    def aspect_is_vertical(self) -> bool:
        return self.height > self.width

    @property
    def caption_font_size(self) -> int:
        if self.caption_size:
            return self.caption_size
        return max(28, round(self.width * 0.072))

    def clip_dir(self, slug: str) -> Path:
        return self.out_dir / slug


def settings_from_env(**overrides) -> Settings:
    """Build settings from environment variables, applying ``overrides`` last."""
    env = {
        "urls": env_list("CLIP_URLS") or env_list("VIDEO_URLS"),
        "out_dir": env_str("CLIP_OUT_DIR", "out"),
        "work_dir": env_str("CLIP_WORK_DIR"),
        "clips": env_int("CLIP_COUNT", 3),
        "min_duration": env_float("CLIP_MIN_DURATION", 18.0),
        "max_duration": env_float("CLIP_MAX_DURATION", 60.0),
        "language": env_str("CLIP_LANGUAGE"),
        "whisper_model": env_str("WHISPER_MODEL", "base"),
        "prefer_subtitles": env_bool("CLIP_PREFER_SUBTITLES", True),
        "caption_style": env_str("CAPTION_STYLE", "karaoke"),
        "caption_font": env_str("CAPTION_FONT", "DejaVu Sans"),
        "caption_size": env_int("CAPTION_SIZE", 0),
        "caption_primary": env_str("CAPTION_PRIMARY", "#FFFFFF"),
        "caption_highlight": env_str("CAPTION_HIGHLIGHT", "#FFE100"),
        "caption_outline": env_str("CAPTION_OUTLINE", "#000000"),
        "uppercase_captions": env_bool("CAPTION_UPPERCASE", True),
        "words_per_caption": env_int("CAPTION_WORDS", 3),
        "hook_titles": env_bool("CAPTION_HOOK_TITLES", True),
        "fit": env_str("CLIP_FIT", "crop"),
        "fps": env_int("CLIP_FPS", 30),
        "crf": env_int("CLIP_CRF", 20),
        "preset": env_str("CLIP_PRESET", "veryfast"),
        "loudness": env_bool("CLIP_LOUDNESS", True),
        "denoise": env_bool("CLIP_DENOISE", False),
        "max_video_height": env_int("CLIP_MAX_VIDEO_HEIGHT", 1080),
        "ffmpeg": env_str("FFMPEG_BIN", "ffmpeg"),
        "ffprobe": env_str("FFPROBE_BIN", "ffprobe"),
        "llm_backend": env_str("LLM_BACKEND", "auto"),
        "llm_model": env_str("LLM_MODEL"),
        "llm_temperature": env_float("LLM_TEMPERATURE", 0.3),
        "llm_max_tokens": env_int("LLM_MAX_TOKENS", 4000),
        "claude_model": env_str("CLAUDE_MODEL", "claude-sonnet-4-5"),
        "claude_timeout": env_int("CLAUDE_TIMEOUT", 900),
        "bridge_port": env_int("CLIP_BRIDGE_PORT", 0),
        "cookies": env_str("CLIP_COOKIES"),
        "ytdlp_extra_args": env_list("YTDLP_EXTRA_ARGS"),
        "dry_run": env_bool("CLIP_DRY_RUN", False),
        "keep_source": env_bool("CLIP_KEEP_SOURCE", False),
        "fail_fast": env_bool("CLIP_FAIL_FAST", False),
        "verbose": env_bool("CLIP_VERBOSE", False),
        "seed": env_int("CLIP_SEED", -1) if env_str("CLIP_SEED") else None,
    }
    known = {f for f in Settings.__dataclass_fields__}  # type: ignore[attr-defined]
    payload = {k: v for k, v in overrides.items() if k in known and v is not None}
    env.update(payload)
    env = {k: v for k, v in env.items() if k in known}
    return Settings(**env)
