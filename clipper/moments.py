"""Viral-moment selection: prompt the LLM, then validate like a paranoid editor."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .llm import ChatLLM, LLMError
from .log import get_logger, step
from .prompts import REPAIR, SYSTEM, build_user_prompt
from .subtitles import Segment, Transcript, render_for_prompt

log = get_logger()

MAX_CHUNK_CHARS = 9000
MAX_CHUNK_SPAN = 600.0

HOOK_WORDS = {
    # english
    "secret", "mistake", "never", "always", "everyone", "nobody", "actually",
    "honestly", "reason", "because", "how", "why", "biggest", "worst", "best",
    "crazy", "insane", "unbelievable", "shocking", "truth", "lie", "money",
    "million", "billion", "percent", "failed", "failure", "lost", "won",
    "listen", "look", "wait", "stop", "important", "problem", "solution",
    "hack", "trick", "tip", "advice", "lesson", "story", "happened", "imagine",
    "guess", "believe", "scared", "afraid", "love", "hate", "angry", "funny",
    "laugh", "first", "last", "only", "free", "risk", "regret", "wish",
    # roman urdu / hindi
    "yaar", "bhai", "beta", "matlab", "lekin", "agar", "phir", "abhi", "zaroor",
    "bilkul", "paise", "paisa", "zindagi", "sach", "jhoot", "log", "baat",
    "kaam", "sab", "kabhi", "kyun", "kaise", "kitna", "lakh", "crore",
    # urdu script
    "پیسہ", "پیسے", "زندگی", "سچ", "کیوں", "کیسے", "بہت", "کبھی", "لوگ",
}


@dataclass
class ClipPlan:
    start: float
    end: float
    hook: str
    title: str
    reason: str = ""
    score: int = 50
    index: int = 0
    source: str = "llm"
    layout: str = ""  # "" = decide from --fit, else force facecam | gameplay | cam

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "duration": round(self.duration, 2),
            "hook": self.hook,
            "title": self.title,
            "reason": self.reason,
            "score": self.score,
            "source": self.source,
            "layout": self.layout,
        }


# --------------------------------------------------------------------------- #
# chunking
# --------------------------------------------------------------------------- #
def chunk_segments(
    segments: list[Segment], *, max_chars: int = MAX_CHUNK_CHARS, max_span: float = MAX_CHUNK_SPAN
) -> list[list[Segment]]:
    """Split a long transcript into overlapping-ish windows that fit a prompt."""
    chunks: list[list[Segment]] = []
    current: list[Segment] = []
    used = 0
    for segment in segments:
        length = len(segment.text) + 24
        span = (segment.end - current[0].start) if current else 0.0
        if current and (used + length > max_chars or span > max_span):
            chunks.append(current)
            current = []
            used = 0
        current.append(segment)
        used += length
    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:[.,]\d+)?", value)
        if match:
            return float(match.group(0).replace(",", "."))
    return None


def _text(value: Any, limit: int, fallback: str = "") -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return fallback
    return text[:limit]


def _overlap_ratio(a: tuple[float, float], b: tuple[float, float]) -> float:
    intersection = min(a[1], b[1]) - max(a[0], b[0])
    if intersection <= 0:
        return 0.0
    shortest = min(a[1] - a[0], b[1] - b[0])
    return intersection / shortest if shortest > 0 else 1.0


def snap_to_transcript(plan: ClipPlan, transcript: Transcript, tolerance: float = 2.0) -> ClipPlan:
    """Nudge clip edges onto nearby word boundaries so cuts sound clean."""
    words = transcript.words()
    if not words:
        return plan

    starts = [w.start for w in words]
    ends = [w.end for w in words]

    best_start = min(starts, key=lambda t: abs(t - plan.start))
    if abs(best_start - plan.start) <= tolerance:
        plan.start = best_start

    best_end = min(ends, key=lambda t: abs(t - plan.end))
    if abs(best_end - plan.end) <= tolerance:
        plan.end = best_end

    return plan


def validate_plans(
    items: Iterable[dict[str, Any]],
    *,
    duration: float,
    settings: Settings,
    transcript: Transcript | None = None,
    source: str = "llm",
    limit: int | None = None,
) -> list[ClipPlan]:
    """Turn whatever the model returned into clips we can actually render."""
    plans: list[ClipPlan] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        start = _to_float(raw.get("start", raw.get("start_time")))
        end = _to_float(raw.get("end", raw.get("end_time")))
        if start is None or end is None:
            continue
        if end < start:
            start, end = end, start

        start = max(0.0, min(start, max(0.0, duration - 2.0)))
        end = min(duration, end)

        # too short -> try to grow it, too long -> trim it
        if end - start < settings.min_duration:
            need = settings.min_duration - (end - start)
            grow_end = min(duration, end + need)
            deficit = settings.min_duration - (grow_end - start)
            if deficit > 0:
                start = max(0.0, start - deficit)
            end = grow_end
        if end - start > settings.max_duration:
            end = start + settings.max_duration

        if end - start < max(4.0, settings.min_duration * 0.6):
            continue

        score = _to_float(raw.get("score", raw.get("virality")))
        plan = ClipPlan(
            start=round(start, 2),
            end=round(end, 2),
            hook=_text(raw.get("hook"), 90, fallback=_text(raw.get("title"), 60, "Watch this")),
            title=_text(raw.get("title"), 90, fallback=_text(raw.get("hook"), 60, "Clip")),
            reason=_text(raw.get("reason", raw.get("why")), 300),
            score=int(max(0, min(100, score if score is not None else 50))),
            source=source,
        )
        if transcript is not None:
            plan = snap_to_transcript(plan, transcript)
            if len(transcript.text_between(plan.start, plan.end).strip()) < 8:
                log.debug("dropping clip with no speech: %.1f-%.1f", plan.start, plan.end)
                continue
        plans.append(plan)

    return rank_plans(plans, limit=limit if limit is not None else settings.clips)


def rank_plans(
    plans: list[ClipPlan], *, limit: int | None = None, max_overlap: float = 0.25
) -> list[ClipPlan]:
    """Sort by score, drop overlapping duplicates, re-index."""
    ordered = sorted(plans, key=lambda p: (-p.score, -p.duration))
    kept: list[ClipPlan] = []
    for plan in ordered:
        if any(_overlap_ratio((plan.start, plan.end), (k.start, k.end)) > max_overlap for k in kept):
            log.debug("dropping overlapping clip %.1f-%.1f", plan.start, plan.end)
            continue
        kept.append(plan)
        if limit is not None and len(kept) >= limit:
            break

    kept.sort(key=lambda p: p.start)
    for index, plan in enumerate(kept, start=1):
        plan.index = index
    return kept


# --------------------------------------------------------------------------- #
# LLM path
# --------------------------------------------------------------------------- #
def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("clips", "moments", "results", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if "start" in payload and "end" in payload:
            return [payload]
    return []


def plan_with_llm(
    transcript: Transcript,
    duration: float,
    settings: Settings,
    llm: ChatLLM,
    *,
    title: str = "",
) -> list[ClipPlan]:
    """Chunked map over the transcript, then rank the union of candidates."""
    chunks = chunk_segments(transcript.segments)
    if not chunks:
        return []

    per_chunk = max(2, math.ceil(settings.clips * 1.5 / max(1, len(chunks)))) + 1
    candidates: list[ClipPlan] = []

    for number, chunk in enumerate(chunks, start=1):
        block = "\n".join(
            f"[{segment.start:6.1f} - {segment.end:6.1f}] {segment.text}" for segment in chunk
        )
        user = build_user_prompt(
            title=title,
            duration=duration,
            language=transcript.language,
            count=per_chunk,
            min_duration=settings.min_duration,
            max_duration=settings.max_duration,
            transcript_block=block,
        )
        log.info(
            "asking %s for moments (chunk %d/%d, %.0fs of video)",
            llm.provider.name,
            number,
            len(chunks),
            chunk[-1].end - chunk[0].start,
        )
        try:
            payload = llm.complete_json(
                SYSTEM,
                user,
                temperature=settings.llm_temperature,
                max_tokens=settings.llm_max_tokens,
            )
        except LLMError as exc:
            log.warning("chunk %d/%d failed: %s", number, len(chunks), exc)
            continue

        items = _extract_items(payload)
        if not items:
            log.warning("model returned no usable clips for chunk %d", number)
            continue

        candidates.extend(
            validate_plans(
                items,
                duration=duration,
                settings=settings,
                transcript=transcript,
                source=f"llm:{llm.provider.name}",
                limit=per_chunk,
            )
        )

    if not candidates:
        return []

    # Cross-chunk ranking: scores from different windows are not perfectly
    # comparable, so round-robin the best of each chunk first, then fill up.
    ranked = rank_plans(candidates, limit=None)
    if len(ranked) < settings.clips:
        log.info("only %d distinct clips survived validation", len(ranked))
    return rank_plans(ranked, limit=settings.clips)


# --------------------------------------------------------------------------- #
# heuristic path (no LLM at all)
# --------------------------------------------------------------------------- #
def _keyword_score(text: str) -> float:
    lowered = text.lower()
    tokens = re.findall(r"[\w\u0600-\u06ff']+", lowered)
    if not tokens:
        return 0.0
    hits = sum(1 for token in tokens if token in HOOK_WORDS)
    punctuation = text.count("?") * 2.0 + text.count("!") * 1.5
    numbers = len(re.findall(r"\b\d[\d,.]*\b", text))
    density = hits / max(1.0, len(tokens) / 40.0)
    return density + punctuation + numbers * 0.5


def _evenly_spaced(duration: float, settings: Settings) -> list[ClipPlan]:
    """Last resort: equal windows so a run still produces something postable."""
    plans: list[ClipPlan] = []
    span = duration / settings.clips if settings.clips else duration
    for index in range(settings.clips):
        start = index * span
        end = min(duration, start + min(span, settings.max_duration))
        if end - start < 3:
            continue
        plans.append(
            ClipPlan(
                start=round(start, 2),
                end=round(end, 2),
                hook="Watch this",
                title=f"Clip {index + 1}",
                reason="evenly spaced fallback",
                score=40,
                source="heuristic",
            )
        )
    return plans


def heuristic_clips(
    transcript: Transcript, duration: float, settings: Settings
) -> list[ClipPlan]:
    """Fallback: score sliding windows on hook-word density and speech pace."""
    words = transcript.words()
    if not words:
        return _evenly_spaced(duration, settings)

    target = min(settings.max_duration, max(settings.min_duration, 32.0))
    step_size = max(4.0, target / 4)
    candidates: list[ClipPlan] = []
    cursor = 0.0

    while cursor + settings.min_duration <= duration:
        end = min(duration, cursor + target)
        if end - cursor < settings.min_duration:
            break
        text = transcript.text_between(cursor, end)
        spoken = len(text.split())
        pace = spoken / max(1.0, end - cursor)
        if spoken < 10:
            cursor += step_size
            continue
        score = _keyword_score(text) + min(8.0, pace * 6.0)
        # A window that starts right after a sentence boundary reads better.
        first_words = " ".join(text.split()[:6])
        score += 2.0 if first_words[:1].isupper() else 0.0

        hook = text.split(".")[0][:70].strip() or "Watch this"
        candidates.append(
            ClipPlan(
                start=round(cursor, 2),
                end=round(end, 2),
                hook=hook,
                title=hook[:60],
                reason="heuristic pick: hook-word density and speech pace",
                score=int(max(10, min(95, 35 + score * 4))),
                source="heuristic",
            )
        )
        cursor += step_size

    if not candidates:
        return _evenly_spaced(duration, settings)

    # Sliding windows overlap by design; only one of them may survive.
    return rank_plans(candidates, limit=settings.clips, max_overlap=0.0)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def plan_clips(
    transcript: Transcript,
    duration: float,
    settings: Settings,
    *,
    llm: ChatLLM | None = None,
    title: str = "",
) -> tuple[list[ClipPlan], str]:
    """Return ``(clips, method)`` using the best available strategy."""
    if llm is not None:
        with step(f"Finding viral moments with {llm.provider.name}"):
            plans = plan_with_llm(transcript, duration, settings, llm, title=title)
        if plans:
            return plans, f"llm:{llm.provider.name}"
        log.warning("LLM returned nothing usable — falling back to the heuristic scorer")

    with step("Finding moments without an LLM"):
        plans = heuristic_clips(transcript, duration, settings)
    return plans, "heuristic"


__all__ = [
    "ClipPlan",
    "chunk_segments",
    "heuristic_clips",
    "plan_clips",
    "plan_with_llm",
    "rank_plans",
    "render_for_prompt",
    "snap_to_transcript",
    "validate_plans",
    "REPAIR",
]
