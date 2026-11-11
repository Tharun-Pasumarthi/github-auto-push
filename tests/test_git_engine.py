"""Integration and unit tests for the central Git commit and push engine."""

from __future__ import annotations

import os
import subprocess
from datetime import date
from pathlib import Path

import pytest

from src.git_engine import GitEngine, GitEngineError, GitPushRejectedError, GitPushTransientError
from tests.conftest import run_git


def test_git_engine_commits_only_reports_and_pushes(git_workspace: tuple[Path, Path]) -> None:
    local, remote = git_workspace
    (local / "reports" / "2026-11-12.md").write_text("# Report\n", encoding="utf-8")
    (local / "untracked_noise.txt").write_text("should not be staged\n", encoding="utf-8")

    engine = GitEngine(local, allow_local_remote=True)
    result = engine.commit_and_push_reports(date(2026, 11, 12))

    assert result.committed is True
    assert result.pushed is True
    assert result.commit_message == "chore: daily GitHub heartbeat 2026-11-12"
    assert result.changed_files == ("reports/2026-11-12.md",)
    assert engine.is_path_clean(local / "reports" / "2026-11-12.md") is True

    # Remote received the commit and excluded untracked files outside reports/
    remote_log = run_git(remote, "log", "-1", "--pretty=%s|%an|%ae")
    assert remote_log == (
        "chore: daily GitHub heartbeat 2026-11-12|"
        "github-actions[bot]|"
        "41898282+github-actions[bot]@users.noreply.github.com"
    )
    remote_tree = run_git(remote, "ls-tree", "-r", "--name-only", "main")
    assert "reports/2026-11-12.md" in remote_tree
    assert "untracked_noise.txt" not in remote_tree

    # Re-running with no changes performs no duplicate commit
    second = engine.commit_and_push_reports(date(2026, 11, 12))
    assert second.committed is False
    assert second.pushed is False


def test_git_engine_refuses_preexisting_staged_non_report_files(
    git_workspace: tuple[Path, Path],
) -> None:
    local, _ = git_workspace
    (local / "secret.txt").write_text("oops\n", encoding="utf-8")
    run_git(local, "add", "secret.txt")
    (local / "reports" / "2026-11-12.md").write_text("# Report\n", encoding="utf-8")

    engine = GitEngine(local, allow_local_remote=True)
    with pytest.raises(GitEngineError, match="outside 'reports/'"):
        engine.commit_and_push_reports(date(2026, 11, 12))


def test_git_engine_rejects_non_https_or_mismatched_central_remote(
    git_workspace: tuple[Path, Path],
) -> None:
    local, _ = git_workspace
    run_git(local, "remote", "set-url", "origin", "http://github.com/acme/github-auto-push.git")
    engine = GitEngine(local, allow_local_remote=False)
    with pytest.raises(GitEngineError, match="Only HTTPS"):
        engine.ensure_safe_central_remote()

    run_git(local, "remote", "set-url", "origin", "https://github.com/acme/other-repo.git")
    with pytest.raises(GitEngineError, match="does not match expected repository"):
        engine.ensure_safe_central_remote(expected_repository="acme/github-auto-push")


def test_git_engine_detects_rejected_push_without_force_push(
    git_workspace: tuple[Path, Path],
) -> None:
    local, remote = git_workspace
    # Advance remote via a second clone to force a non-fast-forward rejection.
    other = local.parent / "other"
    run_git(local.parent, "clone", str(remote), str(other))
    run_git(other, "config", "user.name", "Other")
    run_git(other, "config", "user.email", "other@example.com")
    (other / "README.md").write_text("Remote change\n", encoding="utf-8")
    run_git(other, "add", "README.md")
    run_git(other, "commit", "-m", "remote advance")
    run_git(other, "push", "origin", "main")

    (local / "reports" / "2026-11-12.md").write_text("# Daily\n", encoding="utf-8")
    sleeps: list[float] = []
    engine = GitEngine(
        local,
        allow_local_remote=True,
        max_push_retries=3,
        sleep_fn=sleeps.append,
    )

    with pytest.raises(GitPushRejectedError, match="rejected"):
        engine.commit_and_push_reports(date(2026, 11, 12))
    assert sleeps == []


def test_git_engine_retries_transient_push_errors_only(
    git_workspace: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local, _ = git_workspace
    sleeps: list[float] = []
    engine = GitEngine(
        local,
        allow_local_remote=True,
        max_push_retries=2,
        backoff_base=1.5,
        sleep_fn=sleeps.append,
    )

    original_run_git = engine._run_git
    push_attempts = 0

    def flaky_run_git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        nonlocal push_attempts
        if args and args[0] == "push":
            push_attempts += 1
            if push_attempts < 3:
                return subprocess.CompletedProcess(
                    args=["git", *args],
                    returncode=128,
                    stdout="",
                    stderr="fatal: unable to access 'https://github.com/...': Connection reset by peer",
                )
        return original_run_git(args, check=check)

    monkeypatch.setattr(engine, "_run_git", flaky_run_git)
    (local / "reports" / "2026-11-12.md").write_text("# Retry OK\n", encoding="utf-8")

    res = engine.commit_and_push_reports(date(2026, 11, 12))
    assert res.pushed is True
    assert push_attempts == 3
    assert sleeps == [1.5, 3.0]

    # Exhausting transient retries raises GitPushTransientError
    def always_failing_push(
        args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "push":
            return subprocess.CompletedProcess(
                args=["git", *args],
                returncode=128,
                stdout="",
                stderr="fatal: Could not resolve host: github.com",
            )
        return original_run_git(args, check=check)

    sleeps.clear()
    monkeypatch.setattr(engine, "_run_git", always_failing_push)
    (local / "reports" / "2026-11-13.md").write_text("# Retry Fail\n", encoding="utf-8")
    with pytest.raises(GitPushTransientError, match="failed after retries"):
        engine.commit_and_push_reports(date(2026, 11, 13))
    assert sleeps == [1.5, 3.0]


def test_git_engine_strips_monitoring_token_from_subprocess_env(
    git_workspace: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local, _ = git_workspace
    monkeypatch.setenv("REPOSITORY_READ_TOKEN", "ghp_read_only_secret_12345")
    captured_envs: list[dict[str, str]] = []
    real_run = subprocess.run

    def spying_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs.get("env")
        if isinstance(env, dict):
            captured_envs.append(dict(env))
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "run", spying_run)
    (local / "reports" / "2026-11-12.md").write_text("# Env Check\n", encoding="utf-8")

    engine = GitEngine(local, allow_local_remote=True)
    res = engine.commit_and_push_reports(date(2026, 11, 12))
    assert res.pushed is True
    assert captured_envs
    assert all("REPOSITORY_READ_TOKEN" not in env for env in captured_envs)
    assert os.getenv("REPOSITORY_READ_TOKEN") == "ghp_read_only_secret_12345"

