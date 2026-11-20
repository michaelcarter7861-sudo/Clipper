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
   ├─ edit              jump cuts, speed, punch-in, grade, progress bar, watermark
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
| `editing` | `full` | `full` (jump cuts + grade + punch-in + progress bar) · `cuts` (sirf jump cuts) · **`talking`** (talking head: sakht pause/filler/retake cuts + alternate punch-ins) · `off` (raw) |
| `watermark` | *khali* | corner mein chhota handle/text, e.g. `@yourchannel` |
| `max_height` | `1080` | sab se oonchi source resolution jo download ho (720 se tez run) |
| `fit` | `crop` | `crop` (center crop) · `blur` (blurred background) · `pad` (black bars) · **`facecam`** (streamer ka cam upar, gameplay neeche) · `auto` (facecam mile to wohi, warna crop) |
| `language` | `auto` | `en`, `hi`, `ur`, … subtitles/whisper ke liye |
| `whisper_model` | `base` | jab video par subtitles na hon |
| `llm_backend` | `auto` | `auto` · `claude-code` · `api` · `heuristic` |
| `audio_only` | `false` | pehle sirf audio utaar kar transcribe/sunlo, phir sirf wohi hisse download karo jo clips bane — lambi stream par bohat tez (video section ka kam bhi ek section, replace bhi ek section) |
| `chat_heat` | `true` | live-chat replay ho to us ke bursts ko signal banao |
| `publish_release` | `false` | clips ko GitHub Release mein daal do |

**Formats ka masla khud hal ho jata hai:** har run pehle YouTube se poochta hai ke kaunse
formats maujood hain (log mein table + `out/<slug>/info.json` mein `formats`), phir **sab se
behtar available** format download karta hai. Agar woh na mile to khud `best` aur phir `b`
try karta hai — is liye `Requested format is not available` par run nahi girta. Live streams
(`/live/...`) par shuru se download hota hai, jis ka log mein pehle se warning aata hai.

Agar runner ka IP YouTube ko pasand na aaye (bot-check ya block), do options hain — dono
secrets hain: `YT_COOKIES` (aap ka session) aur `YT_PROXY` (e.g. `http://user:pass@host:port`).
`YTDLP_EXTRA_ARGS` (repository **variable**, yt-dlp flags) bhi support hai, misaal:
`--cookies-from-browser chrome`.

`llm_backend` ka matlab:

- **`auto`** — Claude Code agar install ho aur key mile, warna seedha API call, warna heuristic.
- **`claude-code`** — sirf Claude Code (headless `claude -p`) se moments chunwao.
- **`api`** — Claude Code skip, seedha ek chat-completion call (sasta aur tez).
- **`heuristic`** — koi LLM nahi; hook-words + speech pace se clips. Key ki zaroorat nahi,
  quality kam.

---

## 3. Editing (jo clips ko post-worthy banati hai)

`editing: full` (default) har clip par ye karta hai:

| Step | Kya hota hai | Flag |
|---|---|---|
| **Jump cuts** | 0.45s se lambi khamoshi/rukawat nikal jati hai; speech ke aas paas 0.1s handle rehta hai | `--max-pause`, `--no-tighten` |
| **Filler cut** | `um`, `uh`, `matlab`, `yaani` jaise words kaat deta hai | `--remove-fillers` |
| **Speed** | halka fast-forward (pacing ke liye) | `--speed 1.08` |
| **Punch-in** | 4% zoom — frame composition badal jata hai, screen recording jaisa flat look nahi rehta | `--punch-in 1.06` |
| **Grade** | `clean` / `punchy` / `cinematic` colour preset (contrast, saturation, vignette) | `--look punchy` |
| **Progress bar** | neeche patli si retention bar | `--no-progress-bar` |
| **Watermark** | corner mein aap ka handle, semi-transparent | `--watermark "@yourchannel"` |
| **Captions + hook** | word-by-word karaoke captions aur upar hook title | `--caption-style` |

Cuts ke baad captions automatically re-time ho jati hain (`edit.retime_transcript`), is liye
audio aur text kabhi out of sync nahi hote. Run ke report mein har clip ke saath likha hota
hai: `1 jump cut(s), -4.5s dead air, 1.08× speed, punchy grade, progress bar`.

```bash
python -m clipper --url "https://youtu.be/ID" --editing full --watermark "@yourchannel"
python -m clipper --url "https://youtu.be/ID" --editing cuts      # sirf pacing
python -m clipper --url "https://youtu.be/ID" --editing talking   # podcast / talking head
python -m clipper --url "https://youtu.be/ID" --editing off       # as-shot
```

### `editing: talking` — podcast / talking head

Baat karne wale video ka 30-40% hissa khamoshi, "um", aur wohi line dobara bolna hota hai:

| Kya | Default `full` | `talking` |
|---|---|---|
| Pause cut | 0.45s se lambi | **0.30s** se lambi (pre/post roll 0.05/0.12s) |
| Fillers (`um`, `matlab`, `yaani`) | band | **on** |
| **Retake** (same line dobara, behtar) | — | pehla adhoora take **cut**, doosra rakha jata hai |
| Punch-in | pooray clip par 4% | **har doosre cut par 10%** — cut ke sath frame badalta hai |
| Grade | clean | clean |

Log aur report mein saaf likha aata hai, e.g. `2 jump cut(s), -6.4s dead air, 1 retake(s) removed,
110% punch-in, clean grade, progress bar`. Retake detection sirf tab chalti hai jab wohi 5 lafz
45s ke andar dobara bole jayein — warna kuch nahi kat-ta (`--no-retakes` se band).

### Chat heat — `<<CHAT x14>>`

Live stream ki chat replay ek muft, insaani signal hai: log us waqt type karte hain jab kuch
*ho* raha ho. `chat_heat: true` par Clipper yt-dlp se `live_chat` replay maangta hai (jo video
mein ho), 5-second buckets mein ginti karta hai, aur transcript ki un lines par `<<CHAT x14>>`
marker lagata hai jo us video ki normal chatter se dugni hain. Markers sirf evidence hain —
LLM unhein dekh kar faisla karta hai, output mein woh kabhi nahi aate. `out/<slug>/chat.json`
mein bursts mehfooz rehte hain.

### Audio-only analysis — `audio_only: true`

Lambi stream par 2 ghante ka video download karna sab se mehnga kaam hai. `audio_only` par
Clipper pehle sirf audio utaarta hai (m4a), usi se transcript + loudness + chat signals banata
hai, clips choose karta hai, aur **phir har clip ka sirf apna hissa** (`download_ranges`, keyframe
par cut) download kar ke render karta hai. Agar section download kisi wajah se fail ho jaye to
ek dafa poora video utaar kar wohi clip bana di jati hai — run kabhi khaali nahi jaata.

### Editing aur copyright — seedhi baat

Editing quality barha deti hai, lekin **copyright ka legal masla khatam nahi karti**:

- YouTube ka Content ID audio aur video dono match karta hai; jump cuts/zoom se match *kam*
  ho sakta hai, magar claim/block ka risk zero nahi hota.
- "Transformative use" / fair use kuch mulkon mein ek **legal defence** hai — jo court case
  ki soorat mein tay hoti hai. Koi bhi tool ("is level ki editing karo to issue nahi aayega")
  is ki guarantee nahi de sakta, aur main aisi guarantee nahi de raha.
- Safe tareeqa: **apna content**, client ka licensed content, ya likhit permission wala content.
  Kisi aur ka content use karna ho to: chhota hissa, apni commentary/analysis ke sath, credit
  aur source link, aur platform ke rules follow karein.
- Monetised/policy-strike risk aap ke account par aata hai, is liye pehle 1-2 clips test karein.

## 4. Claude Code + free LLM API kaise kaam karta hai

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

## 5. Local par chalana

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
--fit crop|blur|pad|facecam|auto
--fps 30 --crf 20 --preset veryfast
--llm-backend auto|claude-code|api|heuristic
--language en|hi|ur|…
--whisper-model base
--cookies cookies.txt     YouTube "not a bot" ke liye
--browser obscura         yt-dlp refuse kare to asli browser ('' = band)
--browser-stealth         browser ko fingerprint spoofing ke sath chalao (default off)
--editing full|cuts|talking|off   jump cuts + grade + progress bar (default full)
--retakes / --no-retakes  talking head: pehla adhoora take cut karo
--punch-alternate         har doosre cut par punch-in
--audio-only              pehle audio, phir sirf chosen windows download
--chat chat.json          live_chat replay file (khud dein)
--no-chat-fetch           chat replay download na karo
--chunk-span 720          LLM ko ek prompt mein kitne second transcript (default 12 min)
--max-pause 0.45          is se lambi khamoshi cut ho jati hai
--remove-fillers          um/uh/matlab/yaani bhi cut karo
--speed 1.08              halka fast-forward
--punch-in 1.06           4-6% zoom (naya framing)
--look clean|punchy|cinematic
--watermark "@yourchannel"
--dry-run                 sirf plan + .ass + ffmpeg commands, encoding nahi
--doctor                  environment check
```

Poori list: `python -m clipper --help`

---

## 6. Troubleshooting (chhoti list)

| Problem | Hal |
|---|---|
| `Sign in to confirm you're not a bot` | Browser se `cookies.txt` export karein aur `YT_COOKIES` secret mein daal dein (raw ya base64). Check karne ke liye: `python -m clipper.cookies --check cookies.txt --test`. Details: [docs/SETUP.md](docs/SETUP.md#youtube-cookies) |
| `Requested format is not available` | Ab khud handle hota hai: default client → `android` → `tv` → `web_safari` → `ios` → `mweb` → `best` → `b`, aur cookies ke sath ek cookie-free try bhi |
| `YouTube is blocking this machine's IP` (naya message) | Runner ka IP YouTube ke liye flagged hai — link ka masla nahi. Neeche "Runner ka IP block ho to" parhein |
| `youtube test: PASS` par phir bhi formats nahi | Runner par **JS runtime** (deno ya node) chahiye — YouTube ki signature challenge ke liye. Workflow khud `deno` install karta hai (fail ho to node se kaam chalta hai). Local par: `npm i -g deno` ya node install karein; check: `python -m clipper --doctor` ki `yt-dlp js runtime` line |
| `Formats it listed: none` / "storyboard images only" | YouTube ne sirf thumbnails diye, asli stream nahi. Yani is runner ke IP/cookie session par **proof-of-origin (PO token)** check lag raha hai. Hal: `YT_PROXY` (residential proxy) set karein, ya `YT_COOKIES` naye browser session se dobara export karein. Artifact mein `out/formats.txt` dekhein — har koshish ki asli wajah wahan likhi hoti hai |
| Live stream (`/live/...`) | Live par formats badalte rehte hain. Stream khatam hone ke baad `https://www.youtube.com/watch?v=ID` se chalayein |
| `YouTube did not offer a format` (report mein) | Woh video sirf aise format de raha hai jo download nahi ho sakte (kabhi live/SABR). Log ki formats table bhejein — proxy ya doosra link try karein |
| Formats dekhne hain (local) | `python -m clipper --list-formats --url "link"` |
| `no usable subtitles` | `requirements-whisper.txt` install ho, ya `whisper_model` = `small` |
| Captions gayab / boxes | Runner par fonts nahi. `fonts-dejavu-core` install karein, ya `--caption-font` badlein |
| Clips bane hi nahi | Log mein `brain:` line dekhein — key missing ho to `heuristic` chalta hai; `GROQ_API_KEY` check karein |
| LLM ne ghalat timings diye | `--llm-backend api` + `LLM_MODEL` mein bara model; ya `claude-code` |
| Actions minutes khatam | Private repo par minutes count hote hain; public repo par free. Ek 10-min video ≈ 8–15 min runner time |
| Clip lambai ajeeb | `min_duration` / `max_duration` set karein; clip boundaries word-timings par snap hoti hain |

### Diagnose a link (andaza nahi, pukhta test)

Actions tab → **Clipper · diagnose a link** → *Run workflow* → link paste karein. Ye workflow
teen runner networks (Ubuntu, Windows, macOS) par yt-dlp se poochta hai ke YouTube kya deta hai —
cookies ke sath aur bina. Natija run page ke summary mein aur `diagnostics-<os>` artifact mein
milta hai.

Aik dafa ka naap (is repo par, aap ke `YT_COOKIES` ke sath):

| Runner | IP | Cookies ke sath | Bina cookies |
|---|---|---|---|
| Ubuntu | 130.131.238.22 | sirf storyboards | bot-check |
| Windows | 20.80.78.224 | sirf storyboards | bot-check |
| macOS | 13.105.117.159 | sirf storyboards | bot-check |

Yani GitHub-hosted runners par **har OS aur har IP** par wohi jawab aata hai: cookies se session
qabool hota hai magar stream nahi milta, aur bina cookies bot-check. Ye **code se hal nahi hota** —
runner ka IP hi masla hai.

### Facecam layout — stream clips ko asli look (`fit: facecam`)

Twitch/Kick/YouTube streams par streamer ka chehra ek chhote dabbe mein hota hai. `fit: facecam`
(clips workflow ka dropdown) us dabbe ko dhoondta hai aur clip banata hai: **chehra upar,
gameplay neeche, 1080x1920**, hook title seam ke neeche, captions gameplay panel par.

![facecam layout](docs/images/facecam-layout.png)

Kaam kaise karta hai (GTA 6 Clipper skill se liya gaya algorithm):

1. Clip ki window se 12 frames nikalta hai (`ffmpeg`).
2. Har frame mein chehra dhoondta hai (OpenCV Haar cascade) aur woh chehra rakhta hai jo
   **ek hi jagah** bar bar mila — yahi facecam ko gameplay wale chehre se alag karta hai.
3. Us chehre ke charon taraf static seedhi lines scan karke overlay ka box nikalta hai.
4. Gameplay ka crop overlay se **door** shift kar deta hai, warna chehra do dafa dikhta.

Jaise bhi kuch ghalat ho (OpenCV na ho, chehra na mile), render **crop** par gir jata hai —
run fail nahi hota. Log mein `clip 1: no facecam answer (…)` ya `facecam 155x121 at 18,202`
likha aata hai, aur `clips.json` mein har clip ka `layout`.

Setup: `pip install -r requirements-facecam.txt` (OpenCV 4.x — 5.x ne Haar detector hata diya
hai, is liye pin kiya hua hai). Workflow khud install karta hai jab `fit` facecam/auto ho.

Bina YouTube ke test karna ho to repo mein sample hai:

```
fit  = facecam
urls = samples/facecam-sample.mp4
```

(Repo ke andar ka path seedha chal jata hai — `samples/facecam-sample.vtt` us ka transcript hai,
is liye is sample par Whisper ki bhi zaroorat nahi.)

### Browser fallback — jab yt-dlp ko mana kar diya jaye (obscura)

Runner par yt-dlp ko "Sign in to confirm you're not a bot" milta hai to Clipper ab ek **asli
browser** se bhi poochta hai: [obscura](https://github.com/h4ckf0r0day/obscura) (Rust headless
engine, Apache-2.0, CDP + V8, HttpOnly cookie jar).

```
Actions → Clipper · YouTube → viral clips → browser = obscura   (default)
baat karni ho to:  browser = off
```

Do koshish, sasti pehle:

1. browser ki apni cookies → `cookies.txt` → yt-dlp dobara (phone se cookies export karne ki
   zaroorat khatam)
2. browser ke player response ki stream URLs → seedha download, alag video/audio ho to ffmpeg se mux

**Naap (GitHub runner, obscura 0.2.7, 2026-11-20, `youtu.be/DENbjnmSems`):**

| Kya | Natija |
|---|---|
| install + `--version` | OK — `obscura 0.2.7` |
| watch page, JS path | engine **abort** (SIGABRT, core dumped) — YouTube ka `webcomponents-sd.js` |
| watch page, raw (JS bypass) | `playability=LOGIN_REQUIRED` — *"Sign in to confirm you're not a bot"*, **0 stream** |
| cookies jo browser ne diye | 9 (anonymous visitor cookies, login nahi) |

Yani browser se **cookies ki zaroorat khatam nahi hoti** — block runner ke IP ka hai, client ka
nahi. Jo cheez ab bhi kaam karti hai: `YT_PROXY` (residential) ya direct file/link. Browser
fallback code mein rehta hai (aage obscura ke naye version, ya doosri sites ke liye), aur
`--check-youtube` us ka status bhi batata hai.

### Loud moments — picker ko asli signal

Har run ek ffmpeg pass mein per-second loudness naapta hai aur transcript ki un lines par
`<<LOUD(+12dB)>>` marker lagata hai jo streamer ke apne normal se 9 dB upar hain. LLM ko yeh
markers evidence ke tor par milte hain (faisla nahi). `signals.json` mein raw events, aur band
karna ho to `CLIP_SIGNALS=0`.

### Post file — har clip ke sath `.txt`

Hare clip ke sath likha jata hai: `TITLE`, `HOOK`, `CAPTION`, hashtags, `SOURCE` aur `FROM/TO`
timestamps. LLM backend caption + hashtags deta hai (`caption`, `hashtags` uske JSON mein);
heuristic mode mein sirf title/hook/source.

### YouTube link, koi proxy nahi — 2026-11-20 ka naapa hua sach

Ek asli link (`youtu.be/DENbjnmSems`) par GitHub runner se ye sab try kiya gaya, aur sab
ek hi jagah rukte hain: YouTube runner ke datacenter IP ko "Sign in to confirm you're not a
bot" kehta hai.

| Koshish | Natija |
|---|---|
| yt-dlp ke 8 clients (default, android, tv, ios, mweb, web_safari, best, best single file) | `Sign in to confirm you're not a bot` |
| obscura browser (raw page + us ki cookies) | `LOGIN_REQUIRED`, 0 stream, 9 anonymous cookie |
| **PO token provider** (`bgutil-ytdlp-pot-provider` + docker provider, v2.0.2) | wahi bot check — token se ye gate nahi khulta |
| 20+ public Invidious / Piped instances (python **aur** curl, API + media URL) | 403/Cloudflare/dead; ek instance (`pipedapi.ducks.party`) API deta hai magar us ke media URLs hamare IP par 401/404, kyunki googlevideo URL **instance ke IP** ke liye sign hota hai |
| cobalt instances | auth maangte hain ya Cloudflare 502 |

Yani **bina cookies ya residential proxy ke YouTube link se clips nahi banenge** — ye Clipper
ka bug nahi, YouTube ka IP gate hai. Aage jo cheez **aaj kaam karti hai**:

### Jo chal raha hai: file link (no proxy, no cookies)

Koi bhi URL jo seedha media file deta hai — GitHub Release asset, Dropbox `?dl=1`, S3,
apna server. Clipper aise link ko yt-dlp ke paas bhejta hi nahi, khud bytes utaarta hai
(`_direct_file_fallback`), phir wohi pipeline chalati hai.

```text
urls: https://github.com/<you>/<repo>/releases/download/clips-.../my-video.mp4
```

Proof (aaj ka run): [`44742950408`](https://github.com/michaelcarter7861-sudo/Clipper/actions/runs/44742950408)
— green, Release `clips-20261120-131921` mein 4.7 MB clip. Ye run ek Release asset se khud
clip banata hai.

### Cookies — YouTube link ka wo raasta jo chalta hai

`docs/SETUP.md` → *YouTube cookies*: `cookies.txt` banayein (extension, 30 second), base64
karein, `YT_COOKIES` secret mein paste. Clipper khud check karta hai ke YouTube us session ko
qubool karta hai ya nahi. (Is sandbox ke paas kisi ka account nahi, is liye cookies ka raasta
yahan *measured* nahi — code path mojood hai, test aap ke account se hoga.)

### Jab run kuch na banaye — "prove it" switches

Zero clips ka matlab failure hai, kamyabi nahi: `Run summary` ab job ko red kar deta hai.

- `publish_release` on → clips GitHub Release mein attach (phone se seedha download), plus
  Release notes mein `out/` ki listing aur log ki last 40 lines.
- `save_report` on → wahi listing + log ka tail `diagnostics/clips-<run>.txt` mein commit ho
  jata hai, run ke baad repo mein parh lein.
- Ek asli proof: `urls: samples/facecam-sample.mp4` (repo ke andar ka local file) → 4.7 MB
  clip, `fit: facecam`. Details: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

### Runner ka IP block ho to (GitHub Actions ka sab se bara masla)

GitHub runners ke IP **datacenter** ke hote hain, aur YouTube unhein aksar sirf thumbnails deta hai.
Pehchan: run ke summary mein **"YouTube access: BLOCKED"** ya error mein *"blocking this machine's IP"*.

Teen raste (pehla sab se pakka):

1. **Residential proxy** — secret `YT_PROXY` = `http://user:pass@host:port` (koi bhi residential
   proxy service; datacenter proxy se faida nahi hota). Phir workflow dobara chalayein.
2. **Direct file link** — YouTube se video nikalne ke bajaye file hi de dein:
   - kisi bhi tarah video file hasil karein (apna upload, client ka kaam, licensed footage),
   - usay Dropbox par daal ke link ka `?dl=0` → `?dl=1` kar dein,
   - wohi link `urls` input mein paste karein. Pipeline direct file par YouTube ko touch hi
     nahi karti (thumbnails ki list banane ki zaroorat nahi), sirf transcript + clips banati hai.
3. **Local / Colab** — jahan IP residential (ya Google ka) ho:
   ```bash
   python -m clipper --url "/path/video.mp4" --editing full --count 3
   ```
   Local file paths aur `file://` links bhi chalte hain. Phone se Colab:
   `!git clone https://github.com/michaelcarter7861-sudo/Clipper && !pip install -r Clipper/requirements.txt`
   phir `!cd Clipper && python -m clipper --url "LINK"`.

Quick test: `python -m clipper --check-youtube` → `PASS` matlab is machine se YouTube chal raha hai,
`FAIL` matlab IP hi block hai (proxy chahiye).

Zyada: [docs/SETUP.md](docs/SETUP.md) aur [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## 7. Limits, cost, aur ek zaroori warning

- **Free LLM tiers** par rate limits hoti hain. Bara video + kam quota = slow run ya fallback
  (heuristic). `llm_backend: api` sabse sasta hai, `claude-code` sabse "smart" (agent file
  parh kar khud decide karta hai).
- **GitHub Actions**: har run mein ffmpeg + whisper model download hota hai (cache ke sath
  tez). 120 minute ka timeout set hai.
- **Copyright / YouTube ToS**: sirf wo content clip karein jiska aap ko haq hai (apna channel,
  client ka kaam, licensed material). Editing copyright se bachne ka license nahi deti —
  section 3 ki "seedhi baat" parh lein. YouTube bot-check ke liye sirf **apne** account ki
  cookies use karein.

### Ye tool kya nahi karta (jaan boojh kar)

- Koi "sab bypass karne wala" tool/repo integrate nahi kiya jata — na DRM/paid-content
  bypass, na aisi cheez jo YouTube ki Terms of Service ke khilaf ho. Is tarah ke tools
  account ban aur legal risk banate hain, aur GitHub par milne wale aise "bypass" repos
  mein malware bhi aam hai.
- Cookies sirf **apne** browser session ko reuse karti hain (wahi cheez jo aap manually
  karte ho), aur wahi YouTube ka supported tareeqa hai. `python -m clipper.cookies` us file
  ko check aur test karta hai — bypass nahi karta.
- Copyright match ko chhupane ke liye "obfuscation" (speed/pitch/zoom tricks ka misuse) —
  aisa kuch bhi nahi. Editing sirf quality aur pacing ke liye hai.

---

## 8. Development

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
