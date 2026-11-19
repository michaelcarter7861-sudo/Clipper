# Setup details

Everything the README keeps short, in one place.

- [Secrets cheat-sheet](#secrets-cheat-sheet)
- [Getting a free LLM key](#getting-a-free-llm-key)
- [FreeLLMAPI router (Tashfeen Ahmed)](#freellmapi-router-tashfeen-ahmed)
- [Claude Code in CI](#claude-code-in-ci)
- [YouTube cookies](#youtube-cookies)
- [Whisper (videos without subtitles)](#whisper-videos-without-subtitles)
- [Fonts and caption styling](#fonts-and-caption-styling)
- [Running locally](#running-locally)
- [Cost and limits](#cost-and-limits)
- [Reading the outputs](#reading-the-outputs)

---

## Secrets cheat-sheet

Add these at **Settings → Secrets and variables → Actions → New repository secret**.
You only need **one** LLM entry.

| Secret | Required | Used for |
|---|---|---|
| `GROQ_API_KEY` | one of these | free LLM (recommended, fastest) |
| `GEMINI_API_KEY` | ″ | free LLM via Google AI Studio's OpenAI endpoint |
| `CEREBRAS_API_KEY` | ″ | free LLM |
| `OPENROUTER_API_KEY` | ″ | free models via OpenRouter |
| `MISTRAL_API_KEY` | ″ | free tier via Mistral |
| `OPENAI_BASE_URL` + `OPENAI_API_KEY` | ″ | any OpenAI-compatible endpoint, e.g. a FreeLLMAPI router at `https://…/v1` |
| `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` | ″ | an Anthropic-compatible endpoint (FreeLLMAPI bare origin) — Claude Code talks to it directly, no bridge |
| `LLM_MODEL` | optional | override the model id, e.g. `llama-3.1-8b-instant` (repository *variable* or secret) |
| `CLAUDE_MODEL` | optional | model id Claude Code reports (default `claude-sonnet-4-5`; the router decides the real model) — variable or secret |
| `YT_COOKIES` | optional | YouTube cookies, raw `cookies.txt` text **or** base64 |

Environment-only knobs (set them in the workflow `env:` block if you want to change defaults):
`CLIP_COUNT`, `CLIP_MIN_DURATION`, `CLIP_MAX_DURATION`, `CAPTION_STYLE`, `CAPTION_HIGHLIGHT`,
`CAPTION_FONT`, `CLIP_FIT`, `CLIP_FPS`, `CLIP_CRF`, `CLIP_PRESET`, `WHISPER_MODEL`,
`CLIP_LANGUAGE`, `LLM_BACKEND`, `LLM_TEMPERATURE`, `YTDLP_EXTRA_ARGS`, `CLIP_VERBOSE`.

---

## Getting a free LLM key

### Groq (recommended)

1. <https://console.groq.com/keys> → sign in (Google/GitHub) → **Create API Key**.
2. Copy it (starts with `gsk_`).
3. Save as secret `GROQ_API_KEY`.

Groq's free tier is fast and generous enough for a few videos a day. Default model here is
`llama-3.3-70b-versatile`; `LLM_MODEL=llama-3.1-8b-instant` is cheaper and faster if you
only need rough picks.

### Google AI Studio (Gemini)

1. <https://aistudio.google.com/apikey> → **Create API key**.
2. Save as `GEMINI_API_KEY`. The pipeline uses Google's OpenAI-compatible endpoint with
   `gemini-2.5-flash` by default.

### OpenRouter / Cerebras / Mistral

Same idea: create a key, save it under the matching secret name. OpenRouter's default model
here is a `:free` variant — free models there are rate-limited and can be busy.

---

## FreeLLMAPI router (Tashfeen Ahmed)

[github.com/tashfeenahmed/freellmapi](https://github.com/tashfeenahmed/freellmapi) stacks many
free provider tiers behind one endpoint, with failover. Two ways to wire it in:

### 1. Router on your machine + Claude Code on your machine

```bash
curl -fsSL https://freellmapi.co/install.sh | bash      # Docker required
# open http://localhost:3001 → Keys → add provider keys → copy the unified key
npx freellmapi setup-claude --url http://localhost:3001 --api-key <unified-key>
claude
```

### 2. Router reachable from GitHub Actions

The runner must be able to reach the router, so host it somewhere (a small VM, Fly/Render,
a tunnel). Then set, in order of preference:

- **Claude Code directly** (router speaks Anthropic's wire format):
  `ANTHROPIC_BASE_URL=https://router.example.com` (bare origin, **no** `/v1`) and
  `ANTHROPIC_AUTH_TOKEN=<unified key>`.
- **Plain API shape**: `OPENAI_BASE_URL=https://router.example.com/v1` and
  `OPENAI_API_KEY=<unified key>`.

If both are set, the workflow prefers the Anthropic endpoint for Claude Code and falls back
to the OpenAI one for the API path.

> Don't have a router yet? Skip it. One free key (Groq/Gemini) + the built-in bridge gives
> you the same "Claude Code on a free LLM" result inside Actions.

---

## Claude Code in CI

The workflow installs the CLI with:

```bash
npm install -g @anthropic-ai/claude-code
```

and then runs it headless:

```bash
claude -p "<brief>" --output-format text --permission-mode acceptEdits \
       --allowedTools Read,Write --no-session-persistence --model "$CLAUDE_MODEL"
```

with these environment variables:

| Variable | Value |
|---|---|
| `ANTHROPIC_BASE_URL` | your Anthropic endpoint, or the local bridge |
| `ANTHROPIC_AUTH_TOKEN` | any non-empty token (the bridge ignores it) |
| `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` | `CLAUDE_MODEL` |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `1` |
| `DISABLE_TELEMETRY`, `DISABLE_ERROR_REPORTING`, `DISABLE_AUTOUPDATER` | `1` |

Notes:

- No Anthropic subscription or `ANTHROPIC_API_KEY` is needed. If you *do* have a
  `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, you can skip the bridge by setting
  `llm_backend: claude-code` with no `ANTHROPIC_BASE_URL` — but that is a paid-plan path.
- Free models sometimes answer with prose instead of writing `clips.json`. The pipeline
  validates the file, retries once through the plain API path, and finally falls back to the
  heuristic scorer — so a run never ends empty-handed.
- `CLAUDE_TIMEOUT` (default 900 s) caps the agent run.

---

## YouTube cookies

YouTube increasingly asks datacenter IPs to "confirm you're not a bot". Cookies from your own
logged-in browser fix it (and unlock age-restricted or members-only videos you legitimately
have access to).

1. Install a "Get cookies.txt LOCALLY" style extension in your browser.
2. Open YouTube (logged in), export in **Netscape** format → `cookies.txt`.
3. Either:
   - **raw**: paste the whole file content into the `YT_COOKIES` secret, or
   - **base64**: `base64 -w0 cookies.txt | pbcopy` (macOS: `base64 -i cookies.txt`) and paste that.
4. The workflow writes it to `.clipper-work/cookies.txt` and exports `CLIP_COOKIES`.

Locally: `python -m clipper --url … --cookies ./cookies.txt`.

Cookies are credentials — never commit them. They are gitignored here.

---

## Whisper (videos without subtitles)

Subtitles are preferred: they are free, instant and carry word timings. When a video has
none, `faster-whisper` runs on CPU:

```bash
pip install -r requirements-whisper.txt
python -m clipper --url … --whisper-model small --language en
```

| Model | Size | Speed (CPU) | Use |
|---|---|---|---|
| `tiny` | ~75 MB | fastest | rough drafts |
| `base` | ~145 MB | default | good enough for English |
| `small` | ~490 MB | 2–4× slower | better for Urdu/Hindi accents |
| `medium` | ~1.5 GB | slow | when accuracy matters more than time |

The Actions workflow caches the model between runs.

---

## Fonts and caption styling

libass needs a font that exists on the machine. Ubuntu runners have DejaVu; the workflow
installs `fonts-dejavu-core` explicitly. Other useful options: `fonts-liberation`,
`fonts-noto-core` (much wider script coverage, including Urdu/Arabic).

```bash
python -m clipper --url … \
  --caption-font "Noto Sans" --caption-size 84 \
  --caption-highlight "#00FF88" --words-per-caption 2 --no-uppercase
```

`--caption-style plain` removes the word-by-word highlight, `none` removes captions
entirely (hook titles still show unless `--no-hook-titles`). The generated `.ass` sits next
to each clip, so you can tweak it in Aegisub/Subtitle Edit and re-burn with ffmpeg.

---

## Running locally

```bash
git clone <this repo> && cd Clipper
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-whisper.txt

export GROQ_API_KEY=gsk_...
python -m clipper --doctor
python -m clipper --url "https://youtu.be/VIDEO_ID" --clips 3 --llm-backend auto
```

Useful during development: `--dry-run` writes captions and prints the exact ffmpeg commands
without encoding; `--keep-source` keeps the downloaded video; `--verbose` explains every
decision (chunking, dropped clips, retries).

---

## Cost and limits

- **LLM**: free tiers. A 30-minute video is split into ~5 transcript chunks; each chunk is one
  request. `claude-code` uses several requests per video (Read → Write → answer).
- **Actions**: private repos spend minutes (2,000/month on the free plan). A 10-minute video
  with 3 clips is roughly 8–15 runner minutes including installs. Public repos are free.
- **Artifacts**: 90-day default retention here is 30 days; clips are ~3–15 MB each. If you
  need long-term storage, turn on `publish_release` or upload to your own storage.
- **Rate limits**: if a provider returns 429 the client backs off and retries; if the quota is
  gone, the run degrades to the API path and then to the heuristic scorer.

---

## Reading the outputs

- `out/index.html` — every video in the run, click through per video.
- `out/<slug>/index.html` — vertical previews, hook, score, timings, download links.
- `out/report.md` — human-readable table (also used as the GitHub Release notes).
- `out/manifest.json` — everything the pipeline decided, including which LLM/method was used.
- `out/<slug>/clips.json` — the editor's picks with reasons; edit and re-render if you
  disagree with a cut.
- `out/<slug>/transcript.json` — word-level transcript, reusable without re-downloading.

The run summary (Actions → run page) lists every clip with its file path.
