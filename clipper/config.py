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
FIT_MODES = ("crop", "blur", "pad", "facecam", "auto")
LLM_BACKENDS = ("auto", "claude-code", "api", "heuristic")
EDITING_MODES = ("full", "cuts", "off")
LOOKS = ("none", "clean", "punchy", "cinematic")

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


def _is_source(candidate: str) -> bool:
    """A source is a link (http/https/file) or a media file that exists locally."""
    lowered = candidate.lower()
    if lowered.startswith(("http://", "https://", "file://")):
        return True
    if "://" in candidate:
        return False
    try:
        return Path(candidate).expanduser().is_file()
    except OSError:
        return False


def parse_urls(raw: str | Iterable[str] | None) -> list[str]:
    """Split a raw input into a de-duplicated list of links or local files."""
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
        if candidate and _is_source(candidate) and candidate not in urls:
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

    # editing
    editing: str = "full"  # full | cuts | off (a preset for the knobs below)
    tighten: bool = True
    max_pause: float = 0.45
    pre_roll: float = 0.10
    post_roll: float = 0.18
    remove_fillers: bool = False
    speed: float = 1.0
    punch_in: float = 1.04
    look: str = "clean"
    progress_bar: bool = True
    watermark: str = ""

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

    # browser (obscura) — used only when yt-dlp is refused
    browser: str = ""
    browser_stealth: bool = False
    browser_timeout: int = 90

    # youtube
    cookies: Path | None = None
    ytdlp_extra_args: list[str] = field(default_factory=list)
    proxy: str = ""

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
        self.proxy = (self.proxy or "").strip()

        self.clips = max(1, min(int(self.clips), 12))
        self.min_duration = max(4.0, float(self.min_duration))
        if self.max_duration <= self.min_duration:
            self.max_duration = self.min_duration + 5.0
        self.max_duration = min(float(self.max_duration), 180.0)

        self.caption_style = self.caption_style if self.caption_style in CAPTION_STYLES else "karaoke"
        self.fit = self.fit if self.fit in FIT_MODES else "crop"
        self.llm_backend = self.llm_backend if self.llm_backend in LLM_BACKENDS else "auto"
        self.editing = self.editing if self.editing in EDITING_MODES else "full"
        self.look = self.look if self.look in LOOKS else "clean"
        self.speed = min(2.0, max(0.5, float(self.speed)))
        self.punch_in = min(1.35, max(1.0, float(self.punch_in)))
        self.max_pause = min(3.0, max(0.15, float(self.max_pause)))
        self.pre_roll = min(1.0, max(0.0, float(self.pre_roll)))
        self.post_roll = min(1.5, max(0.0, float(self.post_roll)))
        self.watermark = (self.watermark or "").strip()[:40]
        self.browser = (self.browser or "").strip()
        self.browser_timeout = max(15, min(int(self.browser_timeout), 600))
        self._apply_editing_preset()
        self.words_per_caption = max(1, min(int(self.words_per_caption), 6))
        self.width = max(240, int(self.width))
        self.height = max(240, int(self.height))
        self.fps = max(15, min(int(self.fps), 60))
        self.crf = max(14, min(int(self.crf), 30))
        self.caption_size = max(0, int(self.caption_size))

    def _apply_editing_preset(self) -> None:
        """``editing`` only overrides knobs the caller left at their defaults."""
        fields = Settings.__dataclass_fields__  # type: ignore[attr-defined]

        def untouched(name: str) -> bool:
            return getattr(self, name) == fields[name].default

        if self.editing == "full":
            return
        if self.editing == "off":
            self.tighten = False
            if untouched("speed"):
                self.speed = 1.0
            if untouched("punch_in"):
                self.punch_in = 1.0
            if untouched("look"):
                self.look = "none"
            if untouched("progress_bar"):
                self.progress_bar = False
            return
        # "cuts": tighten only, no visual grade
        self.tighten = True
        if untouched("speed"):
            self.speed = 1.0
        if untouched("punch_in"):
            self.punch_in = 1.0
        if untouched("look"):
            self.look = "none"
        if untouched("progress_bar"):
            self.progress_bar = False

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
        "editing": env_str("CLIP_EDITING", "full"),
        "tighten": env_bool("CLIP_TIGHTEN", True),
        "max_pause": env_float("CLIP_MAX_PAUSE", 0.45),
        "pre_roll": env_float("CLIP_PRE_ROLL", 0.10),
        "post_roll": env_float("CLIP_POST_ROLL", 0.18),
        "remove_fillers": env_bool("CLIP_REMOVE_FILLERS", False),
        "speed": env_float("CLIP_SPEED", 1.0),
        "punch_in": env_float("CLIP_PUNCH_IN", 1.04),
        "look": env_str("CLIP_LOOK", "clean"),
        "progress_bar": env_bool("CLIP_PROGRESS_BAR", True),
        "watermark": env_str("CLIP_WATERMARK", ""),
        "llm_backend": env_str("LLM_BACKEND", "auto"),
        "llm_model": env_str("LLM_MODEL"),
        "llm_temperature": env_float("LLM_TEMPERATURE", 0.3),
        "llm_max_tokens": env_int("LLM_MAX_TOKENS", 4000),
        "claude_model": env_str("CLAUDE_MODEL", "claude-sonnet-4-5"),
        "claude_timeout": env_int("CLAUDE_TIMEOUT", 900),
        "bridge_port": env_int("CLIP_BRIDGE_PORT", 0),
        "browser": env_str("CLIP_BROWSER", "") or "",
        "browser_stealth": env_bool("CLIP_BROWSER_STEALTH", False),
        "browser_timeout": env_int("CLIP_BROWSER_TIMEOUT", 90),
        "cookies": env_str("CLIP_COOKIES"),
        "ytdlp_extra_args": env_list("YTDLP_EXTRA_ARGS"),
        "proxy": env_str("CLIP_PROXY", "") or env_str("YT_PROXY", ""),
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
