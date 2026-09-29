copse 0.7.0 is about making copse pleasant to live in: launches that don't hang, nothing left behind between sessions, a sidebar you can navigate, and docs for every command.

## Launches that don't hang
- Workers keep their profile's permission mode on every launch path, including resume and `copse continue`, and the built-in reviewer no longer waits on prompts (`permission_mode: dontAsk`).
- New worktrees are pre-trusted in Claude Code, so a worker nobody is watching never sits on the "trust this folder?" dialog. `~/.claude.json` is written safely: its mode is kept, writes are locked, it's re-read just before replacing, and symlinks are followed.
- `copse start` no longer waits on cleanup: stopping old processes and applying session retention run in a detached helper.
- An agent stuck on a permission or trust prompt shows as waiting (◆) in the sidebar, and its supervisor is told once after 90 seconds.
- Messages copse types into an agent's chat now start with a typed line naming the sender ("copse delivered this message from ..."), so agents act on them instead of treating them as untrusted pasted text.

## Nothing left behind
- `copse prune` also removes merged, clean worker worktrees (branches are kept, dirty worktrees are kept and listed), copse tmux sessions holding only idle shells, leftover copse tmux servers, and empty worktree folders.
- The sidebar hides worktrees whose branch is merged and whose agents are finished.
- The background sweep deletes stale sidebar lock files.
- Closing, killing and cleanup check which agent owns a tmux pane first, so an old agent's record can't take down a newer agent's window after tmux reuses pane ids.
- The test suite no longer leaks tmux servers or sockets, even when a run is killed partway.

## A navigable sidebar
- `?` lists every key; ◆ marks only what needs you (◇ for a worker autopilot will review), and `n` jumps to the next one.
- Space/Tab collapse a group, `/` filters by branch, agent or profile, and the selection stays on the same agent as rows reorder.
- Everything fits a 30-column pane; long branch names are shortened in the middle.

## Docs
- README and https://pawdelta.com/copse/ now include a "Recommended use" guide, every command, every agent (MCP) tool, and the sidebar keys. A test keeps them in sync with the code.
