import json
import os
import time
from types import SimpleNamespace

from copse import git, mcp_server


def test_untracked_summary_collapses_noise_and_dirs():
    files = [f".venv/lib/f{i}.py" for i in range(500)] + [
        "pkg/a/b.py", "pkg/c.py", "notes.txt", "web/node_modules/x/y.js", "web/src/z.js"]
    text = git.summarize_untracked(files)
    assert "Untracked files (505)" in text
    assert ".venv/ (500 files)" in text
    assert "pkg/ (2 files)" in text
    assert "web/node_modules/ (1 file)" in text
    assert "  notes.txt" in text
    assert len(text.splitlines()) < 10


def test_untracked_summary_caps_listing():
    text = git.summarize_untracked([f"d{i}/f" for i in range(100)])
    assert len(text.splitlines()) <= git.MAX_UNTRACKED_LISTED + 2
    assert "and 80 more" in text


def test_last_activity_claude_transcript(tmp_path):
    t = tmp_path / "t.jsonl"
    lines = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"},
                                                       {"type": "tool_use", "name": "WebFetch"}]}},
        {"type": "user", "message": {"content": "hi"}},
    ]
    t.write_text("\n".join(json.dumps(x) for x in lines) + "\n{broken")
    old = time.time() - 40
    os.utime(t, (old, old))
    out = mcp_server._last_activity(SimpleNamespace(transcript_path=str(t)))
    assert out.startswith(" last: WebFetch 4") and out.endswith("s ago")


def test_last_activity_native_and_missing(tmp_path):
    t = tmp_path / "n.jsonl"
    t.write_text(json.dumps({"type": "assistant", "tool_calls": [{"name": "bash"}]}) + "\n")
    assert " last: bash " in mcp_server._last_activity(SimpleNamespace(transcript_path=str(t)))
    assert mcp_server._last_activity(SimpleNamespace(transcript_path=None)) == ""
    assert mcp_server._last_activity(SimpleNamespace(transcript_path=str(tmp_path / "no"))) == ""
