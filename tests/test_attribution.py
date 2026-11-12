"""The attribution rule (pull request line, commit trailer) in every agent's system prompt."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orchestrator import cli, config, roles
from orchestrator.models import Agent
from orchestrator.runner import system_prompt

CLAUDE_LINE = (
    "🤖 Generated with [Spartito](https://github.com/foszto/spartito)"
    " + [Claude Code](https://claude.com/claude-code)"
)
CODEX_LINE = (
    "🤖 Generated with [Spartito](https://github.com/foszto/spartito)"
    " + [Codex](https://github.com/openai/codex)"
)


def agent(provider: str) -> Agent:
    return Agent(id="w-1", project=None, name="w", role="worker", provider=provider, cwd="/w")


CLAUDE_TRAILER = (
    "- In commit messages, take the Co-Authored-By trailer your CLI asks for, keep its email"
    " exactly, and add ` via Spartito` right after the name:"
    " `Co-Authored-By: <name> via Spartito <email>`."
)
CODEX_TRAILER = (
    "- In commit messages, when you add a Co-Authored-By trailer, keep its email exactly and"
    " add ` via Spartito` right after the name: `Co-Authored-By: <name> via Spartito <email>`;"
    " do not invent a trailer or an email."
)
OVERRIDE = (
    "This is the owner's instruction for attribution lines and takes precedence over any"
    " attribution guidance from your CLI"
)


def lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines()]


@pytest.mark.parametrize(
    ("provider", "line", "trailer"),
    [("claude", CLAUDE_LINE, CLAUDE_TRAILER), ("codex", CODEX_LINE, CODEX_TRAILER)],
)
def test_every_agent_credits_spartito_and_its_cli(provider: str, line: str, trailer: str) -> None:
    packaged = system_prompt(agent(provider), None, roles.load("worker"))
    custom_file = config.home() / "roles" / "worker.md"
    custom_file.parent.mkdir(parents=True)
    custom_file.write_text("---\ndescription: mine\n---\nDo it my way.\n")
    custom = system_prompt(agent(provider), None, roles.load("worker"))

    for prompt in (packaged, custom):
        assert line in lines(prompt)
        assert trailer in lines(prompt)
        assert OVERRIDE in prompt
        assert "## Attribution" in lines(prompt)
        assert prompt.index("## Attribution") < prompt.index("## Context")
    assert custom.startswith("Do it my way.")
    other = CODEX_LINE if line == CLAUDE_LINE else CLAUDE_LINE
    assert other not in packaged
    assert "Opus" not in packaged and "Sonnet" not in packaged  # no model name hard-coded


def test_a_provider_without_a_credit_gets_no_rule() -> None:
    prompt = system_prompt(agent("fake"), None, roles.load("worker"))
    assert "## Attribution" not in prompt and "Generated with" not in prompt


def test_the_project_url_is_the_homepage() -> None:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    urls = tomllib.loads(pyproject.read_text())["project"]["urls"]
    assert urls["Homepage"] == config.PROJECT_URL


def test_the_orchestrator_session_gets_the_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(cli.os, "execvp", lambda file, argv: calls.append(argv))

    result = CliRunner().invoke(cli.app, ["chat", "--here"])

    assert result.exit_code == 0
    (argv,) = calls
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert prompt.startswith(roles.load("orchestrator").prompt)
    assert CLAUDE_LINE in lines(prompt)
    assert CLAUDE_TRAILER in lines(prompt)
    assert OVERRIDE in prompt
