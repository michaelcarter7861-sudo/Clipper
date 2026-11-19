"""Logging helpers with GitHub Actions friendly output."""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager

LOGGER_NAME = "clipper"


def in_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS", "").lower() == "true"


def setup_logging(verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        logger.setLevel(logging.DEBUG if verbose else logging.INFO)
        return logger

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


@contextmanager
def step(title: str):
    """Collapsible log group in Actions, a plain banner elsewhere."""
    log = get_logger()
    if in_github_actions():
        print(f"::group::{title}", flush=True)
    else:
        log.info("== %s", title)
    try:
        yield
    finally:
        if in_github_actions():
            print("::endgroup::", flush=True)


def notice(message: str) -> None:
    if in_github_actions():
        print(f"::notice::{message}", flush=True)
    get_logger().info(message)


def warning(message: str) -> None:
    if in_github_actions():
        print(f"::warning::{message}", flush=True)
    get_logger().warning(message)


def summary(markdown: str) -> None:
    """Append markdown to the workflow run summary when running in Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(markdown.rstrip() + "\n")
    except OSError as exc:  # pragma: no cover - best effort only
        get_logger().debug("could not write step summary: %s", exc)
