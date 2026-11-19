"""The command line (config.CLI_NAME)."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from . import __version__, autostart, capabilities, config, loadout, providers, roles
from . import mcp as mcp_servers
from .core.access import Caller
from .models import Agent
from .providers import McpServer, kit, locate
from .providers.claude import loadout_flags, tool_search
from .service import (
    ORCHESTRATOR_ID,
    ConfirmAgents,
    NeedsConfirmation,
    Orchestrator,
    OrchestratorError,
    cache_hint,
    next_message,
)
from .settings import (
    ARCHIVE_AFTER,
    ARCHIVE_AFTER_DEFAULT,
    BOT_COAUTHOR,
    COMPACT_AT,
    COMPACT_AT_DEFAULT,
    DESKTOP_NOTIFICATIONS,
    KEEP_AWAKE,
    TIERS,
)
from .textfmt import fit_line as one_line

# Help texts are wrapped in the source: Markdown reflows them (Rich markup would keep every
# line break and swallow `[...]`).
MARKUP = "markdown"
# The command's name in every text that tells someone to run it. Docstrings shown by --help
# cannot be f-strings: they write `{cli}`, and name_the_command puts this in once all commands
# are registered.
CLI = config.CLI_NAME

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=MARKUP,
    help=f"{config.PROJECT_NAME}: run {providers.builtin_labels()} agents in parallel in git"
    " worktrees, steered through one orchestrator agent. Start with"
    f" `{CLI} project add`, then `{CLI} chat` or `{CLI} web`.",
)


def show_version(value: bool) -> None:
    if value:
        typer.echo(f"{CLI} {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    version: Annotated[
        bool,
        typer.Option(
            "--version", is_eager=True, callback=show_version, help="Print the version and exit."
        ),
    ] = False,
) -> None:
    # No docstring: the help of the command line stays the one given to `app`.
    pass


project_app = typer.Typer(
    no_args_is_help=True, rich_markup_mode=MARKUP, help="Manage projects (git repositories)."
)
app.add_typer(project_app, name="project")
config_app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode=MARKUP,
    help="Settings: model and effort per tier, automatic archiving and compaction, keep-awake,"
    " desktop notifications.",
)
app.add_typer(config_app, name="config")

STATUS_COLORS = {
    "running": typer.colors.CYAN,
    "waiting": typer.colors.YELLOW,
    "done": typer.colors.GREEN,
    "failed": typer.colors.RED,
    "stopped": typer.colors.MAGENTA,
    "skipped": typer.colors.MAGENTA,
    "idle": typer.colors.BLUE,
}


def orch() -> Orchestrator:
    return Orchestrator()


def fail(message: object) -> typer.Exit:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(1)


def count(n: int) -> str:
    return str(n) if n < 1000 else f"{n / 1000:.1f}k" if n < 1_000_000 else f"{n / 1e6:.1f}M"


def tokens(counts: dict | None) -> str:
    """`<prompt>/<output> tok`, the prompt counting cache reads and writes."""
    if not counts:
        return ""
    prompt = counts["in"] + counts["cache_read"] + counts["cache_write"]
    return f"{count(prompt)}/{count(counts['out'])} tok"


CONTEXT_WARN = 80  # percent of the window from which the context is shown in a warning colour
# (or, with automatic compaction on, once it is above the threshold)


def short(n: int) -> str:
    """780k, 1M, 1.5M: a size at a glance."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.0f}k"
    return f"{n / 1e6:.1f}".removesuffix(".0") + "M"


def context_text(context: dict | None) -> str:
    """`780k/1M 78%`: the conversation against the model's window; `compacted` when its
    size is known only after the next turn; empty when nothing is known."""
    if not context:
        return ""
    if context["compacted"]:
        return "compacted"
    if not context["window"]:
        return f"{short(context['tokens'])} ctx"
    return f"{short(context['tokens'])}/{short(context['window'])} {context['percent']}%"


def context_warn(context: dict | None) -> bool:
    """Above the automatic compaction threshold (when it is on), else CONTEXT_WARN."""
    if not context:
        return False
    if context.get("compact_at"):
        return (context["tokens"] or 0) > context["compact_at"] or bool(context["compact_paused"])
    return (context["percent"] or 0) >= CONTEXT_WARN


def context_styled(context: dict | None, width: int = 0) -> str:
    text = f"{context_text(context):<{width}}"
    if context_warn(context):
        red = (context["percent"] or 0) >= 95
        return typer.style(text, fg=typer.colors.RED if red else typer.colors.YELLOW)
    return text


def compact_at_text(context: dict | None) -> str:
    """How automatic compaction stands for the agent, for `{cli} status`."""
    if not context or "compact_at" not in context:
        return ""
    if not context["compact_at"]:
        return "automatic compaction off"
    if context["compact_paused"]:
        return typer.style(
            f"automatic compaction paused: the last compaction left {short(context['compact_paused'])},"
            f" above the threshold of {short(context['compact_at'])}",
            fg=typer.colors.YELLOW,
        )
    return f"automatic compaction above {short(context['compact_at'])}"


def usage_line(counts: dict | None, cost: float | None, model: str | None = None) -> str:
    parts = []
    if counts:
        parts += [
            f"in {count(counts['in'])}",
            f"cache read {count(counts['cache_read'])}",
            f"cache write {count(counts['cache_write'])}",
            f"out {count(counts['out'])}",
        ]
    if cost:
        parts.append(f"${cost:.4f}")
    if model:
        parts.append(model)
    return " · ".join(parts) or "-"


def setup(view: dict, agent: Agent | None) -> str:
    """Effective `model/effort`; `*` marks a value set explicitly on the agent."""
    model = (view["model"] or "default") + ("*" if agent and agent.model else "")
    effort = view["effort"] and view["effort"] + ("*" if agent and agent.effort else "")
    return f"{model}/{effort}" if effort else model


def idle_with_children(view: dict) -> bool:
    """No own turn queued or running, the last one went fine (or there was none), and
    descendants still work. A failed or stopped last turn keeps its status."""
    return view["status"] in ("done", "new") and view["children_running"] > 0


def queued_after(view: dict) -> int:
    """Turns queued behind the running (or first waiting) one; `queued_runs` counts both."""
    return max(view["queued_runs"] - 1, 0)


def waiting_turns(view: dict) -> int:
    """Turns waiting to start (`queued_runs` also counts the running one)."""
    return view["queued_runs"] - (view["status"] == "running")


def shown_name(agent_id: str) -> str:
    """The orchestrator agent by its display name, the id still there for commands."""
    if agent_id == ORCHESTRATOR_ID:
        return f"{config.ORCHESTRATOR_NAME} ({ORCHESTRATOR_ID})"
    return agent_id


def sender_label(run: dict) -> str:
    """Who queued a run: an agent id, `user`, or `system` for a notification."""
    return run["sender"] or ("system" if run["kind"] == "notification" else "user")


def state_label(view: dict) -> tuple[str, str | None]:
    """Short state for listings and its colour: `running`, `waiting`, `idle` or the last
    run's status; `·3↓`: 3 descendants work (`idle·3↓`: only they do); `+N`: N more turns
    queued behind."""
    status = "idle" if idle_with_children(view) else view["status"]
    text = status
    if view["children_running"]:
        text += f"·{view['children_running']}↓"
    if waiting := queued_after(view):
        text += f" +{waiting}"
    return text, STATUS_COLORS.get(status)


# Set in the environment of `chat`'s Claude Code, so the commands its model runs through Bash
# (pre-approved: `Bash(spartito *)`, and without ORCH_AGENT_ID, so they count as the owner's)
# can be told apart where only the owner in their own shell may act.
CHAT_ENV = "ORCH_CHAT_SESSION"


def mcp_config() -> str:
    own = McpServer(
        sys.executable, ["-m", "orchestrator.mcp_server"], {"ORCH_HOME": str(config.home())}
    )
    return json.dumps({"mcpServers": {kit.OWN_SERVER: kit.own_server_entry(own)}})


# -- orchestrator session ---------------------------------------------------


# The orchestrator role is written for the headless orchestrator too; this session is the
# user's own conversation, where request_decision is refused (no agent records it).
CHAT_NOTE = (
    "## This session\n"
    "You talk with the user directly: ask them here for anything that needs their decision;"
    " `request_decision` is for the headless orchestrator and leads. `withdraw_decision`"
    " works here too: it closes an open decision of any agent that is no longer needed."
)


@app.command()
def chat(
    resume: Annotated[
        bool, typer.Option("--continue", "-c", help="Continue the last orchestrator conversation.")
    ] = False,
    here: Annotated[
        bool,
        typer.Option(help="Run in the current directory instead of the orchestrator workspace."),
    ] = False,
    model: Annotated[
        str | None,
        typer.Option(help="Model for the orchestrator session; default: the orchestrator tier's."),
    ] = None,
    effort: Annotated[
        str | None,
        typer.Option(help="Effort for the orchestrator session; default: the orchestrator tier's."),
    ] = None,
) -> None:
    """Open the orchestrator: an interactive Claude Code session that steers all agents."""
    if not shutil.which("claude", path=locate.search_path()):
        raise fail(
            f"the `claude` CLI is not available: {locate.not_found('claude')}; {locate.says_more()}"
        )
    data = orch().settings()
    tier = data.get("orchestrator", {}).get("claude", {})
    bot_coauthor = data.get(BOT_COAUTHOR, True) is not False
    model = model or tier.get("model")
    effort = effort or tier.get("effort")
    if effort and effort not in providers.get("claude").efforts:
        allowed = ", ".join(providers.get("claude").efforts)
        raise fail(f"claude has no effort '{effort}' (use: {allowed})")
    credit = getattr(providers.get("claude"), "credit", None)
    role = roles.load("orchestrator")
    try:
        # No project: only $ORCH_HOME/loadout.toml applies.
        chosen = loadout.resolve(
            role, warn=lambda text: typer.secho(f"warning: {text}", fg="yellow", err=True)
        )
    except loadout.LoadoutError as exc:
        raise fail(str(exc)) from None
    if chosen.tools is not None and "Bash" not in chosen.tools:
        chosen = replace(chosen, tools=(*chosen.tools, "Bash"))  # for `spartito wait` and co.
    # A person works in this session: --disable-slash-commands would also take away the
    # built-in /compact, /clear, /model and /context. Headless agents keep the setting.
    chosen = replace(chosen, slash_commands=True)
    # No capability wirings here: only `tool_search = always` adds ToolSearch (for `orch`).
    search, _ = tool_search(chosen, own_server=True)
    argv = [
        "claude",
        "--mcp-config", mcp_config(),
        "--append-system-prompt",
        f"{role.prompt}\n\n{CHAT_NOTE}\n\n{roles.attribution(credit, bot_coauthor=bot_coauthor)}",
        "--allowedTools", "mcp__orch", f"Bash({CLI} *)",
        *loadout_flags(chosen, search=search),
    ]  # fmt: skip
    if resume:
        argv.append("--continue")
    else:
        argv += ["--name", config.ORCHESTRATOR_NAME]  # a display label: --continue goes by cwd
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    if not here:
        os.chdir(config.workspace_dir())
    os.execvpe("claude", argv, {**os.environ, "PATH": locate.search_path(), CHAT_ENV: "1"})


@app.command()
def web(host: str = "127.0.0.1", port: int = 8787) -> None:
    """Serve the web dashboard: add projects, start agents and talk to them."""
    from .web import serve
    from .web.app import LOOPBACK_HOSTS

    server = serve.make(host, port)
    if host not in LOOPBACK_HOSTS:
        typer.secho(
            "warning: the web UI has no authentication and can start agents that run code; "
            "anyone who can reach this address controls them.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    typer.echo(f"web UI: http://{host}:{port}")
    serve.serve(server)


app_app = typer.Typer(
    rich_markup_mode=MARKUP,
    help="Open the dashboard in its own window: a native one with the extra `spartito[app]`,"
    " else a Chromium-family browser in app mode, else the default browser. Uses the running"
    f" `{CLI} web` of this home, or serves the dashboard on 127.0.0.1 until the window is closed."
    " With the window open already, brings it to the front (at `--agent` or `--decision`)."
    f" `{CLI} app install` adds a launcher that starts it like an application.",
)
app.add_typer(app_app, name="app")
LauncherName = Annotated[
    str | None, typer.Option("--name", hidden=True, help="Launcher name (for tests).")
]
LauncherDir = Annotated[
    str | None, typer.Option("--dir", hidden=True, help="Launcher directory (for tests).")
]


def app_launcher(name: str | None, directory: str | None):
    from pathlib import Path

    from .gui import launcher

    try:
        return launcher.launcher(name, Path(directory) if directory else None)
    except launcher.LauncherError as exc:
        raise fail(exc) from None


@app_app.callback(invoke_without_command=True)
def app_window(
    ctx: typer.Context,
    port: Annotated[
        int, typer.Option(help="Port of its own server when none is running (default: a free one).")
    ] = 0,
    agent: Annotated[str | None, typer.Option(help="Open it at this agent.")] = None,
    decision: Annotated[int | None, typer.Option(help="Open it at this decision.")] = None,
    close_after: Annotated[
        float | None, typer.Option("--close-after", hidden=True, help="Close after SECONDS.")
    ] = None,
    echo_location: Annotated[
        bool,
        typer.Option("--echo-location", hidden=True, help="Say where the native window's page is."),
    ] = False,
    notify: Annotated[
        bool,
        typer.Option(
            "--notify", hidden=True, help="Post the pending notifications as the app, then exit."
        ),
    ] = False,
    name: LauncherName = None,
    directory: LauncherDir = None,
) -> None:
    """Open the dashboard in its own window: a native one with the extra `spartito[app]`,
    else a Chromium-family browser in app mode, else the default browser. Uses the running
    `{cli} web` of this home, or serves the dashboard on 127.0.0.1 until the window is closed.
    With the window open already, brings it to the front (at `--agent` or `--decision`)."""
    if ctx.invoked_subcommand:
        return
    from . import app_open
    from .gui import window

    if agent is not None and decision is not None:
        raise fail("use either --agent or --decision")
    if decision is not None and decision < 0:
        raise fail("--decision takes a decision number")
    target = agent or (f"decision:{decision}" if decision is not None else "")
    if notify:  # started hidden by a background process (desktop.py); a click: the window
        target = window.notify_only() or ""
        if not target:
            return
    try:
        app_open.check_target(target)
    except ValueError as exc:
        raise fail(exc) from None
    item = app_launcher(name, directory) if name or directory else None
    try:
        code = window.run(port, close_after, target, item, echo_location)
    except window.AppError as exc:
        raise fail(exc) from None
    if code:
        raise typer.Exit(code)


@app_app.command("install")
def app_install(name: LauncherName = None, directory: LauncherDir = None) -> None:
    """Install a launcher that opens `{cli} app` like an application (macOS:
    ~/Applications/Spartito.app; Linux: a desktop entry). Running it again rewrites it."""
    from .gui import launcher

    item = app_launcher(name, directory)
    notes: list[str] = []
    try:
        path = launcher.install(item, notes)
    except launcher.LauncherError as exc:
        raise fail(exc) from None
    typer.echo(f"installed {item.name} ({item.identifier}): {path}")
    for note in notes:
        typer.echo(note)


@app_app.command("uninstall")
def app_uninstall(name: LauncherName = None, directory: LauncherDir = None) -> None:
    """Remove the launcher `{cli} app install` wrote for this home."""
    from .gui import launcher

    item = app_launcher(name, directory)
    path = launcher.uninstall(item)
    if path:
        typer.echo(f"removed {item.name}: {path}")
    else:
        typer.echo(f"{item.name} is not installed ({item.path})")


@app.command()
def menubar(
    close_after: Annotated[
        float | None, typer.Option("--close-after", hidden=True, help="Quit after SECONDS.")
    ] = None,
    print_lines: Annotated[
        bool, typer.Option("--print", hidden=True, help="Print the title and menu on changes.")
    ] = False,
) -> None:
    """Show the running agents and the open decisions in the macOS menu bar (the extra
    `spartito[menubar]`); its menu opens a decision or the dashboard window."""
    from .gui import menubar as item

    echo = (lambda text: typer.echo(text + "\n", nl=False)) if print_lines else None
    try:
        code = item.run(close_after, echo)
    except item.MenubarError as exc:
        raise fail(exc) from None
    if code:
        raise typer.Exit(code)


# -- background service -----------------------------------------------------

service_app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode=MARKUP,
    help=f"Run `{CLI} web` in the background: started at login and restarted when it dies"
    " (macOS: a launchd agent; Linux: a systemd user unit).",
)
app.add_typer(service_app, name="service")
LabelOption = Annotated[
    str | None, typer.Option("--label", hidden=True, help="Service label (for tests).")
]


def autostart_fail(action):
    try:
        return action()
    except autostart.AutostartError as exc:
        raise fail(exc) from None


def service_label(label: str | None) -> str:
    """The --label given (checked before anything runs) or the home's default label."""
    if label is not None:
        return autostart_fail(lambda: autostart.check_label(label))
    return autostart.default_label(autostart.home())


@service_app.command("install")
def service_install(
    host: Annotated[str, typer.Option(help=f"As for `{CLI} web`.")] = autostart.DEFAULT_HOST,
    port: Annotated[int, typer.Option(help=f"As for `{CLI} web`.")] = autostart.DEFAULT_PORT,
    label: LabelOption = None,
) -> None:
    """Install the dashboard service and (re)start it. Running it again replaces it."""
    from .web.app import LOOPBACK_HOSTS

    svc = autostart.service(host, port, service_label(label))
    unit = autostart_fail(lambda: autostart.install(svc))
    if host not in LOOPBACK_HOSTS:
        typer.secho(
            "warning: the web UI has no authentication and can start agents that run code; "
            "anyone who can reach this address controls them.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    kind = autostart.platform_kind()
    typer.echo(f"installed {svc.label}: http://{host}:{port}")
    typer.echo(f"unit: {unit}")
    typer.echo(f"logs: {autostart.logs_hint(svc.log, svc.label, kind)}")


@service_app.command("uninstall")
def service_uninstall(label: LabelOption = None) -> None:
    """Stop the dashboard service and remove it."""
    name = service_label(label)
    unit = autostart_fail(lambda: autostart.uninstall(name))
    typer.echo(f"removed {name}: {unit}" if unit else f"{name} is not installed")


@service_app.command("status")
def service_status(label: LabelOption = None) -> None:
    """Whether the dashboard service is installed and running, and who answers on its port."""
    name = service_label(label)
    st = autostart_fail(lambda: autostart.status(name))
    typer.echo(f"service: {st.label} ({st.platform})")
    typer.echo(f"unit:    {st.unit}" + ("" if st.installed else " (not installed)"))
    running = "loaded" if st.platform == "launchd" else "active"
    state = f"{running}, pid {st.pid}" if st.pid else running if st.loaded else f"not {running}"
    typer.echo(f"state:   {state}")
    if st.host and st.port:
        url = f"http://{st.host}:{st.port}"
        answer = {
            "ours": "answers",
            "foreign": f"something else answers ({st.detail})",
            "none": "nothing answers",
        }[st.answer]
        typer.echo(f"web UI:  {url}: {answer}")
    typer.echo(f"logs:    {st.logs}")


@service_app.command("logs")
def service_logs(
    lines: Annotated[int, typer.Option("--lines", "-n", help="Last lines shown (macOS).")] = 20,
    label: LabelOption = None,
) -> None:
    """Where the service writes its output (macOS: its last lines too)."""
    name = service_label(label)
    kind = autostart_fail(autostart.platform_kind)
    log = autostart.home() / "logs" / "web.log"
    typer.echo(autostart.logs_hint(log, name, kind))
    if kind == "launchd" and lines > 0 and log.exists():
        with log.open(errors="replace") as handle:
            tail = handle.readlines()[-lines:]
        typer.echo("".join(tail), nl=False)


# -- projects ---------------------------------------------------------------


@project_app.command("add")
def project_add(
    path: Annotated[str, typer.Argument(help="A directory inside the git repository.")] = ".",
    name: Annotated[
        str | None, typer.Option(help="Defaults to the repository directory name.")
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option(
            help="Default agent provider for the project; default: the first available one"
            f" (`{CLI} providers`)."
        ),
    ] = None,
) -> None:
    """Register a git repository as a project."""
    try:
        o = orch()
        project = o.add_project(path, name, provider)
    except (OrchestratorError, KeyError) as exc:
        raise fail(exc) from None
    typer.echo(
        f"added {project.name}  {project.path}  (base branch: {project.base_branch},"
        f" provider: {project.default_provider})"
    )
    if note := o.project_note(project):
        typer.secho(f"note: {note}", fg=typer.colors.YELLOW)


@project_app.command("provider")
def project_provider(
    project: str,
    provider: Annotated[str, typer.Argument(help="The provider of the project's new agents.")],
) -> None:
    """Set the provider a project's new agents get (it must be available here). Existing
    agents keep theirs; `{cli} model AGENT --provider NAME` moves one."""
    o = orch()
    try:
        found = o.set_default_provider(project, provider)
    except OrchestratorError as exc:
        raise fail(exc) from None
    typer.echo(f"{found.name}: new agents run on {found.default_provider}")
    if note := o.default_provider_note(found):
        typer.secho(f"note: {note}", fg=typer.colors.YELLOW)


@project_app.command("list")
def project_list() -> None:
    """List projects."""
    o = orch()
    for project in o.projects():
        count = o.agent_count(project.name)
        typer.echo(
            f"{project.name:<20} {project.default_provider:<7} {count:>3} agents  {project.path}"
        )


@project_app.command("rm")
def project_rm(
    name: str,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Delete its archived agents without asking.")
    ] = False,
) -> None:
    """Unregister a project with its settings, budget, board items and decisions. The
    repository and its branches are not touched.

    Agents that are not archived must be archived first (`{cli} archive`), and archived ones
    with work that is not merged or committed deleted by hand (`{cli} rm`); the refusal names
    the commands. The other archived agents are deleted with the project, as `{cli} rm`
    deletes one (their branches are kept), after a confirmation.
    """
    o = orch()
    try:
        try:
            result = o.remove_project(name)
        except ConfirmAgents as exc:
            if not yes and not typer.confirm(f"{exc}. Remove anyway?"):
                raise typer.Exit(1) from None
            result = o.remove_project(name, exc.agents)  # refused if they changed meanwhile
    except OrchestratorError as exc:
        raise fail(exc) from None
    for agent_id in result["agents"]:
        typer.echo(f"deleted {agent_id}")
    if result["branches"]:
        typer.echo(f"branches kept: {', '.join(result['branches'])}")
    items, asked = len(result["board_items"]), len(result["decisions"])
    extra = [f"{items} board item{'s' * (items != 1)}"] if items else []
    extra += [f"{asked} decision{'s' * (asked != 1)}"] if asked else []
    deleted = f" ({' and '.join(extra)} deleted)" if extra else ""
    typer.echo(f"removed {result['removed']}{deleted}")


# -- settings ---------------------------------------------------------------


@config_app.command("show")
def config_show(project: Annotated[str | None, typer.Option("--project", "-p")] = None) -> None:
    """Show the settings: global, or a project's overrides."""
    o = orch()
    try:
        data = o.settings(project)
    except OrchestratorError as exc:
        raise fail(exc) from None
    typer.secho(f"{project or 'global'} settings", bold=True)
    for tier in TIERS:
        for provider, values in data.get(tier, {}).items():
            typer.echo(
                f"  {tier:<13} {provider:<7} model={values.get('model', '-'):<16} effort={values.get('effort', '-')}"
            )
    if not any(tier in data for tier in TIERS):
        typer.echo(
            "  (none: agents use the CLI defaults)" if not project else "  (none: inherits global)"
        )
    if ARCHIVE_AFTER in data:
        typer.echo(f"  archive after {hours_text(data[ARCHIVE_AFTER])}")
    elif not project:
        typer.echo(f"  archive after {hours_text(ARCHIVE_AFTER_DEFAULT)} (default)")
    if not project:
        on = data.get(KEEP_AWAKE, True) is not False
        default = "" if KEEP_AWAKE in data else " (default)"
        typer.echo(f"  keep awake while agents work: {'on' if on else 'off'}{default}")
        on = data.get(DESKTOP_NOTIFICATIONS, True) is not False
        default = "" if DESKTOP_NOTIFICATIONS in data else " (default)"
        typer.echo(f"  desktop notifications: {'on' if on else 'off'}{default}")
        on = data.get(BOT_COAUTHOR, True) is not False
        default = "" if BOT_COAUTHOR in data else " (default)"
        typer.echo(f"  bot co-author trailer in agents' commits: {'on' if on else 'off'}{default}")
    if COMPACT_AT in data:
        typer.echo(f"  compact automatically {compact_text(data[COMPACT_AT])}")
    elif not project:
        typer.echo(f"  compact automatically {compact_text(COMPACT_AT_DEFAULT)} (default)")


def compact_text(tokens: int) -> str:
    return "off (never)" if not tokens else f"above {tokens:,} tokens of context"


def hours_text(hours: float) -> str:
    return "off (never automatically)" if not hours else f"{hours:g} idle hours"


@config_app.command("set")
def config_set(
    tier: Annotated[str, typer.Argument(help=f"One of: {', '.join(TIERS)}.")],
    provider: Annotated[str, typer.Option(help="Provider the values apply to.")] = "claude",
    model: Annotated[str | None, typer.Option(help='Model name; "" clears it.')] = None,
    effort: Annotated[str | None, typer.Option(help='Effort level; "" clears it.')] = None,
    project: Annotated[
        str | None,
        typer.Option(
            "--project", "-p", help="Override for one project instead of the global default."
        ),
    ] = None,
) -> None:
    """Set the default model and/or effort for a tier, globally or for one project."""
    o = orch()
    try:
        data = o.settings(project)
        values = data.setdefault(tier, {}).setdefault(provider, {})
        for key, value in (("model", model), ("effort", effort)):
            if value is not None:
                values[key] = value
        o.update_settings(data, project)
    except OrchestratorError as exc:
        raise fail(exc) from None
    config_show(project)


@config_app.command("archive-after")
def config_archive_after(
    hours: Annotated[
        str, typer.Argument(help='Idle hours; 0 or "off": never; "" clears (inherit).')
    ],
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="For one project instead of the global default."),
    ] = None,
) -> None:
    """Archive finished agents automatically after this many idle hours (default 24).

    Only agents whose work is safe: no worktree of their own, or their branch merged into
    the project's base branch and no uncommitted changes. Never project leads or the
    orchestrator, never while they or their agents work.
    """
    o = orch()
    try:
        data = o.settings(project)
        data[ARCHIVE_AFTER] = hours
        o.update_settings(data, project)
    except OrchestratorError as exc:
        raise fail(exc) from None
    config_show(project)


@config_app.command("keep-awake")
def config_keep_awake(
    value: Annotated[str, typer.Argument(help='"on" or "off"; "" clears (default: on).')],
) -> None:
    """Keep the machine from sleeping on idle while any agent's turn runs (default on).

    macOS: caffeinate -i (the display may still sleep); Linux: systemd-inhibit. A closed
    lid or a forced sleep is outside its control.
    """
    o = orch()
    try:
        data = o.settings()
        data[KEEP_AWAKE] = value
        o.update_settings(data)
    except OrchestratorError as exc:
        raise fail(exc) from None
    config_show(None)


@config_app.command("compact-at")
def config_compact_at(
    tokens: Annotated[
        str, typer.Argument(help='Tokens (e.g. 256000 or 256k); 0 or "off": never; "" clears.')
    ],
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="For one project instead of the global default."),
    ] = None,
) -> None:
    """Compact an agent's conversation automatically once a turn ends with its context
    above this many tokens (default 256k).

    The compaction runs before the agent's next turn (ahead of a queued message). Not again
    while the agent's last compaction left the context above the threshold: that is shown
    in its log and `{cli} status`. Leads keep their rules, state and open work.
    """
    o = orch()
    try:
        data = o.settings(project)
        data[COMPACT_AT] = tokens
        o.update_settings(data, project)
    except OrchestratorError as exc:
        raise fail(exc) from None
    config_show(project)


@config_app.command("desktop-notifications")
def config_desktop_notifications(
    value: Annotated[str, typer.Argument(help='"on" or "off"; "" clears (default: on).')],
) -> None:
    """Desktop notifications on this machine (default on; macOS only): a decision waits
    for you, or an agent you messaged directly has finished."""
    o = orch()
    try:
        data = o.settings()
        data[DESKTOP_NOTIFICATIONS] = value
        o.update_settings(data)
    except OrchestratorError as exc:
        raise fail(exc) from None
    config_show(None)


@config_app.command("bot-coauthor")
def config_bot_coauthor(
    value: Annotated[str, typer.Argument(help='"on" or "off"; "" clears (default: on).')],
) -> None:
    """Whether agents add the trailer that credits the project's GitHub App bot to every
    commit message (default on), so that GitHub lists the project under a repository's
    Contributors.

    Applies to turns started after the change. An organization's managed attribution
    settings take precedence: then agents add exactly those lines and no bot trailer.
    """
    o = orch()
    try:
        data = o.settings()
        data[BOT_COAUTHOR] = value
        o.update_settings(data)
    except OrchestratorError as exc:
        raise fail(exc) from None
    config_show(None)


# -- decisions --------------------------------------------------------------


def age(timestamp: str) -> str:
    try:
        seconds = (datetime.now(UTC) - datetime.fromisoformat(timestamp)).total_seconds()
    except ValueError:
        return timestamp
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h" if minutes < 48 * 60 else f"{minutes // 1440}d"


@app.command()
def decisions(
    all_: Annotated[bool, typer.Option("--all", "-a", help="Include closed ones.")] = False,
) -> None:
    """List the decisions agents wait for (answer with `{cli} decide`)."""
    listed = orch().list_decisions(closed=all_)
    if not listed:
        typer.echo("no decisions" if all_ else "no open decisions")
    for item in listed:
        where = item["project"] or "-"
        note = {"archived": " (archived: an answer restores it)", "gone": " (agent removed)"}
        head = f"#{item['id']}  {where} · {item['agent_name']} · {age(item['created_at'])} ago"
        typer.secho(head + note.get(item["agent_state"] or "", ""), bold=True, nl=False)
        if item["state"] != "open":
            typer.secho(f"  [{item['state']}]", fg=typer.colors.MAGENTA, nl=False)
        typer.echo(f"\n  {item['question']}")
        for number, option in enumerate(item["options"], 1):
            typer.echo(f"    {number}) {option}")
        if item["recommendation"]:
            typer.echo(f"  recommends: {item['recommendation']}")
        if item["answer"] is not None:
            typer.echo(f"  answer: {item['answer']}")
        if item["note"]:
            typer.echo(f"  note: {item['note']}")


@app.command()
def notices(
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many, newest first.")] = 20,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Each notice's steps as well.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="As JSON, with the steps.")] = False,
) -> None:
    """How the recent desktop notifications went: one line per notice (newest first) with the
    way it was shown, by which process, when, and why (docs/desktop.md: the trace)."""
    if limit < 1:
        raise fail("--limit takes a positive number")
    found = orch().notice_trace(limit)
    if as_json:
        typer.echo(json.dumps(found, indent=2))
        return
    if not found:
        typer.echo("no notices")
    for notice in found:
        line = notice["summary"]
        head = (
            f"#{notice['id']}  {notice['created_at']}  {notice['kind']}  {line['path']}"
            f"  {line['actor']}  {line['at']}"
        )
        typer.secho(head, bold=verbose, nl=False)
        typer.echo(f"  — {line['reason']}" if line["reason"] else "")
        if verbose:
            typer.echo(f"  {one_line(notice['text'], 100)}")
            for item in notice["steps"]:
                where = f" {item['path']}" if item["path"] else ""
                detail = f": {item['detail']}" if item["detail"] else ""
                typer.echo(f"    {item['at']}  {item['actor']}  {item['step']}{where}{detail}")


@app.command()
def decide(
    decision_id: int,
    answer: Annotated[str, typer.Argument(help="An option number, or free text.")],
) -> None:
    """Answer a decision; the agent receives it as a message from you."""
    try:
        result = orch().answer_decision(decision_id, answer)
    except OrchestratorError as exc:
        raise fail(exc) from None
    chosen = result["decision"]["answer"]
    typer.echo(f"#{decision_id} answered: {chosen} -> {result['agent_id']}  run {result['run_id']}")


# -- task board -------------------------------------------------------------

board_app = typer.Typer(
    rich_markup_mode=MARKUP, help="The task board of each project: ideas and tasks."
)
app.add_typer(board_app, name="board")
ProjectOption = Annotated[str | None, typer.Option("--project", "-p")]
StateOption = Annotated[
    str | None, typer.Option("--state", "-s", help="idea|ready|in_progress|done")
]


def board_line(item) -> str:
    links = " ".join(f"[{link}]" for link in (item.agent_id, item.branch) if link)
    return f"  #{item.id}  {item.title}" + (f"  {links}" if links else "")


@board_app.callback(invoke_without_command=True)
def board_list(
    ctx: typer.Context,
    project: ProjectOption = None,
    state: StateOption = None,
    live: Annotated[
        bool, typer.Option("--live", help="Each item's agent status and branch state.")
    ] = False,
) -> None:
    """List the board by project and state (add, edit, move and dispatch below)."""
    if ctx.invoked_subcommand:
        return
    o = orch()
    if live:
        o.board_sync()
    try:
        items = o.board_items(project, state)
    except OrchestratorError as exc:
        raise fail(exc) from None
    lives = o.board_live(items) if live else {}
    if not items:
        typer.echo("the board is empty")
    heading = None
    for item in items:
        if (item.project, item.state) != heading:
            heading = (item.project, item.state)
            typer.secho(f"{item.project} · {item.state}", bold=True)
        typer.echo(board_line(item) + (board_live_text(lives[item.id]) if live else ""))
        if item.description:
            typer.echo(f"      {item.description.splitlines()[0][:100]}")


def board_live_text(live: dict) -> str:
    """ "  · agent running · branch +3 dirty", what is known of it."""
    from .board import git_text

    parts = []
    if live["agent"]:
        parts.append(f"agent {live['agent']['status']}")
    if live["git"]:
        parts.append(f"branch {git_text(live['git'])}")
    return "".join(f"  · {part}" for part in parts)


def board_fail(action):
    try:
        return action()
    except OrchestratorError as exc:
        raise fail(exc) from None


@board_app.command("add")
def board_add(
    title: str,
    project: Annotated[str, typer.Option("--project", "-p")],
    description: Annotated[str, typer.Option("--description", "-d")] = "",
    state: Annotated[str, typer.Option("--state", "-s")] = "idea",
) -> None:
    """Put an item on a project's board."""
    item = board_fail(lambda: orch().add_board_item(project, title, description, state))
    typer.echo(f"#{item.id} added to {item.project} · {item.state}")


@board_app.command("edit")
def board_edit(
    item_id: int,
    title: Annotated[str | None, typer.Option("--title")] = None,
    description: Annotated[str | None, typer.Option("--description", "-d")] = None,
    state: StateOption = None,
    agent: Annotated[str | None, typer.Option("--agent", help='An agent id; "" clears.')] = None,
    branch: Annotated[str | None, typer.Option("--branch", help='"" clears.')] = None,
) -> None:
    """Change an item's title, description, state or links."""
    given = {"title": title, "description": description, "state": state}
    changes = {
        key: value
        for key, value in {**given, "agent_id": agent, "branch": branch}.items()
        if value is not None
    }
    if not changes:
        raise fail("nothing to change")
    item = board_fail(lambda: orch().update_board_item(item_id, changes))
    typer.echo(f"#{item.id} {item.project} · {item.state}")
    typer.echo(board_line(item))


def board_change(changes: dict) -> str:
    """{field: [old, new]} as "field: old → new", the description as "description changed"."""
    parts = []
    for key, value in changes.items():
        if isinstance(value, list):
            old, new = ("–" if part in (None, "") else part for part in value)
            parts.append(f"{key}: {old} → {new}")
        else:
            parts.append(f"{key} {value}")
    return ", ".join(parts)


def board_actor(entry: dict) -> str:
    kind, name = entry["actor_kind"], entry["actor"]
    if kind is None:  # an unrecorded gap: nobody known
        who = "–"
    elif kind == "owner":
        who = "owner"
    elif kind == "agent":
        who = f"agent {name}" if name else "chat session"
    else:
        who = f"{kind}: {name}"
    return who + (f" (run {entry['run_id']})" if entry["run_id"] else "")


@board_app.command("rm")
def board_rm(
    item_id: int,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Delete without asking.")] = False,
) -> None:
    """Delete an item for good, with its history and its decision links."""
    o = orch()
    item = board_fail(lambda: o.board_item(item_id))
    if not yes and not typer.confirm(f"Delete #{item.id} {item.title!r} from {item.project}?"):
        raise typer.Exit(1)
    board_fail(lambda: o.delete_board_item(item_id))
    typer.echo(f"#{item.id} deleted from {item.project}")


@board_app.command("show")
def board_show(item_id: int) -> None:
    """Show an item with its history and the runs that worked on it."""
    detail = board_fail(lambda: orch().board_detail(item_id))
    item = detail["item"]
    links = " ".join(f"[{link}]" for link in (item["agent_id"], item["branch"]) if link)
    typer.secho(f"#{item['id']}  {item['title']}", bold=True)
    typer.echo(f"  {item['project']} · {item['state']}" + (f"  {links}" if links else ""))
    typer.echo(f"  added {item['created_at']} · changed {item['updated_at']}")
    for line in item["description"].splitlines():
        typer.echo(f"  | {line}")
    if source := detail.get("source"):
        typer.echo(f"  from {source['agent_id']}, message {source['event_id']}")
    for blocker in detail.get("blockers", []):
        typer.echo(f"  blocked by decision #{blocker['id']}: {blocker['question']}")
    typer.secho("history", bold=True)
    for entry in detail["history"]:
        change = board_change(entry["changes"])
        line = f"  {entry['ts']}  {board_actor(entry)}  {entry['action']}"
        typer.echo(line + (f"  {change}" if change else ""))
    if detail["unrecorded"]:
        gap = "a later change" if detail["history"] else "its changes so far"
        typer.echo(f"  ({gap} not recorded)")
    if detail["runs"]:
        typer.secho("runs", bold=True)
    for run in detail["runs"]:
        ended = f" → {run['ended_at']}" if run["ended_at"] else ""
        typer.echo(
            f"  run {run['id']}  {run['agent_id']}  {run['status']}  {run['created_at']}{ended}"
        )


@board_app.command("move")
def board_move(item_id: int, direction: Annotated[str, typer.Argument(help="up or down")]) -> None:
    """Move an item up or down within its state."""
    o = orch()
    item = board_fail(lambda: o.move_board_item(item_id, direction))
    order = [other.id for other in o.board_items(item.project, item.state)]
    place = f"{order.index(item.id) + 1} of {len(order)}"
    typer.echo(f"#{item.id} is now {place} in {item.project} · {item.state}")


@board_app.command("link")
def board_link(
    item_id: int,
    decision_id: int,
    remove: Annotated[bool, typer.Option("--remove", help="Unlink it.")] = False,
) -> None:
    """Link a decision to the item as one that blocks it (--remove: unlink it)."""
    blockers = board_fail(lambda: orch().link_decision(item_id, decision_id, not remove))
    if not blockers:
        typer.echo(f"#{item_id} is not blocked")
    for blocker in blockers:
        how = "linked" if blocker["linked"] else "unlinked, of its agent"
        typer.echo(
            f"#{item_id} blocked by decision #{blocker['id']} ({how}): {blocker['question']}"
        )


@board_app.command("dispatch")
def board_dispatch(
    ids: Annotated[
        list[int] | None, typer.Argument(help="Item ids (idea or ready), in any project.")
    ] = None,
    project: ProjectOption = None,
) -> None:
    """Send board items to their project's lead, one message per project (they become
    in_progress): the ids named, or with -p alone the project's ready items."""
    if ids:
        board_dispatch_items(ids, project)
        return
    if not project:
        raise fail("name item ids or --project")
    result = board_fail(lambda: orch().dispatch_board(project))
    if not result["items"]:
        typer.echo(f"no ready items in {project}; nothing sent")
        return
    ids = ", ".join(f"#{item['id']}" for item in result["items"])
    typer.echo(f"{ids} -> {result['agent_id']}  run {result['run_id']}")
    print_note(result.get("note"))


def board_dispatch_items(ids: list[int], project: str | None) -> None:
    result = board_fail(lambda: orch().dispatch_items(ids, project))
    for sent in result["results"]:
        names = ", ".join(f"#{item['id']}" for item in sent["items"])
        typer.echo(f"{names} -> {sent['agent_id']}  run {sent['run_id']}")
        print_note(sent.get("note"))
    for skipped in result["skipped"]:
        typer.echo(f"#{skipped['id']} not sent: {skipped['reason']}")
    if not result["results"]:
        typer.echo("nothing sent")


# -- agents -----------------------------------------------------------------


@app.command()
def spawn(
    task: Annotated[str, typer.Argument(help="The self-contained brief for the agent.")],
    project: Annotated[str, typer.Option("--project", "-p")],
    name: Annotated[str | None, typer.Option("--name", "-n")] = None,
    role: Annotated[str, typer.Option("--role", "-r")] = "worker",
    provider: Annotated[
        str | None, typer.Option(help=f"One of: {', '.join(providers.PROVIDERS)}.")
    ] = None,
    model: Annotated[str | None, typer.Option(help="Overrides the configured default.")] = None,
    effort: Annotated[str | None, typer.Option(help="Overrides the configured default.")] = None,
    worktree: Annotated[
        bool | None,
        typer.Option(
            "--worktree/--no-worktree",
            help="Own worktree, or the main checkout. Default depends on the role.",
        ),
    ] = None,
    share: Annotated[str | None, typer.Option(help="Work in this agent's worktree.")] = None,
    base: Annotated[str | None, typer.Option(help="Ref or agent id to branch from.")] = None,
    after: Annotated[
        list[str] | None, typer.Option(help="Agent or run id to wait for; repeatable.")
    ] = None,
    permission: Annotated[
        str | None,
        typer.Option(
            help="Claude permission mode / Codex sandbox mode / opencode: default or auto."
        ),
    ] = None,
    granted: Annotated[
        str | None,
        typer.Option(
            "--capabilities",
            help='The agent\'s own capabilities: "chrome" or "mcp:NAME" turns one on,'
            ' "-chrome" off; comma-separated.',
        ),
    ] = None,
) -> None:
    """Start a new agent on a task and return immediately."""
    try:
        own = capabilities.parse(granted or "")
    except ValueError as exc:
        raise fail(exc) from None
    o = orch()
    try:
        agent, run = o.spawn(
            project, task, name=name, role=role, provider=provider, model=model, effort=effort,
            worktree=worktree, share_with=share, base=base, after=after, permission=permission,
            capabilities=own, granter=calling_agent(o) if own else None,
        )  # fmt: skip
    except OrchestratorError as exc:
        raise fail(exc) from None
    typer.echo(f"{agent.id}  run {run.id}  {agent.cwd}")
    print_note(agent.provider_note)


def print_note(note: str | None) -> None:
    if note:
        typer.secho(f"note: {note}", fg=typer.colors.YELLOW)


@app.command()
def ask(
    project: str,
    message: str,
    provider: Annotated[
        str | None,
        typer.Option(help="The lead's provider when it is created now; default: the project's."),
    ] = None,
) -> None:
    """Send a message to a project's lead agent (created on first use)."""
    o = orch()
    try:
        lead = o.lead(project, provider=provider)
        run = o.send(lead.id, message)
    except OrchestratorError as exc:
        raise fail(exc) from None
    typer.echo(f"{lead.id}  run {run.id}")
    print_note(lead.provider_note)


@app.command()
def send(
    agent_id: str,
    message: str,
    after: Annotated[
        list[str] | None, typer.Option(help="Agent or run id to wait for; repeatable.")
    ] = None,
) -> None:
    """Send a follow-up message to an agent (queued if it is busy)."""
    try:
        run = orch().send(agent_id, message, after)
    except OrchestratorError as exc:
        raise fail(exc) from None
    typer.echo(f"run {run.id}")


@app.command()
def ps(
    project: Annotated[str | None, typer.Option("--project", "-p")] = None,
    archived: Annotated[
        bool, typer.Option("--archived", "-a", help="List the archived agents instead.")
    ] = False,
) -> None:
    """List agents as a tree: project -> agent -> sub-agents (archived ones: --archived).

    `idle·3↓`: no own turn, 3 sub-agents still working (it is notified when they finish);
    `failed·3↓`: its last turn failed while they work; `running +2`: two more turns queued
    behind the running one.
    """
    o = orch()
    if archived:
        show_archived(o, project)
        return
    views = o.overview(project)
    width = max((len(state_label(view)[0]) for view in views), default=0)
    width = max(width, 8)
    agents = {agent.id: agent for agent in o.agents(project)}
    children: dict[str | None, list[dict]] = {}
    home = {view["id"]: view["project"] for view in views}
    for view in views:
        same_project = home.get(view["parent_id"]) == view["project"]
        parent = view["parent_id"] if same_project else None
        children.setdefault((view["project"], parent), []).append(view)

    def show(project_name: str, parent: str | None, depth: int) -> None:
        for view in children.get((project_name, parent), []):
            text, color = state_label(view)
            status = typer.style(f"{text:<{width}}", fg=color)
            label = "  " * depth + view["id"]
            cost = f"${view['cost_usd']:.2f}" if view["cost_usd"] else ""
            typer.echo(
                f"  {label:<34} {status} {view['role']:<9} {view['provider']:<7}"
                f" {setup(view, agents.get(view['id'])):<18} {context_styled(view['context'], 14)}"
                f" {tokens(view['tokens']):<15} {cost:<7} {view['branch'] or ''}"
            )
            show(project_name, view["id"], depth + 1)

    for view in views:
        if view["project"] is None and not project:
            text, color = state_label(view)
            status = typer.style(text, fg=color)
            cost = f"${view['cost_usd']:.2f}" if view["cost_usd"] else ""
            setup_text = setup(view, agents.get(view["id"]) or o.get_agent(view["id"]))
            context = context_text(view["context"]) and context_styled(view["context"])
            extra = "  ".join(filter(None, (setup_text, context, tokens(view["tokens"]), cost)))
            label = shown_name(view["id"])
            typer.secho(f"{label}  {status}  {extra}", bold=True)
    for item in o.projects():
        if project and item.name != project:
            continue
        typer.secho(f"{item.name}  {item.path}", bold=True)
        show(item.name, None, 0)


def show_archived(o: Orchestrator, project: str | None) -> None:
    try:
        views = o.overview(project, archived=True)
    except OrchestratorError as exc:
        raise fail(exc) from None
    if not views:
        typer.echo("no archived agents")
        return
    for name in sorted({view["project"] for view in views}):
        typer.secho(name, bold=True)
        for view in (view for view in views if view["project"] == name):
            text, color = state_label(view)
            when = f"{view['archived_at'][:16].replace('T', ' ')} {view['archived_how']}"
            typer.echo(
                f"  {view['id']:<34} {typer.style(f'{text:<8}', fg=color)} {view['role']:<9}"
                f" {when:<24} {view['branch'] or ''}"
            )
    typer.echo(f"{CLI} restore <id> brings one back; {CLI} rm <id> deletes it for good")


@app.command()
def status(agent_id: str) -> None:
    """Show one agent: state, last result and the changes in its worktree."""
    o = orch()
    try:
        o.reconcile()
        view = o.view(o.agent(agent_id), detail=True)
        changes = o.changes(agent_id)
    except OrchestratorError as exc:
        raise fail(exc) from None
    last = view.pop("last_run")
    counts, cost = view.pop("tokens"), view.pop("cost_usd")
    context, refusal = view.pop("context"), view.pop("compact_refusal")
    compaction = view.pop("last_compaction")
    queue, working = view.pop("queue"), view.pop("children_running")
    granted = view.pop("capabilities")
    if view["id"] == ORCHESTRATOR_ID:
        view["name"] = config.ORCHESTRATOR_NAME  # the id line keeps `orchestrator`
    if mark := view.pop("archived", None):
        view["archived"] = f"{mark['archived_at']} ({mark['how']}; {CLI} restore {agent_id})"
    for key in ("model", "effort"):
        override = view.pop(f"{key}_override")
        view[key] = f"{view[key] or 'default'}" + (" (set on the agent)" if override else "")
    if idle_with_children({**view, "children_running": working}):
        last_run = "" if view["status"] == "new" else f" (last run: {view['status']})"
        view["status"] = f"idle while its agents work{last_run}"
    for key, value in view.items():
        typer.echo(f"{key:<12} {value}")
    skipped = [item["name"] for item in granted["items"] if item["state"] == "skipped"]
    extras = ", ".join(granted["on"]) or "none"
    skipped_text = f"  (skipped: {', '.join(skipped)}; {CLI} capabilities show {agent_id})"
    typer.echo(f"{'capabilities':<12} {extras}{skipped_text if skipped else ''}")
    plural = "agent" if working == 1 else "agents"
    typer.echo(f"{'children':<12} {f'{working} {plural} working' if working else 'none working'}")
    typer.echo(f"{'usage':<12} {usage_line(counts, cost)}")
    hint = f"{CLI} compact {agent_id}" if refusal is None else f"no compact: {refusal}"
    if context and context["compacted"]:
        typer.echo(f"{'context':<12} compacted; its size is known after the next turn")
    elif context:
        typer.echo(f"{'context':<12} {context_styled(context)}  ({hint})")
    else:
        typer.echo(f"{'context':<12} unknown  ({hint})")
    if automatic := compact_at_text(context):
        typer.echo(f"{'':<12} {automatic}")
    if compaction:  # never the agent's last run or status: shown here
        outcome = compaction["result"] if compaction["status"] == "done" else compaction["error"]
        text = f"#{compaction['id']} {compaction['status']}: {one_line(outcome or '')}"
        typer.secho(
            f"{'compaction':<12} {text}",
            fg=None if compaction["status"] == "done" else typer.colors.RED,
        )
    if queue:
        waiting = max(waiting_turns(view), len(queue))
        typer.echo(f"{'queue':<12} {waiting} waiting")
        for run in queue:
            typer.echo(
                f"  #{run['id']:<6} {run['created_at'][11:19]}  {run['kind']:<12}"
                f" {sender_label(run):<20} {one_line(run['prompt'])}"
            )
        if waiting > len(queue):
            typer.echo(f"  +{waiting - len(queue)} more")
    typer.echo(f"{'changes':<12} {changes.replace(chr(10), chr(10) + ' ' * 13)}")
    if last:
        kind = f" ({last['kind']})" if last["kind"] != "message" else ""
        typer.secho(f"\nlast run {last['id']}{kind}: {last['status']}", bold=True)
        if any(last["usage"].values()):
            usage = last["usage"]
            typer.echo(usage_line(usage["tokens"], usage["cost_usd"], usage["model"]))
        typer.echo(
            last["error"] if last["status"] != "done" and last["error"] else last["result"] or ""
        )


@app.command()
def logs(
    agent_id: str,
    follow: Annotated[bool, typer.Option("--follow", "-f")] = False,
    tail: Annotated[int, typer.Option("--tail", "-n")] = 50,
) -> None:
    """Show an agent's activity (messages, tool calls, results)."""
    o = orch()
    try:
        o.agent(agent_id)
    except OrchestratorError as exc:
        raise fail(exc) from None
    last_id, first = 0, True
    while True:
        for event in o.events(agent_id, after=last_id, tail=tail if first else None):
            last_id = event.id
            kind = typer.style(
                f"{event.kind:<11}",
                fg=typer.colors.RED if event.kind == "error" else typer.colors.BLUE,
            )
            typer.echo(f"{event.ts[11:19]} r{event.run_id} {kind} {event.text}")
        first = False
        if not follow:
            return
        time.sleep(1)


@app.command()
def wait(
    agent_ids: list[str],
    timeout: Annotated[float, typer.Option(help="Seconds; exit code 2 on timeout.")] = 3600,
) -> None:
    """Block until the agents are idle, then print their outcome. Exit 1 if any did not succeed."""
    o = orch()
    try:
        finished, views = o.wait(agent_ids, timeout)
    except OrchestratorError as exc:
        raise fail(exc) from None
    for view in views:
        last = view["last_run"] or {}
        typer.secho(f"== {view['id']}: {view['status']}", bold=True)
        typer.echo(last.get("result") or last.get("error") or "")
    if not finished:
        raise typer.Exit(2)
    if any(view["status"] != "done" for view in views):
        raise typer.Exit(1)


@app.command("model")
def model_command(
    agent_id: str,
    model: Annotated[
        str | None, typer.Option(help='The agent\'s own model; "" clears it (inherit).')
    ] = None,
    effort: Annotated[
        str | None, typer.Option(help='The agent\'s own effort; "" clears it (inherit).')
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option(help="Move the agent to this provider (its conversation there: below)."),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Move without asking about the conversation.")
    ] = False,
) -> None:
    """Show, set or clear an agent's own model and effort, or move it to another provider.

    Without options it shows them. A cleared value falls back to the tier default
    (`{cli} config`). A change applies from the agent's next turn; a running one keeps its
    model. In an existing session a model switch starts a fresh prompt cache (the next
    turn re-reads the whole conversation at full price); an effort switch keeps the cache
    with Codex and may start a fresh one with Claude.

    `--provider` moves an idle agent (the orchestrator too) to another available provider. A
    conversation cannot move: the one on the old provider is kept for when the agent moves
    back, and the next message continues the agent's earlier conversation on the new one,
    if it had one there, or starts a new one; the agent's own model and effort are
    cleared; its worktree, branch, board items and sub-agents stay. It asks first when the
    agent has a conversation.
    """
    changes = {
        key: value or None
        for key, value in (("model", model), ("effort", effort))
        if value is not None
    }
    o = orch()
    moved_from = None
    moved: dict = {}
    try:
        if provider:
            before = o.setup(agent_id)
            moved_from = before["provider"] if before["exists"] else None
            try:
                moved = o.set_provider(agent_id, provider, confirm=yes)
            except NeedsConfirmation as exc:
                if not typer.confirm(f"{exc}. Move anyway?"):
                    raise typer.Exit(1) from None
                moved = o.set_provider(agent_id, provider, confirm=True)
        # the newer setup, keeping the note of a move that created the orchestrator
        setup = moved | (o.set_overrides(agent_id, changes) if changes else o.setup(agent_id))
    except OrchestratorError as exc:
        raise fail(exc) from None
    typer.secho(f"{setup['agent_id']}  {setup['provider']}", bold=True)
    for key in ("model", "effort"):
        value = setup["effective"][key] or "default"
        own, inherited = setup[f"{key}_override"], setup["inherited"][key] or "default"
        source = f"own (inherited: {inherited})" if own else "inherited"
        typer.echo(f"  {key:<7} {value:<20} {source}")
    if moved_from and moved_from != setup["provider"]:
        then = next_message(setup["provider"], setup["session_id"])
        typer.echo(f"note: moved from {moved_from} to {setup['provider']}: {then}")
    print_note(setup.get("note"))
    if not changes:
        return
    notes = []
    if setup["running"]:
        notes.append("a turn is running: it keeps its model; the change applies from the next one")
    if setup["session_id"]:
        notes.append(cache_hint(setup["provider"], tuple(sorted(changes, reverse=True))))
    for note in notes:
        typer.echo(f"note: {note}")


@app.command()
def stop(agent_id: str) -> None:
    """Stop an agent's running and queued turns; the agent and its worktree stay."""
    try:
        typer.echo(f"stopped {orch().stop(agent_id)} run(s)")
    except OrchestratorError as exc:
        raise fail(exc) from None


@app.command()
def compact(
    agent_id: str,
    instructions: Annotated[
        str | None, typer.Argument(help="What the summary must keep (Claude's /compact).")
    ] = None,
) -> None:
    """Compact an agent's conversation (queued like a message): Claude Code's own
    `/compact [instructions]` replaces it with a summary in the same session. Codex: a turn
    with a lower auto-compaction limit, without instructions."""
    try:
        run = orch().compact(agent_id, instructions)
    except OrchestratorError as exc:
        raise fail(exc) from None
    typer.echo(f"run {run.id}")


@app.command()
def withdraw(run_id: int) -> None:
    """Withdraw a queued message or compaction (a `#<run id>` of `{cli} status`) before it
    starts."""
    try:
        run = orch().withdraw(run_id)
    except OrchestratorError as exc:
        raise fail(exc) from None
    typer.echo(f"withdrew run {run.id} of {run.agent_id}")


@app.command()
def edit(run_id: int, message: str) -> None:
    """Replace the text of a queued message (a `#<run id>` of `{cli} status`) before it starts."""
    try:
        run = orch().replace_message(run_id, message)
    except OrchestratorError as exc:
        raise fail(exc) from None
    typer.echo(f"replaced the text of run {run.id} of {run.agent_id}")


@app.command()
def archive(
    agent_id: Annotated[str | None, typer.Argument(help="The agent to archive.")] = None,
    safe: Annotated[
        bool,
        typer.Option(
            "--safe", help="Archive every idle agent whose work is safe (of --project, or all)."
        ),
    ] = False,
    project: Annotated[str | None, typer.Option("--project", "-p")] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Archive unmerged work without asking.")
    ] = False,
) -> None:
    """Archive an agent: hidden from `{cli} ps`, the dashboard and list_agents; its record,
    runs, logs, branch and worktree are kept. `{cli} restore` or a message brings it back.

    An agent with commits not merged into the base branch or uncommitted changes is archived
    only after a confirmation; one that (or whose agents) has a queued or running turn not
    at all. `--safe` archives every idle agent with no worktree of its own or a merged, clean
    one (not project leads).
    """
    o = orch()
    if safe == bool(agent_id):
        raise fail("give an agent id, or --safe")
    try:
        if safe:
            result = o.archive_safe(project)
            for archived_id in result["archived"]:
                typer.echo(f"archived {archived_id}")
            for kept_id, reason in result["kept"].items():
                typer.echo(f"kept {kept_id}: {reason}")
            if not result["archived"]:
                typer.echo("nothing to archive")
            return
        try:
            o.archive(agent_id)
        except NeedsConfirmation as exc:
            if not yes and not typer.confirm(f"{exc}. Archive anyway?"):
                raise typer.Exit(1) from None
            o.archive(agent_id, confirm=True)
    except OrchestratorError as exc:
        raise fail(exc) from None
    typer.echo(f"archived {agent_id} ({CLI} restore {agent_id} brings it back)")


@app.command()
def restore(agent_id: str) -> None:
    """Take an agent out of the archive (a message to it does that too)."""
    try:
        orch().restore(agent_id)
    except OrchestratorError as exc:
        raise fail(exc) from None
    typer.echo(f"restored {agent_id}")


@app.command()
def rm(
    agent_id: str,
    delete_branch: Annotated[bool, typer.Option(help="Also delete the agent's branch.")] = False,
) -> None:
    """Delete an archived agent for good, with its worktree (uncommitted work is lost).

    Only archived agents (`{cli} ps --archived`) can be deleted; `{cli} archive` first.
    """
    try:
        orch().remove(agent_id, delete_branch)
    except OrchestratorError as exc:
        raise fail(exc) from None


@app.command()
def attach(agent_id: str) -> None:
    """Take over an agent's conversation interactively in its workspace."""
    o = orch()
    try:
        agent = o.agent(agent_id)
    except OrchestratorError as exc:
        raise fail(exc) from None
    if not agent.session_id:
        raise fail("the agent has no session yet")
    if o.active_runs(agent.id):
        raise fail(f"the agent is busy; `{CLI} stop` it first or wait")
    argv = providers.get(agent.provider).attach_command(agent)
    # PWD too: a CLI may take its directory from it rather than the cwd (opencode does).
    env = {**os.environ, "PATH": locate.search_path(), "PWD": agent.cwd}
    if not shutil.which(argv[0], path=env["PATH"]):
        raise fail(
            f"the `{argv[0]}` CLI is not available: {locate.not_found(argv[0])};"
            f" {locate.says_more()}"
        )
    os.chdir(agent.cwd)
    os.execvpe(argv[0], argv, env)


@app.command("providers")
def list_providers(
    as_json: Annotated[
        bool, typer.Option("--json", help="As JSON, with models and limits.")
    ] = False,
) -> None:
    """Check which coding-agent CLIs (providers) this machine can run, now.

    Each one is looked for on PATH and in the usual install locations, and asked its
    version (with `--json` also its models, as the dashboard lists them), and the login of
    a provider with an offline check (opencode): no agent turn is run. `*` marks the provider a new project gets: the first
    available one. CAPABILITIES: the ones it carries (`{cli} capabilities`). When one is not installed, a line below the table says what to do after
    installing it (not with `--json`)."""
    found = orch().provider_list(refresh=True, models=as_json)
    if as_json:
        typer.echo(json.dumps(found, indent=2))
        return
    width = max(len(entry["name"]) for entry in [*found, {"name": "NAME"}]) + 2
    carried = {entry["name"]: ",".join(entry["capabilities"]) or "-" for entry in found}
    extras = max(len(text) for text in [*carried.values(), "CAPABILITIES"]) + 2
    typer.echo(f"  {'NAME':<{width}}{'STATUS':<15}{'VERSION':<12}{'CAPABILITIES':<{extras}}REASON")
    for entry in found:
        mark = "* " if entry["default"] else "  "
        line = (
            f"{mark}{entry['name']:<{width}}{entry['status']:<15}"
            f"{entry['version'] or '-':<12}{carried[entry['name']]:<{extras}}{entry['reason']}"
        )
        typer.secho(line, fg=None if entry["usable"] else typer.colors.YELLOW)
    if any(entry["status"] == locate.NOT_INSTALLED for entry in found):
        typer.echo(f"\n{locate.install_advice()}")


@app.command("roles")
def list_roles() -> None:
    """List available roles."""
    for role in roles.available():
        flags = ("lead " if role.lead else "") + ("worktree" if role.worktree else "checkout")
        typer.echo(f"{role.name:<13} {flags:<14} {role.description}")


# -- capabilities ---------------------------------------------------------------

capabilities_app = typer.Typer(
    rich_markup_mode=MARKUP,
    help="Capabilities: extras an agent's CLI gets for its turns, such as Claude in Chrome's"
    " browser tools or an MCP server (`mcp:NAME`); on or off globally, per project, per role"
    " and per agent.",
)
app.add_typer(capabilities_app, name="capabilities")
RoleOption = Annotated[
    str | None, typer.Option("--role", "-r", help="For agents of this role (in --project's scope).")
]
AgentOption = Annotated[
    str | None, typer.Option("--agent", "-a", help="For this agent only (its own setting).")
]


def on_off_text(value: bool | None, default: bool | None = None) -> str:
    """`on`, `off`; unset: `inherit`, or the default as `off (default)`."""
    if value is not None:
        return "on" if value else "off"
    return "inherit" if default is None else f"{'on' if default else 'off'} (default)"


def role_lines(entries: list[dict], key: str, scope: str = "") -> list[str]:
    """`role reviewer: chrome off (final)`, per role and capability."""
    lines = []
    for entry in entries:
        for role, on in entry.get(key, {}).items():
            final = "" if on else " (final)"
            lines.append(f"role {role}{scope}: {entry['name']} {on_off_text(on)}{final}")
    return sorted(lines)


def print_capabilities(entries: list[dict], project: str | None) -> None:
    """The built-in capabilities' table, and the MCP servers' in a section of their own."""
    servers = [entry for entry in entries if entry.get("kind") == capabilities.MCP_KIND]
    entries = [entry for entry in entries if entry.get("kind") != capabilities.MCP_KIND]
    columns = ["NAME", "PROVIDERS", "GLOBAL", *(["PROJECT"] if project else []), "STATUS"]
    rows = [
        [
            entry["name"],
            ",".join(entry["providers"]) or "-",
            on_off_text(entry["global"], entry["default"]),
            *([on_off_text(entry["project"])] if project else []),
            entry["status"],
            entry["reason"],
        ]
        for entry in entries
    ]
    widths = [max(len(row[i]) for row in [columns, *rows]) + 2 for i in range(len(columns))]
    for row in [[*columns, "REASON"], *rows]:
        cells = "".join(f"{cell:<{width}}" for cell, width in zip(row, widths, strict=False))
        typer.echo(f"  {cells}{row[-1]}")
    lines = role_lines(entries, "roles")
    if project:
        lines += role_lines(entries, "project_roles", f" ({project})")
    for line in lines:
        typer.echo(line)
    if servers:
        typer.echo(f"\nMCP servers ({CLI} mcp):")
        print_mcp_servers(servers, project, key="name")


@capabilities_app.callback(invoke_without_command=True)
def capabilities_list(
    ctx: typer.Context,
    project: ProjectOption = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="As JSON, with what each one reaches and requires.")
    ] = False,
) -> None:
    """List the capabilities: the providers that support each one, the global setting (with
    `--project` that project's too), whether it can work here, and below the table the
    roles' settings. A role's `off` is final: an agent's own setting cannot turn it back on.

    Turn them on and off with `{cli} capabilities on|off|inherit NAME`."""
    if ctx.invoked_subcommand:
        return
    try:
        # The provider check (versions only) may run first: a new process knows nothing yet.
        entries = orch().capability_list(project, wait=True)
    except OrchestratorError as exc:
        raise fail(exc) from None
    if as_json:
        typer.echo(json.dumps(entries, indent=2))
        return
    print_capabilities(entries, project)


def set_capability(
    state: bool | None, name: str, project: str | None, role: str | None, agent: str | None
) -> None:
    if agent and (project or role):
        raise fail("--agent cannot be combined with --project or --role")
    o = orch()
    try:
        granter = calling_agent(o)
        if agent:
            print_breakdown(o.set_agent_capabilities(agent, {name: state}, granter))
            return
        o.set_capability(name, state, project, role, granter)
        print_capabilities(o.capability_list(project, wait=True), project)
    except OrchestratorError as exc:
        raise fail(exc) from None


@capabilities_app.command("on")
def capabilities_on(
    name: str, project: ProjectOption = None, role: RoleOption = None, agent: AgentOption = None
) -> None:
    """Turn a capability on: globally, for a project, for a role (globally or in a project)
    or for one agent. Applies from each agent's next turn."""
    set_capability(True, name, project, role, agent)


@capabilities_app.command("off")
def capabilities_off(
    name: str, project: ProjectOption = None, role: RoleOption = None, agent: AgentOption = None
) -> None:
    """Turn a capability off at that level (for a role: a restriction that agents' own
    settings cannot lift)."""
    set_capability(False, name, project, role, agent)


@capabilities_app.command("inherit")
def capabilities_inherit(
    name: str, project: ProjectOption = None, role: RoleOption = None, agent: AgentOption = None
) -> None:
    """Remove the setting at that level: the one above applies."""
    set_capability(None, name, project, role, agent)


def calling_agent(o: Orchestrator) -> Agent | None:
    """The agent whose turn runs this command (ORCH_AGENT_ID, as for its MCP tools): it gives
    capabilities as that agent; None for the owner. A turn of another home (its
    TURN_HOME_ENV, e.g. a command tried in a temporary home) is the owner of this one."""
    turn_home = os.environ.get(config.TURN_HOME_ENV)
    if turn_home and Path(turn_home).resolve() != config.home().resolve():
        return None
    return Caller.from_env(o).agent()


def print_breakdown(found: dict) -> None:
    """One line per capability: name, state, the level that decided it, and a note; an MCP
    server's also its status at the agent's last turn."""
    width = max([len(item["name"]) for item in found["items"]] + [4]) + 2
    for item in found["items"]:
        notes = [item["note"]] if item["note"] else []
        if item.get("kind") == capabilities.MCP_KIND and item.get("last_status"):
            notes.append(f"last status: {item['last_status']}")
        note = f"  {'; '.join(notes)}" if notes else ""
        typer.echo(f"{item['name']:<{width}}{item['state']:<9}{item['source']}{note}")


@capabilities_app.command("show")
def capabilities_show(agent_id: str) -> None:
    """What the agent's next turn gets: each capability's state (on, off, or skipped with
    the reason) and the level that decided it."""
    try:
        print_breakdown(orch().agent_capabilities(agent_id))
    except OrchestratorError as exc:
        raise fail(exc) from None


# -- MCP servers ----------------------------------------------------------------

mcp_app = typer.Typer(
    rich_markup_mode=MARKUP,
    help="MCP servers added in Spartito: each one is the capability `mcp:NAME`, off until a"
    " level turns it on, given to Claude agents' turns (docs/mcp.md).",
)
app.add_typer(mcp_app, name="mcp")


def mcp_status(entry: dict) -> str:
    return entry.get("last_status") or "-"


def print_mcp_servers(entries: list[dict], project: str | None, key: str = "server") -> None:
    """The servers' table (`key`: the name column, `server` or the capability `name`) and
    the roles' settings below it."""
    columns = ["NAME", "TRANSPORT", "TARGET", "GLOBAL", *(["PROJECT"] if project else [])]
    rows = [
        [
            entry[key],
            entry["transport"],
            entry["definition"].get("command") or entry["definition"].get("url") or "-",
            on_off_text(entry["global"], entry["default"]),
            *([on_off_text(entry["project"])] if project else []),
            mcp_status(entry),
        ]
        for entry in entries
    ]
    widths = [max(len(row[i]) for row in [columns, *rows]) + 2 for i in range(len(columns))]
    for row in [[*columns, "LAST STATUS"], *rows]:
        cells = "".join(f"{cell:<{width}}" for cell, width in zip(row, widths, strict=False))
        typer.echo(f"  {cells}{row[-1]}")
    lines = role_lines(entries, "roles")
    if project:
        lines += role_lines(entries, "project_roles", f" ({project})")
    for line in lines:
        typer.echo(line)


NO_SERVERS = (
    f"no MCP servers added; add one with `{CLI} mcp add NAME --command CMD` (a local command)"
    f" or `{CLI} mcp add NAME --url URL` (a remote server)"
)


@mcp_app.callback(invoke_without_command=True)
def mcp_list(
    ctx: typer.Context,
    project: ProjectOption = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="As JSON, with the definition, reach and agents.")
    ] = False,
) -> None:
    """List the MCP servers added in Spartito: transport, the command or URL origin, the
    global setting (with `--project` that project's too) and the status the latest turn that
    had it reported; below the table the roles' settings.

    Add one with `{cli} mcp add`, turn it on with `{cli} mcp on NAME`."""
    if ctx.invoked_subcommand:
        return
    try:
        entries = orch().mcp_list(project)
    except OrchestratorError as exc:
        raise fail(exc) from None
    if as_json:
        typer.echo(json.dumps(entries, indent=2))
    elif not entries:
        typer.echo(NO_SERVERS)
    else:
        print_mcp_servers(entries, project)


def definition_lines(entry: dict) -> list[tuple[str, str]]:
    shown = entry.get("definition") or {}
    if entry.get("transport") == mcp_servers.STDIO:
        return [
            ("command", shown["command"]),
            ("args", " ".join(shown["args"]) or "none"),
            ("env", ", ".join(f"{k}={v}" for k, v in shown["env"].items()) or "none"),
        ]
    headers = ", ".join(f"{k}: {v}" for k, v in shown.get("headers", {}).items())
    return [("url", f"{shown.get('url')} (origin only)"), ("headers", headers or "none")]


@mcp_app.command("show")
def mcp_show(
    name: str,
    project: ProjectOption = None,
    as_json: Annotated[bool, typer.Option("--json", help="As JSON.")] = False,
) -> None:
    """One MCP server: its definition (a URL as its origin only), what it reaches, its
    levels, the agents with their own setting for it and its last status."""
    try:
        entry = orch().mcp_show(name, project)
    except OrchestratorError as exc:
        raise fail(exc) from None
    if as_json:
        typer.echo(json.dumps(entry, indent=2))
        return
    lines = [
        ("name", f"{entry['server']}  (capability {entry['name']})"),
        ("transport", entry["transport"]),
        *definition_lines(entry),
        ("reaches", entry["reaches"]),
        ("global", on_off_text(entry["global"], entry["default"])),
        *([("project", on_off_text(entry["project"]))] if project else []),
        ("last status", mcp_status(entry)),
    ]
    roles = [(role, on, "") for role, on in entry["roles"].items()]
    roles += [(role, on, f" ({project})") for role, on in entry.get("project_roles", {}).items()]
    roles_text = [
        f"{role}{where} {on_off_text(on)}{'' if on else ' (final)'}" for role, on, where in roles
    ]
    lines.append(("roles", ", ".join(roles_text) or "none"))
    agents = [f"{agent} {on_off_text(on)}" for agent, on in entry.get("agents", {}).items()]
    lines.append(("agents", ", ".join(agents) or "none"))
    for label, value in lines:
        typer.echo(f"{label:<12} {value}")


def not_in_chat(action: str) -> None:
    """Adding and removing MCP servers change what code runs and with whose credentials: the
    owner's alone, so not a command the `chat` session's model runs (CHAT_ENV)."""
    if os.environ.get(CHAT_ENV):
        raise fail(
            f"`{CLI} mcp {action}` is refused in a `{CLI} chat` session: adding and removing"
            " MCP servers is the owner's alone; run the command in your own shell"
        )


@mcp_app.command("add")
def mcp_add(
    name: Annotated[str, typer.Argument(help="Letters, digits, '_' and '-'.")],
    command: Annotated[
        str | None, typer.Option("--command", help="A local server (stdio): the command it runs.")
    ] = None,
    args: Annotated[
        list[str] | None,
        typer.Option(
            "--arg",
            help="An argument of --command; repeatable, in order. One starting with '-' works"
            " too: --arg --read-only.",
        ),
    ] = None,
    env: Annotated[
        list[str] | None,
        typer.Option("--env", help="NAME=${VAR} for --command; repeatable. No literal values."),
    ] = None,
    url: Annotated[
        str | None, typer.Option("--url", help="A remote server (HTTP): its http(s) URL.")
    ] = None,
    headers: Annotated[
        list[str] | None,
        typer.Option("--header", help="'Name: ${VAR}' for --url; repeatable. No literal values."),
    ] = None,
    reaches: Annotated[
        str | None,
        typer.Option("--reaches", help="Your one line on what it reaches, shown after Spartito's."),
    ] = None,
) -> None:
    """Add an MCP server: a command (stdio) or a URL (HTTP). Env and header values are
    `${VAR}` references, read from the environment of the process that runs the turns;
    Spartito stores no secret. It is off until you turn it on. Only the owner adds servers:
    refused in an agent's turn."""
    not_in_chat("add")
    o = orch()
    try:
        entry = o.mcp_add(
            name, command=command, args=args or (), env=env, url=url, headers=headers,
            reaches=reaches, granter=calling_agent(o),
        )  # fmt: skip
    except OrchestratorError as exc:
        raise fail(exc) from None
    typer.echo(f"added {entry['server']} ({entry['transport']}): {entry['reaches']}")
    typer.echo(
        f"it is off until a level turns it on: `{CLI} mcp on {entry['server']}`"
        " (globally; --project P, --role R or --agent A for less)"
    )


@mcp_app.command("remove")
def mcp_remove(name: str) -> None:
    """Remove an MCP server, and its setting at every level (global, projects, roles,
    agents). Only the owner removes servers: refused in an agent's turn."""
    not_in_chat("remove")
    o = orch()
    try:
        entry = o.mcp_remove(name, calling_agent(o))
    except OrchestratorError as exc:
        raise fail(exc) from None
    cleared = ", ".join(entry["cleared"])
    where = f"its settings were removed from: {cleared}" if cleared else "it had no setting"
    typer.echo(f"removed {entry['server']}; {where}")


@mcp_app.command("on")
def mcp_on(
    name: str, project: ProjectOption = None, role: RoleOption = None, agent: AgentOption = None
) -> None:
    """Turn an MCP server on: globally, for a project, for a role (globally or in a project)
    or for one agent. The same as `{cli} capabilities on mcp:NAME`."""
    set_capability(True, mcp_servers.capability_name(name), project, role, agent)


@mcp_app.command("off")
def mcp_off(
    name: str, project: ProjectOption = None, role: RoleOption = None, agent: AgentOption = None
) -> None:
    """Turn an MCP server off at that level (for a role: final). The same as
    `{cli} capabilities off mcp:NAME`."""
    set_capability(False, mcp_servers.capability_name(name), project, role, agent)


@mcp_app.command("inherit")
def mcp_inherit(
    name: str, project: ProjectOption = None, role: RoleOption = None, agent: AgentOption = None
) -> None:
    """Remove the MCP server's setting at that level: the one above applies. The same as
    `{cli} capabilities inherit mcp:NAME`."""
    set_capability(None, mcp_servers.capability_name(name), project, role, agent)


@mcp_app.command("serve")
def mcp_serve() -> None:
    """Run Spartito's own MCP server, the one with the agents' tools, on stdio (lead agents
    and `{cli} chat` start it themselves; there is no need to)."""
    from .mcp_server import main

    main()


# -- cost reports and budgets -------------------------------------------------


def money(value: float | None) -> str:
    """`$1.23`; `n/a` when no cost was reported (Codex reports none)."""
    return "n/a" if value is None else f"${value:,.2f}"


def cost_cell(row: dict) -> str:
    """The cost of a report row, with `*` when some of its turns reported no cost."""
    return money(row["cost_usd"]) + ("*" if row["unpriced"] and row["cost_usd"] is not None else "")


def prompt_tokens(counts: dict) -> int:
    return counts["in"] + counts["cache_read"] + counts["cache_write"]


@app.command()
def cost(
    by: Annotated[
        str,
        typer.Option(
            "--by", help="Group by project (default), day (local time), agent, model or role."
        ),
    ] = "project",
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only from this local day on (YYYY-MM-DD)."),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option("--until", help="Only up to this local day, included (YYYY-MM-DD)."),
    ] = None,
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Only this project's agents.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="As JSON.")] = False,
) -> None:
    """Report the spend of the recorded runs per project, day, agent, model or role, with a
    total.

    Recorded usage only: turns run by very old versions may have no record. Codex reports no
    cost: its turns and tokens are listed, its cost is `n/a`. What agents deleted for good had
    spent is listed in rows of its own; it has no dates, so `--since` and `--until` leave it
    out. Budgets: `{cli} budget`."""
    try:
        data = orch().cost_report(by, since, project, until)
    except OrchestratorError as exc:
        raise fail(exc) from None
    if as_json:
        typer.echo(json.dumps(data, indent=2))
        return
    scope = f" of {project}" if project else ""
    if data["since"] or data["until"]:
        when = f"from {data['since'] or 'the start'} to {data['until'] or 'today'}"
    else:
        when = "all time"
    typer.echo(f"recorded usage{scope} by {by}, {when}")
    heading = by.upper()
    labels = [row["label"] for row in data["rows"]] + ["TOTAL"]
    width = max(len(label) for label in [*labels, heading]) + 2
    typer.echo(f"{heading:<{width}}{'TURNS':>7}{'PROMPT':>10}{'OUTPUT':>10}{'COST':>12}")
    total = {**data["total"], "label": "TOTAL"}
    for row in [*data["rows"], total]:
        turns = "-" if row["turns"] is None else str(row["turns"])
        line = (
            f"{row['label']:<{width}}{turns:>7}{count(prompt_tokens(row['tokens'])):>10}"
            f"{count(row['tokens']['out']):>10}{cost_cell(row):>12}"
        )
        typer.secho(line, bold=row is total)
    if data["unpriced"]:
        typer.echo(
            f"\n* {data['unpriced']} turn(s) reported no cost (Codex reports none): their tokens"
            " are counted, their cost is not in the figures; n/a: no turn of the row reported one."
        )
    if any(row["removed"] for row in data["rows"]):
        typer.echo("Removed agents: what agents deleted for good had spent; turns not kept.")
    if data["removed_left_out"]:
        typer.echo(
            "What removed agents had spent has no dates and is left out with --since or --until."
        )


budget_app = typer.Typer(
    rich_markup_mode=MARKUP,
    help="Budgets: one global and one per project, each warning at a threshold; the hard stop"
    " that refuses new turns is opt-in.",
)
app.add_typer(budget_app, name="budget")


def show_budgets() -> None:
    found = orch().budget_list()
    if not found:
        typer.echo(f"no budgets; `{CLI} budget set AMOUNT [-p PROJECT]` sets one")
        return
    typer.echo(
        f"{'SCOPE':<20} {'PERIOD':<7} {'SPENT':>10} {'AMOUNT':>10} {'USED':>6}"
        f" {'WARN AT':>8} {'HARD STOP':>10}  STATE"
    )
    colors = {"warning": typer.colors.YELLOW, "reached": typer.colors.RED}
    for item in found:
        percent = "-" if item["percent"] is None else f"{item['percent']}%"
        state = typer.style(item["state"], fg=colors.get(item["state"]))
        typer.echo(
            f"{item['project'] or 'global':<20} {item['period']:<7} {money(item['spent_usd']):>10}"
            f" {money(item['amount_usd']):>10} {percent:>6} {str(item['warn_at']) + '%':>8}"
            f" {'on' if item['hard_stop'] else 'off':>10}  {state}"
        )
    typer.echo(
        "Reported cost only (Codex reports none); day and month in local time; 'total' also"
        " counts removed agents. Checked after each turn, so a budget can be overshot."
    )


@budget_app.callback(invoke_without_command=True)
def budget_main(ctx: typer.Context) -> None:
    """List the budgets with what is spent in their current period (`{cli} budget list`)."""
    if not ctx.invoked_subcommand:
        show_budgets()


@budget_app.command("list")
def budget_list() -> None:
    """List the budgets: each with what is spent in its current period, the percentage of
    its amount, and its state (ok, warning, reached)."""
    show_budgets()


@budget_app.command("set")
def budget_set(
    amount: Annotated[
        float | None,
        typer.Argument(help="The amount in USD (may be left out to change an existing budget)."),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="For this project instead of the global budget."),
    ] = None,
    period: Annotated[
        str | None,
        typer.Option("--period", help="day, month (a new budget's default) or total; local time."),
    ] = None,
    warn_at: Annotated[
        int | None,
        typer.Option("--warn-at", help="Warn at this percentage of the amount (default 80)."),
    ] = None,
    hard_stop: Annotated[
        bool | None,
        typer.Option(
            "--hard-stop/--no-hard-stop",
            help="Refuse new turns once the amount is reached (default: off).",
        ),
    ] = None,
) -> None:
    """Set the global budget, or a project's with `-p`. A value not given keeps what the budget
    had. The owner is warned once per period at the threshold and once at the amount. With
    `--hard-stop`, new message turns are refused once the amount is reached, until it is raised
    (`{cli} budget set AMOUNT`) or removed (`{cli} budget remove`)."""
    try:
        item = orch().set_budget(amount, project, period, warn_at, hard_stop)
    except OrchestratorError as exc:
        raise fail(exc) from None
    stop = ", hard stop" if item["hard_stop"] else ""
    per = "in total" if item["period"] == "total" else f"per {item['period']}"
    typer.echo(
        f"{item['project'] or 'global'} budget: {money(item['amount_usd'])} {per},"
        f" warning at {item['warn_at']}%{stop}; spent {money(item['spent_usd'])}"
        f" ({item['state']})"
    )


@budget_app.command("remove")
def budget_remove(
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="The project's budget instead of the global one."),
    ] = None,
) -> None:
    """Remove the global budget, or a project's with `-p`."""
    try:
        orch().remove_budget(project)
    except OrchestratorError as exc:
        raise fail(exc) from None
    typer.echo(f"removed the {project or 'global'} budget")


# -- state: backup, list, restore -------------------------------------------

state_app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode=MARKUP,
    help="Back up the state (the database, your roles and loadout.toml, optionally the logs),"
    " list the backups, restore one. Moving a home: docs/state.md.",
)
app.add_typer(state_app, name="state")


def size_text(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""  # not reached


@state_app.command("backup")
def state_backup(
    to: Annotated[
        str | None,
        typer.Option(
            "--to", help="The new directory to write it to (default: one in the home's backups/)."
        ),
    ] = None,
    logs: Annotated[bool, typer.Option("--logs", help="Include the logs/ directory.")] = False,
) -> None:
    """Back up the state, also while agents run: the database (through SQLite's online backup
    API, then checked with `PRAGMA integrity_check`), the home's roles/ and loadout.toml, and
    with `--logs` the logs. Nothing outside the home: no worktrees, no provider data.
    `{cli} state list` lists them, `{cli} state restore` restores one."""
    try:
        result = orch().backup_state(to, logs)
    except OrchestratorError as exc:
        raise fail(exc) from None
    parts = ["database", *result["files"]]
    typer.echo(f"backup: {result['path']}")
    typer.echo(f"  {', '.join(parts)}; {size_text(result['size'])}")
    for path in result["skipped"]:
        typer.echo(f"  left out (a symbolic link): {path}")
    typer.secho(f"warning: {result['warning']}", fg=typer.colors.YELLOW, err=True)


@state_app.command("list")
def state_list(as_json: Annotated[bool, typer.Option("--json", help="As JSON.")] = False) -> None:
    """List the backups in the home's backups/, newest first: the directories `{cli} state
    backup` made and database files (`*.db`) put there by hand."""
    found = orch().list_backups()
    if as_json:
        typer.echo(json.dumps(found, indent=2))
        return
    if not found:
        typer.echo("no backups")
    for item in found:
        when = item["time"].replace("T", " ").replace("+00:00", " UTC")
        logs = "logs" if item["logs"] else ""
        typer.echo(f"{item['name']:<34} {when}  {size_text(item['size']):>9}  {logs}".rstrip())


@state_app.command("restore")
def state_restore(
    backup: Annotated[
        str,
        typer.Argument(
            help="A name from `state list`, or the path of a backup directory or database file."
        ),
    ],
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Restore; without it, only say what it would do.")
    ] = False,
    logs: Annotated[
        bool, typer.Option("--logs", help="Also restore the backup's logs/, if it has them.")
    ] = False,
) -> None:
    """Restore a backup into this home. Refuses while any turn is running or queued, and a file
    that is not an intact state database. Without `--yes` it only says what it would do. The
    state it replaces is first kept as a new backup, so nothing is lost; the database is
    replaced in place, so a running dashboard keeps working, but restart it."""

    def kept(path: str, name: str) -> None:
        """Printed before the restore commits: the replaced state is findable whatever follows."""
        typer.echo(
            f"the replaced state is kept in {path}"
            f" (`{CLI} state restore {name} --yes` brings it back)"
        )

    try:
        result = orch().restore_state(backup, confirm=yes, logs=logs, announce=kept)
    except NeedsConfirmation as exc:
        typer.echo(str(exc))
        typer.secho(
            f"nothing changed; run `{CLI} state restore {backup} --yes` to restore", err=True
        )
        raise typer.Exit(1) from None
    except OrchestratorError as exc:
        raise fail(exc) from None
    typer.echo(f"restored {result['restored']}")
    if result["files"]:
        typer.echo(f"  and the backup's {', '.join(result['files'])}")
    for path in result["kept_skipped"]:
        typer.echo(f"  not in the kept backup (a symbolic link): {path}")
    for entry in result["left"]:
        typer.echo(f"  not restored: {entry}")
    if result["newer"]:
        typer.secho(f"note: {result['newer']}", fg=typer.colors.YELLOW, err=True)
    typer.secho(result["restart"], fg=typer.colors.YELLOW, err=True)


# -- version and upgrades -----------------------------------------------------


@app.command("version")
def version_command(
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Ask PyPI whether a newer release exists (the only network request, and only"
            " with this option).",
        ),
    ] = False,
) -> None:
    """Print the version and the kind of install: an editable checkout (with its directory) or
    an installed package. With `--check`, ask PyPI's JSON API whether a newer release exists;
    {cli} never does this on its own."""
    o = Orchestrator  # static methods: no database is opened
    info = o.version_info()
    typer.echo(f"{CLI} {info['version']}")
    where = f" ({info['checkout']})" if info["kind"] == "editable" and info["checkout"] else ""
    how = " (uv tool)" if info["kind"] == "package" and info["tool"] else ""
    typer.echo(f"install: {info['label']}{where}{how}")
    if not check:
        return
    try:
        result = o.check_release()
    except OrchestratorError as exc:
        raise fail(exc) from None
    installed, latest = result["installed"], result["latest"]
    typer.echo(
        {
            "newer": f"a newer release is on PyPI: {latest} (installed: {installed});"
            f" `{CLI} upgrade` prepares the upgrade",
            "current": f"up to date: {installed} is the latest release on PyPI",
            "ahead": f"{installed} is newer than the latest release on PyPI ({latest})",
            "unknown": f"the latest release on PyPI is {latest}; the version of this install"
            f" is not known ({installed})",
            "unpublished": f"{result['name']} is not published on PyPI (installed: {installed})",
        }[result["status"]]
    )


@app.command()
def upgrade(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Only print the plan: no backup, and active turns listed."),
    ] = False,
) -> None:
    """Prepare an upgrade; it runs no package manager and no git. Refuses while any turn is
    queued or running, backs the state up (as `{cli} state backup`), then prints the kind of
    install, the command that upgrades it and what must be restarted afterwards."""
    from .gui import launcher

    try:
        launcher_there = launcher.installed(launcher.launcher())
    except launcher.LauncherError:  # not on this platform
        launcher_there = False
    try:
        plan = orch().prepare_upgrade(launcher_there, dry_run)
    except OrchestratorError as exc:
        raise fail(exc) from None
    for line in plan["busy"]:
        typer.secho(f"active: {line} (without --dry-run this refuses)", fg=typer.colors.YELLOW)
    backup = plan["backup"]
    if backup:
        typer.echo(f"backup: {backup['path']}")
        typer.echo(f"  to go back: `{CLI} state restore {backup['name']} --yes`")
        typer.secho(f"warning: {backup['warning']}", fg=typer.colors.YELLOW, err=True)
    else:
        typer.echo(f"backup: none (dry run); without --dry-run one goes into {plan['backups_dir']}")
    info = plan["install"]
    where = f" ({info['checkout']})" if info["kind"] == "editable" and info["checkout"] else ""
    typer.echo(f"install: {CLI} {plan['version']}, {info['label']}{where}")
    if plan["commands"]:
        typer.echo("upgrade it with:")
        for line in plan["commands"]:
            typer.echo(f"  {line}")
    for note in plan["notes"]:
        typer.echo(f"  note: {note}")
    if plan["restart"]:
        typer.echo("then restart:")
        for item in plan["restart"]:
            typer.echo(f"  - {item}")
    else:
        typer.echo("then restart: nothing of this home runs that needs it")
    typer.echo("not visible from here, if you run them:")
    for item in plan["unseen"]:
        typer.echo(f"  - {item}")
    typer.echo(f"check the new version with `{CLI} version`.")


def name_the_command(group: typer.Typer) -> None:
    """Put CLI where the docstrings of `group`'s commands and callbacks say `{cli}`."""
    for info in [*group.registered_commands, group.registered_callback]:
        callback = info and info.callback
        if callback and callback.__doc__:
            callback.__doc__ = callback.__doc__.replace("{cli}", CLI)
    for sub in group.registered_groups:
        if sub.typer_instance:
            name_the_command(sub.typer_instance)


name_the_command(app)

OLD_NAME = "orch"  # the command's name before CLI_NAME; an older install's script keeps it


def main() -> None:
    """The command line under CLI_NAME, whatever the script that started it is called. An
    editable install from before the rename still has its `orch` script, running this code
    until that install is replaced (docs/usage.md): it works, and says so on every call."""
    if os.path.basename(sys.argv[0]) == OLD_NAME:
        typer.secho(
            f"warning: `{OLD_NAME}` is now `{config.CLI_NAME}`; this script is left from an"
            " older install and goes away when that install is replaced"
            " (docs/usage.md#upgrading-from-orch)",
            fg=typer.colors.YELLOW,
            err=True,
        )
    app(prog_name=config.CLI_NAME)
