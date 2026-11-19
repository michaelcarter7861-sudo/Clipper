"""Prompts for viral-moment detection.

Kept separate from the logic so the quality bar is easy to review and tune.
"""

from __future__ import annotations

SYSTEM = """\
You are a short-form video editor who has cut thousands of TikToks, Reels and \
Shorts. You can tell within two seconds whether a moment stops a scroll. You are \
given a timestamped transcript and you return the exact spans worth clipping.

Rules:
- Every clip must work with zero context: someone who lands on it cold understands it.
- The hook must land in the first 1-2 seconds: a claim, a question, a number, a \
confession, a contradiction or a story beat. Never open on "so", "um", "welcome \
back", a sponsor read, an intro or housekeeping.
- Start and end on sentence boundaries. Never cut mid-thought.
- Prefer: specific numbers, contrarian takes, confessions, stakes, before/after \
stories, quotable one-liners, emotional peaks, "nobody tells you this" advice.
- Avoid: filler, rambling setup, repeated points, inside jokes, anything that only \
makes sense with earlier context, and anything that is only good if you can see the \
video.
- Do not return overlapping clips. Do not invent timestamps outside the transcript.
- Reply with JSON only: no markdown, no code fences, no commentary.
"""

USER_TEMPLATE = """\
VIDEO TITLE: {title}
VIDEO LENGTH: {duration:.1f} seconds
LANGUAGE: {language}
HOW MANY CLIPS: {count}

HARD CONSTRAINTS
- Each clip must be between {min_duration:.0f} and {max_duration:.0f} seconds long.
- Clips must not overlap each other.
- "start" and "end" are absolute seconds from the beginning of the video.
- Return the {count} best clips, best first.

TRANSCRIPT (start - end in seconds, then the spoken text):
{transcript}

Reply with exactly this JSON shape:
{{"clips": [{{"start": 123.4, "end": 171.2, "hook": "SHORT ON-SCREEN HOOK (max 60 chars)", \
"title": "short label for this clip", "reason": "why this moment stops the scroll", \
"score": 87}}]}}

"score" is 0-100: how likely this clip is to hold a cold viewer. Be strict; most \
transcript windows are a 40, only genuinely strong moments are 80+.
"""


def build_user_prompt(
    *,
    title: str,
    duration: float,
    language: str | None,
    count: int,
    min_duration: float,
    max_duration: float,
    transcript_block: str,
) -> str:
    return USER_TEMPLATE.format(
        title=title or "untitled",
        duration=duration,
        language=language or "unknown",
        count=count,
        min_duration=min_duration,
        max_duration=max_duration,
        transcript=transcript_block,
    )


DIRECTOR_BRIEF = """\
# Job: pick the viral moments from one YouTube video

You are the editor for a short-form clipping pipeline. Your only deliverable is \
`{clips_path}` — a JSON file describing which spans of the source video to cut.

## Inputs
- `{transcript_path}` — JSON transcript: `{{"segments": [{{"start", "end", "text", "words"}}]}}`
- Source video: **{title}** ({duration:.1f}s long, language: {language})
- Source file for reference: `{video_path}` (already downloaded — do not move or modify it)

## What to produce
Write `{clips_path}` containing exactly:

```json
{{"clips": [{{"start": 123.4, "end": 171.2, "hook": "ON-SCREEN HOOK <=60 chars", \
"title": "short label", "reason": "why it stops the scroll", "score": 0-100}}]}}
```

## Constraints
- Exactly {count} clips, best first, between {min_duration:.0f}s and {max_duration:.0f}s each.
- Absolute seconds from the start of the video; no overlaps; no invented timestamps.
- Sentence boundaries only. Hook must land in the first 1-2 seconds.
- Prefer specific numbers, contrarian takes, confessions, stakes, quotable lines, \
emotional peaks. Avoid intros, sponsor reads, filler, and anything that needs context.

## How to work
1. Read the transcript file (it may be long — read it in pieces if needed).
2. Think about the strongest cold-open moments. Rank them honestly.
3. Write the JSON file. Nothing else. Do not edit any other file, do not run the \
video pipeline, do not write commentary outside the JSON.
4. Final answer: one short sentence naming the clips you chose.
"""


REPAIR = (
    "The previous answer was not valid JSON for this schema. "
    'Reply with JSON only: {"clips": [{"start": number, "end": number, '
    '"hook": string, "title": string, "reason": string, "score": number}]}'
)
