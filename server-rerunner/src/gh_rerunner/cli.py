"""Core CLI and polling logic for gh-rerunner."""
from __future__ import annotations

import re
import sys
import time
from typing import Optional

import click
from github import Github, GithubException
from github.WorkflowRun import WorkflowRun

# Matches GitHub PR and Actions run URLs
_URL_RE = re.compile(
    r"https://github\.com/(?P<repo>[^/\s]+/[^/\s]+)/"
    r"(?P<kind>pull|actions/runs)/(?P<num>\d+)"
)

# Conclusions that mean the run is finished cleanly — no retry needed
_DONE_CONCLUSIONS = {"success", "neutral", "skipped"}

# Conclusions that warrant a rerun
_RETRY_CONCLUSIONS = {"failure", "timed_out", "action_required"}

# Summary status hints that mean there is nothing left to do
_SKIP_STATUSES = {"merged", "success", "closed"}

# Summary status hints that mean CI data is not yet available
_WARN_STATUSES = {"fetching"}

# Header line emitted by backport-tracker: "# gh-rerunner: ignore_ci=job1,job2"
_CONFIG_RE = re.compile(
    r"^#\s*gh-rerunner:\s*ignore_ci=(?P<jobs>[^\r\n]+)",
    re.MULTILINE | re.IGNORECASE,
)

# Summary entry line:  [STATUS] branch: URL
_ENTRY_RE = re.compile(
    r"^\[(?P<status>[A-Z_]+)\]\s+[^:]+:\s+(?P<url>https://github\.com/\S+)",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_urls(text: str) -> list[str]:
    """Pull all GitHub PR / run URLs out of an arbitrary block of text,
    preserving order and deduplicating."""
    seen: set[str] = set()
    result: list[str] = []
    for m in _URL_RE.finditer(text):
        url = m.group(0)
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


class SummaryEntry:
    __slots__ = ("status", "url")

    def __init__(self, status: str, url: str) -> None:
        self.status = status.lower()
        self.url = url


class ParsedSummary:
    """Result of parsing a backport-tracker copy-summary block."""
    __slots__ = ("entries", "ignore_ci")

    def __init__(
        self,
        entries: list[SummaryEntry],
        ignore_ci: list[str],
    ) -> None:
        self.entries = entries
        self.ignore_ci = ignore_ci


def _parse_summary(text: str) -> ParsedSummary:
    """Parse a backport-tracker copy-summary block.

    Extracts:
    - Per-PR status hints and URLs (``[STATUS] branch: url`` lines)
    - Optional ``# gh-rerunner: ignore_ci=job1,job2`` config header

    For plain URL lists (no status prefix) each URL is returned with
    status='' so the caller treats them as unknown.
    """
    # --- Config header ---
    ignore_ci: list[str] = []
    cfg_m = _CONFIG_RE.search(text)
    if cfg_m:
        ignore_ci = [j.strip() for j in cfg_m.group("jobs").split(",") if j.strip()]

    # --- Structured entries ---
    seen: set[str] = set()
    entries: list[SummaryEntry] = []

    for m in _ENTRY_RE.finditer(text):
        url = _URL_RE.search(m.group("url"))
        if not url:
            continue
        clean_url = url.group(0)
        if clean_url not in seen:
            seen.add(clean_url)
            entries.append(SummaryEntry(m.group("status"), clean_url))

    # Fall back to bare URL extraction if no structured entries found
    if not entries:
        for url in _extract_urls(text):
            entries.append(SummaryEntry("", url))

    return ParsedSummary(entries, ignore_ci)


def _all_failures_ignored(run: WorkflowRun, ignore_ci: list[str]) -> bool:
    """Return True if every failed job in *run* matches an ignore_ci pattern."""
    if not ignore_ci:
        return False
    failed_jobs = [
        job for job in run.jobs()
        if job.conclusion in {"failure", "timed_out"}
    ]
    if not failed_jobs:
        return False
    return all(
        any(pat.lower() in job.name.lower() for pat in ignore_ci)
        for job in failed_jobs
    )


def _resolve_pr_runs(repo, pr_number: int) -> list[WorkflowRun]:
    """Return all retryable (or still in-progress) workflow runs for a PR's
    head commit. Falls back to the single most-recent run when everything is
    already green so the caller can at least report its status."""
    pr = repo.get_pull(pr_number)
    sha = pr.head.sha
    live: list[WorkflowRun] = [
        r for r in repo.get_workflow_runs(head_sha=sha)
        if r.status != "completed" or r.conclusion in _RETRY_CONCLUSIONS
    ]
    if not live:
        all_runs = list(repo.get_workflow_runs(head_sha=sha))
        live = all_runs[:1]
    return live


def _resolve_target(
    target: str, repo_opt: Optional[str], g: Github
) -> list[WorkflowRun]:
    """Parse a target string into one or more WorkflowRun objects."""
    m = _URL_RE.search(target)
    if m:
        repo = g.get_repo(m.group("repo"))
        kind, num = m.group("kind"), int(m.group("num"))
        if kind == "actions/runs":
            return [repo.get_workflow_run(num)]
        return _resolve_pr_runs(repo, num)  # pull URL

    if target.isdigit():
        if not repo_opt:
            raise click.UsageError(
                f"--repo / -R is required when the target is a bare run ID ({target!r})."
            )
        return [g.get_repo(repo_opt).get_workflow_run(int(target))]

    raise click.UsageError(f"Cannot parse target: {target!r}")


def _trigger_rerun(run: WorkflowRun) -> None:
    """Rerun only the failed jobs. Falls back to a full rerun on older PyGithub."""
    if hasattr(run, "rerun_failed_jobs"):
        run.rerun_failed_jobs()
    else:
        run.rerun()


def _exc_message(exc: GithubException) -> str:
    if isinstance(exc.data, dict):
        return exc.data.get("message", str(exc))
    return str(exc)


# ---------------------------------------------------------------------------
# Token creation deep-links
# ---------------------------------------------------------------------------

# Fine-grained PAT — pre-fills description; user must pick repo + grant Actions write
_FINE_GRAINED_URL = (
    "https://github.com/settings/personal-access-tokens/new"
    "?description=gh-rerunner"
)
# Classic PAT — pre-selects the `repo` scope (which includes actions write)
_CLASSIC_URL = (
    "https://github.com/settings/tokens/new"
    "?scopes=repo&description=gh-rerunner"
)


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def main() -> None:
    """Headless GitHub Actions auto-rerunner.\n
    \b
    Run `gh-rerunner auth` to get a link for creating a token with the
    minimum required permissions.
    """


# ---------------------------------------------------------------------------
# auth subcommand
# ---------------------------------------------------------------------------

@main.command("auth")
def auth_cmd() -> None:
    """Print links to create a GitHub token with just-enough permissions.

    \b
    Two options — pick one:

    \b
    OPTION A — Fine-grained personal access token (recommended)
    Scope:  Actions  →  Read and write  (on the target repo only)
    This is the minimal permission required; no other scopes needed.

    \b
    OPTION B — Classic personal access token
    Scope:  repo  (grants Actions write as part of the bundle)
    Easier to set up but broader than strictly necessary.
    """
    click.echo("\n── Fine-grained PAT (recommended) ──────────────────────────")
    click.echo("1. Open the link below and create the token.")
    click.echo("2. Select the target repository.")
    click.echo("3. Under Repository permissions → Actions, choose Read and write.")
    click.echo(f"\n   {_FINE_GRAINED_URL}\n")

    click.echo("── Classic PAT (simpler, broader) ───────────────────────────")
    click.echo("1. Open the link below and create the token.")
    click.echo("2. The `repo` scope will be pre-selected — that is sufficient.")
    click.echo(f"\n   {_CLASSIC_URL}\n")

    click.echo("── Using the token ──────────────────────────────────────────")
    click.echo("  export GITHUB_TOKEN=<your-token>")
    click.echo("  gh-rerunner run https://github.com/owner/repo/pull/123\n")


# ---------------------------------------------------------------------------
# run subcommand
# ---------------------------------------------------------------------------

@main.command("run")
@click.argument("targets", nargs=-1, metavar="[TARGETS]...")
@click.option(
    "--token", "-t",
    envvar="GITHUB_TOKEN",
    required=True,
    help="GitHub personal access token. Falls back to $GITHUB_TOKEN.",
)
@click.option(
    "--repo", "-R", "repo_opt",
    default=None,
    metavar="OWNER/REPO",
    help="Repository in owner/repo format. Required for bare run IDs.",
)
@click.option(
    "--max-retries", "-n",
    default=3,
    show_default=True,
    help="Maximum rerun attempts per run before giving up.",
)
@click.option(
    "--interval", "-i",
    default=30,
    show_default=True,
    help="Polling interval in seconds.",
)
@click.option(
    "--ignore-ci",
    default="",
    metavar="JOB[,JOB...]",
    help=(
        "Comma-separated substrings of CI job names to ignore. "
        "A run whose only failures are in ignored jobs will not be rerun. "
        "Merged automatically with any ignore_ci encoded in the summary."
    ),
)
def run_cmd(
    targets: tuple[str, ...],
    token: str,
    repo_opt: Optional[str],
    max_retries: int,
    interval: int,
    ignore_ci: str,
) -> None:
    """Watch and auto-rerun failed GitHub Actions runs.

    \b
    TARGETS can be any mix of:
      Run URL    https://github.com/owner/repo/actions/runs/12345
      PR URL     https://github.com/owner/repo/pull/456
      Run ID     12345   (requires --repo owner/repo)

    \b
    When stdin is a pipe, GitHub URLs are read from it automatically — with
    or without explicit TARGETS. This accepts the copy-summary output from
    backport-tracker.js (including its encoded ignore_ci config header), e.g.:
      # gh-rerunner: ignore_ci=lint,build
      [OPEN]   release-1.2: https://github.com/owner/repo/pull/456
      [MERGED] release-1.3: https://github.com/owner/repo/pull/457

    \b
    Quick-start examples:
      gh-rerunner run https://github.com/owner/repo/actions/runs/12345
      gh-rerunner run --repo owner/repo 12345 --max-retries 5
      gh-rerunner run -t ghp_xxx        (interactive: paste URLs, empty line to start)
      pbpaste | gh-rerunner run
      cat summary.txt | gh-rerunner run -n 5 -i 60
    """
    g = Github(token)

    # --- Collect raw summary text from all input sources ---
    raw_text_parts: list[str] = []

    if not sys.stdin.isatty():
        raw_text_parts.append(sys.stdin.read())

    if targets:
        raw_text_parts.append("\n".join(targets))

    # Interactive fallback
    if not raw_text_parts and sys.stdout.isatty():
        click.echo(
            "Paste backport-tracker summary or enter URLs/run IDs, "
            "one per line.\nEmpty line or Ctrl-D to start:"
        )
        lines: list[str] = []
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                click.echo()
                break
            if not line:
                break
            lines.append(line)
        raw_text_parts.append("\n".join(lines))

    combined = "\n".join(raw_text_parts)
    parsed = _parse_summary(combined)

    # Merge ignore_ci from CLI option + summary header
    cli_ignore = [j.strip() for j in ignore_ci.split(",") if j.strip()]
    effective_ignore_ci = list(dict.fromkeys(parsed.ignore_ci + cli_ignore))  # dedup, ordered
    if effective_ignore_ci:
        click.echo(f"  Ignoring CI jobs matching: {', '.join(effective_ignore_ci)}")

    if not parsed.entries:
        raise click.UsageError(
            "No targets found. Pass URLs / run IDs, or pipe backport-tracker output."
        )

    # --- Pre-filter entries by status hint ---
    active_urls: list[str] = []
    for entry in parsed.entries:
        if entry.status in _SKIP_STATUSES:
            click.echo(f"  skipping [{entry.status.upper()}] {entry.url}")
        elif entry.status in _WARN_STATUSES:
            click.echo(
                f"  warning: [{entry.status.upper()}] {entry.url} — "
                "CI data not yet loaded; will watch anyway"
            )
            active_urls.append(entry.url)
        else:
            active_urls.append(entry.url)

    if not active_urls:
        click.echo("Nothing to watch — all entries were skipped.")
        return

    # -----------------------------------------------------------------------
    # Resolve remaining targets → WorkflowRun objects
    # -----------------------------------------------------------------------
    all_runs: list[WorkflowRun] = []
    for t in active_urls:
        try:
            resolved = _resolve_target(t, repo_opt, g)
        except GithubException as exc:
            raise click.ClickException(f"Cannot resolve {t!r}: {_exc_message(exc)}")
        all_runs.extend(resolved)
        for r in resolved:
            click.echo(f"  + {r.html_url}")

    if not all_runs:
        raise click.ClickException("No workflow runs found for the given targets.")

    click.echo(
        f"\nWatching {len(all_runs)} run(s) | "
        f"max-retries={max_retries} | interval={interval}s\n"
    )

    # -----------------------------------------------------------------------
    # Per-run mutable state
    # -----------------------------------------------------------------------
    state: dict[int, dict] = {
        r.id: {
            "run": r,
            "repo_name": r.repository.full_name,
            "retries": 0,
            "done": False,
        }
        for r in all_runs
    }
    repo_cache: dict[str, object] = {}

    def _repo(name: str):
        if name not in repo_cache:
            repo_cache[name] = g.get_repo(name)
        return repo_cache[name]

    # -----------------------------------------------------------------------
    # Polling loop
    # -----------------------------------------------------------------------
    while True:
        pending = [s for s in state.values() if not s["done"]]
        if not pending:
            click.echo("All runs finished.")
            break

        for s in pending:
            run_id: int = s["run"].id
            label = f"[{s['repo_name']}#{run_id}]"

            try:
                run: WorkflowRun = _repo(s["repo_name"]).get_workflow_run(run_id)
                s["run"] = run
            except GithubException as exc:
                click.echo(f"  {label} fetch error: {_exc_message(exc)}", err=True)
                continue

            status, conclusion = run.status, run.conclusion

            if status != "completed":
                click.echo(f"  {label} {status}...")
                continue

            if conclusion in _DONE_CONCLUSIONS:
                click.echo(f"  {label} {conclusion.upper()}")
                s["done"] = True

            elif conclusion in _RETRY_CONCLUSIONS:
                if effective_ignore_ci and _all_failures_ignored(run, effective_ignore_ci):
                    click.echo(
                        f"  {label} {conclusion} — all failures are in ignored jobs, skipping rerun."
                    )
                    s["done"] = True
                elif s["retries"] < max_retries:
                    s["retries"] += 1
                    click.echo(
                        f"  {label} {conclusion} — "
                        f"rerunning ({s['retries']}/{max_retries})..."
                    )
                    try:
                        _trigger_rerun(run)
                    except GithubException as exc:
                        click.echo(
                            f"  {label} rerun API error: {_exc_message(exc)}", err=True
                        )
                        s["done"] = True
                else:
                    click.echo(
                        f"  {label} {conclusion} — "
                        f"max retries ({max_retries}) reached."
                    )
                    s["done"] = True

            else:
                # cancelled, stale, etc.
                click.echo(f"  {label} concluded: {conclusion} — not retrying.")
                s["done"] = True

        time.sleep(interval)
