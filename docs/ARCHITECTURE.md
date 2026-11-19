# How it works

A short tour for whoever maintains this next.

```
cli.py ──▶ pipeline.run()
              │
              ├─ make_backend()        picks claude-code / api / heuristic
              │     └─ bridge.py       (only for claude-code with an OpenAI-shaped provider)
              │
              ├─ youtube.download()    yt-dlp → source.mp4 + source.<lang>.vtt
              ├─ subtitles.parse_vtt() or transcribe.transcribe()
              ├─ claude_code.run_director() or moments.plan_with_llm() or moments.heuristic_clips()
              ├─ render.render_clip()  ffmpeg: reframe → captions → loudnorm → x264
              └─ _write_outputs()      manifest.json, report.md, index.html, run summary
```

## Stages

### 1. Ingest (`youtube.py`)

`yt-dlp` Python API, capped at 1080p, `noplaylist`, subtitles requested in the preferred
language plus English/Hindi/Urdu. Cookies come from `CLIP_COOKIES`; power users can push raw
flags through `YTDLP_EXTRA_ARGS` (parsed with yt-dlp's own `parse_options`). Errors are
translated into actionable messages (bot check → cookies, private video, members-only).

### 2. Transcript (`subtitles.py`, `transcribe.py`)

The normalised model is deliberately small:

```python
Transcript(segments=[Segment(start, end, text, words=[Word(start, end, text)])])
```

`parse_vtt` handles WebVTT and SRT, including YouTube's auto-caption quirks:

- `<00:00:03.640><c> going</c>` gives per-word start times;
- cues repeat the tail of the previous line, so a rolling window of the previous cue's words
  is stripped when it matches the head of the next cue (minimum 3 words, to avoid eating
  genuine repeats like "I think … I think");
- untagged cues get word timings distributed proportionally to word length.

Whisper is the fallback, producing the same structure with real word timestamps.

### 3. Moment selection (`moments.py`, `prompts.py`, `claude_code.py`)

Three strategies behind one interface, all returning validated `ClipPlan`s:

1. **Claude Code** — the agent reads `transcript.json` with the `Read` tool and writes
   `clips.json` with `Write`. Cheapest reliable way to get a *reasoned* selection, and it
   exercises the bridge.
2. **Direct API** — transcript chunked to ≤9,000 characters / ≤10 minutes, one request per
   chunk, `response_format=json_object`, JSON extracted leniently, optional repair call.
3. **Heuristic** — sliding windows scored on hook-word density (English + Roman Urdu + Urdu
   script), question/exclamation marks, numbers and speech pace; greedy non-overlapping pick.

Validation is deliberately strict and lives in one place (`validate_plans`):

- clamp to `[0, duration]`, grow clips shorter than `min_duration`, trim longer than `max_duration`;
- snap edges onto nearby word boundaries (±2 s) so cuts do not land mid-syllable;
- drop windows with fewer than 8 characters of speech;
- rank by score, drop anything overlapping an accepted clip by >25% (0% for heuristic), keep
  the top `clips`, and re-index by start time.

### 4. Captions (`captions.py`)

Pure ASS generation, no external subtitle library:

- words are packed into bursts of ≤3 words / ≤26 characters, split before a gap >0.65 s;
- each burst is one `Dialogue` event with `{\kNN}` karaoke tags, so libass swaps the current
  word to the highlight colour on its own (cheap, no per-word events);
- long bursts wrap with an explicit `\N` (libass is configured with `WrapStyle: 2`, i.e. no
  automatic wrapping, so overflow is impossible);
- the hook title is a separate top-aligned style with a fade/pop transform, shrunk and wrapped
  to at most 3 lines;
- emoji and non-BMP glyphs are stripped (DejaVu cannot draw them and would show tofu),
  and `{}`/`\` are escaped so a stray brace cannot break the file.

### 5. Render (`render.py`)

One ffmpeg `-filter_complex` graph per clip, input-seeked for speed:

```
[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,
     setsar=1,fps=30,fade in/out,ass=captions.ass[v]
[0:a]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,afade in/out[a]
```

`fit=blur` splits the stream: blurred, darkened cover copy underneath, letterboxed original on
top. `fit=pad` letterboxes on black. Output is `libx264` (CRF 20, `veryfast`), `yuv420p`,
AAC 160 kbps, `+faststart`, and a JPEG thumbnail is grabbed at 1 s. If the source has no audio
or the audio graph fails, the clip is retried with `-an` rather than losing the render.

### 6. Outputs (`pipeline.py`)

Per video: `clips.json`, `transcript.json`, `info.json`, an ASS file and thumbnail per clip,
plus a self-contained `index.html` gallery with `<video>` previews. At the root:
`manifest.json`, `report.md`, `index.html`. In Actions the report is also appended to
`$GITHUB_STEP_SUMMARY`.

## The bridge (`bridge.py`)

`claude` speaks Anthropic Messages; free providers speak OpenAI Chat Completions. The bridge
translates in both directions, including:

- system prompt as blocks → one system message; mid-conversation system reminders become
  `<system-note>` user messages (Groq rejects late system messages);
- `tool_use` / `tool_result` blocks ⇄ `tool_calls` / `role: "tool"` messages;
- Anthropic tool schemas (`input_schema`) → OpenAI function schemas, and `tool_choice`
  (`auto`/`any`/`tool`/`none`) → `auto`/`required`/function/`none`;
- streaming: OpenAI deltas → `message_start`, `content_block_*`, `message_delta`,
  `message_stop`. Text streams through live; tool calls are buffered and emitted as one
  complete `tool_use` block (free providers dribble JSON arguments inconsistently);
- stop reasons (`tool_calls` → `tool_use`, `length` → `max_tokens`, …), token usage, and
  Anthropic-shaped errors (502) instead of crashing.

It is a plain `ThreadingHTTPServer` (stdlib only), started in a daemon thread by the pipeline
or standalone via `python -m clipper.bridge`.

## Testing

- Unit: config parsing, VTT/SRT quirks, clip validation/ranking, ASS structure and karaoke
  timing, filter-graph strings, JSON extraction, bridge translation.
- Integration: the bridge against a fake OpenAI server (stream + non-stream + tool calls);
  a real ffmpeg render of a generated clip with burned captions and a thumbnail; a real
  `claude` process against the bridge + mock LLM, asserting it uses Read/Write and produces
  `clips.json`.
- Workflow sanity: every YAML parses, the clip workflow keeps its inputs, timeout, artifact
  upload and env contract.

`pytest -q` runs everything; the ffmpeg and claude tests skip cleanly when those binaries are
missing.

## Extension points

| Want | Touch |
|---|---|
| A different caption look | `captions.py` (`_style_header`, `group_words`) |
| Smarter selection criteria | `prompts.py` |
| Another LLM provider | `llm.py` `_PROVIDER_TABLE` |
| Speaker-aware crop / face tracking | `render.py` `_base_video_chain` (crop expressions) |
| Music bed, SFX, b-roll | `render.py` (add inputs + `amix`/`overlay`) |
| Auto-upload to TikTok/Shorts | new module after `render_clip`, plus a workflow step |
