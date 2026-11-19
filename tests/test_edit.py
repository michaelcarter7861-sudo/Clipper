from __future__ import annotations

from clipper.config import Settings
from clipper.edit import (
    Keep,
    build_edit_plan,
    retime_transcript,
    summarise,
)
from clipper.subtitles import Segment, Transcript, Word


def transcript_with_gaps() -> Transcript:
    """0.0-2.0 speech, 2.0-4.5 silence, 4.5-8.0 speech, 8.0-9.0 tail silence."""
    words = [
        Word(0.0, 0.6, "this"),
        Word(0.6, 1.2, "is"),
        Word(1.2, 2.0, "speech"),
        Word(4.5, 5.0, "and"),
        Word(5.0, 6.2, "then"),
        Word(6.2, 8.0, "more"),
    ]
    return Transcript(
        segments=[
            Segment(0.0, 2.0, "this is speech", words[:3]),
            Segment(4.5, 8.0, "and then more", words[3:]),
        ],
        language="en",
        source="test",
        duration=9.0,
    )


def test_no_gaps_keeps_one_piece():
    transcript = Transcript(
        segments=[
            Segment(
                0,
                5,
                "a b c",
                [Word(0, 1, "a"), Word(1, 2.5, "b"), Word(2.5, 5, "c")],
            )
        ],
        duration=5,
    )
    plan = build_edit_plan(transcript, 0, 5, Settings())
    assert len(plan.keeps) == 1
    assert plan.is_simple
    assert plan.output_duration == 5.0


def test_long_pause_is_cut():
    plan = build_edit_plan(transcript_with_gaps(), 0, 9, Settings(max_pause=0.45))
    assert len(plan.keeps) == 2
    assert plan.removed > 1.0
    assert plan.output_duration < 9.0
    assert plan.keeps[0].start == 0.0
    # trailing dead air is trimmed, the hook start is kept
    assert 8.0 <= plan.keeps[-1].end <= 9.0
    assert not plan.is_simple


def test_tighten_off_is_untouched():
    plan = build_edit_plan(transcript_with_gaps(), 0, 9, Settings(tighten=False))
    assert len(plan.keeps) == 1
    assert plan.output_duration == 9.0
    assert plan.removed == 0.0


def test_speed_changes_output_duration_and_mapping():
    plan = build_edit_plan(
        transcript_with_gaps(), 0, 9, Settings(max_pause=0.45, speed=1.25)
    )
    kept = sum(keep.duration for keep in plan.keeps)
    assert plan.output_duration == kept / 1.25
    assert plan.map_time(0.0) == 0.0
    first_end = plan.map_time(plan.keeps[0].end)
    assert abs(first_end - plan.keeps[0].duration / 1.25) < 1e-6


def test_map_time_returns_none_inside_a_cut():
    plan = build_edit_plan(transcript_with_gaps(), 0, 9, Settings(max_pause=0.45))
    assert plan.map_time(3.0) is None
    assert plan.map_time(plan.keeps[1].start + 0.1) is not None


def test_retime_transcript_places_words_inside_output():
    plan = build_edit_plan(transcript_with_gaps(), 0, 9, Settings(max_pause=0.45))
    retimed = retime_transcript(transcript_with_gaps(), plan)
    words = retimed.words()
    assert words
    assert all(0 <= word.start < plan.output_duration for word in words)
    assert all(word.end <= plan.output_duration + 1e-6 for word in words)
    assert [word.start for word in words] == sorted(word.start for word in words)
    # "and then more" used to start at 4.5s and must now start almost immediately
    assert words[-3].text == "and"
    assert words[-3].start < 3.0


def test_retime_drops_words_that_were_cut():
    transcript = transcript_with_gaps()
    plan = build_edit_plan(transcript, 0, 9, Settings(max_pause=0.45))
    # A word sitting inside the removed silence must disappear.
    transcript.segments.insert(
        1, Segment(3.1, 3.4, "ghost", [Word(3.1, 3.4, "ghost")])
    )
    retimed = retime_transcript(transcript, plan)
    assert "ghost" not in [word.text for word in retimed.words()]


def test_fillers_are_removed_when_enabled():
    words = [
        Word(0.0, 0.4, "so"),
        Word(0.4, 1.4, "um"),
        Word(1.4, 2.0, "money"),
        Word(2.0, 2.8, "matters"),
    ]
    transcript = Transcript(
        segments=[Segment(0, 2.8, "so um money matters", words)], duration=2.8
    )
    plan = build_edit_plan(transcript, 0, 2.8, Settings(remove_fillers=True, max_pause=0.45))
    assert plan.fillers_removed >= 1
    assert plan.removed >= 0.3
    assert len(plan.keeps) == 2


def test_too_few_words_falls_back():
    transcript = Transcript(
        segments=[Segment(0, 4, "hi there", [Word(0, 1, "hi"), Word(3, 4, "there")])],
        duration=4,
    )
    plan = build_edit_plan(transcript, 0, 4, Settings())
    assert plan.is_simple
    assert plan.notes


def test_clip_window_is_relative_to_clip_start():
    transcript = Transcript(
        segments=[
            Segment(
                30,
                40,
                "later words",
                [Word(30, 31, "later"), Word(33, 34, "words")],
            )
        ],
        duration=60,
    )
    plan = build_edit_plan(transcript, 30, 40, Settings(max_pause=0.45))
    assert plan.keeps[0].start == 0.0
    assert all(keep.start >= 0 and keep.end <= 10.0001 for keep in plan.keeps)
    assert plan.source_duration == 10.0


def test_summary_mentions_the_edits():
    settings = Settings(max_pause=0.45, look="punchy", progress_bar=True, watermark="@me")
    plan = build_edit_plan(transcript_with_gaps(), 0, 9, settings)
    text = summarise(plan, settings)
    assert "jump cut" in text
    assert "punchy grade" in text
    assert "progress bar" in text
    assert "watermark" in text


def test_summary_for_untouched_clip():
    settings = Settings(editing="off")
    plan = build_edit_plan(transcript_with_gaps(), 0, 9, settings)
    assert summarise(plan, settings) == "as-shot (no extra editing)"


def test_keep_duration_helper():
    assert Keep(1.0, 4.5).duration == 3.5
    assert Keep(4.5, 1.0).duration == 0.0


def test_plan_serialises_to_dict():
    plan = build_edit_plan(transcript_with_gaps(), 0, 9, Settings(max_pause=0.45))
    payload = plan.to_dict()
    assert payload["cuts"] == len(plan.keeps) - 1
    assert payload["removed_seconds"] > 0
    assert payload["output_duration"] <= payload["source_duration"]
