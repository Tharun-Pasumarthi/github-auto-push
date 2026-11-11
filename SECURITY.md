# Security Policy and Operational Boundaries

## Security Architecture

The **GitHub Auto-Push Scheduler** enforces strict separation between **central heartbeat writes** and **monitored repository reads**:

1. **Central Repository Write Credential (`GITHUB_TOKEN`)**
   - Provided automatically by GitHub Actions per job run.
   - Scoped exclusively to the central `github-auto-push` repository.
   - Explicitly restricted in `.github/workflows/daily-sync.yml` to `permissions: contents: write` (all other scopes are denied).
   - Used by `actions/checkout` and `src/git_engine.py` exclusively to push `reports/YYYY-MM-DD.md` to `origin`.

2. **Monitored Repository Read Credential (`REPOSITORY_READ_TOKEN`)**
   - Optional repository secret containing a fine-grained Personal Access Token (PAT) for private monitored repositories.
   - Must grant **only** `Contents: Read-only` (and `Metadata: Read-only`) on explicitly selected monitored repositories.
   - Passed solely to `src/github_client.py` for HTTPS `GET` requests.
   - Explicitly stripped from Git subprocess environments by `src/security.py` (`sanitize_git_env`) so monitoring tokens are never passed to `git` commands.

---

## Guardrails Enforced in Code

- **Read-Only API Operations:** `GitHubClient` performs only HTTP `GET` requests against `https://api.github.com` and never invokes mutating endpoints (`POST`, `PUT`, `PATCH`, `DELETE`).
- **HTTPS-Only Enforcement:** `validate_https_url` rejects plain `http://` endpoints, embedded URL credentials, and untrusted API hosts.
- **Central Remote Verification:** `GitEngine.ensure_safe_central_remote` validates that `origin` is HTTPS `github.com` and matches `GITHUB_REPOSITORY` when executed in GitHub Actions.
- **Scoped Git Staging:** `GitEngine` checks for unexpected pre-staged files outside `reports/`, executes `git add -- reports/`, and verifies all staged files live under `reports/` before committing.
- **No Force-Push:** `GitEngine` never uses `--force`, `-f`, or `--force-with-lease`. Non-fast-forward rejections or permission errors fail immediately without overwriting remote history.
- **Input Validation:** `src/security.py` and `src/repository_manager.py` validate `owner/repo` slugs and Git branch names against path traversal, whitespace, and malformed reference syntax.
- **Secret Redaction:** `SecretRedactionFilter`, `ISTFormatter`, `GitHubClient`, `ReportData`, and `GitEngine` scrub known tokens, `gh*_/github_pat_*` patterns, `Authorization` headers, and URL credentials from terminal output, `logs/`, Markdown `reports/`, and `$GITHUB_STEP_SUMMARY`.
- **Supply-Chain Pinning:** All third-party GitHub Actions in `.github/workflows/` are pinned to verified 40-character full commit SHAs.

---

## Reporting a Vulnerability

If you discover a security vulnerability in this repository:

1. **Do not** publish secrets, tokens, or private repository details in public issues.
2. Rotate any potentially exposed GitHub tokens immediately under **GitHub Settings → Developer settings → Personal access tokens**.
3. Document reproduction steps privately and review workflow permissions, Actions secrets, and branch protection settings before re-enabling scheduled runs.

