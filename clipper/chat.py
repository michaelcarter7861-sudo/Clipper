"""Chat heat: the audience counts the loud moments for us.

A YouTube live-chat replay is a per-second human reaction track — better evidence than any
audio measurement, because people type when something *happens*, not when it is merely loud.
yt-dlp writes it next to the video with ``--write-subs --sub-langs live_chat`` (a
``*.live_chat.json`` file); Clipper reads that file and marks bursts on the transcript the
same way :mod:`clipper.signals` marks loud audio.

Nothing here is invented: no file, no markers. A video without chat replay simply gets the
loudness path only.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .log import get_logger

log = get_logger()

MAX_EVENTS = 60


@dataclass(frozen=True)
class ChatEvent:
    """A burst of chat messages, in absolute video seconds."""

    start: float
    end: float
    count: int
    sample: str = ""

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "count": self.count,
            "sample": self.sample[:120],
        }


# --------------------------------------------------------------------------- #
# reading a live_chat replay
# --------------------------------------------------------------------------- #
def _renderer_text(item: dict[str, Any]) -> str:
    for key in (
        "liveChatTextMessageRenderer",
        "liveChatPaidMessageRenderer",
        "liveChatMembershipItemRenderer",
        "liveChatPaidStickerRenderer",
    ):
        node = item.get(key)
        if not isinstance(node, dict):
            continue
        message = node.get("message")
        if isinstance(message, dict):
            runs = message.get("runs") or []
            text = "".join(str(run.get("text") or "") for run in runs if isinstance(run, dict))
            if text.strip():
                return text.strip()
            if message.get("simpleText"):
                return str(message["simpleText"]).strip()
        if node.get("messageText"):
            return str(node["messageText"]).strip()
    return ""


def _offset(entry: dict[str, Any]) -> float | None:
    action = entry.get("replayChatItemAction")
    if isinstance(action, dict):
        raw = action.get("videoOffsetTimeMsec")
        if raw is not None:
            try:
                return max(0.0, float(raw) / 1000.0)
            except (TypeError, ValueError):
                return None
    for key in ("videoOffsetTimeMsec", "time_in_seconds", "time"):
        if key in entry:
            try:
                value = float(entry[key])
            except (TypeError, ValueError):
                continue
            return value / 1000.0 if key != "time_in_seconds" else value
    return None


def _messages_in(entry: dict[str, Any]) -> list[str]:
    action = entry.get("replayChatItemAction")
    if isinstance(action, dict):
        texts = []
        for item_action in action.get("actions") or []:
            if not isinstance(item_action, dict):
                continue
            add = item_action.get("addChatItemAction")
            if isinstance(add, dict) and isinstance(add.get("item"), dict):
                text = _renderer_text(add["item"])
                if text:
                    texts.append(text)
        if texts:
            return texts
    return [_renderer_text(entry)] if _renderer_text(entry) else []


def parse_chat(payload: Any) -> list[tuple[float, str]]:
    """``live_chat.json`` payload → ``[(seconds, text), …]``, oldest first."""
    entries: list[dict[str, Any]] = []
    if isinstance(payload, list):
        entries = [entry for entry in payload if isinstance(entry, dict)]
    elif isinstance(payload, dict):
        for key in ("replayChatItemAction", "actions", "comments", "messages", "chat"):
            value = payload.get(key)
            if isinstance(value, list):
                entries = [entry for entry in value if isinstance(entry, dict)]
                break
        if not entries and _offset(payload) is not None:
            entries = [payload]

    messages: list[tuple[float, str]] = []
    for entry in entries:
        seconds = _offset(entry)
        if seconds is None:
            continue
        for text in _messages_in(entry):
            messages.append((seconds, text))
    messages.sort(key=lambda item: item[0])
    return messages


def load_chat(path: Path | str) -> list[tuple[float, str]]:
    """Read a chat replay file; accepts a JSON array or one JSON object per line."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        log.warning("could not read chat replay %s: %s", path, exc)
        return []
    if not raw:
        return []
    try:
        return parse_chat(json.loads(raw))
    except ValueError:
        pass

    messages: list[tuple[float, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            messages.extend(parse_chat(json.loads(line)))
        except ValueError:
            continue
    messages.sort(key=lambda item: item[0])
    return messages


# --------------------------------------------------------------------------- #
# bursts
# --------------------------------------------------------------------------- #
def bursts(
    messages: list[tuple[float, str]],
    *,
    bucket: float = 5.0,
    min_messages: int = 6,
    max_events: int = MAX_EVENTS,
) -> list[ChatEvent]:
    """Bucket messages per second-window and keep the windows that stand out."""
    if not messages:
        return []
    bucket = max(1.0, float(bucket))
    counts: dict[int, list[str]] = {}
    for seconds, text in messages:
        index = int(seconds // bucket)
        counts.setdefault(index, []).append(text)

    values = sorted(len(items) for items in counts.values())
    if len(values) >= 8:
        # Ignore the busy tail when working out what "normal" chatter looks like for
        # this video, otherwise one huge burst drags the baseline up with it.
        quiet = values[: max(1, int(len(values) * 0.7))]
        baseline = statistics.median(quiet)
        threshold = max(float(min_messages), baseline * 2.0)
    else:
        # Too few windows to know what normal is; the absolute floor has to do.
        threshold = float(min_messages)
    hot = {index for index, items in counts.items() if len(items) >= threshold}
    if not hot:
        return []

    events: list[ChatEvent] = []
    for index in sorted(hot):
        start = index * bucket
        end = start + bucket
        items = counts[index]
        if events and start - events[-1].end <= 0.01:
            previous = events[-1]
            merged = previous.count + len(items)
            events[-1] = ChatEvent(
                start=previous.start,
                end=max(previous.end, end),
                count=merged,
                sample=previous.sample or items[0],
            )
            continue
        events.append(ChatEvent(start=start, end=end, count=len(items), sample=items[0][:120]))

    events.sort(key=lambda event: event.count, reverse=True)
    return sorted(events[:max_events], key=lambda event: event.start)


def marker_for(event: ChatEvent) -> str:
    return f"<<CHAT x{event.count}>>"


def at(events: list[ChatEvent], seconds: float, *, slack: float = 3.0) -> ChatEvent | None:
    """The strongest burst within ``slack`` seconds of ``seconds``."""
    best: ChatEvent | None = None
    for event in events:
        if event.start - slack <= seconds <= event.end + slack:
            if best is None or event.count > best.count:
                best = event
    return best


def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def summarise(events: list[ChatEvent]) -> str:
    if not events:
        return "no chat replay (or no bursts found)"
    peak = max(event.count for event in events)
    span = events[-1].end - events[0].start
    return f"{len(events)} chat burst(s), peak {peak} message(s)/{int(span / 60) + 1} min window"


__all__ = [
    "ChatEvent",
    "at",
    "bursts",
    "load_chat",
    "marker_for",
    "parse_chat",
    "summarise",
]
