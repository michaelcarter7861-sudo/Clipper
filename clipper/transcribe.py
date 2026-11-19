"""Speech-to-text fallback for videos without usable subtitles."""

from __future__ import annotations

from pathlib import Path

from .config import Settings
from .log import get_logger
from .subtitles import Segment, Transcript, Word, _group_words
from .youtube import extract_audio

log = get_logger()


class TranscriptionError(RuntimeError):
    pass


def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return False
    return True


def transcribe(video_path: Path, settings: Settings, work_dir: Path) -> Transcript:
    """Transcribe with faster-whisper, returning word-level timings on CPU."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise TranscriptionError(
            "faster-whisper is not installed; install requirements-whisper.txt "
            "or pick a video that has subtitles"
        ) from exc

    wav = extract_audio(settings.ffmpeg, video_path, work_dir / "audio.wav")
    model_name = settings.whisper_model
    log.info("transcribing with faster-whisper (%s, cpu/int8) — this can take a while", model_name)

    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    language = settings.language if settings.language not in (None, "", "auto") else None
    segments_iter, info = model.transcribe(
        str(wav),
        language=language,
        word_timestamps=True,
        vad_filter=True,
        beam_size=5,
        condition_on_previous_text=False,
    )

    segments: list[Segment] = []
    all_words: list[Word] = []
    for raw in segments_iter:
        text = (raw.text or "").strip()
        if not text:
            continue
        words: list[Word] = []
        for word in getattr(raw, "words", None) or []:
            token = (word.word or "").strip()
            if not token:
                continue
            words.append(Word(start=float(word.start), end=float(word.end), text=token))
        all_words.extend(words)
        segments.append(
            Segment(start=float(raw.start), end=float(raw.end), text=text, words=words)
        )

    if not segments:
        raise TranscriptionError("whisper produced no speech; is the video silent?")

    if not any(segment.words for segment in segments):
        segments = _group_words(all_words)

    duration = max((s.end for s in segments), default=0.0)
    return Transcript(
        segments=segments,
        language=getattr(info, "language", None),
        source=f"whisper:{model_name}",
        duration=duration or None,
    )
