# Clipper — YouTube link → viral clips (captions + editing)

**Ek link do, clips lo.** GitHub Actions par ek button dabao, YouTube video ka URL paste karo,
aur wapas **9:16 vertical clips** lo — jisme viral moment khud dhoonda gaya ho, captions
burn ho chuke hon, audio loudness normalised ho, aur file seedha TikTok / Reels / Shorts
par upload karne layak ho.

Moment dhoondne ka kaam **Claude Code** (headless) karta hai — aur wo bhi **free LLM API keys**
par, [Tashfeen Ahmed ka FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) ya kisi bhi
free OpenAI-compatible provider (Groq / Google AI Studio / Cerebras / OpenRouter) ke through.
Koi paid Anthropic subscription zaroori nahi.

```
YouTube link
   │
   ├─ yt-dlp            video + subtitles (ya faster-whisper se transcript)
   ├─ Claude Code       transcript parhta hai, viral moments chunta hai → clips.json
   │    └─ bridge       Anthropic API ⇄ free OpenAI-compatible LLM (Groq etc.)
   ├─ transcript        word-level timings
   └─ ffmpeg            9:16 reframe + karaoke captions + hook title + loudnorm + x264
                               ↓
                        out/<video>/clip-01-....mp4  +  index.html gallery
```

---

## 1. Sabse tez tareeqa: GitHub Actions (recommended)

Sirf 3 cheezein karni hain, ek dafa:

**Step 1 — free LLM key lo (2 minute)**

| Provider | Free tier | Link |
|---|---|---|
| **Groq** (recommend: sabse fast) | haan, generous | <https://console.groq.com/keys> |
| Google AI Studio (Gemini) | haan | <https://aistudio.google.com/apikey> |
| Cerebras | haan | <https://cloud.cerebras.ai> |
| OpenRouter (free models) | haan | <https://openrouter.ai/keys> |

Kisi ek ka bhi key kaafi hai. (Agar aap ke paas FreeLLMAPI router chal raha hai to wo bhi
chalega — [section 3](#3-claude-code--free-llm-api-kaise-kaam-karta-hai) dekhein.)

**Step 2 — key ko repo secret mein daalo**

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

- Name: `GROQ_API_KEY` (ya `GEMINI_API_KEY` / `CEREBRAS_API_KEY` / `OPENROUTER_API_KEY`)
- Secret: apni key paste karein

**Step 3 — workflow chalao**

**Actions** tab → **Clipper · YouTube → viral clips** → **Run workflow** →
`urls` mein link paste karein → **Run workflow**.

Ek se zyada video chahiye to comma ya nayi line se alag karein:

```
https://youtu.be/VIDEO_ID_1
https://youtu.be/VIDEO_ID_2
```

Run khatam hone par (10–25 minute, video ki lambai par depend) **Artifacts → `clips`**
download karein, unzip karein, aur `index.html` browser mein khol lein — har clip ka
preview, hook, score aur `download mp4` link wahan milega.

> Artifact 30 din tak rehta hai. Permanent chahiye to `publish_release` = true karein:
> clips ek GitHub Release mein chale jayenge.

---

## 2. Workflow inputs

| Input | Default | Kya karta hai |
|---|---|---|
| `urls` | *required* | YouTube link(s) — comma / newline se alag |
| `clips` | `3` | per video kitne clips |
| `min_duration` / `max_duration` | `18` / `60` | clip ki lambai (seconds) |
| `caption_style` | `karaoke` | `karaoke` (word highlight) · `plain` · `none` |
| `fit` | `crop` | `crop` (center crop) · `blur` (blurred background) · `pad` (black bars) |
| `language` | `auto` | `en`, `hi`, `ur`, … subtitles/whisper ke liye |
| `whisper_model` | `base` | jab video par subtitles na hon |
| `llm_backend` | `auto` | `auto` · `claude-code` · `api` · `heuristic` |
| `publish_release` | `false` | clips ko GitHub Release mein daal do |

`llm_backend` ka matlab:

- **`auto`** — Claude Code agar install ho aur key mile, warna seedha API call, warna heuristic.
- **`claude-code`** — sirf Claude Code (headless `claude -p`) se moments chunwao.
- **`api`** — Claude Code skip, seedha ek chat-completion call (sasta aur tez).
- **`heuristic`** — koi LLM nahi; hook-words + speech pace se clips. Key ki zaroorat nahi,
  quality kam.

---

## 3. Claude Code + free LLM API kaise kaam karta hai

Claude Code Anthropic ki API language bolta hai. Free providers (Groq, Gemini, Cerebras,
OpenRouter, FreeLLMAPI) OpenAI ki language bolte hain. Is liye repo ke andar ek chhota sa
**bridge** hai (`clipper/bridge.py`) jo dono ke darmiyan translate karta hai:

```
claude -p "..."  ──Anthropic /v1/messages──▶  bridge  ──OpenAI /v1/chat/completions──▶  Groq / Gemini / FreeLLMAPI
```

- Workflow khud bridge start karta hai, `ANTHROPIC_BASE_URL` set karta hai, phir `claude`
  ko chalata hai. Claude Code ke `Read` / `Write` tools normal kaam karte hain.
- Claude Code fail ho jaye (free model ne JSON nahi diya) to pipeline khud **direct API**
  path par gir jata hai — clips phir bhi bante hain.

### FreeLLMAPI (Tashfeen Ahmed) ke sath — do tareeqe

**A) Aap ke paas FreeLLMAPI router chal raha hai** (Docker / desktop app / kisi server par),
to usko seedha Claude Code ka endpoint bana dein:

```bash
curl -fsSL https://freellmapi.co/install.sh | bash     # Docker se local router
# dashboard: http://localhost:3001  → Keys page → provider keys add karein → unified key copy
npx freellmapi setup-claude --url http://localhost:3001 --api-key <unified-key>
```

GitHub Actions se use karna hai to router runner se reachable hona chahiye (public URL /
tunnel), aur secrets mein ye do cheezein daalein:

- `ANTHROPIC_BASE_URL` = `https://your-router.example.com` (bare origin, `/v1` ke baghair)
- `ANTHROPIC_AUTH_TOKEN` = unified key (`freellmapi-...`)

Ya OpenAI-shape mein (Claude Code ke baghair):

- `OPENAI_BASE_URL` = `https://your-router.example.com/v1`
- `OPENAI_API_KEY` = unified key

**B) Bina router ke (Actions mein sabse aasan)** — sirf `GROQ_API_KEY` (ya koi ek free key)
secret daalein. Bridge usi key ko Anthropic shape mein Claude Code ko de deta hai.

### Apne laptop par Claude Code + free keys

```bash
pip install -r requirements.txt
export GROQ_API_KEY=...                 # koi bhi free key
python -m clipper.bridge --port 8787    # bridge chalu (naya terminal)

export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_AUTH_TOKEN=local
export ANTHROPIC_MODEL=llama-3.3-70b-versatile
claude                                   # ab Claude Code free pool par chal raha hai
```

---

## 4. Local par chalana

Zaroori cheezein: **Python 3.10+**, **ffmpeg** (libass ke sath), aur ek LLM key.

```bash
# ffmpeg
sudo apt-get install -y ffmpeg fonts-dejavu-core     # Ubuntu/Debian
brew install ffmpeg                                  # macOS

# is repo mein
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-whisper.txt     # whisper optional hai
export GROQ_API_KEY=...

python -m clipper --doctor                                  # environment check
python -m clipper --url "https://youtu.be/VIDEO_ID" --clips 3
```

Sab kuch `out/` mein aata hai:

```
out/
├── index.html                       # sab videos ki list
├── report.md                        # kaun sa clip, kitne second, score, hook
├── manifest.json                    # machine-readable
└── <video-slug>/
    ├── index.html                   # gallery + preview + download links
    ├── clip-01-<hook>.mp4           # 1080×1920, captions burned in
    ├── clip-01-<hook>.jpg           # thumbnail
    ├── clip-01-<hook>.ass           # caption file (edit karke dobara render kar sakte hain)
    ├── clips.json                   # LLM ne kya chuna aur kyun
    ├── transcript.json              # word-level timings
    └── info.json                    # video metadata
```

### CLI flags (kaam ke)

```
--url URL                 ek ya zyada link (repeat karein)
--clips N                 per video clips (default 3)
--min-duration / --max-duration
--caption-style karaoke|plain|none
--caption-highlight "#FFE100"     highlight colour
--caption-font "DejaVu Sans"
--words-per-caption 3
--no-hook-titles          upar wala hook title band karo
--fit crop|blur|pad
--fps 30 --crf 20 --preset veryfast
--llm-backend auto|claude-code|api|heuristic
--language en|hi|ur|…
--whisper-model base
--cookies cookies.txt     YouTube "not a bot" ke liye
--dry-run                 sirf plan + .ass + ffmpeg commands, encoding nahi
--doctor                  environment check
```

Poori list: `python -m clipper --help`

---

## 5. Troubleshooting (chhoti list)

| Problem | Hal |
|---|---|
| `Sign in to confirm you're not a bot` | Browser se `cookies.txt` export karein (extension: "Get cookies.txt LOCALLY") aur repo secret `YT_COOKIES` mein base64 ya raw text daal dein. Details: [docs/SETUP.md](docs/SETUP.md#youtube-cookies) |
| `no usable subtitles` | `requirements-whisper.txt` install ho, ya `whisper_model` = `small` |
| Captions gayab / boxes | Runner par fonts nahi. `fonts-dejavu-core` install karein, ya `--caption-font` badlein |
| Clips bane hi nahi | Log mein `brain:` line dekhein — key missing ho to `heuristic` chalta hai; `GROQ_API_KEY` check karein |
| LLM ne ghalat timings diye | `--llm-backend api` + `LLM_MODEL` mein bara model; ya `claude-code` |
| Actions minutes khatam | Private repo par minutes count hote hain; public repo par free. Ek 10-min video ≈ 8–15 min runner time |
| Clip lambai ajeeb | `min_duration` / `max_duration` set karein; clip boundaries word-timings par snap hoti hain |

Zyada: [docs/SETUP.md](docs/SETUP.md) aur [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## 6. Limits, cost, aur ek zaroori warning

- **Free LLM tiers** par rate limits hoti hain. Bara video + kam quota = slow run ya fallback
  (heuristic). `llm_backend: api` sabse sasta hai, `claude-code` sabse "smart" (agent file
  parh kar khud decide karta hai).
- **GitHub Actions**: har run mein ffmpeg + whisper model download hota hai (cache ke sath
  tez). 120 minute ka timeout set hai.
- **Copyright / YouTube ToS**: sirf wo content clip karein jiska aap ko haq hai (apna channel,
  client ka kaam, licensed material). YouTube downloads bot-check de sakta hai; cookies
  apne hi account ki use karein. Ye tool kisi ki copyright ki khilaf-warzi ka license nahi deta.

---

## 7. Development

```bash
pip install -r requirements-dev.txt
ruff check clipper tests
pytest -q                     # 100+ tests, ffmpeg integration included
```

Claude Code ka end-to-end test (`tests/test_claude_code.py`) asli `claude` binary ke sath ek
mock LLM par chalta hai — `npm i -g @anthropic-ai/claude-code` ke baad khud chal jata hai,
warna skip ho jata hai.

Structure:

```
clipper/
├── cli.py          command line + --doctor
├── config.py       settings (env + flags)
├── youtube.py      yt-dlp download, cookies, audio extract
├── subtitles.py    WebVTT/SRT parsing, rolling-caption dedupe, transcript model
├── transcribe.py   faster-whisper fallback
├── moments.py      viral-moment selection + validation/ranking + heuristic
├── prompts.py      the editor prompts
├── claude_code.py  headless Claude Code director
├── bridge.py       Anthropic ⇄ OpenAI translation server
├── captions.py     styled ASS captions with per-word karaoke
├── render.py       ffmpeg reframe/caption/loudnorm/encode
└── pipeline.py     orchestration, galleries, manifest, run summary
```

MIT-style "use at your own risk" — see `LICENSE`.
