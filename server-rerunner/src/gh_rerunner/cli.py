"""Core CLI and polling logic for gh-rerunner."""
from __future__ import annotations

import io
import importlib
import json
import os
import select
import signal
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
from collections import deque
from typing import Any, Optional

try:
    import termios
    import tty
except ImportError:
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

import click
from github import Github, GithubException
from github.WorkflowRun import WorkflowRun

try:
    _rich_console = importlib.import_module("rich.console")
    _rich_live = importlib.import_module("rich.live")
    _rich_panel = importlib.import_module("rich.panel")
    _rich_table = importlib.import_module("rich.table")
    _rich_text = importlib.import_module("rich.text")

    _RichGroup = getattr(_rich_console, "Group", None)
    _RichLive = getattr(_rich_live, "Live", None)
    _RichPanel = getattr(_rich_panel, "Panel", None)
    _RichTable = getattr(_rich_table, "Table", None)
    _RichText = getattr(_rich_text, "Text", None)
    _RICH_AVAILABLE = all(x is not None for x in (_RichGroup, _RichLive, _RichPanel, _RichTable))
except Exception:
    _RichGroup = None
    _RichLive = None
    _RichPanel = None
    _RichTable = None
    _RichText = None
    _RICH_AVAILABLE = False

# Matches GitHub PR and Actions run URLs
_URL_RE = re.compile(
    r"https://github\.com/(?P<repo>[^/\s]+/[^/\s]+)/"
    r"(?P<kind>pull|actions/runs)/(?P<num>\d+)"
)

# Conclusions that mean the run is finished cleanly — no retry needed
_DONE_CONCLUSIONS = {"success", "neutral", "skipped"}

# Conclusions that warrant a rerun
_RETRY_CONCLUSIONS = {"failure", "timed_out", "action_required", "cancelled"}

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

_CONVENTIONAL_TITLE_PREFIX_RE = re.compile(
    r"^(?:\[[^\]]+\]\s+)?[a-zA-Z][a-zA-Z0-9_-]*(?:\([^)]+\))?(?:!)?:\s+"
)

_BACKPORT_TARGET_RE = re.compile(
    r"(?i)\b(?:backport|cherry[- ]pick)(?:\s+to)?[\s:/_-]+(?P<branch>[A-Za-z0-9._/-]+)"
)

# Mergify/bors bp/ style head refs:  bp/release-3.11/pr-15111  or  mergify/bp/release-3.11/pr-15111
_BACKPORT_BP_REF_RE = re.compile(
    r"(?:^|/)bp/(?P<branch>[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*)/pr-\d+$"
)

# Matches the source PR number in a backport PR description, e.g. "Backport of #15111"
_BACKPORT_SOURCE_PR_RE = re.compile(
    r"(?i)(?:backport|cherry[- ]pick)\b[^#\n]{0,60}#(\d+)"
)

_CONFIG_PATH = Path.home() / ".gh-rerunner.json"
_PR_STATUS_CACHE_PATH = Path.home() / ".gh-rerunner-cache.json"
_PR_STATUS_CACHE_TTL_SECONDS = 3600  # 1 hour
_SESSION_PATH = Path.home() / ".gh-rerunner-sessions.json"
_MAX_SESSIONS = 20
_DEFAULT_REPO_CONFIG = {
    "ignore_ci": [],
    "required_labels": [],
    "required_reviews": 0,
}


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


def _clean_pr_title(title: str) -> str:
    text = title.strip()
    if not text:
        return ""
    return _CONVENTIONAL_TITLE_PREFIX_RE.sub("", text)


def _extract_backport_target_branch(title: str, head_ref: str) -> str:
    # Try explicit backport/cherry-pick keyword in title or head ref
    for source in (title, head_ref):
        if not source:
            continue
        match = _BACKPORT_TARGET_RE.search(source)
        if match:
            branch = match.group("branch")
            # Branch patterns like 123-to-release/3.1.x should resolve to release/3.1.x.
            to_match = re.search(r"(?:^|[-_/])to[-_/](?P<branch>[A-Za-z0-9._/-]+)$", branch)
            if to_match:
                return to_match.group("branch")
            return branch

    # Mergify / bors  bp/<branch>/pr-<num>  style
    bp_match = _BACKPORT_BP_REF_RE.search(head_ref)
    if bp_match:
        return bp_match.group("branch")

    ref = head_ref.lower()
    if "backport" in ref:
        match = re.search(r"(?:to|into|for)[-_](?P<branch>[A-Za-z0-9._/-]+)", head_ref)
        if match:
            return match.group("branch")
    return ""


def _extract_backport_source_pr(body: str) -> int:
    """Return the source PR number mentioned in a backport PR description, or 0 if not found."""
    if not body:
        return 0
    m = _BACKPORT_SOURCE_PR_RE.search(body)
    if m:
        try:
            return int(m.group(1))
        except (ValueError, IndexError):
            return 0
    return 0


def _build_pr_display_meta(title: str, head_ref: str, body: str = "") -> dict[str, Any]:
    cleaned = _clean_pr_title(title) or title.strip()
    backport_target = _extract_backport_target_branch(title, head_ref)
    lowered = f"{title} {head_ref}".lower()
    is_backport = bool(backport_target) or "backport" in lowered or "cherry-pick" in lowered

    base_title = cleaned
    if is_backport:
        base_title = re.sub(r"(?i)\bbackport\b", "", cleaned).strip(" -:_") or cleaned

    source_pr = _extract_backport_source_pr(body) if is_backport else 0

    return {
        "pr_title": cleaned,
        "pr_base_title": base_title,
        "is_backport": is_backport,
        "backport_target": backport_target,
        "backport_source_pr": source_pr,
    }


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


def _load_user_config() -> dict[str, Any]:
    if not _CONFIG_PATH.exists():
        return {"repos": {}}
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"repos": {}}
    if not isinstance(data, dict):
        return {"repos": {}}
    repos = data.get("repos")
    if not isinstance(repos, dict):
        data["repos"] = {}
    return data


def _save_user_config(data: dict[str, Any]) -> None:
    if "repos" not in data or not isinstance(data["repos"], dict):
        data["repos"] = {}
    _CONFIG_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_pr_status_cache() -> dict[str, Any]:
    if not _PR_STATUS_CACHE_PATH.exists():
        return {"prs": {}}
    try:
        data = json.loads(_PR_STATUS_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"prs": {}}
    if not isinstance(data, dict):
        return {"prs": {}}
    prs = data.get("prs")
    if not isinstance(prs, dict):
        data["prs"] = {}
    return data


def _save_pr_status_cache(data: dict[str, Any]) -> None:
    if "prs" not in data or not isinstance(data["prs"], dict):
        data["prs"] = {}
    _PR_STATUS_CACHE_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _pr_cache_key(repo_name: str, pr_number: int) -> str:
    return f"{repo_name}#{pr_number}"


def _get_cached_pr_status(
    cache_data: dict[str, Any],
    repo_name: str,
    pr_number: int,
    ttl_seconds: int = _PR_STATUS_CACHE_TTL_SECONDS,
) -> Optional[dict[str, str]]:
    prs = cache_data.get("prs", {}) if isinstance(cache_data, dict) else {}
    if not isinstance(prs, dict):
        return None
    raw = prs.get(_pr_cache_key(repo_name, pr_number))
    if not isinstance(raw, dict):
        return None
    ts = raw.get("ts")
    if not isinstance(ts, (int, float)):
        return None
    # Skip TTL check for merged PRs—they don't change
    is_merged = bool(raw.get("is_merged", False))
    if not is_merged and time.time() - float(ts) > ttl_seconds:
        return None
    branch = raw.get("branch")
    detail = raw.get("detail")
    title = raw.get("title", "")
    if not isinstance(branch, str) or not isinstance(detail, str) or not isinstance(title, str):
        return None
    source_pr = raw.get("source_pr", 0)
    if not isinstance(source_pr, int):
        source_pr = 0
    return {"branch": branch, "detail": detail, "title": title, "source_pr": source_pr}


def _set_cached_pr_status(
    cache_data: dict[str, Any],
    repo_name: str,
    pr_number: int,
    branch: str,
    detail: str,
    title: str,
    source_pr: int = 0,
    is_merged: bool = False,
) -> None:
    prs = cache_data.setdefault("prs", {})
    if not isinstance(prs, dict):
        cache_data["prs"] = {}
        prs = cache_data["prs"]
    prs[_pr_cache_key(repo_name, pr_number)] = {
        "ts": time.time(),
        "branch": branch,
        "detail": detail,
        "title": title,
        "source_pr": source_pr,
        "is_merged": is_merged,
    }


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------

def _load_sessions() -> list[dict[str, Any]]:
    """Load saved sessions list from disk. Returns [] on error."""
    try:
        data = json.loads(_SESSION_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _save_session(raw_text: str, metadata: dict[str, str]) -> int:
    """Append a new session entry (raw summary text + metadata) and return its 1-based index."""
    sessions = _load_sessions()
    entry = {
        "ts": time.time(),
        "ts_human": time.strftime("%Y-%m-%d %H:%M:%S"),
        "raw": raw_text,
        "metadata": metadata,
    }
    sessions.append(entry)
    # Keep only the last N sessions
    if len(sessions) > _MAX_SESSIONS:
        sessions = sessions[-_MAX_SESSIONS:]
    _SESSION_PATH.write_text(json.dumps(sessions, indent=2) + "\n", encoding="utf-8")
    return len(sessions)


def _resolve_session_ref(ref: str) -> Optional[str]:
    """Resolve a session reference (#last, #N) to the stored raw text."""
    sessions = _load_sessions()
    if not sessions:
        return None
    ref = ref.lstrip("#").strip().lower()
    if ref == "last":
        return str(sessions[-1]["raw"])
    try:
        idx = int(ref)
        if 1 <= idx <= len(sessions):
            return str(sessions[idx - 1]["raw"])
    except (ValueError, IndexError):
        pass
    return None


def _repo_config(data: dict[str, Any], repo: str) -> dict[str, Any]:
    repos = data.get("repos") if isinstance(data, dict) else None
    cfg = repos.get(repo, {}) if isinstance(repos, dict) else {}
    if not isinstance(cfg, dict):
        cfg = {}
    ignore_ci = cfg.get("ignore_ci", [])
    required_labels = cfg.get("required_labels", [])
    required_reviews = cfg.get("required_reviews", 0)
    if not isinstance(ignore_ci, list):
        ignore_ci = []
    if not isinstance(required_labels, list):
        required_labels = []
    if not isinstance(required_reviews, int):
        required_reviews = 0
    return {
        "ignore_ci": [str(x).strip() for x in ignore_ci if str(x).strip()],
        "required_labels": [str(x).strip() for x in required_labels if str(x).strip()],
        "required_reviews": max(required_reviews, 0),
    }


def _target_repo(target: str, repo_opt: Optional[str]) -> Optional[str]:
    m = _URL_RE.search(target)
    if m:
        return m.group("repo")
    if target.isdigit():
        return repo_opt
    return None


def _target_pr_number(target: str) -> Optional[int]:
    m = _URL_RE.search(target)
    if not m or m.group("kind") != "pull":
        return None
    return int(m.group("num"))


def _count_approved_reviews(pr: Any) -> int:
    latest_state_by_user: dict[str, str] = {}
    for review in pr.get_reviews():
        user = getattr(getattr(review, "user", None), "login", None)
        if not user:
            continue
        latest_state_by_user[user] = str(getattr(review, "state", "")).upper()
    return sum(1 for state in latest_state_by_user.values() if state == "APPROVED")


def _pr_requirements_status(pr: Any, cfg: dict[str, Any]) -> tuple[bool, str]:
    required_labels = cfg.get("required_labels", [])
    required_reviews = int(cfg.get("required_reviews", 0) or 0)

    missing_labels: list[str] = []
    if required_labels:
        present = [str(getattr(label, "name", "")).lower() for label in getattr(pr, "labels", [])]
        for req in required_labels:
            req_l = str(req).lower()
            if not any(req_l in p for p in present):
                missing_labels.append(str(req))

    approved_count = 0
    if required_reviews > 0:
        approved_count = _count_approved_reviews(pr)

    if missing_labels:
        return False, f"missing labels: {', '.join(missing_labels)}"
    if required_reviews > 0 and approved_count < required_reviews:
        return False, f"approved reviews {approved_count}/{required_reviews}"
    return True, "ok"


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


def _format_markdown_summary(
    title: str,
    entries: list[tuple[str, str, str]],
    metadata: dict[str, str] | None = None,
) -> str:
    lines = [f"# {title}"]
    meta = {"format": "2"}
    if metadata:
        meta.update(metadata)
    attrs = " ".join(f'{key}="{value}"' for key, value in meta.items())
    lines.append(f"<!-- gh-rerunner: {attrs} -->")
    lines.extend(f"- [{branch}]({url}) {detail}" for branch, url, detail in entries)
    return "\n".join(lines)


def _pick_pr_status(repo, pr) -> str:
    if getattr(pr, "merged", False) or getattr(pr, "merged_at", None):
        return "Merged"
    if getattr(pr, "state", "").lower() == "closed":
        return "Closed"

    try:
        runs = list(repo.get_workflow_runs(head_sha=pr.head.sha))
    except GithubException:
        return "CI unavailable"

    if not runs:
        return "CI unavailable"

    has_pending = False
    has_failure = False
    has_success = False
    for run in runs:
        if run.status != "completed":
            has_pending = True
            continue
        conclusion = (run.conclusion or "").lower()
        if conclusion in _RETRY_CONCLUSIONS:
            has_failure = True
        elif conclusion in _DONE_CONCLUSIONS:
            has_success = True
        else:
            has_pending = True

    if has_failure:
        return "CI failed"
    if has_pending:
        return "CI pending"
    if has_success:
        return "CI passed"
    return "CI unavailable"


def _collect_assigned_pr_entries(
    g: Github,
    repo_opt: Optional[str] = None,
    include_closed: bool = False,
    filter_pattern: Optional[str] = None,
) -> tuple[str, list[tuple[str, str, str]], dict[str, str]]:
    login = g.get_user().login
    metadata: dict[str, str] = {"source": "assigned-prs", "assignee": login}
    if repo_opt:
        metadata["repo"] = repo_opt
    metadata["scope"] = "open+closed" if include_closed else "open"

    filter_re = _compile_regex(filter_pattern)
    pr_status_cache = _load_pr_status_cache()
    cache_changed = False

    queries = [f"is:pr assignee:{login} is:open"]
    if include_closed:
        queries.append(f"is:pr assignee:{login} is:closed")
    if repo_opt:
        queries = [f"repo:{repo_opt} {query}" for query in queries]

    seen_urls: set[str] = set()
    entries: list[tuple[str, str, str]] = []
    for query_index, query in enumerate(queries, 1):
        state = "open" if "is:open" in query else "closed"
        click.echo(f"  Fetching {state} assigned PRs...", err=True)
        count = 0
        for issue in g.search_issues(query=query, sort="updated", order="desc"):
            if not getattr(issue, "pull_request", None):
                continue
            url = issue.html_url
            if url in seen_urls:
                continue
            seen_urls.add(url)
            count += 1

            detail = "CI unavailable"
            branch = getattr(issue, "title", "PR")
            title = str(getattr(issue, "title", "") or "").strip()

            # Extract repo full name from the URL to avoid a lazy-loaded API
            # call on issue.repository (which costs 1 request per PR).
            url_m = _URL_RE.search(url)
            repo_full_name = url_m.group("repo") if url_m else str(getattr(issue.repository, "full_name", ""))

            cached = _get_cached_pr_status(pr_status_cache, repo_full_name, issue.number)
            if cached is not None:
                branch = cached["branch"] or branch
                detail = cached["detail"] or detail
                title = cached["title"] or title
                click.echo(f"    PR #{issue.number} — using cached status", err=True)
            else:
                click.echo(f"    PR #{issue.number} — fetching CI status...", err=True)
                try:
                    repo = g.get_repo(repo_full_name)
                    pr = repo.get_pull(issue.number)
                    branch = pr.head.ref or branch
                    detail = _pick_pr_status(repo, pr)
                    title = str(getattr(pr, "title", "") or title).strip()
                    body = str(getattr(pr, "body", "") or "")
                    source_pr = _extract_backport_source_pr(body)
                    is_merged = bool(getattr(pr, "merged", False))
                    _set_cached_pr_status(
                        pr_status_cache,
                        repo_full_name,
                        issue.number,
                        branch,
                        detail,
                        title,
                        source_pr=source_pr,
                        is_merged=is_merged,
                    )
                    cache_changed = True
                except GithubException:
                    pass

            if filter_re and not (
                filter_re.search(branch)
                or filter_re.search(url)
                or filter_re.search(title)
                or filter_re.search(repo_full_name)
            ):
                continue

            entries.append((branch, url, detail))

        if count == 0:
            click.echo(f"    (no {state} assigned PRs found)", err=True)
        else:
            click.echo(f"    {count} {state} assigned PR(s) processed", err=True)

    title = f"Assigned PRs for @{login}"
    if cache_changed:
        _save_pr_status_cache(pr_status_cache)
        click.echo(f"    cache: saved to {_PR_STATUS_CACHE_PATH}", err=True)
    return title, entries, metadata


def _download_binary(url: str, token: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "gh-rerunner",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise click.ClickException(f"Failed to fetch logs from {url}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise click.ClickException(f"Failed to fetch logs from {url}: {exc.reason}") from exc


def _decode_log_archive(blob: bytes) -> list[tuple[str, str]]:
    if blob[:2] == b"PK":
        entries: list[tuple[str, str]] = []
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            for name in archive.namelist():
                if name.endswith("/"):
                    continue
                with archive.open(name) as handle:
                    entries.append((name, handle.read().decode("utf-8", errors="replace")))
        return entries
    return [("workflow.log", blob.decode("utf-8", errors="replace"))]


def _compile_regex(pattern: Optional[str]) -> Optional[re.Pattern[str]]:
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise click.BadParameter(f"invalid regex: {exc}") from exc


def _highlight_pattern(text: str, pattern: Optional[re.Pattern[str]]) -> str:
    if not pattern:
        return text
    return pattern.sub(lambda match: click.style(match.group(0), fg="yellow", bold=True), text)


def _render_context_lines(
    text: str,
    pattern: Optional[re.Pattern[str]],
    context: int,
) -> list[str]:
    lines = text.splitlines()
    if not lines:
        return []
    if pattern is None:
        return [f"{index + 1:>5} | {_highlight_pattern(line, pattern)}" for index, line in enumerate(lines)]

    match_indexes = [index for index, line in enumerate(lines) if pattern.search(line)]
    if not match_indexes:
        return []

    ranges: list[tuple[int, int]] = []
    start = max(0, match_indexes[0] - context)
    end = min(len(lines) - 1, match_indexes[0] + context)
    for index in match_indexes[1:]:
        next_start = max(0, index - context)
        next_end = min(len(lines) - 1, index + context)
        if next_start <= end + 1:
            end = max(end, next_end)
        else:
            ranges.append((start, end))
            start, end = next_start, next_end
    ranges.append((start, end))

    rendered: list[str] = []
    for range_index, (start, end) in enumerate(ranges):
        if range_index > 0:
            rendered.append("    ...")
        for index in range(start, end + 1):
            prefix = ">" if pattern.search(lines[index]) else " "
            rendered.append(
                f"{prefix}{index + 1:>5} | {_highlight_pattern(lines[index], pattern)}"
            )
    return rendered


def _collect_failed_jobs(run: Any) -> list[Any]:
    return [
        job for job in run.jobs()
        if (job.conclusion or "").lower() in _RETRY_CONCLUSIONS
    ]


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
# config subcommands
# ---------------------------------------------------------------------------

@main.group("config")
def config_group() -> None:
    """Manage persistent per-repo defaults stored in ~/.gh-rerunner.json."""


@config_group.command("show")
@click.option("--repo", "repo_opt", default=None, metavar="OWNER/REPO", help="Show config for one repo only.")
def config_show_cmd(repo_opt: Optional[str]) -> None:
    cfg = _load_user_config()
    repos = cfg.get("repos", {}) if isinstance(cfg, dict) else {}
    if repo_opt:
        one = _repo_config(cfg, repo_opt)
        click.echo(json.dumps({"path": str(_CONFIG_PATH), "repo": repo_opt, "config": one}, indent=2))
        return
    click.echo(json.dumps({"path": str(_CONFIG_PATH), "repos": repos}, indent=2, sort_keys=True))


@config_group.command("set")
@click.option("--repo", "repo_opt", required=True, metavar="OWNER/REPO", help="Repository key to configure.")
@click.option("--ignore-ci", default=None, metavar="JOB[,JOB...]", help="Comma-separated ignored CI job substrings.")
@click.option("--required-labels", default=None, metavar="LABEL[,LABEL...]", help="Comma-separated required label substrings.")
@click.option("--required-reviews", default=None, type=click.IntRange(0, 100), help="Required number of approvals.")
def config_set_cmd(
    repo_opt: str,
    ignore_ci: Optional[str],
    required_labels: Optional[str],
    required_reviews: Optional[int],
) -> None:
    if ignore_ci is None and required_labels is None and required_reviews is None:
        raise click.UsageError("Provide at least one setting to update.")

    cfg = _load_user_config()
    repos = cfg.setdefault("repos", {})
    if not isinstance(repos, dict):
        cfg["repos"] = {}
        repos = cfg["repos"]

    current = _repo_config(cfg, repo_opt)
    if ignore_ci is not None:
        current["ignore_ci"] = [x.strip() for x in ignore_ci.split(",") if x.strip()]
    if required_labels is not None:
        current["required_labels"] = [x.strip() for x in required_labels.split(",") if x.strip()]
    if required_reviews is not None:
        current["required_reviews"] = required_reviews

    repos[repo_opt] = current
    _save_user_config(cfg)
    click.echo(f"Saved config for {repo_opt} at {_CONFIG_PATH}")


@config_group.command("clear")
@click.option("--repo", "repo_opt", required=True, metavar="OWNER/REPO", help="Repository key to remove.")
def config_clear_cmd(repo_opt: str) -> None:
    cfg = _load_user_config()
    repos = cfg.get("repos", {}) if isinstance(cfg, dict) else {}
    if isinstance(repos, dict) and repo_opt in repos:
        repos.pop(repo_opt, None)
        _save_user_config(cfg)
        click.echo(f"Removed config for {repo_opt} from {_CONFIG_PATH}")
    else:
        click.echo(f"No saved config for {repo_opt}")


# ---------------------------------------------------------------------------
# assigned-prs subcommand
# ---------------------------------------------------------------------------

@main.command("assigned-prs")
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
    help="Optional repository scope for assigned PR lookup.",
)
@click.option(
    "--include-closed/--open-only",
    default=False,
    show_default=True,
    help="Include closed assigned PRs in addition to open ones.",
)
@click.option(
    "--filter", "filter_pattern",
    default=None,
    metavar="REGEX",
    help="Optional regex to filter assigned PRs by branch/title/url/repo.",
)
def assigned_prs_cmd(
    token: str,
    repo_opt: Optional[str],
    include_closed: bool,
    filter_pattern: Optional[str],
) -> None:
    """Export assigned PRs in the same markdown format used by backport-tracker."""
    g = Github(token)
    click.echo("Fetching assigned PRs...", err=True)
    title, entries, metadata = _collect_assigned_pr_entries(
        g,
        repo_opt=repo_opt,
        include_closed=include_closed,
        filter_pattern=filter_pattern,
    )
    click.echo(f"Found {len(entries)} assigned PR(s)", err=True)
    if not entries:
        click.echo(f"# {title}")
        click.echo(f"<!-- gh-rerunner: format=\"2\" source=\"assigned-prs\" assignee=\"{g.get_user().login}\" -->")
        click.echo("No assigned PRs found.")
        return
    click.echo(f"Exporting to markdown...", err=True)
    click.echo(_format_markdown_summary(title, entries, metadata))


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
@click.option(
    "--assigned/--no-assigned",
    default=False,
    show_default=True,
    help="Shortcut: fetch assigned PRs and watch/rerun their workflow runs.",
)
@click.option(
    "--assigned-filter",
    default=None,
    metavar="REGEX",
    help="Regex filter for --assigned mode (branch/title/url/repo).",
)
@click.option(
    "--include-closed/--open-only",
    default=False,
    show_default=True,
    help="In --assigned mode, include closed assigned PRs (default: open only).",
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
    assigned: bool,
    assigned_filter: Optional[str],
    include_closed: bool,
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
      gh-rerunner run #last             (resume most recent session)
      gh-rerunner run #3                (resume session #3)
    """
    g = Github(token)
    user_cfg = _load_user_config()
    pr_status_cache = _load_pr_status_cache()
    pr_cache_changed = False

    # --- Collect raw summary text from all input sources ---
    raw_text_parts: list[str] = []

    if not sys.stdin.isatty():
        raw_text_parts.append(sys.stdin.read())

    # Check for session reference: a single arg like #last or #3
    session_resume = False
    if targets and len(targets) == 1 and targets[0].startswith("#"):
        resolved = _resolve_session_ref(targets[0])
        if resolved is None:
            sessions = _load_sessions()
            raise click.UsageError(
                f"No session found for {targets[0]!r}. "
                f"Available: #last or #1..#{len(sessions)}" if sessions else
                f"No saved sessions found."
            )
        click.echo(f"Resuming session {targets[0]!r}...", err=True)
        raw_text_parts.append(resolved)
        session_resume = True
    elif targets:
        raw_text_parts.append("\n".join(targets))

    if assigned:
        if targets:
            raise click.UsageError("Do not pass explicit TARGETS together with --assigned.")
        click.echo("Collecting assigned PRs for run shortcut...", err=True)
        assigned_title, assigned_entries, assigned_metadata = _collect_assigned_pr_entries(
            g,
            repo_opt=repo_opt,
            include_closed=include_closed,
            filter_pattern=assigned_filter,
        )
        if not assigned_entries:
            click.echo("No assigned PRs matched -- nothing to watch.")
            return
        click.echo(
            f"Using {len(assigned_entries)} assigned PR(s) from '{assigned_title}'.",
            err=True,
        )
        raw_text_parts.append(
            _format_markdown_summary(assigned_title, assigned_entries, assigned_metadata)
        )

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

    # Save this as a new session (unless we are already resuming one)
    if not session_resume and combined.strip():
        session_idx = _save_session(combined, {})
        click.echo(f"  Session #{session_idx} saved — resume with: gh-rerunner run #{session_idx}", err=True)

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
    run_ignore_ci: dict[int, list[str]] = {}

    for entry in active_entries:
        t = entry.url
        repo_name = _target_repo(t, repo_opt)
        repo_rules = _repo_config(user_cfg, repo_name) if repo_name else _DEFAULT_REPO_CONFIG
        target_state.setdefault(
            t,
            {
                "source": t,
                "status_hint": entry.status or "unknown",
                "run_ids": [],
                "pr_title": "",
                "pr_base_title": "",
                "is_backport": False,
                "backport_target": "",
                "backport_source_pr": 0,
            },
        )

        url_match = _URL_RE.search(t)
        pr_num = _target_pr_number(t)
        pr_obj: Any = None
        if url_match and url_match.group("kind") == "pull" and repo_name:
            required_labels = repo_rules.get("required_labels", [])
            required_reviews = int(repo_rules.get("required_reviews", 0) or 0)
            if required_labels or required_reviews > 0:
                try:
                    pr_obj = g.get_repo(repo_name).get_pull(int(url_match.group("num")))
                except GithubException as exc:
                    raise click.ClickException(f"Cannot load PR for requirements check {t!r}: {_exc_message(exc)}")
                ok, reason = _pr_requirements_status(pr_obj, repo_rules)
                if not ok:
                    click.echo(f"  skipping [REQUIREMENTS] {t} — {reason}")
                    continue

        if pr_num is not None and repo_name:
            cached = _get_cached_pr_status(pr_status_cache, repo_name, pr_num)
            if cached is not None and cached.get("title"):
                click.echo(f"  {_short_target(t)} — using cached PR metadata", err=True)
                meta = _build_pr_display_meta(cached.get("title", ""), cached.get("branch", ""))
                meta["backport_source_pr"] = int(cached.get("source_pr") or 0)
                target_state[t].update(meta)
            else:
                click.echo(f"  {_short_target(t)} — fetching PR metadata...", err=True)
                try:
                    if pr_obj is None:
                        pr_obj = g.get_repo(repo_name).get_pull(pr_num)
                    title = str(getattr(pr_obj, "title", "")).strip()
                    branch = str(getattr(getattr(pr_obj, "head", None), "ref", "") or "")
                    body = str(getattr(pr_obj, "body", "") or "")
                    meta = _build_pr_display_meta(title, branch, body)
                    target_state[t].update(meta)
                    is_merged = bool(getattr(pr_obj, "merged", False))
                    _set_cached_pr_status(
                        pr_status_cache,
                        repo_name,
                        pr_num,
                        branch=branch,
                        detail=str(entry.status or "unknown"),
                        title=title,
                        source_pr=meta.get("backport_source_pr", 0),
                        is_merged=is_merged,
                    )
                    pr_cache_changed = True
                except GithubException:
                    pass

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
            merged_ignore = list(dict.fromkeys(
                effective_ignore_ci + list(repo_rules.get("ignore_ci", []))
            ))
            run_ignore_ci[r.id] = merged_ignore
            click.echo(f"  + {r.html_url}")

    target_state = {k: v for k, v in target_state.items() if v["run_ids"]}

    if not all_runs:
        raise click.ClickException("No workflow runs found for the given targets.")

    if pr_cache_changed:
        _save_pr_status_cache(pr_status_cache)
        click.echo(f"  cache: saved to {_PR_STATUS_CACHE_PATH}", err=True)

    # Keep a fixed terminal window when interactive; in non-tty contexts,
    # retain the previous streaming behavior.
    use_rolling = bool(rolling and sys.stdout.isatty())
    use_rich_tui = bool(use_rolling and _RICH_AVAILABLE)

    click.echo(
        f"\nWatching {len(all_runs)} run(s) across {len(target_state)} target(s) | "
        f"max-retries={max_retries} | interval={interval}s"
    )
    if use_rich_tui:
        click.echo("Rich overwatch dashboard enabled. Press Ctrl-C to stop.\n")
    elif use_rolling:
        click.echo("Live dashboard enabled. Press Ctrl-C to stop.\n")
        if not _RICH_AVAILABLE:
            click.echo("Tip: install 'rich' for an enhanced TUI dashboard.")
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
    ui_state: dict[str, Any] = {
        "focus": "runs",
        "offset_targets": 0,
        "offset_runs": 0,
        "offset_logs": 0,
        "selected_targets": 0,
        "selected_runs": 0,
    }

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
        return f"{stage}\n{ok}/{total} ok"

    def _target_title_subtitle(t_url: str) -> str:
        """Plain-text subtitle for rolling/click output."""
        t_state = target_state.get(t_url, {})
        base = str(t_state.get("pr_title", "")).strip()
        if t_state.get("is_backport"):
            branch = str(t_state.get("backport_target", "")).strip() or "unknown"
            source = int(t_state.get("backport_source_pr") or 0)
            source_part = f"#{source} " if source else ""
            suffix = f"  \u21c6 backport {source_part}\u2192 {branch}"
            return f"{base}{suffix}" if base else suffix.strip()
        return base or "PR title unavailable"

    def _target_rich_cell(t_url: str, prefix: str) -> Any:
        """Build a Rich Text cell for the target table row."""
        t_state = target_state.get(t_url, {})
        short = _short_target(t_url)
        if _RichText is None:
            # Fallback: plain string
            return f"{prefix} {short}\n  {_target_title_subtitle(t_url)}"
        cell = _RichText()
        cell.append(f"{prefix} {short}")
        base = str(t_state.get("pr_title", "")).strip()
        if t_state.get("is_backport"):
            branch = str(t_state.get("backport_target", "")).strip() or "unknown"
            source = int(t_state.get("backport_source_pr") or 0)
            source_part = f"#{source} " if source else ""
            cell.append(f"  \u21c6 backport {source_part}\u2192 {branch}", style="dim yellow")
            if base:
                cell.append(f"\n  {base}", style="dim")
        else:
            if base:
                cell.append(f"\n  {base}", style="dim")
        return cell

    def _sorted_target_items() -> list[str]:
        return sorted(
            target_state,
            key=lambda t_url: (
                str(target_state[t_url].get("pr_base_title", "")).lower() or _short_target(t_url),
                bool(target_state[t_url].get("is_backport", False)),
                str(target_state[t_url].get("backport_target", "")).lower(),
                _short_target(t_url),
            ),
        )

    def _ensure_selected_visible(
        total: int,
        page_size: int,
        selected: int,
        offset: int,
    ) -> tuple[int, int]:
        if total <= 0:
            return 0, 0

        selected = max(0, min(selected, total - 1))
        offset = _clip_offset(total, page_size, offset)
        if selected < offset:
            offset = selected
        elif selected >= offset + max(1, page_size):
            offset = selected - max(1, page_size) + 1
        offset = _clip_offset(total, page_size, offset)
        return selected, offset

    def _clip_offset(total: int, page_size: int, offset: int) -> int:
        if total <= 0:
            return 0
        max_offset = max(0, total - max(1, page_size))
        return max(0, min(offset, max_offset))

    def _scroll_meta(total: int, page_size: int, offset: int) -> str:
        if total == 0:
            return "0/0"
        start = offset + 1
        end = min(total, offset + page_size)
        return f"{start}-{end}/{total}"

    def _pane_title(base: str, pane: str, total: int, page_size: int, offset: int) -> str:
        marker = "*" if ui_state["focus"] == pane else " "
        return f"{marker} {base} [{_scroll_meta(total, page_size, offset)}]"

    def _read_keys_nonblocking() -> list[str]:
        if not sys.stdin.isatty() or termios is None:
            return []

        keys: list[str] = []
        try:
            while True:
                ready, _, _ = select.select([sys.stdin], [], [], 0)
                if not ready:
                    break
                data = os.read(sys.stdin.fileno(), 32)
                if not data:
                    break
                text = data.decode("utf-8", errors="ignore")
                while "\x1b[A" in text:
                    keys.append("up")
                    text = text.replace("\x1b[A", "", 1)
                while "\x1b[B" in text:
                    keys.append("down")
                    text = text.replace("\x1b[B", "", 1)
                keys.extend(list(text))
        except (OSError, ValueError):
            return []
        return keys

    def _handle_ui_keys() -> bool:
        changed = False
        for key in _read_keys_nonblocking():
            target_items = _sorted_target_items()
            run_ids_sorted = sorted(rid for rid in state if state[rid]["result"] != "ignored")

            if key == "\t":
                order = ["targets", "runs", "logs"]
                idx = order.index(ui_state["focus"])
                ui_state["focus"] = order[(idx + 1) % len(order)]
                changed = True
                continue

            if key in {"j", "down"}:
                focus = ui_state["focus"]
                if focus in {"targets", "runs"}:
                    selected_key = f"selected_{focus}"
                    ui_state[selected_key] = min(
                        ui_state[selected_key] + 1,
                        max(0, (len(target_items) if focus == "targets" else len(run_ids_sorted)) - 1),
                    )
                else:
                    ui_state[f"offset_{focus}"] += 1
                changed = True
                continue

            if key in {"k", "up"}:
                focus = ui_state["focus"]
                if focus in {"targets", "runs"}:
                    selected_key = f"selected_{focus}"
                    ui_state[selected_key] = max(0, ui_state[selected_key] - 1)
                else:
                    ui_state[f"offset_{focus}"] = max(0, ui_state[f"offset_{focus}"] - 1)
                changed = True
                continue

            if key == "g":
                focus = ui_state["focus"]
                if focus in {"targets", "runs"}:
                    ui_state[f"selected_{focus}"] = 0
                else:
                    ui_state[f"offset_{focus}"] = 0
                changed = True
                continue

            if key == "G":
                focus = ui_state["focus"]
                if focus == "targets":
                    ui_state["selected_targets"] = max(0, len(target_items) - 1)
                elif focus == "runs":
                    ui_state["selected_runs"] = max(0, len(run_ids_sorted) - 1)
                else:
                    ui_state[f"offset_{focus}"] = 10**9
                changed = True
                continue

            if key in {"o", "\r", "\n"}:
                focus = ui_state["focus"]
                if focus == "targets" and target_items:
                    index = max(0, min(ui_state["selected_targets"], len(target_items) - 1))
                    target_url = target_items[index]
                    webbrowser.open_new_tab(target_url)
                    _event(f"Opened target in browser: {target_url}")
                    changed = True
                elif focus == "runs" and run_ids_sorted:
                    index = max(0, min(ui_state["selected_runs"], len(run_ids_sorted) - 1))
                    run_url = str(getattr(state[run_ids_sorted[index]]["run"], "html_url", ""))
                    if run_url:
                        webbrowser.open_new_tab(run_url)
                        _event(f"Opened run in browser: {run_url}")
                changed = True
                continue
        return changed

    def _render_dashboard() -> None:
        click.clear()
        click.echo("gh-rerunner live dashboard")
        click.echo(
            f"Targets={len(target_state)} | Runs={len(state)} | "
            f"max-retries={max_retries} | interval={interval}s"
        )
        if shutdown_requested:
            click.echo("Status: stopping (Ctrl-C received)")
        click.echo()
        click.echo("Target totals:")
        for t in _sorted_target_items():
            hint = target_state[t]["status_hint"].upper()
            click.echo(f"  {_short_target(t)} [{hint}] -> {_target_label(t)}")
            click.echo(f"    {_target_title_subtitle(t)}")

        click.echo()
        click.echo("Run states:")
        for run_id in sorted(state):
            s = state[run_id]
            label = f"{s['repo_name']}#{run_id}"
            live = f"{s['last_status']}/{s['last_conclusion'] or '-'}"
            run_target = run_to_target.get(run_id, "")
            pr_title = _target_title_subtitle(run_target) if run_target else "PR title unavailable"
            click.echo(
                f"  {label} | PR: {pr_title} | {s['result']} | retries {s['retries']}/{max_retries} | {live}"
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

    def _build_summary_lines() -> list[str]:
        success_count = sum(1 for s in state.values() if s["result"] == "success")
        failed = [s for s in state.values() if s["result"] in {"failed", "api_error", "not_retryable"}]
        lines = [f"Final: {success_count}/{len(state)} succeeded, {len(failed)} need attention."]
        for s in failed:
            target = run_to_target.get(s["run"].id, "")
            lines.append(f"  ✗ {_short_target(target)} → {s['run'].html_url} ({s['result']})")
        return lines

    def _build_rich_dashboard() -> Any:
        if not _RICH_AVAILABLE or _RichTable is None or _RichPanel is None or _RichGroup is None:
            return "Rich TUI unavailable"

        all_done = all(s["done"] for s in state.values())
        header = (
            f"Targets={len(target_state)} | Runs={len(state)} | "
            f"max-retries={max_retries} | interval={interval}s"
        )
        if shutdown_requested and all_done:
            header = f"{header} | status=done"
        elif shutdown_requested:
            header = f"{header} | status=stopping"
        else:
            header = f"{header} | status=running"

        # Get terminal size to decide layout and page sizes.
        try:
            term_size = os.get_terminal_size()
            term_width = term_size.columns
            term_height = term_size.lines
        except OSError:
            term_width = 80
            term_height = 24

        is_wide = term_width >= 140
        available_rows = max(12, term_height - 10)
        if is_wide:
            top_rows = max(4, min(available_rows - 4, available_rows // 2))
            targets_rows = top_rows
            runs_rows = top_rows
            logs_rows = max(4, available_rows - top_rows)
        else:
            pane_rows = max(4, available_rows // 3)
            targets_rows = pane_rows
            runs_rows = pane_rows
            logs_rows = max(4, available_rows - targets_rows - runs_rows)

        target_items = _sorted_target_items()
        run_ids_sorted = sorted(rid for rid in state if state[rid]["result"] != "ignored")
        log_items = list(events)

        ui_state["selected_targets"], ui_state["offset_targets"] = _ensure_selected_visible(
            len(target_items),
            targets_rows,
            ui_state["selected_targets"],
            ui_state["offset_targets"],
        )
        ui_state["selected_runs"], ui_state["offset_runs"] = _ensure_selected_visible(
            len(run_ids_sorted),
            runs_rows,
            ui_state["selected_runs"],
            ui_state["offset_runs"],
        )

        ui_state["offset_logs"] = _clip_offset(
            len(log_items), logs_rows, ui_state["offset_logs"]
        )

        visible_targets = target_items[
            ui_state["offset_targets"]: ui_state["offset_targets"] + targets_rows
        ]
        visible_runs = run_ids_sorted[
            ui_state["offset_runs"]: ui_state["offset_runs"] + runs_rows
        ]
        visible_logs = log_items[
            ui_state["offset_logs"]: ui_state["offset_logs"] + logs_rows
        ]

        target_table = _RichTable(
            title=_pane_title(
                "Target Totals",
                "targets",
                len(target_items),
                targets_rows,
                ui_state["offset_targets"],
            ),
            expand=True,
            min_width=40,
        )
        target_table.add_column("Target / PR", style="cyan", overflow="fold")
        target_table.add_column("Hint", style="magenta")
        target_table.add_column("State", style="green")
        for row_index, t in enumerate(visible_targets):
            hint = target_state[t]["status_hint"].upper()
            absolute_index = ui_state["offset_targets"] + row_index
            selected = absolute_index == ui_state["selected_targets"] and ui_state["focus"] == "targets"
            prefix = ">" if selected else " "
            cell = _target_rich_cell(t, prefix)
            target_table.add_row(
                cell,
                hint,
                _target_label(t),
                style="bold white on blue" if selected else "",
            )

        run_table = _RichTable(
            title=_pane_title(
                "Run States",
                "runs",
                len(run_ids_sorted),
                runs_rows,
                ui_state["offset_runs"],
            ),
            expand=True,
            min_width=40,
        )
        run_table.add_column("Run / PR", style="cyan", overflow="fold")
        run_table.add_column("Result", style="green")
        run_table.add_column("Retries", style="yellow")
        run_table.add_column("Live", style="magenta")
        for row_index, run_id in enumerate(visible_runs):
            s = state[run_id]
            label = f"{s['repo_name']}#{run_id}"
            live = f"{s['last_status']}/{s['last_conclusion'] or '-'}"
            run_target = run_to_target.get(run_id, "")
            absolute_index = ui_state["offset_runs"] + row_index
            selected = absolute_index == ui_state["selected_runs"] and ui_state["focus"] == "runs"
            prefix = ">" if selected else " "
            if _RichText is not None:
                cell: Any = _RichText()
                cell.append(f"{prefix} {label}")
                if run_target:
                    t_state = target_state.get(run_target, {})
                    base = str(t_state.get("pr_title", "")).strip()
                    if t_state.get("is_backport"):
                        branch = str(t_state.get("backport_target", "")).strip() or "unknown"
                        source = int(t_state.get("backport_source_pr") or 0)
                        source_part = f"#{source} " if source else ""
                        cell.append(f"  \u21c6 backport {source_part}\u2192 {branch}", style="dim yellow")
                        if base:
                            cell.append(f"\n  {base}", style="dim")
                    elif base:
                        cell.append(f"\n  {base}", style="dim")
            else:
                subtitle = _target_title_subtitle(run_target) if run_target else ""
                cell = f"{prefix} {label}" + (f"\n  {subtitle}" if subtitle else "")
            run_table.add_row(
                cell,
                str(s["result"]),
                f"{s['retries']}/{max_retries}",
                live,
                style="bold white on blue" if selected else "",
            )

        logs_table = _RichTable(
            title=_pane_title(
                "Recent Logs",
                "logs",
                len(log_items),
                logs_rows,
                ui_state["offset_logs"],
            ),
            expand=True,
        )
        logs_table.add_column("Event", overflow="fold")
        if visible_logs:
            for event in visible_logs:
                logs_table.add_row(event)
        else:
            logs_table.add_row("(no events yet)")

        all_done = all(s["done"] for s in state.values())
        if all_done or shutdown_requested:
            summary_lines = _build_summary_lines()
        else:
            summary_lines = []

        help_text = (
            "TAB switch pane | j/k or arrows move/select | o/Enter open browser | "
            "g/G top/bottom | Ctrl-C exit"
        )
        summary_text = ("\n" + "\n".join(summary_lines)) if summary_lines else ""
        header_panel = _RichPanel(
            f"{header}\n{help_text}{summary_text}",
            title="gh-rerunner overwatch",
            border_style="blue" if not (all_done or shutdown_requested) else "green" if all(s["result"] == "success" for s in state.values()) else "red",
        )

        # For wide screens, arrange target and run tables side-by-side
        if is_wide:
            # Import Columns for side-by-side layout (available in rich 12.0+)
            try:
                _rich_columns = importlib.import_module("rich.columns")
                _Columns = getattr(_rich_columns, "Columns", None)
                if _Columns:
                    return _RichGroup(
                        header_panel,
                        _Columns([target_table, run_table]),
                        logs_table,
                    )
            except (ImportError, AttributeError):
                pass

        # Default vertical layout for narrower terminals
        return _RichGroup(
            header_panel,
            target_table,
            run_table,
            logs_table,
        )

    def _repo(name: str) -> Any:
        if name not in repo_cache:
            repo_cache[name] = g.get_repo(name)
        return repo_cache[name]

    # -----------------------------------------------------------------------
    # Polling loop
    # -----------------------------------------------------------------------
    live_obj: Any = None
    window_resized = False
    original_sigwinch: Any = None
    original_sigint: Any = None
    original_tty: Any = None
    shutdown_requested = False

    def _on_window_resize(signum: int, frame: Any) -> None:
        """Signal handler for terminal window resize."""
        nonlocal window_resized
        window_resized = True

    def _on_sigint(signum: int, frame: Any) -> None:
        """SIGINT handler to integrate Ctrl-C with dashboard rendering."""
        nonlocal shutdown_requested
        if shutdown_requested:
            raise KeyboardInterrupt
        shutdown_requested = True
        _event("Interrupted by user (Ctrl-C) — shutting down...")

    # Set up signal handler for window resize (Unix/Linux/macOS)
    if hasattr(signal, "SIGWINCH"):
        original_sigwinch = signal.signal(signal.SIGWINCH, _on_window_resize)
    original_sigint = signal.signal(signal.SIGINT, _on_sigint)

    try:
        if use_rich_tui:
            if _RichLive is None:
                raise click.ClickException("Rich TUI requested but rich is not available.")
            if termios is not None and tty is not None and sys.stdin.isatty():
                original_tty = termios.tcgetattr(sys.stdin.fileno())
                tty.setcbreak(sys.stdin.fileno())
            live_obj = _RichLive(_build_rich_dashboard(), refresh_per_second=4)
            live_obj.start()

        while True:
            if shutdown_requested:
                break

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
                    ignore_list = run_ignore_ci.get(run_id, effective_ignore_ci)
                    if ignore_list and _all_failures_ignored(run, ignore_list):
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

            if use_rich_tui and live_obj is not None:
                live_obj.update(_build_rich_dashboard())
            elif use_rolling:
                _render_dashboard()

            if shutdown_requested:
                break

            # Sleep in short ticks so key presses are handled responsively.
            _KEY_TICK = 0.1
            elapsed = 0.0
            while elapsed < interval:
                if shutdown_requested:
                    break
                time.sleep(_KEY_TICK)
                elapsed += _KEY_TICK

                if window_resized:
                    window_resized = False
                    if use_rich_tui and live_obj is not None:
                        live_obj.update(_build_rich_dashboard())
                    elif use_rolling:
                        _render_dashboard()

                if use_rich_tui and _handle_ui_keys():
                    if live_obj is not None:
                        live_obj.update(_build_rich_dashboard())
    except KeyboardInterrupt:
        shutdown_requested = True
        _event("Interrupted by user.")
        if use_rich_tui and live_obj is not None:
            live_obj.update(_build_rich_dashboard())
        elif use_rolling:
            _render_dashboard()
    finally:
        # Render final summary before stopping TUI
        if use_rich_tui and live_obj is not None:
            live_obj.update(_build_rich_dashboard())
        elif use_rolling:
            _render_dashboard()
        # Restore original signal handlers and TTY
        if hasattr(signal, "SIGWINCH"):
            signal.signal(signal.SIGWINCH, original_sigwinch)
        if original_sigint is not None:
            signal.signal(signal.SIGINT, original_sigint)
        if original_tty is not None and termios is not None and sys.stdin.isatty():
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, original_tty)
        if live_obj is not None:
            live_obj.stop()

    success_count = sum(1 for s in state.values() if s["result"] == "success")
    failed_runs = [s for s in state.values() if s["result"] in {"failed", "api_error", "not_retryable"}]

    if not use_rich_tui:
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


# ---------------------------------------------------------------------------
# failed-logs subcommand
# ---------------------------------------------------------------------------

@main.command("failed-logs")
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
    "--grep", "grep_pattern",
    default=None,
    metavar="REGEX",
    help="Only print log lines matching REGEX, plus adjacent context lines.",
)
@click.option(
    "--context",
    default=2,
    show_default=True,
    type=click.IntRange(0, 50),
    help="Number of adjacent lines to show around each regex match.",
)
def failed_logs_cmd(
    targets: tuple[str, ...],
    token: str,
    repo_opt: Optional[str],
    grep_pattern: Optional[str],
    context: int,
) -> None:
    """Print failed workflow jobs and their logs, filtered by an optional regex."""
    g = Github(token)

    raw_text_parts: list[str] = []

    if not sys.stdin.isatty():
        raw_text_parts.append(sys.stdin.read())

    if targets:
        raw_text_parts.append("\n".join(targets))

    if not raw_text_parts and sys.stdout.isatty():
        click.echo(
            "Paste PR/run URLs or a backport-tracker summary, one per line.\n"
            "Empty line or Ctrl-D to start:"
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

    if not parsed.entries:
        raise click.UsageError(
            "No targets found. Pass URLs / run IDs, or pipe backport-tracker output."
        )

    pattern = _compile_regex(grep_pattern)
    any_failed_jobs = False

    click.echo(f"Resolving targets...", err=True)
    for entry in parsed.entries:
        try:
            resolved = _resolve_target(entry.url, repo_opt, g)
        except GithubException as exc:
            raise click.ClickException(f"Cannot resolve {entry.url!r}: {_exc_message(exc)}")

        for run in resolved:
            click.echo(f"Fetching failed jobs from {_short_target(run.html_url)}...", err=True)
            failed_jobs = _collect_failed_jobs(run)
            if not failed_jobs:
                click.echo(f"{_short_target(run.html_url)}: no failed jobs found")
                continue

            any_failed_jobs = True
            click.echo(f"{_short_target(run.html_url)}")

            for job_index, job in enumerate(failed_jobs, 1):
                click.echo(f"  job: {job.name} ({job.conclusion})")

                failed_steps = [
                    step for step in (job.steps or [])
                    if (step.conclusion or "").lower() in _RETRY_CONCLUSIONS
                ]
                if failed_steps:
                    click.echo("    failed steps:")
                    for step in failed_steps:
                        click.echo(f"      - {step.name} ({step.conclusion})")
                else:
                    click.echo("    failed steps: (none reported by API)")

                click.echo(f"    Downloading logs ({job_index}/{len(failed_jobs)})...", err=True)
                blob = _download_binary(job.logs_url, token)
                click.echo(f"    Parsing logs...", err=True)
                for file_name, log_text in _decode_log_archive(blob):
                    click.echo(f"    log: {file_name}")
                    rendered = _render_context_lines(log_text, pattern, context)
                    if pattern and not rendered:
                        click.echo("      (no matching lines)")
                        continue
                    for line in rendered:
                        click.echo(f"      {line}")

    if not any_failed_jobs:
        click.echo("No failed jobs found in the requested targets.")
