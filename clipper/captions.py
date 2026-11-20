"""Burn-in captions: styled ASS with per-word karaoke highlighting."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .subtitles import Transcript, Word

_EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F000-\U0001F2FF"
    "\U0000FE0F" "\U00002B00-\U00002BFF" "\U00002190-\U000021FF" "\U0001F900-\U0001F9FF" "]"
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    remainder = seconds % 60
    return f"{hours:d}:{minutes:02d}:{remainder:05.2f}"


def hex_to_ass(color: str, alpha: int = 0) -> str:
    """``#RRGGBB`` -> ASS ``&HAABBGGRR``."""
    raw = (color or "#FFFFFF").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) == 8:  # already RRGGBBAA
        raw = raw[:6]
    if len(raw) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", raw):
        raw = "FFFFFF"
    red, green, blue = raw[0:2], raw[2:4], raw[4:6]
    return f"&H{max(0, min(255, alpha)):02X}{blue}{green}{red}".upper()


def sanitise_text(text: str) -> str:
    """Make text safe for ASS and drop glyphs the caption font cannot draw."""
    text = _EMOJI_RE.sub("", text or "")
    text = "".join(ch for ch in text if ch <= "\uFFFF").replace("\u200b", "")
    text = text.replace("\\", "/").replace("{", "(").replace("}", ")")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _display(text: str, uppercase: bool) -> str:
    return text.upper() if uppercase else text


@dataclass
class CaptionGroup:
    words: list[Word]

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)


def group_words(
    words: Sequence[Word],
    *,
    max_words: int = 3,
    max_chars: int = 20,
    max_gap: float = 0.65,
    max_span: float = 3.2,
) -> list[CaptionGroup]:
    """Pack words into short caption bursts (2-3 words reads best on mobile)."""
    groups: list[CaptionGroup] = []
    current: list[Word] = []

    def flush() -> None:
        nonlocal current
        if current:
            groups.append(CaptionGroup(words=current))
            current = []

    for word in sorted(words, key=lambda w: w.start):
        if not current:
            current = [word]
            continue

        candidate_text = " ".join(w.text for w in [*current, word])
        gap = word.start - current[-1].end
        span = word.end - current[0].start
        if (
            len(current) >= max_words
            or len(candidate_text) > max_chars
            or gap > max_gap
            or span > max_span
        ):
            flush()
            current = [word]
        else:
            current.append(word)
    flush()
    return groups


def _wrap_lines(text: str, max_chars: int = 20, max_lines: int = 2) -> str:
    """Greedy word wrap for ASS (which only honours explicit ``\\N`` breaks)."""
    if len(text) <= max_chars or " " not in text:
        return text

    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if not current or len(candidate) <= max_chars:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: max(0, max_chars - 1)].rstrip() + "…"
    return "\\N".join(lines)


def _split_for_lines(words: list[Word], max_chars: int) -> list[list[Word]]:
    """Split a caption burst into at most two balanced lines."""
    if len(words) <= 1:
        return [words]
    text = " ".join(word.text for word in words)
    if len(text) <= max_chars:
        return [words]

    best_index, best_delta = 1, len(text)
    for index in range(1, len(words)):
        left = len(" ".join(word.text for word in words[:index]))
        right = len(" ".join(word.text for word in words[index:]))
        delta = abs(left - right)
        if delta < best_delta:
            best_index, best_delta = index, delta
    return [words[:best_index], words[best_index:]]


def _karaoke_body(
    group: CaptionGroup, settings: Settings, group_end: float, *, max_chars: int = 22
) -> str:
    """One event per group; ``\\k`` switches each word to the highlight colour."""
    words = group.words
    tags: list[str] = []
    for index, word in enumerate(words):
        if index + 1 < len(words):
            span = max(0.06, words[index + 1].start - word.start)
        else:
            span = max(0.06, group_end - word.start)
        centiseconds = max(1, int(round(span * 100)))
        text = _display(sanitise_text(word.text), settings.uppercase_captions)
        tags.append(f"{{\\k{centiseconds}}}{text}")

    lines: list[str] = []
    cursor = 0
    for line_words in _split_for_lines(words, max_chars):
        lines.append(" ".join(tags[cursor : cursor + len(line_words)]))
        cursor += len(line_words)
    return "\\N".join(lines)


def _style_header(
    settings: Settings,
    *,
    caption_margin_v: int | None = None,
    hook_margin_v: int | None = None,
) -> str:
    size = settings.caption_font_size
    hook_size = max(24, int(round(size * 0.74)))
    primary = hex_to_ass(settings.caption_primary)
    highlight = hex_to_ass(settings.caption_highlight)
    outline = hex_to_ass(settings.caption_outline)

    # karaoke: sung words use PrimaryColour, un-sung use SecondaryColour.
    if settings.caption_style == "karaoke":
        cap_primary, cap_secondary = highlight, primary
    else:
        cap_primary, cap_secondary = primary, primary

    # A facecam clip puts the captions over the gameplay panel, not the middle
    # of the frame, so the caller can shift both anchors.
    margin_v = (
        int(caption_margin_v)
        if caption_margin_v is not None
        else int(round(settings.height * 0.235))
    )
    hook_margin = (
        int(hook_margin_v)
        if hook_margin_v is not None
        else int(round(settings.height * 0.105))
    )
    outline_width = max(3, int(round(size * 0.085)))

    return f"""[Script Info]
; Generated by Clipper
ScriptType: v4.00+
PlayResX: {settings.width}
PlayResY: {settings.height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{settings.caption_font},{size},{cap_primary},{cap_secondary},{outline},&H96000000,-1,0,0,0,100,100,0,0,1,{outline_width},1,2,{int(settings.width * 0.06)},{int(settings.width * 0.06)},{margin_v},1
Style: Hook,{settings.caption_font},{hook_size},&H00FFFFFF,&H00FFFFFF,{outline},&H96000000,-1,0,0,0,100,100,0,0,1,{max(2, outline_width - 1)},1,8,{int(settings.width * 0.08)},{int(settings.width * 0.08)},{hook_margin},1
Style: Watermark,{settings.caption_font},{max(18, int(round(size * 0.42)))},&H00FFFFFF,&H00FFFFFF,{outline},&H96000000,-1,0,0,0,100,100,1,0,1,{max(1, outline_width // 3)},0,3,{int(settings.width * 0.035)},{int(settings.width * 0.035)},{int(settings.height * 0.05)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_ass(
    transcript: Transcript,
    start: float,
    end: float,
    settings: Settings,
    *,
    hook: str | None = None,
    clip_duration: float | None = None,
    caption_margin_v: int | None = None,
    hook_margin_v: int | None = None,
) -> str:
    """Build a complete .ass file for the window ``[start, end)``."""
    duration = clip_duration if clip_duration is not None else max(0.1, end - start)
    lines = [
        _style_header(
            settings, caption_margin_v=caption_margin_v, hook_margin_v=hook_margin_v
        )
    ]

    words = [
        Word(start=w.start - start, end=max(w.start - start, w.end - start), text=w.text)
        for w in transcript.words_between(start, end)
        if sanitise_text(w.text)
    ]
    words = [w for w in words if w.start < duration]

    if settings.caption_style != "none" and words:
        groups = group_words(words, max_words=settings.words_per_caption, max_chars=26)
        for index, group in enumerate(groups):
            next_start = groups[index + 1].start if index + 1 < len(groups) else duration
            group_end = min(group.end + 0.14, next_start - 0.02, duration)
            if group_end <= group.start:
                group_end = min(duration, group.start + 0.4)
            text = _karaoke_body(group, settings, group_end, max_chars=18)
            lines.append(
                "Dialogue: 0,"
                f"{ass_time(group.start)},{ass_time(group_end)},Caption,,0,0,0,,"
                f"{{\\fad(50,0)}}{text}"
            )

    watermark = sanitise_text(settings.watermark)
    if watermark:
        lines.append(
            "Dialogue: 2,"
            f"{ass_time(0.0)},{ass_time(duration)},Watermark,,0,0,0,,"
            "{\\alpha&H45&\\fad(300,300)}"
            + _display(watermark, False)
        )

    hook_text = sanitise_text(hook or "")
    if settings.hook_titles and hook_text:
        hook_end = min(duration - 0.15, 4.6)
        if hook_end > 0.6:
            display = _display(hook_text, settings.uppercase_captions)
            # Long hooks shrink a little so they never blow past the frame.
            scale = 0.82 if len(display) > 40 else 1.0
            size_override = (
                f"\\fs{max(20, int(round(settings.caption_font_size * 0.74 * scale)))}"
                if scale != 1.0
                else ""
            )
            lines.append(
                "Dialogue: 1,"
                f"{ass_time(0.12)},{ass_time(hook_end)},Hook,,0,0,0,,"
                "{\\fad(240,320)"
                + size_override
                + "\\fscx108\\fscy108\\t(0,220,\\fscx100\\fscy100)}"
                + _wrap_lines(display, max_chars=20, max_lines=3)
            )

    return "\n".join(lines) + "\n"


def write_ass(
    path: Path,
    transcript: Transcript,
    start: float,
    end: float,
    settings: Settings,
    *,
    hook: str | None = None,
    clip_duration: float | None = None,
    caption_margin_v: int | None = None,
    hook_margin_v: int | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        build_ass(
            transcript,
            start,
            end,
            settings,
            hook=hook,
            clip_duration=clip_duration,
            caption_margin_v=caption_margin_v,
            hook_margin_v=hook_margin_v,
        ),
        encoding="utf-8",
    )
    return path


def caption_filters(
    ass_path: Path, *, fonts_dir: Path | None = None
) -> str:
    """ffmpeg filter argument for burning an .ass file, with escaping."""
    def escape(value: str) -> str:
        return (
            value.replace("\\", "/")
            .replace(":", "\\:")
            .replace("'", "\\'")
            .replace("[", "\\[")
            .replace("]", "\\]")
            .replace(",", "\\,")
        )

    options = [f"filename={escape(str(ass_path))}"]
    if fonts_dir is not None:
        options.append(f"fontsdir={escape(str(fonts_dir))}")
    return "ass=" + ":".join(options)


def iter_events(ass_content: str) -> Iterable[str]:
    for line in ass_content.splitlines():
        if line.startswith("Dialogue:"):
            yield line
