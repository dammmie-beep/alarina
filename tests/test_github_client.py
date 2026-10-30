"""Unit tests for GitHubClient using respx to mock httpx.

No live network calls are made. The fake token is never a real credential.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from alarina.github.client import GitHubClient, GitHubGraphQLError

_TOKEN = "ghp_fake_token_for_tests"
_GQL_URL = "https://api.github.com/graphql"
_REST_BASE = "https://api.github.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gql_response(data: dict[str, Any], remaining: int = 4999) -> httpx.Response:
    """Wrap a GraphQL data payload in the standard envelope."""
    return httpx.Response(
        200,
        headers={"X-RateLimit-Remaining": str(remaining), "X-RateLimit-Reset": "9999999999"},
        json={"data": {"rateLimit": {"remaining": remaining}, **data}},
    )


def _issues_page(
    nodes: list[dict[str, Any]],
    *,
    has_next: bool = False,
    end_cursor: str | None = None,
) -> dict[str, Any]:
    return {
        "repository": {
            "issues": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                "nodes": nodes,
            }
        }
    }


def _minimal_issue(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "body text",
        "createdAt": "2024-01-01T00:00:00Z",
        "closedAt": "2024-01-10T00:00:00Z",
        "labels": {"nodes": [{"name": "bug"}]},
        "timelineItems": {"nodes": []},
        "comments": {"nodes": []},
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestBearerAuth:
    async def test_authorization_header_injected(self) -> None:
        with respx.mock:
            route = respx.post(_GQL_URL).mock(return_value=_gql_response({}))
            async with GitHubClient(_TOKEN) as client:
                await client.graphql("query { viewer { login } }", {})
        assert route.called
        assert route.calls[0].request.headers["Authorization"] == f"Bearer {_TOKEN}"

    async def test_token_absent_from_request_url_and_body(self) -> None:
        with respx.mock:
            route = respx.post(_GQL_URL).mock(return_value=_gql_response({}))
            async with GitHubClient(_TOKEN) as client:
                await client.graphql("query { viewer { login } }", {})
        request = route.calls[0].request
        assert _TOKEN not in str(request.url)
        assert _TOKEN not in request.content.decode()


# ---------------------------------------------------------------------------
# graphql()
# ---------------------------------------------------------------------------


class TestGraphQL:
    async def test_returns_data_field(self) -> None:
        with respx.mock:
            respx.post(_GQL_URL).mock(return_value=_gql_response({"viewer": {"login": "alice"}}))
            async with GitHubClient(_TOKEN) as client:
                data = await client.graphql("query { viewer { login } }", {})
        assert data["viewer"]["login"] == "alice"

    async def test_raises_graphql_error_on_errors_field(self) -> None:
        with respx.mock:
            respx.post(_GQL_URL).mock(
                return_value=httpx.Response(200, json={"errors": [{"message": "Not found"}]})
            )
            async with GitHubClient(_TOKEN) as client:
                with pytest.raises(GitHubGraphQLError, match="Not found"):
                    await client.graphql("query { viewer { login } }", {})

    async def test_rate_limit_state_updated_from_graphql_field(self) -> None:
        with respx.mock:
            respx.post(_GQL_URL).mock(return_value=_gql_response({}, remaining=77))
            async with GitHubClient(_TOKEN) as client:
                await client.graphql("query { viewer { login } }", {})
                assert client._state.remaining == 77

    async def test_rate_limit_state_updated_from_rest_headers(self) -> None:
        with respx.mock:
            respx.get(f"{_REST_BASE}/repos/psf/black/contributors").mock(
                return_value=httpx.Response(
                    200,
                    headers={"X-RateLimit-Remaining": "333", "X-RateLimit-Reset": "9999999999"},
                    json=[{"login": "alice", "contributions": 10}],
                )
            )
            async with GitHubClient(_TOKEN) as client:
                await client.get("/repos/psf/black/contributors")
                assert client._state.remaining == 333


# ---------------------------------------------------------------------------
# iter_closed_issues()
# ---------------------------------------------------------------------------


class TestIterClosedIssues:
    async def test_single_page_yields_one_batch(self) -> None:
        with respx.mock:
            respx.post(_GQL_URL).mock(
                return_value=_gql_response(_issues_page([_minimal_issue(1)]))
            )
            async with GitHubClient(_TOKEN) as client:
                pages = [p async for p in client.iter_closed_issues("psf", "black")]
        assert len(pages) == 1
        assert pages[0][0]["number"] == 1

    async def test_paginates_until_no_next_page(self) -> None:
        with respx.mock:
            respx.post(_GQL_URL).mock(
                side_effect=[
                    _gql_response(_issues_page([_minimal_issue(1)], has_next=True, end_cursor="c1")),
                    _gql_response(_issues_page([_minimal_issue(2)], has_next=False)),
                ]
            )
            async with GitHubClient(_TOKEN) as client:
                pages = [p async for p in client.iter_closed_issues("psf", "black")]
        assert len(pages) == 2
        assert pages[0][0]["number"] == 1
        assert pages[1][0]["number"] == 2

    async def test_max_pages_stops_early(self) -> None:
        with respx.mock:
            # Server would serve more pages, but max_pages=1 stops after the first
            respx.post(_GQL_URL).mock(
                return_value=_gql_response(
                    _issues_page([_minimal_issue(1)], has_next=True, end_cursor="c1")
                )
            )
            async with GitHubClient(_TOKEN) as client:
                pages = [
                    p async for p in client.iter_closed_issues("psf", "black", max_pages=1)
                ]
        assert len(pages) == 1

    async def test_cursor_sent_on_second_request(self) -> None:
        captured_variables: list[dict[str, Any]] = []

        async def capture(request: httpx.Request, **_: Any) -> httpx.Response:
            import json

            body = json.loads(request.content)
            captured_variables.append(body["variables"])
            page = len(captured_variables)
            has_next = page < 2
            return _gql_response(
                _issues_page(
                    [_minimal_issue(page)],
                    has_next=has_next,
                    end_cursor="cursor_abc" if has_next else None,
                )
            )

        with respx.mock:
            respx.post(_GQL_URL).mock(side_effect=capture)
            async with GitHubClient(_TOKEN) as client:
                _ = [p async for p in client.iter_closed_issues("psf", "black")]

        assert captured_variables[0]["cursor"] is None
        assert captured_variables[1]["cursor"] == "cursor_abc"


# ---------------------------------------------------------------------------
# get_contributors()
# ---------------------------------------------------------------------------


class TestGetContributors:
    async def test_returns_full_list_single_page(self) -> None:
        contributors = [{"login": "alice", "contributions": 42}]
        with respx.mock:
            respx.get(f"{_REST_BASE}/repos/psf/black/contributors").mock(
                return_value=httpx.Response(200, json=contributors)
            )
            async with GitHubClient(_TOKEN) as client:
                result = await client.get_contributors("psf", "black")
        assert result == contributors

    async def test_paginates_until_short_page(self) -> None:
        page1 = [{"login": f"user{i}", "contributions": i} for i in range(100)]
        page2 = [{"login": "last_user", "contributions": 1}]
        with respx.mock:
            respx.get(f"{_REST_BASE}/repos/psf/black/contributors").mock(
                side_effect=[
                    httpx.Response(200, json=page1),
                    httpx.Response(200, json=page2),
                ]
            )
            async with GitHubClient(_TOKEN) as client:
                result = await client.get_contributors("psf", "black")
        assert len(result) == 101
        assert result[-1]["login"] == "last_user"

    async def test_returns_empty_list_for_repo_with_no_contributors(self) -> None:
        with respx.mock:
            respx.get(f"{_REST_BASE}/repos/psf/black/contributors").mock(
                return_value=httpx.Response(200, json=[])
            )
            async with GitHubClient(_TOKEN) as client:
                result = await client.get_contributors("psf", "black")
        assert result == []
