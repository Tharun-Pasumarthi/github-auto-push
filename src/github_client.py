"""Read-only GitHub REST API client with HTTPS enforcement and bounded retries."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter

from .repository_manager import RepositorySpec
from .security import DEFAULT_API_BASE, redact_secrets, validate_https_url

TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class CheckStatus(StrEnum):
    """Normalized health status for each checked repository."""

    OK = "OK"
    ARCHIVED = "ARCHIVED"
    ERROR = "ERROR"


class GitHubApiError(RuntimeError):
    """Base error for GitHub REST API failures."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        category: str = "API_ERROR",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.category = category


class GitHubAuthError(GitHubApiError):
    """Raised on HTTP 401 authentication failure."""

    def __init__(self, message: str = "Authentication failed (HTTP 401)") -> None:
        super().__init__(message, status_code=401, category="AUTH_ERROR")


class GitHubForbiddenError(GitHubApiError):
    """Raised on HTTP 403 non-rate-limit access denial."""

    def __init__(self, message: str = "Access forbidden (HTTP 403)") -> None:
        super().__init__(message, status_code=403, category="FORBIDDEN")


class GitHubRateLimitError(GitHubApiError):
    """Raised when GitHub API rate limits remain exhausted after bounded retries."""

    def __init__(self, message: str, *, status_code: int = 429) -> None:
        super().__init__(message, status_code=status_code, category="RATE_LIMIT")


class GitHubNotFoundError(GitHubApiError):
    """Raised on HTTP 404 when a repository or branch is missing/inaccessible."""

    def __init__(self, message: str = "Resource not found (HTTP 404)") -> None:
        super().__init__(message, status_code=404, category="NOT_FOUND")


@dataclass(frozen=True, slots=True)
class RepositoryCheckResult:
    """Result of inspecting a single monitored repository in read-only mode."""

    name: str
    full_name: str
    branch: str
    status: CheckStatus
    accessible: bool
    archived: bool = False
    visibility: str | None = None
    default_branch: str | None = None
    latest_commit_sha: str | None = None
    last_push_utc: str | None = None
    http_status: int | None = None
    error_category: str | None = None
    message: str = ""


def _is_rate_limit_response(response: requests.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code == 403:
        remaining = response.headers.get("X-RateLimit-Remaining", "").strip()
        retry_after = response.headers.get("Retry-After", "").strip()
        if remaining == "0" or bool(retry_after):
            return True
    return False


def _compute_retry_delay(
    attempt: int,
    backoff_base: float,
    max_retry_wait: float,
    response: requests.Response | None = None,
) -> float:
    exponential = backoff_base * (2**attempt)
    if response is not None:
        retry_after = response.headers.get("Retry-After", "").strip()
        if retry_after:
            try:
                parsed_wait = float(retry_after)
                if parsed_wait > 0:
                    return min(parsed_wait, max_retry_wait)
            except ValueError:
                pass
    return min(exponential, max_retry_wait)


class GitHubClient:
    """Strict read-only GitHub REST API client over HTTPS."""

    def __init__(
        self,
        token: str | None = None,
        *,
        api_base: str = DEFAULT_API_BASE,
        allowed_hosts: set[str] | None = None,
        timeout: tuple[float, float] = (5.0, 20.0),
        max_retries: int = 3,
        backoff_base: float = 1.0,
        max_retry_wait: float = 15.0,
        session: requests.Session | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        effective_hosts = {"api.github.com"} if allowed_hosts is None else allowed_hosts
        self.api_base = validate_https_url(api_base, allowed_hosts=effective_hosts)
        self._token = token.strip() if token else None
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.backoff_base = max(0.0, backoff_base)
        self.max_retry_wait = max(0.0, max_retry_wait)
        self.sleep_fn = sleep_fn
        self.logger = logger

        self.session = session or self._build_session(self._token)

    @staticmethod
    def _build_session(token: str | None) -> requests.Session:
        session = requests.Session()
        adapter = HTTPAdapter(max_retries=0)
        session.mount("https://", adapter)
        session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "github-auto-push-scheduler/1.0",
            }
        )
        if token:
            session.headers["Authorization"] = f"Bearer {token}"
        return session

    def close(self) -> None:
        self.session.close()

    def _redact(self, text: str) -> str:
        secrets = (self._token,) if self._token else ()
        return redact_secrets(text, secrets)

    def _log_retry(self, path: str, reason: str, attempt: int, delay: float) -> None:
        if self.logger is not None:
            self.logger.warning(
                "Transient GitHub API error for %s (%s). Retry %d/%d in %.1fs.",
                path,
                self._redact(reason),
                attempt + 1,
                self.max_retries,
                delay,
            )

    def api_get(self, path: str) -> dict[str, Any]:
        """Execute a strictly read-only HTTPS GET call with bounded retries."""
        normalized_path = path if path.startswith("/") else f"/{path}"
        url = validate_https_url(f"{self.api_base}{normalized_path}")

        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(url, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt < self.max_retries:
                    delay = _compute_retry_delay(attempt, self.backoff_base, self.max_retry_wait)
                    self._log_retry(normalized_path, type(exc).__name__, attempt, delay)
                    self.sleep_fn(delay)
                    continue
                raise GitHubApiError(
                    f"Network failure ({type(exc).__name__}) contacting GitHub API",
                    category="NETWORK_ERROR",
                ) from exc
            except requests.RequestException as exc:
                raise GitHubApiError(
                    f"HTTP client error ({type(exc).__name__})",
                    category="NETWORK_ERROR",
                ) from exc

            status = response.status_code

            if _is_rate_limit_response(response):
                if attempt < self.max_retries:
                    delay = _compute_retry_delay(
                        attempt, self.backoff_base, self.max_retry_wait, response
                    )
                    self._log_retry(normalized_path, f"HTTP {status} rate limit", attempt, delay)
                    self.sleep_fn(delay)
                    continue
                reset_ts = response.headers.get("X-RateLimit-Reset", "unknown")
                raise GitHubRateLimitError(
                    f"GitHub API rate limit exceeded (HTTP {status}, reset={reset_ts})",
                    status_code=status,
                )

            if status in TRANSIENT_STATUS_CODES:
                if attempt < self.max_retries:
                    delay = _compute_retry_delay(
                        attempt, self.backoff_base, self.max_retry_wait, response
                    )
                    self._log_retry(normalized_path, f"HTTP {status}", attempt, delay)
                    self.sleep_fn(delay)
                    continue
                raise GitHubApiError(
                    f"GitHub API unavailable after retries (HTTP {status})",
                    status_code=status,
                    category="API_ERROR",
                )

            if status == 401:
                raise GitHubAuthError(
                    "Authentication failed (HTTP 401): check REPOSITORY_READ_TOKEN or GITHUB_TOKEN."
                )
            if status == 403:
                raise GitHubForbiddenError(
                    "Access forbidden (HTTP 403): read token lacks access to this repository."
                )
            if status == 404:
                raise GitHubNotFoundError("Resource not found or inaccessible (HTTP 404).")
            if status >= 400:
                raise GitHubApiError(
                    f"GitHub API returned HTTP {status}.",
                    status_code=status,
                    category="API_ERROR",
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise GitHubApiError(
                    "GitHub API returned malformed JSON payload.",
                    status_code=status,
                    category="API_ERROR",
                ) from exc

            if not isinstance(payload, dict):
                raise GitHubApiError(
                    "Unexpected GitHub API JSON structure (expected object).",
                    status_code=status,
                    category="API_ERROR",
                )
            return payload

        raise GitHubApiError("Exhausted GitHub API retry budget.", category="API_ERROR")

    def check_repository(self, spec: RepositorySpec) -> RepositoryCheckResult:
        """Inspect repository and branch metadata without cloning or writing."""
        repo_path = f"/repos/{spec.full_name}"
        try:
            repo_data = self.api_get(repo_path)
        except GitHubNotFoundError:
            return RepositoryCheckResult(
                name=spec.name,
                full_name=spec.full_name,
                branch=spec.branch,
                status=CheckStatus.ERROR,
                accessible=False,
                http_status=404,
                error_category="NOT_FOUND",
                message=(
                    "Repository not found, deleted, or private without read-token access (HTTP 404)."
                ),
            )
        except GitHubApiError as exc:
            return RepositoryCheckResult(
                name=spec.name,
                full_name=spec.full_name,
                branch=spec.branch,
                status=CheckStatus.ERROR,
                accessible=False,
                http_status=exc.status_code,
                error_category=exc.category,
                message=self._redact(str(exc)),
            )

        is_archived = bool(repo_data.get("archived", False))
        visibility = "private" if repo_data.get("private") else "public"
        default_branch = repo_data.get("default_branch")
        last_push_utc = repo_data.get("pushed_at")

        encoded_branch = quote(spec.branch, safe="")
        branch_path = f"/repos/{spec.full_name}/branches/{encoded_branch}"

        try:
            branch_data = self.api_get(branch_path)
        except GitHubNotFoundError:
            if is_archived:
                return RepositoryCheckResult(
                    name=spec.name,
                    full_name=spec.full_name,
                    branch=spec.branch,
                    status=CheckStatus.ARCHIVED,
                    accessible=True,
                    archived=True,
                    visibility=visibility,
                    default_branch=default_branch,
                    last_push_utc=last_push_utc,
                    http_status=404,
                    message=(
                        f"Repository is archived; configured branch '{spec.branch}' was not found. "
                        "No write actions were attempted."
                    ),
                )
            return RepositoryCheckResult(
                name=spec.name,
                full_name=spec.full_name,
                branch=spec.branch,
                status=CheckStatus.ERROR,
                accessible=False,
                archived=False,
                visibility=visibility,
                default_branch=default_branch,
                last_push_utc=last_push_utc,
                http_status=404,
                error_category="MISSING_BRANCH",
                message=f"Configured branch '{spec.branch}' was not found (HTTP 404).",
            )
        except GitHubApiError as exc:
            if is_archived:
                return RepositoryCheckResult(
                    name=spec.name,
                    full_name=spec.full_name,
                    branch=spec.branch,
                    status=CheckStatus.ARCHIVED,
                    accessible=True,
                    archived=True,
                    visibility=visibility,
                    default_branch=default_branch,
                    last_push_utc=last_push_utc,
                    http_status=exc.status_code,
                    error_category=exc.category,
                    message=(
                        "Repository is archived (branch inspection returned "
                        f"{self._redact(str(exc))})."
                    ),
                )
            return RepositoryCheckResult(
                name=spec.name,
                full_name=spec.full_name,
                branch=spec.branch,
                status=CheckStatus.ERROR,
                accessible=False,
                archived=False,
                visibility=visibility,
                default_branch=default_branch,
                last_push_utc=last_push_utc,
                http_status=exc.status_code,
                error_category=exc.category,
                message=self._redact(str(exc)),
            )

        commit_obj = branch_data.get("commit")
        latest_sha = commit_obj.get("sha") if isinstance(commit_obj, dict) else None

        if is_archived:
            return RepositoryCheckResult(
                name=spec.name,
                full_name=spec.full_name,
                branch=spec.branch,
                status=CheckStatus.ARCHIVED,
                accessible=True,
                archived=True,
                visibility=visibility,
                default_branch=default_branch,
                latest_commit_sha=latest_sha,
                last_push_utc=last_push_utc,
                http_status=200,
                message="Repository is archived; read-only inspection only.",
            )

        return RepositoryCheckResult(
            name=spec.name,
            full_name=spec.full_name,
            branch=spec.branch,
            status=CheckStatus.OK,
            accessible=True,
            archived=False,
            visibility=visibility,
            default_branch=default_branch,
            latest_commit_sha=latest_sha,
            last_push_utc=last_push_utc,
            http_status=200,
            message="Repository and configured branch are accessible (read-only check).",
        )

