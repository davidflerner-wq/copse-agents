import json
import subprocess
import sys


def test_stdio_server_starts_and_lists_tools(tmp_path):
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    proc = subprocess.Popen(
        [sys.executable, "-m", "copse", "mcp"], cwd=tmp_path, text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        for m in msgs:
            proc.stdin.write(json.dumps(m) + "\n")
            proc.stdin.flush()
        reply = None
        for line in proc.stdout:
            r = json.loads(line)
            if r.get("id") == 2:
                reply = r
                break
    finally:
        proc.kill()
    assert reply, proc.stderr.read()
    names = {t["name"] for t in reply["result"]["tools"]}
    assert {"handoff", "assign", "send_message", "report_result", "workspace_diff",
            "merge_workspace", "remove_workspace", "wait_for_worker"} <= names
