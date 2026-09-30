copse 0.11.4 is the first release on PyPI since 0.11.1: it carries everything from 0.11.2 and 0.11.3, which never reached PyPI because a test failed on Linux CI. In short: the pipeline no longer merges into your default branch on its own, a supervisor can run on (or hand over to) another branch, chat text is easy to copy, and a local Ollama server copse started is stopped with your session.

## Fixed in 0.11.4
- **copse recognises an `ollama serve` started from a long path.** On Linux, `ps` cut piped output at 80 columns, so copse could miss the server it started and leave it running.


## 0.11.3: Changed
- **No automatic merges into the default branch.** When a reviewed branch's base is the repo's default branch (usually `main`), the pipeline now sends the supervisor a "needs you" message instead of merging; `merge_workspace` merges it. Set `"merge_into"` to an integration branch in `.copse/config.json` to have workers branch from and merge into it, or `"auto_merge_default_branch": true` for the old behaviour. (#8)

## 0.11.3: Added
- **`copse start -b BRANCH` / `-w PATH`** runs the supervisor in that branch's worktree, creating it if needed. A linked worktree finds the repo's `.copse` config through the main checkout. (#9)
- **`copse handover --to BRANCH|PATH`** (and a `handover` tool for supervisors) moves the goal, milestones, workers, queued tasks and a note to a new supervisor there. (#10)
- **`list_agents` shows each worker's last tool call and how long ago it was active.** (#11)
- **Copying chat text:** dragging selects within the chat pane and copies to the clipboard (`pbcopy`, `wl-copy` or `xclip`); `h` in the sidebar or `prefix S` hides the sidebar; `"sidebar": "bottom"` puts the dashboard under the chat. (#12)

## 0.11.3: Fixed
- **The pipeline no longer kills its own reviewer while cleaning up after a merge,** which could leave a half-removed worktree, a stale status and no "merged" message. (#6)
- **`workspace_diff(stat_only=true)` is bounded:** untracked files are summarized per directory, `.venv`/`node_modules` and the like are collapsed, and long listings are cut off. (#7)


## 0.11.2: Fixed
- **Local models are shut down with the session.** A `ollama serve` that copse started is now stopped, along with the model it holds in memory, once no running copse session uses it: when the last such chat is closed or paused, or (for a session whose tmux went away) at the next cleanup sweep. Starting a new session in the same checkout keeps it running for the new one. A server you started yourself, or the Ollama app, is never stopped.
