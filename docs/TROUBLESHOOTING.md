# When a run makes no clips

A clip run that produces nothing is a failure, not a success with an empty folder. Since
2026-11-20 the workflow enforces that: `Cut clips` runs with `pipefail`, and `Run summary`
fails the job when `out/` holds no `clip-*.mp4`. If a run is red, work down this list.

## 1. Read the reason, not the artifact

Three places carry the answer, all readable on a phone:

| Where | What is in it |
|---|---|
| The run summary page | clips produced, formats YouTube listed, and the last errors under *Why it failed* |
| `diagnostics/clips-<run>.txt` | set `save_report` on the run: `out/` file listing with sizes, ffmpeg version, last 150 log lines, committed back to the repo |
| The Release body | set `publish_release` on the run: clip files attached **and** the same listing plus the last 40 log lines in the notes |

`save_report` and `publish_release` are the two "prove it" switches; turn both on when you are
debugging a link.

## 2. The most common causes

| Symptom in the report | Cause | Fix |
|---|---|---|
| `HTTP Error 403: Forbidden` on a `commondatastorage.googleapis.com` / sample link | the host stopped serving the runner's IP; nothing to do with Clipper | use the real video link, or the bundled `samples/facecam-sample.mp4` |
| `Sign in to confirm you're not a bot` | YouTube is refusing the runner's datacenter IP. Measured 2026-11-20: 8 yt-dlp clients, the obscura browser, the PO-token provider plugin, and 20+ public Invidious/Piped instances all fail; Piped's media URLs are signed for the *instance's* IP, so they 401 here | give a **direct file URL** (Release asset, Dropbox `?dl=1`), or `YT_COOKIES`, or a residential `YT_PROXY` |
| `ffmpeg could not extract audio` on a video with no audio track | the source really has no audio | nothing to do — since 2026-11-20 such a video is clipped with no captions instead of failing |
| `no clips passed validation` | the video is shorter than `min_duration`, or the captions are empty | lower `min_duration`, or let Whisper transcribe |
| `ffmpeg produced no output` | the filter chain failed on this source | read the 600-character ffmpeg message in the log |
| `out/` listing shows a `.txt` next to no `.mp4` | that clip threw during render | the log line `clip N failed: …` has the reason |

Two of these bugs cost a day of false-green runs, and both are now fixed in the workflow:
`tee` was masking the pipeline's exit code (fixed with `set -o pipefail`), and the release step
skipped silently with zero clips (fixed: the job now fails first).

## 3. Prove the pipeline in one click

```text
urls:   samples/facecam-sample.mp4
fit:    facecam
clips:  2
publish_release: true
```

That link is a local file inside the repo: no download, no YouTube, no proxy. A green run
produces a ~4.7 MB `.mp4`, a `.jpg`, a `.txt` post, `signals.json`, and a Release with the
clip attached. If this fails, the problem is the runner or the renderer — not the video.
