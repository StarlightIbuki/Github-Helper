from __future__ import annotations

import re
from types import SimpleNamespace

import click

from gh_rerunner.cli import (
    _collect_failed_jobs,
    _format_markdown_summary,
    _render_context_lines,
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
            SimpleNamespace(conclusion=None),
        ],
    )

    failed_jobs = _collect_failed_jobs(run)

    assert [job.conclusion for job in failed_jobs] == ["failure", "timed_out"]
