import json
import time
from pathlib import Path

from copse import agents, usage, view
from copse.db import Agent


def _line(msg_id, input_tokens=0, output_tokens=0, cache_read=0, cache_creation=0,
          model="claude-sonnet-5", type_="assistant"):
    return json.dumps({
        "type": type_,
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            },
        },
    }) + "\n"


def write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for line in lines:
            f.write(line)


def test_sums_usage_across_assistant_messages(db, tmp_path):
    t = tmp_path / "sess.jsonl"
    write(t, [
        _line("m1", input_tokens=10, output_tokens=20, cache_read=100, cache_creation=5),
        _line("m2", input_tokens=6, output_tokens=155, cache_read=0, cache_creation=25169,
              model="claude-opus-4-7"),
    ])
    u = usage.transcript_usage(db, str(t))
    assert u is not None
    assert u.input_tokens == 16
    assert u.output_tokens == 175
    assert u.cache_read_tokens == 100
    assert u.cache_creation_tokens == 25174
    assert u.model == "claude-opus-4-7"  # the last message's model


def test_ignores_non_assistant_entries(db, tmp_path):
    t = tmp_path / "sess.jsonl"
    write(t, [
        json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n",
        json.dumps({"type": "queue-operation", "operation": "enqueue"}) + "\n",
        _line("m1", input_tokens=1, output_tokens=2),
    ])
    u = usage.transcript_usage(db, str(t))
    assert (u.input_tokens, u.output_tokens) == (1, 2)


def test_dedupes_a_message_streamed_across_several_lines(db, tmp_path):
    """Claude Code writes one line per content block; every block of the same
    message repeats its full (identical) usage. Only the first counts."""
    t = tmp_path / "sess.jsonl"
    write(t, [
        _line("m1", input_tokens=6, output_tokens=155, cache_creation=25169),
        _line("m1", input_tokens=6, output_tokens=155, cache_creation=25169),
        _line("m1", input_tokens=6, output_tokens=155, cache_creation=25169),
    ])
    u = usage.transcript_usage(db, str(t))
    assert (u.input_tokens, u.output_tokens, u.cache_creation_tokens) == (6, 155, 25169)


def test_incremental_reread_only_parses_new_bytes(db, tmp_path):
    t = tmp_path / "sess.jsonl"
    write(t, [_line("m1", input_tokens=10, output_tokens=20)])
    first = usage.transcript_usage(db, str(t))
    assert (first.input_tokens, first.output_tokens) == (10, 20)

    cached = db.get_usage_cache(str(t))
    assert cached["size"] == t.stat().st_size

    write(t, [_line("m2", input_tokens=1, output_tokens=2)])
    second = usage.transcript_usage(db, str(t))
    assert (second.input_tokens, second.output_tokens) == (11, 22)


def test_incremental_reread_dedupes_a_message_split_across_calls(db, tmp_path):
    """A message's later content-block line can land in its own incremental
    read, separate from the call that already counted the message's first
    line; it must still not be double-counted."""
    t = tmp_path / "sess.jsonl"
    write(t, [_line("m1", input_tokens=10, output_tokens=20)])
    first = usage.transcript_usage(db, str(t))
    assert (first.input_tokens, first.output_tokens) == (10, 20)

    write(t, [_line("m1", input_tokens=10, output_tokens=20)])
    second = usage.transcript_usage(db, str(t))
    assert (second.input_tokens, second.output_tokens) == (10, 20)

    write(t, [_line("m2", input_tokens=1, output_tokens=2)])
    third = usage.transcript_usage(db, str(t))
    assert (third.input_tokens, third.output_tokens) == (11, 22)


def test_missing_file_returns_none(db, tmp_path):
    assert usage.transcript_usage(db, str(tmp_path / "nope.jsonl")) is None


def test_agent_usage_none_without_a_transcript(db):
    a = Agent("a1", "ws1", "developer", "claude", None, "interactive", "idle", "", None, time.time())
    assert usage.agent_usage(db, a) is None


def test_agent_usage_none_for_non_claude_providers(db):
    # Codex (and any other non-Claude-Code provider) never gets a
    # transcript_path recorded, since only Claude Code's hooks supply one.
    a = Agent("a1", "ws1", "developer", "codex", None, "interactive", "idle", "", None, time.time())
    assert usage.agent_usage(db, a) is None


def test_includes_subagent_transcripts(db, tmp_path):
    t = tmp_path / "sess.jsonl"
    write(t, [_line("m1", input_tokens=10, output_tokens=20)])
    sub = tmp_path / "sess" / "subagents" / "agent-abc.jsonl"
    write(sub, [_line("m2", input_tokens=1, output_tokens=2)])
    u = usage.transcript_usage(db, str(t))
    assert (u.input_tokens, u.output_tokens) == (11, 22)


def test_format_helpers():
    assert usage.format_tokens(999) == "999"
    assert usage.format_tokens(1000) == "1k"
    assert usage.format_tokens(182000) == "182k"
    assert usage.format_tokens(1500) == "2k"  # half up, not banker's rounding to even
    assert usage.format_tokens(2500) == "3k"
    assert usage.format_tokens(2499) == "2k"
    assert usage.short_model("claude-opus-4-7") == "opus"
    assert usage.short_model("claude-sonnet-5") == "sonnet"
    assert usage.short_model(None) == "?"


def test_summary_line_format():
    u = usage.Usage(input_tokens=2000, output_tokens=9000, cache_read_tokens=160000,
                    cache_creation_tokens=20000, model="claude-sonnet-5")
    assert usage.summary_line(u) == "tokens: 182k in (160k cached, 20k written) · 9k out · sonnet"


def test_report_result_forwards_usage_summary(db, tmp_path, monkeypatch):
    from copse import workspaces

    from conftest import sh

    origin = tmp_path / "origin.git"
    sh(f"git init -q --bare -b main {origin}", tmp_path)
    work = tmp_path / "proj"
    work.mkdir()
    sh("git init -q -b main", work)
    (work / "f.txt").write_text("x")
    sh("git add -A && git commit -qm init", work)

    ws = workspaces.create(db, str(work), "feature").workspace
    t = tmp_path / "sess.jsonl"
    write(t, [_line("m1", input_tokens=2000, output_tokens=9000, cache_read=160000, cache_creation=20000)])

    boss = Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing", "@0",
                 None, time.time())
    db.add_agent(boss)
    worker = Agent("w1", ws.id, "developer", "claude", "boss", "assign", "processing", "@1",
                   None, time.time(), transcript_path=str(t))
    db.add_agent(worker)
    monkeypatch.setattr(agents, "is_alive", lambda a: True)

    agents.report_result(db, "w1", "done: added login")
    msg = db.pop_pending("boss")
    assert msg is not None
    assert "done: added login" in msg.body
    assert "tokens: 182k in (160k cached, 20k written)" in msg.body
    assert "sonnet" in msg.body


def test_hook_records_the_transcript_path(db, repo):
    from copse import workspaces

    ws = workspaces.create(db, str(repo), "feature").workspace
    a = Agent("a1", ws.id, "developer", "claude", None, "assign", "idle", "", None, time.time())
    db.add_agent(a)
    agents.handle_hook(db, "a1", "prompt-submit", {"transcript_path": "/tmp/x/a1.jsonl"})
    assert db.get_agent("a1").transcript_path == "/tmp/x/a1.jsonl"


def test_agent_entry_reads_once_then_only_stats(db, tmp_path, monkeypatch):
    from copse import workspaces

    from conftest import sh

    origin = tmp_path / "origin.git"
    sh(f"git init -q --bare -b main {origin}", tmp_path)
    work = tmp_path / "proj"
    work.mkdir()
    sh("git init -q -b main", work)
    (work / "f.txt").write_text("x")
    sh("git add -A && git commit -qm init", work)
    ws = workspaces.create(db, str(work), "feature").workspace

    t = tmp_path / "sess.jsonl"
    write(t, [_line("m1", input_tokens=10, output_tokens=20)])
    a = Agent("a1", ws.id, "developer", "claude", None, "assign", "idle", "", "done", time.time(),
             transcript_path=str(t))
    db.add_agent(a)

    calls = []
    real_parse = usage._parse_new

    def counting_parse(path, offset, last_id):
        calls.append(path)
        return real_parse(path, offset, last_id)

    monkeypatch.setattr(usage, "_parse_new", counting_parse)

    entry1 = view.agent_entry(db, db.get_agent("a1"))
    entry2 = view.agent_entry(db, db.get_agent("a1"))
    assert entry1["tokens"] == entry2["tokens"] == "30 tok"
    assert len(calls) == 1  # the second call found no new bytes: stat only


# -- replaced/truncated files, model label, junk ------------------------------


def test_truncated_file_is_reparsed_from_the_start(db, tmp_path):
    t = tmp_path / "sess.jsonl"
    write(t, [_line("m1", input_tokens=10, output_tokens=20),
              _line("m2", input_tokens=10, output_tokens=20)])
    assert usage.transcript_usage(db, str(t)).input_tokens == 20

    t.write_text(_line("m3", input_tokens=1, output_tokens=2))  # truncated in place
    u = usage.transcript_usage(db, str(t))
    assert (u.input_tokens, u.output_tokens) == (1, 2)


def test_replaced_larger_file_is_reparsed_from_the_start(db, tmp_path):
    t = tmp_path / "sess.jsonl"
    write(t, [_line("m1", input_tokens=10, output_tokens=20)])
    assert usage.transcript_usage(db, str(t)).input_tokens == 10

    new = tmp_path / "new.jsonl"
    write(new, [_line("m2", input_tokens=1, output_tokens=2),
                _line("m3", input_tokens=1, output_tokens=2),
                _line("m4", input_tokens=1, output_tokens=2)])
    assert new.stat().st_size > t.stat().st_size
    new.replace(t)  # a new inode, larger than what was cached

    u = usage.transcript_usage(db, str(t))
    assert (u.input_tokens, u.output_tokens) == (3, 6)


def test_model_label_is_the_main_transcripts_and_skips_synthetic(db, tmp_path):
    t = tmp_path / "sess.jsonl"
    write(t, [_line("m1", input_tokens=1, model="claude-opus-4-7"),
              _line("m2", input_tokens=1, model="<synthetic>")])
    sub = tmp_path / "sess" / "subagents" / "agent-abc.jsonl"
    write(sub, [_line("m3", input_tokens=1, model="claude-haiku-4-5")])
    u = usage.transcript_usage(db, str(t))
    assert u.input_tokens == 3
    assert u.model == "claude-opus-4-7"


def test_malformed_usage_values_are_skipped(db, tmp_path):
    t = tmp_path / "sess.jsonl"
    bad = json.dumps({"type": "assistant",
                      "message": {"id": "bad", "usage": {"input_tokens": "lots"}}}) + "\n"
    write(t, [bad, _line("m1", input_tokens=5, output_tokens=6)])
    u = usage.transcript_usage(db, str(t))
    assert (u.input_tokens, u.output_tokens) == (5, 6)


def test_unreadable_file_does_not_raise(db, tmp_path, monkeypatch):
    t = tmp_path / "sess.jsonl"
    write(t, [_line("m1", input_tokens=5)])

    def boom(*a, **k):
        raise PermissionError("nope")

    monkeypatch.setattr(Path, "open", boom)
    u = usage.transcript_usage(db, str(t))
    assert u is not None and u.total == 0


def test_no_summary_line_when_usage_is_empty(db, repo, monkeypatch):
    from copse import workspaces

    ws = workspaces.create(db, str(repo), "feature").workspace
    t = repo.parent / "empty.jsonl"
    t.write_text("")
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "boss", "assign", "processing", "@1",
                       None, time.time(), transcript_path=str(t)))
    monkeypatch.setattr(agents, "is_alive", lambda a: True)

    agents.report_result(db, "w1", "done")
    assert "tokens:" not in db.pop_pending("boss").body


def test_agent_entry_survives_a_broken_usage_read(db, repo, monkeypatch):
    from copse import workspaces

    ws = workspaces.create(db, str(repo), "feature").workspace
    a = Agent("a1", ws.id, "developer", "claude", None, "assign", "idle", "", "done", time.time(),
             transcript_path="/wherever.jsonl")
    db.add_agent(a)

    def boom(db, agent):
        raise RuntimeError("boom")

    monkeypatch.setattr(view.usage_mod, "agent_usage", boom)

    entry = view.agent_entry(db, db.get_agent("a1"))
    assert "tokens" not in entry


def test_hook_ignores_transcript_path_for_non_claude_providers(db, repo):
    from copse import workspaces

    ws = workspaces.create(db, str(repo), "feature").workspace
    db.add_agent(Agent("a1", ws.id, "developer", "antigravity", None, "assign", "idle", "", None,
                       time.time()))
    agents.handle_hook(db, "a1", "prompt-submit", {"transcript_path": "/tmp/x/a1.jsonl"})
    assert db.get_agent("a1").transcript_path is None
