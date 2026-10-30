"""Tests for src/alarina/llm.py.

The instructor client is mocked so no real LLM call is made.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alarina.llm import DirectDifficultyRating, rate_difficulty_direct


def _mock_instructor_client(difficulty: int) -> MagicMock:
    """Return a mock instructor client that resolves to the given difficulty."""
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=DirectDifficultyRating(difficulty=difficulty)
    )
    return mock_client


class TestDirectDifficultyRating:
    def test_accepts_boundary_values(self) -> None:
        assert DirectDifficultyRating(difficulty=1).difficulty == 1
        assert DirectDifficultyRating(difficulty=10).difficulty == 10

    def test_rejects_below_minimum(self) -> None:
        with pytest.raises(Exception):
            DirectDifficultyRating(difficulty=0)

    def test_rejects_above_maximum(self) -> None:
        with pytest.raises(Exception):
            DirectDifficultyRating(difficulty=11)


class TestRateDifficultyDirect:
    async def test_returns_integer_difficulty(self) -> None:
        with patch("alarina.llm.instructor.from_litellm", return_value=_mock_instructor_client(5)):
            result = await rate_difficulty_direct("Fix typo", None, [])
        assert result == 5

    async def test_issue_text_wrapped_in_tags(self) -> None:
        captured_messages: list[list[dict]] = []
        mock_client = MagicMock()

        async def capture_create(**kwargs):  # type: ignore[no-untyped-def]
            captured_messages.append(kwargs["messages"])
            return DirectDifficultyRating(difficulty=3)

        mock_client.chat.completions.create = capture_create
        with patch("alarina.llm.instructor.from_litellm", return_value=mock_client):
            await rate_difficulty_direct("A bug", "body text", ["bug"])

        user_content = captured_messages[0][1]["content"]
        assert "<issue_content>" in user_content
        assert "</issue_content>" in user_content
        assert "body text" in user_content

    async def test_labels_appear_outside_issue_content_tags(self) -> None:
        captured_messages: list[list[dict]] = []
        mock_client = MagicMock()

        async def capture_create(**kwargs):  # type: ignore[no-untyped-def]
            captured_messages.append(kwargs["messages"])
            return DirectDifficultyRating(difficulty=2)

        mock_client.chat.completions.create = capture_create
        with patch("alarina.llm.instructor.from_litellm", return_value=mock_client):
            await rate_difficulty_direct("A bug", None, ["good first issue"])

        user_content = captured_messages[0][1]["content"]
        # Label must appear before the opening tag (not inside untrusted block)
        tag_pos = user_content.index("<issue_content>")
        label_pos = user_content.index("good first issue")
        assert label_pos < tag_pos

    async def test_uses_specified_model(self) -> None:
        mock_client = _mock_instructor_client(7)
        with patch("alarina.llm.instructor.from_litellm", return_value=mock_client):
            await rate_difficulty_direct("title", None, [], model="groq/mixtral-8x7b-32768")

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"] == "groq/mixtral-8x7b-32768"

    async def test_max_retries_set_to_two(self) -> None:
        mock_client = _mock_instructor_client(4)
        with patch("alarina.llm.instructor.from_litellm", return_value=mock_client):
            await rate_difficulty_direct("title", None, [])

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert call_kwargs["max_retries"] == 2

    async def test_none_body_does_not_raise(self) -> None:
        with patch("alarina.llm.instructor.from_litellm", return_value=_mock_instructor_client(5)):
            result = await rate_difficulty_direct("title", None, [])
        assert 1 <= result <= 10

    async def test_empty_labels_list_shows_none_in_prompt(self) -> None:
        captured: list[list[dict]] = []
        mock_client = MagicMock()

        async def capture(**kwargs):  # type: ignore[no-untyped-def]
            captured.append(kwargs["messages"])
            return DirectDifficultyRating(difficulty=5)

        mock_client.chat.completions.create = capture
        with patch("alarina.llm.instructor.from_litellm", return_value=mock_client):
            await rate_difficulty_direct("title", None, [])

        assert "none" in captured[0][1]["content"].lower()
