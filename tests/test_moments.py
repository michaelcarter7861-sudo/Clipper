from __future__ import annotations

from dataclasses import dataclass

import pytest

from clipper.config import Settings
from clipper.moments import (
    ClipPlan,
    chunk_segments,
    heuristic_clips,
    plan_clips,
    rank_plans,
    snap_to_transcript,
    validate_plans,
)
from clipper.subtitles import Segment, Transcript, Word


def make_transcript(duration: float = 300.0, step: float = 3.0) -> Transcript:
    segments = []
    words = []
    t = 0.0
    index = 0
    while t < duration:
        end = min(duration, t + step)
        text = f"segment {index} the secret is nobody tells you this number {index}"
        segment_words = []
        per = (end - t) / 6
        for k in range(6):
            word = Word(t + k * per, t + (k + 1) * per, f"w{index}_{k}")
            segment_words.append(word)
            words.append(word)
        segments.append(Segment(start=t, end=end, text=text, words=segment_words))
        t = end
        index += 1
    return Transcript(segments=segments, language="en", source="test", duration=duration)


def test_validate_clamps_and_trims():
    settings = Settings(clips=5, min_duration=10, max_duration=30)
    plans = validate_plans(
        [
            {"start": 5, "end": 200, "hook": "long", "score": 90},
            {"start": -10, "end": 25, "hook": "early", "score": 70},
            {"start": 400, "end": 460, "hook": "past the end", "score": 60},
        ],
        duration=300,
        settings=settings,
    )
    assert all(0 <= p.start < p.end <= 300 for p in plans)
    assert all(p.duration <= settings.max_duration + 0.01 for p in plans)


def test_too_short_clips_are_grown_or_dropped():
    settings = Settings(clips=3, min_duration=20, max_duration=60)
    plans = validate_plans(
        [{"start": 100, "end": 104, "hook": "tiny"}], duration=300, settings=settings
    )
    assert len(plans) == 1
    assert plans[0].duration >= settings.min_duration


def test_rank_drops_overlaps_and_orders_by_score():
    plans = [
        ClipPlan(0, 30, "a", "a", score=90),
        ClipPlan(5, 35, "b", "b", score=80),  # overlaps a
        ClipPlan(100, 130, "c", "c", score=60),
    ]
    ranked = rank_plans(plans, limit=3)
    assert [p.hook for p in ranked] == ["c", "a"][::-1] or [p.hook for p in ranked] == ["a", "c"]
    assert len(ranked) == 2
    assert [p.index for p in ranked] == [1, 2]


def test_rank_respects_limit():
    plans = [ClipPlan(i * 100, i * 100 + 20, f"h{i}", f"t{i}", score=50) for i in range(5)]
    assert len(rank_plans(plans, limit=2)) == 2


def test_garbage_items_are_ignored():
    settings = Settings(clips=3)
    plans = validate_plans(
        [
            "not a dict",
            {"hook": "no timings"},
            {"start": "abc", "end": "def"},
            {"start": 10, "end": 30, "hook": "ok", "score": "82"},
        ],
        duration=300,
        settings=settings,
    )
    assert len(plans) == 1
    assert plans[0].score == 82


def test_hook_and_title_have_fallbacks():
    settings = Settings(clips=1, min_duration=10, max_duration=30)
    plans = validate_plans([{"start": 0, "end": 25, "score": 10}], duration=60, settings=settings)
    assert plans[0].hook
    assert plans[0].title


def test_no_speech_windows_are_dropped():
    transcript = Transcript(
        segments=[Segment(start=100, end=110, text="hello", words=[Word(100, 110, "hello")])],
        duration=300,
    )
    plans = validate_plans(
        [{"start": 0, "end": 30, "hook": "silence"}],
        duration=300,
        settings=Settings(clips=2),
        transcript=transcript,
    )
    assert plans == []


def test_chunk_segments_limits_size():
    transcript = make_transcript(duration=3000, step=5)
    chunks = chunk_segments(transcript.segments, max_chars=2000, max_span=240)
    assert len(chunks) > 1
    for chunk in chunks:
        assert sum(len(s.text) for s in chunk) <= 2200
        assert chunk[-1].end - chunk[0].start <= 260


def test_snap_moves_to_word_boundary():
    transcript = make_transcript(duration=60, step=5)
    plan = ClipPlan(1.2, 10.9, "h", "t")
    snapped = snap_to_transcript(plan, transcript)
    starts = {w.start for w in transcript.words()}
    assert snapped.start in starts


def test_heuristic_produces_non_overlapping_clips():
    transcript = make_transcript(duration=600)
    settings = Settings(clips=3, min_duration=15, max_duration=45)
    clips = heuristic_clips(transcript, 600, settings)
    assert 1 <= len(clips) <= 3
    for clip in clips:
        assert settings.min_duration - 0.1 <= clip.duration <= settings.max_duration + 0.1
    ordered = sorted(clips, key=lambda c: c.start)
    for left, right in zip(ordered, ordered[1:], strict=False):
        assert left.end <= right.start + 0.01


def test_heuristic_without_words_still_returns_something():
    transcript = Transcript(
        segments=[Segment(start=0, end=300, text="hello world " * 40, words=[])]
    )
    clips = heuristic_clips(transcript, 300, Settings(clips=2))
    assert clips


@dataclass
class FakeProvider:
    name: str = "fake"
    model: str = "fake-model"


class FakeLLM:
    def __init__(self, payload):
        self.provider = FakeProvider()
        self.payload = payload
        self.calls = 0

    def complete_json(self, system, user, **kwargs):
        self.calls += 1
        assert "TRANSCRIPT" in user
        return self.payload


def test_plan_clips_uses_llm_payload():
    transcript = make_transcript(duration=300)
    settings = Settings(clips=2, min_duration=10, max_duration=40)
    llm = FakeLLM(
        {
            "clips": [
                {
                    "start": 20,
                    "end": 45,
                    "hook": "the secret nobody tells you",
                    "title": "secret",
                    "reason": "strong hook",
                    "score": 91,
                },
                {
                    "start": 120,
                    "end": 150,
                    "hook": "this number changes everything",
                    "title": "number",
                    "reason": "specific",
                    "score": 77,
                },
            ]
        }
    )
    clips, method = plan_clips(transcript, 300, settings, llm=llm)
    assert method == "llm:fake"
    assert len(clips) == 2
    assert clips[0].score >= clips[1].score
    assert all(clip.index > 0 for clip in clips)


def test_plan_clips_falls_back_when_llm_is_broken():
    transcript = make_transcript(duration=300)

    class Broken:
        provider = FakeProvider()

        def complete_json(self, *args, **kwargs):
            from clipper.llm import LLMError

            raise LLMError("upstream exploded")

    clips, method = plan_clips(transcript, 300, Settings(clips=2), llm=Broken())
    assert method == "heuristic"
    assert clips


def test_plan_clips_handles_list_payloads():
    transcript = make_transcript(duration=120)
    llm = FakeLLM([{"start": 10, "end": 30, "hook": "h", "score": 70}])
    clips, _ = plan_clips(transcript, 120, Settings(clips=1, min_duration=10, max_duration=40), llm=llm)
    assert len(clips) == 1


@pytest.mark.parametrize("payload", [{}, {"clips": []}, {"clips": "nope"}])
def test_empty_payloads_fall_back(payload):
    transcript = make_transcript(duration=120)
    clips, method = plan_clips(transcript, 120, Settings(clips=1), llm=FakeLLM(payload))
    assert clips
    assert method == "heuristic"
