"""Selecting and copying chat text (issue #12): pane-scoped mouse copy to the
system clipboard, the sidebar hide/show toggle, and the bottom layout."""

import json
import uuid

import pytest

from copse import config, tmux, watch


@pytest.fixture
def session(tmp_path):
    name = f"sel-{uuid.uuid4().hex[:6]}"
    tmux.ensure_session(name, str(tmp_path), {})
    yield name
    tmux.kill_session(name)


def test_clipboard_command_prefers_pbcopy(monkeypatch):
    monkeypatch.setattr(tmux.shutil, "which", lambda n: f"/bin/{n}" if n in ("pbcopy", "xclip") else None)
    assert tmux.clipboard_command() == "pbcopy"


def test_clipboard_command_linux(monkeypatch):
    monkeypatch.setattr(tmux.shutil, "which", lambda n: f"/bin/{n}" if n in ("wl-copy", "xclip") else None)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert tmux.clipboard_command() == "wl-copy"
    monkeypatch.delenv("WAYLAND_DISPLAY")
    assert tmux.clipboard_command() == "xclip -selection clipboard -i"


def test_clipboard_command_none(monkeypatch):
    monkeypatch.setattr(tmux.shutil, "which", lambda n: None)
    assert tmux.clipboard_command() is None


def _bindings(table: str) -> str:
    return tmux._tmux("list-keys", "-T", table).stdout


def test_mouse_drag_copies_to_clipboard_only_for_copse_sessions(session, monkeypatch):
    monkeypatch.setattr(tmux, "clipboard_command", lambda: "pbcopy")
    tmux.bind_session_keys(session)
    for table in ("copy-mode", "copy-mode-vi"):
        line = next(ln for ln in _bindings(table).splitlines() if "MouseDragEnd1Pane" in ln)
        assert "#{@copse}" in line and "pbcopy" in line
        assert "copy-pipe-and-cancel" in line  # the fallback keeps tmux's own default
    got = tmux._tmux("show-options", "-t", session, "@copse").stdout
    assert "@copse 1" in got


def test_no_clipboard_tool_keeps_default_binding(session, monkeypatch):
    monkeypatch.setattr(tmux, "clipboard_command", lambda: None)
    tmux.bind_session_keys(session)
    line = next(ln for ln in _bindings("copy-mode").splitlines() if "MouseDragEnd1Pane" in ln)
    assert "pbcopy" not in line and "copy-pipe-and-cancel" in line


def _zoomed(window: str) -> str:
    return tmux._tmux("display-message", "-p", "-t", window, "#{window_zoomed_flag}").stdout.strip()


def test_toggle_sidebar_hides_and_restores(session, tmp_path):
    chat = tmux.new_window(session, "chat", str(tmp_path), ["sleep", "60"], {})
    side = tmux.split_left(chat, str(tmp_path), ["sleep", "60"], {})
    window = tmux.pane_window(side)
    tmux._tmux("select-pane", "-t", side)  # focus is in the sidebar, as when h is pressed
    tmux.toggle_sidebar(side)
    assert _zoomed(window) == "1"
    assert tmux._tmux("display-message", "-p", "-t", window, "#{pane_id}").stdout.strip() == chat
    tmux.toggle_sidebar(side)
    assert _zoomed(window) == "0"
    assert tmux.window_alive(side)  # still running the whole time


def test_prefix_s_binding_is_guarded_to_copse_sessions(session):
    tmux.bind_session_keys(session)
    cmd = next(ln for ln in _bindings("prefix").splitlines() if " S " in ln)
    assert "#{@copse}" in cmd and "resize-pane -Z" in cmd


def test_bottom_layout_puts_sidebar_under_the_chat(session, tmp_path):
    chat = tmux.new_window(session, "chat", str(tmp_path), ["sleep", "60"], {})
    side = tmux.split_left(chat, str(tmp_path), ["sleep", "60"], {}, columns=30, position="bottom")

    def geom(p):
        return tuple(int(x) for x in tmux._tmux(
            "display-message", "-p", "-t", p, "#{pane_left} #{pane_top} #{pane_width}").stdout.split())

    (cl, ct, cw), (sl, st, sw) = geom(chat), geom(side)
    assert st > ct and sl == cl and sw == cw


def test_sidebar_option_is_read_from_config(tmp_path):
    assert config.load_repo_config(tmp_path).sidebar == "left"
    (tmp_path / ".copse").mkdir()
    (tmp_path / ".copse" / "config.json").write_text(json.dumps({"sidebar": "bottom"}))
    assert config.load_repo_config(tmp_path).sidebar == "bottom"


def test_legend_mentions_hide_key_and_zoom():
    text = "\n".join(line.text for line in watch.help_lines(30, in_tmux=True, sidebar=True))
    assert "hide" in text and "prefix S" in text and "prefix z" in text
    plain = "\n".join(line.text for line in watch.help_lines(30, in_tmux=True, sidebar=False))
    assert "hide" not in plain


def test_h_key_returns_hide_only_in_the_sidebar():
    state = watch.NavState()
    assert watch.handle_key(state, ord("h"), [], 10, sidebar=True) == "hide"
    assert watch.handle_key(state, ord("h"), [], 10, sidebar=False) is None
