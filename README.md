# Alarina

*Alárìnà* (Yoruba: go-between, matchmaker) — an open-source Python tool that
matches contributors to GitHub issues they can realistically finish, based on
their experience and available time.

## Install

```sh
pip install alarina
# or
uv tool install alarina
```

## Quick start

```sh
alarina match --help
```

## GitHub token

Alarina needs a read-only GitHub personal access token (public repos only):

```sh
# Store in OS keyring (recommended)
keyring set alarina github_token

# Or export as an environment variable
export GITHUB_TOKEN=ghp_...
```

## Development

```sh
uv sync --extra dev
uv run pytest
uv run ruff check src
uv run mypy src
```

## Licence

MIT — see [LICENSE](LICENSE).
