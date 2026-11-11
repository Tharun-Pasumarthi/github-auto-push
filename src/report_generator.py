"""Daily Markdown heartbeat report generator with same-day deduplication."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from .github_client import CheckStatus, RepositoryCheckResult
from .repository_manager import RepositorySpec
from .security import redact_secrets

_VOLATILE_TIME_RE = re.compile(r"^- \*\*Execution time \(IST\):\*\* .*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class ReportData:
    """All inputs needed to render the daily central heartbeat report."""

    timestamp_ist: datetime
    total_configured: int
    enabled_count: int
    disabled_repositories: Sequence[RepositorySpec]
    results: Sequence[RepositoryCheckResult]
    config_error: str | None = None
    central_repository: str | None = None
    extra_secrets: Iterable[str] = ()


@dataclass(frozen=True, slots=True)
class ReportWriteOutcome:
    """Outcome of generating and optionally writing today's Markdown report."""

    path: Path
    action: Literal["created", "updated", "unchanged"]
    content: str


def _md_cell(value: str | None, fallback: str = "—") -> str:
    if not value:
        return fallback
    return value.replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip() or fallback


def render_markdown_report(data: ReportData) -> str:
    """Render a human-readable, secret-free daily Markdown activity report."""
    ts = data.timestamp_ist
    iso_date = ts.strftime("%Y-%m-%d")
    human_date = f"{ts.day} {ts.strftime('%B %Y')}"
    ist_time = ts.strftime("%H:%M:%S %Z")

    ok_count = sum(1 for r in data.results if r.status == CheckStatus.OK)
    archived_count = sum(1 for r in data.results if r.status == CheckStatus.ARCHIVED)
    error_count = sum(1 for r in data.results if r.status == CheckStatus.ERROR)
    disabled_count = len(data.disabled_repositories)

    if data.config_error:
        execution_status = "Completed with configuration error (heartbeat generated)"
    elif error_count > 0:
        execution_status = "Completed with repository errors (heartbeat generated)"
    else:
        execution_status = "Successful"

    lines: list[str] = [
        "# Daily GitHub Activity Report",
        "",
        f"- **Date (IST):** {human_date} (`{iso_date}`)",
        f"- **Execution time (IST):** {ist_time}",
        f"- **Execution status:** {execution_status}",
    ]
    if data.central_repository:
        lines.append(f"- **Central repository:** `{_md_cell(data.central_repository)}`")
    lines.extend(
        [
            "- **Heartbeat mode:** Central repository daily report (monitored repositories untouched)",
            f"- **Configured repositories:** {data.total_configured} "
            f"({data.enabled_count} enabled, {disabled_count} disabled)",
            f"- **Repositories checked:** {len(data.results)}",
            f"- **Successful:** {ok_count}",
            f"- **Archived:** {archived_count}",
            f"- **Failed:** {error_count}",
            "",
            "> **Read-only boundary:** Monitored project repositories are inspected solely via "
            "GitHub REST API metadata `GET` requests. No files are cloned, modified, committed, "
            "or pushed to any monitored project repository.",
            "",
        ]
    )

    if data.config_error:
        lines.extend(
            [
                "## Configuration Error",
                "",
                "Unable to load `config/repositories.json`. The central heartbeat report was still "
                "generated so today's central activity record is preserved.",
                "",
                f"- **Error:** `{_md_cell(data.config_error)}`",
                "",
            ]
        )

    lines.extend(["## Repository Status", ""])
    if data.config_error:
        lines.append("Repository checks skipped due to configuration error.")
    elif not data.results:
        lines.append("No enabled repositories found in `config/repositories.json`.")
    else:
        lines.extend(
            [
                "| Name | Repository | Branch | Visibility | Status | Latest Commit | Last Push (UTC) |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for row in data.results:
            short_sha = f"`{row.latest_commit_sha[:12]}`" if row.latest_commit_sha else "—"
            visibility = _md_cell(row.visibility)
            last_push = _md_cell(row.last_push_utc)
            lines.append(
                f"| {_md_cell(row.name)} | `{_md_cell(row.full_name)}` | "
                f"`{_md_cell(row.branch)}` | {visibility} | **{row.status.value}** | "
                f"{short_sha} | {last_push} |"
            )

        lines.extend(["", "### Detailed Check Notes", ""])
        for row in data.results:
            sha_note = f" (commit `{row.latest_commit_sha}`)" if row.latest_commit_sha else ""
            cat_note = f" [{row.error_category}]" if row.error_category else ""
            lines.append(
                f"- **{_md_cell(row.name)}** (`{_md_cell(row.full_name)}` @ "
                f"`{_md_cell(row.branch)}`): **{row.status.value}**{cat_note} — "
                f"{row.message}{sha_note}"
            )

    if data.disabled_repositories:
        lines.extend(["", "## Disabled Repositories", ""])
        for spec in data.disabled_repositories:
            lines.append(
                f"- **{_md_cell(spec.name)}** (`{_md_cell(spec.full_name)}` @ "
                f"`{_md_cell(spec.branch)}`): Skipped (`enabled: false`)"
            )

    lines.extend(
        [
            "",
            "## Execution Summary",
            "",
            f"- Total configured repositories: {data.total_configured}",
            f"- Repositories checked: {len(data.results)}",
            f"- Accessible (OK): {ok_count}",
            f"- Archived: {archived_count}",
            f"- Failed checks: {error_count}",
            f"- Disabled (skipped): {disabled_count}",
            f"- Daily report file: `reports/{iso_date}.md`",
            "",
        ]
    )

    raw_markdown = "\n".join(lines)
    return redact_secrets(raw_markdown, data.extra_secrets)


def strip_volatile_fields(markdown: str) -> str:
    """Remove per-run clock time so identical same-day states compare equal."""
    return _VOLATILE_TIME_RE.sub("- **Execution time (IST):** <normalized>", markdown)


def write_daily_report(
    report_dir: Path,
    data: ReportData,
    *,
    is_clean_in_git: bool = False,
    force_update: bool = False,
) -> ReportWriteOutcome:
    """Write reports/YYYY-MM-DD.md unless an identical report is already committed today."""
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{data.timestamp_ist:%Y-%m-%d}.md"
    rendered = render_markdown_report(data)

    if not report_path.exists():
        report_path.write_text(rendered, encoding="utf-8")
        return ReportWriteOutcome(path=report_path, action="created", content=rendered)

    existing = report_path.read_text(encoding="utf-8")
    if (
        is_clean_in_git
        and not force_update
        and strip_volatile_fields(existing) == strip_volatile_fields(rendered)
    ):
        return ReportWriteOutcome(path=report_path, action="unchanged", content=existing)

    report_path.write_text(rendered, encoding="utf-8")
    return ReportWriteOutcome(path=report_path, action="updated", content=rendered)

