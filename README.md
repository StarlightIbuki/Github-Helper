# GitHub Helper Userscripts

A collection of Tampermonkey userscripts that add quality-of-life features to GitHub.

## Scripts

### [backport-tracker.js](backport-tracker.js) — Backport Tracker

Injects a **Backports** panel into the PR sidebar that automatically finds and tracks the status of backport PRs linked in comments.

**How it works:**

- **On a main/default branch PR** — after the PR is merged, scans all comments for lines matching `backport to <branch>` or `backport PR for <branch>` and collects the associated PR links. It then polls each backport PR for:
  - Overall state (Open / Merged / Closed)
  - CI check results (passing, failing, running)
  - Manager approval status
- **On a backport PR** (i.e., the base branch is not `main`/`master`) — shows a back-link to the original PR it was spawned from.

**Sidebar panel features:**

| Icon | Meaning |
|---|---|
| Check (green) | PR merged or all CI passed |
| X (red) | CI test failure — click to jump to the failed job |
| Dot (yellow) | CI running or pending |
| Shield (yellow) | Awaiting approval (CI job, label, or reviews) |
| Sync (spinning) | Fetching status |
| Branch | Link to original PR (no CI check) |

- **Refresh button** — manually re-polls all non-completed PRs.
- **⚙ Settings button** — opens a per-repo config panel (saved to `localStorage`):
  - **Ignore CI job** — substring match against CI check names; matching checks will be excluded from status tracking.
  - **Required label** — label name substring that must be present on the PR.
  - **Required review approvals** — minimum number of approved reviews required.
  - All three fields are optional and independent; set only what your repo uses.
- **Copy summary** — copies a plain-text status summary to the clipboard. Each line follows the format:
  ```
  [STATUS] branch-name: https://github.com/owner/repo/pull/123
  ```
  If **Ignore CI job** is configured, a machine-readable header is prepended so `gh-rerunner` can pick it up automatically:
  ```
  # gh-rerunner: ignore_ci=lint,build-docs
  [MERGED]   next/3.10.x: https://github.com/owner/repo/pull/100
  [FETCHING] next/3.11.x: https://github.com/owner/repo/pull/101
  [FAILURE]  next/3.12.x: https://github.com/owner/repo/pull/102
  ```
- **Auto-refresh** — statuses are refreshed automatically every 30 seconds.

**Tooltip** (hover any row) shows a breakdown: `CI: N passed, N failed, N running`, plus one line per configured approval check with its current state.

**Comment scanning** — recognises backport links from common bot/manual formats (`backport to`, `backporting to`, `cherry-pick to`, `ported to`, and backtick-quoted variants). If none match, falls back to collecting all same-repo PR links found in any comment.

---

### [rerunner.js](rerunner.js) — GitHub Actions Auto-Rerunner

Adds a persistent **Start auto-retry** button to GitHub Actions run pages that automatically re-runs failed jobs up to a configurable limit.

**How it works:**

Uses a `MutationObserver` to watch the run status badge in real time. When a failure is detected, it clicks through the "Re-run failed jobs" dialog automatically.

**Controls injected into the toolbar:**

- **Start / Stop** toggle button — enables or disables the watcher.
- **Counter** (`N/max`) — shows current retry count over the limit. Click the count to reset it.
- **Limit** — click the max number to edit it inline.

State (retry count, limit, running/stopped) is persisted per run ID in `localStorage`, so it survives page refreshes.

---

## Installation

1. Install the [Tampermonkey](https://www.tampermonkey.net/) browser extension.
2. Click one of the install links below to open the script directly in Tampermonkey:
   - [Install Backport Tracker](https://raw.githubusercontent.com/StarlightIbuki/Github-Helper/main/backport-tracker.js)
   - [Install Auto-Rerunner](https://raw.githubusercontent.com/StarlightIbuki/Github-Helper/main/rerunner.js)
3. Click **Install** in the Tampermonkey dialog.

## Auto-Update

Both scripts include `@updateURL` and `@downloadURL` headers pointing to this repository. Tampermonkey will check for updates automatically based on your configured update interval (default: once a day). You can also trigger a manual check via **Tampermonkey Dashboard → Check for updates**.

Version bumps in the `@version` header are what trigger the update prompt.

---

## Headless CI Rerunner (`server-rerunner/`)

For running CI reruns on a server without a browser, see [`server-rerunner/`](server-rerunner/) — a Python CLI that polls the GitHub API and retries failed jobs automatically.

### Backport Tracker → gh-rerunner workflow

1. Open your merged main-branch PR and let the Backport Tracker load all statuses.
2. Configure **Ignore CI job** in ⚙ Settings if your repo has jobs that are flaky or irrelevant (e.g. `lint`, `build-docs`).
3. Click **Copy summary**. The clipboard now contains a block like:
   ```
   # gh-rerunner: ignore_ci=lint,build-docs
   [MERGED]   next/3.10.x: https://github.com/owner/repo/pull/100
   [MERGED]   next/3.11.x: https://github.com/owner/repo/pull/101
   [FETCHING] next/3.12.x: https://github.com/owner/repo/pull/102
   [FAILURE]  next/3.13.x: https://github.com/owner/repo/pull/103
   ```
4. Pipe it to `gh-rerunner run` on your server (or locally):
   ```bash
   pbpaste | gh-rerunner run
   ```
   - `[MERGED]` and `[SUCCESS]` entries are skipped automatically.
   - `[FETCHING]` entries emit a warning but are still watched.
   - The `ignore_ci` header is read automatically — no extra flags needed.
   - Failed jobs whose names match an ignored pattern are not retried.

See [`server-rerunner/README.md`](server-rerunner/README.md) for installation, token setup, and full CLI reference.
