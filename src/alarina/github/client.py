from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from .rate_limit import RateLimitState, with_retry

logger = logging.getLogger(__name__)

_GRAPHQL_URL = "https://api.github.com/graphql"
_REST_BASE = "https://api.github.com"
_GRAPHQL_CONCURRENCY = 3

# Both event types needed: CrossReferencedEvent catches "Fixes #n" PRs;
# ClosedEvent catches direct closes where GitHub auto-linked the PR.
_ISSUES_QUERY = """\
query($owner: String!, $name: String!, $cursor: String) {
  rateLimit { remaining }
  repository(owner: $owner, name: $name) {
    issues(
      states: CLOSED
      first: 50
      after: $cursor
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        body
        createdAt
        closedAt
        labels(first: 10) { nodes { name } }
        timelineItems(first: 25, itemTypes: [CROSS_REFERENCED_EVENT, CLOSED_EVENT]) {
          nodes {
            ... on CrossReferencedEvent {
              source {
                ... on PullRequest {
                  number merged mergedAt additions deletions changedFiles
                  reviews(first: 1) { totalCount }
                  files(first: 100) { nodes { path } }
                  author { login }
                }
              }
            }
            ... on ClosedEvent {
              closer {
                ... on PullRequest {
                  number merged mergedAt additions deletions changedFiles
                  reviews(first: 1) { totalCount }
                  files(first: 100) { nodes { path } }
                  author { login }
                }
              }
            }
          }
        }
        comments(first: 50) {
          nodes { createdAt author { login } }
        }
      }
    }
  }
}
"""


class GitHubGraphQLError(RuntimeError):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("; ".join(e.get("message", str(e)) for e in errors))
        self.errors = errors


class _BearerAuth(httpx.Auth):
    # Custom Auth subclass so the token is injected into headers without
    # ever appearing in log output or exception messages.
    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(self, request: httpx.Request):  # type: ignore[override]
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


class GitHubClient:
    def __init__(self, token: str) -> None:
        self._auth = _BearerAuth(token)
        self._state = RateLimitState()
        self._sem = asyncio.Semaphore(_GRAPHQL_CONCURRENCY)
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> GitHubClient:
        self._http = httpx.AsyncClient(
            auth=self._auth,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        async with self._sem:
            return await with_retry(
                lambda: self._do_graphql(query, variables),
                state=self._state,
            )

    async def _do_graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        assert self._http is not None
        resp = await self._http.post(
            _GRAPHQL_URL,
            json={"query": query, "variables": variables},
        )
        self._state.update_from_headers(resp.headers)  # update before raise so wait_if_exhausted sees it
        resp.raise_for_status()
        payload: dict[str, Any] = resp.json()
        if "errors" in payload:
            raise GitHubGraphQLError(payload["errors"])
        data: dict[str, Any] = payload.get("data", {})
        if rl := data.get("rateLimit"):
            self._state.update_from_graphql(rl)
        return data

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await with_retry(
            lambda: self._do_get(path, params or {}),
            state=self._state,
        )

    async def _do_get(self, path: str, params: dict[str, Any]) -> Any:
        assert self._http is not None
        resp = await self._http.get(f"{_REST_BASE}{path}", params=params)
        self._state.update_from_headers(resp.headers)
        resp.raise_for_status()
        return resp.json()

    async def iter_closed_issues(
        self,
        owner: str,
        name: str,
        *,
        max_pages: int | None = None,
    ) -> AsyncGenerator[list[dict[str, Any]], None]:
        cursor: str | None = None
        page = 0
        while True:
            data = await self.graphql(
                _ISSUES_QUERY,
                {"owner": owner, "name": name, "cursor": cursor},
            )
            conn = data["repository"]["issues"]
            yield conn["nodes"]
            page += 1
            if not conn["pageInfo"]["hasNextPage"]:
                break
            if max_pages is not None and page >= max_pages:
                break
            cursor = conn["pageInfo"]["endCursor"]

    async def get_contributors(
        self, owner: str, name: str
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page = 1
        while True:
            batch: list[dict[str, Any]] = await self.get(
                f"/repos/{owner}/{name}/contributors",
                {"per_page": 100, "page": page, "anon": "false"},
            )
            if not batch:
                break
            result.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return result
