#!/usr/bin/env python3
"""Build samples/facecam-sample.mp4 — a stream-like clip with a facecam overlay.

No third-party footage: the "face" is drawn with Pillow, the background is
ffmpeg's testsrc2, and the face is pasted in the bottom-left corner at 160x128
with a white border. The point is a small, licence-clean fixture the facecam
detector can be tested against.

    python3 samples/make_facecam_sample.py

Needs Pillow and ffmpeg on PATH (or pass --ffmpeg).
"""

from __future__ import annotations

import argparse
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path


def _png(path: Path, size: tuple[int, int], pixels: bytes) -> None:
    """Write an RGB PNG without Pillow's help when Pillow is missing."""
    width, height = size
    raw = b"".join(b"\x00" + pixels[row * width * 3 : (row + 1) * width * 3] for row in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def draw_panel(path: Path, width: int = 320, height: int = 256) -> None:
    """A webcam-style panel: a room behind a head, framed like a real facecam.

    The head is about 45% of the panel width — real facecams are not headshots,
    and the detector's proportions depend on that.
    """
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except ImportError:  # pragma: no cover - fallback for bare machines
        _cartoon_face_bytes(path, width)
        return

    image = Image.new("RGB", (width, height), (208, 206, 202))
    draw = ImageDraw.Draw(image)
    # A dim room with a lit wall on the right: static, like a streamer's room.
    for x in range(width):
        tone = int(150 + 60 * (x / width))
        draw.line([(x, 0), (x, height)], fill=(tone, tone - 6, tone - 14))
    draw.rectangle([0, int(height * 0.62), width, height], fill=(96, 84, 78))
    face_size = int(height * 0.62)
    scale = face_size / 320.0
    left = (width - face_size) / 2
    top = height * 0.16
    def at(values: list[float]) -> list[float]:
        xs = [left + value * scale for value in values]
        return xs

    def rect(x0, y0, x1, y1):
        return [left + x0 * scale, top + y0 * scale, left + x1 * scale, top + y1 * scale]

    draw.ellipse(rect(70, 40, 250, 280), fill=(224, 186, 156))
    draw.ellipse(rect(80, 120, 240, 275), fill=(216, 178, 148))
    draw.ellipse(rect(62, 22, 258, 140), fill=(48, 38, 34))
    draw.ellipse(rect(78, 60, 242, 130), fill=(224, 186, 156))
    draw.rectangle(rect(105, 118, 150, 126), fill=(40, 30, 26))
    draw.rectangle(rect(170, 118, 215, 126), fill=(40, 30, 26))
    for cx in (128, 192):
        draw.ellipse(rect(cx - 26, 132, cx + 26, 158), fill=(250, 250, 250))
        draw.ellipse(rect(cx - 13, 136, cx + 13, 160), fill=(96, 62, 40))
        draw.ellipse(rect(cx - 6, 141, cx + 6, 155), fill=(14, 10, 8))
        draw.arc(
            rect(cx - 27, 126, cx + 27, 152), 200, 340, fill=(30, 22, 18),
            width=max(2, int(4 * scale)),
        )
    draw.line(
        at([160, 160]) + at([160, 196]), fill=(196, 158, 130), width=max(2, int(8 * scale))
    )
    draw.ellipse(rect(146, 190, 174, 208), fill=(200, 162, 134))
    draw.ellipse(rect(143, 199, 152, 208), fill=(120, 90, 74))
    draw.ellipse(rect(168, 199, 177, 208), fill=(120, 90, 74))
    draw.ellipse(rect(126, 212, 194, 252), fill=(150, 74, 74))
    draw.line(
        at([128, 232]) + at([192, 232]), fill=(240, 235, 232), width=max(2, int(6 * scale))
    )
    # A visible panel border, like a real facecam overlay.
    draw.rectangle([1, 1, width - 2, height - 2], outline=(250, 248, 244), width=3)
    image.filter(ImageFilter.GaussianBlur(0.9)).save(path)


def _cartoon_face_bytes(path: Path, size: int) -> None:
    """Last resort: a flat face-shaped image, no Pillow required."""
    width = height = size
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            dx, dy = (x - width / 2) / (width * 0.29), (y - height / 2) / (height * 0.38)
            inside = dx * dx + dy * dy < 1
            tone = (214, 176, 146) if inside else (232, 232, 232)
            if inside and (abs(y - height * 0.44) < height * 0.03):
                tone = (30, 22, 18) if abs(x - width / 2) > width * 0.08 else tone
            pixels.extend(tone)
    _png(path, (width, height), bytes(pixels))


def build(out: Path, ffmpeg: str) -> Path:
    with tempfile.TemporaryDirectory(prefix="facecam-sample-") as tmp:
        face = Path(tmp) / "panel.png"
        draw_panel(face)
        out.parent.mkdir(parents=True, exist_ok=True)
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            # Gameplay moves everywhere; without the grain the detector's
            # motion fallback would think the whole frame is a facecam.
            "-f", "lavfi",
            "-i", "testsrc2=size=640x360:rate=24:duration=12,noise=alls=28:allf=t+u",
            "-loop", "1", "-i", str(face),
            "-filter_complex",
            "[1:v]scale=160:128:force_original_aspect_ratio=increase,crop=160:128[fc];"
            "[0:v][fc]overlay=16:200:shortest=1[bg];"
            "[bg]drawbox=x=16:y=200:w=160:h=128:color=white@1:t=2[out]",
            "-map", "[out]", "-c:v", "libx264", "-preset", "veryslow",
            "-crf", "36", "-pix_fmt", "yuv420p", "-t", "12", str(out),
        ]
        subprocess.run(command, check=True)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(Path(__file__).with_name("facecam-sample.mp4")))
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    args = parser.parse_args()
    if not shutil.which(args.ffmpeg) and not Path(args.ffmpeg).exists():
        print(f"ffmpeg not found ({args.ffmpeg})", file=sys.stderr)
        return 1
    path = build(Path(args.out), args.ffmpeg)
    print(f"wrote {path} ({path.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
