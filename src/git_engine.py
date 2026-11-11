"""Central repository Git commit and push engine with strict safety guards."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .security import redact_secrets, sanitize_git_env, validate_central_remote_url

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"

_REJECTION_PATTERNS = (
    "rejected",
    "non-fast-forward",
    "fetch first",
    "protected branch",
    "permission",
    "denied",
    "403",
    "401",
    "authentication failed",
    "cannot lock ref",
    "remote rejected",
    "pre-receive hook declined",
)

_TRANSIENT_PATTERNS = (
    "could not resolve host",
    "connection timed out",
    "operation timed out",
    "connection reset",
    "failed to connect",
    "unable to access",
    "returned error: 5",
    "http 50",
    "tls",
    "ssl",
    "eof",
    "recv failure",
)


class GitEngineError(RuntimeError):
    """Base error for central Git operations."""


class GitPushRejectedError(GitEngineError):
    """Raised when the central remote rejects a push (non-retryable)."""


class GitPushTransientError(GitEngineError):
    """Raised when pushing fails after bounded retries on transient network issues."""


@dataclass(frozen=True, slots=True)
class GitRunResult:
    """Outcome of staging, committing, and pushing central daily reports."""

    committed: bool
    pushed: bool
    branch: str
    commit_sha: str | None = None
    commit_message: str | None = None
    changed_files: tuple[str, ...] = ()
    message: str = ""


def _classify_push_failure(error_output: str) -> str:
    lowered = error_output.lower()
    if any(pattern in lowered for pattern in _REJECTION_PATTERNS):
        return "rejected"
    if any(pattern in lowered for pattern in _TRANSIENT_PATTERNS):
        return "transient"
    return "rejected"


class GitEngine:
    """Commits and pushes daily heartbeat reports strictly to the central repository."""

    def __init__(
        self,
        repo_root: Path,
        *,
        reports_dir_name: str = "reports",
        remote_name: str = "origin",
        bot_name: str = BOT_NAME,
        bot_email: str = BOT_EMAIL,
        allow_local_remote: bool = False,
        max_push_retries: int = 3,
        backoff_base: float = 2.0,
        command_timeout: float = 60.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.reports_dir_name = reports_dir_name.strip("/\\")
        self.remote_name = remote_name
        self.bot_name = bot_name
        self.bot_email = bot_email
        self.allow_local_remote = allow_local_remote
        self.max_push_retries = max(0, max_push_retries)
        self.backoff_base = max(0.0, backoff_base)
        self.command_timeout = command_timeout
        self.sleep_fn = sleep_fn
        self.logger = logger

    def _run_git(
        self,
        args: list[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["git", *args]
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.repo_root,
                env=sanitize_git_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.command_timeout,
                check=False,
            )
        except OSError as exc:
            raise GitEngineError(f"Failed to invoke git ({args[0]}): {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitEngineError(
                f"Git command 'git {' '.join(args)}' timed out after {self.command_timeout}s"
            ) from exc

        if check and proc.returncode != 0:
            detail = redact_secrets((proc.stderr or proc.stdout or "").strip())
            raise GitEngineError(
                f"Git command 'git {' '.join(args)}' failed (exit {proc.returncode}): {detail}"
            )
        return proc

    def is_git_repository(self) -> bool:
        """Return True if repo_root is inside a Git working tree."""
        try:
            proc = self._run_git(["rev-parse", "--is-inside-work-tree"], check=False)
        except GitEngineError:
            return False
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def is_path_clean(self, file_path: Path) -> bool:
        """Return True if file_path is tracked in Git and has no local modifications."""
        if not self.is_git_repository():
            return False
        try:
            rel = file_path.resolve().relative_to(self.repo_root).as_posix()
        except ValueError:
            return False

        tracked = self._run_git(["ls-files", "--error-unmatch", "--", rel], check=False)
        if tracked.returncode != 0:
            return False

        status = self._run_git(["status", "--porcelain", "--", rel], check=False)
        return status.returncode == 0 and not status.stdout.strip()

    def ensure_safe_central_remote(self, expected_repository: str | None = None) -> str:
        """Verify that remote points safely to the central repository."""
        proc = self._run_git(["remote", "get-url", self.remote_name])
        raw_url = proc.stdout.strip()
        try:
            return validate_central_remote_url(
                raw_url,
                expected_repository=expected_repository,
                allow_local_remote=self.allow_local_remote,
            )
        except ValueError as exc:
            raise GitEngineError(f"Unsafe central remote '{self.remote_name}': {exc}") from exc

    def resolve_target_branch(self, explicit_branch: str | None = None) -> str:
        """Determine target branch without guessing or operating on detached HEAD."""
        if explicit_branch and explicit_branch.strip():
            return explicit_branch.strip()

        ref_name = (os.getenv("GITHUB_REF_NAME") or "").strip()
        if ref_name:
            return ref_name

        proc = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        branch = proc.stdout.strip()
        if not branch or branch == "HEAD":
            raise GitEngineError(
                "Repository is in detached HEAD state and GITHUB_REF_NAME is not set"
            )
        return branch

    def configure_bot_identity(self) -> None:
        """Configure repository-local Git identity for automated commits."""
        self._run_git(["config", "user.name", self.bot_name])
        self._run_git(["config", "user.email", self.bot_email])

    def _get_staged_files(self) -> list[str]:
        proc = self._run_git(["diff", "--cached", "--name-only"])
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    def _assert_only_reports_staged(self, staged_files: list[str], stage_desc: str) -> None:
        prefix = f"{self.reports_dir_name}/"
        unexpected = [path for path in staged_files if not path.startswith(prefix)]
        if unexpected:
            raise GitEngineError(
                f"Refusing to commit {stage_desc} files outside '{prefix}': {unexpected}"
            )

    def _push_with_retries(self, branch: str) -> None:
        for attempt in range(self.max_push_retries + 1):
            proc = self._run_git(
                ["push", self.remote_name, f"HEAD:{branch}"],
                check=False,
            )
            if proc.returncode == 0:
                return

            detail = redact_secrets((proc.stderr or proc.stdout or "").strip())
            kind = _classify_push_failure(detail)
            if kind == "rejected":
                raise GitPushRejectedError(
                    f"Central push to '{self.remote_name}/{branch}' was rejected: {detail}"
                )

            if attempt < self.max_push_retries:
                delay = self.backoff_base * (2**attempt)
                if self.logger is not None:
                    self.logger.warning(
                        "Transient git push failure (%s). Retry %d/%d in %.1fs.",
                        detail,
                        attempt + 1,
                        self.max_push_retries,
                        delay,
                    )
                self.sleep_fn(delay)
                continue

            raise GitPushTransientError(
                f"Central push to '{self.remote_name}/{branch}' failed after retries: {detail}"
            )

    def commit_and_push_reports(
        self,
        run_date: date,
        *,
        summary_body: str | None = None,
        target_branch: str | None = None,
        expected_repository: str | None = None,
    ) -> GitRunResult:
        """Stage reports/, commit if changed, and push strictly to the central remote."""
        if not self.is_git_repository():
            raise GitEngineError(f"Not a Git repository: {self.repo_root}")

        expected_repo = expected_repository
        if expected_repo is None:
            expected_repo = (os.getenv("GITHUB_REPOSITORY") or "").strip() or None

        self.ensure_safe_central_remote(expected_repository=expected_repo)
        branch = self.resolve_target_branch(explicit_branch=target_branch)

        pre_staged = self._get_staged_files()
        self._assert_only_reports_staged(pre_staged, "pre-existing staged")

        self.configure_bot_identity()
        self._run_git(["add", "--", f"{self.reports_dir_name}/"])

        staged = self._get_staged_files()
        self._assert_only_reports_staged(staged, "staged")
        if not staged:
            return GitRunResult(
                committed=False,
                pushed=False,
                branch=branch,
                message="No report changes detected; skipping commit and push.",
            )

        commit_subject = f"chore: daily GitHub heartbeat {run_date:%Y-%m-%d}"
        commit_args = ["commit", "-m", commit_subject]
        if summary_body and summary_body.strip():
            commit_args.extend(["-m", summary_body.strip()])
        self._run_git(commit_args)

        sha_proc = self._run_git(["rev-parse", "HEAD"])
        commit_sha = sha_proc.stdout.strip()

        self._push_with_retries(branch)

        return GitRunResult(
            committed=True,
            pushed=True,
            branch=branch,
            commit_sha=commit_sha,
            commit_message=commit_subject,
            changed_files=tuple(staged),
            message=f"Committed {commit_sha[:12]} and pushed to {self.remote_name}/{branch}.",
        )

