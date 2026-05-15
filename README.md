# GitHub Helper

A toolkit for tracking and managing backport PRs and CI reruns on GitHub. 

## Quick Start

### 1 — Install the userscript

[**→ Install Backport Tracker**](https://raw.githubusercontent.com/StarlightIbuki/Github-Helper/main/backport-tracker.js) *(requires [Tampermonkey](https://www.tampermonkey.net/))*

Click the link above and confirm **Install** in the Tampermonkey dialog.

### 2 — Install and start the server (Optional and recommended)

```bash
pipx install "gh-rerunner @ git+https://github.com/StarlightIbuki/Github-Helper.git#subdirectory=server-rerunner"
```

> `pipx` installs the CLI into an isolated environment. Install it with `brew install pipx` (macOS), `apt install pipx` (Debian/Ubuntu), or `pip install pipx`.

Then authenticate and start:

```bash
gh-rerunner auth                                          # one-time login via browser
caffeinate -i gh-rerunner watch --serve --no-tui \
  --host 0.0.0.0                                          # macOS — keeps machine awake
```

The web UI is now at **http://localhost:53210/**.

### 3 — Send backport PRs to the server

Open any merged PR on GitHub. The Backport Tracker sidebar will show a **Watch on server** button — click it to push all backport URLs to the running server in one RPC call. Done.

---

## Userscript

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
- **Watch on server** button — appears when `gh-rerunner` is detected running locally (default: `127.0.0.1:53210`). Sends all PR URLs and `ignore_ci` settings to the server via a single RPC call.
- **Copy summary** — copies a markdown summary to the clipboard:
  ```
  # Backport PRs for "Fix: clustering syncing issue" #3125
  <!-- gh-rerunner: format="2" source_pr="https://github.com/owner/repo/pull/3125" source_pr_description_b64="..." ignore_ci="lint,build-docs" -->
  - [next/3.11.x.x](https://github.com/owner/repo/pull/4001) Merged
  - [next/3.12.x.x](https://github.com/owner/repo/pull/4002) 1 Review required; 1 label required; CI failed
  - [next/3.13.x.x](https://github.com/owner/repo/pull/4003) CI failed
  ```
  The list text is derived from the same status/hover data used by the UI.
  The metadata comment is machine-readable for `gh-rerunner` and includes the source PR URL, source PR description (base64), and optional `ignore_ci` patterns.
- **Auto-refresh** — statuses are refreshed automatically every 30 seconds.

**Tooltip** (hover any row) shows a breakdown: `CI: N passed, N failed, N running`, plus one line per configured approval check with its current state.

**Comment scanning** — recognises backport links from common bot/manual formats (`backport to`, `backporting to`, `cherry-pick to`, `ported to`, and backtick-quoted variants). If none match, falls back to collecting all same-repo PR links found in any comment.

---

## Installation

1. Install the [Tampermonkey](https://www.tampermonkey.net/) browser extension.
2. Click the install link below to open the script directly in Tampermonkey:
   - [Install Backport Tracker](https://raw.githubusercontent.com/StarlightIbuki/Github-Helper/main/backport-tracker.js)
3. Click **Install** in the Tampermonkey dialog.

## Auto-Update

The script includes `@updateURL` and `@downloadURL` headers pointing to this repository. Tampermonkey will check for updates automatically based on your configured update interval (default: once a day). You can also trigger a manual check via **Tampermonkey Dashboard → Check for updates**.

Version bumps in the `@version` header are what trigger the update prompt.

---

## CI Rerunner (`server-rerunner/`)

[`server-rerunner/`](server-rerunner/) is a Python server that polls GitHub Actions and retries failed jobs automatically — **no browser required**. It can run continuously on a server, keeping CI moving even when your laptop is off.

**Key commands:**

| Command | What it does |
|---|---|
| `gh-rerunner watch [TARGETS]...` | Start the supervisor: auto-rerun engine, TUI, and optional web UI + HTTP server |
| `gh-rerunner ls` | Export assigned PRs in Backport-Tracker-compatible markdown |
| `gh-rerunner logs [TARGETS]...` | Print failed-job logs (`--grep` to filter) |
| `gh-rerunner auth` | Authenticate via GitHub device flow (or `--pat` for PAT) |
| `gh-rerunner config set/show/clear` | Manage per-repo defaults |

### Recommended workflow

1. **Start the server once** on an always-on machine and keep it running:

   **macOS** — use `caffeinate` to prevent the machine from sleeping:
   ```bash
   caffeinate -i gh-rerunner watch --serve --no-tui --host 0.0.0.0
   ```

   **Linux** — inhibit sleep with `systemd-inhibit`:
   ```bash
   systemd-inhibit --what=sleep gh-rerunner watch --serve --no-tui --host 0.0.0.0
   ```

   **Windows** — keep-awake with PowerShell before launching:
   ```powershell
   powercfg /change standby-timeout-ac 0
   gh-rerunner watch --serve --no-tui --host 0.0.0.0
   ```

   **Alternatively**, run on a remote server where hibernation is not a concern — see [`server-rerunner/README.md`](server-rerunner/README.md) for a `nohup` example.

   Trackers persist across restarts in `~/.gh-rerunner-trackers.json`.

2. **Open the web UI** at `http://<host>:53210/` from any browser to see live CI status, add or remove targets, and trigger reruns manually. No terminal access needed after the server is running.

3. **Add targets** from the web UI directly, or pipe a Backport Tracker summary from your laptop:
   ```bash
   pbpaste | gh-rerunner watch
   ```

4. The server polls GitHub every 30 s (configurable), retries failed jobs automatically up to the configured limit, and keeps the web UI updated in real time.

### Backport Tracker → gh-rerunner workflow

1. Open your merged main-branch PR and let the Backport Tracker load all statuses.
2. Configure **Ignore CI job** in ⚙ Settings if your repo has jobs that are flaky or irrelevant (e.g. `lint`, `build-docs`).
3. Click **Copy summary**. The clipboard will contain a block like:
   ```
   # Backport PRs for "Fix: clustering syncing issue" #3125
   <!-- gh-rerunner: format="2" source_pr="https://github.com/owner/repo/pull/3125" source_pr_description_b64="..." ignore_ci="lint,build-docs" -->
   - [next/3.10.x](https://github.com/owner/repo/pull/100) Merged
   - [next/3.11.x](https://github.com/owner/repo/pull/101) Merged
   - [next/3.12.x](https://github.com/owner/repo/pull/102) CI pending
   - [next/3.13.x](https://github.com/owner/repo/pull/103) CI failed
   ```
4. Click **Watch on server** in the Backport Tracker sidebar. If `gh-rerunner` is running (default: `127.0.0.1:53210`), the userscript sends all PR URLs and the `ignore_ci` directive directly to the server via a single RPC call — no copy-paste needed.
5. The web UI shows live status for every tracked PR and run. Failed jobs are retried automatically; you can also trigger a manual rerun from the UI.
   - `Merged` entries are skipped automatically.
   - `CI pending` entries are watched until they settle.

   > **Alternative:** Click **Copy summary** and paste it into the server's web UI add-targets box, or pipe it from the CLI: `pbpaste | gh-rerunner watch`.

See [`server-rerunner/README.md`](server-rerunner/README.md) for installation, authentication, and the full CLI reference.
