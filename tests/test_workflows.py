from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOWS = sorted(Path(".github/workflows").glob("*.yml"))


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_workflows_exist():
    names = {path.name for path in WORKFLOWS}
    assert {"clips.yml", "tests.yml"} <= names


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_structure(path: Path):
    data = load(path)
    assert isinstance(data, dict)
    assert data.get("name")
    assert "on" in data or True in data  # `on:` is parsed as True by YAML 1.1
    assert isinstance(data.get("jobs"), dict) and data["jobs"]
    for job in data["jobs"].values():
        assert job.get("runs-on")
        assert job.get("steps")


def test_clip_workflow_inputs_and_contract():
    data = load(Path(".github/workflows/clips.yml"))
    triggers = data.get("on") or data.get(True)
    assert "workflow_dispatch" in triggers
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert "urls" in inputs and inputs["urls"]["required"] is True
    assert inputs["caption_style"]["options"] == ["karaoke", "plain", "none"]
    assert inputs["fit"]["options"] == ["crop", "blur", "pad"]
    assert data["permissions"]["contents"] == "write"

    job = data["jobs"]["clip"]
    assert job["timeout-minutes"] <= 180
    steps = job["steps"]
    uses = [step.get("uses", "") for step in steps]
    assert any("actions/upload-artifact" in value for value in uses)
    assert any("actions/setup-python" in value for value in uses)

    runs = " ".join(step.get("run", "") for step in steps)
    assert "python -m clipper" in runs
    assert "ffmpeg" in runs
    assert "claude-code" in runs

    assert inputs["max_height"]["default"] == "1080"

    cut_step = next(step for step in steps if step.get("env", {}).get("CLIP_URLS"))
    env = cut_step["env"]
    for key in ("CLIP_URLS", "CLIP_COUNT", "LLM_BACKEND", "GROQ_API_KEY", "GEMINI_API_KEY"):
        assert key in env, key
    # formats/proxy knobs and the artefact-friendly summary
    for key in ("CLIP_MAX_VIDEO_HEIGHT", "YT_PROXY", "YTDLP_EXTRA_ARGS"):
        assert key in env, key

    summary = next(step for step in steps if step.get("name") == "Run summary")
    assert summary["if"] == "always()"
    assert "GITHUB_STEP_SUMMARY" in summary["run"]


def test_tests_workflow_runs_pytest():
    data = load(Path(".github/workflows/tests.yml"))
    runs = " ".join(
        step.get("run", "") for job in data["jobs"].values() for step in job["steps"]
    )
    assert "pytest" in runs
    assert "ruff" in runs
