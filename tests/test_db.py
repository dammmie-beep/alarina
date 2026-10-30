"""Tests for benchmark/db.py.

Uses a real SQLite database in a pytest tmp_path directory — no mocking.
This verifies the DDL, JSON serialisation, and duplicate-handling logic
against the actual SQLite engine.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from benchmark.db import PairRow, RepoRow, get_or_create_repo, insert_pair, open_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "test.db"


def _repo(full_name: str = "psf/black") -> RepoRow:
    return RepoRow(full_name=full_name, language="Python", stars=39_000, collected_at="2024-01-01T00:00:00Z")


def _pair(repo_id: int, issue_number: int = 42, pr_number: int = 99) -> PairRow:
    return PairRow(
        repo_id=repo_id,
        issue_number=issue_number,
        issue_title="Fix the formatting",
        issue_body="Steps to reproduce: ...",
        issue_labels=["bug", "good first issue"],
        issue_created_at="2024-01-01T00:00:00Z",
        issue_closed_at="2024-01-10T00:00:00Z",
        pr_number=pr_number,
        pr_merged_at="2024-01-10T00:00:00Z",
        lines_added=10,
        lines_deleted=5,
        files_changed_count=2,
        files_changed_paths=["src/foo.py", "tests/test_foo.py"],
        review_rounds=2,
        first_contributor_comment_at="2024-01-02T00:00:00Z",
        time_to_merge_hours=192.0,
        fixer_login="alice",
        fixer_prior_contributions=15,
    )


def _minimal_pair(repo_id: int, issue_number: int = 1, pr_number: int = 2) -> PairRow:
    """All-nullable fields set to None — validates that NULL handling works."""
    return PairRow(
        repo_id=repo_id,
        issue_number=issue_number,
        issue_title="title",
        issue_body=None,
        issue_labels=[],
        issue_created_at=None,
        issue_closed_at=None,
        pr_number=pr_number,
        pr_merged_at=None,
        lines_added=None,
        lines_deleted=None,
        files_changed_count=None,
        files_changed_paths=[],
        review_rounds=None,
        first_contributor_comment_at=None,
        time_to_merge_hours=None,
        fixer_login=None,
        fixer_prior_contributions=None,
    )


# ---------------------------------------------------------------------------
# Schema / open_db
# ---------------------------------------------------------------------------


class TestOpenDb:
    async def test_creates_both_tables(self, db_path: pathlib.Path) -> None:
        async with open_db(db_path) as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = {row["name"] for row in await cursor.fetchall()}
        assert {"repos", "issue_pr_pairs"}.issubset(tables)

    async def test_schema_is_idempotent(self, db_path: pathlib.Path) -> None:
        async with open_db(db_path):
            pass
        async with open_db(db_path):
            pass  # second open must not raise

    async def test_creates_parent_directory(self, tmp_path: pathlib.Path) -> None:
        nested = tmp_path / "a" / "b" / "test.db"
        async with open_db(nested):
            pass
        assert nested.exists()


# ---------------------------------------------------------------------------
# get_or_create_repo
# ---------------------------------------------------------------------------


class TestGetOrCreateRepo:
    async def test_returns_positive_integer_id(self, db_path: pathlib.Path) -> None:
        async with open_db(db_path) as conn:
            repo_id = await get_or_create_repo(conn, _repo())
        assert isinstance(repo_id, int)
        assert repo_id > 0

    async def test_same_id_returned_on_repeated_call(self, db_path: pathlib.Path) -> None:
        async with open_db(db_path) as conn:
            id1 = await get_or_create_repo(conn, _repo())
            id2 = await get_or_create_repo(conn, _repo())
        assert id1 == id2

    async def test_different_repos_get_different_ids(self, db_path: pathlib.Path) -> None:
        async with open_db(db_path) as conn:
            id1 = await get_or_create_repo(conn, _repo("psf/black"))
            id2 = await get_or_create_repo(conn, _repo("pallets/flask"))
        assert id1 != id2


# ---------------------------------------------------------------------------
# insert_pair
# ---------------------------------------------------------------------------


class TestInsertPair:
    async def test_inserts_one_row(self, db_path: pathlib.Path) -> None:
        async with open_db(db_path) as conn:
            repo_id = await get_or_create_repo(conn, _repo())
            await insert_pair(conn, _pair(repo_id))
            await conn.commit()
            cursor = await conn.execute("SELECT COUNT(*) as n FROM issue_pr_pairs")
            row = await cursor.fetchone()
        assert row["n"] == 1

    async def test_duplicate_is_silently_ignored(self, db_path: pathlib.Path) -> None:
        async with open_db(db_path) as conn:
            repo_id = await get_or_create_repo(conn, _repo())
            pair = _pair(repo_id)
            await insert_pair(conn, pair)
            await insert_pair(conn, pair)  # second call must not raise
            await conn.commit()
            cursor = await conn.execute("SELECT COUNT(*) as n FROM issue_pr_pairs")
            row = await cursor.fetchone()
        assert row["n"] == 1

    async def test_labels_round_trip_through_json(self, db_path: pathlib.Path) -> None:
        async with open_db(db_path) as conn:
            repo_id = await get_or_create_repo(conn, _repo())
            await insert_pair(conn, _pair(repo_id))
            await conn.commit()
            cursor = await conn.execute("SELECT issue_labels FROM issue_pr_pairs")
            row = await cursor.fetchone()
        assert json.loads(row["issue_labels"]) == ["bug", "good first issue"]

    async def test_file_paths_round_trip_through_json(self, db_path: pathlib.Path) -> None:
        async with open_db(db_path) as conn:
            repo_id = await get_or_create_repo(conn, _repo())
            await insert_pair(conn, _pair(repo_id))
            await conn.commit()
            cursor = await conn.execute("SELECT files_changed_paths FROM issue_pr_pairs")
            row = await cursor.fetchone()
        assert json.loads(row["files_changed_paths"]) == ["src/foo.py", "tests/test_foo.py"]

    async def test_nullable_fields_stored_as_null(self, db_path: pathlib.Path) -> None:
        async with open_db(db_path) as conn:
            repo_id = await get_or_create_repo(conn, _repo())
            await insert_pair(conn, _minimal_pair(repo_id))
            await conn.commit()
            cursor = await conn.execute(
                "SELECT fixer_login, time_to_merge_hours, lines_added FROM issue_pr_pairs"
            )
            row = await cursor.fetchone()
        assert row["fixer_login"] is None
        assert row["time_to_merge_hours"] is None
        assert row["lines_added"] is None

    async def test_multiple_pairs_for_same_repo(self, db_path: pathlib.Path) -> None:
        async with open_db(db_path) as conn:
            repo_id = await get_or_create_repo(conn, _repo())
            await insert_pair(conn, _pair(repo_id, issue_number=1, pr_number=10))
            await insert_pair(conn, _pair(repo_id, issue_number=2, pr_number=20))
            await conn.commit()
            cursor = await conn.execute("SELECT COUNT(*) as n FROM issue_pr_pairs")
            row = await cursor.fetchone()
        assert row["n"] == 2
