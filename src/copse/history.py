"""Durable, append-only history of what agents did: a worker's report, a
reviewer's verdict, a successful merge, and a milestone check. Written at
``report_result``, ``submit_review``, ``merge_workspace`` success, and
``check_milestone``.

Session pruning (sessions.py) deletes agent rows (and cascades reviews and
milestones with them) to keep disk use bounded; history has no foreign keys
so it survives that. It's capped per repo instead (see CAP_PER_REPO).
"""

from __future__ import annotations

import json
import time

from copse.db import DB, HistoryEntry
from copse.usage import Usage, format_tokens

TASK_CHARS = 300
RESULT_CHARS = 2000
CAP_PER_REPO = 5000

KINDS = ("worker_result", "review", "merge", "check", "milestone")


def _trim(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tokens_json(usage: Usage | None) -> str | None:
    if usage is None:
        return None
    return json.dumps({
        "input": usage.input_tokens, "output": usage.output_tokens,
        "cache_read": usage.cache_read_tokens, "cache_creation": usage.cache_creation_tokens,
        "model": usage.model,
    })


def record(db: DB, repo_root: str, kind: str, *, agent_id: str | None = None,
          branch: str | None = None, profile: str | None = None,
          task: str | None = None, result: str | None = None,
          usage: Usage | None = None) -> None:
    db.add_history(
        repo_root, kind, agent_id=agent_id, branch=branch, profile=profile,
        task=_trim(task, TASK_CHARS), result=_trim(result, RESULT_CHARS),
        tokens=_tokens_json(usage),
    )
    db.prune_history(repo_root, CAP_PER_REPO)


def _token_totals(tokens: str | None) -> dict | None:
    if not tokens:
        return None
    try:
        return json.loads(tokens)
    except ValueError:
        return None


def tokens_total(tokens: str | None) -> int:
    d = _token_totals(tokens)
    if not d:
        return 0
    return (d.get("input", 0) + d.get("output", 0) + d.get("cache_read", 0)
            + d.get("cache_creation", 0))


def tokens_summary(tokens: str | None) -> str:
    """The compact form for the `copse history` table, or "-" if there's none."""
    d = _token_totals(tokens)
    if not d:
        return "-"
    total_in = d.get("input", 0) + d.get("cache_read", 0) + d.get("cache_creation", 0)
    return f"{format_tokens(total_in)} in · {format_tokens(d.get('output', 0))} out"


def row_summary(row: HistoryEntry, width: int = 60) -> str:
    """The first line of whatever this row has to say: its task, or its
    result if it has no task (a review/merge/check row)."""
    text = (row.task or row.result or "").strip()
    line = text.splitlines()[0] if text else ""
    return line if len(line) <= width else line[: width - 1] + "…"
