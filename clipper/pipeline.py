"""End-to-end orchestration: URL in, captioned vertical clips out."""

from __future__ import annotations

import html
import json
import os
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .bridge import Bridge, start_bridge
from .chat import ChatEvent, bursts, load_chat
from .chat import summarise as summarise_chat
from .claude_code import is_available as claude_available
from .claude_code import run_director
from .config import Settings
from .llm import ChatLLM, Provider, resolve_provider, summarise_provider
from .log import get_logger, notice, step, summary, warning
from .moments import ClipPlan, heuristic_clips, plan_clips
from .render import RenderResult, doctor, render_clip
from .signals import LoudEvent, loud_moments
from .signals import summarise as summarise_signals
from .subtitles import Transcript, parse_vtt
from .transcribe import transcribe, whisper_available
from .youtube import (
    VideoSource,
    download,
    download_audio,
    download_chat,
    download_section,
    local_media_path,
    slugify,
)

log = get_logger()


class PipelineError(RuntimeError):
    pass


@dataclass
class ClipOutput:
    plan: ClipPlan
    path: Path | None
    thumbnail: Path | None
    ass: Path | None
    output_duration: float = 0.0
    editing: str = ""
    edit: dict | None = None
    layout: str = ""

    def to_dict(self) -> dict:
        payload = self.plan.to_dict()
        payload["file"] = self.path.name if self.path else None
        payload["thumbnail"] = self.thumbnail.name if self.thumbnail else None
        payload["output_duration"] = round(self.output_duration, 2)
        payload["editing"] = self.editing
        payload["layout"] = self.layout
        if self.edit:
            payload["edit"] = self.edit
        return payload


@dataclass
class VideoResult:
    source: VideoSource
    transcript: Transcript
    clips: list[ClipOutput] = field(default_factory=list)
    method: str = "unknown"
    dir: Path | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "video": self.source.to_dict(),
            "method": self.method,
            "transcript_source": self.transcript.source,
            "language": self.transcript.language,
            "clips": [clip.to_dict() for clip in self.clips],
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# backend selection
# --------------------------------------------------------------------------- #
@dataclass
class Backend:
    kind: str  # claude-code | api | heuristic
    provider: Provider | None = None
    bridge: Bridge | None = None
    base_url: str | None = None
    token: str = "local"

    def describe(self) -> str:
        if self.kind == "claude-code":
            return f"Claude Code → {self.base_url} ({self.provider.model if self.provider else 'custom'})"
        if self.kind == "api":
            return f"direct API · {summarise_provider(self.provider)}"
        return "heuristic (no LLM)"


@contextmanager
def make_backend(settings: Settings) -> Iterator[Backend]:
    """Pick the best available brain and always clean up the bridge."""
    provider = resolve_provider(settings)
    want_claude = settings.llm_backend in ("auto", "claude-code")
    bridge: Bridge | None = None

    def finish(backend: Backend) -> Iterator[Backend]:
        try:
            yield backend
        finally:
            if bridge is not None:
                bridge.stop()

    if settings.llm_backend == "heuristic":
        yield Backend(kind="heuristic")
        return

    if want_claude and claude_available():
        explicit_base = (os.environ.get("ANTHROPIC_BASE_URL") or "").strip()
        token = (
            os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ANTHROPIC_API_KEY")
            or "local"
        ).strip()

        if explicit_base:
            yield Backend(
                kind="claude-code",
                provider=provider,
                base_url=explicit_base.rstrip("/"),
                token=token,
            )
            return

        if provider is not None:
            try:
                bridge = start_bridge(
                    provider, port=settings.bridge_port, token=token, timeout=900
                )
            except OSError as exc:
                warning(f"could not start the Claude bridge ({exc}); using the direct API path")
            else:
                yield from finish(
                    Backend(
                        kind="claude-code",
                        provider=provider,
                        bridge=bridge,
                        base_url=bridge.base_url,
                        token=token,
                    )
                )
                return

    if provider is not None:
        yield Backend(kind="api", provider=provider)
        return

    if settings.llm_backend in ("auto", "claude-code", "api"):
        warning(
            "no LLM key found (GROQ_API_KEY / GEMINI_API_KEY / OPENAI_BASE_URL+OPENAI_API_KEY "
            "/ ANTHROPIC_BASE_URL) — falling back to the heuristic scorer"
        )
    yield Backend(kind="heuristic")


# --------------------------------------------------------------------------- #
# transcript
# --------------------------------------------------------------------------- #
def _pick_subtitle(files: list[Path], language: str | None) -> Path | None:
    if not files:
        return None

    def rank(path: Path) -> tuple[int, int]:
        name = path.name.lower()
        preferred = 0
        if language and f".{language.lower()}" in name:
            preferred = 3
        elif ".en" in name:
            preferred = 2
        elif ".hi" in name or ".ur" in name:
            preferred = 1
        auto = 0 if "auto" in name or name.count(".") <= 2 else 1
        return (preferred, auto)

    ordered = sorted(files, key=rank, reverse=True)
    return ordered[0]


def get_transcript(
    source: VideoSource, work_dir: Path, settings: Settings
) -> Transcript:
    cache = work_dir / "transcript.json"
    if cache.exists():
        try:
            transcript = Transcript.load(cache)
            if not transcript.is_empty:
                log.info("reusing cached transcript (%s)", transcript.source)
                return transcript
        except (json.JSONDecodeError, KeyError, ValueError):
            log.debug("cached transcript unreadable, rebuilding")

    transcript: Transcript | None = None
    if settings.prefer_subtitles:
        subtitle = _pick_subtitle(source.subtitle_files, settings.language)
        if subtitle is not None:
            with step(f"Reading subtitles: {subtitle.name}"):
                raw = subtitle.read_text(encoding="utf-8", errors="replace")
                language = settings.language
                if not language:
                    suffix = subtitle.suffixes
                    if len(suffix) >= 2:
                        language = suffix[-2].lstrip(".").split("-")[0]
                candidate = parse_vtt(raw, language=language, source=f"subtitles:{subtitle.name}")
            if not candidate.is_empty:
                log.info(
                    "subtitles gave %d segments / %d words",
                    len(candidate.segments),
                    len(candidate.words()),
                )
                transcript = candidate

    if transcript is None:
        from .render import has_audio

        silent = source.path is not None and not has_audio(settings.ffprobe, source.path)
        if silent:
            # A video with no audio track cannot be transcribed — but it can still be
            # clipped. Do not fail a run over it; captions simply have nothing to say.
            warning("this video has no audio track — no transcript, clips are picked by pacing")
            transcript = Transcript(segments=[], source="silent", duration=source.duration or 0.0)
        elif not whisper_available():
            raise PipelineError(
                "this video has no usable subtitles and faster-whisper is not installed. "
                "Install requirements-whisper.txt (or pick a video with captions)."
            )
        else:
            with step("Transcribing audio (no usable subtitles)"):
                transcript = transcribe(source.path, settings, work_dir)

    if source.duration:
        transcript.duration = source.duration
    transcript.save(cache)
    return transcript


# --------------------------------------------------------------------------- #
# clip selection
# --------------------------------------------------------------------------- #
def choose_clips(
    transcript: Transcript,
    source: VideoSource,
    settings: Settings,
    backend: Backend,
    verbatim_dir: Path,
    loud: list[LoudEvent] | None = None,
    chat: list[ChatEvent] | None = None,
) -> tuple[list[ClipPlan], str, str]:
    """Return ``(clips, method, notes)``."""
    if backend.kind == "claude-code" and backend.base_url:
        with step("Claude Code is choosing the clips"):
            result = run_director(
                transcript,
                transcript_path=verbatim_dir / "transcript.json",
                video_path=source.path or Path("source.mp4"),
                work_dir=verbatim_dir,
                title=source.title,
                duration=source.duration or transcript.duration or 0.0,
                settings=settings,
                base_url=backend.base_url,
                token=backend.token,
            )
        if result and result.clips:
            return result.clips, "claude-code", f"brief: {result.brief_path.name}"
        warning("Claude Code produced nothing usable — falling back")

    if backend.kind in ("claude-code", "api") and backend.provider is not None:
        llm = ChatLLM(backend.provider, timeout=240)
        clips, method = plan_clips(
            transcript,
            source.duration or transcript.duration or 0.0,
            settings,
            llm=llm,
            title=source.title,
            loud=loud,
            chat=chat,
        )
        if clips:
            return clips, method, summarise_provider(backend.provider)

    clips = heuristic_clips(transcript, source.duration or transcript.duration or 0.0, settings)
    return clips, "heuristic", "no LLM available"


# --------------------------------------------------------------------------- #
# outputs
# --------------------------------------------------------------------------- #
def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _hook_slug(text: str, fallback: str) -> str:
    slug = slugify(text)[:32].strip("-")
    return slug or fallback


def _clip_filename(plan: ClipPlan) -> str:
    slug = _hook_slug(plan.hook or plan.title, f"clip-{plan.index:02d}")
    return f"clip-{plan.index:02d}-{slug}.mp4"


def _post_text(output: ClipOutput, source: VideoSource) -> str:
    """The `.txt` that goes with a clip: what to post it as."""
    plan = output.plan
    lines = [f"TITLE: {plan.title or plan.hook or 'clip'}"]
    if plan.hook and plan.hook != plan.title:
        lines.append(f"HOOK: {plan.hook}")
    if plan.caption:
        lines += ["", "CAPTION:", plan.caption]
    if plan.hashtags:
        lines += ["", " ".join(plan.hashtags)]
    lines += [
        "",
        f"SOURCE: {source.title}" + (f" — {source.url}" if source.url else ""),
        f"FROM: {_clock(plan.start)} TO: {_clock(plan.end)}"
        + (f" ({plan.duration:.0f}s)" if plan.duration else ""),
    ]
    details = [f"score {plan.score}"]
    if plan.layout:
        details.append(f"layout {plan.layout}")
    if plan.reason:
        details.append(plan.reason)
    lines.append("NOTES: " + " · ".join(details))
    return "\n".join(lines) + "\n"


def _clock(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _gallery_html(result: VideoResult, settings: Settings) -> str:
    source = result.source
    cards: list[str] = []
    for clip in result.clips:
        plan = clip.plan
        video_src = html.escape(clip.path.name) if clip.path else ""
        poster = html.escape(clip.thumbnail.name) if clip.thumbnail else ""
        cards.append(
            f"""
      <article class="card">
        <video controls playsinline preload="metadata" poster="{poster}" src="{video_src}"></video>
        <div class="body">
          <h3>{html.escape(plan.hook or plan.title)}</h3>
          <p class="meta">{plan.start:.1f}s → {plan.end:.1f}s · {plan.duration:.0f}s · score {plan.score}</p>
          <p class="reason">{html.escape(plan.reason)}</p>
          <p class="file"><a href="{video_src}" download>download mp4</a></p>
        </div>
      </article>"""
        )

    if not cards:
        cards.append('<p class="empty">No clips were produced for this video.</p>')

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(source.title)} — clips</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 32px 20px 64px; background: #0b0b0f; color: #f2f2f7;
         font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  header {{ max-width: 1100px; margin: 0 auto 28px; }}
  h1 {{ font-size: 22px; margin: 0 0 6px; }}
  .sub {{ color: #9a9aa8; font-size: 13px; }}
  .grid {{ max-width: 1100px; margin: 0 auto; display: grid; gap: 20px;
           grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); }}
  .card {{ background: #15151c; border: 1px solid #23232e; border-radius: 14px; overflow: hidden; }}
  video {{ width: 100%; aspect-ratio: 9/16; background: #000; display: block; }}
  .body {{ padding: 12px 14px 16px; }}
  h3 {{ margin: 0 0 6px; font-size: 15px; }}
  .meta {{ margin: 0 0 8px; color: #8f8f9d; font-size: 12px; }}
  .reason {{ margin: 0 0 10px; color: #c9c9d4; font-size: 13px; }}
  .file a {{ color: #7cc4ff; font-size: 13px; text-decoration: none; }}
  .empty {{ color: #9a9aa8; }}
  footer {{ max-width: 1100px; margin: 36px auto 0; color: #6f6f7c; font-size: 12px; }}
</style>
</head>
<body>
  <header>
    <h1>{html.escape(source.title)}</h1>
    <p class="sub">
      {html.escape(source.uploader or "unknown channel")} ·
      {settings.width}×{settings.height} · {settings.fps} fps ·
      captions: {settings.caption_style} · clips from {html.escape(result.method)}
    </p>
  </header>
  <main class="grid">{''.join(cards)}
  </main>
  <footer>Generated by Clipper v{__version__} · <a href="../index.html" style="color:#7cc4ff">all videos</a></footer>
</body>
</html>
"""


def _index_html(results: list[VideoResult]) -> str:
    cards = []
    for result in results:
        slug = result.dir.name if result.dir else ""
        covered = len(result.clips)
        cards.append(
            f"""
      <a class="card" href="{html.escape(slug)}/index.html">
        <strong>{html.escape(result.source.title)}</strong>
        <span>{covered} clip(s) · {html.escape(result.method)}</span>
        <span class="dim">{html.escape((result.source.uploader or "")[:60])}</span>
      </a>"""
        )
    if not cards:
        cards.append('<p class="empty">Nothing was produced.</p>')
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clipper — {len(results)} video(s)</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; padding:48px 20px; background:#0b0b0f; color:#f2f2f7;
         font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  h1 {{ max-width:900px; margin:0 auto 24px; font-size:22px; }}
  .grid {{ max-width:900px; margin:0 auto; display:grid; gap:14px;
           grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); }}
  .card {{ display:flex; flex-direction:column; gap:4px; padding:16px; border-radius:12px;
           background:#15151c; border:1px solid #23232e; color:inherit; text-decoration:none; }}
  .dim {{ color:#8f8f9d; font-size:12px; }}
  .empty {{ color:#9a9aa8; }}
</style></head>
<body><h1>Clipper run · {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}</h1>
<div class="grid">{''.join(cards)}</div></body></html>
"""


def _report_markdown(results: list[VideoResult], settings: Settings) -> str:
    lines = ["# Clipping report", ""]
    lines.append(f"- generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    lines.append(f"- output: {settings.width}×{settings.height}, {settings.fps} fps, captions: {settings.caption_style}")
    lines.append("")
    for result in results:
        lines.append(f"## {result.source.title}")
        lines.append("")
        if result.source.url:
            lines.append(f"- source: {result.source.url}")
        lines.append(f"- duration: {result.source.duration:.0f}s")
        lines.append(f"- transcript: {result.transcript.source} ({len(result.transcript.words())} words)")
        lines.append(f"- selection: {result.method}")
        if result.error:
            lines.append(f"- **error:** {result.error}")
        lines.append("")
        if result.clips:
            lines.append("| # | file | in | out | length | edit | score | layout | hook |")
            lines.append("|---|------|----|-----|--------|------|-------|--------|------|")
            for clip in result.clips:
                plan = clip.plan
                name = clip.path.name if clip.path else "(dry run)"
                hook = (plan.hook or plan.title).replace("|", "/")
                length = clip.output_duration or plan.duration
                lines.append(
                    f"| {plan.index} | `{name}` | {plan.start:.1f}s | {plan.end:.1f}s | "
                    f"{length:.0f}s | {clip.editing or '-'} | {plan.score} | "
                    f"{clip.layout or '-'} | {hook} |"
                )
        else:
            lines.append("_no clips_")
        lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# main entry point
# --------------------------------------------------------------------------- #
def run(settings: Settings) -> int:
    if not settings.urls:
        raise PipelineError("no URLs given (pass --url or set CLIP_URLS)")

    problems = doctor(settings)
    if problems:
        for problem in problems:
            warning(problem)
        if not resolve_ffmpeg(settings):
            raise PipelineError("ffmpeg is required to render clips")

    settings.out_dir.mkdir(parents=True, exist_ok=True)
    log.info("Clipper v%s · %d URL(s) · ffmpeg=%s", __version__, len(settings.urls), settings.ffmpeg)
    log.info("LLM provider: %s", summarise_provider(resolve_provider(settings)))

    results: list[VideoResult] = []
    started = time.time()

    with make_backend(settings) as backend:
        log.info("brain: %s", backend.describe())

        for url in settings.urls:
            try:
                result = process_url(url, settings, backend)
                results.append(result)
            except Exception as exc:  # noqa: BLE001 - one bad URL must not kill the run
                log.error("failed for %s: %s", url, exc)
                log.debug("traceback", exc_info=True)
                if settings.fail_fast:
                    raise
                results.append(
                    VideoResult(
                        source=VideoSource(url=url, video_id="", title=url, duration=0.0),
                        transcript=Transcript(),
                        method="failed",
                        error=str(exc),
                    )
                )

    _write_outputs(results, settings)
    elapsed = time.time() - started
    produced = sum(len(result.clips) for result in results)
    failures = [result for result in results if result.error]

    if produced == 0:
        raise PipelineError("no clips were produced; check the log above")

    notice(f"done: {produced} clip(s) from {len(results)} video(s) in {elapsed / 60:.1f} min")
    if failures:
        warning(f"{len(failures)} video(s) failed")
    print(f"\nClips are in {settings.out_dir.resolve()}  (open index.html)")
    return 0


def resolve_ffmpeg(settings: Settings) -> str:
    from .render import resolve_binary

    return resolve_binary(settings.ffmpeg)


def _chat_signals(url: str, work_dir: Path, settings: Settings) -> list[ChatEvent]:
    """Chat bursts for this video, or an empty list. A chat file never fails a run."""
    try:
        path = download_chat(url, work_dir, settings)
        if path is None:
            return []
        messages = load_chat(path)
        if not messages:
            log.info("chat replay had no readable messages")
            return []
        events = bursts(
            messages,
            bucket=settings.chat_bucket,
            min_messages=settings.chat_min,
        )
        log.info("chat replay: %d message(s) → %d burst(s)", len(messages), len(events))
        return events
    except Exception as exc:  # noqa: BLE001 - signals are a bonus
        log.debug("chat signals skipped: %s", exc)
        return []


def _shift_plan(plan: ClipPlan, offset: float) -> ClipPlan:
    """Re-express a clip plan against a file that starts at ``offset``."""
    from dataclasses import replace

    return replace(plan, start=0.0, end=max(0.4, plan.end - offset))


def _shift_transcript(transcript: Transcript, offset: float, end: float) -> Transcript:
    """Words of ``[offset, end)`` re-timed so the section file starts at zero."""
    from .subtitles import Word, _group_words

    words = [
        Word(start=max(0.0, word.start - offset), end=max(0.05, word.end - offset), text=word.text)
        for word in transcript.words_between(offset, end)
    ]
    words.sort(key=lambda word: word.start)
    return Transcript(
        segments=_group_words(words),
        language=transcript.language,
        source=transcript.source,
        duration=max(0.0, end - offset),
    )


def _media_for_clip(
    plan: ClipPlan,
    source: VideoSource,
    transcript: Transcript,
    base: Path,
    work_dir: Path,
    settings: Settings,
) -> tuple[Path, ClipPlan, Transcript, Path | None]:
    """Which file to render this clip from, and the plan/transcript that match it.

    Normally that is the one downloaded video. In audio-only mode it is a freshly
    downloaded section of exactly this clip, re-timed to start at zero — which is how a
    2-hour stream becomes three small downloads instead of one huge one.
    """
    if not source.audio_only or source.path is None:
        return source.path, plan, transcript, None

    try:
        section = download_section(source.url, plan.start, plan.end, work_dir, settings)
    except Exception as exc:  # noqa: BLE001 - fall back to the whole video once
        warning(f"clip {plan.index}: section download failed ({exc}); downloading the video")
        full = download(source.url, base, settings)
        source.audio_only = False
        source.path = full.path
        source.subtitle_files = full.subtitle_files or source.subtitle_files
        if source.path is None:
            raise PipelineError("the fallback download produced no video") from exc
        return source.path, plan, transcript, None

    if section.path is None:
        raise PipelineError("the section download produced no file")
    log.info(
        "clip %d: rendering from a %.0fs section (%.1f MB) instead of the whole video",
        plan.index,
        section.duration,
        section.path.stat().st_size / 1e6,
    )
    return (
        section.path,
        _shift_plan(plan, plan.start),
        _shift_transcript(transcript, plan.start, plan.end),
        section.path,
    )


def process_url(url: str, settings: Settings, backend: Backend) -> VideoResult:
    with step(f"Video: {url}"):
        out_dir = settings.out_dir
        staging = Path(settings.work_dir) if settings.work_dir else None
        base = staging / "ingest" if staging else out_dir / "_ingest"
        base.mkdir(parents=True, exist_ok=True)

        if settings.audio_only and local_media_path(url) is None:
            with step("Downloading the audio only"):
                source = download_audio(url, base, settings)
        else:
            with step("Downloading"):
                source = download(url, base, settings)
        log.info("downloaded: %s (%.1fs)", source.title, source.duration)

        clip_dir = out_dir / source.slug
        work_dir = clip_dir / "_work"
        work_dir.mkdir(parents=True, exist_ok=True)

        transcript = get_transcript(source, work_dir, settings)
        shutil.copyfile(work_dir / "transcript.json", clip_dir / "transcript.json")
        _write_json(clip_dir / "info.json", source.to_dict())

        loud: list[LoudEvent] = []
        if settings.signals and source.path is not None:
            with step("Listening for loud moments"):
                loud = loud_moments(
                    source.path,
                    settings,
                    end=source.duration or transcript.duration or None,
                )
            log.info("audio signals: %s", summarise_signals(loud))
            _write_json(clip_dir / "signals.json", [event.to_dict() for event in loud])

        chat: list[ChatEvent] = []
        with step("Reading the chat replay"):
            chat = _chat_signals(url, work_dir, settings)
        if chat:
            _write_json(clip_dir / "chat.json", [event.to_dict() for event in chat])
            log.info("chat signals: %s", summarise_chat(chat))

        clips, method, notes = choose_clips(
            transcript, source, settings, backend, work_dir, loud=loud, chat=chat
        )
        if not clips:
            raise PipelineError("no clips passed validation")

        log.info("selected %d clip(s) via %s", len(clips), method)
        _write_json(
            clip_dir / "clips.json",
            {"video": source.to_dict(), "method": method, "notes": notes, "clips": [c.to_dict() for c in clips]},
        )

        outputs: list[ClipOutput] = []
        for plan in clips:
            name = _clip_filename(plan)
            target = clip_dir / name
            try:
                media, render_plan, render_transcript, section = _media_for_clip(
                    plan, source, transcript, base, work_dir, settings
                )
                rendered: RenderResult = render_clip(
                    media,
                    target,
                    render_plan,
                    render_transcript,
                    settings,
                    ass_dir=clip_dir,
                    dry_run=settings.dry_run,
                )
                if section is not None and not settings.keep_source:
                    section.unlink(missing_ok=True)
                if not settings.dry_run:
                    (clip_dir / f"{target.stem}.txt").write_text(
                        _post_text(
                            ClipOutput(
                                plan=plan,
                                path=target,
                                thumbnail=None,
                                ass=None,
                                layout=rendered.layout,
                            ),
                            source,
                        ),
                        encoding="utf-8",
                    )
                outputs.append(
                    ClipOutput(
                        plan=plan,
                        path=rendered.path if not settings.dry_run else None,
                        thumbnail=rendered.thumbnail,
                        ass=clip_dir / f"{target.stem}.ass",
                        output_duration=rendered.output_duration,
                        editing=rendered.editing,
                        edit=rendered.edit.to_dict() if rendered.edit else None,
                        layout=rendered.layout,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                log.error("clip %d failed: %s", plan.index, exc)
                if settings.fail_fast:
                    raise

        # Second pass: record what the render actually produced (cuts, output length).
        _write_json(
            clip_dir / "clips.json",
            {
                "video": source.to_dict(),
                "method": method,
                "notes": notes,
                "clips": [clip.to_dict() for clip in outputs],
            },
        )

        result = VideoResult(
            source=source,
            transcript=transcript,
            clips=outputs,
            method=method,
            dir=clip_dir,
        )
        (clip_dir / "index.html").write_text(_gallery_html(result, settings), encoding="utf-8")

        # Never delete a file the user handed us: only our own downloads.
        if (
            not settings.keep_source
            and not source.local
            and source.path
            and source.path.exists()
        ):
            try:
                source.path.unlink()
                for extra in base.glob("source*"):
                    if extra.is_file():
                        extra.unlink()
                shutil.rmtree(work_dir, ignore_errors=True)
            except OSError as exc:
                log.debug("cleanup skipped: %s", exc)

        if len(outputs) < len(clips):
            result.error = f"{len(clips) - len(outputs)} clip(s) failed to render"

    return result


# --------------------------------------------------------------------------- #
# run outputs
# --------------------------------------------------------------------------- #
def _write_outputs(results: list[VideoResult], settings: Settings) -> None:
    out_dir = settings.out_dir
    manifest = {
        "tool": f"clipper v{__version__}",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "settings": {
            "width": settings.width,
            "height": settings.height,
            "fps": settings.fps,
            "caption_style": settings.caption_style,
            "fit": settings.fit,
            "clips_per_video": settings.clips,
            "min_duration": settings.min_duration,
            "max_duration": settings.max_duration,
            "llm_backend": settings.llm_backend,
            "editing": settings.editing,
            "tighten": settings.tighten,
            "speed": settings.speed,
            "look": settings.look,
            "punch_in": settings.punch_in,
            "progress_bar": settings.progress_bar,
        },
        "videos": [result.to_dict() for result in results if result.dir],
        "failures": [result.error for result in results if result.error],
    }
    _write_json(out_dir / "manifest.json", manifest)
    (out_dir / "report.md").write_text(_report_markdown(results, settings), encoding="utf-8")
    (out_dir / "index.html").write_text(_index_html(results), encoding="utf-8")

    rows = ["| video | clips | method | file |", "|---|---|---|---|"]
    for result in results:
        if result.dir is None:
            continue
        for clip in result.clips:
            if clip.path is None:
                continue
            rows.append(
                f"| {result.source.title[:48]} | {clip.plan.index} | {result.method} | "
                f"`{result.dir.name}/{clip.path.name}` |"
            )
    produced = sum(len(result.clips) for result in results)
    summary(
        "## Clipper finished\n\n"
        f"**{produced} clip(s)** from {len(results)} video(s).\n\n"
        + "\n".join(rows)
        + "\n\nDownload the `clips` artifact, unzip it and open `index.html`.\n"
    )


__all__ = ["run", "process_url", "PipelineError", "make_backend", "Backend"]
