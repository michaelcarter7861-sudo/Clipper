"""Editing: turn a raw window into a paced, polished clip.

Three things happen here, all without re-encoding twice:

1. **Tightening** — long pauses (and optionally filler words) are cut out, so the
   clip reads like a hand edit instead of a raw screen recording.
2. **Re-timing** — word timings are mapped onto the tightened timeline so
   captions stay glued to the audio after every cut.
3. **Polish knobs** — speed, punch-in, colour look and the retention bar live in
   :mod:`clipper.render`, driven by the plan produced here.

The plan is expressed in source-relative seconds (0 = first frame of the clip),
which is what the ffmpeg graph consumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Settings
from .log import get_logger
from .subtitles import Transcript, Word, _group_words

log = get_logger()

FILLERS = {
    # english
    "um", "umm", "uh", "uhh", "er", "erm", "ah", "ahh", "hmm", "mmm",
    "like", "y'know", "youknow", "basically", "literally", "actually",
    # roman urdu / hindi
    "matlab", "yaani", "yani", "toh", "to", "phir", "bas",
    # urdu script
    "یعنی", "متلب", "تو",
}

MIN_KEEP = 0.30  # never emit a fragment shorter than this
MAX_KEEPS = 60  # degenerate transcripts fall back to one keep


@dataclass(frozen=True)
class Keep:
    """A slice of the source clip that survives the edit, in clip-relative time."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class EditPlan:
    keeps: list[Keep]
    speed: float = 1.0
    removed: float = 0.0
    fillers_removed: int = 0
    source_duration: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def output_duration(self) -> float:
        total = sum(keep.duration for keep in self.keeps)
        return total / self.speed if self.speed else total

    @property
    def is_simple(self) -> bool:
        """True when a single-pass encode is enough (no cuts, no speed change)."""
        return len(self.keeps) <= 1 and abs(self.speed - 1.0) < 1e-6

    def map_time(self, source_time: float) -> float | None:
        """Source-relative time → output time (``None`` when the moment was cut)."""
        cursor = 0.0
        for keep in self.keeps:
            if source_time < keep.start:
                return None
            if source_time <= keep.end:
                return (cursor + (source_time - keep.start)) / self.speed
            cursor += keep.duration
        return None

    def to_dict(self) -> dict:
        return {
            "keeps": [{"start": round(k.start, 3), "end": round(k.end, 3)} for k in self.keeps],
            "speed": self.speed,
            "removed_seconds": round(self.removed, 2),
            "fillers_removed": self.fillers_removed,
            "source_duration": round(self.source_duration, 2),
            "output_duration": round(self.output_duration, 2),
            "cuts": max(0, len(self.keeps) - 1),
            "notes": self.notes,
        }


def _clean_word(text: str) -> str:
    return text.strip().strip(".,!?;:\"'()[]").lower()


def build_edit_plan(
    transcript: Transcript, start: float, end: float, settings: Settings
) -> EditPlan:
    """Decide which slices of ``[start, end)`` to keep. Never raises."""
    duration = max(0.1, end - start)
    speed = min(2.0, max(0.5, float(settings.speed)))
    whole = EditPlan(keeps=[Keep(0.0, duration)], speed=speed, source_duration=duration)

    if not settings.tighten:
        return whole

    words = [w for w in transcript.words_between(start, end) if w.end > w.start]
    if len(words) < 3:
        whole.notes.append("too little speech to tighten")
        return whole

    relative = [
        Word(start=max(0.0, w.start - start), end=min(duration, w.end - start), text=w.text)
        for w in words
    ]

    max_pause = max(0.15, float(settings.max_pause))
    pre_roll = max(0.0, float(settings.pre_roll))
    post_roll = max(0.0, float(settings.post_roll))

    groups: list[list[float]] = []  # [start, end] pairs
    current: list[float] | None = None
    fillers = 0

    for word in relative:
        is_filler = settings.remove_fillers and _clean_word(word.text) in FILLERS
        if is_filler:
            # A filler closes the current group; the gap logic then cuts it out.
            if current is not None:
                groups.append(current)
                current = None
            fillers += 1
            continue

        if current is None:
            current = [word.start, word.end]
            continue

        gap = word.start - current[1]
        if gap > max_pause:
            groups.append(current)
            current = [word.start, word.end]
        else:
            current[1] = max(current[1], word.end)
    if current is not None:
        groups.append(current)

    # Expand each group by its handles, then merge anything that overlaps.
    keeps: list[Keep] = []
    for group_start, group_end in groups:
        padded = Keep(max(0.0, group_start - pre_roll), min(duration, group_end + post_roll))
        if padded.duration < MIN_KEEP:
            continue
        if keeps and padded.start - keeps[-1].end <= 0.02:
            keeps[-1] = Keep(keeps[-1].start, max(keeps[-1].end, padded.end))
        else:
            keeps.append(padded)

    if not keeps:
        whole.notes.append("no usable speech groups")
        return whole

    keeps[0] = Keep(0.0 if keeps[0].start <= 0.6 else keeps[0].start, keeps[0].end)
    if duration - keeps[-1].end <= 0.35:
        keeps[-1] = Keep(keeps[-1].start, duration)

    kept_seconds = sum(keep.duration for keep in keeps)
    removed = duration - kept_seconds
    if removed < 0.35 or len(keeps) > MAX_KEEPS:
        whole.notes.append(
            "pauses were short" if removed < 0.35 else "too many cuts, keeping the raw window"
        )
        return whole

    plan = EditPlan(
        keeps=keeps,
        speed=speed,
        removed=removed,
        fillers_removed=fillers,
        source_duration=duration,
        notes=[f"{len(keeps) - 1} cut(s), {removed:.1f}s of silence removed"],
    )
    log.debug(
        "edit plan: %d keeps, %.1fs → %.1fs, speed %.2f",
        len(keeps),
        duration,
        plan.output_duration,
        speed,
    )
    return plan


def retime_transcript(transcript: Transcript, plan: EditPlan) -> Transcript:
    """Rebuild word timings on the edited timeline for caption alignment."""
    words: list[Word] = []
    for word in transcript.words():
        mapped_start = plan.map_time(word.start)
        if mapped_start is None:
            continue
        mapped_end = plan.map_time(word.end)
        if mapped_end is None:
            mapped_end = mapped_start + min(0.3, max(0.08, (word.end - word.start) / plan.speed))
        if mapped_end <= mapped_start:
            mapped_end = mapped_start + 0.08
        words.append(Word(start=mapped_start, end=mapped_end, text=word.text))

    words.sort(key=lambda w: w.start)
    duration = plan.output_duration
    return Transcript(
        segments=_group_words(words),
        language=transcript.language,
        source=transcript.source,
        duration=duration,
    )


def summarise(plan: EditPlan, settings: Settings) -> str:
    """One-line description for logs and the run report."""
    bits: list[str] = []
    cuts = max(0, len(plan.keeps) - 1)
    if cuts:
        bits.append(f"{cuts} jump cut(s)")
    if plan.removed >= 0.5:
        bits.append(f"-{plan.removed:.1f}s dead air")
    if plan.fillers_removed:
        bits.append(f"{plan.fillers_removed} filler word(s) cut")
    if abs(plan.speed - 1.0) > 1e-6:
        bits.append(f"{plan.speed:.2f}× speed")
    if settings.punch_in and abs(settings.punch_in - 1.0) > 1e-6:
        bits.append(f"{settings.punch_in:.0%} punch-in")
    if settings.look != "none":
        bits.append(f"{settings.look} grade")
    if settings.progress_bar:
        bits.append("progress bar")
    if settings.watermark:
        bits.append("watermark")
    return ", ".join(bits) if bits else "as-shot (no extra editing)"
