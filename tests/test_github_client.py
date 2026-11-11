"""Unit tests for the read-only GitHub REST API client (no real network/tokens)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
import requests

from src.github_client import CheckStatus, GitHubClient
from src.repository_manager import RepositorySpec


@dataclass
class FakeResponse:
    status_code: int
    payload: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    """Deterministic requests.Session mock recording every HTTP GET call."""

    def __init__(self, routes: dict[str, list[FakeResponse | Exception]]) -> None:
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls: list[tuple[str, tuple[float, float] | float | None]] = []
        self.closed = False

    def get(self, url: str, timeout: tuple[float, float] | float | None = None) -> FakeResponse:
        self.calls.append((url, timeout))
        for suffix, queue in self.routes.items():
            if url.endswith(suffix):
                item = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(item, Exception):
                    raise item
                return item
        return FakeResponse(404, {"message": "Not Found"})

    def close(self) -> None:
        self.closed = True


def _spec(full_name: str = "octo/service", branch: str = "main") -> RepositorySpec:
    return RepositorySpec(name="Service", full_name=full_name, branch=branch, enabled=True)


def test_check_repository_success_with_slash_branch() -> None:
    session = FakeSession(
        {
            "/repos/octo/service": [
                FakeResponse(
                    200,
                    {
                        "archived": False,
                        "private": True,
                        "default_branch": "main",
                        "pushed_at": "2026-11-11T18:10:00Z",
                    },
                )
            ],
            "/repos/octo/service/branches/release%2Fv1": [
                FakeResponse(200, {"commit": {"sha": "abc123def4567890"}})
            ],
        }
    )
    client = GitHubClient(token="ghp_testtoken123456", session=session)  # type: ignore[arg-type]
    result = client.check_repository(_spec(branch="release/v1"))

    assert result.status == CheckStatus.OK
    assert result.accessible is True
    assert result.visibility == "private"
    assert result.latest_commit_sha == "abc123def4567890"
    assert result.last_push_utc == "2026-11-11T18:10:00Z"
    assert session.calls[0][1] == (5.0, 20.0)


def test_check_repository_archived() -> None:
    session = FakeSession(
        {
            "/repos/octo/legacy": [
                FakeResponse(
                    200,
                    {
                        "archived": True,
                        "private": False,
                        "default_branch": "main",
                        "pushed_at": "2024-01-01T00:00:00Z",
                    },
                )
            ],
            "/repos/octo/legacy/branches/main": [
                FakeResponse(200, {"commit": {"sha": "deadbeef123456"}})
            ],
        }
    )
    client = GitHubClient(session=session)  # type: ignore[arg-type]
    result = client.check_repository(_spec("octo/legacy"))

    assert result.status == CheckStatus.ARCHIVED
    assert result.archived is True
    assert result.latest_commit_sha == "deadbeef123456"


def test_check_repository_missing_repo_and_missing_branch() -> None:
    missing_repo_session = FakeSession(
        {"/repos/octo/deleted": [FakeResponse(404, {"message": "Not Found"})]}
    )
    client = GitHubClient(session=missing_repo_session)  # type: ignore[arg-type]
    res_repo = client.check_repository(_spec("octo/deleted"))
    assert res_repo.status == CheckStatus.ERROR
    assert res_repo.error_category == "NOT_FOUND"

    missing_branch_session = FakeSession(
        {
            "/repos/octo/app": [FakeResponse(200, {"archived": False, "private": False})],
            "/repos/octo/app/branches/missing": [FakeResponse(404, {"message": "Branch not found"})],
        }
    )
    client2 = GitHubClient(session=missing_branch_session)  # type: ignore[arg-type]
    res_branch = client2.check_repository(_spec("octo/app", "missing"))
    assert res_branch.status == CheckStatus.ERROR
    assert res_branch.error_category == "MISSING_BRANCH"
    assert "missing" in res_branch.message


def test_check_repository_auth_and_forbidden_do_not_retry() -> None:
    sleeps: list[float] = []
    session = FakeSession({"/repos/octo/priv": [FakeResponse(401, {"message": "Bad credentials"})]})
    client = GitHubClient(
        token="ghp_secret_value_12345",
        session=session,  # type: ignore[arg-type]
        max_retries=3,
        sleep_fn=sleeps.append,
    )
    res_401 = client.check_repository(_spec("octo/priv"))
    assert res_401.status == CheckStatus.ERROR
    assert res_401.error_category == "AUTH_ERROR"
    assert len(session.calls) == 1
    assert sleeps == []
    assert "ghp_secret_value_12345" not in res_401.message

    session_403 = FakeSession({"/repos/octo/priv": [FakeResponse(403, {"message": "Forbidden"})]})
    client_403 = GitHubClient(
        session=session_403,  # type: ignore[arg-type]
        max_retries=3,
        sleep_fn=sleeps.append,
    )
    res_403 = client_403.check_repository(_spec("octo/priv"))
    assert res_403.error_category == "FORBIDDEN"
    assert len(session_403.calls) == 1


def test_retries_exponential_backoff_on_rate_limit_and_transient_errors() -> None:
    sleeps: list[float] = []
    session = FakeSession(
        {
            "/repos/octo/flaky": [
                requests.Timeout("timed out"),
                FakeResponse(503, {"message": "Service Unavailable"}),
                FakeResponse(
                    403,
                    {"message": "Rate limit"},
                    headers={"X-RateLimit-Remaining": "0", "Retry-After": "3"},
                ),
                FakeResponse(
                    200,
                    {"archived": False, "private": False, "pushed_at": "2026-11-12T00:00:00Z"},
                ),
            ],
            "/repos/octo/flaky/branches/main": [
                FakeResponse(200, {"commit": {"sha": "112233445566"}})
            ],
        }
    )
    client = GitHubClient(
        session=session,  # type: ignore[arg-type]
        max_retries=3,
        backoff_base=1.0,
        sleep_fn=sleeps.append,
    )
    result = client.check_repository(_spec("octo/flaky"))

    assert result.status == CheckStatus.OK
    assert result.latest_commit_sha == "112233445566"
    assert sleeps == [1.0, 2.0, 3.0]


def test_rate_limit_exhaustion_returns_rate_limit_error() -> None:
    sleeps: list[float] = []
    session = FakeSession(
        {
            "/repos/octo/limited": [
                FakeResponse(429, {}, headers={"Retry-After": "60", "X-RateLimit-Reset": "9999"})
            ]
        }
    )
    client = GitHubClient(
        session=session,  # type: ignore[arg-type]
        max_retries=2,
        backoff_base=1.0,
        max_retry_wait=5.0,
        sleep_fn=sleeps.append,
    )
    result = client.check_repository(_spec("octo/limited"))

    assert result.status == CheckStatus.ERROR
    assert result.error_category == "RATE_LIMIT"
    assert sleeps == [5.0, 5.0]
    assert len(session.calls) == 3


def test_client_rejects_insecure_api_base() -> None:
    with pytest.raises(ValueError, match="Only HTTPS"):
        GitHubClient(api_base="http://api.github.com")

