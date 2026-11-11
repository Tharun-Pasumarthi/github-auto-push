"""Repository configuration loading and schema validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .security import (
    is_placeholder_repo,
    validate_branch_name,
    validate_repo_full_name,
)


class ConfigurationError(ValueError):
    """Raised when config/repositories.json is missing, malformed, or invalid."""


@dataclass(frozen=True, slots=True)
class RepositorySpec:
    """Validated configuration for a monitored GitHub repository."""

    name: str
    full_name: str
    branch: str
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class RepositoryConfig:
    """Parsed multi-repository configuration with enabled/disabled views."""

    repositories: tuple[RepositorySpec, ...]

    @property
    def total_count(self) -> int:
        return len(self.repositories)

    @property
    def enabled_repositories(self) -> tuple[RepositorySpec, ...]:
        return tuple(repo for repo in self.repositories if repo.enabled)

    @property
    def disabled_repositories(self) -> tuple[RepositorySpec, ...]:
        return tuple(repo for repo in self.repositories if not repo.enabled)


def _parse_repository_entry(index: int, raw: object) -> RepositorySpec:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"Repository entry at index {index} must be a JSON object")

    if "full_name" not in raw:
        raise ConfigurationError(f"Repository entry {index} is missing required field 'full_name'")
    try:
        owner, repo = validate_repo_full_name(raw["full_name"])
    except ValueError as exc:
        raise ConfigurationError(f"Repository entry {index}: {exc}") from exc
    full_name = f"{owner}/{repo}"

    if "branch" not in raw:
        raise ConfigurationError(f"Repository '{full_name}' is missing required field 'branch'")
    try:
        branch = validate_branch_name(raw["branch"])
    except ValueError as exc:
        raise ConfigurationError(f"Repository '{full_name}': {exc}") from exc

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigurationError(
            f"Repository '{full_name}': field 'enabled' must be a boolean (true/false)"
        )

    raw_name = raw.get("name")
    if raw_name is None:
        name = full_name
    elif not isinstance(raw_name, str) or not raw_name.strip():
        raise ConfigurationError(
            f"Repository '{full_name}': field 'name' must be a non-empty string when provided"
        )
    else:
        name = raw_name.strip()

    if enabled and is_placeholder_repo(full_name):
        raise ConfigurationError(
            f"Repository '{full_name}' uses an unconfigured template placeholder while enabled=true; "
            "update config/repositories.json with a real 'owner/repository' or set enabled=false"
        )

    return RepositorySpec(
        name=name,
        full_name=full_name,
        branch=branch,
        enabled=enabled,
    )


def load_repositories_config(config_path: Path) -> RepositoryConfig:
    """Load and strictly validate config/repositories.json."""
    if not config_path.exists():
        raise ConfigurationError(f"Configuration file not found: {config_path}")

    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"Unable to read configuration file '{config_path}': {exc}") from exc

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"Invalid JSON in '{config_path}' at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(payload, dict):
        raise ConfigurationError("Configuration root must be a JSON object")

    repos_raw = payload.get("repositories")
    if not isinstance(repos_raw, list):
        raise ConfigurationError("'repositories' must be a JSON array")

    parsed: list[RepositorySpec] = []
    seen_targets: set[tuple[str, str]] = set()
    for idx, item in enumerate(repos_raw):
        spec = _parse_repository_entry(idx, item)
        target_key = (spec.full_name.lower(), spec.branch)
        if target_key in seen_targets:
            raise ConfigurationError(
                f"Duplicate repository configuration for '{spec.full_name}' on branch '{spec.branch}'"
            )
        seen_targets.add(target_key)
        parsed.append(spec)

    return RepositoryConfig(repositories=tuple(parsed))

