from __future__ import annotations

import json
from pathlib import Path

from gh_rerunner.server import JSONRPCServer


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_server_startup_rehydrates_tracker_meta_from_cache(tmp_path: Path):
    cache_path = tmp_path / "cache.json"
    trackers_path = tmp_path / "trackers.json"
    sessions_path = tmp_path / "sessions.json"

    _write_json(
        cache_path,
        {
            "prs": {
                "org/repo#123": {
                    "ts": 1,
                    "branch": "mergify/bp/release-3.11/pr-123",
                    "detail": "CI failed",
                    "title": "chore(backport): fix flaky retry",
                    "source_pr": 456,
                    "backport_target": "release-3.11",
                    "is_merged": False,
                }
            }
        },
    )

    _write_json(
        trackers_path,
        [
            {
                "id": 1,
                "target": "https://github.com/org/repo/pull/123",
                "attempts_total": 2,
                "attempts_used": 0,
                "interval_seconds": 60,
                "active": True,
                "auto_rerun": True,
                "last_detail": "",
                "last_error": "",
                "last_action": "created",
                "last_updated": 0,
                "next_check_ts": 0,
                "ignore_jobs": [],
                "repo": "",
                "pr_number": 0,
                "pr_title": "",
                "pr_base_title": "",
                "is_backport": False,
                "backport_target": "",
                "backport_source_pr": 0,
                "jobs": [],
            }
        ],
    )

    server = JSONRPCServer(
        token=None,
        cache_path=cache_path,
        session_path=sessions_path,
        trackers_path=trackers_path,
    )

    trackers = server.snapshot_trackers()
    assert len(trackers) == 1
    tracker = trackers[0]
    assert tracker["repo"] == "org/repo"
    assert tracker["pr_number"] == 123
    assert tracker["last_detail"] == "CI failed"
    assert tracker["pr_title"] == "fix flaky retry"
    assert tracker["is_backport"] is True
    assert tracker["backport_target"] == "release-3.11"
    assert tracker["backport_source_pr"] == 456


def test_tracker_update_without_token_preserves_existing_meta(tmp_path: Path):
    cache_path = tmp_path / "cache.json"
    trackers_path = tmp_path / "trackers.json"
    sessions_path = tmp_path / "sessions.json"

    _write_json(
        cache_path,
        {
            "prs": {
                "org/repo#42": {
                    "ts": 1,
                    "branch": "feature/x",
                    "detail": "CI pending",
                    "title": "feat: keep cached metadata",
                    "source_pr": 0,
                    "backport_target": "",
                    "is_merged": False,
                }
            }
        },
    )

    _write_json(trackers_path, [])

    server = JSONRPCServer(
        token=None,
        cache_path=cache_path,
        session_path=sessions_path,
        trackers_path=trackers_path,
    )

    tracker = server.submit_tracker_sync("https://github.com/org/repo/pull/42")

    # Simulate one refresh tick while unauthenticated.
    server._update_tracker(tracker)

    assert tracker["last_error"] == "Server token missing"
    assert tracker["last_action"] == "idle"
    assert tracker["last_detail"] == "CI pending"
    assert tracker["pr_title"] == "keep cached metadata"
