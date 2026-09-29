copse 0.11.0 lets agents reach directories outside their worktree, and adds a design document.

## New
- **`add_dirs`: directories outside the workspace.** A worktree is the agent's world, but a shared build cache, a checked-out reference repo, or a folder of profiles kept beside the repo sits outside it, and an agent that needs one stopped for a permission nobody could grant. `add_dirs` in `.copse/config.json` names those directories for every profile the repo launches; a profile's own `add_dirs` adds to that list. Each entry is passed to Claude Code as `--add-dir`. It is full tool access, not read access, and any `CLAUDE.md` in those directories is loaded. Relative entries resolve against the repo root, and a directory that does not exist is reported on stderr rather than ignored silently. Contributed by @davidflerner-wq (#2).
- **Design document.** `docs/DESIGN.md` describes the architecture: process model, module map, data model, the pipeline and message-delivery flows, autopilot, providers, the trust model, and known limitations, with diagrams.

## Docs
- The README says why a `Write(path)` allow rule does nothing for file permission checks (only `Edit(path)` rules are matched) and lists `add_dirs` in the config table.
