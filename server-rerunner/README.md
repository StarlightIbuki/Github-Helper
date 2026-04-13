# gh-rerunner

A headless Python CLI that polls GitHub Actions workflow runs and automatically re-runs failed jobs — designed to keep running on a server even when your laptop is off.

Pairs naturally with the **Backport Tracker** userscript: pipe its copy-summary output directly into `gh-rerunner run` for a fast workflow.

## Requirements

- Python 3.9+
- A GitHub personal access token (see [Authentication](#authentication))

## Installation

```bash
cd server-rerunner
pip install -e .
```

This installs the `gh-rerunner` command.

## Authentication

You need a GitHub token with permission to read and trigger workflow reruns.

Run the following to get pre-filled token creation links with the minimum required scopes:

```
gh-rerunner auth
```

### Fine-grained PAT (recommended)

1. Follow the link printed by `gh-rerunner auth`.
2. Select the target repository.
3. Under **Repository permissions → Actions**, choose **Read and write**.
4. No other permissions are needed.

### Classic PAT (simpler, broader)

1. Follow the classic link printed by `gh-rerunner auth`.
2. The `repo` scope will be pre-selected — that is sufficient.

> The `repo` scope grants write access to all repository resources, not just Actions.
> Use a fine-grained PAT if you want tighter access control.

### Providing the token

Set the token in your environment (recommended for scripts and servers):

```bash
export GITHUB_TOKEN=ghp_your_token_here
```

Or pass it inline per command:

```bash
gh-rerunner run -t ghp_your_token_here <target>
```

---

## Usage

### `gh-rerunner auth`

Print token creation links for the minimum required permissions.

```
gh-rerunner auth
```

---

### `gh-rerunner run`

Watch one or more workflow runs / PRs and rerun failed jobs automatically.

```
gh-rerunner run [OPTIONS] [TARGETS]...
```

**TARGETS** can be any mix of:

| Format | Example |
|---|---|
| Actions run URL | `https://github.com/owner/repo/actions/runs/12345` |
| PR URL | `https://github.com/owner/repo/pull/456` |
| Bare run ID | `12345` (requires `--repo owner/repo`) |

**Options:**

| Flag | Default | Description |
|---|---|---|
| `-t / --token` | `$GITHUB_TOKEN` | GitHub PAT |
| `-R / --repo OWNER/REPO` | — | Required for bare run IDs |
| `-n / --max-retries N` | `3` | Max rerun attempts per run |
| `-i / --interval SECS` | `30` | Polling interval in seconds |

---

## Examples

```bash
# Watch a single Actions run
gh-rerunner run https://github.com/owner/repo/actions/runs/12345

# Watch all runs for a PR's head commit
gh-rerunner run https://github.com/owner/repo/pull/456

# Bare run ID — requires --repo
gh-rerunner run --repo owner/repo 12345

# Up to 5 retries, check every minute
gh-rerunner run -n 5 -i 60 https://github.com/owner/repo/pull/456

# Pipe backport-tracker "Copy summary" output directly (URLs extracted automatically)
pbpaste | gh-rerunner run

# Combine piped input with an explicit extra target
pbpaste | gh-rerunner run https://github.com/owner/repo/pull/999

# Read from a saved summary file
cat summary.txt | gh-rerunner run -n 5
```

### Backport Tracker integration

In the Backport Tracker sidebar panel, click **Copy summary**. The output looks like:

```
[OPEN]   release-1.2: https://github.com/owner/repo/pull/111
[MERGED] release-1.3: https://github.com/owner/repo/pull/112
[OPEN]   release-1.4: https://github.com/owner/repo/pull/113
```

Pipe it straight to `gh-rerunner run` — it extracts all GitHub URLs automatically:

```bash
pbpaste | gh-rerunner run -n 3
```

---

## Running on a server

```bash
# Install into a venv
python -m venv .venv && source .venv/bin/activate
pip install -e /path/to/server-rerunner

# Set the token permanently in your shell profile or systemd environment
export GITHUB_TOKEN=ghp_...

# Run in the background with nohup, keeping logs
nohup gh-rerunner run -n 5 -i 60 https://github.com/owner/repo/pull/456 \
  >> ~/rerunner.log 2>&1 &
```

For a persistent service, a `systemd` unit or `tmux`/`screen` session works well.
