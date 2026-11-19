from __future__ import annotations

from pathlib import Path

from clipper.captions import (
    ass_time,
    build_ass,
    caption_filters,
    group_words,
    hex_to_ass,
    sanitise_text,
)
from clipper.config import Settings
from clipper.subtitles import Segment, Transcript, Word

WORDS = [
    Word(0.0, 0.4, "so"),
    Word(0.4, 0.9, "this"),
    Word(0.9, 1.4, "is"),
    Word(1.4, 2.1, "the"),
    Word(2.1, 2.9, "secret"),
]


def make_transcript() -> Transcript:
    return Transcript(
        segments=[
            Segment(
                start=0,
                end=3,
                text="so this is the secret",
                words=[Word(w.start + 10, w.end + 10, w.text) for w in WORDS],
            )
        ],
        language="en",
        duration=60,
    )


def test_hex_to_ass_reverses_to_bgr():
    assert hex_to_ass("#FFE100") == "&H0000E1FF"
    assert hex_to_ass("ffffff") == "&H00FFFFFF"
    assert hex_to_ass("not-a-colour", alpha=150) == "&H96FFFFFF"


def test_ass_time_format():
    assert ass_time(0) == "0:00:00.00"
    assert ass_time(61.25) == "0:01:01.25"
    assert ass_time(3723.5) == "1:02:03.50"


def test_sanitise_strips_markup_and_emoji():
    assert sanitise_text("{loud} text\\here") == "(loud) text/here"
    assert sanitise_text("hello 👋 world") == "hello world"
    assert "\n" not in sanitise_text("a\nb")


def test_group_words_packs_short_bursts():
    groups = group_words(WORDS, max_words=2, max_chars=40)
    assert len(groups) == 3
    assert groups[0].text == "so this"
    assert all(len(group.words) <= 2 for group in groups)


def test_group_words_breaks_on_gaps():
    words = [Word(0, 0.5, "one"), Word(3.0, 3.5, "two")]
    groups = group_words(words, max_gap=0.65)
    assert len(groups) == 2


def test_build_ass_contains_styles_karaoke_and_hook():
    settings = Settings(caption_style="karaoke", hook_titles=True)
    content = build_ass(make_transcript(), 10.0, 20.0, settings, hook="the secret nobody shares")

    assert "[Script Info]" in content
    assert "Style: Caption" in content
    assert "Style: Hook" in content
    assert "\\k" in content, "karaoke timing tags are required"
    assert "Dialogue: 0," in content
    assert "THE SECRET NOBODY SHARES" in content.replace("\\N", " ")


def test_caption_timings_are_clip_relative():
    settings = Settings(caption_style="karaoke")
    content = build_ass(make_transcript(), 10.0, 20.0, settings)
    dialogue = [line for line in content.splitlines() if line.startswith("Dialogue: 0,")]
    assert dialogue
    first = dialogue[0].split(",")
    assert first[1].startswith("0:00:00"), first[1]


def test_captions_can_be_disabled():
    settings = Settings(caption_style="none", hook_titles=False)
    content = build_ass(make_transcript(), 10.0, 20.0, settings, hook="ignored")
    assert "Dialogue:" not in content


def test_plain_style_has_no_highlight_swap():
    karaoke = build_ass(make_transcript(), 10, 20, Settings(caption_style="karaoke"))
    plain = build_ass(make_transcript(), 10, 20, Settings(caption_style="plain"))
    k_style = next(line for line in karaoke.splitlines() if line.startswith("Style: Caption"))
    p_style = next(line for line in plain.splitlines() if line.startswith("Style: Caption"))
    k_colours = k_style.split(",")[3:5]
    p_colours = p_style.split(",")[3:5]
    assert k_colours[0] != k_colours[1], "karaoke swaps primary/secondary colour"
    assert p_colours[0] == p_colours[1], "plain style keeps one colour"


def test_uppercase_option():
    settings = Settings(uppercase_captions=False)
    content = build_ass(make_transcript(), 10, 20, settings)
    assert "secret" in content
    assert "SECRET" not in content


def test_caption_filters_escapes_paths(tmp_path: Path):
    path = tmp_path / "cap:1,2.ass"
    value = caption_filters(path)
    assert value.startswith("ass=filename=")
    assert "\\:" in value
    assert "\\," in value


def test_long_group_wraps_to_two_lines():
    words = [Word(0, 1, "extraordinarily"), Word(1, 2, "long")]
    transcript = Transcript(
        segments=[Segment(start=0, end=2, text="x", words=words)], duration=10
    )
    content = build_ass(transcript, 0, 5, Settings(words_per_caption=2))
    assert "\\N" in content
    assert "EXTRAORDINARILY\\N" in content


def test_transcript_words_outside_window_are_excluded():
    transcript = Transcript(
        segments=[
            Segment(start=0, end=2, text="before", words=[Word(0, 2, "before")]),
            Segment(start=10, end=12, text="inside", words=[Word(10, 12, "inside")]),
            Segment(start=30, end=32, text="after", words=[Word(30, 32, "after")]),
        ],
        duration=40,
    )
    content = build_ass(transcript, 9, 15, Settings(uppercase_captions=False))
    assert "inside" in content
    assert "before" not in content
    assert "after" not in content
