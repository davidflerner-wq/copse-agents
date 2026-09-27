# copse

*Published on PyPI as `copse-agents`; the command is `copse`. This project is
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
copse init                                  # optional: writes .copse/config.json
copse new fix-login -p "Fix the login redirect bug; add a test"
copse ls                                    # workspaces, agents, ahead/behind
copse attach fix-login                      # watch or talk to the agent
copse diff fix-login --stat
copse pr fix-login                          # push + gh pr create
copse rm fix-login                          # keeps the branch
```

Or let a supervisor split up the work:

```sh
copse start -p "Add CSV export to reports and a settings page; tests for both"
# a supervisor starts in this checkout. It calls assign(...) once per task, each
# worker gets branch copse/developer/<task>-xxxx, and the supervisor reviews
# with workspace_diff and merges with merge_workspace.
```

## Commands

| | |
|---|---|
| `copse new BRANCH [-b BASE] [-a PROFILE] [-p PROMPT]` | worktree + branch + agent |
| `copse start [-a supervisor]` | agent in the current checkout |
| `copse ls [--all]` | workspaces and agents |
| `copse watch [--all] [--once]` | live dashboard: agent status, who's waiting on you, queued messages; enter attaches, `p` peeks |
| `copse attach / cd / open [WS]` | tmux session / path / editor |
| `copse status / diff [--stat] [WS]` | compared with the base branch (committed + uncommitted) |
| `copse sync [--merge] [WS]` | rebase (or merge) the latest base into the branch |
| `copse commit -m MSG / push / pr [WS]` | ship it |
| `copse merge [--squash] [WS]` | merge into the base locally |
| `copse rm WS [-f] [-D]` | remove the worktree; `-D` deletes the branch too, only if merged unless `-f` |
| `copse send AGENT MSG` | message an agent; waits in its inbox until it's idle |
| `copse agent spawn/kill/peek/profiles` | manage agents |

With no `WS` argument, commands act on the workspace you're in.

## Repo config: `.copse/config.json`

```json
{
  "setup": ["pnpm install", "cp \"$COPSE_ROOT_PATH/.env.local\" ."],
  "teardown": ["docker compose down"],
  "copy": [".env", "apps/*/.env"],
  "base_branch": "main",
  "branch_prefix": "",
  "default_agent": "developer",
  "fetch": true
}
```

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
`~/.copse/agents/`, then its built-ins (`supervisor`, `developer`, `reviewer`):

```markdown
---
name: frontend
description: React/TypeScript specialist
provider: claude          # claude | codex | shell
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

## How it works

- **State** lives in `~/.copse/copse.db` (SQLite, WAL mode). The CLI, the hooks,
  and every agent's MCP server share it. Worktrees live in
  `~/.copse/worktrees/<repo>/<branch>`, and the base branch is recorded in git
  config as `branch.<b>.copse-base`.
- **Agent status comes from hooks, not screen-scraping.** Guessing an agent's
  state by pattern-matching terminal output breaks whenever a CLI redesigns its
  interface. copse launches Claude Code with `--settings` hooks
  (`SessionStart`, `UserPromptSubmit`, `Stop`, `Notification`) that call
  `copse _hook <event>`. The `Stop` hook also delivers queued messages: it
  returns `{"decision": "block", "reason": <message>}`, so Claude continues with
  the message as its next instruction and nothing is typed into a busy terminal.
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

## Acknowledgements

copse builds on ideas from
[CLI Agent Orchestrator](https://github.com/awslabs/cli-agent-orchestrator) and
[Superset](https://github.com/superset-sh/superset).

## License

Apache-2.0
