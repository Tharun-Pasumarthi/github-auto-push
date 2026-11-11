"""Shared pytest fixtures for GitHub Auto-Push Scheduler tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def run_git(cwd: Path, *args: str) -> str:
    """Execute a git command in tests and fail loudly on unexpected errors."""
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture()
def git_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """Create a local bare remote and a cloned working repository on branch 'main'."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    run_git(remote, "init", "--bare", "--initial-branch=main")

    local = tmp_path / "local"
    run_git(tmp_path, "clone", str(remote), str(local))
    run_git(local, "config", "user.name", "Test Setup")
    run_git(local, "config", "user.email", "setup@example.com")
    run_git(local, "checkout", "-B", "main")

    (local / "README.md").write_text("# Test Central Repo\n", encoding="utf-8")
    (local / "reports").mkdir()
    (local / "reports" / ".gitkeep").write_text("", encoding="utf-8")
    run_git(local, "add", "README.md", "reports/.gitkeep")
    run_git(local, "commit", "-m", "chore: initial commit")
    run_git(local, "push", "-u", "origin", "main")

    return local, remote

