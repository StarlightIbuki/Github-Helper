from __future__ import annotations

import re
from types import SimpleNamespace

import click

from gh_rerunner.cli import (
    _count_approved_reviews,
    _collect_failed_jobs,
    _format_markdown_summary,
    _get_cached_pr_status,
    _pr_requirements_status,
    _render_context_lines,
    _set_cached_pr_status,
)


def test_format_markdown_summary_matches_backport_style():
    text = _format_markdown_summary(
        "Assigned PRs for @octo",
        [
            ("feature-a", "https://github.com/org/repo/pull/1", "CI pending"),
            ("feature-b", "https://github.com/org/repo/pull/2", "CI failed"),
        ],
        {"source": "assigned-prs", "assignee": "octo"},
    )

    assert text == "\n".join([
        '# Assigned PRs for @octo',
        '<!-- gh-rerunner: format="2" source="assigned-prs" assignee="octo" -->',
        '- [feature-a](https://github.com/org/repo/pull/1) CI pending',
        '- [feature-b](https://github.com/org/repo/pull/2) CI failed',
    ])


def test_render_context_lines_highlights_matches_with_adjacent_lines():
    pattern = re.compile(r"needle")
    rendered = _render_context_lines(
        "alpha\nbeta\nneedle line\ngamma\ndelta",
        pattern,
        1,
    )

    assert rendered == [
        "     2 | beta",
        f">    3 | {click.style('needle', fg='yellow', bold=True)} line",
        "     4 | gamma",
    ]


def test_collect_failed_jobs_filters_retry_conclusions():
    run = SimpleNamespace(
        jobs=lambda: [
            SimpleNamespace(conclusion="failure"),
            SimpleNamespace(conclusion="success"),
            SimpleNamespace(conclusion="timed_out"),
            SimpleNamespace(conclusion="cancelled"),
            SimpleNamespace(conclusion=None),
        ],
    )

    failed_jobs = _collect_failed_jobs(run)

    assert [job.conclusion for job in failed_jobs] == ["failure", "timed_out", "cancelled"]


def test_count_approved_reviews_uses_latest_state_per_user():
    reviews = [
        SimpleNamespace(user=SimpleNamespace(login="alice"), state="APPROVED"),
        SimpleNamespace(user=SimpleNamespace(login="bob"), state="COMMENTED"),
        SimpleNamespace(user=SimpleNamespace(login="alice"), state="CHANGES_REQUESTED"),
        SimpleNamespace(user=SimpleNamespace(login="carol"), state="APPROVED"),
    ]
    pr = SimpleNamespace(get_reviews=lambda: reviews)

    assert _count_approved_reviews(pr) == 1


def test_pr_requirements_status_checks_labels_and_reviews():
    pr = SimpleNamespace(
        labels=[SimpleNamespace(name="needs-backport"), SimpleNamespace(name="team/ci")],
        get_reviews=lambda: [
            SimpleNamespace(user=SimpleNamespace(login="alice"), state="APPROVED"),
            SimpleNamespace(user=SimpleNamespace(login="bob"), state="APPROVED"),
        ],
    )
    ok, reason = _pr_requirements_status(
        pr,
        {"required_labels": ["backport", "team/ci"], "required_reviews": 2},
    )

    assert ok is True
    assert reason == "ok"


def test_pr_requirements_status_fails_when_missing_label():
    pr = SimpleNamespace(
        labels=[SimpleNamespace(name="bugfix")],
        get_reviews=lambda: [],
    )
    ok, reason = _pr_requirements_status(
        pr,
        {"required_labels": ["release"], "required_reviews": 0},
    )

    assert ok is False
    assert "missing labels" in reason


def test_pr_status_cache_round_trip():
    cache = {"prs": {}}

    _set_cached_pr_status(
        cache,
        repo_name="org/repo",
        pr_number=123,
        branch="feature/x",
        detail="CI failed",
        title="Fix flaky test",
    )

    cached = _get_cached_pr_status(cache, "org/repo", 123, ttl_seconds=99999)

    assert cached == {
        "branch": "feature/x",
        "detail": "CI failed",
        "title": "Fix flaky test",
    }


def test_pr_status_cache_expires_by_ttl():
    cache = {
        "prs": {
            "org/repo#1": {
                "ts": 1,
                "branch": "a",
                "detail": "CI pending",
                "title": "Old",
            }
        }
    }

    # TTL is 0 sec here, so entry is always stale.
    assert _get_cached_pr_status(cache, "org/repo", 1, ttl_seconds=0) is None
