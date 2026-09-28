"""Finding your way around the sidebar: the `?` key list, rows that need you
(and `n` to reach them), folding workspaces, `/` to filter, a selection that
follows its agent, and text that fits a 30-column pane. Everything here runs
against ``render`` and ``handle_key``, so no curses screen is needed."""

import curses
import time

from copse import autopilot, view, watch, workspaces
from copse.db import Agent

# The sidebar pane is 30 columns; text starts after the selection bar and
# stops one short of the edge (see watch._loop).
WIDTH = 28


def ws(agents, id="repo/feat", branch="feat", **kw):
    return {"id": id, "name": branch, "branch": branch, "base_branch": "main", "path": "/",
            "ahead": 0, "behind": 0, "dirty": 0, "agents": agents, **kw}


def agent(id, status="idle", profile="developer", **kw):
    return {"id": id, "profile": profile, "provider": "claude", "status": status,
            "mode": "assign", "status_since": 1000.0, "pending": 0, "reported": False,
            "window": "@1", **kw}


def press(state, snap, *keys, sidebar=True, visible=40):
    """Press ``keys`` in order, re-rendering between them like the loop
    does. Returns the last action and the lines it leaves on screen."""
    action = None
    for key in keys:
        lines = watch.render(snap, 1090, WIDTH, state=state)
        action = handle(state, key, lines, visible, sidebar)
    return action, watch.render(snap, 1090, WIDTH, state=state)


def handle(state, key, lines, visible, sidebar):
    return watch.handle_key(state, ord(key) if isinstance(key, str) else key, lines, visible,
                            sidebar)


def selected(state, lines):
    i = watch.selection(state, lines)
    return None if i is None else lines[i]


def selected_agent(state, lines):
    ln = selected(state, lines)
    return ln.agent["id"] if ln and ln.agent else None


def texts(lines):
    return [ln.text for ln in lines]


# -- 1. `?` and the key hint -------------------------------------------------


def test_help_overlay_lists_every_key_and_fits_the_sidebar():
    lines = watch.help_lines(WIDTH, in_tmux=True)
    text = "\n".join(texts(lines))
    for key in ("↑↓", "PgUp", "Home", "⏎", "p ", "x ", "n ", "Spc", "/ ", "r ", "? ", "q "):
        assert key in text, key
    assert "prefix L" in text
    assert all(len(t) <= WIDTH for t in texts(lines))


def test_question_mark_toggles_the_help_and_any_key_closes_it():
    state = watch.NavState()
    snap = [ws([agent("a1")])]
    press(state, snap, "?")
    assert state.help
    # A key pressed while the help is up only closes it: `q` doesn't quit.
    action, _ = press(state, snap, "q")
    assert not state.help and action is None
    press(state, snap, "?", "?")
    assert not state.help


def test_footer_hint_fits_and_points_at_the_help():
    hint = watch.footer(watch.NavState(), WIDTH)
    assert hint.style == "dim" and hint.text.endswith("? keys") and len(hint.text) <= WIDTH
    assert watch.footer(watch.NavState(), 8).text == "? keys"
    assert watch.footer(watch.NavState(), 3) is None


# -- 2. rows that need you ---------------------------------------------------


def test_what_needs_you():
    w = ws([])
    assert watch.needs_you(agent("a", "waiting"), w) == "needs you"
    assert watch.needs_you(agent("a", reported=True), w) == "to review"
    assert watch.needs_you(agent("a", reported=True), {**w, "review": "changes"}) == "changes requested"
    assert watch.needs_you(agent("a", reported=True), {**w, "review": "approved"}) is None
    assert watch.needs_you(agent("a", "processing", reported=True), w) is None  # still going
    assert watch.needs_you(agent("a", mode="interactive", reported=True), w) is None
    assert watch.needs_you(agent("a"), w) is None
    assert watch.needs_you(agent("sup", mode="interactive"), {**w, "asking": "sup"}) == "has a question"


def test_under_autopilot_only_a_human_prompt_or_question_needs_you():
    w = ws([], autopilot=True)
    assert watch.needs_you(agent("a", reported=True), w) is None
    assert watch.needs_you(agent("a", reported=True), {**w, "review": "changes"}) is None
    assert watch.needs_you(agent("a", "waiting"), w) == "needs you"
    assert watch.needs_you(agent("sup", mode="interactive"), {**w, "asking": "sup"}) == "has a question"
    assert watch.awaiting_review(agent("a", reported=True), w) == "to review"


def test_under_autopilot_a_report_is_quiet_unpinned_and_skipped_by_n():
    snap = [ws([agent("busy01", "processing"), agent("done01", reported=True),
                agent("wait01", "waiting")], id="w1", autopilot=True),
            ws([agent("sup001", mode="interactive")], id="w0", branch="main", autopilot=True,
               asking="sup001")]
    lines = watch.render(snap, 1090, WIDTH)
    order = [ln.agent["id"] for ln in lines if ln.agent]
    assert order[:3] == ["wait01", "busy01", "done01"]  # the report keeps its place
    done = next(ln for ln in lines if ln.agent and ln.agent["id"] == "done01")
    assert done.text.startswith("  ◇ ") and done.style == "dim" and not done.needs
    sup = next(ln for ln in lines if ln.agent and ln.agent["id"] == "sup001")
    assert sup.text.startswith("  ◆ ") and sup.needs
    state = watch.NavState()
    seen = [selected_agent(state, press(state, snap, "n")[1]) for _ in range(3)]
    assert seen == ["sup001", "wait01", "sup001"]


def test_rows_needing_you_are_marked_and_pinned_to_the_top_of_their_group():
    snap = [ws([agent("busy01", "processing"), agent("done01", reported=True),
                agent("wait01", "waiting")])]
    lines = watch.render(snap, 1090, WIDTH)
    order = [ln.agent["id"] for ln in lines if ln.agent]
    assert order[:2] == ["done01", "wait01"] or order[:2] == ["wait01", "done01"]
    assert order[2] == "busy01"
    for ln in lines:
        if ln.agent and ln.agent["id"] != "busy01":
            assert ln.needs and ln.style == "alert" and ln.text.startswith("  ◆ ")
    assert lines[0].text.startswith("2 need you")


def test_n_jumps_to_the_next_row_needing_you_and_wraps():
    snap = [ws([agent("a1")], id="w1", branch="one"),
            ws([agent("w2", "waiting"), agent("b2")], id="w2", branch="two"),
            ws([agent("w3", "waiting")], id="w3", branch="three")]
    state = watch.NavState()
    _, lines = press(state, snap, "n")
    assert selected_agent(state, lines) == "w2"
    _, lines = press(state, snap, "n")
    assert selected_agent(state, lines) == "w3"
    _, lines = press(state, snap, "n")
    assert selected_agent(state, lines) == "w2"


def test_n_lands_on_a_folded_group_hiding_one_and_says_when_nothing_needs_you():
    snap = [ws([agent("a1")], id="w1", branch="one"),
            ws([agent("w2", "waiting")], id="w2", branch="two")]
    state = watch.NavState(collapsed={"w2"})
    _, lines = press(state, snap, "n")
    assert selected(state, lines).group == "w2"

    quiet = watch.NavState()
    press(quiet, [ws([agent("a1")])], "n")
    assert quiet.notice == "nothing needs you"


# -- 3. groups ----------------------------------------------------------------


def test_each_workspace_is_a_selectable_group_header():
    snap = [ws([agent("a1")], id="w1", branch="one"), ws([agent("b1")], id="w2", branch="two")]
    lines = watch.render(snap, 1090, WIDTH)
    headers = [ln for ln in lines if ln.group]
    assert [h.text for h in headers] == ["▾ one", "▾ two"]
    assert [h.group for h in headers] == ["w1", "w2"]
    # Moving up from the first agent reaches its header.
    state = watch.NavState()
    _, lines = press(state, snap, curses.KEY_UP)
    assert selected(state, lines).group == "w1"


def test_space_folds_the_group_and_shows_its_counts():
    snap = [ws([agent("a1"), agent("a2", "waiting"), agent("a3")], id="w1", branch="one"),
            ws([agent("b1")], id="w2", branch="two")]
    state = watch.NavState()
    _, lines = press(state, snap, " ")
    assert state.collapsed == {"w1"}
    header = selected(state, lines)
    assert header.group == "w1" and header.text == "▸ one (3) 1◆"
    assert header.style == "alert" and header.needs == 1
    assert not any(ln.agent and ln.agent["id"].startswith("a") for ln in lines)
    assert any(ln.agent and ln.agent["id"] == "b1" for ln in lines)  # others untouched

    _, lines = press(state, snap, "\t")  # Tab unfolds it again
    assert state.collapsed == set()
    assert any(ln.agent and ln.agent["id"] == "a1" for ln in lines)


def test_enter_on_a_header_folds_it_and_on_an_agent_attaches():
    snap = [ws([agent("a1")], id="w1")]
    state = watch.NavState()
    assert press(state, snap, 10)[0] == "attach"
    press(state, snap, curses.KEY_UP)
    action, _ = press(state, snap, 10)
    assert action is None and state.collapsed == {"w1"}
    # p and x do nothing on a header.
    assert press(state, snap, "p")[0] is None and press(state, snap, "x")[0] is None


def test_fold_state_survives_refreshes():
    state = watch.NavState()
    snap = [ws([agent("a1")], id="w1")]
    press(state, snap, " ")
    snap = [ws([agent("a1"), agent("a2")], id="w1")]  # a later snapshot
    lines = watch.render(snap, 1090, WIDTH, state=state)
    assert [ln.text for ln in lines if ln.group] == ["▸ feat (2)"]


# -- 4. `/` filter ------------------------------------------------------------


def test_slash_filters_by_branch_agent_or_profile_as_you_type():
    snap = [ws([agent("a1b2c3", profile="developer")], id="w1", branch="feat/login"),
            ws([agent("d4e5f6", profile="reviewer")], id="w2", branch="fix/crash")]
    state = watch.NavState()
    _, lines = press(state, snap, "/", "l", "o", "g")
    assert state.filtering and state.filter == "log"
    assert [ln.group for ln in lines if ln.group] == ["w1"]

    _, lines = press(state, snap, *[curses.KEY_BACKSPACE] * 3, *"review")
    assert [ln.agent["id"] for ln in lines if ln.agent] == ["d4e5f6"]

    _, lines = press(state, snap, *[127] * 6, *"a1b2")
    assert [ln.agent["id"] for ln in lines if ln.agent] == ["a1b2c3"]

    _, lines = press(state, snap, *[127] * 4, *"zzz")
    assert "Nothing matches" in lines[-1].text


def test_typing_q_into_the_filter_does_not_quit():
    state = watch.NavState()
    action, _ = press(state, [ws([agent("a1")])], "/", "q")
    assert action is None and state.filter == "q"


def test_enter_keeps_the_filter_and_esc_clears_it_without_quitting():
    snap = [ws([agent("a1")], id="w1", branch="one"), ws([agent("b1")], id="w2", branch="two")]
    state = watch.NavState()
    press(state, snap, "/", *"two", 10)
    assert not state.filtering and state.filter == "two"
    assert watch.footer(state, WIDTH).text.startswith("/two")
    action, lines = press(state, snap, 27)
    assert action is None and state.filter == ""
    assert len([ln for ln in lines if ln.group]) == 2
    # With no filter left, Esc still doesn't quit the sidebar; only q does.
    assert press(state, snap, 27)[0] is None
    assert press(state, snap, "q")[0] == "quit"


def test_esc_while_typing_clears_the_filter():
    state = watch.NavState()
    action, _ = press(state, [ws([agent("a1")])], "/", *"ab", 27)
    assert action is None and state.filter == "" and not state.filtering


def test_esc_still_quits_the_full_screen_dashboard_once_the_filter_is_gone():
    state = watch.NavState(filter="x")
    snap = [ws([agent("a1")])]
    assert press(state, snap, 27, sidebar=False)[0] is None and state.filter == ""
    assert press(state, snap, 27, sidebar=False)[0] == "quit"


# -- 5. the selection follows its agent --------------------------------------


def test_selection_stays_on_the_same_agent_when_rows_reorder():
    state = watch.NavState()
    snap = [ws([agent("a1"), agent("a2"), agent("a3")])]
    _, lines = press(state, snap, "j", "j")
    assert selected_agent(state, lines) == "a3"
    # a1 now needs you, so it jumps to the top; a3 is still the one selected.
    snap = [ws([agent("a1", "waiting"), agent("a2"), agent("a3")])]
    lines = watch.render(snap, 1090, WIDTH, state=state)
    assert selected_agent(state, lines) == "a3"
    # And across workspaces reordering.
    snap = [ws([agent("x1")], id="other", branch="other"), *snap]
    lines = watch.render(snap, 1090, WIDTH, state=state)
    assert selected_agent(state, lines) == "a3"


def test_a_vanished_agent_hands_the_selection_to_its_neighbour():
    state = watch.NavState()
    snap = [ws([agent("a1"), agent("a2"), agent("a3")])]
    press(state, snap, "j")
    lines = watch.render([ws([agent("a1"), agent("a3")])], 1090, WIDTH, state=state)
    assert selected_agent(state, lines) == "a3"


def test_first_draw_selects_the_first_agent_not_a_header():
    state = watch.NavState()
    lines = watch.render([ws([agent("a1")])], 1090, WIDTH, state=state)
    assert selected_agent(state, lines) == "a1"


def test_page_keys_move_the_selection_onto_the_new_page():
    snap = [ws([agent(f"a{i}")], id=f"w{i}", branch=f"b{i}") for i in range(10)]
    state = watch.NavState()
    _, lines = press(state, snap, curses.KEY_NPAGE, visible=8)
    i = watch.selection(state, lines)
    assert state.offset > 0 and state.offset <= i < state.offset + 8
    _, lines = press(state, snap, curses.KEY_HOME, visible=8)
    assert state.offset == 0


# -- 6. fitting 30 columns ----------------------------------------------------


def test_long_branch_names_are_elided_in_the_middle():
    assert watch.elide_middle("feat/a-very-long-branch-name-indeed", 16) == "feat/a-v…-indeed"
    assert watch.elide_middle("short", 16) == "short"
    branch = "feat/sidebar-navigation-and-a-lot-more-words-v2"
    lines = watch.render([ws([agent("a1")], branch=branch)], 1090, WIDTH)
    header = next(ln for ln in lines if ln.group)
    assert len(header.text) == WIDTH
    assert header.text.startswith("▾ feat/sidebar") and header.text.endswith("words-v2")
    assert "…" in header.text


def test_folded_long_branch_keeps_its_counts_in_view():
    branch = "feat/sidebar-navigation-and-a-lot-more-words-v2"
    state = watch.NavState(collapsed={"w1"})
    lines = watch.render([ws([agent("a1", "waiting"), agent("a2")], id="w1", branch=branch)],
                         1090, WIDTH, state=state)
    header = next(ln for ln in lines if ln.group)
    assert len(header.text) <= WIDTH and header.text.endswith(" (2) 1◆")


def test_your_checkout_tag_shortens_before_the_branch_does():
    root = ws([agent("a1")], name="root", branch="main")
    assert watch.render([root], 1090, 40)[3].text == "▾ main  (your checkout)"
    assert watch.render([root], 1090, 16)[3].text == "▾ main (yours)"


def test_nothing_is_wider_than_the_pane_or_split_mid_word():
    snap = [ws([agent("a1b2c3d4", "waiting", profile="very-long-profile-name-for-a-worker",
                      pending=12, tokens="1.2M tokens · $3.40",
                      subagents=[{"id": "s", "agent_type": "general-purpose-explorer",
                                  "started_at": 1000, "ended_at": None}])],
               branch="feat/some-extremely-long-branch-name", ahead=12, behind=3, dirty=40,
               name="root")]
    for state in (watch.NavState(), watch.NavState(collapsed={"repo/feat"})):
        lines = watch.render(snap, 1090, WIDTH, state=state)
        assert all(len(ln.text) <= WIDTH for ln in lines), [ln.text for ln in lines if len(ln.text) > WIDTH]
    lines = watch.render(snap, 1090, WIDTH)
    words = {w for ln in lines for w in ln.text.split()}
    # Hyphenated words stay whole instead of breaking at a hyphen.
    assert "general-purpose-explorer" in words
    assert all(not w.endswith("-") for w in words)


def test_wrap_elides_a_word_too_long_for_any_line():
    out = watch._wrap("see supercalifragilisticexpialidocious-and-more", 20, "  ")
    assert all(len(t) <= 20 for t in out)
    assert out[-1].endswith("…")


# -- the review verdict the markers rely on -----------------------------------


def test_snapshot_carries_the_latest_review_verdict(db, repo):
    w = workspaces.create(db, str(repo), "reviewed").workspace
    entry = lambda: next(e for e in view.snapshot(db, str(repo)) if e["id"] == w.id)  # noqa: E731
    assert entry()["review"] is None
    db.add_review(w.id, "abc123", None, False, "needs tests")
    assert entry()["review"] == "changes"
    db.add_review(w.id, "def456", None, True, "lgtm")
    assert entry()["review"] == "approved"


def test_last_review_uses_an_index(db):
    plan = " ".join(r["detail"] for r in db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM reviews WHERE workspace_id=? ORDER BY id DESC LIMIT 1",
        ("w",)))
    assert "reviews_workspace_id" in plan


def test_snapshot_carries_the_sessions_autopilot_and_open_question(db, repo):
    w = workspaces.create(db, str(repo), "auto").workspace
    now = time.time()
    db.add_agent(Agent("root01", w.id, "supervisor", "claude", None, "interactive", "idle",
                       "%991", None, now))
    db.add_agent(Agent("work01", w.id, "developer", "claude", "root01", "assign", "idle",
                       "%992", "done", now))
    entry = lambda: next(e for e in view.snapshot(db, str(repo)) if e["id"] == w.id)  # noqa: E731
    assert (entry()["autopilot"], entry()["asking"]) == (False, None)
    db.add_autopilot("root01")
    assert (entry()["autopilot"], entry()["asking"]) == (True, None)
    autopilot.need_user(db, "root01", "Which database?")
    assert (entry()["autopilot"], entry()["asking"]) == (True, "root01")
    autopilot.set_enabled(db, "root01", False)
    assert entry()["autopilot"] is False
