from dataclasses import dataclass


@dataclass(frozen=True)
class Repo:
    full_name: str
    language: str


BENCHMARK_REPOS: list[Repo] = [
    Repo("psf/black", "Python"),
    Repo("pallets/flask", "Python"),
    Repo("tiangolo/fastapi", "Python"),
    Repo("home-assistant/core", "Python"),
    Repo("astral-sh/ruff", "Rust"),
    Repo("rust-lang/rust-clippy", "Rust"),
    Repo("nickel-lang/nickel", "Nickel"),
    Repo("denoland/deno", "TypeScript"),
    Repo("microsoft/vscode", "TypeScript"),
    Repo("vitejs/vite", "TypeScript"),
    Repo("excalidraw/excalidraw", "TypeScript"),
    Repo("golang/go", "Go"),
    Repo("gin-gonic/gin", "Go"),
    Repo("rails/rails", "Ruby"),
    Repo("jekyll/jekyll", "Ruby"),
    Repo("spring-projects/spring-framework", "Java"),
    Repo("elastic/elasticsearch", "Java"),
    Repo("neovim/neovim", "C"),
    Repo("curl/curl", "C"),
    Repo("jqlang/jq", "C"),
]
