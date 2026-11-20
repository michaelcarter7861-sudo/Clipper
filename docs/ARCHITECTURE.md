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
              ├─ edit.build_edit_plan() jump cuts + speed, word timings re-mapped
              ├─ render.render_clip()  ffmpeg: trim/concat → reframe → grade → captions → x264
              └─ _write_outputs()      manifest.json, report.md, index.html, run summary
```

## Stages

### 1. Ingest (`youtube.py`)

`yt-dlp` Python API, `noplaylist`, subtitles requested in the preferred language plus
English/Hindi/Urdu. Three things keep the download from dying on an odd video:

1. **Probe** — `probe_formats()` extracts metadata with `skip_download`, so the log (and
   `out/<slug>/info.json`) can show exactly what YouTube offered: id, ext, resolution, fps,
   codecs, size.
2. **Best available** — `format: bv*+ba/b/best` plus `format_sort`
   (`res:<max_height>`, `ext:mp4:m4a`, `codec:h264`, `br`); preferences, never filters.
3. **JS runtime** — `js_runtimes` always carries Deno and, when present, Node: yt-dlp needs a
   JS runtime to solve YouTube's `nsig` challenge, and its built-in default (Deno-only) is not
   installed on GitHub runners. Without it YouTube returns storyboards only.
4. **Fallback ladder** — format problems *and* unusable player responses
   (`_is_retryable`: format errors, "page needs to be reloaded", missing player response) move
   on to the next try: default client → `android` → `tv` → `web_safari` → `ios` → `mweb` →
   `best` → `b`, plus one cookie-free attempt. Bot-check/private/unavailable errors are fatal
   and friendly (`_friendly_error`), so no time is wasted retrying them. Every attempt is
   appended to `out/formats.txt` together with the raw format list and the available JS
   runtime, so a failed run can be diagnosed from the artifact alone.

Cookies come from `CLIP_COOKIES`, a proxy from `CLIP_PROXY`; power users can push raw flags
through `YTDLP_EXTRA_ARGS`, and `_parse_extra_args` keeps only the flags that differ from an
empty command line so yt-dlp's defaults cannot clobber `outtmpl`/`format`/subtitles.

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

### 5. Editing (`edit.py`)

`build_edit_plan` turns a clip window into a list of `Keep(start, end)` slices in clip-relative
time:

- words are grouped into speech runs; a gap wider than `max_pause` (default 0.45 s) ends a run;
- optional filler words (`um`, `matlab`, `yaani`, …) end a run too, so they get cut;
- each run gets `pre_roll` 0.10 s / `post_roll` 0.18 s handles, and runs that touch are merged;
- fragments under `MIN_KEEP` (0.30 s) are dropped, a clip whose first run starts within 0.6 s
  is anchored at 0 so the hook is never clipped, trailing dead air is trimmed;
- if less than 0.35 s would be removed (or there would be >60 cuts) the raw window is kept —
  micro-edits are worse than no edit.

`EditPlan.map_time` maps source time → output time (`None` inside a cut). `retime_transcript`
uses it to rebuild the transcript on the edited timeline, which is what the caption writer
consumes, so cuts can never desync subtitles. Speed is clamped to 0.5–2.0 because ffmpeg's
`atempo` needs that range; video uses `setpts=PTS/speed`.

The visual side (`look`, `punch_in`, `progress_bar`) is applied inside the same ffmpeg graph
after the reframe: `scale`+`crop` for the punch-in, `eq`/`unsharp`/`vignette` for the grade,
and `drawbox` with a `t`-dependent width for the retention bar. The watermark is an ASS style
so no font file path juggling is needed.

### 6. Render (`render.py`)

One ffmpeg `-filter_complex` graph per clip, input-seeked for speed:

```
# with jump cuts
[0:v]trim=0:1.58,setpts=PTS-STARTPTS[v0]; [0:a]atrim=0:1.58,asetpts=PTS-STARTPTS[a0];
[0:v]trim=4.40:6.58,setpts=PTS-STARTPTS[v1]; … ;
[v0][a0][v1][a1]concat=n=2:v=1:a=1[cv][ca];
[cv]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30,
    scale=punch-in,crop=1080:1920,eq/unsharp/vignette,fade in/out,
    drawbox=progress bar,ass=captions.ass[v]
[ca]atempo=speed,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,afade in/out[a]
```

`fit=blur` splits the stream: blurred, darkened cover copy underneath, letterboxed original on
top. `fit=pad` letterboxes on black. Output is `libx264` (CRF 20, `veryfast`), `yuv420p`,
AAC 160 kbps, `+faststart`, and a JPEG thumbnail is grabbed at 1 s. If the source has no audio
or the audio graph fails, the clip is retried with `-an` rather than losing the render.

### 7. Outputs (`pipeline.py`)

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
| More editing moves (b-roll, SFX, transitions) | `edit.py` plan + `render.py` graph |
| Music bed, SFX, b-roll | `render.py` (add inputs + `amix`/`overlay`) |
| Auto-upload to TikTok/Shorts | new module after `render_clip`, plus a workflow step |
