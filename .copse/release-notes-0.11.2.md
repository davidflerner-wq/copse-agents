copse 0.11.2 stops the local model server it started when your session ends, so a 20 GB model doesn't stay in memory.

## Fixed
- **Local models are shut down with the session.** A `ollama serve` that copse started is now stopped, along with the model it holds in memory, once no running copse session uses it: when the last such chat is closed or paused, or (for a session whose tmux went away) at the next cleanup sweep. Starting a new session in the same checkout keeps it running for the new one. A server you started yourself, or the Ollama app, is never stopped.
