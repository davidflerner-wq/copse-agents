# grove

Run CLI coding agents (Claude Code, Codex, …) in tmux. Each one works on its own
git branch in its own worktree, and a supervisor agent can delegate to workers,
review their branches, and merge them back.

grove combines two ideas:

- **From [CAO](https://github.com/awslabs/cli-agent-orchestrator):** agents in tmux
  sessions, supervisor→worker delegation over MCP (`handoff`, `assign`,
  `send_message`), an inbox that delivers messages when an agent goes idle, and
  markdown agent profiles.
- **From [Superset](https://github.com/superset-sh/superset):** each workspace is a
  worktree on its own branch, cut from a freshly fetched base. Each repo can
  define setup/teardown scripts, local files to copy in, and gets a port block
  per workspace. You can diff against the base, sync, commit, push, open a PR,
  and merge back. Removal checks for uncommitted changes and keeps the branch.

## Install

```sh
brew install tmux
uv tool install --editable ~/Projects/grove
```

## Quick start

```sh
cd ~/code/myapp
grove init                                  # optional: writes .grove/config.json
grove new fix-login -p "Fix the login redirect bug; add a test"
grove ls                                    # workspaces, agents, ahead/behind
grove attach fix-login                      # watch or talk to the agent
grove diff fix-login --stat
grove pr fix-login                          # push + gh pr create
grove rm fix-login                          # keeps the branch
```

Or let a supervisor split up the work:

```sh
grove start -p "Add CSV export to reports and a settings page; tests for both"
# a supervisor starts in this checkout. It calls assign(...) once per task, each
# worker gets branch grove/developer/<task>-xxxx, and the supervisor reviews
# with workspace_diff and merges with merge_workspace.
```

## Commands

| | |
|---|---|
| `grove new BRANCH [-b BASE] [-a PROFILE] [-p PROMPT]` | worktree + branch + agent |
| `grove start [-a supervisor]` | agent in the current checkout |
| `grove ls [--all]` | workspaces and agents |
| `grove attach / cd / open [WS]` | tmux session / path / editor |
| `grove status / diff [--stat] [WS]` | compared with the base branch (committed + uncommitted) |
| `grove sync [--merge] [WS]` | rebase (or merge) the latest base into the branch |
| `grove commit -m MSG / push / pr [WS]` | ship it |
| `grove merge [--squash] [WS]` | merge into the base locally |
| `grove rm WS [-f] [-D]` | remove the worktree; `-D` deletes the branch too, only if merged unless `-f` |
| `grove send AGENT MSG` | message an agent; waits in its inbox until it's idle |
| `grove agent spawn/kill/peek/profiles` | manage agents |

With no `WS` argument, commands act on the workspace you're in.

## Repo config: `.grove/config.json`

```json
{
  "setup": ["pnpm install", "cp \"$GROVE_ROOT_PATH/.env.local\" ."],
  "teardown": ["docker compose down"],
  "copy": [".env", "apps/*/.env"],
  "base_branch": "main",
  "branch_prefix": "",
  "default_agent": "developer",
  "fetch": true
}
```

`.grove/config.local.json` is gitignored and overrides keys for you only. For
`setup`/`teardown` it can also give `{"before": [...], "after": [...]}` to run
commands around the team's list.

Setup, teardown, and agents all see these variables: `GROVE_ROOT_PATH`,
`GROVE_WORKSPACE_PATH`, `GROVE_WORKSPACE_NAME`, `GROVE_WORKSPACE_ID`,
`GROVE_BRANCH`, `GROVE_BASE_BRANCH`, and `GROVE_PORT_BASE`. Each workspace gets
ten ports, from `GROVE_PORT_BASE` to `GROVE_PORT_BASE+9`, so parallel dev
servers don't collide. Agents also get `GROVE_AGENT_ID`.

## Agent profiles

Markdown files with frontmatter. grove looks in `.grove/agents/`, then
`~/.grove/agents/`, then its built-ins (`supervisor`, `developer`, `reviewer`):

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
worker is waiting on one, `grove ls` shows it as `waiting`, and you attach to
approve it. The built-in `developer` profile edits files without asking
(`acceptEdits`) and has an `allowed_tools` list covering git inspect/commit and
common test/build commands: `pytest`, `uv run`, `npm/pnpm/yarn test|run`,
`cargo`, `go`, `make`, `swift`, `xcodebuild`. It can't push or run arbitrary
commands. Note that `npm run`, `make`, and `uv run` execute whatever the repo
defines, so only point workers at repos you trust. Override the list in
`.grove/agents/developer.md`.

## How it works

- **State** lives in `~/.grove/grove.db` (SQLite, WAL mode). The CLI, the hooks,
  and every agent's MCP server share it. Worktrees live in
  `~/.grove/worktrees/<repo>/<branch>`, and the base branch is recorded in git
  config as `branch.<b>.grove-base`.
- **Agent status comes from hooks, not screen-scraping.** CAO works out whether
  Claude Code is idle by regex-matching the terminal, which breaks whenever the
  TUI changes. grove launches Claude Code with `--settings` hooks
  (`SessionStart`, `UserPromptSubmit`, `Stop`, `Notification`) that call
  `grove _hook <event>`. The `Stop` hook also delivers queued messages: it
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
PYTHONPATH=tests uv run pytest
```
