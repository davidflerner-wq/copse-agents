copse 0.9.0 runs workers and reviewers on open-weight models with its own agent loop.

## New
- **Native provider.** A profile with `provider: native` runs copse's own harness instead of a third-party CLI: one client over OpenAI chat-completions and Anthropic Messages endpoints, the worker's core tools (Read, Write, Edit, Glob, Grep, Bash) confined to its worktree, Claude Code's `permission_mode` / `allowed_tools` shapes, and a loop that reports its status to copse directly, takes queued messages between model calls, folds old context, and calls `report_result`, `submit_review`, `send_message` and `workspace_diff` in-process. A paused native worker resumes where it was.
- **Open-weight models.** Built-in `developer-local` and `reviewer-local` profiles run Qwen3-Coder 30B on Ollama; the README lists endpoints for Ollama, LM Studio and hosted providers. Profiles' `env.NAME:` lines reach any agent's process, so a Claude Code profile can point at another backend too.
- **`copse doctor`** probes each native profile's endpoint and says whether the model is there.

## Fixes
- Native loop: a compound command such as `git add -A && git commit` is allowed when each part matches any rule, and the refusal names the part that isn't covered. Tool calls a model writes as text (`<tool_call>` blocks) are recovered instead of dropped.
- Sidebar shows open-weight model IDs without the provider prefix and tag suffix.
- Tests spawn shell workers without a prompt, so CI without Claude Code no longer kills its own tmux session.
