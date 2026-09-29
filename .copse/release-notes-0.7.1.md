copse 0.7.1 fixes leftover rows in the sidebar after a launch.

## Fixes
- Launching `copse` no longer leaves the previous session's supervisor (or its workers) in the sidebar to close by hand. A stopped supervisor used to linger for 30 seconds even after a new session started, and a stopped session's workers were never hidden. Now, once a newer session is running, the old one leaves the sidebar immediately, along with any of its workers that aren't running. Everything stays resumable with `copse continue`.
