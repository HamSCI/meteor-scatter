"""Git provenance for inventory --json output."""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent


def _run_git(*args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT,
            capture_output=True, text=True, timeout=5,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _detect_git_info() -> dict:
    sha = _run_git("rev-parse", "HEAD")
    if not sha:
        return {}
    return {
        "sha": sha,
        "short": sha[:7],
        "ref": _run_git("rev-parse", "--abbrev-ref", "HEAD") or "detached",
        "dirty": _run_git("status", "--porcelain") != "",
    }


GIT_INFO: dict = _detect_git_info()
