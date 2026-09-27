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

app = typer.Typer(add_completion=False, help="""copse: run coding agents in parallel, each on its own git branch.

Run `copse` with no arguments to open (or reopen) a supervisor chat here, with
the live dashboard underneath. Outside a git repo it starts a scratch session;
`copse transfer <repo>` moves that work into a real repository later.""")
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
        return
    subprocess.run(tmux.attach_command(ws.tmux_session))
    _after_detach(ws)


def _after_detach(ws: Workspace) -> None:
    """Back at the user's own prompt: say what happened and what's still running."""
    from copse import scratch

    db = DB()
    if tmux.has_session(ws.tmux_session):
        typer.echo("Detached; everything is still running. Run `copse` here to reopen.")
        return
    typer.echo("copse session paused: nothing is running, and all work is saved.")
    typer.echo("  `copse continue` picks it up where you left off; `copse` starts fresh.")
    if scratch.is_scratch(ws.path) and not scratch.transferred_to(ws.path):
        typer.echo(f"  Scratch work is saved in {ws.path}; `copse transfer <repo>` moves it into a repo.")


def _run(fn, *args, **kwargs):
    from copse.scratch import ScratchError

    try:
        return fn(*args, **kwargs)
    except (git.GitError, workspaces.WorkspaceError, agents.AgentError,
            tmux.TmuxError, ScratchError, KeyError, ValueError) as e:
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
    """Start a fresh chat with an agent here (default: a supervisor), with the
    dashboard of every agent in this repo beneath it. A session still running
    here is paused first; `copse continue` brings paused sessions back."""
    from copse import sessions

    db = DB()
    ws = _here_or_scratch(db, reuse_scratch=False)
    _pause_running(db, ws)
    sessions.enforce(db, ws.repo_root)
    a = _run(agents.spawn, db, ws, agent, prompt=prompt, provider_name=provider,
             watch_pane=watch)
    typer.echo(f"✓ {a.profile} agent {a.id} in {ws.id} ({ws.branch})")
    if attach:
        _attach(ws, a.tmux_window)


def _pause_running(db: DB, ws: Workspace) -> None:
    """At most one live session per checkout: pause any that's still running."""
    for a in db.list_agents(ws.id):
        if a.mode == "interactive" and a.status not in ("paused", "done") and agents.is_alive(a):
            agents.pause(db, a.id)
            typer.echo(f"Paused the session that was still running here ({a.id}); "
                       f"`copse continue {a.id}` brings it back.")


def _describe(s) -> str:
    workers = len(s.members) - 1
    ago = _ago(time.time() - s.paused_at)
    what = f", {workers} worker(s)" if workers else ""
    branches = f": {', '.join(s.branches[:3])}" + (" …" if len(s.branches) > 3 else "") if s.branches else ""
    return f"{s.root.id}  paused {ago} ago{what}{branches}"


def _ago(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


@app.command("continue")
def continue_cmd(
    session_id: Optional[str] = typer.Argument(None, help="Session to resume (default: the most recent)."),
    attach: bool = typer.Option(True, "--attach/--no-attach"),
) -> None:
    """Pick up a paused session: the chat and its workers resume where they stopped."""
    from copse import scratch, sessions

    db = DB()
    cwd = os.getcwd()
    try:
        git.main_repo_root(cwd)
        ws = _run(workspaces.adopt_root, db, cwd)
    except git.GitError:
        ws = scratch.for_origin(db, cwd)
        if ws is None:
            _fail("no scratch session started from this folder to continue; run `copse` to start one")
    available = sessions.paused(db, ws.repo_root)
    if not available:
        live = agents.find_running(db, ws, "supervisor")
        if live and attach:
            typer.echo(f"Session {live.id} is already running; reopening it.")
            _attach(ws, live.tmux_window)
            return
        _fail("no paused sessions here. Run `copse` to start a fresh one.")
    if session_id:
        matches = [s for s in available if s.root.id.startswith(session_id)]
        if len(matches) != 1:
            _fail(f"no single paused session matches {session_id!r}. Available:\n  "
                  + "\n  ".join(_describe(s) for s in available))
        chosen = matches[0]
    else:
        chosen = available[0]
    others = [s for s in available if s.root.id != chosen.root.id]
    _pause_running(db, ws)
    resumed = _run(agents.resume, db, chosen.root.id)
    typer.secho(f"↺ continuing {_describe(chosen)} ({len(resumed)} agent(s) restarted)", fg="green")
    if others:
        typer.echo("Other paused sessions (copse continue <id>):")
        for s in others:
            typer.echo(f"  {_describe(s)}")
    if attach:
        root = db.get_agent(chosen.root.id)
        _attach(chosen.workspace, root.tmux_window)


@app.command("sessions")
def sessions_cmd() -> None:
    """List paused sessions in this repo, with the disk their worktrees use."""
    from copse import sessions

    db = DB()
    root = _run(git.main_repo_root, os.getcwd())
    found = sessions.paused(db, root)
    if not found:
        typer.echo("no paused sessions")
        return
    for s in found:
        size = sum(sessions.disk_usage(w.path) for w in {db.get_workspace(m.workspace_id) for m in s.members[1:]} - {None}
                   if w and w.kind == "worktree" and os.path.isdir(w.path))
        typer.echo(f"{_describe(s)}  ({size / 1e6:.0f} MB in worktrees)")
    typer.echo(f"Keeps the newest {sessions.KEEP} for up to {sessions.MAX_AGE_DAYS} days. `copse prune` cleans up now.")


@app.command()
def prune() -> None:
    """Apply the retention rules now: drop paused sessions beyond the newest few or
    older than a week, and old scratch sessions with nothing left to transfer.
    Never merges or deletes branches; worktrees with uncommitted changes stay."""
    from copse import sessions

    db = DB()
    dropped = 0
    try:
        dropped = sessions.enforce(db, git.main_repo_root(os.getcwd()))
    except git.GitError:
        pass
    removed = sessions.prune_scratch(db)
    typer.echo(f"dropped {dropped} paused session(s), removed {removed} old scratch session(s)")


def _here_or_scratch(db: DB, reuse_scratch: bool) -> Workspace:
    """This checkout, or (outside any git repo) a scratch session for this folder."""
    from copse import scratch

    cwd = os.getcwd()
    try:
        git.main_repo_root(cwd)
    except git.GitError:
        existing = scratch.for_origin(db, cwd) if reuse_scratch else None
        if existing:
            typer.echo(f"↺ scratch session {existing.id}")
            return existing
        ws = _run(scratch.create, db, cwd)
        typer.secho(f"No git repository here, so this is a scratch session, tracked in {ws.path}", fg="cyan")
        typer.echo("Nothing is created in this folder. When you're ready, move the work into a real repo:")
        typer.echo("  copse transfer ~/path/to/repo    (or ask the supervisor to do it)")
        return ws
    _offer_pending_scratch(db, cwd)
    return _run(workspaces.adopt_root, db, cwd)


def _offer_pending_scratch(db: DB, cwd: str) -> None:
    """Inside a real repo: offer to bring in scratch work that hasn't moved yet."""
    from copse import scratch

    if scratch.is_scratch(cwd) or not sys.stdin.isatty():
        return
    for s in scratch.pending(db)[:3]:
        n = scratch.commit_count(s)
        dirty = " + uncommitted changes" if git.dirty_files(s.path) else ""
        started = scratch.origin_of(s.path) or "?"
        if typer.confirm(f"Bring scratch session {s.name} ({n} commit(s){dirty}, started in {started}) into this repo?", default=False):
            _do_transfer(db, s, cwd, None)


def _do_transfer(db: DB, s: Workspace, target: str, branch: Optional[str]) -> None:
    from copse import scratch

    t = _run(scratch.transfer, db, s, target, branch)
    extra = " (uncommitted work was committed first)" if t.snapshot else ""
    typer.secho(f"✓ moved {t.commits} commit(s){extra} onto branch {t.workspace.branch}", fg="green")
    typer.echo(f"  workspace {t.workspace.id} at {t.workspace.path}")
    typer.echo(f"  review: copse diff {t.workspace.name}   merge: copse merge {t.workspace.name}   PR: copse pr {t.workspace.name}")


@app.command()
def transfer(
    target: Optional[str] = typer.Argument(None, help="A folder inside the real git repo (default: here)."),
    source: Optional[str] = typer.Option(None, "--from", help="Scratch session name or id (default: the one you're in, or the newest)."),
    branch: Optional[str] = typer.Option(None, "--branch", "-b", help="Branch to create (default: copse/from-<session>)."),
) -> None:
    """Move a scratch session's work into a real repository, on its own branch."""
    from copse import scratch

    db = DB()
    here = os.getcwd()
    if source:
        s = _ws(db, source)
    elif scratch.is_scratch(here):
        s = _ws(db, None)
    else:
        options = scratch.pending(db)
        if not options:
            _fail("no scratch sessions with work to transfer")
        s = options[0]
    dest = target or here
    if scratch.is_scratch(dest):
        _fail("give the path of the real repository to move the work into, e.g. copse transfer ~/Projects/myapp")
    _do_transfer(db, s, dest, branch)


@app.callback(invoke_without_command=True)
def default(
    ctx: typer.Context,
    cont: bool = typer.Option(False, "--continue", "-c", help="Pick up the most recent paused session instead of starting fresh."),
) -> None:
    """Bare `copse`: a fresh supervisor chat here (a scratch session outside git)."""
    if ctx.invoked_subcommand is None:
        if cont:
            continue_cmd(session_id=None, attach=True)
        else:
            start(agent="supervisor", prompt=None, provider=None, attach=True, watch=True)


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
    sidebar: bool = typer.Option(False, "--sidebar", hidden=True),
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


@app.command("_ended", hidden=True)
def ended_cmd(agent_id: str) -> None:
    agents.ended(DB(), agent_id)


@app.command("_flush", hidden=True)
def flush_cmd(agent_id: str, delay: float = typer.Option(0.0)) -> None:
    time.sleep(delay)
    agents.flush(DB(), agent_id)
