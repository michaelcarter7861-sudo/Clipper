# Setup details

Everything the README keeps short, in one place.

- [Secrets cheat-sheet](#secrets-cheat-sheet)
- [Getting a free LLM key](#getting-a-free-llm-key)
- [FreeLLMAPI router (Tashfeen Ahmed)](#freellmapi-router-tashfeen-ahmed)
- [Claude Code in CI](#claude-code-in-ci)
- [YouTube cookies (as the easiest possible way)](#youtube-cookies)
- [The browser fallback — and what it can't do](#the-browser-fallback)
- [Editing knobs](#editing-knobs)
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
| `YT_PROXY` | optional | proxy for yt-dlp, e.g. `http://user:pass@host:port` — use it when this runner's IP gets bot-checked |
| `YTDLP_EXTRA_ARGS` | optional | repository **variable** with extra yt-dlp flags, e.g. `--cookies-from-browser chrome` (only the flags you set are used) |

Environment-only knobs (set them in the workflow `env:` block if you want to change defaults):
`CLIP_COUNT`, `CLIP_MIN_DURATION`, `CLIP_MAX_DURATION`, `CAPTION_STYLE`, `CAPTION_HIGHLIGHT`,
`CAPTION_FONT`, `CLIP_FIT`, `CLIP_FPS`, `CLIP_CRF`, `CLIP_PRESET`, `WHISPER_MODEL`,
`CLIP_LANGUAGE`, `LLM_BACKEND`, `LLM_TEMPERATURE`, `YTDLP_EXTRA_ARGS`, `CLIP_PROXY`,
`CLIP_MAX_VIDEO_HEIGHT`, `CLIP_VERBOSE`, `CLIP_BROWSER`, `CLIP_BROWSER_STEALTH`,
`CLIP_BROWSER_TIMEOUT`, `CLIP_SIGNALS`.

### How a link is downloaded (formats + live streams)

Every run does a **format probe first**: yt-dlp lists what YouTube actually offers, the log
prints a table (`id / ext / res / fps / codec / size`) and the same rows land in
`out/<slug>/info.json`. The download then asks for the best available format
(`bv*+ba/b` sorted by `res` ≤ `max_height`, mp4/m4a, h264). If YouTube answers
`Requested format is not available`, the pipeline retries by itself with `best`, then `b` —
so a narrow format selector can no longer kill a run.

**A JS runtime is required.** Modern yt-dlp solves YouTube's signature (`nsig`) challenges with
an external JS runtime, and its default is **Deno only** — which is not on GitHub runners.
The workflow installs `deno` (via npm) and the code also hands yt-dlp the runner's `node`, so
`js_runtimes` contains both. Without one, YouTube answers with storyboards only and every
format selector says *"Requested format is not available"*. Check it with
`python -m clipper --doctor` (line `yt-dlp js runtime`).

The probe asks for **all** formats (`format: all`), so a video that only answers with
storyboards is visible instead of looking like "no formats at all". When the first try fails
with a format error, the ladder switches **player client** (`android` → `tv` → `web_safari` →
`ios` → `mweb`), then falls back to `best` / `b`, and finally retries without cookies when a
cookie file is present — YouTube answers differently per client, and a storyboard-only reply
is the classic sign of a throttled IP or a stale session (PO-token check). Every attempt, plus
the raw format list, is appended to `out/formats.txt` so a failed run can be diagnosed from
the artifact alone.

Live links (`/live/…`) are fetched from the start of the stream (warning in the log), and
formats on a running stream keep changing — clipping an already-finished stream
(`/watch?v=…`) is far more reliable. Upcoming streams are rejected with a clear message.

### When YouTube blocks the runner

`python -m clipper --check-youtube` probes a public-domain video. If even that yields no
downloadable format, the block is on the machine's IP (GitHub runners are datacenter
addresses) and no code change helps:

- **`YT_PROXY`** — a *residential* proxy (`http://user:pass@host:port`) is the reliable fix.
- **Direct file link** — pass a direct video URL (Dropbox `?dl=1`, a signed S3 link, …) and the
  pipeline skips YouTube entirely: no probe, no format ladder, just transcript + clips.
- **Local file** — `python -m clipper --url "/path/video.mp4"` (or a `file://` link) treats an
  existing file as the source, with ffprobe for duration and a sibling `.vtt`/`.srt` for captions.

Every failed download leaves `out/formats.txt` in the artifact: the raw format list, the JS
runtime that was available, and the exact error of every attempt (client by client).

See the formats yourself without downloading anything:

```bash
python -m clipper --list-formats --url "https://youtu.be/VIDEO_ID"
```

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

YouTube asks datacenter IPs (like GitHub runners) to "confirm you're not a bot". The
supported fix is to reuse **your own logged-in browser session** — the same thing you would
do manually. No bypass tooling is involved, and none is supported.

### Three steps in the browser

1. **Install the extension** "Get cookies.txt LOCALLY" (Chrome/Edge/Firefox store).
   It only reads the cookies of the tab you are on.
2. **Open `youtube.com` while logged in**, click the extension, choose **Export** (Netscape
   format). You get a `cookies.txt`.
3. **Check it before you paste it anywhere**:

```bash
python -m clipper.cookies --check cookies.txt --test
```

You should see something like:

```
OK · 24 cookie(s) from cookies.txt
  youtube/google cookies : 24
  login cookies          : HSID, SAPISID, SID, SSID, __Secure-3PSID
  youtube test: PASS — YouTube accepted the cookies (tested on: Big Buck Bunny…)
```

If it says `FAIL`, log out/in on YouTube, export again, and re-check. Common causes:

| Message | Meaning |
|---|---|
| `no YouTube/Google cookies` | file exported from another site / wrong tab |
| `no Google login cookies` | you were not logged in when exporting |
| `every cookie has expired` | export is old — do it again |
| `YouTube still wants a sign-in` | cookies are for the wrong account, or rotated |

### Then, in GitHub

- **Simplest:** paste the file's text into the secret `YT_COOKIES` (raw text is fine).
- **Or base64** (handy for copy/paste on mobile):
  `base64 -w0 cookies.txt` (macOS: `base64 -i cookies.txt`) → paste as `YT_COOKIES`.

The workflow runs `python -m clipper.cookies --from-secret --out … --test`, which accepts
either form, normalises the file, and prints whether YouTube accepted it. If the cookies are
bad the run continues without them instead of failing.

Locally: `python -m clipper --url … --cookies ./cookies.txt`.

> Cookies are credentials. Never commit them (they are gitignored here), never share them in
> chat, and revoke sessions from your Google account page if a file leaks.

## The browser fallback

When yt-dlp is refused, Clipper can ask [obscura](https://github.com/h4ckf0r0day/obscura) — a
headless Rust browser — for the page and its cookies (`--browser obscura`, on by default in the
workflow when the binary is present; `--check-youtube` prints both results). Full details and
the raw measurement are in [docs/BROWSER.md](BROWSER.md).

The short, honest version, measured on a GitHub runner with obscura 0.2.7:

| Step | Result |
|---|---|
| browser installs and starts | yes |
| YouTube page through the JS engine | engine aborts (SIGABRT) on `webcomponents-sd.js` — handled by a raw-page path |
| player response from the raw page | `LOGIN_REQUIRED` — *"Sign in to confirm you're not a bot"*, 0 streams |
| cookies the browser returns | 9 anonymous visitor cookies |

So the browser **does not** make cookies unnecessary: the block is tied to the runner's IP, and
every client we tried (yt-dlp's android/ios/tv/mweb/web, a real browser) got the same answer.
Cookies above, or `YT_PROXY` with a residential IP, remain the working routes.

## Editing knobs

All of these are off/neutral in `--editing off`, and on in the default `--editing full`.

| Flag | Env | Default | Effect |
|---|---|---|---|
| `--editing` | `CLIP_EDITING` | `full` | `full` · `cuts` · `off` preset |
| `--no-tighten` | `CLIP_TIGHTEN=false` | tighten on | shut off jump cuts |
| `--max-pause` | `CLIP_MAX_PAUSE` | `0.45` | pauses longer than this get cut |
| `--remove-fillers` | `CLIP_REMOVE_FILLERS` | off | also cut um/uh/matlab/yaani |
| `--speed` | `CLIP_SPEED` | `1.0` | playback speed (0.5–2.0; audio uses `atempo`) |
| `--punch-in` | `CLIP_PUNCH_IN` | `1.04` | zoom factor (1.0 = off) |
| `--look` | `CLIP_LOOK` | `clean` | `none` · `clean` · `punchy` · `cinematic` |
| `--no-progress-bar` | `CLIP_PROGRESS_BAR=false` | bar on | thin retention bar at the bottom |
| `--watermark` | `CLIP_WATERMARK` | empty | corner handle/text, e.g. `@yourchannel` |

How it works: `clipper/edit.py` builds a keep-list from the word timings (speech groups plus
0.10 s pre-roll / 0.18 s post-roll, merged when they touch), ffmpeg trims and concatenates
those slices, and the captions are rebuilt on the **new** timeline so they never drift.
`clips.json` records the plan (`cuts`, `removed_seconds`, `output_duration`) so you can audit
a cut you did not like.

Editing improves pacing and originality of presentation — it is **not** a copyright shield.
See the README section "Editing aur copyright — seedhi baat".

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
