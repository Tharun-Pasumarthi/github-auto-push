#!/usr/bin/env python3
"""Main orchestration entrypoint for the GitHub Auto-Push Scheduler."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.git_engine import GitEngine, GitEngineError, GitRunResult
from src.github_client import CheckStatus, GitHubClient, RepositoryCheckResult
from src.logger import close_logger_handlers, configure_logger
from src.report_generator import ReportData, ReportWriteOutcome, write_daily_report
from src.repository_manager import ConfigurationError, RepositoryConfig, load_repositories_config
from src.security import redact_secrets, resolve_monitoring_token

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "repositories.json"
DEFAULT_REPORTS_DIR = ROOT / "reports"
DEFAULT_LOGS_DIR = ROOT / "logs"
IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True, slots=True)
class SchedulerOutcome:
    """Structured result of a complete scheduler execution."""

    exit_code: int
    timestamp_ist: datetime
    report_outcome: ReportWriteOutcome | None
    git_result: GitRunResult | None
    results: tuple[RepositoryCheckResult, ...]
    config_error: str | None
    push_error: str | None
    log_path: Path


def _collect_env_secrets(env: Mapping[str, str]) -> tuple[str, ...]:
    candidates = []
    for key in ("REPOSITORY_READ_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        value = (env.get(key) or "").strip()
        if value:
            candidates.append(value)
    return tuple(dict.fromkeys(candidates))


def _write_github_step_summary(
    env: Mapping[str, str],
    outcome: SchedulerOutcome,
    extra_secrets: tuple[str, ...],
) -> None:
    summary_file = (env.get("GITHUB_STEP_SUMMARY") or "").strip()
    if not summary_file:
        return

    ok_count = sum(1 for r in outcome.results if r.status == CheckStatus.OK)
    archived_count = sum(1 for r in outcome.results if r.status == CheckStatus.ARCHIVED)
    error_count = sum(1 for r in outcome.results if r.status == CheckStatus.ERROR)

    push_state = "Skipped (--push disabled)"
    if outcome.push_error:
        push_state = f"FAILED ({outcome.push_error})"
    elif outcome.git_result is not None:
        if outcome.git_result.pushed:
            sha = (outcome.git_result.commit_sha or "")[:12]
            push_state = f"Pushed commit `{sha}` to `{outcome.git_result.branch}`"
        else:
            push_state = outcome.git_result.message

    report_rel = outcome.report_outcome.path.name if outcome.report_outcome else "N/A"
    report_action = outcome.report_outcome.action if outcome.report_outcome else "none"

    summary_lines = [
        "### Daily GitHub Heartbeat Summary",
        "",
        f"- **Date (IST):** {outcome.timestamp_ist:%Y-%m-%d %H:%M:%S %Z}",
        f"- **Repositories checked:** {len(outcome.results)} "
        f"(OK: {ok_count}, Archived: {archived_count}, Errors: {error_count})",
        f"- **Daily report:** `reports/{report_rel}` ({report_action})",
        f"- **Central Git push:** {push_state}",
        "",
    ]
    text = redact_secrets("\n".join(summary_lines), extra_secrets)
    try:
        with Path(summary_file).open("a", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        pass


def run_scheduler(
    *,
    repo_root: Path = ROOT,
    config_path: Path = DEFAULT_CONFIG_PATH,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    logs_dir: Path = DEFAULT_LOGS_DIR,
    push: bool = False,
    force_report_update: bool = False,
    target_branch: str | None = None,
    now_ist: datetime | None = None,
    verbose: bool = False,
    env: Mapping[str, str] | None = None,
    client_factory: Callable[[str | None, logging.Logger], GitHubClient] | None = None,
    git_engine_factory: Callable[[Path, logging.Logger], GitEngine] | None = None,
) -> SchedulerOutcome:
    """Execute one full monitoring, reporting, and central commit/push cycle."""
    effective_env = os.environ if env is None else env
    timestamp = (now_ist or datetime.now(IST)).astimezone(IST)
    extra_secrets = _collect_env_secrets(effective_env)

    log_level = logging.DEBUG if verbose else logging.INFO
    logger, log_path = configure_logger(
        logs_dir,
        run_date=timestamp.date(),
        level=log_level,
        extra_secrets=extra_secrets,
    )

    central_repo = (effective_env.get("GITHUB_REPOSITORY") or "").strip() or None
    token, token_source = resolve_monitoring_token(effective_env)

    report_outcome: ReportWriteOutcome | None = None
    git_result: GitRunResult | None = None
    results: list[RepositoryCheckResult] = []
    config_error: str | None = None
    push_error: str | None = None
    exit_code = 0

    try:
        logger.info(
            "Starting GitHub Auto-Push Scheduler at %s (%s UTC).",
            timestamp.strftime("%Y-%m-%d %H:%M:%S %Z"),
            timestamp.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S"),
        )
        logger.info("Central repository: %s", central_repo or repo_root.name)
        logger.info("Read-only API credential source: %s", token_source)
        if not token:
            logger.warning(
                "No monitoring token configured; private repositories will not be accessible."
            )

        repo_config: RepositoryConfig | None = None
        try:
            repo_config = load_repositories_config(config_path)
            logger.info(
                "Loaded configuration: %d total (%d enabled, %d disabled).",
                repo_config.total_count,
                len(repo_config.enabled_repositories),
                len(repo_config.disabled_repositories),
            )
        except ConfigurationError as exc:
            config_error = redact_secrets(f"{type(exc).__name__}: {exc}", extra_secrets)
            logger.error("Configuration error: %s", config_error)
        except Exception as exc:  # noqa: BLE001
            config_error = redact_secrets(f"{type(exc).__name__}: {exc}", extra_secrets)
            logger.error("Unexpected configuration failure: %s", config_error)

        if repo_config is not None and repo_config.enabled_repositories:
            client = (
                client_factory(token, logger)
                if client_factory is not None
                else GitHubClient(token=token, logger=logger)
            )
            try:
                for spec in repo_config.enabled_repositories:
                    logger.info(
                        "Checking repository '%s' (%s @ %s)...",
                        spec.name,
                        spec.full_name,
                        spec.branch,
                    )
                    res = client.check_repository(spec)
                    results.append(res)
                    sha_display = res.latest_commit_sha[:12] if res.latest_commit_sha else "N/A"
                    if res.status == CheckStatus.ERROR:
                        logger.error(
                            "Check FAILED for %s [%s]: %s",
                            spec.full_name,
                            res.error_category or "ERROR",
                            res.message,
                        )
                    elif res.status == CheckStatus.ARCHIVED:
                        logger.warning(
                            "Repository %s is ARCHIVED (branch=%s, sha=%s).",
                            spec.full_name,
                            spec.branch,
                            sha_display,
                        )
                    else:
                        logger.info(
                            "Check OK for %s (branch=%s, sha=%s, pushed=%s).",
                            spec.full_name,
                            spec.branch,
                            sha_display,
                            res.last_push_utc or "unknown",
                        )
            finally:
                client.close()
        elif repo_config is not None:
            logger.info("No enabled repositories configured; generating central heartbeat only.")

        git_engine = (
            git_engine_factory(repo_root, logger)
            if git_engine_factory is not None
            else GitEngine(
                repo_root,
                reports_dir_name=reports_dir.name,
                logger=logger,
            )
        )

        daily_report_path = reports_dir / f"{timestamp:%Y-%m-%d}.md"
        is_clean_in_git = git_engine.is_path_clean(daily_report_path)

        report_data = ReportData(
            timestamp_ist=timestamp,
            total_configured=repo_config.total_count if repo_config else 0,
            enabled_count=len(repo_config.enabled_repositories) if repo_config else 0,
            disabled_repositories=repo_config.disabled_repositories if repo_config else (),
            results=tuple(results),
            config_error=config_error,
            central_repository=central_repo,
            extra_secrets=extra_secrets,
        )
        report_outcome = write_daily_report(
            reports_dir,
            report_data,
            is_clean_in_git=is_clean_in_git,
            force_update=force_report_update,
        )
        logger.info(
            "Daily report %s: %s",
            report_outcome.action,
            report_outcome.path,
        )

        if push:
            ok_count = sum(1 for r in results if r.status == CheckStatus.OK)
            archived_count = sum(1 for r in results if r.status == CheckStatus.ARCHIVED)
            error_count = sum(1 for r in results if r.status == CheckStatus.ERROR)
            summary_body = (
                f"Repositories checked: {len(results)} | "
                f"Accessible: {ok_count} | Archived: {archived_count} | Errors: {error_count}"
            )
            try:
                git_result = git_engine.commit_and_push_reports(
                    timestamp.date(),
                    summary_body=summary_body,
                    target_branch=target_branch,
                    expected_repository=central_repo,
                )
                logger.info("Central Git engine: %s", git_result.message)
            except GitEngineError as exc:
                push_error = redact_secrets(str(exc), extra_secrets)
                logger.critical("Central Git push failed: %s", push_error)
                exit_code = 1
        else:
            logger.info("Git commit/push skipped (run with --push to commit and push report).")

    except Exception as exc:  # noqa: BLE001
        fatal_msg = redact_secrets(f"{type(exc).__name__}: {exc}", extra_secrets)
        logger.critical("Unhandled scheduler failure: %s", fatal_msg, exc_info=True)
        push_error = push_error or fatal_msg
        exit_code = 1
    finally:
        final_outcome = SchedulerOutcome(
            exit_code=exit_code,
            timestamp_ist=timestamp,
            report_outcome=report_outcome,
            git_result=git_result,
            results=tuple(results),
            config_error=config_error,
            push_error=push_error,
            log_path=log_path,
        )
        _write_github_step_summary(effective_env, final_outcome, extra_secrets)
        if exit_code == 0:
            logger.info("Scheduler finished with status SUCCESS (exit=0).")
        else:
            logger.error("Scheduler finished with status FAILED (exit=%d).", exit_code)
        close_logger_handlers(logger)

    return final_outcome


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser for the scheduler."""
    parser = argparse.ArgumentParser(
        description="Run read-only multi-repository checks and central GitHub heartbeat push."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to repositories.json configuration file.",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=DEFAULT_REPORTS_DIR,
        help="Directory where daily Markdown reports are written.",
    )
    parser.add_argument(
        "--logs-dir",
        type=Path,
        default=DEFAULT_LOGS_DIR,
        help="Directory where structured execution logs are written.",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Stage reports/, commit changes, and push to the central Git repository.",
    )
    parser.add_argument(
        "--force-report-update",
        action="store_true",
        help="Rewrite today's report timestamp even if repository results are unchanged.",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help="Optional target branch name for central push (defaults to GITHUB_REF_NAME or HEAD).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    args = build_parser().parse_args(argv)
    outcome = run_scheduler(
        repo_root=ROOT,
        config_path=args.config.resolve(),
        reports_dir=args.reports_dir.resolve(),
        logs_dir=args.logs_dir.resolve(),
        push=args.push,
        force_report_update=args.force_report_update,
        target_branch=args.branch,
        verbose=args.verbose,
    )
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
