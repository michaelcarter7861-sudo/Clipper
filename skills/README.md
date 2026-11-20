# Vendored skills (upstream code, unmodified)

Yahan do upstream projects ka code as-is rakha gaya hai, taake is repo ke saath hi
istemal ho sake. Dono MIT hain; LICENSE files sath hain.

| Folder | Upstream | Commit | Kya karta hai |
|---|---|---|---|
| `gta6-clipper/` | [vossashtonai/gta6-clipper](https://github.com/vossashtonai/gta6-clipper) | `11cee2b` | Stream (YouTube/Twitch/Kick) → 1080x1920 clips: facecam upar, gameplay neeche, hook title, word-by-word captions, post caption + hashtags |
| `gta6-live-clipper/` | same repo | `11cee2b` | Chalte hue stream se 1-minute pieces record karke ~5–8 min baad clip |
| `video-editing-skill/` | [social-media-skills/video-editing-skill](https://github.com/social-media-skills/video-editing-skill) | `f94d051` | Talking-head video ki pehli editing: pauses, "um"/"uh", stutter, retake, punch-ins, audio clean, SRT + chapters |

Kuch bade example files (preview mp4, showcase png) chhod diye gaye hain — code par koi asar nahi.

## Ye kya hai, aur kya nahi

Ye **Claude Code / Codex / Cursor skills** hain: markdown hidayat + Python scripts
(ffmpeg, whisper-cpp, yt-dlp, OpenCV, Pillow). Inhe chalane ka tareeqa:

```bash
# gta6-clipper (Mac) — Homebrew, whisper-cpp, deno, venv
bash skills/gta6-clipper/scripts/setup.sh
```

Masle jo pehle hi maloom hain:

1. **Mac-only setup.** `setup.sh` Homebrew + `whisper-cli` maangta hai, Linux runner par
   nahi chalta. `video-editing-skill` Linux par chalta hai (sirf ffmpeg + Pillow).
2. **Claude Code session lazmi.** gta6-clipper mein "picking" (step 3) khud agent karta hai —
   koi API key nahi. Bina agent ke sirf `prep` aur `render` chalte hain, picks koi nahi likhta.
3. **YouTube IP block.** GitHub runner ka datacenter IP hai; yahan wala `prep` wahi block
   hit karega jo `clipper/` hit karta hai (dekhein: run `44710034897`). Direct file ya
   residential `YT_PROXY` par hi chalega.

## Jo cheezein yahan se port karni hain (better than our current pipeline)

| Idea | Upstream | Humare paas ab | Faida |
|---|---|---|---|
| Sirf audio download (`bestaudio`) analysis ke liye | `prep_one` | poora video download | 4 ghante ka stream ≈ 220 MB |
| Loudness markers (`<<LOUD(+12dB)>>`) | `loud_moments` | sirf transcript | moment-picking ko asli signal |
| YouTube replay chat speed (`<<CHAT x4.1>>`) | `chat_spikes` | kuch nahi | hype ka sab se pukhta signal |
| 12-minute transcript chunks | `prep_one` | ek hi bara prompt | LLM har chunk 12 min par |
| Facecam box detect + upar/neeche stack | `find_facecam` + `render_one` | crop reframe | stream clips asli lagte hain |
| Hook title card + word captions | `caption_frames` | karaoke captions | hook pehle 3 second mein |
| `.txt` post caption + hashtags | `render_one` | report | post karne ke liye tayyar |
| Silence / filler / retake cutting, punch-ins | `edit_video.py` | kuch nahi | talking-head videos ke liye naya mode |

Ye table hi port ka plan hai — jo chalna shuru ho, wohi pehle.
