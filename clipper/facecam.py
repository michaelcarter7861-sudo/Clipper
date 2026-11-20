"""Find a streamer's facecam overlay so a stream clip can be stacked 9:16.

Ported from the GTA 6 Clipper skill (``skills/gta6-clipper``, upstream
``vossashtonai/gta6-clipper`` @ 11cee2b, MIT). The upstream code samples frames,
looks for a face that stays in the same small spot, then scans for the overlay's
straight static edges around it — that is what separates a facecam from a face
that happens to be on screen.

Two kinds of answer:

- ``facecam`` — a small face in the same place every time: real overlay. The clip
  is built as facecam on top, gameplay below.
- ``cam`` — one face filling the frame (a full-screen camera view).
- ``gameplay`` — no usable overlay: plain reframe.
- ``unknown`` — OpenCV is not installed or the frames could not be read.

Everything is best-effort: a wrong answer must never break a render, so the
caller keeps the plain reframe when this returns ``unknown`` or ``gameplay``.
"""

from __future__ import annotations

import json
import statistics
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .log import get_logger

log = get_logger()

MODES = ("facecam", "cam", "gameplay", "unknown")

#: ``fit`` values that ask for facecam detection before rendering.
DETECTING_FITS = ("facecam", "auto")

#: A facecam panel narrower than this share of the frame is a real overlay.
MAX_OVERLAY_WIDTH = 0.2
#: The detector must find the same face in at least this share of sampled frames.
MIN_PERSISTENCE = 0.4


@dataclass
class Layout:
    """What the detector saw, in source-video pixels."""

    mode: str = "unknown"
    box: tuple[int, int, int, int] | None = None  # x, y, w, h of the overlay
    face: tuple[float, float, float, float] | None = None  # x, y, w, h of the face
    width: int = 0
    height: int = 0
    sampled: int = 0
    note: str = ""

    @property
    def stacked(self) -> bool:
        """True when the clip should be built facecam-over-gameplay."""
        return self.mode == "facecam" and self.box is not None

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "box": list(self.box) if self.box else None,
            "face": [round(v, 1) for v in self.face] if self.face else None,
            "source_size": [self.width, self.height],
            "sampled_frames": self.sampled,
            "note": self.note,
        }


@dataclass
class StackPlan:
    """The 9:16 geometry for a facecam clip, in output pixels."""

    top_h: int
    bottom_h: int
    cam: tuple[int, int, int, int]
    game: tuple[int, int, int, int]
    seam: int
    caption_margin_v: int
    hook_margin_v: int

    def to_dict(self) -> dict:
        return {
            "top_h": self.top_h,
            "bottom_h": self.bottom_h,
            "seam": self.seam,
            "caption_margin_v": self.caption_margin_v,
            "hook_margin_v": self.hook_margin_v,
        }


# --------------------------------------------------------------------------- #
# availability
# --------------------------------------------------------------------------- #
def cv2_available() -> bool:
    """True when OpenCV can be imported (``pip install opencv-python-headless``)."""
    try:  # pragma: no cover - depends on the machine
        import cv2  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure means "not available"
        return False
    return True


# --------------------------------------------------------------------------- #
# frames
# --------------------------------------------------------------------------- #
def _probe_size(ffprobe: str, source: Path) -> tuple[int, int]:
    try:
        completed = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "json", str(source),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        stream = json.loads(completed.stdout or "{}")["streams"][0]
        return int(stream["width"]), int(stream["height"])
    except Exception:  # noqa: BLE001 - a missing probe must not break detection
        return 0, 0


def sample_frames(
    source: Path,
    out_dir: Path,
    *,
    start: float = 0.0,
    end: float | None = None,
    ffmpeg: str = "ffmpeg",
    count: int = 12,
    timeout: int = 180,
) -> list[Path]:
    """Write ``count`` JPEG frames spread over ``[start, end)`` and return them."""
    count = max(2, int(count))
    span = (end - start) if end is not None and end > start else 60.0
    span = max(1.0, min(span, 3600.0))
    out_dir.mkdir(parents=True, exist_ok=True)
    rate = max(0.01, count / span)
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{max(0.0, start):.3f}", "-t", f"{span:.3f}",
        "-i", str(source), "-vf", f"fps={rate:.6f}",
        "-frames:v", str(count), "-q:v", "3", str(out_dir / "frame_%03d.jpg"),
    ]
    try:
        subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception as exc:  # noqa: BLE001 - no frames just means "no answer"
        log.debug("frame sampling failed: %s", exc)
    return sorted(out_dir.glob("frame_*.jpg"))


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
def _cluster(faces: list[tuple[float, float, float, float]], width: int, height: int):
    """Keep the face box that shows up in the same place most often."""
    best: list[tuple[float, float, float, float]] = []
    for box in faces:
        cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
        members = [
            other
            for other in faces
            if abs(other[0] + other[2] / 2 - cx) < max(12.0, width * 0.06)
            and abs(other[1] + other[3] / 2 - cy) < max(12.0, height * 0.08)
        ]
        if len(members) > len(best):
            best = members
    return best


def _scan(profile, start: float, stop: float, *, limit: float, floor: float) -> float | None:
    """First strong edge while walking outward from ``start`` towards ``stop``."""
    low, high = (start, stop) if stop >= start else (stop, start)
    indices = [i for i in range(int(low), int(high)) if 0 <= i < len(profile)]
    if len(indices) < 5:
        return None
    values = [float(profile[i]) for i in indices]
    baseline = statistics.median(values) + 1e-3
    peak = max(values)
    if peak <= limit * baseline or peak <= floor:
        return None
    return float(indices[values.index(peak)])


def _motion_edges(grays: list, face: tuple[float, float, float, float], width: int, height: int):
    """Walk outward from the face until the picture starts moving (so: gameplay).

    A facecam is the still-ish part of the frame; the game behind it moves. Cheap,
    and it survives thin or missing overlay borders.
    """
    import cv2
    import numpy as np

    if len(grays) < 3:
        return None
    diffs = [
        np.abs(second.astype(np.float32) - first.astype(np.float32))
        for first, second in zip(grays, grays[1:], strict=False)
    ]
    motion = cv2.blur(np.median(np.stack(diffs), axis=0), (9, 9))
    fx, fy, fw, fh = face
    inside = motion[int(fy) : int(fy + fh), int(fx) : int(fx + fw)]
    base = float(np.median(inside)) if inside.size else float(np.median(motion))
    limit = max(2.0, base * 3.0 + 1.5)
    columns = motion.mean(axis=0)
    rows = motion.mean(axis=1)

    def walk(profile, start: float, step: int, stop: int) -> float:
        index = int(max(0, min(len(profile) - 1, start)))
        last = index
        while 0 <= index < len(profile) and (stop - index) * step > 0:
            if profile[index] > limit:
                return float(last)
            last = index
            index += step
        return float(last)

    return (
        walk(columns, fx - fw * 0.1, -1, 1),
        walk(rows, fy - fh * 0.1, -1, 1),
        walk(columns, fx + fw * 1.1, 1, width - 2),
        walk(rows, fy + fh * 1.1, 1, height - 2),
    )


def _overlay_box(frames, width: int, height: int, persistence: float):
    """Return ``(layout_mode, box, face)`` from decoded frames."""
    import cv2  # imported lazily: the module works without OpenCV
    import numpy as np

    detector = None
    haar_path = ""
    try:
        haar_path = str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
        detector = cv2.CascadeClassifier(haar_path)
        if detector.empty():
            detector = None
    except Exception:  # noqa: BLE001 - OpenCV builds without data are possible
        detector = None
    if detector is None:
        return "unknown", None, None, "OpenCV has no face cascade on this machine"

    faces: list[list[tuple[float, float, float, float]]] = []
    gradients: list[tuple] = []
    grays: list = []
    used = 0
    for path in frames:
        image = cv2.imread(str(path))
        if image is None:
            continue
        if image.shape[1] != width or image.shape[0] != height:
            image = cv2.resize(image, (width or image.shape[1], height or image.shape[0]))
        used += 1
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        grays.append(gray)
        gradients.append(
            (
                np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0)),
                np.abs(cv2.Sobel(gray, cv2.CV_32F, 0, 1)),
            )
        )
        found = detector.detectMultiScale(gray, 1.1, 6, minSize=(40, 40))
        faces.append([tuple(float(v) for v in box) for box in found])

    if not used:
        return "unknown", None, None, "no frames could be read"

    flat = [box for group in faces for box in group]
    if not flat:
        return "gameplay", None, None, "no face found"
    best = _cluster(flat, width, height)
    if len(best) < max(3, len(faces) * persistence):
        return "gameplay", None, None, "no face that stays in one place"

    fx = statistics.median(b[0] for b in best)
    fy = statistics.median(b[1] for b in best)
    fw = statistics.median(b[2] for b in best)
    fh = statistics.median(b[3] for b in best)
    if fw > width * MAX_OVERLAY_WIDTH:
        return "cam", None, (fx, fy, fw, fh), "face fills the frame"

    gx = np.median(np.stack([g[0] for g in gradients]), axis=0)
    gy = np.median(np.stack([g[1] for g in gradients]), axis=0)
    y0, y1 = int(max(0, fy - fh * 0.5)), int(min(height, fy + fh * 1.5))
    x0, x1 = int(max(0, fx - fw * 0.5)), int(min(width, fx + fw * 1.5))
    columns = gx[y0:y1].mean(axis=0) if y1 > y0 else gx.mean(axis=0)
    rows = gy[:, x0:x1].mean(axis=1) if x1 > x0 else gy.mean(axis=1)

    left = _scan(columns, fx - fw * 0.15, fx - fw * 2.6, limit=3.5, floor=6.0)
    right = _scan(columns, fx + fw * 1.15, fx + fw * 3.6, limit=3.5, floor=6.0)
    top = _scan(rows, fy - fh * 0.15, fy - fh * 2.2, limit=3.5, floor=6.0)
    bottom = _scan(rows, fy + fh * 1.15, fy + fh * 3.2, limit=3.5, floor=6.0)

    candidates: list[tuple[float, float, float, float]] = []
    if None not in (left, right, top, bottom):
        candidates.append((left, top, right, bottom))

    # Straight edges are not always there (thin borders, busy gameplay), so also
    # ask "where does the picture start moving again" and keep the tightest box
    # that still holds the whole face. Errors here almost always overshoot.
    motion = _motion_edges(grays, (fx, fy, fw, fh), width, height)
    if motion is not None:
        m_left, m_top, m_right, m_bottom = motion
        candidates.append((m_left, m_top, m_right, m_bottom))

    guess_w = fw * 2.4
    candidates.append(
        (fx + fw / 2 - guess_w / 2, fy - fh * 0.6, fx + fw / 2 + guess_w / 2, fy + fh * 1.6)
    )

    cleaned = [
        box
        for box in (
            _clean_box(left_c, top_c, right_c, bottom_c, fx, fy, fw, fh, width, height)
            for left_c, top_c, right_c, bottom_c in candidates
        )
        if box is not None
    ]
    for box in cleaned:
        # A panel that covers most of the frame is not an overlay, it is the
        # whole picture — stacking it would crop the gameplay to nothing.
        if box[2] <= width * 0.7 and box[3] <= height * 0.8:
            return "facecam", box, (fx, fy, fw, fh), "facecam overlay"
    if cleaned:
        return "facecam", cleaned[0], (fx, fy, fw, fh), "facecam overlay (rough)"
    return "gameplay", None, (fx, fy, fw, fh), "face found but no overlay box"


def _clean_box(
    left: float,
    top: float,
    right: float,
    bottom: float,
    fx: float,
    fy: float,
    fw: float,
    fh: float,
    width: int,
    height: int,
) -> tuple[int, int, int, int] | None:
    """Clamp a candidate to the frame and reject anything that is not an overlay."""
    left, right = max(0.0, left + 2), min(float(width), right - 2)
    top, bottom = max(0.0, top + 2), min(float(height), bottom - 2)
    box_w, box_h = right - left, bottom - top
    if box_w < fw * 1.4 or box_h < fh * 1.4:
        return None
    centre_x, centre_y = fx + fw / 2, fy + fh / 2
    if not (left <= centre_x <= right and top <= centre_y <= bottom):
        return None
    if box_w >= width - 4 and box_h >= height - 4:
        return None  # "the whole frame": that is not an overlay
    return (int(left), int(top), int(box_w), int(box_h))


def _even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def _overlaps(
    ax: float, ay: float, aw: float, ah: float, bx: float, by: float, bw: float, bh: float
) -> bool:
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def plan_stack(settings: Settings, layout: Layout) -> StackPlan | None:
    """Output geometry for a facecam clip, or ``None`` when the box is unusable."""
    if not layout.stacked or layout.box is None:
        return None
    width, height = _even(settings.width), _even(settings.height)
    src_w, src_h = layout.width, layout.height
    if not src_w or not src_h:
        return None

    cam_x, cam_y, cam_w, cam_h = layout.box
    cam_w, cam_h = max(2, cam_w), max(2, cam_h)
    cam_x = max(0, min(cam_x, src_w - cam_w))
    cam_y = max(0, min(cam_y, src_h - cam_h))

    # A wide facecam gets a taller top panel, capped so gameplay keeps the frame.
    top_h = _even(min(height * 0.5, max(height * 0.29, width * cam_h / cam_w)))
    top_h = max(2, min(top_h, height - 200))
    bottom_h = height - top_h

    # The gameplay area is cropped to the panel's aspect ratio, centred.
    game_w = src_h * width / bottom_h
    game_h = float(src_h)
    if game_w > src_w:
        game_w, game_h = float(src_w), src_w * bottom_h / width
    game_x = (src_w - game_w) / 2
    game_y = (src_h - game_h) / 2

    # The overlay would otherwise appear twice (once big on top, once small in the
    # gameplay crop), so slide the gameplay crop away from it when there is room.
    if _overlaps(game_x, game_y, game_w, game_h, cam_x, cam_y, cam_w, cam_h):
        if cam_x + cam_w / 2 < src_w / 2:
            shifted = min(src_w - game_w, cam_x + cam_w + 4.0)
        else:
            shifted = max(0.0, cam_x - game_w - 4.0)
        if not _overlaps(shifted, game_y, game_w, game_h, cam_x, cam_y, cam_w, cam_h):
            game_x = shifted
        elif cam_y + cam_h / 2 < src_h / 2:
            game_y = min(src_h - game_h, cam_y + cam_h + 4.0)
        else:
            game_y = max(0.0, cam_y - game_h - 4.0)

    caption_margin_v = int(round(height - (top_h + bottom_h * 0.58)))
    # The hook straddles the seam (the GTA 6 Clipper look) instead of sitting on
    # top of the captions inside the gameplay panel.
    hook_size = settings.caption_font_size * 0.74
    hook_margin_v = int(round(top_h - hook_size * 1.1))
    return StackPlan(
        top_h=top_h,
        bottom_h=bottom_h,
        cam=(int(cam_x), int(cam_y), int(cam_w), int(cam_h)),
        game=(int(game_x), int(game_y), _even(game_w), _even(game_h)),
        seam=top_h,
        caption_margin_v=max(24, caption_margin_v),
        hook_margin_v=max(16, hook_margin_v),
    )


def detect_layout(
    source: Path,
    *,
    settings: Settings,
    start: float = 0.0,
    end: float | None = None,
    frames: int = 12,
) -> Layout:
    """Best-effort answer for one clip window of ``source``. Never raises."""
    if not cv2_available():
        return Layout(note="opencv-python-headless is not installed")
    if not Path(source).exists():
        return Layout(note=f"{source} is not readable")
    try:
        width, height = _probe_size(settings.ffprobe, Path(source))
        if not width or not height:
            return Layout(note="could not read the source size")
        with tempfile.TemporaryDirectory(prefix="facecam-") as tmp:
            sampled = sample_frames(
                Path(source),
                Path(tmp),
                start=start,
                end=end,
                ffmpeg=settings.ffmpeg,
                count=frames,
            )
            if not sampled:
                return Layout(width=width, height=height, note="no frames were sampled")
            mode, box, face, note = _overlay_box(sampled, width, height, MIN_PERSISTENCE)
        return Layout(
            mode=mode,
            box=box,
            face=face,
            width=width,
            height=height,
            sampled=len(sampled),
            note=note,
        )
    except Exception as exc:  # noqa: BLE001 - detection must never break a render
        log.debug("facecam detection failed: %s", exc)
        return Layout(note=f"detection failed: {exc}")
