# GitHub Auto-Push Scheduler

A production-ready, cloud-scheduled **GitHub Auto-Push Scheduler** powered by GitHub Actions and Python 3.12+. It monitors multiple configured GitHub repositories in strict read-only mode every day at **8:30 PM IST (15:00 UTC)** and guarantees a daily heartbeat report commit and push to this central `github-auto-push` repository.

---

## 1. What the Project Does

- Runs automatically once per day at **4:15 PM IST (16:15 Asia/Kolkata)** via GitHub Actions (`cron: "45 10 * * *"`). Uses an off-peak minute (`:45`) to bypass top-of-the-hour queue congestion on GitHub Actions runners.
- Supports on-demand manual runs via **Actions → Daily GitHub Heartbeat → Run workflow** (`workflow_dispatch`).
- Inspects any number of enabled public or private GitHub repositories using read-only GitHub REST API `GET` requests.
- Detects archived repositories, deleted/inaccessible repositories, missing branches, rate limits, and authentication errors without aborting the central heartbeat.
- Generates a dated Markdown report at `reports/YYYY-MM-DD.md` (IST date).
- Stages **only** `reports/`, commits with `chore: daily GitHub heartbeat YYYY-MM-DD`, and pushes to the central repository's default branch.
- Avoids unnecessary same-day duplicate commits when a manual rerun finds identical repository state already committed for that date.
- **Never** clones, edits, commits to, or pushes to any monitored project repository.

> **Scope note:** This cloud automation inspects remote GitHub repositories only. It cannot access uncommitted local files on your workstation.

---

## 2. Architecture

| Component | Technology | Responsibility |
|---|---|---|
| Cloud Scheduler | GitHub Actions (`.github/workflows/daily-sync.yml`) | Daily `45 10 * * *` (4:15 PM IST) cron + `workflow_dispatch`, concurrency guard, job timeout, pinned actions, log artifact retention |
| Orchestrator | `src/scheduler.py` | Coordinates config loading, monitoring, reporting, central Git push, and exit status |
| Config Manager | `src/repository_manager.py` | Parses and validates `config/repositories.json` |
| API Client | `src/github_client.py` | HTTPS-only, read-only `GET` checks with timeouts and bounded exponential backoff retries |
| Git Engine | `src/git_engine.py` | Configures `github-actions[bot]`, stages `reports/` only, verifies central remote, commits & pushes without `--force` |
| Report Generator | `src/report_generator.py` | Renders IST Markdown reports and prevents redundant same-day rewrites |
| Security Layer | `src/security.py` | Token separation, HTTPS & input validation, remote verification, secret redaction |
| Logging | `src/logger.py` | IST-timestamped console and `logs/heartbeat-YYYY-MM-DD.log` output with secret-redacting filter/formatter |

---

## 3. Folder Structure

```text
github-auto-push/
├── .github/
│   └── workflows/
│       ├── ci.yml                # Read-only pytest CI on code/config changes
│       └── daily-sync.yml        # Daily 8:30 PM IST (15:00 UTC) heartbeat workflow
├── config/
│   └── repositories.json         # Monitored repository list
├── logs/                         # Runtime diagnostic logs (gitignored; uploaded as Actions artifacts)
├── reports/                      # Committed daily Markdown reports (reports/YYYY-MM-DD.md)
├── src/
│   ├── __init__.py
│   ├── git_engine.py             # Safe central commit & push engine
│   ├── github_client.py          # Read-only GitHub REST API client with bounded retries
│   ├── logger.py                 # Structured secret-redacting logger
│   ├── report_generator.py       # Daily Markdown report renderer & deduplicator
│   ├── repository_manager.py     # Configuration loader & validator
│   ├── scheduler.py              # Main CLI & orchestration entrypoint
│   └── security.py               # Security validation, token isolation & redaction
├── tests/                        # Mocked pytest unit & integration test suite
├── .gitignore
├── README.md
├── requirements-dev.txt
├── requirements.txt
└── SECURITY.md
```

---

## 4. Prerequisites

- A GitHub account.
- **Git** installed locally to push the initial project to GitHub.
- **Python 3.12+** (only required if running tests or testing the scheduler locally; GitHub Actions installs Python 3.12 automatically in the cloud).

---

## 5. Creating the Central GitHub Repository

1. Sign in to GitHub and create a new repository named `github-auto-push`.
2. Choose **Private** (recommended) or **Public**.
3. Initialize it **without** conflicting files (or pull/merge cleanly if initialized with a README).
4. Note the default branch name (typically `main`).

---

## 6. Uploading the Project

From inside the `github-auto-push` project directory:

```bash
git init -b main
git add .
git commit -m "feat: initialize GitHub Auto-Push Scheduler"
git remote add origin https://github.com/<YOUR_USERNAME>/github-auto-push.git
git push -u origin main
```

---

## 7. Configuring Monitored Repositories

Edit `config/repositories.json` to list the repositories you want monitored:

```json
{
  "repositories": [
    {
      "name": "Project One",
      "full_name": "YOUR_USERNAME/project-one",
      "branch": "main",
      "enabled": true
    },
    {
      "name": "Legacy Archive",
      "full_name": "YOUR_USERNAME/legacy-archive",
      "branch": "master",
      "enabled": false
    }
  ]
}
```

Configuration rules:
- `full_name`: Required `owner/repository` slug.
- `branch`: Required branch name to inspect (supports slash branches like `release/v1`).
- `enabled`: Boolean (`true` or `false`). Disabled repositories are skipped and noted in the report.
- `name`: Optional human-readable label (defaults to `full_name`).
- If you leave the initial template entry `"enabled": false`, the central heartbeat still runs and pushes a daily report without errors.

---

## 8. Creating a Fine-Grained Access Token

For **private** monitored repositories, create a **Fine-grained Personal Access Token (PAT)** with strict read-only access:

1. Open GitHub **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. Set a descriptive name (e.g., `github-auto-push-readonly-monitor`) and an expiration policy.
3. Under **Repository access**, choose **Only select repositories** and select **only** the monitored repositories (do not grant access to unrelated repositories).
4. Under **Repository permissions**:
   - **Contents**: **Read-only** (automatically includes **Metadata: Read-only**).
   - Leave all write permissions (`Contents: write`, `Administration`, `Workflows`, etc.) set to **No access**.
5. Generate and copy the token immediately.

> If you only monitor **public** repositories, you do not need to create a PAT; the workflow falls back to the built-in `GITHUB_TOKEN`.

---

## 9. Configuring GitHub Actions Secrets

1. Open your central `github-auto-push` repository on GitHub.
2. Go to **Settings → Secrets and variables → Actions**.
3. Click **New repository secret**.
4. Name: `REPOSITORY_READ_TOKEN`
5. Value: Paste your read-only fine-grained token.
6. Click **Add secret**.

Never commit tokens into `config/repositories.json`, source files, reports, or logs.

---

## 10. Enabling Workflow Write Permissions

The central workflow uses the built-in `GITHUB_TOKEN` (never `REPOSITORY_READ_TOKEN`) to commit and push `reports/YYYY-MM-DD.md` back to `github-auto-push`.

1. In `github-auto-push`, open **Settings → Actions → General**.
2. Scroll to **Workflow permissions**.
3. Select **Read and write permissions**.
4. Click **Save**.

`.github/workflows/daily-sync.yml` also explicitly scopes token permissions to `contents: write` only.

---

## 11. Running the First Manual Workflow

1. Open the **Actions** tab in your `github-auto-push` repository.
2. Enable workflows if GitHub prompts you.
3. Select **Daily GitHub Heartbeat** in the left sidebar.
4. Click **Run workflow** → **Run workflow**.
5. Wait for the job (`Daily Check, Report, and Central Push`) to complete and verify it finishes with a green checkmark.

---

## 12. Verifying the Daily Report

After the workflow succeeds:

1. Check the repository's commit history for a commit authored by `github-actions[bot]`:
   `chore: daily GitHub heartbeat YYYY-MM-DD`
2. Open `reports/YYYY-MM-DD.md` and verify:
   - Date and execution time in IST (`Asia/Kolkata`).
   - Configured, checked, successful, archived, and failed counts.
   - Branch, visibility, latest commit SHA, and last push UTC for each accessible repository.
3. Open the workflow run **Summary** and download the `heartbeat-logs-*` artifact if you want to inspect full diagnostic logs.

> **Schedule note:** Cron `45 10 * * *` runs at 10:45 UTC (16:15 IST / 4:15 PM IST). GitHub Actions executes 100% in GitHub's cloud, so it triggers automatically even when your personal computer or device is turned off or offline.

---

## 13. Local Development & Testing

Install dependencies and run the automated test suite (requires no GitHub credentials):

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -v
```

Run a local read-only dry check (generates `reports/YYYY-MM-DD.md` and `logs/heartbeat-YYYY-MM-DD.log` without committing or pushing):

```bash
python -m src.scheduler
```

On Windows PowerShell:

```powershell
$env:REPOSITORY_READ_TOKEN = "YOUR_READ_ONLY_TOKEN"
python -m src.scheduler
```

---

## 14. Troubleshooting

| Symptom | Likely Cause | Resolution |
|---|---|---|
| `HTTP 401` in report | `REPOSITORY_READ_TOKEN` expired, revoked, or malformed | Regenerate the fine-grained PAT and update the `REPOSITORY_READ_TOKEN` repository secret. |
| `HTTP 403` / `HTTP 404` on private repo | Read token does not include that repo or lacks `Contents: Read-only` / `Metadata: Read-only` | Edit token repository access in GitHub Settings to include the repository with read-only permissions. |
| `MISSING_BRANCH` (`HTTP 404` on branch) | Configured branch in `config/repositories.json` was renamed or deleted | Update `"branch"` in `config/repositories.json` to match the repository's actual branch. |
| Push rejected (`403` / `protected branch`) | Workflow write permissions disabled or branch protection blocks `github-actions[bot]` | Enable **Read and write permissions** in **Settings → Actions → General** and check branch protection rules on the central repo. |
| Push rejected (`non-fast-forward`) | Remote branch changed concurrently | Re-run the workflow manually after remote changes settle; the engine never force-pushes. |
| Same-day manual rerun makes no new commit | Today's `reports/YYYY-MM-DD.md` is already committed with identical repository status | Expected behavior to avoid duplicate commits. Use `--force-report-update` locally or wait for repository/date changes. |
| Scheduled run did not fire | Actions disabled on repo, or `.github/workflows/daily-sync.yml` is not on the default branch | Ensure the workflow file is committed to the repository's **default branch** and Actions are enabled. |

---

## 15. Security Recommendations

- Keep the central `github-auto-push` repository **private**.
- Use a dedicated **fine-grained PAT** with **Read-only** access strictly to the monitored repositories, and rotate it periodically before expiry.
- Keep `actions/checkout`, `actions/setup-python`, and `actions/upload-artifact` pinned to verified full commit SHAs (Dependabot can monitor updates safely via the trailing `# vX.Y.Z` comments).
- Never enable `--force` push or grant `REPOSITORY_READ_TOKEN` write scope on monitored repositories.
- Review [SECURITY.md](SECURITY.md) for the complete threat model and operational boundaries.
