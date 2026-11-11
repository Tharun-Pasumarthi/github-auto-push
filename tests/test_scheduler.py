"""End-to-end scheduler orchestration tests with mocked GitHub API and local bare Git remote."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.git_engine import GitEngine
from src.github_client import GitHubClient
from src.scheduler import run_scheduler
from tests.conftest import run_git
from tests.test_github_client import FakeResponse, FakeSession

IST = ZoneInfo("Asia/Kolkata")


def test_scheduler_end_to_end_checks_commits_and_pushes_and_avoids_duplicate(
    git_workspace: tuple[Path, Path],
) -> None:
    local, remote = git_workspace
    config_dir = local / "config"
    config_dir.mkdir()
    config_path = config_dir / "repositories.json"
    config_path.write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "name": "TaskCall",
                        "full_name": "acme/TaskCall",
                        "branch": "main",
                        "enabled": True,
                    },
                    {
                        "name": "Missing",
                        "full_name": "acme/Missing",
                        "branch": "main",
                        "enabled": True,
                    },
                    {
                        "name": "Paused",
                        "full_name": "acme/Paused",
                        "branch": "main",
                        "enabled": False,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    def make_client(token: str | None, logger: object) -> GitHubClient:
        session = FakeSession(
            {
                "/repos/acme/TaskCall": [
                    FakeResponse(
                        200,
                        {"archived": False, "private": True, "pushed_at": "2026-11-12T10:00:00Z"},
                    )
                ],
                "/repos/acme/TaskCall/branches/main": [
                    FakeResponse(200, {"commit": {"sha": "aabbccddeeff00112233"}})
                ],
                "/repos/acme/Missing": [FakeResponse(404, {"message": "Not Found"})],
            }
        )
        return GitHubClient(token=token, session=session, logger=logger)  # type: ignore[arg-type]

    summary_file = local / "step_summary.md"
    env = {
        "REPOSITORY_READ_TOKEN": "ghp_private_read_secret_0001",
        "GITHUB_TOKEN": "ghs_central_actions_token_0002",
        "GITHUB_STEP_SUMMARY": str(summary_file),
    }

    ts1 = datetime(2026, 11, 12, 20, 30, 0, tzinfo=IST)
    outcome1 = run_scheduler(
        repo_root=local,
        config_path=config_path,
        reports_dir=local / "reports",
        logs_dir=local / "logs",
        push=True,
        now_ist=ts1,
        env=env,
        client_factory=make_client,
        git_engine_factory=lambda root, logger: GitEngine(
            root, allow_local_remote=True, logger=logger
        ),
    )

    assert outcome1.exit_code == 0
    assert outcome1.report_outcome is not None
    assert outcome1.report_outcome.action == "created"
    assert outcome1.git_result is not None
    assert outcome1.git_result.committed is True
    assert outcome1.git_result.pushed is True

    report_text = outcome1.report_outcome.path.read_text(encoding="utf-8")
    assert "`acme/TaskCall`" in report_text
    assert "`acme/Missing`" in report_text
    assert "ghp_private_read_secret_0001" not in report_text
    assert "ghs_central_actions_token_0002" not in report_text

    log_text = outcome1.log_path.read_text(encoding="utf-8")
    assert "ghp_private_read_secret_0001" not in log_text
    assert "ghs_central_actions_token_0002" not in log_text
    assert "SUCCESS" in log_text
    assert "Pushed commit" in summary_file.read_text(encoding="utf-8")

    # Same-day rerun with unchanged results avoids redundant duplicate commit
    ts2 = datetime(2026, 11, 12, 21, 0, 0, tzinfo=IST)
    outcome2 = run_scheduler(
        repo_root=local,
        config_path=config_path,
        reports_dir=local / "reports",
        logs_dir=local / "logs",
        push=True,
        now_ist=ts2,
        env=env,
        client_factory=make_client,
        git_engine_factory=lambda root, logger: GitEngine(
            root, allow_local_remote=True, logger=logger
        ),
    )
    assert outcome2.exit_code == 0
    assert outcome2.report_outcome is not None
    assert outcome2.report_outcome.action == "unchanged"
    assert outcome2.git_result is not None
    assert outcome2.git_result.committed is False
    assert run_git(remote, "rev-list", "--count", "main") == "2"


def test_scheduler_config_error_still_pushes_heartbeat(git_workspace: tuple[Path, Path]) -> None:
    local, _ = git_workspace
    bad_cfg = local / "config" / "repositories.json"
    bad_cfg.parent.mkdir(parents=True)
    bad_cfg.write_text("{broken json", encoding="utf-8")

    outcome = run_scheduler(
        repo_root=local,
        config_path=bad_cfg,
        reports_dir=local / "reports",
        logs_dir=local / "logs",
        push=True,
        now_ist=datetime(2026, 11, 12, 20, 30, 0, tzinfo=IST),
        env={},
        git_engine_factory=lambda root, logger: GitEngine(
            root, allow_local_remote=True, logger=logger
        ),
    )

    assert outcome.exit_code == 0
    assert outcome.config_error is not None
    assert outcome.report_outcome is not None
    assert outcome.report_outcome.action == "created"
    assert outcome.git_result is not None
    assert outcome.git_result.pushed is True
    assert "Configuration Error" in outcome.report_outcome.path.read_text(encoding="utf-8")


def test_scheduler_fails_loudly_when_central_push_is_rejected(
    git_workspace: tuple[Path, Path],
) -> None:
    local, remote = git_workspace
    # Advance remote so local push is rejected (non-fast-forward).
    other = local.parent / "other"
    run_git(local.parent, "clone", str(remote), str(other))
    run_git(other, "config", "user.name", "Other")
    run_git(other, "config", "user.email", "other@example.com")
    (other / "README.md").write_text("ahead\n", encoding="utf-8")
    run_git(other, "add", "README.md")
    run_git(other, "commit", "-m", "ahead")
    run_git(other, "push", "origin", "main")

    cfg = local / "config" / "repositories.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"repositories": []}), encoding="utf-8")

    outcome = run_scheduler(
        repo_root=local,
        config_path=cfg,
        reports_dir=local / "reports",
        logs_dir=local / "logs",
        push=True,
        now_ist=datetime(2026, 11, 12, 20, 30, 0, tzinfo=IST),
        env={},
        git_engine_factory=lambda root, logger: GitEngine(
            root, allow_local_remote=True, logger=logger
        ),
    )

    assert outcome.exit_code == 1
    assert outcome.push_error is not None
    assert "rejected" in outcome.push_error.lower()
    assert "FAILED" in outcome.log_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "cmd",
    [
        [sys.executable, "-m", "src.scheduler", "--help"],
        [sys.executable, "src/scheduler.py", "--help"],
    ],
)
def test_scheduler_cli_entrypoints_work(cmd: list[str]) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    assert "--push" in proc.stdout

