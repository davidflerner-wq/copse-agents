# copse

*[pawdelta.com/copse](https://pawdelta.com/copse/) · Published on PyPI as `copse-agents`; the command is `copse`. This project is
unrelated to the Copse desktop app at copse.dev.*

A supervisor for your coding agents. copse runs Claude Code and Codex
side by side in tmux, gives each agent its own git worktree and branch, and
lets a supervisor agent split up work, hand it out, review each branch, and
merge the results. Nobody edits the same files, and nothing lands without
review.

- **Parallel agents, no collisions.** Every workspace is a separate worktree on
  its own branch, cut from a freshly fetched base, with its own block of ports
  for dev servers.
- **Delegation built in.** Agents get a `copse` MCP server: `assign` work to
  parallel workers, `handoff` a task and wait for it (in bounded steps, with
  `wait_for_worker` to keep waiting), `send_message` between agents, then
  `workspace_diff`, `merge_workspace`, and `remove_workspace`.
- **The whole branch lifecycle.** Diff against the base, sync (rebase or
  merge), commit, push, open a PR, merge back. Removal refuses to throw away
  uncommitted work and keeps the branch unless you say otherwise.
- **Per-repo setup.** `.copse/config.json` defines setup and teardown scripts
  and which local files (like `.env`) to copy into new workspaces.
- **Reliable status.** copse knows whether each agent is working, idle, or
  waiting for your approval from the agent's own lifecycle hooks, not by
  scraping the terminal.

## Install

```sh
brew install tmux
uv tool install copse-agents                  # once published
uv tool install --editable ~/Projects/copse   # from a local checkout
```

## Quick start

```sh
cd ~/code/myapp
copse
```

That's it. `copse` opens a supervisor chat (Claude Code) in your repo, with a
narrow sidebar on the left showing every agent: who's working, who's idle, and
who's waiting for your approval. Tell the supervisor what you want. It splits the
work between workers, each on its own branch, then reviews and merges their
branches. It starts in under a second.

**The sidebar follows you.** There's one sidebar pane per session root, not
one per window: switch to any other copse window or session (⏎ in the
sidebar, `copse attach`, prefix-L back, clicking a pane) and it relocates
there too, always beside whatever you're looking at, never spawning a second
dashboard. Scroll it
with the mouse wheel, PageUp/PageDown, or Home/End when there's more than fits;
moving the ↑↓ selection scrolls to keep it in view.

When an agent uses Claude Code's own Agent tool, its built-in subagents (Explore,
Plan, ...) show up nested underneath it in the sidebar too, e.g. `↳ Explore ·
running 1m`, so you can see what it's fanned out to without leaving copse.

**Closing and coming back.** When you quit the supervisor's chat, the copse window
closes cleanly and you're back at your prompt. The whole session is paused: its
workers stop too, and everything is kept (branches, worktrees,
session: its workers stop too, and everything is kept (branches, worktrees,
queued messages, and each agent's Claude conversation). `copse continue` (or
`copse -c`) picks up the most recent paused session and lists the others by id
(`copse continue <id>`). Plain `copse` always starts fresh. `copse sessions` lists
what's paused. copse keeps the newest 3 paused sessions per repo for up to 7 days;
cleanup never merges anything or deletes branches, and worktrees with uncommitted
changes are kept.

**Not in a git repo?** `copse` still works. It starts a *scratch session*: a
fresh git repo under `~/.copse/scratch/`, and nothing is created in the folder you ran
it from. When the work belongs in a real repository, run `copse transfer ~/path/to/repo`
(or ask the supervisor). The commits land on a new branch there, ready to review and
merge. Running `copse` in a repo also offers to bring in any scratch work that hasn't
been moved yet.

Or drive a single workspace yourself:

```sh
copse new fix-login -p "Fix the login redirect bug; add a test"
copse ls                                    # workspaces, agents, ahead/behind
copse diff fix-login --stat
copse pr fix-login                          # push + gh pr create
copse rm fix-login                          # keeps the branch
```

## Autopilot

`copse` starts the supervisor with autopilot on. Tell it what we're building,
or put the goal in `.copse/goals.md`, and it works like a project manager:

1. **Goal and milestones.** The goal is split into milestones, and each one has
   a check command that copse runs itself. A milestone is done only when its
   check exits 0, so progress is verified, not just claimed.
2. **Workers in parallel.** The supervisor splits each milestone into tasks
   and starts workers on their own branches, up to `max_agents` at once.
   Claude workers run the task as a Claude Code `/goal` with a finish line, so
   they keep going until it's met.
3. **Gated merges.** A branch merges only when everything is committed, a
   reviewer agent has approved that exact commit, your pre-commit hooks pass,
   and your `checks` pass. copse runs these itself before `merge_workspace`,
   and caches a clean commit's passing result so it isn't re-run for every
   review and merge attempt at the same sha. `request_review` starts the
   reviewer immediately and runs `checks` in the background, delivering a
   pass/fail summary (output only for failures) as a message once they
   finish, instead of asking the reviewer to run the whole suite itself.
4. **It keeps going.** If the supervisor stops while milestones are still
   unverified and no worker is running, copse tells it to continue. It stops
   when every check passes, when it needs a decision from you, after three
   reminders with no progress, or when your Claude usage nears its limit.

The sidebar shows the goal, each milestone (✓ verified, ✗ failing, ○ not
checked yet), and anything that needs you.

```markdown
<!-- .copse/goals.md -->
# Settings page

## Settings API
check: uv run pytest tests/test_settings_api.py -q

## Settings UI
check: npm test -- settings
```

`copse autopilot` shows progress, `copse autopilot check` runs the checks
now, and `copse autopilot off` (or `on`) hands the wheel back (or takes it
again). `copse --no-autopilot`, or `"autopilot": false` in the repo config,
starts without it.

To track your Claude usage, copse gives the agents it launches a status line.
It records the usage percentage Claude Code reports, then prints whatever
your own status line prints, so what you see doesn't change.

## Commands

| | |
|---|---|
| `copse new BRANCH [-b BASE] [-a PROFILE] [-p PROMPT]` | worktree + branch + agent |
| `copse` | a fresh supervisor chat here, dashboard alongside |
| `copse continue [ID]` / `copse -c` | resume a paused session (default: the most recent) |
| `copse sessions` / `copse prune` | list paused sessions / apply the retention rules now |
| `copse start [-a PROFILE] [-p PROMPT] [--no-watch] [--no-autopilot]` | the same, with options |
| `copse autopilot [on\|off\|check]` | the goal's progress; turn autopilot on or off; run the checks now |
| `copse transfer [REPO] [--from SESSION] [-b BRANCH]` | move a scratch session's work into a real repo |
| `copse ls [--all]` | workspaces and agents |
| `copse history [--limit N] [--kind K] [--all]` | durable log of worker results, reviews, merges and milestone checks |
| `copse watch [--all] [--once]` | the dashboard on its own (the same view as the sidebar): enter attaches, `p` peeks |
| `copse attach / cd / open [WS]` | tmux session / path / editor |
| `copse status / diff [--stat] [WS]` | compared with the base branch (committed + uncommitted) |
| `copse sync [--merge] [WS]` | rebase (or merge) the latest base into the branch |
| `copse commit -m MSG / push / pr [WS]` | ship it |
| `copse merge [--squash] [WS]` | merge into the base locally |
| `copse rm WS [-f] [-D]` | remove the worktree; `-D` deletes the branch too, only if merged unless `-f` |
| `copse send AGENT MSG` | message an agent; waits in its inbox until it's idle |
| `copse agent spawn/kill/peek/profiles` | manage agents |

With no `WS` argument, commands act on the workspace you're in.

## Token usage and history

Every Claude Code agent's token usage (input, cached, output, model) is read
straight from its own transcript JSONL under `~/.claude/projects/`, summed
incrementally so it's cheap to check often. It shows up:

- in the sidebar and `copse ls`, next to each agent (e.g. `191k tok`)
- appended to the result a worker or reviewer forwards to its supervisor
  (e.g. `tokens: 182k in (160k cached, 20k written) · 9k out · sonnet`)
- in `copse history`, per row, with a total across the rows shown. Each row
  holds only what its agent used since that agent's previous row, so the
  total never double counts

`copse history` is an append-only log of what happened: a worker's report, a
reviewer's verdict, a successful merge, and a milestone check (reports and
merges carry tokens). Unlike `copse ls`, it survives session pruning (`copse prune`), so
it's the place to look for what an agent did after its session is gone. It's
capped at 5000 rows per repo, oldest dropped first. Recording usage or
history never blocks a report, merge or check: a failure there is logged and
skipped.

## Repo config: `.copse/config.json`

```json
{
  "setup": ["pnpm install", "cp \"$COPSE_ROOT_PATH/.env.local\" ."],
  "teardown": ["docker compose down"],
  "copy": [".env", "apps/*/.env"],
  "base_branch": "main",
  "branch_prefix": "",
  "default_agent": "developer",
  "fetch": true,
  "checks": ["uv run pytest -q"],
  "max_agents": 4
}
```

The last two are for autopilot and merge gates:

| Key | Default | |
|---|---|---|
| `autopilot` | `true` | start the supervisor with autopilot on |
| `checks` | `[]` | commands that must pass in a worker's branch before it merges |
| `review` | only under autopilot | require a reviewer's approval before merging |
| `reviewer` | `"reviewer"` | the agent profile that reviews (a Codex profile gives a second model's view) |
| `pre_commit` | `true` | run [pre-commit](https://pre-commit.com) over the branch, if the repo uses it |
| `max_agents` | `4` | workers running at once per session (`0`: no cap) |
| `check_timeout` | `900` | seconds each check may take |
| `usage_limit` | `90` | autopilot stops pushing on at this % of your Claude usage limit |

`.copse/config.local.json` is gitignored and overrides keys for you only. For
`setup`/`teardown` it can also give `{"before": [...], "after": [...]}` to run
commands around the team's list.

Setup, teardown, and agents all see these variables: `COPSE_ROOT_PATH`,
`COPSE_WORKSPACE_PATH`, `COPSE_WORKSPACE_NAME`, `COPSE_WORKSPACE_ID`,
`COPSE_BRANCH`, `COPSE_BASE_BRANCH`, and `COPSE_PORT_BASE`. Each workspace gets
ten ports, from `COPSE_PORT_BASE` to `COPSE_PORT_BASE+9`, so parallel dev
servers don't collide. Agents also get `COPSE_AGENT_ID`.

## Agent profiles

Markdown files with frontmatter. copse looks in `.copse/agents/`, then
`~/.copse/agents/`, then its built-ins (`supervisor`, `developer`, `reviewer`, `subagent`):

```markdown
---
name: frontend
description: React/TypeScript specialist
provider: claude          # claude | codex | antigravity | shell | subagent
model: sonnet             # optional
permission_mode: acceptEdits   # optional, Claude Code only
---
You are a frontend engineer...
```

**Permissions.** Workers run with Claude Code's normal permission prompts. When a
worker is waiting on one, `copse ls` shows it as `waiting`, and you attach to
approve it. The built-in `developer` profile edits files without asking
(`acceptEdits`) and has an `allowed_tools` list covering git inspect/commit and
common test/build commands: `pytest`, `uv run`, `npm/pnpm/yarn test|run`,
`cargo`, `go`, `make`, `swift`, `xcodebuild`. It can't push or run arbitrary
commands. Note that `npm run`, `make`, and `uv run` execute whatever the repo
defines, so only point workers at repos you trust. Override the list in
`.copse/agents/developer.md`.

### Cheap workers

By default a Claude Code worker loads everything your own `claude` does: your
plugins, MCP servers and `~/.claude/CLAUDE.md`. That context is re-read on every
turn and can be most of a worker's input tokens. These optional fields (Claude
Code only, all off by default) trim it:

| Field | Passes | Effect |
|---|---|---|
| `strict_mcp: true` | `--strict-mcp-config` | Only copse's MCP server loads; your other MCP servers don't. |
| `setting_sources: project,local` | `--setting-sources` | Skips your user settings (`~/.claude`: plugins, hooks, `CLAUDE.md`). copse's own hooks come through `--settings`, which is always applied. |
| `effort: low` | `--effort` | `low`, `medium`, `high`, `xhigh` or `max`. |
| `headless: true` | `claude -p` | No interactive TUI; each turn is one `claude -p` run (see below). |

```markdown
---
name: cheap
description: Small, well-specified edits at low cost
provider: claude
model: sonnet
effort: low
strict_mcp: true
setting_sources: project,local   # no user plugins or ~/.claude/CLAUDE.md
headless: true
permission_mode: acceptEdits
allowed_tools: Bash(git add:*), Bash(git commit:*), Bash(git status:*), Bash(git diff:*), Bash(uv run:*), Bash(pytest:*)
---
You are a developer agent running under copse. Implement the task, run the
tests, commit, and report.
```

**Headless workers** still run in a tmux window, where a small copse runner
starts `claude -p` for the task, then `claude -p --resume <session>` for each
message sent to the worker while it's idle. Messages sent mid-turn are handed
over when the turn ends, as for any Claude worker, and so is the reminder to call
`report_result`. The window shows each turn's prompt and final answer (`copse
agent peek`). If `claude` exits with an error, the worker stops, and `handoff`
reports the error. Differences from an interactive worker:

- Nothing can answer a permission prompt, so any tool not covered by
  `permission_mode` and `allowed_tools` is refused rather than waiting for you.
  A headless worker never shows as `waiting`.
- You can't attach and type into it. Talk to it with `copse send` or `send_message`.
- A `done_when` finish line is added to the task, without `/goal` (an interactive command).

Blank values and anything after ` #` are ignored, so frontmatter can carry comments.

### Subagent workers

The built-in `subagent` profile (`provider: subagent`) gives a Claude Code
supervisor copse's worktree and branch handling for work done by its **own**
subagent (its Agent tool), with no separate `claude` process. `handoff` or
`assign` with it creates the workspace and returns immediately. The reply
contains the worktree path, the branch, the agent id and a ready-made prompt
for the Agent tool. That prompt tells the subagent to work only in that
directory, commit there and end with a summary. The supervisor then calls
`complete_subagent(agent_id, result)`. The worker shows as working until then
and done after. `workspace_diff`, `request_review`, `merge_workspace` and
`remove_workspace` work as usual. copse can't message a subagent, and
`send_message` says so. Worktrees live under `~/.copse/worktrees/`, outside the
supervisor's own directory. Unless the supervisor runs with permission to edit
there (`--add-dir ~/.copse/worktrees`, or `additionalDirectories` in Claude
Code settings), the subagent's edits ask for approval.

## Google Antigravity

copse runs Google Antigravity's terminal agent, `agy`, as well as Claude Code and
Codex. Install it and sign in once:

```sh
curl -fsSL https://antigravity.google/cli/install.sh | bash
agy        # sign in with your Google account, then quit
```

Then run the whole session on it with `copse --provider antigravity`, or mix models:
give a profile `provider: antigravity` (for example a `gemini-reviewer` for a second
model's review) and the supervisor can hand it tasks.

`agy` has no command-line options for hooks, MCP servers or instructions, so copse
adds three files to the checkout's `.agents/` folder: `mcp_config.json` (copse's tools),
`hooks.json` (status, messages, autopilot) and `rules/copse.md`. They're listed in
`.git/info/exclude`, so they never show up in `git status`. copse adds to these files if
you already have them, and won't change one that's committed. Each agent's first
message is a short warm-up with its instructions, because `agy` connects MCP servers
only once a conversation has started.

**Permissions.** `agy` doesn't let hooks approve shell commands, so an Antigravity
agent asks before running anything your own `agy` settings don't already allow, and
the sidebar shows it as needing you. To let agents run tests and commit without asking,
add rules to `~/.gemini/antigravity-cli/settings.json`, for example:

```json
{ "permissions": { "allow": ["command(uv run pytest)", "command(git status)",
                               "command(git diff)", "command(git add)", "command(git commit)"] } }
```

## How it works

- **Look:** copse's tmux sessions get their own dark purple theme and mouse
  scrolling. Your own tmux setup and other sessions are untouched (apart from
  tmux's `focus-events`, which Claude Code asks for).
- **State** lives in `~/.copse/copse.db` (SQLite, WAL mode). The CLI, the hooks,
  and every agent's MCP server share it. Worktrees live in
  `~/.copse/worktrees/<repo>/<branch>`, and the base branch is recorded in git
  config as `branch.<b>.copse-base`.
- **Agent status comes from hooks, not screen-scraping.** Guessing an agent's
  state by pattern-matching terminal output breaks whenever a CLI redesigns its
  interface. copse launches Claude Code with `--settings` hooks
  (`SessionStart`, `UserPromptSubmit`, `Stop`, `StopFailure`, `Notification`) that call
  `copse _hook <event>`. The `Stop` hook also delivers queued messages: it
  returns `{"decision": "block", "reason": <message>}`, so Claude continues with
  the message as its next instruction and nothing is typed into a busy terminal.
  A message queued for an *idle* agent is typed in instead, but only once copse
  checks the screen and finds a clear chat input: not text you're still typing,
  and not Claude Code's background-session launcher (which would otherwise
  start a whole new session). Otherwise it stays queued for the next chance.
- **Results are explicit.** Workers call the `report_result` MCP tool instead of
  having their output parsed from the screen. If a worker stops without
  reporting, the Stop hook reminds it once.
- **Worker isolation:** `handoff`/`assign` with `isolate=true` (the default)
  create a worktree whose branch starts from the *supervisor's* current branch,
  so workers build on the supervisor's committed work. `merge_workspace`
  brings a worker's branch back.

## Development

```sh
uv sync
uv run pytest
```

### Releasing

1. Bump `version` in `pyproject.toml`, commit, and push.
2. Create a GitHub release tagged `v<version>` (e.g. `gh release create v0.1.1 --generate-notes`).
3. The Publish workflow tests, builds, and uploads to PyPI via Trusted Publishing.


## License

Apache-2.0
