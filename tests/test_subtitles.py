from __future__ import annotations

from clipper.subtitles import (
    Segment,
    Transcript,
    Word,
    ensure_words,
    parse_timestamp,
    parse_vtt,
    render_for_prompt,
)

ROLLING_VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.080 --> 00:00:02.000 align:start position:0%
 
so<00:00:00.639><c> in</c><00:00:00.959><c> this</c>

00:00:02.000 --> 00:00:04.000 align:start position:0%
so in this video
we<00:00:02.500><c> are</c><00:00:03.000><c> testing</c>

00:00:04.000 --> 00:00:06.500 align:start position:0%
we are testing
the<00:00:04.600><c> caption</c><00:00:05.200><c> pipeline</c>
"""

PLAIN_VTT = """WEBVTT

00:00:01.000 --> 00:00:03.000
Hello there, this is a test.

00:00:03.000 --> 00:00:05.500
We are checking captions now.
"""

SRT = """1
00:00:00,500 --> 00:00:02,000
First line here

2
00:00:02,000 --> 00:00:04,000
Second line here
"""


def test_parse_timestamp_variants():
    assert parse_timestamp("00:01:02.500") == 62.5
    assert parse_timestamp("01:02.500") == 62.5
    assert parse_timestamp("1:02:03,250") == 3723.25
    assert parse_timestamp(None) == 0.0


def test_parse_rolling_captions_does_not_repeat_words():
    transcript = parse_vtt(ROLLING_VTT, language="en")
    text = " ".join(segment.text for segment in transcript.segments).lower()
    assert text.count("so in this") == 1
    assert text.count("we are testing") == 1
    assert "caption pipeline" in text
    assert transcript.language == "en"


def test_word_timings_from_cue_tags():
    transcript = parse_vtt(ROLLING_VTT)
    words = {word.text.lower(): word for word in transcript.words()}
    assert words["in"].start == 0.639
    assert words["this"].start == 0.959
    assert words["caption"].start == 4.6
    assert words["pipeline"].end > words["pipeline"].start


def test_plain_vtt_distributes_word_timings():
    transcript = parse_vtt(PLAIN_VTT)
    assert len(transcript.segments) == 2
    first = transcript.segments[0]
    assert first.start == 1.0
    assert first.words, "words should be synthesised for untagged cues"
    assert first.words[0].start == 1.0
    assert first.words[-1].end <= 3.01
    starts = [word.start for word in first.words]
    assert starts == sorted(starts)
    assert len(set(starts)) > 1


def test_srt_is_supported():
    transcript = parse_vtt(SRT)
    assert "First line here" in transcript.text_between(0, 4)
    assert "Second line here" in transcript.text_between(0, 4)
    assert transcript.duration and transcript.duration > 3.5


def test_ensure_words_fills_missing_timings():
    transcript = Transcript(
        segments=[Segment(start=0.0, end=2.0, text="one two three four", words=[])]
    )
    ensure_words(transcript)
    words = transcript.segments[0].words
    assert [word.text for word in words] == ["one", "two", "three", "four"]
    assert all(word.end > word.start for word in words)


def test_words_between_and_text_between():
    transcript = Transcript(
        segments=[
            Segment(
                start=1.0,
                end=3.0,
                text="alpha beta",
                words=[Word(1.0, 2.0, "alpha"), Word(2.0, 3.0, "beta")],
            ),
            Segment(
                start=5.0,
                end=6.0,
                text="gamma",
                words=[Word(5.0, 6.0, "gamma")],
            ),
        ]
    )
    assert [w.text for w in transcript.words_between(2.5, 5.5)] == ["beta", "gamma"]
    assert "alpha" in transcript.text_between(0, 2.5)


def test_transcript_roundtrip(tmp_path):
    transcript = parse_vtt(ROLLING_VTT, language="en")
    path = tmp_path / "transcript.json"
    transcript.save(path)
    loaded = Transcript.load(path)
    assert len(loaded.segments) == len(transcript.segments)
    assert loaded.words()[0].text == transcript.words()[0].text
    assert loaded.language == "en"


def test_render_for_prompt_respects_budget():
    transcript = Transcript(
        segments=[Segment(start=i, end=i + 1, text="x" * 50) for i in range(100)]
    )
    block = render_for_prompt(transcript, max_chars=400)
    assert len(block) <= 500
    assert block.startswith("[   0.0 -")


def test_empty_input_is_safe():
    assert parse_vtt("").is_empty
    assert parse_vtt("WEBVTT\n\nnot a cue").is_empty
