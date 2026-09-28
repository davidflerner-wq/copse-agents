copse 0.10.0 stops Claude Code workers from stalling on permission prompts and rounds out the native provider.

## New
- **Compound commands don't prompt.** A worker's shell command is approved by copse's own `PreToolUse` hook when every part of it matches the profile's `allowed_tools`, with `cd` into the worker's own worktree counting as covered. Claude Code matches a rule against a compound command only as a whole, so `cd src && pytest -q` used to stop a worker until someone answered; copse never denies anything itself.
- **Stuck-worker message names auto mode.** When a worker whose profile runs in Claude Code's auto mode sits on a prompt anyway, the message to its supervisor says auto mode is probably switched off in that session and what to check.
- **Native workers stream.** Model output arrives live over SSE for both wire formats (OpenAI chat completions and Anthropic Messages); an HTTP error on the streaming request retries without it.
- **Model-written fold summaries.** When a native worker's context fills, the model summarizes its own work so far (one extra call, no tools); copse's deterministic summary stands in when that fails.
- **Local reviewer by default.** With no `review_profile` set and Codex not installed, a Claude worker's review goes to the built-in `reviewer-local` profile when its model answers, so cross-model review works with only Ollama on the machine.
- **`copse doctor`** warns once per Ollama endpoint when the served context length is below a profile's `context_tokens`, with the `OLLAMA_CONTEXT_LENGTH` line to fix it.

## Fixes
- Native `Write` refuses to replace an existing file the model hasn't read (seen on qwen3-coder: a 661-line README replaced with a 3-line stub); the refusal says to Read first or use Edit.
- The sidebar no longer follows a worker session's own window change out of the person's sight: a worker's tmux session changes its active window at launch with no client attached, and the sidebar used to move there.
- Native runner: the `copse_tools` signature keeps its space.
