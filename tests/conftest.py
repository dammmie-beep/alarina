import pytest


@pytest.fixture
def github_token() -> str:
    return "ghp_test_token_for_cassettes"
