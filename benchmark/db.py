from __future__ import annotations

import json
import pathlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import aiosqlite

_HERE = pathlib.Path(__file__).parent
DB_PATH = _HERE / "data" / "alarina_benchmark.db"
_SCHEMA_PATH = _HERE / "schema.sql"


@dataclass
class RepoRow:
    full_name: str
    language: str
    stars: int
    collected_at: str  # ISO-8601


@dataclass
class PairRow:
    repo_id: int
    issue_number: int
    issue_title: str
    issue_body: str | None
    issue_labels: list[str]
    issue_created_at: str | None
    issue_closed_at: str | None
    pr_number: int
    pr_merged_at: str | None
    lines_added: int | None
    lines_deleted: int | None
    files_changed_count: int | None
    files_changed_paths: list[str]
    review_rounds: int | None
    first_contributor_comment_at: str | None
    time_to_merge_hours: float | None
    fixer_login: str | None
    fixer_prior_contributions: int | None


@asynccontextmanager
async def open_db(
    path: pathlib.Path = DB_PATH,
) -> AsyncGenerator[aiosqlite.Connection, None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(_SCHEMA_PATH.read_text())
        yield conn


async def get_or_create_repo(conn: aiosqlite.Connection, row: RepoRow) -> int:
    await conn.execute(
        "INSERT OR IGNORE INTO repos (full_name, language, stars, collected_at) VALUES (?, ?, ?, ?)",
        (row.full_name, row.language, row.stars, row.collected_at),
    )
    await conn.commit()
    cursor = await conn.execute("SELECT id FROM repos WHERE full_name = ?", (row.full_name,))
    result = await cursor.fetchone()
    assert result is not None
    return int(result["id"])


async def insert_pair(conn: aiosqlite.Connection, row: PairRow) -> None:
    """Insert one issue–PR pair, silently ignoring duplicates.

    Does not commit — callers should commit after processing each repo
    to batch writes and allow crash recovery at repo granularity.
    """
    await conn.execute(
        """
        INSERT OR IGNORE INTO issue_pr_pairs (
            repo_id, issue_number, issue_title, issue_body,
            issue_labels, issue_created_at, issue_closed_at,
            pr_number, pr_merged_at, lines_added, lines_deleted,
            files_changed_count, files_changed_paths, review_rounds,
            first_contributor_comment_at, time_to_merge_hours,
            fixer_login, fixer_prior_contributions
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.repo_id,
            row.issue_number,
            row.issue_title,
            row.issue_body,
            json.dumps(row.issue_labels),
            row.issue_created_at,
            row.issue_closed_at,
            row.pr_number,
            row.pr_merged_at,
            row.lines_added,
            row.lines_deleted,
            row.files_changed_count,
            json.dumps(row.files_changed_paths),
            row.review_rounds,
            row.first_contributor_comment_at,
            row.time_to_merge_hours,
            row.fixer_login,
            row.fixer_prior_contributions,
        ),
    )
