"""Tests for benchmark/build_dataset.py.

Pure-function tests cover all helpers without any I/O.
The integration test wires respx (GitHub mock) + a real temp SQLite DB to
verify the full pipeline end-to-end with one repo and one issue-PR pair.
"""

from __future__ import annotations

import pathlib
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from benchmark.build_dataset import (
    _build_pair_row,
    _extract_merged_prs,
    _first_fixer_comment,
    _time_to_merge_hours,
    _truncate,
    build,
)
from benchmark.db import open_db

_GQL_URL = "https://api.github.com/graphql"
_REST_BASE = "https://api.github.com"
_TOKEN = "ghp_fake"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _pr(
    number: int = 99,
    merged: bool = True,
    merged_at: str = "2024-01-10T12:00:00Z",
    author: str = "alice",
) -> dict[str, Any]:
    return {
        "number": number,
        "merged": merged,
        "mergedAt": merged_at,
        "additions": 10,
        "deletions": 5,
        "changedFiles": 2,
        "reviews": {"totalCount": 2},
        "files": {"nodes": [{"path": "src/foo.py"}, {"path": "tests/test_foo.py"}]},
        "author": {"login": author},
    }


def _issue(
    number: int = 42,
    title: str = "Fix the formatting",
    body: str = "Steps: ...",
    labels: list[str] | None = None,
    timeline_nodes: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "body": body,
        "createdAt": "2024-01-01T00:00:00Z",
        "closedAt": "2024-01-10T00:00:00Z",
        "labels": {"nodes": [{"name": lb} for lb in (labels or ["bug"])]},
        "timelineItems": {"nodes": timeline_nodes or []},
        "comments": {
            "nodes": comments or [{"createdAt": "2024-01-02T00:00:00Z", "author": {"login": "alice"}}]
        },
    }


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------


class TestTruncate:
    def test_short_text_returned_unchanged(self) -> None:
        assert _truncate("hello") == "hello"

    def test_none_returned_as_none(self) -> None:
        assert _truncate(None) is None

    def test_empty_string_returned_as_is(self) -> None:
        assert _truncate("") == ""

    def test_exact_limit_not_truncated(self) -> None:
        text = "x" * 4000
        assert _truncate(text) == text

    def test_over_limit_keeps_start_and_end(self) -> None:
        text = "S" * 2000 + "M" * 100 + "E" * 2000
        result = _truncate(text, max_chars=4000)
        assert result is not None
        assert len(result) == 4000
        assert result[:2000] == "S" * 2000
        assert result[-2000:] == "E" * 2000

    def test_middle_content_dropped(self) -> None:
        text = "START" + "x" * 5000 + "END"
        result = _truncate(text, max_chars=10)
        assert result == "START" + "END"


# ---------------------------------------------------------------------------
# _extract_merged_prs
# ---------------------------------------------------------------------------


class TestExtractMergedPrs:
    def test_cross_referenced_merged_pr_included(self) -> None:
        issue = _issue(timeline_nodes=[{"source": _pr()}])
        assert len(_extract_merged_prs(issue)) == 1

    def test_closed_event_merged_pr_included(self) -> None:
        issue = _issue(timeline_nodes=[{"closer": _pr()}])
        assert len(_extract_merged_prs(issue)) == 1

    def test_unmerged_pr_excluded(self) -> None:
        issue = _issue(timeline_nodes=[{"source": _pr(merged=False)}])
        assert _extract_merged_prs(issue) == []

    def test_non_pr_source_excluded(self) -> None:
        # GraphQL returns {} when the union member doesn't match PullRequest
        issue = _issue(timeline_nodes=[{"source": {}}])
        assert _extract_merged_prs(issue) == []

    def test_duplicate_pr_numbers_deduplicated(self) -> None:
        pr = _pr(number=99)
        # Same PR referenced via two different event types
        issue = _issue(timeline_nodes=[{"source": pr}, {"closer": pr}])
        assert len(_extract_merged_prs(issue)) == 1

    def test_two_distinct_prs_both_included(self) -> None:
        issue = _issue(
            timeline_nodes=[{"source": _pr(number=10)}, {"source": _pr(number=20)}]
        )
        assert len(_extract_merged_prs(issue)) == 2

    def test_empty_timeline_returns_empty_list(self) -> None:
        assert _extract_merged_prs(_issue(timeline_nodes=[])) == []


# ---------------------------------------------------------------------------
# _first_fixer_comment
# ---------------------------------------------------------------------------


class TestFirstFixerComment:
    def test_returns_timestamp_of_fixers_first_comment(self) -> None:
        issue = _issue(
            comments=[
                {"createdAt": "2024-01-02T00:00:00Z", "author": {"login": "bystander"}},
                {"createdAt": "2024-01-03T00:00:00Z", "author": {"login": "alice"}},
            ]
        )
        assert _first_fixer_comment(issue, "alice") == "2024-01-03T00:00:00Z"

    def test_returns_none_when_fixer_never_commented(self) -> None:
        issue = _issue(comments=[])
        assert _first_fixer_comment(issue, "alice") is None

    def test_returns_none_for_deleted_author(self) -> None:
        # GitHub returns null for the author of deleted accounts
        issue = _issue(comments=[{"createdAt": "2024-01-02T00:00:00Z", "author": None}])
        assert _first_fixer_comment(issue, "alice") is None


# ---------------------------------------------------------------------------
# _time_to_merge_hours
# ---------------------------------------------------------------------------


class TestTimeToMergeHours:
    def test_computes_hours_between_comment_and_merge(self) -> None:
        result = _time_to_merge_hours("2024-01-01T00:00:00Z", "2024-01-02T12:00:00Z")
        assert result == pytest.approx(36.0)

    def test_returns_none_when_first_comment_missing(self) -> None:
        assert _time_to_merge_hours(None, "2024-01-02T00:00:00Z") is None

    def test_returns_none_when_merged_at_missing(self) -> None:
        assert _time_to_merge_hours("2024-01-01T00:00:00Z", None) is None

    def test_returns_none_for_negative_delta(self) -> None:
        # PR merged before the comment timestamp — treat as missing data
        assert _time_to_merge_hours("2024-01-10T00:00:00Z", "2024-01-01T00:00:00Z") is None

    def test_zero_delta_allowed(self) -> None:
        result = _time_to_merge_hours("2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z")
        assert result == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _build_pair_row
# ---------------------------------------------------------------------------


class TestBuildPairRow:
    def test_fields_mapped_correctly(self) -> None:
        pr = _pr()
        iss = _issue(number=42, labels=["bug", "good first issue"])
        row = _build_pair_row(repo_id=1, issue=iss, pr=pr, contribution_map={"alice": 15})

        assert row.repo_id == 1
        assert row.issue_number == 42
        assert row.pr_number == 99
        assert row.fixer_login == "alice"
        assert row.fixer_prior_contributions == 15
        assert row.issue_labels == ["bug", "good first issue"]
        assert row.files_changed_paths == ["src/foo.py", "tests/test_foo.py"]
        assert row.lines_added == 10
        assert row.lines_deleted == 5
        assert row.review_rounds == 2

    def test_time_to_merge_computed(self) -> None:
        pr = _pr(merged_at="2024-01-10T12:00:00Z")
        iss = _issue(
            comments=[{"createdAt": "2024-01-10T00:00:00Z", "author": {"login": "alice"}}]
        )
        row = _build_pair_row(1, iss, pr, {})
        assert row.time_to_merge_hours == pytest.approx(12.0)

    def test_unknown_fixer_contribution_is_none(self) -> None:
        row = _build_pair_row(1, _issue(), _pr(), contribution_map={})
        assert row.fixer_prior_contributions is None

    def test_null_author_handled(self) -> None:
        pr = {**_pr(), "author": None}
        row = _build_pair_row(1, _issue(), pr, {})
        assert row.fixer_login is None
        assert row.fixer_prior_contributions is None
        assert row.first_contributor_comment_at is None

    def test_long_body_truncated(self) -> None:
        body = "x" * 10_000
        row = _build_pair_row(1, _issue(body=body), _pr(), {})
        assert len(row.issue_body) == 4000  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Integration: build() with mocked GitHub + real SQLite
# ---------------------------------------------------------------------------


def _gql_response(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Reset": "9999999999"},
        json={"data": {"rateLimit": {"remaining": 4999}, **data}},
    )


class TestBuildIntegration:
    async def test_inserts_one_pair_for_one_repo(self, tmp_path: pathlib.Path) -> None:
        db_path = tmp_path / "test.db"
        pr_node = _pr()
        issue_node = _issue(timeline_nodes=[{"source": pr_node}])

        with respx.mock:
            # Only psf/black will be processed because we patch BENCHMARK_REPOS
            respx.get(f"{_REST_BASE}/repos/psf/black").mock(
                return_value=httpx.Response(200, json={"stargazers_count": 39_000})
            )
            respx.get(f"{_REST_BASE}/repos/psf/black/contributors").mock(
                return_value=httpx.Response(200, json=[{"login": "alice", "contributions": 15}])
            )
            respx.post(_GQL_URL).mock(
                return_value=_gql_response(
                    {
                        "repository": {
                            "issues": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [issue_node],
                            }
                        }
                    }
                )
            )

            from benchmark import repos as repos_module
            from benchmark.repos import Repo

            with patch.object(repos_module, "BENCHMARK_REPOS", [Repo("psf/black", "Python")]):
                with patch("alarina.config.get_github_token", return_value=_TOKEN):
                    await build(db_path=db_path)

        async with open_db(db_path) as conn:
            cursor = await conn.execute("SELECT COUNT(*) as n FROM issue_pr_pairs")
            row = await cursor.fetchone()
        assert row["n"] == 1

    async def test_failed_repo_does_not_abort_run(self, tmp_path: pathlib.Path) -> None:
        db_path = tmp_path / "test.db"
        pr_node = _pr()
        issue_node = _issue(timeline_nodes=[{"source": pr_node}])

        with respx.mock:
            # first repo (black) returns a 500 — should be skipped
            respx.get(f"{_REST_BASE}/repos/psf/black").mock(
                return_value=httpx.Response(500)
            )
            # second repo (flask) succeeds
            respx.get(f"{_REST_BASE}/repos/pallets/flask").mock(
                return_value=httpx.Response(200, json={"stargazers_count": 68_000})
            )
            respx.get(f"{_REST_BASE}/repos/pallets/flask/contributors").mock(
                return_value=httpx.Response(200, json=[{"login": "alice", "contributions": 5}])
            )
            respx.post(_GQL_URL).mock(
                return_value=_gql_response(
                    {
                        "repository": {
                            "issues": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [issue_node],
                            }
                        }
                    }
                )
            )

            from benchmark import repos as repos_module
            from benchmark.repos import Repo

            with patch.object(
                repos_module,
                "BENCHMARK_REPOS",
                [Repo("psf/black", "Python"), Repo("pallets/flask", "Python")],
            ):
                with patch("alarina.config.get_github_token", return_value=_TOKEN):
                    await build(db_path=db_path)  # must not raise

        async with open_db(db_path) as conn:
            cursor = await conn.execute("SELECT COUNT(*) as n FROM issue_pr_pairs")
            row = await cursor.fetchone()
        assert row["n"] == 1  # only flask's pair made it
