"""copse command line."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Optional

import typer

from copse import agents, git, tmux, view, workspaces
from copse.config import write_template
from copse.db import DB, Workspace
from copse.profiles import list_profiles

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
agent_app = typer.Typer(no_args_is_help=True, help="Manage agents.")
app.add_typer(agent_app, name="agent")


def _fail(msg: str) -> None:
    typer.secho(msg, fg="red", err=True)
    raise typer.Exit(1)


def _ws(db: DB, ref: Optional[str]) -> Workspace:
    try:
        if ref:
            return workspaces.resolve(db, ref)
        ws = workspaces.current(db)
        if ws:
            return ws
    except workspaces.WorkspaceError as e:
        _fail(str(e))
    _fail("not inside a copse workspace; pass a workspace name")
    raise AssertionError


def _attach(ws: Workspace, window: str | None = None) -> None:
    if not tmux.has_session(ws.tmux_session):
        tmux.ensure_session(ws.tmux_session, ws.path, workspaces.workspace_env(ws))
    if window:
        tmux.select_window(window)
    if os.environ.get("TMUX"):
        subprocess.run([*tmux._base(), "switch-client", "-t", window or f"={ws.tmux_session}"])
    else:
        os.execvp("tmux", tmux.attach_command(ws.tmux_session))


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (git.GitError, workspaces.WorkspaceError, agents.AgentError,
            tmux.TmuxError, KeyError, ValueError) as e:
        _fail(str(e).strip("'\""))


# -- workspaces --------------------------------------------------------------


@app.command()
def init() -> None:
    """Write a starter .copse/config.json in this repo."""
    root = _run(git.main_repo_root, os.getcwd())
    path = write_template(root)
    typer.echo(f"wrote {path}")


@app.command()
def new(
    branch: Optional[str] = typer.Argument(None, help="Branch for the workspace (created if needed). Required unless --pr is given."),
    base: Optional[str] = typer.Option(None, "--base", "-b", help="Base branch (default: repo default). Not allowed with --pr."),
    pr: Optional[int] = typer.Option(None, "--pr", help="Check out this GitHub PR's head branch (via `gh`), based on the PR's base branch. Don't also pass BRANCH or --base; the head branch is always fetched."),
    agent: Optional[str] = typer.Option(None, "--agent", "-a", help="Agent profile to start (default from config; 'none' for no agent)."),
    prompt: Optional[str] = typer.Option(None, "--prompt", "-p", help="First message for the agent."),
    provider: Optional[str] = typer.Option(None, help="Override the profile's provider (claude, codex, shell)."),
    no_fetch: bool = typer.Option(False, "--no-fetch", help="Don't fetch the base branch first."),
    no_setup: bool = typer.Option(False, "--no-setup", help="Skip setup commands."),
    attach: bool = typer.Option(False, "--attach", help="Attach to the tmux session afterward."),
) -> None:
    """Create a worktree on a new branch and start an agent in it."""
    if pr is not None:
        if branch:
            _fail("pass either BRANCH or --pr, not both (--pr uses the PR's head branch)")
        if base:
            _fail("--base can't be combined with --pr (the PR's base branch is used)")
    elif not branch:
        _fail("missing BRANCH (or pass --pr <number>)")
    db = DB()
    if pr is not None:
        created = _run(workspaces.create_from_pr, db, os.getcwd(), pr, run_setup=not no_setup)
    else:
        created = _run(
            workspaces.create, db, os.getcwd(), branch, base,
            fetch=False if no_fetch else None, run_setup=not no_setup,
        )
    ws = created.workspace
    typer.secho(f"✓ {ws.id}", fg="green", bold=True)
    typer.echo(f"  branch  {ws.branch} ({created.how}, from {created.start_point})")
    typer.echo(f"  path    {ws.path}")
    typer.echo(f"  ports   {ws.port_base}-{ws.port_base + 9}  (COPSE_PORT_BASE)")
    if created.copied:
        typer.echo(f"  copied  {', '.join(created.copied)}")
    if created.setup:
        if created.setup.ok:
            typer.echo("  setup   ok")
        else:
            typer.secho(f"  setup   FAILED\n{created.setup.log}", fg="yellow")
            typer.echo(f"  (workspace kept; fix and re-run with `copse setup {ws.name}`)")

    from copse.config import load_repo_config

    profile = agent or load_repo_config(ws.repo_root).default_agent
    window = None
    if profile != "none":
        a = _run(agents.spawn, db, ws, profile, prompt=prompt, provider_name=provider)
        window = a.tmux_window
        typer.echo(f"  agent   {a.id} ({a.profile}/{a.provider})")
    typer.echo(f"\n  copse attach {ws.name}")
    if attach:
        _attach(ws, window)


@app.command()
def start(
    agent: str = typer.Option("supervisor", "--agent", "-a", help="Agent profile."),
    prompt: Optional[str] = typer.Option(None, "--prompt", "-p"),
    provider: Optional[str] = typer.Option(None),
    attach: bool = typer.Option(True, "--attach/--no-attach"),
    watch: bool = typer.Option(True, "--watch/--no-watch", help="Show the copse watch dashboard in a pane under the agent."),
) -> None:
    """Start an agent in the current checkout (default: a supervisor), with the
    dashboard of every agent in this repo beneath it."""
    db = DB()
    ws = _run(workspaces.adopt_root, db, os.getcwd())
    a = _run(agents.spawn, db, ws, agent, prompt=prompt, provider_name=provider, watch_pane=watch)
    typer.echo(f"✓ {a.profile} agent {a.id} in {ws.id} ({ws.branch})")
    if attach:
        _attach(ws, a.tmux_window)


@app.command("ls")
def list_cmd(
    all_repos: bool = typer.Option(False, "--all", help="Every repo, not just this one."),
    as_json: bool = typer.Option(False, "--json", help="Print a JSON array instead of a table."),
) -> None:
    """List workspaces and their agents."""
    db = DB()
    repo_root = None
    if not all_repos:
        try:
            repo_root = git.main_repo_root(os.getcwd())
        except git.GitError:
            pass
    rows = db.find_workspaces(repo_root)
    if as_json:
        typer.echo(json.dumps([view.workspace_entry(db, ws) for ws in rows], indent=2))
        return
    if not rows:
        typer.echo("no workspaces")
        return
    for ws in rows:
        if not os.path.isdir(ws.path):
            typer.secho(f"{ws.id}  (missing: {ws.path})", fg="red")
            continue
        e = view.workspace_entry(db, ws)
        info = ""
        if e["ahead"] is not None:
            dirty = f" *{e['dirty']}" if e["dirty"] else ""
            info = f"  ↑{e['ahead']} ↓{e['behind']}{dirty} vs {ws.base_branch}"
        typer.secho(f"{ws.id}", bold=True, nl=False)
        typer.echo(f"  [{ws.branch}]{info}")
        for a in e["agents"]:
            typer.echo(f"    {a['id']}  {a['profile']:<12} {a['provider']:<7} {a['status']:<11} {a['mode']}")


@app.command()
def watch(
    all_repos: bool = typer.Option(False, "--all", help="Every repo, not just this one."),
    once: bool = typer.Option(False, "--once", help="Print one snapshot and exit."),
) -> None:
    """Live dashboard of workspaces and agents (highlights agents waiting on you)."""
    from copse import watch as watch_mod

    repo_root = None
    if not all_repos:
        try:
            repo_root = git.main_repo_root(os.getcwd())
        except git.GitError:
            pass
    if once or not sys.stdout.isatty():
        typer.echo(watch_mod.print_once(DB(), repo_root, color=sys.stdout.isatty()))
        return
    watch_mod.run(repo_root)


@app.command()
def attach(workspace: Optional[str] = typer.Argument(None)) -> None:
    """Attach to a workspace's tmux session."""
    _attach(_ws(DB(), workspace))


@app.command()
def cd(workspace: str) -> None:
    """Print a workspace's path (use: cd "$(copse cd NAME)")."""
    typer.echo(_ws(DB(), workspace).path)


@app.command("open")
def open_cmd(
    workspace: Optional[str] = typer.Argument(None),
    editor: str = typer.Option(os.environ.get("COPSE_EDITOR", "code"), help="Editor command."),
) -> None:
    """Open a workspace in your editor."""
    subprocess.run([editor, _ws(DB(), workspace).path])


@app.command()
def setup(workspace: Optional[str] = typer.Argument(None)) -> None:
    """Re-run setup commands in a workspace."""
    from copse.config import load_repo_config

    ws = _ws(DB(), workspace)
    cmds = load_repo_config(ws.repo_root).setup
    res = workspaces.run_commands(cmds, ws.path, workspaces.workspace_env(ws))
    typer.echo(res.log or "(no setup commands)")
    raise typer.Exit(0 if res.ok else 1)


@app.command()
def status(workspace: Optional[str] = typer.Argument(None)) -> None:
    """Branch status against base: ahead/behind, uncommitted files, unpushed."""
    ws = _ws(DB(), workspace)
    st = _run(git.status, ws.path, ws.base_branch)
    typer.echo(f"{ws.id}  [{st.branch}]  base {st.base or '-'}")
    typer.echo(f"  {st.ahead} ahead, {st.behind} behind")
    typer.echo(f"  unpushed: {'no upstream' if st.unpushed is None else st.unpushed}")
    for f in st.dirty_files:
        typer.echo(f"  M {f}")


@app.command()
def diff(
    workspace: Optional[str] = typer.Argument(None),
    stat: bool = typer.Option(False, "--stat"),
) -> None:
    """Everything the branch changes vs. its base (commits + uncommitted)."""
    ws = _ws(DB(), workspace)
    text = _run(git.diff, ws.path, workspaces.require_base(ws), stat)
    if sys.stdout.isatty() and not stat:
        subprocess.run(["less", "-R"], input=text, text=True)
    else:
        typer.echo(text or "(no changes)")


@app.command()
def sync(
    workspace: Optional[str] = typer.Argument(None),
    merge: bool = typer.Option(False, "--merge", help="Merge base in instead of rebasing."),
) -> None:
    """Bring the base branch's latest commits into this workspace."""
    ws = _ws(DB(), workspace)
    if git.dirty_files(ws.path):
        _fail("uncommitted changes; commit or stash first")
    ref = _run(git.sync, ws.path, workspaces.require_base(ws), "merge" if merge else "rebase")
    typer.echo(f"✓ {ws.branch} is up to date with {ref}")


@app.command()
def commit(
    workspace: Optional[str] = typer.Argument(None),
    message: str = typer.Option(..., "--message", "-m"),
) -> None:
    """Stage everything and commit in a workspace."""
    ws = _ws(DB(), workspace)
    sha = _run(git.commit_all, ws.path, message)
    typer.echo(f"✓ {sha}" if sha else "nothing to commit")


@app.command()
def push(workspace: Optional[str] = typer.Argument(None)) -> None:
    """Push the workspace branch and set its upstream."""
    ws = _ws(DB(), workspace)
    _run(git.push, ws.path, ws.branch)
    typer.echo(f"✓ pushed {ws.branch}")


@app.command()
def pr(
    workspace: Optional[str] = typer.Argument(None),
    title: Optional[str] = typer.Option(None, "--title", "-t"),
    draft: bool = typer.Option(False, "--draft"),
) -> None:
    """Push and open a pull request against the base branch."""
    ws = _ws(DB(), workspace)
    url = _run(workspaces.pull_request, ws, title, draft)
    typer.echo(url)


@app.command("merge")
def merge_cmd(
    workspace: Optional[str] = typer.Argument(None),
    squash: bool = typer.Option(False, "--squash"),
) -> None:
    """Merge the workspace branch into its base branch locally."""
    db = DB()
    ws = _ws(db, workspace)
    target = _run(workspaces.merge_back, db, ws, squash)
    typer.echo(f"✓ merged {ws.branch} into {ws.base_branch} ({target})")


@app.command()
def rm(
    workspace: str,
    force: bool = typer.Option(False, "--force", "-f", help="Discard uncommitted changes; ignore teardown failure."),
    delete_branch: bool = typer.Option(False, "--delete-branch", "-D", help="Also delete the branch (only if merged, unless --force)."),
) -> None:
    """Stop a workspace's agents and remove its worktree. Keeps the branch by default."""
    db = DB()
    ws = _ws(db, workspace)
    if ws.base_branch and os.path.isdir(ws.path) and not delete_branch:
        st = git.status(ws.path, ws.base_branch)
        if st.ahead and st.unpushed != 0:
            typer.secho(
                f"note: {ws.branch} has {st.ahead} commit(s) not in {ws.base_branch} "
                "and not pushed; the branch is kept.", fg="yellow",
            )
    removed = _run(workspaces.remove, db, ws, force=force, delete_branch=delete_branch)
    if removed.teardown and not removed.teardown.ok:
        typer.secho(f"teardown failed (ignored with --force):\n{removed.teardown.log}", fg="yellow")
    typer.echo(f"✓ removed {ws.id}. {removed.branch_note or 'branch deleted'}")


# -- agents -----------------------------------------------------------------


@agent_app.command("spawn")
def agent_spawn(
    profile: str,
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
    prompt: Optional[str] = typer.Option(None, "--prompt", "-p"),
    provider: Optional[str] = typer.Option(None),
) -> None:
    """Start another agent in an existing workspace."""
    db = DB()
    ws = _ws(db, workspace)
    a = _run(agents.spawn, db, ws, profile, prompt=prompt, provider_name=provider)
    typer.echo(f"✓ {a.id} ({a.profile}/{a.provider}) in {ws.id}")


@agent_app.command("profiles")
def agent_profiles() -> None:
    """List available agent profiles."""
    root = None
    try:
        root = git.main_repo_root(os.getcwd())
    except git.GitError:
        pass
    for p in list_profiles(root):
        typer.echo(f"{p.name:<14} {p.provider:<7} {p.description}")


@agent_app.command("kill")
def agent_kill(agent_id: str) -> None:
    """Stop an agent and close its window."""
    db = DB()
    _run(agents.kill, db, agent_id)
    typer.echo(f"✓ killed {agent_id}")


@agent_app.command("peek")
def agent_peek(agent_id: str, lines: int = typer.Option(40, "--lines", "-n")) -> None:
    """Print the last lines of an agent's terminal."""
    db = DB()
    a = _run(agents.get, db, agent_id)
    typer.echo(tmux.capture(a.tmux_window, lines=lines).rstrip())


@app.command()
def send(agent_id: str, message: str) -> None:
    """Send a message to an agent (queued until it's idle)."""
    db = DB()
    outcome = _run(agents.send_message, db, agent_id, message)
    typer.echo(outcome)


@app.command()
def mcp() -> None:
    """Run the copse MCP server on stdio (agents launch this automatically)."""
    from copse.mcp_server import main

    main()


# -- internal ----------------------------------------------------------------


@app.command("_hook", hidden=True)
def hook(event: str) -> None:
    agent_id = os.environ.get("COPSE_AGENT_ID")
    if not agent_id:
        return
    out = agents.hook_main(DB(), agent_id, event, sys.stdin.read())
    if out:
        typer.echo(out)


@app.command("_flush", hidden=True)
def flush_cmd(agent_id: str, delay: float = typer.Option(0.0)) -> None:
    time.sleep(delay)
    agents.flush(DB(), agent_id)
