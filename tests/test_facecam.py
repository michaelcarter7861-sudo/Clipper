from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from clipper import facecam
from clipper.captions import build_ass
from clipper.config import Settings
from clipper.facecam import (
    Layout,
    _clean_box,
    _cluster,
    _motion_edges,
    _overlaps,
    _scan,
    cv2_available,
    detect_layout,
    plan_stack,
    sample_frames,
)
from clipper.render import build_filter_complex
from clipper.subtitles import Segment, Transcript, Word

SAMPLE = Path("samples/facecam-sample.mp4")


def _settings(**overrides) -> Settings:
    base = {"fit": "facecam", "width": 1080, "height": 1920}
    base.update(overrides)
    return Settings(**base)


def _overlay_layout(**overrides) -> Layout:
    base = {
        "mode": "facecam",
        "box": (26, 402, 315, 249),
        "face": (143.0, 459.0, 90.0, 90.0),
        "width": 1280,
        "height": 720,
    }
    base.update(overrides)
    return Layout(**base)


# --------------------------------------------------------------------------- #
# Layout plumbing
# --------------------------------------------------------------------------- #
def test_layout_only_stacks_with_a_box():
    assert _overlay_layout().stacked is True
    assert Layout(mode="facecam").stacked is False
    assert Layout(mode="gameplay", box=(1, 2, 3, 4)).stacked is False


def test_layout_to_dict_is_json_friendly():
    payload = _overlay_layout().to_dict()
    assert payload["mode"] == "facecam"
    assert payload["box"] == [26, 402, 315, 249]
    assert payload["face"] == [143.0, 459.0, 90.0, 90.0]
    assert payload["source_size"] == [1280, 720]
    assert payload["note"] == ""


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
def test_plan_stack_keeps_the_output_size_exact_and_even():
    stack = plan_stack(_settings(), _overlay_layout())
    assert stack is not None
    assert stack.top_h + stack.bottom_h == 1920
    assert stack.seam == stack.top_h
    assert stack.top_h % 2 == 0 and stack.bottom_h % 2 == 0
    assert all(value % 2 == 0 for value in stack.game[2:])


def test_plan_stack_puts_captions_over_the_gameplay_panel():
    stack = plan_stack(_settings(), _overlay_layout())
    assert stack is not None
    # ASS MarginV counts up from the bottom of the frame.
    caption_centre = 1920 - stack.caption_margin_v
    assert stack.seam < caption_centre < 1920
    # The hook straddles the seam: above it, but still on screen.
    assert 0 < stack.hook_margin_v < stack.seam


def test_plan_stack_moves_gameplay_crop_off_the_overlay():
    stack = plan_stack(_settings(), _overlay_layout())
    assert stack is not None
    game_x, game_y, game_w, game_h = stack.game
    cam_x, cam_y, cam_w, cam_h = stack.cam
    assert not _overlaps(game_x, game_y, game_w, game_h, cam_x, cam_y, cam_w, cam_h)
    # The overlay sits bottom-left, so the crop slides to the right.
    assert game_x > cam_x + cam_w - 2


def test_plan_stack_clamps_a_box_that_hangs_off_the_frame():
    stack = plan_stack(_settings(), _overlay_layout(box=(1200, 700, 300, 200)))
    assert stack is not None
    cam_x, cam_y, cam_w, cam_h = stack.cam
    assert cam_x + cam_w <= 1280 and cam_y + cam_h <= 720


def test_plan_stack_refuses_layouts_without_a_box():
    assert plan_stack(_settings(), Layout(mode="cam", face=(1, 2, 3, 4))) is None
    assert plan_stack(_settings(), Layout(mode="gameplay")) is None
    assert plan_stack(_settings(), _overlay_layout(width=0)) is None


def test_plan_stack_handles_a_tall_frame():
    stack = plan_stack(_settings(), _overlay_layout(width=1080, height=1920, box=(40, 1300, 360, 270)))
    assert stack is not None
    assert stack.top_h + stack.bottom_h == 1920
    assert stack.top_h <= 960


# --------------------------------------------------------------------------- #
# detection helpers
# --------------------------------------------------------------------------- #
def test_cluster_keeps_the_face_that_stays_put():
    faces = [
        (100, 100, 40, 40),
        (102, 101, 40, 40),
        (99, 103, 40, 40),
        (600, 300, 40, 40),  # one-off: a face in the gameplay
    ]
    best = _cluster(faces, 1280, 720)
    assert len(best) == 3
    assert all(box[0] < 200 for box in best)


def test_scan_finds_an_edge_and_ignores_flat_noise():
    profile = [1.0] * 50 + [40.0] * 2 + [1.0] * 50
    assert _scan(profile, 10, 60, limit=3.5, floor=6.0) == 50
    assert _scan([1.0] * 60, 5, 30, limit=3.5, floor=6.0) is None


def test_overlaps_is_strict():
    assert _overlaps(0, 0, 10, 10, 5, 5, 10, 10) is True
    assert _overlaps(0, 0, 10, 10, 10, 0, 10, 10) is False


def test_clean_box_rejects_the_whole_frame_and_tiny_boxes():
    face = (300.0, 200.0, 100.0, 100.0)  # x, y, w, h
    whole = _clean_box(0, 0, 1280, 720, *face, 1280, 720)
    assert whole is None
    tiny = _clean_box(300, 200, 380, 280, *face, 1280, 720)
    assert tiny is None
    ok = _clean_box(250, 150, 500, 400, *face, 1280, 720)
    assert ok is not None and ok[2] >= 240 and ok[3] >= 240


def test_clean_box_rejects_a_box_that_misses_the_face():
    face = (300.0, 200.0, 100.0, 100.0)
    assert _clean_box(700, 400, 1000, 700, *face, 1280, 720) is None


def test_motion_edges_needs_a_few_frames():
    assert _motion_edges([], (0, 0, 10, 10), 100, 100) is None


def test_detect_layout_never_raises_on_a_missing_file():
    layout = detect_layout(Path("/nope/missing.mp4"), settings=_settings())
    assert layout.mode in facecam.MODES
    assert layout.box is None


def test_detect_layout_explains_a_missing_opencv(monkeypatch):
    monkeypatch.setattr(facecam, "cv2_available", lambda: False)
    layout = detect_layout(SAMPLE, settings=_settings())
    assert layout.mode == "unknown"
    assert "opencv" in layout.note.lower()


def test_sample_frames_survives_a_broken_ffmpeg(tmp_path):
    assert sample_frames(SAMPLE, tmp_path, ffmpeg="/nope/ffmpeg", start=0, end=5) == []


# --------------------------------------------------------------------------- #
# filter graph
# --------------------------------------------------------------------------- #
def test_stack_graph_builds_two_panels_and_a_vstack():
    stack = plan_stack(_settings(), _overlay_layout())
    graph = build_filter_complex(
        _settings(), duration=20, ass_filter=None, stack=stack
    )
    assert "split=2[camsrc][gamesrc]" in graph
    assert "vstack=2[stacked]" in graph
    assert "[stacked]setsar=1" in graph
    # The bug this guards: a label followed by a comma is an empty filter.
    assert "[stacked]," not in graph
    assert ",,," not in graph


def test_stack_graph_overrides_the_reframe_chain():
    stack = plan_stack(_settings(), _overlay_layout())
    graph = build_filter_complex(
        _settings(fit="facecam"), duration=20, ass_filter=None, stack=stack
    )
    # The plain reframe (scale-to-fill-then-crop) is replaced by the two panels.
    assert "scale=1080:1920:force_original_aspect_ratio=increase" not in graph
    assert "crop=1080:852[camtop]" in graph
    assert "crop=1080:1068[gamebottom]" in graph


def test_plain_reframe_is_used_when_there_is_no_stack():
    graph = build_filter_complex(
        _settings(fit="auto"), duration=20, ass_filter=None, stack=None
    )
    assert "vstack" not in graph
    assert "crop=1080:1920" in graph


# --------------------------------------------------------------------------- #
# captions follow the panels
# --------------------------------------------------------------------------- #
def test_caption_margins_can_be_moved_for_a_facecam_clip():
    settings = _settings(hook_titles=True)
    words = [Word(start=0.2, end=0.6, text="hello"), Word(start=0.7, end=1.1, text="there")]
    transcript = Transcript(
        segments=[Segment(start=0.2, end=1.1, text="hello there", words=words)], source="test"
    )
    default = build_ass(transcript, 0.0, 2.0, settings, hook="hi")
    moved = build_ass(
        transcript, 0.0, 2.0, settings, hook="hi", caption_margin_v=449, hook_margin_v=890
    )
    def margin(ass: str, style: str) -> str:
        line = next(row for row in ass.splitlines() if row.startswith(f"Style: {style},"))
        return line.split(",")[-2]

    assert margin(moved, "Caption") == "449"
    assert margin(moved, "Hook") == "890"
    assert margin(default, "Caption") != "449"


# --------------------------------------------------------------------------- #
# end-to-end with a real facecam sample (needs ffmpeg + OpenCV)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not cv2_available(), reason="opencv-python-headless not installed")
@pytest.mark.skipif(not SAMPLE.exists(), reason="sample clip is missing")
def test_detects_the_overlay_in_the_sample_clip(tmp_path):
    ffmpeg = os.environ.get("CLIP_TEST_FFMPEG") or shutil.which("ffmpeg") or ""
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    ffprobe = os.environ.get("CLIP_TEST_FFPROBE") or shutil.which("ffprobe") or "ffprobe"
    layout = detect_layout(
        SAMPLE,
        settings=_settings(ffmpeg=ffmpeg, ffprobe=ffprobe),
        start=1,
        end=10,
        frames=10,
    )
    if layout.mode == "unknown":
        pytest.skip(f"detection unavailable here: {layout.note}")
    assert layout.mode == "facecam"
    assert layout.box is not None
    x, y, w, h = layout.box
    # The overlay in samples/facecam-sample.mp4 sits at (16, 200) and is 160x128.
    assert abs(x - 16) <= 12 and abs(y - 200) <= 12
    assert abs(w - 160) <= 25 and abs(h - 128) <= 25

