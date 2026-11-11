"""Unit tests for daily Markdown report generation and same-day deduplication."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.github_client import CheckStatus, RepositoryCheckResult
from src.report_generator import ReportData, render_markdown_report, write_daily_report
from src.repository_manager import RepositorySpec

IST = ZoneInfo("Asia/Kolkata")


def _sample_report_data(
    ts: datetime,
    *,
    config_error: str | None = None,
    results: tuple[RepositoryCheckResult, ...] | None = None,
) -> ReportData:
    default_results = (
        RepositoryCheckResult(
            name="Core Service",
            full_name="acme/core",
            branch="main",
            status=CheckStatus.OK,
            accessible=True,
            visibility="private",
            latest_commit_sha="1234567890abcdef1234",
            last_push_utc="2026-11-12T10:00:00Z",
            message="Repository and configured branch are accessible (read-only check).",
        ),
        RepositoryCheckResult(
            name="Old Docs",
            full_name="acme/old-docs",
            branch="master",
            status=CheckStatus.ARCHIVED,
            accessible=True,
            archived=True,
            visibility="public",
            latest_commit_sha="fedcba0987654321",
            last_push_utc="2024-03-10T08:00:00Z",
            message="Repository is archived; read-only inspection only.",
        ),
        RepositoryCheckResult(
            name="Broken Repo",
            full_name="acme/broken",
            branch="missing-branch",
            status=CheckStatus.ERROR,
            accessible=False,
            visibility="public",
            error_category="MISSING_BRANCH",
            message="Configured branch 'missing-branch' was not found (HTTP 404).",
        ),
    )
    return ReportData(
        timestamp_ist=ts,
        total_configured=4,
        enabled_count=3,
        disabled_repositories=(
            RepositorySpec(name="Paused", full_name="acme/paused", branch="main", enabled=False),
        ),
        results=default_results if results is None else results,
        config_error=config_error,
        central_repository="acme/github-auto-push",
        extra_secrets=("super_secret_token_999",),
    )


def test_render_markdown_report_contains_required_fields() -> None:
    ts = datetime(2026, 11, 12, 20, 30, 5, tzinfo=IST)
    md = render_markdown_report(_sample_report_data(ts))

    assert "# Daily GitHub Activity Report" in md
    assert "12 November 2026 (`2026-11-12`)" in md
    assert "20:30:05 IST" in md
    assert "**Configured repositories:** 4 (3 enabled, 1 disabled)" in md
    assert "**Repositories checked:** 3" in md
    assert "**Successful:** 1" in md
    assert "**Archived:** 1" in md
    assert "**Failed:** 1" in md
    assert "`acme/core`" in md
    assert "`1234567890ab`" in md
    assert "2026-11-12T10:00:00Z" in md
    assert "`acme/old-docs`" in md
    assert "[MISSING_BRANCH]" in md
    assert "Skipped (`enabled: false`)" in md


def test_render_markdown_report_redacts_secrets_and_handles_config_error() -> None:
    ts = datetime(2026, 11, 12, 20, 30, 0, tzinfo=IST)
    data = _sample_report_data(
        ts,
        config_error="Invalid config containing super_secret_token_999 and ghp_abc1234567890xyz",
        results=(),
    )
    md = render_markdown_report(data)

    assert "Configuration Error" in md
    assert "super_secret_token_999" not in md
    assert "ghp_abc1234567890xyz" not in md
    assert "***REDACTED***" in md


def test_write_daily_report_same_day_deduplication(tmp_path: Path) -> None:
    ts1 = datetime(2026, 11, 12, 14, 0, 0, tzinfo=IST)
    first = write_daily_report(tmp_path, _sample_report_data(ts1), is_clean_in_git=False)
    assert first.action == "created"
    assert first.path == tmp_path / "2026-11-12.md"

    # Same-day rerun when report is committed & clean in git and statuses match -> unchanged
    ts2 = datetime(2026, 11, 12, 20, 30, 0, tzinfo=IST)
    second = write_daily_report(tmp_path, _sample_report_data(ts2), is_clean_in_git=True)
    assert second.action == "unchanged"
    assert "14:00:00 IST" in first.path.read_text(encoding="utf-8")

    # Same-day rerun when repo status changes -> updated
    changed_data = _sample_report_data(ts2, results=())
    third = write_daily_report(tmp_path, changed_data, is_clean_in_git=True)
    assert third.action == "updated"
    assert "20:30:00 IST" in first.path.read_text(encoding="utf-8")

