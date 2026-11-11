"""Tests for repository configuration loading and validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.repository_manager import ConfigurationError, load_repositories_config


def test_load_repositories_config_supports_multiple_enabled_and_disabled(tmp_path: Path) -> None:
    cfg_path = tmp_path / "repositories.json"
    repos = [
        {
            "name": f"Project {i}",
            "full_name": f"octo/project-{i}",
            "branch": "main" if i % 2 == 0 else "release/v1",
            "enabled": i % 3 != 0,
        }
        for i in range(1, 26)
    ]
    cfg_path.write_text(json.dumps({"repositories": repos}), encoding="utf-8")

    loaded = load_repositories_config(cfg_path)
    assert loaded.total_count == 25
    assert len(loaded.disabled_repositories) == 8
    assert len(loaded.enabled_repositories) == 17
    assert loaded.enabled_repositories[0].full_name == "octo/project-1"
    assert loaded.enabled_repositories[0].branch == "release/v1"


def test_load_repositories_config_defaults_optional_name_and_enabled(tmp_path: Path) -> None:
    cfg_path = tmp_path / "repositories.json"
    cfg_path.write_text(
        json.dumps({"repositories": [{"full_name": "octo/app", "branch": "develop"}]}),
        encoding="utf-8",
    )

    loaded = load_repositories_config(cfg_path)
    assert loaded.enabled_repositories[0].name == "octo/app"
    assert loaded.enabled_repositories[0].enabled is True


def test_load_repositories_config_allows_disabled_placeholder(tmp_path: Path) -> None:
    cfg_path = tmp_path / "repositories.json"
    cfg_path.write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "name": "Example",
                        "full_name": "YOUR_GITHUB_USERNAME/YOUR_REPOSITORY",
                        "branch": "main",
                        "enabled": False,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    loaded = load_repositories_config(cfg_path)
    assert loaded.total_count == 1
    assert len(loaded.enabled_repositories) == 0


def test_load_repositories_config_rejects_enabled_placeholder(tmp_path: Path) -> None:
    cfg_path = tmp_path / "repositories.json"
    cfg_path.write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "name": "Example",
                        "full_name": "YOUR_GITHUB_USERNAME/YOUR_REPOSITORY",
                        "branch": "main",
                        "enabled": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="placeholder"):
        load_repositories_config(cfg_path)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"wrong_key": []},
        {"repositories": "not-a-list"},
        {"repositories": ["not-an-object"]},
        {"repositories": [{"branch": "main"}]},
        {"repositories": [{"full_name": "bad-format", "branch": "main"}]},
        {"repositories": [{"full_name": "octo/repo", "branch": ""}]},
        {"repositories": [{"full_name": "octo/repo", "branch": "main", "enabled": "yes"}]},
        {"repositories": [{"name": "", "full_name": "octo/repo", "branch": "main"}]},
        {
            "repositories": [
                {"full_name": "octo/repo", "branch": "main"},
                {"full_name": "Octo/repo", "branch": "main"},
            ]
        },
    ],
)
def test_load_repositories_config_rejects_invalid_schemas(
    tmp_path: Path, payload: object
) -> None:
    cfg_path = tmp_path / "repositories.json"
    cfg_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_repositories_config(cfg_path)


def test_load_repositories_config_missing_and_corrupt_json(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="not found"):
        load_repositories_config(tmp_path / "missing.json")

    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{invalid", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Invalid JSON"):
        load_repositories_config(bad_json)

