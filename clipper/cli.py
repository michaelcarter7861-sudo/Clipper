"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import (
    CAPTION_STYLES,
    EDITING_MODES,
    FIT_MODES,
    LLM_BACKENDS,
    LOOKS,
    Settings,
    settings_from_env,
)
from .llm import resolve_provider, summarise_provider
from .log import get_logger, setup_logging
from .pipeline import PipelineError, run
from .render import doctor, ffmpeg_filters, resolve_binary

log = get_logger()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clipper",
        description="Turn YouTube links into captioned, vertical, viral-moment clips.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"clipper {__version__}")
    parser.add_argument(
        "-u",
        "--url",
        dest="urls",
        action="append",
        metavar="URL",
        help="YouTube (or any yt-dlp supported) link; repeat for several videos",
    )
    parser.add_argument("--out", dest="out_dir", help="output directory (default: out)")
    parser.add_argument("--work-dir", dest="work_dir", help="where downloads land")

    selection = parser.add_argument_group("clip selection")
    selection.add_argument("--clips", type=int, help="clips per video")
    selection.add_argument("--min-duration", type=float, help="minimum clip length in seconds")
    selection.add_argument("--max-duration", type=float, help="maximum clip length in seconds")
    selection.add_argument(
        "--llm-backend",
        choices=LLM_BACKENDS,
        help="auto | claude-code | api | heuristic",
    )
    selection.add_argument("--llm-model", help="override the model id")
    selection.add_argument("--claude-model", help="model name reported to Claude Code")

    transcript = parser.add_argument_group("transcript")
    transcript.add_argument("--language", help="preferred subtitle/audio language (e.g. en, hi, ur)")
    transcript.add_argument("--whisper-model", help="faster-whisper model when no subtitles exist")
    transcript.add_argument(
        "--no-subtitles", dest="prefer_subtitles", action="store_false", default=None,
        help="ignore YouTube subtitles and always transcribe audio",
    )

    editing = parser.add_argument_group("editing")
    editing.add_argument(
        "--editing", choices=EDITING_MODES, help="full | cuts (jump cuts only) | off"
    )
    editing.add_argument("--no-tighten", dest="tighten", action="store_false", default=None,
                         help="keep pauses, no jump cuts")
    editing.add_argument("--max-pause", type=float, help="cut pauses longer than this (seconds)")
    editing.add_argument("--remove-fillers", dest="remove_fillers", action="store_true", default=None,
                         help="also cut um/uh/matlab/yaani")
    editing.add_argument("--speed", type=float, help="playback speed, e.g. 1.08")
    editing.add_argument("--punch-in", type=float, help="zoom factor, e.g. 1.04 (1.0 = off)")
    editing.add_argument("--look", choices=LOOKS, help="colour grade preset")
    editing.add_argument("--no-progress-bar", dest="progress_bar", action="store_false", default=None)
    editing.add_argument("--watermark", help="small handle/text in the corner, e.g. @yourchannel")

    captions = parser.add_argument_group("captions")
    captions.add_argument("--caption-style", choices=CAPTION_STYLES)
    captions.add_argument("--caption-font")
    captions.add_argument("--caption-size", type=int, help="font size in px (0 = auto)")
    captions.add_argument("--caption-primary", help="base text colour, e.g. '#FFFFFF'")
    captions.add_argument("--caption-highlight", help="karaoke highlight colour, e.g. '#FFE100'")
    captions.add_argument("--uppercase", dest="uppercase_captions", action="store_true", default=None)
    captions.add_argument("--no-uppercase", dest="uppercase_captions", action="store_false")
    captions.add_argument("--words-per-caption", type=int, help="words per caption burst")
    captions.add_argument("--no-hook-titles", dest="hook_titles", action="store_false", default=None)

    render = parser.add_argument_group("render")
    render.add_argument(
        "--fit",
        choices=FIT_MODES,
        help=(
            "crop | blur | pad | facecam | auto — facecam stacks a streamer's "
            "camera on top of the gameplay (needs opencv-python-headless); "
            "auto uses it when it finds one, otherwise crop"
        ),
    )
    render.add_argument("--fps", type=int)
    render.add_argument("--crf", type=int, help="x264 quality (lower = better, bigger)")
    render.add_argument("--preset", help="x264 preset")
    render.add_argument("--no-loudness", dest="loudness", action="store_false", default=None)
    render.add_argument("--denoise", dest="denoise", action="store_true", default=None)
    render.add_argument("--ffmpeg", help="ffmpeg binary")
    render.add_argument("--ffprobe", help="ffprobe binary")

    youtube = parser.add_argument_group("youtube")
    youtube.add_argument("--cookies", help="path to a Netscape cookies.txt")
    youtube.add_argument("--max-height", dest="max_video_height", type=int, help="max source height")
    youtube.add_argument("--proxy", help="route yt-dlp through a proxy, e.g. http://host:port")
    youtube.add_argument(
        "--check-youtube",
        action="store_true",
        help="probe a public video and report whether YouTube serves this machine",
    )
    youtube.add_argument(
        "--browser",
        default=None,
        help="obscura binary used when yt-dlp is refused ('' disables, default: PATH)",
    )
    youtube.add_argument(
        "--browser-stealth",
        action="store_true",
        default=None,
        help="run the browser with fingerprint spoofing (off by default)",
    )
    youtube.add_argument(
        "--list-formats",
        action="store_true",
        help="print the formats YouTube offers for each URL and exit",
    )

    misc = parser.add_argument_group("misc")
    misc.add_argument("--dry-run", action="store_true", default=None, help="plan + captions, no encoding")
    misc.add_argument("--keep-source", action="store_true", default=None, help="keep the downloaded video")
    misc.add_argument("--fail-fast", action="store_true", default=None)
    misc.add_argument("--verbose", "-v", action="store_true", default=None)
    misc.add_argument("--doctor", action="store_true", help="check the environment and exit")
    return parser


def settings_from_args(argv: list[str] | None = None) -> tuple[Settings, argparse.Namespace]:
    parser = build_parser()
    args = parser.parse_args(argv)
    overrides = {key: value for key, value in vars(args).items() if value is not None}
    overrides.pop("doctor", None)
    overrides.pop("list_formats", None)
    overrides.pop("check_youtube", None)
    settings = settings_from_env(**overrides)
    return settings, args


def main(argv: list[str] | None = None) -> int:
    settings, args = settings_from_args(argv)
    setup_logging(bool(settings.verbose))

    if args.doctor:
        return doctor_cli(settings)

    if getattr(args, "check_youtube", False):
        from .browser import browser_available
        from .youtube import youtube_reachable

        ok, reason = youtube_reachable(settings)
        print(f"youtube: {'PASS' if ok else 'FAIL'} — {reason}")
        browser_ok, browser_why = browser_available(settings)
        print(f"browser: {'PASS' if browser_ok else 'FAIL'} — {browser_why}")
        return 0 if ok else 1

    if getattr(args, "list_formats", False):
        if not settings.urls:
            log.error("give at least one link with --url")
            return 2
        from .youtube import list_formats

        return max(list_formats(url, settings) for url in settings.urls)

    try:
        return run(settings)
    except PipelineError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        log.error("interrupted")
        return 130


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
def _run_version(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        text = (result.stdout or result.stderr).strip().splitlines()
        return text[0][:90] if text else "unknown"
    except (OSError, subprocess.SubprocessError):
        return ""


def doctor_report(settings: Settings) -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []

    checks.append(("python", sys.version_info >= (3, 10), sys.version.split()[0]))

    ffmpeg = resolve_binary(settings.ffmpeg)
    checks.append(("ffmpeg", bool(ffmpeg), ffmpeg or "not found — install ffmpeg"))
    if ffmpeg:
        version = _run_version([ffmpeg, "-version"])
        filters = ffmpeg_filters(ffmpeg)
        has_libass = "ass" in filters
        checks.append(("ffmpeg captions (libass)", has_libass, "ass filter " + ("present" if has_libass else "MISSING")))
        checks.append(("ffmpeg version", True, version or "unknown"))

    ffprobe = resolve_binary(settings.ffprobe)
    checks.append(("ffprobe", bool(ffprobe), ffprobe or "not found (optional)"))

    font_files = []
    for folder in (Path("/usr/share/fonts"), Path.home() / ".fonts", Path.home() / ".local/share/fonts"):
        if folder.exists():
            font_files.extend(folder.rglob("*.ttf"))
    if shutil.which("fc-list"):
        checks.append(("fonts", True, f"fontconfig present, {len(font_files)} ttf files found"))
    elif font_files:
        checks.append(("fonts", True, f"{len(font_files)} ttf files found (no fc-list CLI)"))
    else:
        checks.append(
            ("fonts", False, "no fonts found — install fonts-dejavu-core so captions can render")
        )

    try:
        import yt_dlp

        checks.append(("yt-dlp", True, yt_dlp.version.__version__))
    except Exception:
        checks.append(("yt-dlp", False, "not installed — pip install -r requirements.txt"))

    from .youtube import js_runtime_status

    runtimes = js_runtime_status()
    checks.append(
        (
            "yt-dlp js runtime",
            bool(runtimes),
            ", ".join(f"{name}={path}" for name, path in sorted(runtimes.items()))
            or "none found — install deno or node, YouTube needs it for signature challenges",
        )
    )

    from .transcribe import whisper_available

    checks.append(
        (
            "faster-whisper",
            whisper_available(),
            "available" if whisper_available() else "not installed (only needed for videos without subtitles)",
        )
    )

    from .claude_code import claude_binary

    binary = claude_binary()
    checks.append(("claude CLI", bool(binary), binary or "not installed — npm i -g @anthropic-ai/claude-code"))

    provider = resolve_provider(settings)
    checks.append(("LLM provider", provider is not None, summarise_provider(provider)))

    if settings.cookies:
        checks.append(("cookies file", Path(settings.cookies).exists(), str(settings.cookies)))

    if os.environ.get("GITHUB_ACTIONS"):
        checks.append(("github actions", True, os.environ.get("GITHUB_WORKFLOW", "yes")))
    return checks


def doctor_cli(settings: Settings) -> int:
    print(f"Clipper v{__version__} environment check\n")
    checks = doctor_report(settings)
    for name, ok, detail in checks:
        mark = "✓" if ok else "!"
        print(f"  {mark} {name:<24} {detail}")
    problems = doctor(settings)
    if problems:
        print("\nproblems:")
        for problem in problems:
            print(f"  - {problem}")
    critical = [name for name, ok, _ in checks if not ok and name in {"ffmpeg", "yt-dlp"}]
    if critical:
        print(f"\nMissing required tools: {', '.join(critical)}")
        return 1
    print("\nLooks good. Example:\n  python -m clipper --url 'https://youtu.be/VIDEO_ID' --clips 3")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
