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
from .providers.claude import loadout_flags
from .service import Orchestrator, OrchestratorError
from .settings import TIERS

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
}


def orch() -> Orchestrator:
    return Orchestrator()


def fail(message: object) -> typer.Exit:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(1)


def count(n: int) -> str:
    return str(n) if n < 1000 else f"{n / 1000:.1f}k" if n < 1_000_000 else f"{n / 1e6:.1f}M"


def tokens(usage: dict | None) -> str:
    """`<prompt>/<output> tok`, the prompt counting cache reads and writes."""
    if not usage or usage.get("input_tokens") is None:
        return ""
    prompt = sum(
        usage[key] or 0 for key in ("input_tokens", "cache_read_tokens", "cache_creation_tokens")
    )
    return f"{count(prompt)}/{count(usage['output_tokens'] or 0)} tok"


def usage_line(usage: dict | None) -> str:
    if not usage:
        return "-"
    parts = []
    if usage.get("input_tokens") is not None:
        parts += [
            f"in {count(usage['input_tokens'])}",
            f"cache read {count(usage['cache_read_tokens'] or 0)}",
            f"cache write {count(usage['cache_creation_tokens'] or 0)}",
            f"out {count(usage['output_tokens'] or 0)}",
        ]
    if usage.get("cost_usd"):
        parts.append(f"${usage['cost_usd']:.4f}")
    if usage.get("model"):
        parts.append(usage["model"])
    return " · ".join(parts) or "-"


def setup(view: dict) -> str:
    """Effective `model/effort`; `*` marks a value set explicitly on the agent."""
    model = (view["model"] or "default") + ("*" if view["model_override"] else "")
    effort = view["effort"] and view["effort"] + ("*" if view["effort_override"] else "")
    return f"{model}/{effort}" if effort else model


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
        argv += ["--name", "orchestrator"]
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
        count = len(o.store.list_agents(project.name))
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
    if not data:
        typer.echo(
            "  (none: agents use the CLI defaults)" if not project else "  (none: inherits global)"
        )


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
def ps(project: Annotated[str | None, typer.Option("--project", "-p")] = None) -> None:
    """List agents as a tree: project -> agent -> sub-agents."""
    o = orch()
    views = o.overview(project)
    children: dict[str | None, list[dict]] = {}
    home = {view["id"]: view["project"] for view in views}
    for view in views:
        same_project = home.get(view["parent_id"]) == view["project"]
        parent = view["parent_id"] if same_project else None
        children.setdefault((view["project"], parent), []).append(view)

    def show(project_name: str, parent: str | None, depth: int) -> None:
        for view in children.get((project_name, parent), []):
            status = typer.style(f"{view['status']:<8}", fg=STATUS_COLORS.get(view["status"]))
            label = "  " * depth + view["id"]
            cost = f"${view['cost_usd']:.2f}" if view["cost_usd"] else ""
            typer.echo(
                f"  {label:<34} {status} {view['role']:<9} {view['provider']:<7}"
                f" {setup(view):<18} {tokens(view['usage']):<15} {cost:<7} {view['branch'] or ''}"
            )
            show(project_name, view["id"], depth + 1)

    for view in views:
        if view["project"] is None and not project:
            status = typer.style(view["status"], fg=STATUS_COLORS.get(view["status"]))
            cost = f"${view['cost_usd']:.2f}" if view["cost_usd"] else ""
            extra = "  ".join(filter(None, (setup(view), tokens(view["usage"]), cost)))
            typer.secho(f"{view['id']}  {status}  {extra}", bold=True)
    for item in o.store.list_projects():
        if project and item.name != project:
            continue
        typer.secho(f"{item.name}  {item.path}", bold=True)
        show(item.name, None, 0)


@app.command()
def status(agent_id: str) -> None:
    """Show one agent: state, last result and the changes in its worktree."""
    o = orch()
    try:
        o.store.reconcile()
        view = o.view(o.agent(agent_id))
        changes = o.changes(agent_id)
    except OrchestratorError as exc:
        raise fail(exc) from None
    last = view.pop("last_run")
    usage = view.pop("usage")
    for key in ("model", "effort"):
        override = view.pop(f"{key}_override")
        view[key] = f"{view[key] or 'default'}" + (" (set on the agent)" if override else "")
    for key, value in view.items():
        typer.echo(f"{key:<12} {value}")
    typer.echo(f"{'usage':<12} {usage_line(usage)}")
    typer.echo(f"{'changes':<12} {changes.replace(chr(10), chr(10) + ' ' * 13)}")
    if last:
        typer.secho(f"\nlast run {last['id']}: {last['status']}", bold=True)
        if last["usage"]:
            typer.echo(usage_line(last["usage"]))
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


@app.command()
def stop(agent_id: str) -> None:
    """Stop an agent's running and queued turns; the agent and its worktree stay."""
    try:
        typer.echo(f"stopped {orch().stop(agent_id)} run(s)")
    except OrchestratorError as exc:
        raise fail(exc) from None


@app.command()
def rm(
    agent_id: str,
    delete_branch: Annotated[bool, typer.Option(help="Also delete the agent's branch.")] = False,
) -> None:
    """Delete an agent and its worktree (uncommitted work is lost)."""
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
