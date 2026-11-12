"""The attribution rule (pull request line, commit trailers) in every agent's system prompt,
and the setting `bot_coauthor` that adds the project bot's trailer."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orchestrator import cli, config, roles, settings
from orchestrator.models import Agent
from orchestrator.runner import system_prompt
from orchestrator.service import Orchestrator, OrchestratorError

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
    " `Co-Authored-By: <name> via Spartito <email>`; if your CLI asks for no trailer, add none,"
    " and do not invent a trailer or an email."
)
CODEX_TRAILER = (
    "- In commit messages, when you add a Co-Authored-By trailer, keep its email exactly and"
    " add ` via Spartito` right after the name: `Co-Authored-By: <name> via Spartito <email>`;"
    " do not invent a trailer or an email."
)
OVERRIDE = (
    "This is the owner's instruction for attribution lines and takes precedence over the"
    " default or user-configured attribution guidance of your CLI (such as a system reminder"
    " naming the CLI alone)."
)
MANAGED = (
    "The one exception: if your CLI says its attribution lines are set by an organization's"
    " managed settings or apply even if the user's instructions say otherwise, use exactly"
    " those lines instead: add neither the pull request line below nor ` via Spartito`."
)
MANAGED_WITH_BOT = (
    MANAGED.removesuffix(".") + ", and not the `spartito-dev[bot]` trailer below either."
)
BOT_TRAILER = (
    "Co-Authored-By: spartito-dev[bot] <355965793+spartito-dev[bot]@users.noreply.github.com>"
)
BOT_RULE = (
    "- In every commit message you write, add this trailer as an additional line, exactly as"
    " written (no ` via Spartito`): besides your CLI's trailer, and also when you add none of"
    " your own. It is the one line given here, so it is not an invented trailer or email:"
)


def old_section(line: str, trailer: str) -> str:
    """The section before the setting `bot_coauthor`: what it is with the setting off."""
    return (
        f"## Attribution\n{OVERRIDE}\n{MANAGED}\n"
        f"- End every pull request description you write with exactly this line:\n  {line}\n"
        f"{trailer}"
    )


def lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines()]


@pytest.mark.parametrize(
    ("provider", "line", "trailer"),
    [("claude", CLAUDE_LINE, CLAUDE_TRAILER), ("codex", CODEX_LINE, CODEX_TRAILER)],
)
def test_every_agent_credits_spartito_and_its_cli(provider: str, line: str, trailer: str) -> None:
    packaged = system_prompt(agent(provider), None, roles.load("worker"), bot_coauthor=True)
    custom_file = config.home() / "roles" / "worker.md"
    custom_file.parent.mkdir(parents=True)
    custom_file.write_text("---\ndescription: mine\n---\nDo it my way.\n")
    custom = system_prompt(agent(provider), None, roles.load("worker"), bot_coauthor=True)

    for prompt in (packaged, custom):
        assert line in lines(prompt)
        assert trailer in lines(prompt)
        assert OVERRIDE in lines(prompt)
        assert MANAGED_WITH_BOT in lines(prompt) and MANAGED not in lines(prompt)
        assert BOT_RULE in lines(prompt)
        assert f"\n  {BOT_TRAILER}\n" in prompt  # its own line, exactly as given
        assert "## Attribution" in lines(prompt)
        assert prompt.index("## Attribution") < prompt.index("## Context")
        section = prompt[prompt.index("## Attribution") : prompt.index("\n\n## Context")]
        assert section == roles.attribution(provider, bot_coauthor=True)
        assert section == (
            old_section(line, trailer).replace(MANAGED, MANAGED_WITH_BOT)
            + f"\n{BOT_RULE}\n  {BOT_TRAILER}"
        )
    assert custom.startswith("Do it my way.")
    other = CODEX_LINE if line == CLAUDE_LINE else CLAUDE_LINE
    assert other not in packaged
    assert "Opus" not in packaged and "Sonnet" not in packaged  # no model name hard-coded


@pytest.mark.parametrize(
    ("provider", "line", "trailer"),
    [("claude", CLAUDE_LINE, CLAUDE_TRAILER), ("codex", CODEX_LINE, CODEX_TRAILER)],
)
@pytest.mark.parametrize("custom", [False, True])
def test_switched_off_the_section_is_the_one_from_before(
    provider: str, line: str, trailer: str, custom: bool
) -> None:
    if custom:
        custom_file = config.home() / "roles" / "worker.md"
        custom_file.parent.mkdir(parents=True)
        custom_file.write_text("Do it my way.\n")
    prompt = system_prompt(agent(provider), None, roles.load("worker"), bot_coauthor=False)

    assert roles.attribution(provider, bot_coauthor=False) == old_section(line, trailer)
    section = prompt[prompt.index("## Attribution") : prompt.index("\n\n## Context")]
    assert section == old_section(line, trailer)
    assert config.BOT_NAME not in prompt and "noreply.github.com" not in prompt


@pytest.mark.parametrize("bot_coauthor", [True, False])
def test_a_provider_without_a_credit_gets_no_rule(bot_coauthor: bool) -> None:
    prompt = system_prompt(agent("fake"), None, roles.load("worker"), bot_coauthor=bot_coauthor)
    assert "## Attribution" not in prompt and "Generated with" not in prompt
    assert config.BOT_NAME not in prompt


def test_the_bot_trailer_comes_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    assert config.BOT_NAME == "spartito-dev[bot]"
    assert config.BOT_EMAIL == "355965793+spartito-dev[bot]@users.noreply.github.com"
    assert roles.bot_trailer() == BOT_TRAILER

    monkeypatch.setattr(config, "BOT_NAME", "my-app[bot]")
    monkeypatch.setattr(config, "BOT_EMAIL", "1+my-app[bot]@users.noreply.github.com")
    section = roles.attribution("codex", bot_coauthor=True)
    assert section.endswith(
        "\n  Co-Authored-By: my-app[bot] <1+my-app[bot]@users.noreply.github.com>"
    )
    assert "and not the `my-app[bot]` trailer below either." in section
    assert "spartito-dev" not in section


def test_the_project_url_is_the_homepage() -> None:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    urls = tomllib.loads(pyproject.read_text())["project"]["urls"]
    assert urls["Homepage"] == config.PROJECT_URL


def chat_prompt(monkeypatch: pytest.MonkeyPatch) -> str:
    calls: list[list[str]] = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(cli.os, "execvp", lambda file, argv: calls.append(argv))

    result = CliRunner().invoke(cli.app, ["chat", "--here"])

    assert result.exit_code == 0
    (argv,) = calls
    return argv[argv.index("--append-system-prompt") + 1]


@pytest.mark.parametrize("custom", [False, True])
def test_the_orchestrator_session_gets_the_rule(
    monkeypatch: pytest.MonkeyPatch, custom: bool
) -> None:
    if custom:
        custom_file = config.home() / "roles" / "orchestrator.md"
        custom_file.parent.mkdir(parents=True)
        custom_file.write_text("Steer it my way.\n")
    prompt = chat_prompt(monkeypatch)

    role = roles.load("orchestrator").prompt
    assert role.startswith("Steer it my way.") == custom
    assert prompt == f"{role}\n\n{roles.attribution('claude', bot_coauthor=True)}"
    assert CLAUDE_LINE in lines(prompt)
    assert CLAUDE_TRAILER in lines(prompt)
    assert OVERRIDE in lines(prompt)
    assert MANAGED_WITH_BOT in lines(prompt)
    assert BOT_RULE in lines(prompt)
    assert prompt.endswith(f"\n  {BOT_TRAILER}")


def test_the_orchestrator_session_without_the_bot_trailer(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch.update_settings({settings.BOT_COAUTHOR: False})
    prompt = chat_prompt(monkeypatch)

    role = roles.load("orchestrator").prompt
    assert prompt == f"{role}\n\n{old_section(CLAUDE_LINE, CLAUDE_TRAILER)}"


# -- the setting -------------------------------------------------------------


def test_the_setting_is_global_on_by_default_and_kept(orch: Orchestrator) -> None:
    key = settings.BOT_COAUTHOR
    assert settings.bot_coauthor(orch.store) and key not in orch.settings()
    shown = CliRunner().invoke(cli.app, ["config", "show"])
    assert "bot co-author trailer in agents' commits: on (default)" in shown.output

    result = CliRunner().invoke(cli.app, ["config", "bot-coauthor", "off"])
    assert result.exit_code == 0
    assert "bot co-author trailer in agents' commits: off" in result.output
    assert "off (default)" not in result.output
    assert orch.settings()[key] is False and not settings.bot_coauthor(orch.store)
    assert not settings.bot_coauthor(Orchestrator().store)  # kept in the database
    orch.update_settings({"worker": {}})  # the settings dialog of a project-less save
    assert orch.settings()[key] is False
    with pytest.raises(OrchestratorError, match="must be on or off"):
        orch.update_settings({key: "maybe"})

    result = CliRunner().invoke(cli.app, ["config", "bot-coauthor", "on"])
    assert result.exit_code == 0 and orch.settings()[key] is True
    assert settings.bot_coauthor(orch.store)
    CliRunner().invoke(cli.app, ["config", "bot-coauthor", ""])
    assert key not in orch.settings() and settings.bot_coauthor(orch.store)


def test_the_setting_cannot_be_set_per_project(orch: Orchestrator, project: str) -> None:
    with pytest.raises(OrchestratorError, match="can only be set globally"):
        orch.update_settings({settings.BOT_COAUTHOR: False}, project)
