"""Chat heat: reading a live_chat replay into bursts we can put in a prompt."""

from __future__ import annotations

import json

from clipper.chat import at, bursts, load_chat, marker_for, parse_chat, summarise
from clipper.signals import annotate_lines
from clipper.subtitles import Segment


def replay(offset_seconds: float, text: str) -> dict:
    """The shape yt-dlp writes for a chat replay entry."""
    return {
        "replayChatItemAction": {
            "actions": [
                {
                    "addChatItemAction": {
                        "item": {
                            "liveChatTextMessageRenderer": {
                                "message": {"runs": [{"text": text}]},
                            }
                        }
                    }
                }
            ],
            "videoOffsetTimeMsec": str(int(offset_seconds * 1000)),
        }
    }


def test_parse_chat_reads_yt_dlp_shape():
    payload = [replay(1.0, "hello"), replay(2.5, "LOL"), {"unrelated": True}]
    messages = parse_chat(payload)
    assert messages == [(1.0, "hello"), (2.5, "LOL")]


def test_parse_chat_reads_paid_and_plain_messages():
    payload = [
        {"replayChatItemAction": {"videoOffsetTimeMsec": "4000", "actions": [
            {"addChatItemAction": {"item": {"liveChatPaidMessageRenderer": {
                "message": {"runs": [{"text": "$5 donation"}]}}}}}
        ]}},
        {"replayChatItemAction": {"videoOffsetTimeMsec": "4500", "actions": [
            {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
                "message": {"simpleText": "plain"}}}}}
        ]}},
    ]
    assert parse_chat(payload) == [(4.0, "$5 donation"), (4.5, "plain")]


def test_load_chat_accepts_jsonl(tmp_path):
    path = tmp_path / "video.live_chat.json"
    path.write_text(
        "\n".join(json.dumps(replay(t, f"m{t}")) for t in (3.0, 9.0, 6.0)),
        encoding="utf-8",
    )
    assert [seconds for seconds, _ in load_chat(path)] == [3.0, 6.0, 9.0]


def test_load_chat_survives_rubbish(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("not json at all", encoding="utf-8")
    assert load_chat(path) == []
    assert load_chat(tmp_path / "missing.json") == []


def test_bursts_keep_the_busy_windows():
    messages = []
    # A quiet run of chat, then 20 messages inside two seconds.
    for index in range(4):
        messages.append((index * 20.0, "quiet"))
    for index in range(20):
        messages.append((100.0 + index * 0.1, "POG"))
    events = bursts(messages, bucket=5.0, min_messages=6)
    assert len(events) == 1
    event = events[0]
    assert event.count == 20
    assert event.start <= 100.0 < event.end
    assert marker_for(event) == "<<CHAT x20>>"


def test_bursts_are_empty_without_chatter():
    assert bursts([]) == []
    assert bursts([(0.0, "hi")], min_messages=6) == []


def test_bursts_merges_adjacent_buckets():
    messages = [(10.0 + index * 0.2, "x") for index in range(8)]
    messages += [(15.5 + index * 0.2, "y") for index in range(8)]
    events = bursts(messages, bucket=5.0, min_messages=6)
    assert len(events) == 1
    assert events[0].count == 16
    assert events[0].start == 10.0
    assert events[0].end == 20.0


def test_at_finds_the_nearest_burst():
    events = bursts([(100.0 + index * 0.1, "POG") for index in range(10)], min_messages=5)
    assert at(events, 101.0) is not None
    assert at(events, 400.0) is None


def test_annotate_lines_carries_both_markers():
    segments = [
        Segment(start=0.0, end=4.0, text="nothing special here"),
        Segment(start=100.0, end=104.0, text="and then he did it"),
    ]
    chat = bursts([(101.0 + index * 0.05, "W") for index in range(12)], min_messages=5)
    lines = annotate_lines(segments, [], chat)
    assert "<<CHAT x12>>" in lines[1]
    assert "CHAT" not in lines[0]


def test_summarise_is_honest_when_empty():
    assert "no chat" in summarise([])
    events = bursts([(5.0 + index * 0.1, "x") for index in range(7)], min_messages=5)
    assert "1 chat burst" in summarise(events)
