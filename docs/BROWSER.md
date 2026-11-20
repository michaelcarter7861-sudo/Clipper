# The browser fallback (obscura)

Clipper's research step prefers `yt-dlp`. When YouTube refuses it, Clipper can ask a real
browser instead: [obscura](https://github.com/h4ckf0r0day/obscura), a headless engine in Rust
(Apache-2.0) that runs V8, speaks CDP, and keeps an HttpOnly cookie jar.

```bash
# install (Linux x86_64); the workflow does this for you
curl -LO https://github.com/h4ckf0r0day/obscura/releases/latest/download/obscura-x86_64-linux-no-render-stealth.tar.gz
tar xzf obscura-x86_64-linux-no-render-stealth.tar.gz && sudo mv obscura /usr/local/bin/

python -m clipper --check-youtube          # prints youtube: PASS/FAIL and browser: PASS/FAIL
```

## What it is used for

| Use | How |
|---|---|
| Automatic cookies | `obscura fetch <url> --dump cookies` → Netscape `cookies.txt` → yt-dlp retry |
| Second opinion | `--eval 'JSON.stringify(window.ytInitialPlayerResponse)'`, raw HTML fallback |
| Direct download | stream URLs from the player response, muxed with ffmpeg when separate |

Files: `out/<slug>/browser.json` (what the browser saw) and `browser-cookies.txt` in the work
dir. Nothing is enabled implicitly: `CLIP_BROWSER` / `--browser` selects the binary,
`CLIP_BROWSER_STEALTH` / `--browser-stealth` is **off** by default.

## Measurement (GitHub runner, obscura 0.2.7, 2026-11-20)

Link: `https://youtu.be/DENbjnmSems`, runner IP `172.185.55.187`, no proxy.

| Step | Result |
|---|---|
| `obscura --version` | `obscura 0.2.7` |
| watch page with JS (`--eval`) | engine **aborts**: SIGABRT / core dumped after `webcomponents-sd.js` TypeError |
| watch page raw (`--dump original`) | player response parsed, `playability=LOGIN_REQUIRED`, 0 streams |
| reason | *"Sign in to confirm you're not a bot"* |
| cookies harvested | 9 anonymous visitor cookies |
| yt-dlp on the same runner | same bot-check |

**Conclusion:** a real browser does not remove the need for cookies or a proxy. The refusal
follows the runner's datacenter IP, and it is identical for every client we tried (see the
3-OS table in the README). The code stays, because obscura may grow past YouTube's JS and it
is genuinely useful for other sites — but the working answers remain a residential `YT_PROXY`
or a direct video file/link.
