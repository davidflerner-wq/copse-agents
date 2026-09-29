### Leaner reviews
- Check results are cached per commit and command (passes only), so the review and the merge gate don't rerun the same suite.
- The reviewer no longer runs the full suite itself. A detached `copse _deliver-checks` process runs the checks and sends it a short pass/fail summary, with output tails for failures only.
- Review prompts now include the worker's task and its `done_when` finish line, so approval means "the task is done", not just "the code looks fine".
- Re-reviews are incremental: the reviewer gets its previous findings and the diff since the last reviewed commit, when history is linear.
- The built-in `reviewer` profile runs on Sonnet with lean context (`strict_mcp`, `setting_sources`) at medium effort.

### Reliability
- Autopilot no longer freezes when a worker stops without calling `report_result`: its parent is told right away, and stalled workers are named in the supervisor's reminder.
- `check_milestone` re-checks milestones that already passed and reports regressions; unchanged commits are skipped.
- `merge_workspace` merges the base into a stale branch before running the gates, reports conflicts as a file list, refuses while a worker is still committing, and turns git errors into readable messages. Untracked files in your checkout no longer block a merge.
- Busy/idle detection matches Claude Code's current spinner, and a stale idle status is corrected from the screen.

### Visibility
- Token usage per agent, read incrementally from Claude Code transcripts (subagents included), shows in the sidebar, `copse ls`, and every worker's result message.
- New `copse history`: an append-only log of results, reviews, merges and checks with per-event token counts. It survives session pruning.
- Claude Code's own subagents (the Agent tool) now appear in the sidebar, nested under the agent that started them.

### Other
- The developer profile pre-approves `git merge`, `git fetch`, `git rev-parse` and `git branch`.
- Local state (`.claude`, `.copse`) is excluded from source distributions.
