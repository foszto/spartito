"""`orch` command line."""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import replace
from typing import Annotated

import typer

from . import config, loadout, providers, roles
from .models import Agent
from .providers.claude import loadout_flags
from .service import (
    ORCHESTRATOR_ID,
    NeedsConfirmation,
    Orchestrator,
    OrchestratorError,
    cache_hint,
)
from .settings import ARCHIVE_AFTER, ARCHIVE_AFTER_DEFAULT, KEEP_AWAKE, TIERS

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
project_app = typer.Typer(no_args_is_help=True, help="Manage projects (git repositories).")
app.add_typer(project_app, name="project")
config_app = typer.Typer(no_args_is_help=True, help="Default model and effort per tier.")
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


def context_styled(context: dict | None, width: int = 0) -> str:
    text = f"{context_text(context):<{width}}"
    if context and (context["percent"] or 0) >= CONTEXT_WARN:
        return typer.style(
            text, fg=typer.colors.RED if context["percent"] >= 95 else typer.colors.YELLOW
        )
    return text


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


def one_line(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def mcp_config() -> str:
    import sys

    servers = {
        "orch": {
            "command": sys.executable,
            "args": ["-m", "orchestrator.mcp_server"],
            "env": {"ORCH_HOME": str(config.home())},
        }
    }
    return json.dumps({"mcpServers": servers})


# -- orchestrator session ---------------------------------------------------


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
    if not shutil.which("claude"):
        raise fail("the `claude` CLI is not on PATH")
    tier = orch().settings().get("orchestrator", {}).get("claude", {})
    model = model or tier.get("model")
    effort = effort or tier.get("effort")
    if effort and effort not in providers.get("claude").efforts:
        allowed = ", ".join(providers.get("claude").efforts)
        raise fail(f"claude has no effort '{effort}' (use: {allowed})")
    role = roles.load("orchestrator")
    try:
        # No project: only $ORCH_HOME/loadout.toml applies.
        chosen = loadout.resolve(
            role, warn=lambda text: typer.secho(f"warning: {text}", fg="yellow", err=True)
        )
    except loadout.LoadoutError as exc:
        raise fail(str(exc)) from None
    if chosen.tools is not None and "Bash" not in chosen.tools:
        chosen = replace(chosen, tools=(*chosen.tools, "Bash"))  # for `orch wait` and co.
    # A person works in this session: --disable-slash-commands would also take away the
    # built-in /compact, /clear, /model and /context. Headless agents keep the setting.
    chosen = replace(chosen, slash_commands=True)
    argv = [
        "claude",
        "--mcp-config", mcp_config(),
        "--append-system-prompt", role.prompt,
        "--allowedTools", "mcp__orch", "Bash(orch *)",
        *loadout_flags(chosen),
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
    os.execvp("claude", argv)


@app.command()
def mcp() -> None:
    """Run the orchestrator MCP server on stdio (what `orch chat` and lead agents connect to)."""
    from .mcp_server import main

    main()


@app.command()
def web(host: str = "127.0.0.1", port: int = 8787) -> None:
    """Serve the web UI: add projects, start agents and talk to them."""
    import uvicorn

    from .web.app import LOOPBACK_HOSTS
    from .web.app import app as web_app

    loopback = host in LOOPBACK_HOSTS
    web_app.state.allowed_hosts = LOOPBACK_HOSTS if loopback else None
    if not loopback:
        typer.secho(
            "warning: the web UI has no authentication and can start agents that run code; "
            "anyone who can reach this address controls them.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    typer.echo(f"web UI: http://{host}:{port}")
    uvicorn.run(web_app, host=host, port=port, log_level="warning")


# -- projects ---------------------------------------------------------------


@project_app.command("add")
def project_add(
    path: Annotated[str, typer.Argument(help="A directory inside the git repository.")] = ".",
    name: Annotated[
        str | None, typer.Option(help="Defaults to the repository directory name.")
    ] = None,
    provider: Annotated[
        str, typer.Option(help="Default agent provider for the project.")
    ] = "claude",
) -> None:
    """Register a git repository as a project."""
    try:
        o = orch()
        project = o.add_project(path, name, provider)
    except (OrchestratorError, KeyError) as exc:
        raise fail(exc) from None
    typer.echo(f"added {project.name}  {project.path}  (base branch: {project.base_branch})")
    if note := o.project_note(project):
        typer.secho(f"note: {note}", fg=typer.colors.YELLOW)


@project_app.command("list")
def project_list() -> None:
    """List projects."""
    o = orch()
    for project in o.store.list_projects():
        count = len(o.store.list_agents(project.name)) - len(o.store.archived(project.name))
        typer.echo(
            f"{project.name:<20} {project.default_provider:<7} {count:>3} agents  {project.path}"
        )


@project_app.command("rm")
def project_rm(name: str) -> None:
    """Unregister a project (it must have no agents). The repository is not touched."""
    try:
        orch().remove_project(name)
    except OrchestratorError as exc:
        raise fail(exc) from None


# -- settings ---------------------------------------------------------------


@config_app.command("show")
def config_show(project: Annotated[str | None, typer.Option("--project", "-p")] = None) -> None:
    """Show model/effort settings: global, or a project's overrides."""
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
        str | None, typer.Option(help="Claude permission mode / Codex sandbox mode.")
    ] = None,
) -> None:
    """Start a new agent on a task and return immediately."""
    try:
        agent, run = orch().spawn(
            project, task, name=name, role=role, provider=provider, model=model, effort=effort,
            worktree=worktree, share_with=share, base=base, after=after, permission=permission,
        )  # fmt: skip
    except OrchestratorError as exc:
        raise fail(exc) from None
    typer.echo(f"{agent.id}  run {run.id}  {agent.cwd}")


@app.command()
def ask(project: str, message: str) -> None:
    """Send a message to a project's lead agent (created on first use)."""
    o = orch()
    try:
        lead = o.lead(project)
        run = o.send(lead.id, message)
    except OrchestratorError as exc:
        raise fail(exc) from None
    typer.echo(f"{lead.id}  run {run.id}")


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
    agents = {agent.id: agent for agent in o.store.list_agents(project)}
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
            setup_text = setup(view, agents.get(view["id"]) or o.store.get_agent(view["id"]))
            context = context_text(view["context"]) and context_styled(view["context"])
            extra = "  ".join(filter(None, (setup_text, context, tokens(view["tokens"]), cost)))
            label = shown_name(view["id"])
            typer.secho(f"{label}  {status}  {extra}", bold=True)
    for item in o.store.list_projects():
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
    typer.echo("orch restore <id> brings one back; orch rm <id> deletes it for good")


@app.command()
def status(agent_id: str) -> None:
    """Show one agent: state, last result and the changes in its worktree."""
    o = orch()
    try:
        o.store.reconcile()
        view = o.view(o.agent(agent_id), detail=True)
        changes = o.changes(agent_id)
    except OrchestratorError as exc:
        raise fail(exc) from None
    last = view.pop("last_run")
    counts, cost = view.pop("tokens"), view.pop("cost_usd")
    context, refusal = view.pop("context"), view.pop("compact_refusal")
    compaction = view.pop("last_compaction")
    queue, working = view.pop("queue"), view.pop("children_running")
    if view["id"] == ORCHESTRATOR_ID:
        view["name"] = config.ORCHESTRATOR_NAME  # the id line keeps `orchestrator`
    if mark := view.pop("archived", None):
        view["archived"] = f"{mark['archived_at']} ({mark['how']}; orch restore {agent_id})"
    for key in ("model", "effort"):
        override = view.pop(f"{key}_override")
        view[key] = f"{view[key] or 'default'}" + (" (set on the agent)" if override else "")
    if idle_with_children({**view, "children_running": working}):
        last_run = "" if view["status"] == "new" else f" (last run: {view['status']})"
        view["status"] = f"idle while its agents work{last_run}"
    for key, value in view.items():
        typer.echo(f"{key:<12} {value}")
    plural = "agent" if working == 1 else "agents"
    typer.echo(f"{'children':<12} {f'{working} {plural} working' if working else 'none working'}")
    typer.echo(f"{'usage':<12} {usage_line(counts, cost)}")
    hint = f"orch compact {agent_id}" if refusal is None else f"no compact: {refusal}"
    if context and context["compacted"]:
        typer.echo(f"{'context':<12} compacted; its size is known after the next turn")
    elif context:
        typer.echo(f"{'context':<12} {context_styled(context)}  ({hint})")
    else:
        typer.echo(f"{'context':<12} unknown  ({hint})")
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
        for event in o.store.events(agent_id, after=last_id, tail=tail if first else None):
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
) -> None:
    """Show, set or clear an agent's own model and effort.

    Without options it shows them. A cleared value falls back to the tier default
    (`orch config`). A change applies from the agent's next turn; a running one keeps its
    model. In an existing session a model switch starts a fresh prompt cache (the next
    turn re-reads the whole conversation at full price); an effort switch keeps the cache
    with Codex and may start a fresh one with Claude.
    """
    changes = {
        key: value or None
        for key, value in (("model", model), ("effort", effort))
        if value is not None
    }
    o = orch()
    try:
        setup = o.set_overrides(agent_id, changes) if changes else o.setup(agent_id)
    except OrchestratorError as exc:
        raise fail(exc) from None
    typer.secho(f"{setup['agent_id']}  {setup['provider']}", bold=True)
    for key in ("model", "effort"):
        value = setup["effective"][key] or "default"
        own, inherited = setup[f"{key}_override"], setup["inherited"][key] or "default"
        source = f"own (inherited: {inherited})" if own else "inherited"
        typer.echo(f"  {key:<7} {value:<20} {source}")
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
    """Withdraw a queued message or compaction (a `#<run id>` of `orch status`) before it
    starts."""
    try:
        run = orch().withdraw(run_id)
    except OrchestratorError as exc:
        raise fail(exc) from None
    typer.echo(f"withdrew run {run.id} of {run.agent_id}")


@app.command()
def edit(run_id: int, message: str) -> None:
    """Replace the text of a queued message (a `#<run id>` of `orch status`) before it starts."""
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
    """Archive an agent: hidden from `orch ps`, the dashboard and list_agents; its record,
    runs, logs, branch and worktree are kept. `orch restore` or a message brings it back.

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
    typer.echo(f"archived {agent_id} (orch restore {agent_id} brings it back)")


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

    Only archived agents (`orch ps --archived`) can be deleted; `orch archive` first.
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
    if o.store.active_runs(agent.id):
        raise fail("the agent is busy; `orch stop` it first or wait")
    argv = providers.get(agent.provider).attach_command(agent)
    os.chdir(agent.cwd)
    os.execvp(argv[0], argv)


@app.command("roles")
def list_roles() -> None:
    """List available roles."""
    for role in roles.available():
        flags = ("lead " if role.lead else "") + ("worktree" if role.worktree else "checkout")
        typer.echo(f"{role.name:<13} {flags:<14} {role.description}")


def main() -> None:
    app()
