"""Phase 0 baseline evaluation.

Loads collected issue–PR pairs from the benchmark database and reports
Spearman rank correlations for two baselines against three effort proxies:

  1. Labels-only  — rule-based difficulty score from issue labels alone.
  2. LLM direct   — model asked to rate difficulty 1-10 in one call.

Run (labels only, no API key needed):
    python -m benchmark.baselines

Run (both baselines, first 200 pairs):
    GROQ_API_KEY=... python -m benchmark.baselines --llm --limit 200

Run with a different model:
    GROQ_API_KEY=... python -m benchmark.baselines --llm --model groq/mixtral-8x7b-32768
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import pathlib
from typing import Any

import aiosqlite

from alarina.llm import DEFAULT_MODEL, rate_difficulty_direct
from benchmark.db import DB_PATH, open_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Label → difficulty score table
# ---------------------------------------------------------------------------

_LABEL_SCORES: dict[str, float] = {
    # very easy
    "good first issue": 2.0,
    "good-first-issue": 2.0,
    "goodfirstissue": 2.0,
    "beginner": 2.0,
    "beginner friendly": 2.0,
    "easy": 2.0,
    "starter": 2.0,
    "trivial": 1.0,
    # documentation
    "documentation": 3.0,
    "docs": 3.0,
    "typo": 2.0,
    "spelling": 2.0,
    # help wanted
    "help wanted": 4.0,
    "help-wanted": 4.0,
    "up for grabs": 4.0,
    "up-for-grabs": 4.0,
    # bug / fix
    "bug": 5.0,
    "fix": 5.0,
    "regression": 6.0,
    # enhancement / feature
    "enhancement": 6.0,
    "feature": 6.0,
    "feature request": 6.0,
    "improvement": 5.0,
    # refactor / chore
    "refactor": 5.0,
    "cleanup": 4.0,
    "chore": 4.0,
    "technical debt": 5.0,
    # performance / security / hard
    "performance": 7.0,
    "optimization": 7.0,
    "security": 8.0,
    "vulnerability": 8.0,
    "cve": 8.0,
    "breaking change": 8.0,
    "breaking-change": 8.0,
    "architecture": 8.0,
    "design": 7.0,
}

_DEFAULT_SCORE = 5.0  # midpoint when no labels match


def _labels_score(labels: list[str]) -> float:
    """Return a difficulty estimate (1–10) derived solely from issue labels."""
    if not labels:
        return _DEFAULT_SCORE
    matched = [_LABEL_SCORES[lb.lower()] for lb in labels if lb.lower() in _LABEL_SCORES]
    return max(matched) if matched else _DEFAULT_SCORE


# ---------------------------------------------------------------------------
# Spearman rank correlation (pure Python, handles ties)
# ---------------------------------------------------------------------------


def _rank_with_ties(vals: list[float]) -> list[float]:
    n = len(vals)
    sorted_pairs = sorted(enumerate(vals), key=lambda kv: kv[1])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j < n and sorted_pairs[j][1] == sorted_pairs[i][1]:
            j += 1
        avg_rank = (i + j + 1) / 2.0
        for k in range(i, j):
            ranks[sorted_pairs[k][0]] = avg_rank
        i = j
    return ranks


def _spearman_r(x: list[float], y: list[float]) -> float:
    n = len(x)
    if n < 2:
        return 0.0
    rx = _rank_with_ties(x)
    ry = _rank_with_ties(y)
    mx = sum(rx) / n
    my = sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    var_x = sum((a - mx) ** 2 for a in rx)
    var_y = sum((b - my) ** 2 for b in ry)
    denom = (var_x * var_y) ** 0.5
    return cov / denom if denom > 0 else 0.0


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


async def _load_pairs(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    cursor = await db.execute(
        """
        SELECT
            issue_title,
            issue_body,
            issue_labels,
            lines_added,
            lines_deleted,
            files_changed_count,
            review_rounds,
            time_to_merge_hours
        FROM issue_pr_pairs
        WHERE lines_added     IS NOT NULL
          AND lines_deleted   IS NOT NULL
          AND time_to_merge_hours IS NOT NULL
          AND time_to_merge_hours > 0
        """
    )
    rows = await cursor.fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["issue_labels"] = json.loads(d["issue_labels"] or "[]")
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------


def _run_labels_baseline(pairs: list[dict[str, Any]]) -> list[float]:
    return [_labels_score(p["issue_labels"]) for p in pairs]


async def _run_llm_baseline(
    pairs: list[dict[str, Any]],
    *,
    model: str,
    limit: int | None,
    concurrency: int = 5,
) -> list[float]:
    subset = pairs[:limit] if limit is not None else pairs
    sem = asyncio.Semaphore(concurrency)
    scores: list[float | None] = [None] * len(subset)

    async def _rate(idx: int, pair: dict[str, Any]) -> None:
        async with sem:
            try:
                scores[idx] = float(
                    await rate_difficulty_direct(
                        pair["issue_title"],
                        pair["issue_body"],
                        pair["issue_labels"],
                        model=model,
                    )
                )
            except Exception:
                logger.warning("LLM rating failed for pair %d — skipping.", idx)
                scores[idx] = None

    await asyncio.gather(*(_rate(i, p) for i, p in enumerate(subset)))
    return [s for s in scores if s is not None]


# ---------------------------------------------------------------------------
# Evaluation entry point
# ---------------------------------------------------------------------------


def _effort_proxies(pairs: list[dict[str, Any]]) -> dict[str, list[float]]:
    return {
        "lines_changed": [
            float((p["lines_added"] or 0) + (p["lines_deleted"] or 0)) for p in pairs
        ],
        "review_rounds": [float(p["review_rounds"] or 0) for p in pairs],
        "time_to_merge_h": [float(p["time_to_merge_hours"]) for p in pairs],
    }


def _print_results(
    label: str,
    scores: list[float],
    proxies: dict[str, list[float]],
    n_pairs: int,
) -> None:
    n = len(scores)
    print(f"\n{label}  (n={n} / {n_pairs} pairs evaluated)")
    for proxy_name, proxy_vals in proxies.items():
        # align proxy list to evaluated scores when LLM skips some pairs
        aligned = proxy_vals[: len(scores)]
        r = _spearman_r(scores, aligned)
        print(f"  vs {proxy_name:<18} r = {r:+.3f}")


async def evaluate(
    *,
    run_llm: bool = False,
    model: str = DEFAULT_MODEL,
    limit: int | None = None,
    db_path: pathlib.Path = DB_PATH,
) -> None:
    async with open_db(db_path) as db:
        pairs = await _load_pairs(db)

    if not pairs:
        print("No eligible pairs in the database. Run build_dataset first.")
        return

    proxies = _effort_proxies(pairs)
    print(f"\nBenchmark evaluation — {len(pairs)} pairs with full effort data\n")
    print(f"{'Baseline':<20} {'vs lines_changed':>18} {'vs review_rounds':>18} {'vs time_to_merge_h':>20}")
    print("-" * 78)

    labels_scores = _run_labels_baseline(pairs)
    _print_results_row("Labels-only", labels_scores, proxies)

    if run_llm:
        logger.info("Running LLM baseline (%s) ...", model)
        llm_scores = await _run_llm_baseline(pairs, model=model, limit=limit)
        _print_results_row(f"LLM direct ({model.split('/')[-1][:14]})", llm_scores, proxies)


def _print_results_row(
    label: str,
    scores: list[float],
    proxies: dict[str, list[float]],
) -> None:
    n = len(scores)
    cols = []
    for proxy_vals in proxies.values():
        aligned = proxy_vals[:n]
        r = _spearman_r(scores, aligned)
        cols.append(f"r={r:+.3f}")
    print(f"{label:<20} {cols[0]:>18} {cols[1]:>18} {cols[2]:>20}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Evaluate Phase 0 baselines")
    parser.add_argument("--llm", action="store_true", help="Also run the LLM direct-rating baseline")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="LiteLLM model string")
    parser.add_argument("--limit", type=int, default=None, metavar="N", help="Evaluate only the first N pairs with the LLM")
    args = parser.parse_args()
    asyncio.run(evaluate(run_llm=args.llm, model=args.model, limit=args.limit))
