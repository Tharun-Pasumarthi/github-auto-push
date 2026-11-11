"""Unit tests for security helpers and secret-redacting logging."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.logger import close_logger_handlers, configure_logger
from src.security import (
    is_placeholder_repo,
    redact_secrets,
    resolve_monitoring_token,
    sanitize_git_env,
    validate_branch_name,
    validate_central_remote_url,
    validate_https_url,
    validate_repo_full_name,
)


def test_redact_secrets_scrubs_tokens_headers_and_urls() -> None:
    secret = "custom_secret_value_12345"
    raw = (
        f"Authorization: Bearer {secret} "
        "ghp_abcdefghijklmnopqrstuvwxyz123456 "
        "github_pat_11ABCDEFG_0123456789abcdef "
        "https://x-access-token:supersecret@github.com/org/repo.git"
    )
    redacted = redact_secrets(raw, extra_secrets=[secret])
    assert secret not in redacted
    assert "ghp_" not in redacted
    assert "github_pat_" not in redacted
    assert "supersecret" not in redacted
    assert redacted.count("***REDACTED***") >= 4


def test_validate_https_url_enforces_https_host_and_no_credentials() -> None:
    assert (
        validate_https_url("https://api.github.com/", allowed_hosts={"api.github.com"})
        == "https://api.github.com"
    )
    with pytest.raises(ValueError, match="Only HTTPS"):
        validate_https_url("http://api.github.com")
    with pytest.raises(ValueError, match="Embedded credentials"):
        validate_https_url("https://user:pass@api.github.com")
    with pytest.raises(ValueError, match="allowed HTTPS hosts"):
        validate_https_url("https://evil.example.com", allowed_hosts={"api.github.com"})


def test_validate_repo_full_name_and_branch_name() -> None:
    assert validate_repo_full_name("octocat/Hello-World") == ("octocat", "Hello-World")
    assert validate_branch_name("feature/daily-sync") == "feature/daily-sync"

    for bad_repo in ("", "invalid", "a/b/c", "../secret", "owner/repo name", " owner/repo"):
        with pytest.raises(ValueError):
            validate_repo_full_name(bad_repo)

    for bad_branch in ("", "main..dev", "/main", "main/", "feat branch", "ref.lock", "a@{b}"):
        with pytest.raises(ValueError):
            validate_branch_name(bad_branch)


def test_is_placeholder_repo() -> None:
    assert is_placeholder_repo("YOUR_GITHUB_USERNAME/YOUR_REPOSITORY")
    assert not is_placeholder_repo("octocat/Hello-World")


def test_resolve_monitoring_token_prefers_read_token() -> None:
    token, source = resolve_monitoring_token(
        {"REPOSITORY_READ_TOKEN": "read_tok", "GITHUB_TOKEN": "central_tok"}
    )
    assert token == "read_tok"
    assert source == "REPOSITORY_READ_TOKEN"

    token, source = resolve_monitoring_token({"REPOSITORY_READ_TOKEN": "  ", "GITHUB_TOKEN": "pub"})
    assert token == "pub"
    assert source == "GITHUB_TOKEN"

    token, source = resolve_monitoring_token({})
    assert token is None
    assert source == "unauthenticated"


def test_sanitize_git_env_strips_monitoring_token() -> None:
    cleaned = sanitize_git_env(
        {
            "REPOSITORY_READ_TOKEN": "secret-read-token",
            "GITHUB_TOKEN": "central-token",
            "PATH": "/usr/bin",
        }
    )
    assert "REPOSITORY_READ_TOKEN" not in cleaned
    assert cleaned["GITHUB_TOKEN"] == "central-token"
    assert cleaned["GIT_TERMINAL_PROMPT"] == "0"


def test_validate_central_remote_url() -> None:
    assert (
        validate_central_remote_url(
            "https://github.com/acme/github-auto-push.git",
            expected_repository="acme/github-auto-push",
        )
        == "https://github.com/acme/github-auto-push.git"
    )
    with pytest.raises(ValueError, match="does not match expected"):
        validate_central_remote_url(
            "https://github.com/acme/other-repo.git",
            expected_repository="acme/github-auto-push",
        )
    with pytest.raises(ValueError, match="Only HTTPS"):
        validate_central_remote_url("http://github.com/acme/github-auto-push.git")


def test_logger_redacts_messages_and_tracebacks(tmp_path: Path) -> None:
    secret = "my_very_secret_token_98765"
    logger, log_path = configure_logger(
        tmp_path,
        run_date=date(2026, 11, 12),
        extra_secrets=[secret],
        name="test_redaction_logger",
    )
    try:
        logger.info("Connecting with token=%s", secret)
        try:
            raise RuntimeError(f"Failure carrying {secret}")
        except RuntimeError:
            logger.exception("Caught exception")
    finally:
        close_logger_handlers(logger)

    content = log_path.read_text(encoding="utf-8")
    assert secret not in content
    assert "***REDACTED***" in content

