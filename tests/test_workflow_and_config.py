"""Validation tests for repository JSON config and GitHub Actions YAML workflows."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from src.repository_manager import load_repositories_config

REPO_ROOT = Path(__file__).resolve().parents[1]
SHA_USES_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")


def test_default_repositories_json_is_valid() -> None:
    cfg = load_repositories_config(REPO_ROOT / "config" / "repositories.json")
    assert cfg.total_count >= 1


def test_daily_sync_workflow_schedule_security_and_pins() -> None:
    workflow_path = REPO_ROOT / ".github" / "workflows" / "daily-sync.yml"
    data = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    # PyYAML parses bare `on:` as boolean True unless quoted.
    triggers = data.get("on") if "on" in data else data[True]
    assert triggers["schedule"] == [{"cron": "0 15 * * *"}]
    assert "workflow_dispatch" in triggers

    assert data["permissions"] == {"contents": "write"}
    assert data["concurrency"]["cancel-in-progress"] is False

    job = data["jobs"]["heartbeat"]
    assert job["timeout-minutes"] <= 15

    steps = job["steps"]
    uses_values = [step["uses"] for step in steps if "uses" in step]
    assert len(uses_values) == 3
    for ref in uses_values:
        assert SHA_USES_RE.match(ref), f"Action reference is not pinned to a 40-char SHA: {ref}"

    run_step = next(s for s in steps if "run" in s and "src.scheduler" in s["run"])
    assert "--push" in run_step["run"]
    assert "REPOSITORY_READ_TOKEN" in run_step["env"]
    assert "GITHUB_TOKEN" in run_step["env"]

    upload_step = next(s for s in steps if "upload-artifact" in s.get("uses", ""))
    assert upload_step["if"] == "always()"


def test_ci_workflow_is_read_only_and_sha_pinned() -> None:
    ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    data = yaml.safe_load(ci_path.read_text(encoding="utf-8"))

    assert data["permissions"] == {"contents": "read"}
    for step in data["jobs"]["test"]["steps"]:
        if "uses" in step:
            assert SHA_USES_RE.match(step["uses"])

