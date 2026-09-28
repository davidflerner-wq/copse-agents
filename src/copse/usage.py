"""Token usage summed from a Claude Code session transcript.

Claude Code writes each session as JSONL under
``~/.claude/projects/<cwd-slug>/<session_id>.jsonl``. ``assistant`` entries
carry ``message.usage`` (``input_tokens``, ``output_tokens``,
``cache_read_input_tokens``, ``cache_creation_input_tokens``) and
``message.model``. Its own built-in subagents (the Agent tool) get their own
transcripts alongside it, under ``<session_id>/subagents/*.jsonl``.

Summing is incremental: the ``usage_cache`` table remembers, per file, how
many bytes have already been parsed and the running totals, so a repeated
call only reads new bytes, and costs nothing but a ``stat`` when a file
hasn't grown. A streamed assistant message can appear on more than one JSONL
line (one per content block), each carrying the full, identical ``usage``
for that message, always written back to back; only the first line for a
given message id is counted, so a single ``last_message_id`` per file is
enough to dedupe even across separate incremental calls. A file that shrank
or whose inode changed (rotated or replaced, even by a larger one) is parsed
again from the start.

The model label is the main transcript's latest model; subagents' models
don't override it, and Claude Code's ``<synthetic>`` placeholder is ignored.

Non-Claude-Code agents (Codex, etc.) have no ``transcript_path``, so
``agent_usage`` returns None for them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from copse.db import DB

log = logging.getLogger(__name__)

SYNTHETIC_MODEL = "<synthetic>"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    model: str | None = None

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_creation_tokens + other.cache_creation_tokens,
            other.model or self.model,
        )

    @property
    def total_in(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens

    @property
    def total(self) -> int:
        return self.total_in + self.output_tokens


def _parse_new(path: Path, offset: int, last_message_id: str | None) -> tuple[Usage, int, str | None]:
    """Sum the usage of every complete new line in ``path`` from byte
    ``offset``. Returns the delta, the offset just past the last complete
    line (a trailing partial line is left for next time), and the last
    message id seen."""
    delta = Usage()
    try:
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
    except (OSError, ValueError):
        log.warning("copse: couldn't read transcript %s", path, exc_info=True)
        return delta, offset, last_message_id
    new_offset = offset
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        new_offset += len(line)
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        msg_id = message.get("id")
        if msg_id and msg_id == last_message_id:
            continue
        u = message.get("usage")
        if not isinstance(u, dict):
            continue
        model = message.get("model")
        try:
            delta = delta + Usage(
                input_tokens=int(u.get("input_tokens") or 0),
                output_tokens=int(u.get("output_tokens") or 0),
                cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
                cache_creation_tokens=int(u.get("cache_creation_input_tokens") or 0),
                model=model if isinstance(model, str) and model != SYNTHETIC_MODEL else None,
            )
        except (TypeError, ValueError):
            continue
        if msg_id:
            last_message_id = msg_id
    return delta, new_offset, last_message_id


def _file_usage(db: DB, path: Path) -> Usage | None:
    """``path``'s usage, using (and updating) its cache row. None if the
    file can't be read at all."""
    try:
        st = path.stat()
    except OSError:
        return None
    size, inode = st.st_size, st.st_ino
    cached = db.get_usage_cache(str(path))
    if cached and cached["size"] <= size and cached["inode"] in (None, inode):
        offset = cached["size"]
        base = Usage(cached["input_tokens"], cached["output_tokens"], cached["cache_read_tokens"],
                     cached["cache_creation_tokens"], cached["model"])
        last_id = cached["last_message_id"]
    else:
        # No cache yet, or the file shrank or was replaced: start over.
        offset, base, last_id = 0, Usage(), None
    if size == offset and cached and cached["inode"] == inode:
        return base
    delta, new_offset, last_id = _parse_new(path, offset, last_id)
    total = base + delta
    db.set_usage_cache(str(path), new_offset, total.input_tokens, total.output_tokens,
                       total.cache_read_tokens, total.cache_creation_tokens, total.model, last_id,
                       inode)
    return total


def _subagent_transcripts(main: Path) -> list[Path]:
    d = main.parent / main.stem / "subagents"
    return sorted(d.glob("*.jsonl")) if d.is_dir() else []


def transcript_usage(db: DB, transcript_path: str) -> Usage | None:
    """Usage summed across ``transcript_path`` and any subagent transcripts
    stored alongside it. None if the transcript doesn't exist."""
    main = Path(transcript_path)
    if not main.is_file():
        return None
    total = _file_usage(db, main) or Usage()
    for p in _subagent_transcripts(main):
        u = _file_usage(db, p)
        if u is not None:
            total = total + Usage(u.input_tokens, u.output_tokens, u.cache_read_tokens,
                                  u.cache_creation_tokens)
    return total


def agent_usage(db: DB, agent) -> Usage | None:
    """Usage for a copse ``Agent``'s transcript, or None if it has none (a
    non-Claude-Code provider, or nothing recorded yet)."""
    if not agent.transcript_path:
        return None
    return transcript_usage(db, agent.transcript_path)


def format_tokens(n: int) -> str:
    if n >= 1000:
        return f"{(n + 500) // 1000}k"  # round half up, not to even
    return str(n)


def short_model(model: str | None) -> str:
    if not model:
        return "?"
    m = model.lower()
    for name in ("opus", "sonnet", "haiku"):
        if name in m:
            return name
    # For open-weight models, drop provider prefix and tag suffix
    if "/" in model:
        # Drop everything up to the last "/"
        model = model[model.rindex("/") + 1:]
    if ":" in model:
        # Drop everything from the first ":"
        model = model[:model.index(":")]
    return model


def summary_line(u: Usage) -> str:
    """The one-line summary appended to a forwarded worker/reviewer result,
    e.g. ``tokens: 182k in (160k cached, 20k written) · 9k out · sonnet``."""
    return (f"tokens: {format_tokens(u.total_in)} in ({format_tokens(u.cache_read_tokens)} cached, "
            f"{format_tokens(u.cache_creation_tokens)} written) "
            f"· {format_tokens(u.output_tokens)} out · {short_model(u.model)}")


def short_summary(u: Usage) -> str:
    """The compact form for the sidebar and `copse ls`, e.g. ``191k tok``."""
    return f"{format_tokens(u.total)} tok"
