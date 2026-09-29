copse 0.11.1 starts your local model for you, lets milestones pick their worker profile, and reads Codex's status from its own hook.

## New
- **Local models start themselves.** When `copse` or `copse continue` opens and a native profile points at Ollama on this machine that isn't answering, copse starts `ollama serve` in the background with `OLLAMA_CONTEXT_LENGTH` covering the largest `context_tokens` any profile asks (what `copse doctor` recommends), then loads each profile's model with a keep-alive so the first task doesn't wait on the read from disk. A server you started yourself is left alone; remote endpoints are never touched. Output goes to `~/.copse/ollama.log`; `"local_models": false` turns it off.
- **Milestones can name a worker profile.** A milestone in `set_goal` or `.copse/goals.md` may carry a `profile`, and `assign`/`handoff` called without an `agent_profile` use the first unverified milestone's profile, else the repo's `default_agent`. Small milestones can run on a cheaper profile without the supervisor naming it each time.
- **Codex status from its notify hook.** Codex agents are launched with `-c notify=[...]` (your own Codex config is untouched); a completed turn marks the agent idle and delivers any queued message, so screen reading is only a fallback.

## Docs
- The README's permissions section explains that a `Write(path)` allow rule is not consulted by Claude Code's file permission checks and `Edit(path)` should be used instead.
