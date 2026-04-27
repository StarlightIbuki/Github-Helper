"""Core CLI and polling logic for gh-rerunner."""
from __future__ import annotations

import re
import sys
import time
from collections import deque
from typing import Any, Optional

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

# Header lines emitted by backport-tracker:
#   # gh-rerunner: key=value
_HEADER_RE = re.compile(
    r"^#\s*gh-rerunner:\s*(?P<key>[a-z0-9_\-]+)=(?P<value>[^\r\n]*)$",
    re.MULTILINE | re.IGNORECASE,
)

# Markdown metadata comment emitted by backport-tracker, e.g.:
#   <!-- gh-rerunner: ignore_ci="lint,build" source_pr="..." -->
_META_COMMENT_RE = re.compile(
    r"^\s*<!--\s*gh-rerunner:\s*(?P<body>.*?)\s*-->\s*$",
    re.IGNORECASE,
)

_META_ATTR_RE = re.compile(
    r"([a-z0-9_\-]+)\s*=\s*\"([^\"]*)\"|([a-z0-9_\-]+)\s*=\s*([^\s\"]+)",
    re.IGNORECASE,
)

# Summary entry line:  [STATUS] branch: URL
_ENTRY_RE = re.compile(
    r"^\[(?P<status>[A-Z_]+)\]\s+[^:]+:\s+(?P<url>https://github\.com/\S+)\s*$",
)

# Markdown entry line: - [branch](URL) Detail text
_MD_ENTRY_RE = re.compile(
    r"^\s*-\s+\[[^\]]+\]\((?P<url>https://github\.com/\S+)\)\s*(?P<detail>.*)$",
)


def _short_target(url: str) -> str:
    """Compact target label for terminal output."""
    m = _URL_RE.search(url)
    if not m:
        return url
    return f"{m.group('repo')}:{m.group('kind')}:{m.group('num')}"


def _infer_status_from_markdown_detail(detail: str) -> str:
    """Infer a status hint from markdown detail text."""
    d = detail.strip().lower()
    if not d:
        return ""
    if "merged" in d:
        return "merged"
    if "closed" in d:
        return "closed"
    if "ci pending" in d or "fetching" in d:
        return "fetching"
    if "ci failed" in d:
        return "failure"
    if "ci passed" in d:
        return "success"
    return ""


def _parse_meta_comment_attrs(body: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for m in _META_ATTR_RE.finditer(body):
        if m.group(1):
            attrs[m.group(1).lower()] = m.group(2)
        elif m.group(3):
            attrs[m.group(3).lower()] = m.group(4)
    return attrs


def _is_metadata_line(line: str) -> bool:
    return _HEADER_RE.match(line) is not None or _META_COMMENT_RE.match(line) is not None


def _extract_line_url(url_part: str) -> Optional[str]:
    m = _URL_RE.search(url_part)
    if not m:
        return None
    return m.group(0)


def _append_entry(entries: list[SummaryEntry], seen: set[str], status: str, url: str) -> None:
    if url not in seen:
        seen.add(url)
        entries.append(SummaryEntry(status, url))


def _parse_structured_line(line: str) -> Optional[tuple[str, str]]:
    legacy = _ENTRY_RE.match(line)
    if legacy:
        clean = _extract_line_url(legacy.group("url"))
        if clean:
            return legacy.group("status"), clean

    markdown = _MD_ENTRY_RE.match(line)
    if markdown:
        clean = _extract_line_url(markdown.group("url"))
        if clean:
            status = _infer_status_from_markdown_detail(markdown.group("detail"))
            return status, clean

    return None


def _collect_metadata(text: str) -> tuple[dict[str, str], list[str]]:
    metadata: dict[str, str] = {}
    ignore_ci: list[str] = []

    for m in _HEADER_RE.finditer(text):
        key = m.group("key").lower().strip()
        value = m.group("value").strip()
        metadata[key] = value
        if key == "ignore_ci":
            ignore_ci = [j.strip() for j in value.split(",") if j.strip()]

    for line in text.splitlines():
        c = _META_COMMENT_RE.match(line)
        if not c:
            continue
        attrs = _parse_meta_comment_attrs(c.group("body"))
        metadata.update(attrs)
        if "ignore_ci" in attrs:
            ignore_ci = [j.strip() for j in attrs["ignore_ci"].split(",") if j.strip()]

    return metadata, ignore_ci


def _collect_structured_entries(text: str) -> list[SummaryEntry]:
    entries: list[SummaryEntry] = []
    seen: set[str] = set()
    for line in text.splitlines():
        parsed = _parse_structured_line(line)
        if not parsed:
            continue
        status, url = parsed
        _append_entry(entries, seen, status, url)
    return entries


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
    __slots__ = ("entries", "ignore_ci", "metadata")

    def __init__(
        self,
        entries: list[SummaryEntry],
        ignore_ci: list[str],
        metadata: dict[str, str],
    ) -> None:
        self.entries = entries
        self.ignore_ci = ignore_ci
        self.metadata = metadata


def _parse_summary(text: str) -> ParsedSummary:
    """Parse a backport-tracker copy-summary block.

    Extracts:
        - Per-PR status hints and URLs from either format:
            - Legacy: ``[STATUS] branch: url``
            - Markdown: ``- [branch](url) Detail text``
        - Optional metadata from either format:
            - ``# gh-rerunner: key=value``
            - ``<!-- gh-rerunner: key="value" -->``

    For plain URL lists (no status prefix) each URL is returned with
    status='' so the caller treats them as unknown.
    """
    # --- Config / metadata headers ---
    metadata, ignore_ci = _collect_metadata(text)

    # --- Structured entries (legacy + markdown) ---
    entries = _collect_structured_entries(text)

    # Also include bare URLs appended after a summary block.
    # Ignore metadata header lines so source_pr URLs don't become watch targets.
    # For URLs already present in structured entries, keep structured status hints.
    content_without_headers = "\n".join(
        line for line in text.splitlines()
        if not _is_metadata_line(line)
    )
    seen_urls = {e.url for e in entries}
    for url in _extract_urls(content_without_headers):
        if url not in seen_urls:
            entries.append(SummaryEntry("", url))
            seen_urls.add(url)

    return ParsedSummary(entries, ignore_ci, metadata)


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
    head commit. Returns an empty list when CI is already successful so the
    caller can skip watching this PR target."""
    pr = repo.get_pull(pr_number)
    sha = pr.head.sha
    live: list[WorkflowRun] = [
        r for r in repo.get_workflow_runs(head_sha=sha)
        if r.status != "completed" or r.conclusion in _RETRY_CONCLUSIONS
    ]
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
@click.option(
    "--window-lines",
    default=16,
    show_default=True,
    type=click.IntRange(6, 200),
    help="Number of rolling log lines shown in the fixed dashboard window.",
)
@click.option(
    "--rolling/--no-rolling",
    default=True,
    show_default=True,
    help="Render a fixed live dashboard instead of streaming line-by-line logs.",
)
def run_cmd(
    targets: tuple[str, ...],
    token: str,
    repo_opt: Optional[str],
    max_retries: int,
    interval: int,
    ignore_ci: str,
    window_lines: int,
    rolling: bool,
) -> None:
    """Watch and auto-rerun failed GitHub Actions runs.

    \b
    TARGETS can be any mix of:
      Run URL    https://github.com/owner/repo/actions/runs/12345
      PR URL     https://github.com/owner/repo/pull/456
      Run ID     12345   (requires --repo owner/repo)

    \b
    When stdin is a pipe, GitHub URLs are read from it automatically — with
        or without explicit TARGETS. This accepts copy-summary output from
        backport-tracker.js in either legacy or markdown format, e.g.:
            <!-- gh-rerunner: ignore_ci="lint,build" -->
            - [release-1.2](https://github.com/owner/repo/pull/456) CI failed
            - [release-1.3](https://github.com/owner/repo/pull/457) Merged

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
    active_entries: list[SummaryEntry] = []
    for entry in parsed.entries:
        if entry.status in _SKIP_STATUSES:
            click.echo(f"  skipping [{entry.status.upper()}] {entry.url}")
        elif entry.status in _WARN_STATUSES:
            click.echo(
                f"  warning: [{entry.status.upper()}] {entry.url} — "
                "CI data not yet loaded; will watch anyway"
            )
            active_entries.append(entry)
        else:
            active_entries.append(entry)

    if not active_entries:
        click.echo("Nothing to watch — all entries were skipped.")
        return

    # -----------------------------------------------------------------------
    # Resolve remaining targets → WorkflowRun objects
    # -----------------------------------------------------------------------
    all_runs: list[WorkflowRun] = []
    target_state: dict[str, dict] = {}

    for entry in active_entries:
        t = entry.url
        target_state.setdefault(
            t,
            {
                "source": t,
                "status_hint": entry.status or "unknown",
                "run_ids": [],
            },
        )
        try:
            resolved = _resolve_target(t, repo_opt, g)
        except GithubException as exc:
            raise click.ClickException(f"Cannot resolve {t!r}: {_exc_message(exc)}")
        if not resolved:
            click.echo(f"  skipping [SUCCESS] {t} — all CI runs already passed")
            continue
        all_runs.extend(resolved)
        for r in resolved:
            target_state[t]["run_ids"].append(r.id)
            click.echo(f"  + {r.html_url}")

    target_state = {k: v for k, v in target_state.items() if v["run_ids"]}

    if not all_runs:
        raise click.ClickException("No workflow runs found for the given targets.")

    # Keep a fixed terminal window when interactive; in non-tty contexts,
    # retain the previous streaming behavior.
    use_rolling = bool(rolling and sys.stdout.isatty())

    click.echo(
        f"\nWatching {len(all_runs)} run(s) across {len(target_state)} target(s) | "
        f"max-retries={max_retries} | interval={interval}s"
    )
    if use_rolling:
        click.echo("Live dashboard enabled. Press Ctrl-C to stop.\n")
    else:
        click.echo()

    # -----------------------------------------------------------------------
    # Per-run mutable state
    # -----------------------------------------------------------------------
    state: dict[int, dict] = {
        r.id: {
            "run": r,
            "repo_name": r.repository.full_name,
            "retries": 0,
            "done": False,
            "result": "pending",
            "last_status": "queued",
            "last_conclusion": None,
        }
        for r in all_runs
    }
    repo_cache: dict[str, Any] = {}
    run_to_target: dict[int, str] = {}
    for t, t_state in target_state.items():
        for run_id in t_state["run_ids"]:
            run_to_target[run_id] = t

    events: deque[str] = deque(maxlen=window_lines)

    def _event(msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        events.append(f"[{ts}] {msg}")
        if not use_rolling:
            click.echo(f"  {msg}")

    def _target_totals(t_url: str) -> tuple[int, int, int]:
        run_ids = target_state[t_url]["run_ids"]
        total = len(run_ids)
        done = sum(1 for rid in run_ids if state[rid]["done"])
        ok = sum(1 for rid in run_ids if state[rid]["result"] == "success")
        return total, done, ok

    def _target_label(t_url: str) -> str:
        total, done, ok = _target_totals(t_url)
        failed = done - ok
        if done < total:
            stage = "RUNNING"
        elif failed == 0:
            stage = "SUCCESS"
        else:
            stage = f"FAILED({failed})"
        return f"{stage} | {ok}/{total} success"

    def _render_dashboard() -> None:
        click.clear()
        click.echo("gh-rerunner live dashboard")
        click.echo(
            f"Targets={len(target_state)} | Runs={len(state)} | "
            f"max-retries={max_retries} | interval={interval}s"
        )
        click.echo()
        click.echo("Target totals:")
        for t in sorted(target_state):
            hint = target_state[t]["status_hint"].upper()
            click.echo(f"  {_short_target(t)} [{hint}] -> {_target_label(t)}")

        click.echo()
        click.echo("Run states:")
        for run_id in sorted(state):
            s = state[run_id]
            label = f"{s['repo_name']}#{run_id}"
            live = f"{s['last_status']}/{s['last_conclusion'] or '-'}"
            click.echo(
                f"  {label} | {s['result']} | retries {s['retries']}/{max_retries} | {live}"
            )

        click.echo()
        click.echo(f"Recent logs (last {window_lines}):")
        if events:
            for e in events:
                click.echo(f"  {e}")
        else:
            click.echo("  (no events yet)")
        click.echo()
        click.echo("Ctrl-C to stop")

    def _repo(name: str) -> Any:
        if name not in repo_cache:
            repo_cache[name] = g.get_repo(name)
        return repo_cache[name]

    # -----------------------------------------------------------------------
    # Polling loop
    # -----------------------------------------------------------------------
    try:
        while True:
            pending = [s for s in state.values() if not s["done"]]
            if not pending:
                _event("All runs finished.")
                break

            for s in pending:
                run_id: int = s["run"].id
                label = f"[{s['repo_name']}#{run_id}]"

                try:
                    run: WorkflowRun = _repo(s["repo_name"]).get_workflow_run(run_id)
                    s["run"] = run
                except GithubException as exc:
                    _event(f"{label} fetch error: {_exc_message(exc)}")
                    continue

                status, conclusion = run.status, run.conclusion
                s["last_status"] = status
                s["last_conclusion"] = conclusion

                if status != "completed":
                    continue

                if conclusion in _DONE_CONCLUSIONS:
                    _event(f"{label} {conclusion.upper()}")
                    s["done"] = True
                    s["result"] = "success"

                elif conclusion in _RETRY_CONCLUSIONS:
                    if effective_ignore_ci and _all_failures_ignored(run, effective_ignore_ci):
                        _event(
                            f"{label} {conclusion} — all failures are in ignored jobs, skipping rerun."
                        )
                        s["done"] = True
                        s["result"] = "ignored"
                    elif s["retries"] < max_retries:
                        s["retries"] += 1
                        _event(
                            f"{label} {conclusion} — rerunning ({s['retries']}/{max_retries})..."
                        )
                        try:
                            _trigger_rerun(run)
                            s["result"] = "retrying"
                        except GithubException as exc:
                            _event(f"{label} rerun API error: {_exc_message(exc)}")
                            s["done"] = True
                            s["result"] = "api_error"
                    else:
                        _event(
                            f"{label} {conclusion} — max retries ({max_retries}) reached."
                        )
                        s["done"] = True
                        s["result"] = "failed"

                else:
                    # cancelled, stale, etc.
                    _event(f"{label} concluded: {conclusion} — not retrying.")
                    s["done"] = True
                    s["result"] = "not_retryable"

            if use_rolling:
                _render_dashboard()
            time.sleep(interval)
    except KeyboardInterrupt:
        _event("Interrupted by user.")
        if use_rolling:
            _render_dashboard()

    success_count = sum(1 for s in state.values() if s["result"] == "success")
    failed_runs = [s for s in state.values() if s["result"] in {"failed", "api_error", "not_retryable"}]
    click.echo()
    click.echo(
        f"Final summary: {success_count}/{len(state)} run(s) succeeded, "
        f"{len(failed_runs)} need attention."
    )

    if not sys.stdout.isatty():
        return

    if failed_runs:
        choice = click.prompt(
            "Next action",
            type=click.Choice(["quit", "show-failures", "retry-failed-once"], case_sensitive=False),
            default="show-failures",
            show_choices=True,
        )
        if choice == "show-failures":
            click.echo("Runs needing attention:")
            for s in failed_runs:
                run = s["run"]
                target = run_to_target.get(run.id, "")
                click.echo(f"  - {_short_target(target)} -> {run.html_url} ({s['result']})")
        elif choice == "retry-failed-once":
            for s in failed_runs:
                run = s["run"]
                label = f"[{s['repo_name']}#{run.id}]"
                try:
                    _trigger_rerun(run)
                    click.echo(f"  {label} manual rerun triggered.")
                except GithubException as exc:
                    click.echo(f"  {label} manual rerun failed: {_exc_message(exc)}", err=True)
    else:
        choice = click.prompt(
            "All attempts succeeded. Next action",
            type=click.Choice(["quit", "show-targets"], case_sensitive=False),
            default="show-targets",
            show_choices=True,
        )
        if choice == "show-targets":
            click.echo("Successful targets:")
            for t in sorted(target_state):
                click.echo(f"  - {_short_target(t)}")
