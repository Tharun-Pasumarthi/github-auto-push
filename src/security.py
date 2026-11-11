"""Security helpers: HTTPS enforcement, input validation, token separation, and redaction."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from urllib.parse import quote, urlparse

DEFAULT_API_BASE = "https://api.github.com"
GITHUB_HOST = "github.com"
TOKEN_ENV_PRIMARY = "REPOSITORY_READ_TOKEN"
TOKEN_ENV_FALLBACK = "GITHUB_TOKEN"

_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,38}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_GITHUB_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,})\b"
)
_AUTH_HEADER_RE = re.compile(
    r"(?i)(Authorization\s*:\s*(?:Bearer|Basic|Token)\s+)([^\s,;\"']+)"
)
_URL_CREDENTIAL_RE = re.compile(r"(https?://[^/\s:@]+:)([^@\s/]+)(@)")

_INVALID_BRANCH_CHARS = set(" \t\r\n~^:?*[\\")
_PLACEHOLDER_MARKERS = (
    "YOUR_GITHUB_USERNAME",
    "YOUR_REPOSITORY",
    "YOUR_ORG",
    "EXAMPLE_OWNER",
)


def redact_secrets(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Scrub tokens, authorization headers, and URL credentials from text."""
    if not text:
        return text

    sanitized = text
    for secret in extra_secrets:
        if not secret or len(secret.strip()) < 4:
            continue
        cleaned = secret.strip()
        sanitized = sanitized.replace(cleaned, "***REDACTED***")
        encoded = quote(cleaned, safe="")
        if encoded and encoded != cleaned:
            sanitized = sanitized.replace(encoded, "***REDACTED***")

    sanitized = _GITHUB_TOKEN_RE.sub("***REDACTED***", sanitized)
    sanitized = _AUTH_HEADER_RE.sub(r"\1***REDACTED***", sanitized)
    sanitized = _URL_CREDENTIAL_RE.sub(r"\1***REDACTED***\3", sanitized)
    return sanitized


def validate_https_url(
    url: str,
    *,
    allowed_hosts: set[str] | None = None,
) -> str:
    """Ensure a URL uses HTTPS with a valid host and no embedded credentials."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL must be a non-empty string")

    cleaned = url.strip().rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() != "https":
        raise ValueError(f"Only HTTPS URLs are permitted; got scheme '{parsed.scheme or 'none'}'")
    if not parsed.hostname:
        raise ValueError("URL must include a valid hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Embedded credentials in URLs are not permitted")

    if allowed_hosts is not None:
        normalized_allowed = {h.lower() for h in allowed_hosts}
        if parsed.hostname.lower() not in normalized_allowed:
            raise ValueError(
                f"Host '{parsed.hostname}' is not in allowed HTTPS hosts: {sorted(normalized_allowed)}"
            )
    return cleaned


def validate_repo_full_name(full_name: str) -> tuple[str, str]:
    """Validate a GitHub 'owner/repo' identifier against traversal and injection."""
    if not isinstance(full_name, str):
        raise ValueError("Repository full_name must be a string")

    candidate = full_name.strip()
    if not candidate or candidate != full_name:
        raise ValueError("Repository full_name must not be empty or have surrounding whitespace")
    if any(ch.isspace() for ch in candidate) or candidate.count("/") != 1:
        raise ValueError(f"Invalid repository full_name '{full_name}'; expected 'owner/repo'")

    owner, repo = candidate.split("/", 1)
    if owner in {".", ".."} or repo in {".", ".."} or ".." in candidate:
        raise ValueError(f"Invalid repository full_name '{full_name}'")
    if not _OWNER_RE.match(owner):
        raise ValueError(f"Invalid GitHub owner name in '{full_name}'")
    if not _REPO_RE.match(repo):
        raise ValueError(f"Invalid GitHub repository name in '{full_name}'")
    return owner, repo


def validate_branch_name(branch: str) -> str:
    """Validate a Git branch name using git-check-ref-format safety rules."""
    if not isinstance(branch, str):
        raise ValueError("Branch name must be a string")

    cleaned = branch.strip()
    if not cleaned:
        raise ValueError("Branch name must not be empty")
    if cleaned != branch:
        raise ValueError(f"Branch name '{branch}' must not have leading or trailing whitespace")
    if any(ord(ch) < 32 or ord(ch) == 127 or ch in _INVALID_BRANCH_CHARS for ch in cleaned):
        raise ValueError(f"Branch name '{branch}' contains disallowed characters")
    if (
        cleaned.startswith(("/", "."))
        or cleaned.endswith(("/", ".", ".lock"))
        or "//" in cleaned
        or ".." in cleaned
        or "@{" in cleaned
        or "/." in cleaned
    ):
        raise ValueError(f"Branch name '{branch}' violates Git reference naming rules")
    return cleaned


def is_placeholder_repo(full_name: str) -> bool:
    """Return True when full_name appears to be an unedited template placeholder."""
    upper = full_name.upper()
    return any(marker in upper for marker in _PLACEHOLDER_MARKERS)


def resolve_monitoring_token(
    env: Mapping[str, str] | None = None,
) -> tuple[str | None, str]:
    """Resolve the read-only monitoring token without mixing git push credentials."""
    source_env = os.environ if env is None else env
    read_token = (source_env.get(TOKEN_ENV_PRIMARY) or "").strip()
    if read_token:
        return read_token, TOKEN_ENV_PRIMARY

    fallback_token = (source_env.get(TOKEN_ENV_FALLBACK) or "").strip()
    if fallback_token:
        return fallback_token, TOKEN_ENV_FALLBACK

    return None, "unauthenticated"


def sanitize_git_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return subprocess env for central Git push without monitoring tokens."""
    source_env = os.environ if env is None else env
    cleaned = dict(source_env)
    cleaned.pop(TOKEN_ENV_PRIMARY, None)
    cleaned["GIT_TERMINAL_PROMPT"] = "0"
    cleaned["GCM_INTERACTIVE"] = "never"
    return cleaned


def _extract_github_repo_slug(remote_url: str) -> str:
    parsed = urlparse(remote_url)
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path


def validate_central_remote_url(
    remote_url: str,
    *,
    expected_repository: str | None = None,
    allow_local_remote: bool = False,
) -> str:
    """Validate that the central Git remote points safely to the central GitHub repo."""
    if not isinstance(remote_url, str) or not remote_url.strip():
        raise ValueError("Central Git remote URL is empty")

    cleaned = remote_url.strip()
    if allow_local_remote and not cleaned.startswith(("http://", "https://", "ssh://")):
        return cleaned

    validated = validate_https_url(cleaned, allowed_hosts={GITHUB_HOST})
    slug = _extract_github_repo_slug(validated)
    validate_repo_full_name(slug)

    if expected_repository:
        expected_clean = expected_repository.strip()
        if slug.lower() != expected_clean.lower():
            raise ValueError(
                f"Central remote '{slug}' does not match expected repository '{expected_clean}'"
            )
    return validated

