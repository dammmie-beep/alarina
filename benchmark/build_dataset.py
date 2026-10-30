"""Collect closed issues linked to merged PRs for every benchmark repo.

Run:
    python -m benchmark.build_dataset
    python -m benchmark.build_dataset --max-pages 2   # smoke-test with 2 pages per repo
"""

from __future__ import annotations

import asyncio
import argparse
import datetime
import logging
import pathlib
from typing import Any

import aiosqlite

from alarina.config import get_github_token
from alarina.github.client import GitHubClient
from benchmark.db import DB_PATH, PairRow, RepoRow, get_or_create_repo, insert_pair, open_db
from benchmark.repos import BENCHMARK_REPOS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure helpers — no I/O, tested in test_dataset_builder.py
# ---------------------------------------------------------------------------


def _truncate(text: str | None, max_chars: int = 4000) -> str | None:
    """Keep the first and last half of text when it exceeds max_chars."""
    if not text or len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + text[-half:]


def _extract_merged_prs(issue: dict[str, Any]) -> list[dict[str, Any]]:
    """Return de-duplicated merged-PR dicts from an issue's timeline items.

    Handles both CrossReferencedEvent (source) and ClosedEvent (closer).
    Non-PR subjects and unmerged PRs are silently dropped.
    """
    seen: set[int] = set()
    prs: list[dict[str, Any]] = []
    for node in issue.get("timelineItems", {}).get("nodes", []):
        pr: dict[str, Any] = node.get("source") or node.get("closer") or {}
        if pr.get("merged") and (num := pr.get("number")) not in seen:
            seen.add(num)
            prs.append(pr)
    return prs


def _first_fixer_comment(issue: dict[str, Any], fixer_login: str) -> str | None:
    """Return the ISO timestamp of the fixer's first comment on the issue, or None."""
    for comment in issue.get("comments", {}).get("nodes", []):
        if (comment.get("author") or {}).get("login") == fixer_login:
            return comment["createdAt"]
    return None


def _time_to_merge_hours(
    first_comment_at: str | None,
    merged_at: str | None,
) -> float | None:
    if first_comment_at is None or merged_at is None:
        return None
    t0 = datetime.datetime.fromisoformat(first_comment_at.replace("Z", "+00:00"))
    t1 = datetime.datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
    hours = (t1 - t0).total_seconds() / 3600.0
    # Negative means the PR was merged before the comment — treat as missing data
    return hours if hours >= 0 else None


def _build_pair_row(
    repo_id: int,
    issue: dict[str, Any],
    pr: dict[str, Any],
    contribution_map: dict[str, int],
) -> PairRow:
    fixer_login: str | None = (pr.get("author") or {}).get("login")
    first_comment = _first_fixer_comment(issue, fixer_login) if fixer_login else None
    return PairRow(
        repo_id=repo_id,
        issue_number=issue["number"],
        issue_title=_truncate(issue.get("title"), 4000) or "",
        issue_body=_truncate(issue.get("body"), 4000),
        issue_labels=[lb["name"] for lb in issue.get("labels", {}).get("nodes", [])],
        issue_created_at=issue.get("createdAt"),
        issue_closed_at=issue.get("closedAt"),
        pr_number=pr["number"],
        pr_merged_at=pr.get("mergedAt"),
        lines_added=pr.get("additions"),
        lines_deleted=pr.get("deletions"),
        files_changed_count=pr.get("changedFiles"),
        files_changed_paths=[f["path"] for f in pr.get("files", {}).get("nodes", [])],
        review_rounds=(pr.get("reviews") or {}).get("totalCount"),
        first_contributor_comment_at=first_comment,
        time_to_merge_hours=_time_to_merge_hours(first_comment, pr.get("mergedAt")),
        fixer_login=fixer_login,
        fixer_prior_contributions=contribution_map.get(fixer_login) if fixer_login else None,
    )


# ---------------------------------------------------------------------------
# Async pipeline
# ---------------------------------------------------------------------------


async def _process_repo(
    client: GitHubClient,
    db: aiosqlite.Connection,
    full_name: str,
    owner: str,
    name: str,
    language: str,
    *,
    max_pages: int | None,
) -> int:
    info = await client.get(f"/repos/{owner}/{name}")
    repo_id = await get_or_create_repo(
        db,
        RepoRow(
            full_name=full_name,
            language=language,
            stars=info.get("stargazers_count", 0),
            collected_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        ),
    )

    contributors = await client.get_contributors(owner, name)
    contribution_map = {
        c["login"]: c["contributions"] for c in contributors if c.get("login")
    }

    pairs = 0
    async for page in client.iter_closed_issues(owner, name, max_pages=max_pages):
        for issue in page:
            for pr in _extract_merged_prs(issue):
                await insert_pair(db, _build_pair_row(repo_id, issue, pr, contribution_map))
                pairs += 1

    await db.commit()
    return pairs


async def build(
    *,
    max_pages: int | None = None,
    db_path: pathlib.Path = DB_PATH,
) -> None:
    token = get_github_token()
    async with GitHubClient(token) as client, open_db(db_path) as db:
        for repo in BENCHMARK_REPOS:
            owner, name = repo.full_name.split("/", 1)
            logger.info("Processing %s ...", repo.full_name)
            try:
                pairs = await _process_repo(
                    client, db, repo.full_name, owner, name, repo.language,
                    max_pages=max_pages,
                )
                logger.info("  %s: %d pairs committed.", repo.full_name, pairs)
            except Exception:
                logger.exception("  %s: failed — skipping.", repo.full_name)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Build Alarina benchmark dataset")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        metavar="N",
        help="Limit issue pages per repo (useful for smoke tests)",
    )
    args = parser.parse_args()
    asyncio.run(build(max_pages=args.max_pages))
