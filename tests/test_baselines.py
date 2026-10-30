"""Tests for benchmark/baselines.py.

All LLM calls are mocked. DB tests use a real temp SQLite database.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from benchmark.baselines import (
    _DEFAULT_SCORE,
    _labels_score,
    _load_pairs,
    _rank_with_ties,
    _run_labels_baseline,
    _run_llm_baseline,
    _spearman_r,
    evaluate,
)
from benchmark.db import PairRow, RepoRow, get_or_create_repo, insert_pair, open_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pair(
    repo_id: int,
    *,
    issue_number: int = 1,
    pr_number: int = 2,
    labels: list[str] | None = None,
    lines_added: int = 10,
    lines_deleted: int = 5,
    review_rounds: int = 1,
    time_to_merge_hours: float = 24.0,
) -> PairRow:
    return PairRow(
        repo_id=repo_id,
        issue_number=issue_number,
        issue_title="Some issue",
        issue_body="body",
        issue_labels=labels or ["bug"],
        issue_created_at="2024-01-01T00:00:00Z",
        issue_closed_at="2024-01-10T00:00:00Z",
        pr_number=pr_number,
        pr_merged_at="2024-01-10T00:00:00Z",
        lines_added=lines_added,
        lines_deleted=lines_deleted,
        files_changed_count=1,
        files_changed_paths=["src/a.py"],
        review_rounds=review_rounds,
        first_contributor_comment_at="2024-01-02T00:00:00Z",
        time_to_merge_hours=time_to_merge_hours,
        fixer_login="alice",
        fixer_prior_contributions=5,
    )


async def _seed_db(
    db_path: pathlib.Path,
    pairs: list[PairRow],
) -> None:
    async with open_db(db_path) as db:
        repo_id = await get_or_create_repo(
            db, RepoRow("psf/black", "Python", 39_000, "2024-01-01T00:00:00Z")
        )
        for p in pairs:
            fixed = PairRow(**{**p.__dict__, "repo_id": repo_id})
            await insert_pair(db, fixed)
        await db.commit()


# ---------------------------------------------------------------------------
# _labels_score
# ---------------------------------------------------------------------------


class TestLabelsScore:
    def test_good_first_issue_returns_low_score(self) -> None:
        assert _labels_score(["good first issue"]) == 2.0

    def test_security_returns_high_score(self) -> None:
        assert _labels_score(["security"]) == 8.0

    def test_empty_labels_returns_default(self) -> None:
        assert _labels_score([]) == _DEFAULT_SCORE

    def test_unknown_labels_only_returns_default(self) -> None:
        assert _labels_score(["wontfix", "duplicate"]) == _DEFAULT_SCORE

    def test_max_of_multiple_matched_labels(self) -> None:
        # "good first issue" → 2, "security" → 8; max is 8
        assert _labels_score(["good first issue", "security"]) == 8.0

    def test_case_insensitive(self) -> None:
        assert _labels_score(["Good First Issue"]) == 2.0
        assert _labels_score(["BUG"]) == 5.0

    def test_mixed_known_and_unknown_labels(self) -> None:
        # "wontfix" is unknown (ignored); "enhancement" → 6
        assert _labels_score(["wontfix", "enhancement"]) == 6.0


# ---------------------------------------------------------------------------
# _rank_with_ties / _spearman_r
# ---------------------------------------------------------------------------


class TestRankWithTies:
    def test_no_ties(self) -> None:
        assert _rank_with_ties([3.0, 1.0, 2.0]) == [3.0, 1.0, 2.0]

    def test_all_tied(self) -> None:
        result = _rank_with_ties([5.0, 5.0, 5.0])
        assert result == [2.0, 2.0, 2.0]

    def test_two_tied_at_top(self) -> None:
        # vals [1, 2, 2] → ranks [1, 2.5, 2.5]
        assert _rank_with_ties([1.0, 2.0, 2.0]) == [1.0, 2.5, 2.5]


class TestSpearmanR:
    def test_perfect_positive_correlation(self) -> None:
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert _spearman_r(x, x) == pytest.approx(1.0)

    def test_perfect_negative_correlation(self) -> None:
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [5.0, 4.0, 3.0, 2.0, 1.0]
        assert _spearman_r(x, y) == pytest.approx(-1.0)

    def test_no_correlation(self) -> None:
        # Alternating pattern → r close to 0
        x = [1.0, 2.0, 3.0, 4.0]
        y = [2.0, 1.0, 4.0, 3.0]
        r = _spearman_r(x, y)
        assert -0.5 < r < 0.5

    def test_returns_zero_for_single_element(self) -> None:
        assert _spearman_r([1.0], [1.0]) == 0.0

    def test_handles_constant_y(self) -> None:
        # Constant sequence has zero variance → r = 0
        x = [1.0, 2.0, 3.0]
        y = [5.0, 5.0, 5.0]
        assert _spearman_r(x, y) == 0.0


# ---------------------------------------------------------------------------
# _load_pairs
# ---------------------------------------------------------------------------


class TestLoadPairs:
    async def test_loads_eligible_pairs(self, tmp_path: pathlib.Path) -> None:
        db_path = tmp_path / "test.db"
        await _seed_db(db_path, [_pair(0)])
        async with open_db(db_path) as db:
            pairs = await _load_pairs(db)
        assert len(pairs) == 1

    async def test_excludes_pairs_with_null_lines(self, tmp_path: pathlib.Path) -> None:
        db_path = tmp_path / "test.db"
        incomplete = PairRow(
            repo_id=0, issue_number=1, issue_title="t", issue_body=None,
            issue_labels=[], issue_created_at=None, issue_closed_at=None,
            pr_number=2, pr_merged_at=None, lines_added=None, lines_deleted=None,
            files_changed_count=None, files_changed_paths=[], review_rounds=None,
            first_contributor_comment_at=None, time_to_merge_hours=12.0,
            fixer_login=None, fixer_prior_contributions=None,
        )
        await _seed_db(db_path, [incomplete])
        async with open_db(db_path) as db:
            pairs = await _load_pairs(db)
        assert pairs == []

    async def test_excludes_zero_time_to_merge(self, tmp_path: pathlib.Path) -> None:
        db_path = tmp_path / "test.db"
        await _seed_db(db_path, [_pair(0, time_to_merge_hours=0.0)])
        async with open_db(db_path) as db:
            pairs = await _load_pairs(db)
        assert pairs == []

    async def test_labels_deserialized_from_json(self, tmp_path: pathlib.Path) -> None:
        db_path = tmp_path / "test.db"
        await _seed_db(db_path, [_pair(0, labels=["bug", "good first issue"])])
        async with open_db(db_path) as db:
            pairs = await _load_pairs(db)
        assert pairs[0]["issue_labels"] == ["bug", "good first issue"]


# ---------------------------------------------------------------------------
# _run_labels_baseline
# ---------------------------------------------------------------------------


class TestRunLabelsBaseline:
    def test_scores_list_length_matches_pairs(self) -> None:
        pairs = [
            {"issue_labels": ["bug"]},
            {"issue_labels": ["good first issue"]},
            {"issue_labels": []},
        ]
        scores = _run_labels_baseline(pairs)
        assert len(scores) == 3
        assert scores[0] == 5.0
        assert scores[1] == 2.0
        assert scores[2] == _DEFAULT_SCORE


# ---------------------------------------------------------------------------
# _run_llm_baseline
# ---------------------------------------------------------------------------


class TestRunLlmBaseline:
    async def test_returns_score_for_each_pair(self) -> None:
        pairs = [
            {"issue_title": "Fix typo", "issue_body": None, "issue_labels": []},
            {"issue_title": "Add feature", "issue_body": "...", "issue_labels": ["enhancement"]},
        ]
        with patch(
            "benchmark.baselines.rate_difficulty_direct",
            new_callable=AsyncMock,
            return_value=5,
        ):
            scores = await _run_llm_baseline(pairs, model="groq/llama", limit=None)
        assert len(scores) == 2
        assert all(s == 5.0 for s in scores)

    async def test_limit_restricts_pairs_evaluated(self) -> None:
        pairs = [
            {"issue_title": f"Issue {i}", "issue_body": None, "issue_labels": []}
            for i in range(10)
        ]
        calls: list[int] = []

        async def mock_rate(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(1)
            return 5

        with patch("benchmark.baselines.rate_difficulty_direct", side_effect=mock_rate):
            await _run_llm_baseline(pairs, model="groq/llama", limit=3)

        assert len(calls) == 3

    async def test_failed_pair_is_excluded_from_scores(self) -> None:
        pairs = [
            {"issue_title": "good", "issue_body": None, "issue_labels": []},
            {"issue_title": "bad", "issue_body": None, "issue_labels": []},
        ]
        call_count = 0

        async def mock_rate(title, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            if title == "bad":
                raise RuntimeError("API error")
            return 5

        with patch("benchmark.baselines.rate_difficulty_direct", side_effect=mock_rate):
            scores = await _run_llm_baseline(pairs, model="groq/llama", limit=None)

        assert len(scores) == 1
        assert scores[0] == 5.0


# ---------------------------------------------------------------------------
# evaluate() integration
# ---------------------------------------------------------------------------


class TestEvaluate:
    async def test_prints_results_for_labels_only(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "test.db"
        await _seed_db(
            db_path,
            [
                _pair(0, issue_number=1, pr_number=10, labels=["good first issue"], lines_added=5),
                _pair(0, issue_number=2, pr_number=20, labels=["security"], lines_added=500),
            ],
        )
        await evaluate(run_llm=False, db_path=db_path)
        out = capsys.readouterr().out
        assert "Labels-only" in out
        assert "r=" in out

    async def test_empty_db_prints_message(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "test.db"
        await evaluate(run_llm=False, db_path=db_path)
        out = capsys.readouterr().out
        assert "build_dataset" in out.lower() or "no eligible" in out.lower()
