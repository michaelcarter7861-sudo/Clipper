"""Transcript handling: WebVTT/SRT parsing plus a normalised transcript model.

YouTube auto-captions are the cheapest possible transcript: free, fast and
surprisingly accurate. They also carry per-word timings inside ``<c>`` tags,
which is exactly what karaoke captions need.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_TS = r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
TIMING_RE = re.compile(rf"(?:{_TS})\s*-->\s*(?:{_TS})")
TIMESTAMP_TAG_RE = re.compile(r"<(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})>")
OTHER_TAG_RE = re.compile(r"<[^>]+>")
SENTENCE_END = (".", "!", "?", "。", "！", "？", "؟")


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #
@dataclass
class Word:
    start: float
    end: float
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "text": self.text}


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
            "words": [w.to_dict() for w in self.words],
        }


@dataclass
class Transcript:
    segments: list[Segment] = field(default_factory=list)
    language: str | None = None
    source: str = "unknown"
    duration: float | None = None

    # -- helpers ----------------------------------------------------------- #
    @property
    def is_empty(self) -> bool:
        return not self.segments

    def words(self) -> list[Word]:
        out: list[Word] = []
        for segment in self.segments:
            out.extend(segment.words)
        out.sort(key=lambda w: w.start)
        return out

    def words_between(self, start: float, end: float) -> list[Word]:
        return [w for w in self.words() if w.end > start and w.start < end]

    def text_between(self, start: float, end: float) -> str:
        return " ".join(seg.text for seg in self.segments if seg.end > start and seg.start < end)

    def span(self) -> tuple[float, float]:
        if not self.segments:
            return (0.0, 0.0)
        return (min(s.start for s in self.segments), max(s.end for s in self.segments))

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "source": self.source,
            "duration": self.duration,
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Transcript:
        segments: list[Segment] = []
        for raw in data.get("segments", []):
            words = [
                Word(start=float(w["start"]), end=float(w["end"]), text=str(w["text"]).strip())
                for w in raw.get("words", [])
                if str(w.get("text", "")).strip()
            ]
            segments.append(
                Segment(
                    start=float(raw.get("start", 0.0)),
                    end=float(raw.get("end", 0.0)),
                    text=str(raw.get("text", "")).strip(),
                    words=words,
                )
            )
        return cls(
            segments=segments,
            language=data.get("language"),
            source=str(data.get("source", "cache")),
            duration=data.get("duration"),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Transcript:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def parse_timestamp(raw: str | tuple | None) -> float:
    if raw is None:
        return 0.0
    if isinstance(raw, tuple):
        hours, minutes, seconds, millis = (g or "0" for g in raw)
        return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis.ljust(3, "0")) / 1000
    match = re.match(rf"^{_TS}$", str(raw).strip())
    if not match:
        return 0.0
    return parse_timestamp(match.groups())


def clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    text = text.replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", " ", text).strip()


def _split_words(text: str) -> list[str]:
    return [w for w in re.split(r"\s+", text.strip()) if w]


def _cue_tokens(text: str, cue_start: float) -> list[tuple[str, float]]:
    """Split one cue's payload into ``(word, start_time)`` pairs.

    ``<00:00:03.640><c> going</c>`` means "going" starts at 3.64s; anything
    before the first timestamp starts when the cue starts.
    """
    tokens: list[tuple[str, float]] = []
    pending = cue_start
    pos = 0

    for match in TIMESTAMP_TAG_RE.finditer(text):
        chunk = OTHER_TAG_RE.sub(" ", text[pos : match.start()])
        tokens.extend((word, pending) for word in _split_words(chunk))
        pending = parse_timestamp(match.group(1))
        pos = match.end()

    chunk = OTHER_TAG_RE.sub(" ", text[pos:])
    tokens.extend((word, pending) for word in _split_words(chunk))
    return tokens


def _finalise_words(pairs: list[tuple[str, float]], cue_start: float, cue_end: float) -> list[Word]:
    """Give every word an end time and make timings monotonic."""
    if not pairs:
        return []

    starts = [max(cue_start, min(start, cue_end)) for _, start in pairs]
    # If the cue carried no usable timings, spread words across the cue.
    if len({round(s, 2) for s in starts}) <= 1 and cue_end > cue_start:
        estimated = cue_end - cue_start
        lengths = [max(1, len(word)) for word, _ in pairs]
        total = sum(lengths) or 1
        cursor = cue_start
        starts = []
        for length in lengths:
            starts.append(cursor)
            cursor += estimated * length / total

    # enforce non-decreasing starts
    for index in range(1, len(starts)):
        if starts[index] < starts[index - 1]:
            starts[index] = starts[index - 1]

    words: list[Word] = []
    for index, (text, _) in enumerate(pairs):
        start = starts[index]
        end = starts[index + 1] if index + 1 < len(starts) else cue_end
        if end <= start:
            end = start + 0.12
        words.append(Word(start=round(start, 3), end=round(min(end, max(cue_end, start + 0.12)), 3), text=text))
    return words


def _parse_blocks(content: str) -> Iterable[tuple[float, float, str]]:
    """Yield ``(start, end, payload)`` for every subtitle cue."""
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    for block in re.split(r"\n{2,}", content):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        match = TIMING_RE.search(lines[timing_index])
        if not match:
            continue
        start = parse_timestamp(match.group(1, 2, 3, 4))
        end = parse_timestamp(match.group(5, 6, 7, 8))
        payload = "\n".join(lines[timing_index + 1 :])
        if clean_text(payload):
            yield start, end, payload


_WORD_KEY_RE = re.compile(r"[^\w\u0600-\u06ff]+", re.UNICODE)


def _comparable(word: str) -> str:
    return _WORD_KEY_RE.sub("", word).lower()


def _strip_rolling_repeat(
    previous: list[str], tokens: list[tuple[str, float]], *, min_overlap: int = 3
) -> list[tuple[str, float]]:
    """Drop the duplicated head of a rolling caption cue.

    YouTube's auto-captions show the tail of the previous line at the top of the
    next cue; without this the transcript repeats itself every two lines.
    """
    if not previous or len(tokens) <= min_overlap:
        return tokens

    prev = [_comparable(word) for word in previous]
    current = [_comparable(word) for word, _ in tokens]
    max_overlap = min(len(prev), len(current) - 1)
    for size in range(max_overlap, min_overlap - 1, -1):
        if prev[-size:] == current[:size]:
            return tokens[size:]
    return tokens


def parse_vtt(content: str, *, language: str | None = None, source: str = "subtitles") -> Transcript:
    """Parse WebVTT/SRT into a transcript, de-duplicating rolling captions."""
    words: list[Word] = []
    seen: set[tuple[int, str]] = set()
    previous: list[str] = []

    for start, end, payload in _parse_blocks(content):
        if end <= start:
            end = start + 0.5

        pairs = _cue_tokens(payload, start)
        stripped = _strip_rolling_repeat(previous, pairs)
        previous = [word for word, _ in pairs]

        cue_words = _finalise_words(stripped, start, end)
        if not cue_words:
            continue

        # Safety net: the same word at the same timestamp was already emitted.
        new_words: list[Word] = []
        for word in cue_words:
            key = (round(word.start * 4), word.text.lower())
            if key in seen:
                continue
            seen.add(key)
            new_words.append(word)

        words.extend(new_words)

    words.sort(key=lambda w: w.start)
    duration = max((w.end for w in words), default=0.0)
    return Transcript(
        segments=_group_words(words),
        language=language,
        source=source,
        duration=duration or None,
    )


def _group_words(words: list[Word], *, max_seconds: float = 14.0, max_chars: int = 220) -> list[Segment]:
    """Merge words into sentence-ish segments (what the LLM sees)."""
    segments: list[Segment] = []
    bucket: list[Word] = []

    def flush() -> None:
        if not bucket:
            return
        text = " ".join(w.text for w in bucket).strip()
        if text:
            segments.append(
                Segment(
                    start=bucket[0].start,
                    end=max(bucket[-1].end, bucket[0].start + 0.2),
                    text=text,
                    words=list(bucket),
                )
            )
        bucket.clear()

    for word in words:
        bucket.append(word)
        text_len = sum(len(w.text) + 1 for w in bucket)
        span = bucket[-1].end - bucket[0].start
        if word.text.endswith(SENTENCE_END) or span >= max_seconds or text_len >= max_chars:
            flush()
    flush()
    return segments


def ensure_words(transcript: Transcript) -> Transcript:
    """Guarantee every segment has word timings (synthesise evenly if needed)."""
    for segment in transcript.segments:
        if segment.words:
            continue
        tokens = _split_words(segment.text)
        if not tokens:
            continue
        pairs = [(token, segment.start) for token in tokens]
        segment.words = _finalise_words(pairs, segment.start, segment.end)
    return transcript


def merge_transcripts(parts: list[Transcript]) -> Transcript:
    if not parts:
        return Transcript()
    segments: list[Segment] = []
    for part in parts:
        segments.extend(part.segments)
    segments.sort(key=lambda s: s.start)
    language = next((p.language for p in parts if p.language), None)
    return Transcript(
        segments=segments,
        language=language,
        source="+".join(sorted({p.source for p in parts})),
        duration=max((p.duration or 0.0 for p in parts), default=None),
    )


# --------------------------------------------------------------------------- #
# prompt rendering
# --------------------------------------------------------------------------- #
def render_for_prompt(transcript: Transcript, *, max_chars: int = 12000) -> str:
    """Timestamped transcript lines, truncated to a character budget."""
    lines: list[str] = []
    used = 0
    for segment in transcript.segments:
        line = f"[{segment.start:6.1f} - {segment.end:6.1f}] {segment.text}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)
