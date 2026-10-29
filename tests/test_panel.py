import time
"""Tests for panel rendering decisions that do not depend on live sessions."""
import tkinter as tk

import pytest

from claude_eyes import panel as panel_module
from claude_eyes import sound as sound_module


@pytest.fixture()
def live_panel():
    try:
        root = tk.Tk()
    except tk.TclError:  # no display available
        pytest.skip("Tk display unavailable")
    root.withdraw()
    instance = panel_module.Panel(root)
    yield instance
    try:
        instance.destroy()
    except tk.TclError:
        root.destroy()


def _session(sid, kind, title):
    return sid, {
        "status": "working", "session_id": sid, "cwd": "/Users/x/projects/demo",
        "project_name": "demo", "conversation_title": title,
        "last_prompt": "问题", "agent_kind": kind,
        "prompt_time": "2026-08-24T14:00:00+00:00", "subagents": [],
    }


def _icon_items(entry):
    return entry._icon.find_withtag("agent-icon")


def test_each_agent_draws_its_own_mark_beside_the_title(live_panel):
    """Claude Code's robot and Codex's knot, from their official SVGs."""
    live_panel.upsert(*_session("cc-1", "claude", "改登录"))
    live_panel.upsert(*_session("cx-1", "codex", "cc_lights_2"))
    claude_entry = live_panel._entries["cc-1"]
    codex_entry = live_panel._entries["cx-1"]

    claude_items = _icon_items(claude_entry)
    codex_items = _icon_items(codex_entry)
    # One outline plus two holes each: the robot's eyes, Codex's ">_".
    assert len(claude_items) == 3
    assert len(codex_items) == 3
    assert claude_entry._icon.itemcget(claude_items[0], "fill") == \
        panel_module.AGENT_ICON_FG["claude"]
    assert codex_entry._icon.itemcget(codex_items[0], "fill") == \
        panel_module.AGENT_ICON_FG["codex"]
    # Holes are painted in the row background, which Tk cannot punch out.
    assert claude_entry._icon.itemcget(claude_items[1], "fill") == panel_module.ROW_BG
    # The two marks are genuinely different shapes, not a recoloured one.
    assert len(claude_entry._icon.coords(claude_items[0])) != \
        len(codex_entry._icon.coords(codex_items[0]))


def test_the_light_itself_stays_a_circle_for_every_agent(live_panel):
    """Status owns the light column; the agent mark owns identity."""
    live_panel.upsert(*_session("cc-1", "claude", "改登录"))
    live_panel.upsert(*_session("cx-1", "codex", "cc_lights_2"))

    for sid in ("cc-1", "cx-1"):
        entry = live_panel._entries[sid]
        assert entry._canvas.type(entry._dot) == "oval"
        assert entry._canvas.type(entry._ring) == "oval"


def test_the_mark_is_redrawn_at_the_new_panel_size(live_panel):
    live_panel.upsert(*_session("cx-1", "codex", "cc_lights_2"))
    entry = live_panel._entries["cx-1"]
    live_panel._set_panel_size("large")
    large = entry._icon.coords(_icon_items(entry)[0])
    live_panel._set_panel_size("small")
    small = entry._icon.coords(_icon_items(entry)[0])

    assert max(large) > max(small)


def test_the_mark_survives_collapsing_to_dots_only(live_panel):
    """Horizontal layout hides titles, so the mark is the only agent cue left."""
    live_panel.upsert(*_session("cx-1", "codex", "cc_lights_2"))
    entry = live_panel._entries["cx-1"]
    entry.hide_details()

    assert entry._title_row.grid_info() != {}
    assert entry._lbl_name.winfo_manager() == ""
    assert _icon_items(entry)  # the mark is still drawn


def test_unknown_agent_kind_falls_back_to_claude(live_panel):
    sid, data = _session("weird-1", "claude", "x")
    data["agent_kind"] = ""
    live_panel.upsert(sid, data)

    assert live_panel._entries[sid].agent_kind == "claude"


# ======================================================================
# audio cues
# ======================================================================
def _row(sid, status, subagents=None):
    return sid, {
        "status": status, "session_id": sid, "cwd": "/Users/x/projects/demo",
        "project_name": "demo", "last_prompt": "问题", "agent_kind": "claude",
        "prompt_time": "2026-08-24T14:00:00+00:00",
        "subagents": subagents or [],
    }


@pytest.fixture()
def audible_panel(live_panel, monkeypatch):
    """A panel whose cues are past warm-up and recorded instead of played."""
    played = []
    monkeypatch.setattr(
        sound_module, "play", lambda status: played.append(status) or True
    )
    live_panel._sound._started_at -= 10.0
    live_panel._sound_enabled = True
    live_panel._sound.enabled = True
    return live_panel, played


def test_a_session_turning_yellow_or_red_is_heard(audible_panel):
    instance, played = audible_panel
    instance.upsert(*_row("s1", "working"))
    assert played == []
    instance.upsert(*_row("s1", "waiting"))
    instance.upsert(*_row("s2", "error"))
    assert played == ["waiting", "error"]


def test_the_poll_loop_does_not_repeat_the_cue(audible_panel):
    """upsert runs for every session every 200 ms."""
    instance, played = audible_panel
    for _ in range(20):
        instance.upsert(*_row("s1", "waiting"))
    assert played == ["waiting"]


def test_a_failing_background_command_is_heard_under_a_working_session(audible_panel):
    instance, played = audible_panel
    task = {"agent_id": "task:abc", "kind": "task", "name": "构建", "status": "working"}
    instance.upsert(*_row("s1", "working", [task]))
    assert played == []
    instance.upsert(*_row("s1", "working", [dict(task, status="error")]))
    assert played == ["error"]


def test_muting_from_the_menu_silences_and_persists(audible_panel):
    instance, played = audible_panel
    instance._toggle_sound()
    instance.upsert(*_row("s1", "waiting"))
    assert played == []
    assert instance._load_config()["sound_enabled"] is False
    instance._toggle_sound()
    instance.upsert(*_row("s2", "error"))
    assert played == ["error"]
    assert instance._load_config()["sound_enabled"] is True


def test_a_reopened_session_id_alerts_again(audible_panel):
    """Closing a row must drop its remembered status, not suppress the next one."""
    instance, played = audible_panel
    instance.upsert(*_row("s1", "waiting"))
    instance.remove("s1")
    instance._sound._last_played.clear()  # past the cooldown
    instance.upsert(*_row("s1", "waiting"))
    assert played == ["waiting", "waiting"]


# ======================================================================
# off-screen rescue (unplugged external display)
# ======================================================================
BUILTIN = (0, 0, 1728, 1117)          # the laptop's own display
EXTERNAL_LEFT = (-1512, 0, 1512, 982)  # a monitor placed to its left


@pytest.fixture()
def stranded_panel(live_panel, monkeypatch):
    """A panel the user has dragged, with the display layout under test control."""
    live_panel._user_dragged = True
    live_panel._position = "bottom-right"
    anchored = []
    monkeypatch.setattr(live_panel, "_anchor_to", lambda pos: anchored.append(pos))
    return live_panel, anchored


def _place(instance, monkeypatch, x, y, w=464, h=600):
    monkeypatch.setattr(instance.win, "winfo_rootx", lambda: x)
    monkeypatch.setattr(instance.win, "winfo_rooty", lambda: y)
    monkeypatch.setattr(instance.win, "winfo_width", lambda: w)
    monkeypatch.setattr(instance.win, "winfo_height", lambda: h)
    monkeypatch.setattr(instance.win, "winfo_reqwidth", lambda: w)
    monkeypatch.setattr(instance.win, "winfo_reqheight", lambda: h)


def test_a_panel_left_behind_by_an_unplugged_display_is_brought_back(
    stranded_panel, monkeypatch
):
    """The reported bug: dragged onto an external monitor, then unplugged."""
    instance, anchored = stranded_panel
    monkeypatch.setattr(instance, "_screen_rects", lambda: [BUILTIN])
    _place(instance, monkeypatch, -1060, 555, 464, 1410)

    assert instance._is_stranded() is True
    instance._rescue_offscreen()
    assert anchored == ["bottom-right"]
    # Auto-positioning must resume, or the next poll leaves it stranded again.
    assert instance._user_dragged is False


def test_a_panel_on_a_still_connected_second_display_is_left_alone(
    stranded_panel, monkeypatch
):
    """The case that must not regress: deliberate multi-monitor placement."""
    instance, anchored = stranded_panel
    monkeypatch.setattr(instance, "_screen_rects", lambda: [BUILTIN, EXTERNAL_LEFT])
    _place(instance, monkeypatch, -1060, 555)

    assert instance._is_stranded() is False
    instance._rescue_offscreen()
    assert anchored == []
    assert instance._user_dragged is True


def test_a_panel_hanging_off_an_edge_is_left_alone(stranded_panel, monkeypatch):
    """Partly off-screen is a legitimate place to park it — still grabbable."""
    instance, anchored = stranded_panel
    monkeypatch.setattr(instance, "_screen_rects", lambda: [BUILTIN])
    _place(instance, monkeypatch, 1600, 900)  # 128px still showing

    assert instance._is_stranded() is False
    instance._rescue_offscreen()
    assert anchored == []


def test_a_sliver_too_thin_to_grab_counts_as_stranded(stranded_panel, monkeypatch):
    instance, _ = stranded_panel
    monkeypatch.setattr(instance, "_screen_rects", lambda: [BUILTIN])
    _place(instance, monkeypatch, 1728 - 10, 500)  # 10px showing, unclickable

    assert instance._is_stranded() is True


def test_an_undragged_panel_is_never_touched_by_the_rescue(live_panel, monkeypatch):
    """Auto-positioning already owns that case; the rescue must not fight it."""
    anchored = []
    monkeypatch.setattr(live_panel, "_anchor_to", lambda pos: anchored.append(pos))
    monkeypatch.setattr(live_panel, "_screen_rects", lambda: [BUILTIN])
    _place(live_panel, monkeypatch, -9000, -9000)
    live_panel._user_dragged = False

    live_panel._rescue_offscreen()
    assert anchored == []


def test_an_unknown_display_layout_never_moves_the_panel(stranded_panel, monkeypatch):
    """No NSScreen (non-macOS, or pyobjc missing) means don't guess."""
    instance, anchored = stranded_panel
    monkeypatch.setattr(instance, "_screen_rects", lambda: [])
    _place(instance, monkeypatch, -9000, -9000)

    assert instance._is_stranded() is False
    instance._rescue_offscreen()
    assert anchored == []


def test_screen_rects_reports_the_real_display_in_tk_coordinates(live_panel):
    """Cocoa's y-up origin must be flipped, or every rect misses the window."""
    rects = live_panel._screen_rects()
    if not rects:
        pytest.skip("NSScreen unavailable")
    width, height = live_panel.win.winfo_screenwidth(), live_panel.win.winfo_screenheight()
    # The primary display sits at Tk's origin and matches what Tk reports.
    assert (0, 0, width, height) in rects


# ======================================================================
# closing a row from the menu
# ======================================================================
def _live_row(sid, stamp="2026-08-29T06:11:36+00:00"):
    return sid, {
        "status": "working", "session_id": sid, "cwd": "/Users/x/projects/Skill_test",
        "project_name": "Skill_test", "last_prompt": "问题", "agent_kind": "claude",
        "timestamp": stamp, "prompt_time": stamp, "subagents": [],
    }


@pytest.fixture()
def panel_with_status_dir(live_panel, monkeypatch, tmp_path):
    """A panel whose close action writes to a throwaway status dir."""
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    monkeypatch.setattr(panel_module, "STATUS_DIR", status_dir)
    return live_panel, status_dir


def test_closing_a_row_survives_the_next_poll(panel_with_status_dir):
    """The bug: the row came straight back on the next 200 ms scan."""
    instance, status_dir = panel_with_status_dir
    sid, data = _live_row("s1")
    (status_dir / f"{sid}.json").write_text("{}")

    instance.upsert(sid, data)
    instance.close_session(sid)
    assert sid not in instance._entries
    # The status file on disk is what the poll rebuilds the row from.
    assert not (status_dir / f"{sid}.json").exists()

    for _ in range(10):            # the poll keeps handing the same data back
        instance.upsert(sid, data)
    assert sid not in instance._entries


def test_a_closed_session_comes_back_when_it_does_something_new(
    panel_with_status_dir
):
    """Dismissal is per row-state, not a permanent mute of a live session."""
    instance, _ = panel_with_status_dir
    sid, data = _live_row("s1")
    instance.upsert(sid, data)
    instance.close_session(sid)
    instance.upsert(sid, data)
    assert sid not in instance._entries

    _, newer = _live_row("s1", stamp="2026-08-29T07:00:00+00:00")
    instance.upsert(sid, newer)
    assert sid in instance._entries


def test_closing_a_codex_row_sticks_without_a_status_file(panel_with_status_dir):
    """Codex rows are polled from its own transcripts — there is no file to delete."""
    instance, status_dir = panel_with_status_dir
    sid, data = _live_row("codex:abc")
    data["agent_kind"] = "codex"

    instance.upsert(sid, data)
    instance.close_session(sid)
    for _ in range(10):
        instance.upsert(sid, data)
    assert sid not in instance._entries
    assert list(status_dir.iterdir()) == []


def test_closing_one_row_leaves_the_others_alone(panel_with_status_dir):
    instance, status_dir = panel_with_status_dir
    for sid in ("s1", "s2", "s3"):
        (status_dir / f"{sid}.json").write_text("{}")
        instance.upsert(*_live_row(sid))

    instance.close_session("s2")
    for sid in ("s1", "s2", "s3"):
        instance.upsert(*_live_row(sid))
    assert set(instance._entries) == {"s1", "s3"}
    assert (status_dir / "s1.json").exists()
    assert (status_dir / "s3.json").exists()


def test_remove_still_leaves_the_status_file_alone(panel_with_status_dir):
    """_scan calls remove() for sessions that vanished — it must not delete."""
    instance, status_dir = panel_with_status_dir
    sid, data = _live_row("s1")
    (status_dir / f"{sid}.json").write_text("{}")
    instance.upsert(sid, data)

    instance.remove(sid)
    assert (status_dir / f"{sid}.json").exists()
    instance.upsert(sid, data)
    assert sid in instance._entries      # not dismissed, so it comes back


# ======================================================================
# completion cards: pooled so they never steal keyboard focus
# ======================================================================
def _completion(title="完成"):
    return {
        "conversation_title": title, "last_prompt": "问题",
        "elapsed_seconds": 12.5, "user_turns": 3, "input_tokens": 1200,
        "output_tokens": 340, "model": "claude-opus-5",
        "cwd": "/Users/x/projects/demo",
    }


class FakeNative:
    """Stands in for an NSWindow so the ordering path can be asserted headlessly."""

    def __init__(self):
        self.calls = []

    def orderFrontRegardless(self):
        self.calls.append("front")

    def orderOut_(self, _):
        self.calls.append("out")


def test_a_card_is_shown_without_going_through_tk_map(live_panel, monkeypatch):
    """Tk's map path activates the app on macOS, which is the focus theft."""
    live_panel._warm_popup_pool()
    for popup in live_panel._popups:
        popup.native = FakeNative()
    deiconified = []
    for popup in live_panel._popups:
        monkeypatch.setattr(popup.win, "deiconify",
                            lambda: deiconified.append(True))

    live_panel.show_completion(_completion())
    shown = live_panel._active_popups[0]
    assert shown.native.calls[-1] == "front"
    assert deiconified == [], "fell back to Tk's focus-stealing map path"


def test_without_appkit_it_falls_back_to_tk(live_panel, monkeypatch):
    """Non-macOS has no NSWindow to order around."""
    live_panel._warm_popup_pool()
    for popup in live_panel._popups:
        popup.native = None
    deiconified = []
    monkeypatch.setattr(live_panel._popups[0].win, "deiconify",
                        lambda: deiconified.append(True))

    live_panel.show_completion(_completion())
    assert deiconified == [True]


def test_windows_are_reused_rather_than_recreated(live_panel):
    """A fresh Toplevel per completion is exactly what steals focus."""
    live_panel._warm_popup_pool()
    created = len(live_panel._popups)

    for i in range(6):
        live_panel.show_completion(_completion(f"第{i}轮"))
        live_panel._dismiss_notification(live_panel._active_popups[0])

    assert len(live_panel._popups) == created


def test_dismissing_hides_the_window_but_keeps_it_usable(live_panel):
    live_panel._warm_popup_pool()
    for popup in live_panel._popups:
        popup.native = FakeNative()

    live_panel.show_completion(_completion())
    popup = live_panel._active_popups[0]
    live_panel._dismiss_notification(popup)

    assert popup.native.calls[-1] == "out"
    assert live_panel._active_popups == []
    assert popup in live_panel._popups
    assert popup.win.winfo_exists()


def test_concurrent_cards_stack_up_to_the_pool_size(live_panel):
    live_panel._warm_popup_pool()
    for i in range(panel_module.NOTIFICATION_POOL):
        live_panel.show_completion(_completion(f"会话{i}"))
    assert len(live_panel._active_popups) == panel_module.NOTIFICATION_POOL
    # Stacked down the screen, not drawn on top of each other. The geometry
    # requests need one idle pass before Tk reports them back.
    live_panel._master.update_idletasks()
    ys = [int(p.win.winfo_geometry().rsplit("+", 1)[-1])
          for p in live_panel._active_popups]
    assert ys == sorted(ys) and len(set(ys)) == len(ys)


def test_the_oldest_card_is_recycled_when_the_pool_runs_out(live_panel):
    live_panel._warm_popup_pool()
    for i in range(panel_module.NOTIFICATION_POOL):
        live_panel.show_completion(_completion(f"会话{i}"))
    oldest = live_panel._active_popups[0]

    live_panel.show_completion(_completion("挤进来的"))
    assert len(live_panel._active_popups) == panel_module.NOTIFICATION_POOL
    assert live_panel._active_popups[-1] is oldest, "oldest was not the one reused"


def test_a_recycled_card_shows_no_trace_of_the_previous_one(live_panel):
    live_panel._warm_popup_pool()
    live_panel.show_completion(_completion("第一个会话"))
    popup = live_panel._active_popups[0]
    live_panel._dismiss_notification(popup)
    live_panel.show_completion(_completion("第二个会话"))

    texts = [
        popup.canvas.itemcget(item, "text")
        for item in popup.canvas.find_all()
        if popup.canvas.type(item) == "text"
    ]
    assert any("第二个会话" in t for t in texts)
    assert not any("第一个会话" in t for t in texts)


def test_recycling_a_card_cancels_its_old_dismissal_timer(live_panel):
    """Otherwise the previous turn's timer hides the card that replaced it."""
    live_panel._warm_popup_pool()
    for i in range(panel_module.NOTIFICATION_POOL):
        live_panel.show_completion(_completion(f"会话{i}"))
    victim = live_panel._active_popups[0]
    old_job = victim.dismiss_job

    live_panel.show_completion(_completion("挤进来的"))
    assert victim.dismiss_job is not None
    assert victim.dismiss_job != old_job


# ---------------------------------------------------------------- context meter


def test_context_view_reads_as_used_over_limit():
    ratio, text = panel_module._context_view(
        {"context_tokens": 90_000, "context_limit": 200_000}
    )
    assert ratio == pytest.approx(0.45)
    assert text == "90K/200K 45%"


def test_a_session_with_no_context_figure_gets_no_meter():
    """Codex rows and freshly-started sessions report nothing to measure."""
    assert panel_module._context_view({}) == (None, "")
    assert panel_module._context_view({"context_tokens": 0}) == (None, "")


def test_the_meter_changes_colour_as_the_window_fills():
    assert panel_module._context_color(0.40) == panel_module.CTX_FILL
    assert panel_module._context_color(0.75) == panel_module.CTX_FILL_WARN
    assert panel_module._context_color(0.95) == panel_module.CTX_FILL_ALERT


def test_session_facts_state_context_compaction_and_cost():
    facts = panel_module._session_facts({
        "context_tokens": 150_000,
        "context_limit": 200_000,
        "compact_count": 2,
        "compact_trigger": "auto",
        "compact_dropped": 380_000,
        "cost_usd": 12.3456,
    })
    assert facts == [
        "▤ 上下文 150K/200K 75%",
        "⟲ 已压缩 2 次（自动） 丢弃 380K",
        "≈ $12.35",
    ]


def test_a_plain_turn_adds_no_second_row_to_the_card():
    assert panel_module._session_facts({"user_turns": 3}) == []


def test_cost_is_only_shown_when_claude_code_actually_recorded_one():
    assert panel_module._format_cost(None) == ""
    assert panel_module._format_cost(0) == ""
    assert panel_module._format_cost(True) == ""  # a bool is not a figure


def test_a_row_draws_its_context_meter(live_panel):
    sid, data = _session("ctx-1", "claude", "长会话")
    data.update({"context_tokens": 180_000, "context_limit": 200_000})
    live_panel.upsert(sid, data)
    entry = live_panel._entries[sid]

    assert entry._ctx_ratio == pytest.approx(0.9)
    assert entry._lbl_ctx.cget("text") == "180K/200K 90%"
    assert entry._ctx_frame.grid_info()  # gridded, not hidden


def test_a_compacted_row_shows_how_many_times(live_panel):
    sid, data = _session("ctx-2", "claude", "压缩过的会话")
    data.update({
        "context_tokens": 20_000, "context_limit": 200_000, "compact_count": 3,
    })
    live_panel.upsert(sid, data)

    assert live_panel._entries[sid]._lbl_ctx.cget("text") == "20K/200K 10% ⟲3"


def test_a_row_without_a_context_figure_hides_the_meter(live_panel):
    sid, data = _session("ctx-3", "claude", "新会话")
    live_panel.upsert(sid, data)
    entry = live_panel._entries[sid]

    assert entry._ctx_ratio is None
    assert not entry._ctx_frame.grid_info()


def test_the_meter_appears_once_the_first_reply_lands(live_panel):
    """The bar has to be gridded when it shows up, not just on the next resize."""
    sid, data = _session("ctx-4", "claude", "会话")
    live_panel.upsert(sid, data)
    data = dict(data, context_tokens=50_000, context_limit=200_000)
    live_panel.upsert(sid, data)

    assert live_panel._entries[sid]._ctx_frame.grid_info()


def test_compacting_is_its_own_light(live_panel):
    sid, data = _session("ctx-5", "claude", "压缩中的会话")
    data["status"] = "compacting"
    live_panel.upsert(sid, data)
    entry = live_panel._entries[sid]

    assert entry._lbl_state.cget("text") == "压缩中"
    assert entry._color_rgb == panel_module.COLOR_MAP["compacting"]
    # It blocks the reply for minutes, so the light has to stay alive.
    assert entry._blinking


def test_cost_rides_on_the_context_line(live_panel):
    sid, data = _session("cost-1", "claude", "烧钱的会话")
    data.update({
        "context_tokens": 641_665, "context_limit": 1_000_000,
        "compact_count": 2, "cost_usd": 443.47,
    })
    live_panel.upsert(sid, data)
    entry = live_panel._entries[sid]

    assert entry._lbl_ctx.cget("text") == "642K/1.0M 64% ⟲2  ≈$443"


def test_the_bar_gives_up_width_instead_of_clipping_the_label(live_panel):
    """Tk's 378px default canvas width used to push the text off the row."""
    sid, data = _session("cost-2", "claude", "会话")
    data.update({
        "context_tokens": 641_665, "context_limit": 1_000_000,
        "compact_count": 2, "cost_usd": 443.47,
    })
    live_panel.upsert(sid, data)
    live_panel._master.update()
    entry = live_panel._entries[sid]

    assert entry._lbl_ctx.winfo_width() >= entry._lbl_ctx.winfo_reqwidth()
    assert entry._ctx_bar.winfo_width() > 20  # still a usable bar, not squeezed out


def test_row_cost_stays_readable_across_magnitudes():
    assert panel_module._format_cost_short(4.2) == "≈$4.2"
    assert panel_module._format_cost_short(47.514) == "≈$47.5"
    assert panel_module._format_cost_short(443.47) == "≈$443"
    assert panel_module._format_cost_short(12_345) == "≈$12.3K"
    assert panel_module._format_cost_short(None) == ""


def test_clicking_the_card_dismisses_it(live_panel):
    """The card is topmost in the corner; a click must clear it, not just wait."""
    live_panel._warm_popup_pool()
    live_panel.show_completion(_completion())
    assert len(live_panel._active_popups) == 1
    popup = live_panel._active_popups[0]

    popup.canvas.event_generate("<Button-1>", x=10, y=10)
    live_panel._master.update_idletasks()
    assert live_panel._active_popups == []


def test_a_dismissed_card_frees_up_for_reuse(live_panel):
    live_panel._warm_popup_pool()
    live_panel.show_completion(_completion("第一个"))
    popup = live_panel._active_popups[0]
    popup.win.event_generate("<Button-1>")
    live_panel._master.update_idletasks()
    # The window is reusable, not destroyed.
    assert popup in live_panel._popups
    assert popup.win.winfo_exists()


def test_the_card_names_the_agent_that_finished(live_panel):
    """A Codex turn must not announce itself as Claude Code."""
    live_panel._warm_popup_pool()

    def label_of(kind):
        live_panel.show_completion(dict(_completion(), agent_kind=kind))
        popup = live_panel._active_popups[-1]
        texts = [
            popup.canvas.itemcget(i, "text")
            for i in popup.canvas.find_all()
            if popup.canvas.type(i) == "text"
        ]
        return next(t for t in texts if "COMPLETED" in t)

    assert "CLAUDE CODE" in label_of("claude")
    assert "CODEX" in label_of("codex")
    assert "CLAUDE" not in label_of("codex")


# ======================================================================
# collapsing a project group
# ======================================================================
def _proj_row(sid, project, stamp="2026-09-09T10:00:00+00:00"):
    return sid, {
        "status": "working", "session_id": sid,
        "cwd": f"/Users/x/projects/{project}", "project_name": project,
        "last_prompt": "问题", "agent_kind": "claude",
        "prompt_time": stamp, "timestamp": stamp, "subagents": [],
    }


def _packed(entry):
    """Whether a session row is currently laid out (not folded away)."""
    return bool(entry.frame.winfo_manager())


def test_collapsing_a_project_hides_its_rows_but_keeps_the_header(live_panel):
    live_panel.upsert(*_proj_row("a1", "Alpha"))
    live_panel.upsert(*_proj_row("a2", "Alpha"))
    live_panel.upsert(*_proj_row("b1", "Beta"))
    live_panel._master.update_idletasks()
    assert all(_packed(live_panel._entries[s]) for s in ("a1", "a2", "b1"))

    live_panel._toggle_collapsed("Alpha")
    live_panel._master.update_idletasks()
    assert not _packed(live_panel._entries["a1"])
    assert not _packed(live_panel._entries["a2"])
    assert _packed(live_panel._entries["b1"]), "collapsing Alpha touched Beta"
    # The header stays, so there is something to click to expand again.
    assert live_panel._group_headers


def test_expanding_brings_the_rows_back(live_panel):
    live_panel.upsert(*_proj_row("a1", "Alpha"))
    live_panel._toggle_collapsed("Alpha")
    live_panel._toggle_collapsed("Alpha")
    live_panel._master.update_idletasks()
    assert _packed(live_panel._entries["a1"])


def test_the_collapsed_state_persists_to_config(live_panel):
    live_panel.upsert(*_proj_row("a1", "Alpha"))
    live_panel._toggle_collapsed("Alpha")
    assert live_panel._load_config()["collapsed"] == ["Alpha"]
    live_panel._toggle_collapsed("Alpha")
    assert live_panel._load_config()["collapsed"] == []


def test_a_collapsed_project_stays_collapsed_when_a_new_session_arrives(live_panel):
    live_panel.upsert(*_proj_row("a1", "Alpha"))
    live_panel._toggle_collapsed("Alpha")
    live_panel.upsert(*_proj_row("a2", "Alpha"))
    live_panel._master.update_idletasks()
    assert not _packed(live_panel._entries["a2"]), "new row appeared under a folded project"


def test_the_chevron_reflects_the_collapsed_state(live_panel):
    live_panel.upsert(*_proj_row("a1", "Alpha"))
    live_panel._master.update_idletasks()

    def chevrons():
        found = []
        for header in live_panel._group_headers:
            for child in header.winfo_children():
                if isinstance(child, tk.Label) and child.cget("text") in ("▸", "▾"):
                    found.append(child.cget("text"))
        return found

    assert chevrons() == ["▾"]
    live_panel._toggle_collapsed("Alpha")
    live_panel._master.update_idletasks()
    assert chevrons() == ["▸"]


def test_a_collapsed_group_from_a_previous_session_is_honored(live_panel, monkeypatch):
    """The set is loaded from config at startup, not only toggled at runtime."""
    live_panel._collapsed = {"Alpha"}
    live_panel.upsert(*_proj_row("a1", "Alpha"))
    live_panel.upsert(*_proj_row("b1", "Beta"))
    live_panel._repack_all()
    live_panel._master.update_idletasks()
    assert not _packed(live_panel._entries["a1"])
    assert _packed(live_panel._entries["b1"])


# ======================================================================
# wheel scrolling + chevron placement
# ======================================================================
def _fill(panel, n, project="Alpha"):
    for i in range(n):
        stamp = f"2026-09-09T10:{i % 60:02d}:00+00:00"
        panel.upsert(f"s{i}", {
            "status": "working", "session_id": f"s{i}",
            "cwd": f"/Users/x/projects/{project}", "project_name": project,
            "last_prompt": f"任务 {i}", "agent_kind": "claude",
            "prompt_time": stamp, "timestamp": stamp, "subagents": [],
        })
    panel._master.update_idletasks()


class _Wheel:
    def __init__(self, delta=0, num=0):
        self.delta = delta
        self.num = num


def test_rows_live_in_the_scroll_content_frame(live_panel):
    _fill(live_panel, 3)
    assert live_panel._entries["s0"].frame.master is live_panel._content


def test_a_short_list_does_not_scroll(live_panel, monkeypatch):
    _fill(live_panel, 2)
    monkeypatch.setattr(live_panel._scroll, "yview", lambda: (0.0, 1.0))
    scrolled = []
    monkeypatch.setattr(live_panel._scroll, "yview_scroll",
                        lambda *a: scrolled.append(a))
    assert live_panel._on_mousewheel(_Wheel(delta=-120)) == "break"
    assert scrolled == [], "scrolled a list that already fits"


def test_a_tall_list_scrolls_down_and_back(live_panel, monkeypatch):
    _fill(live_panel, 4)
    monkeypatch.setattr(live_panel._scroll, "yview", lambda: (0.2, 0.6))
    steps = []
    monkeypatch.setattr(live_panel._scroll, "yview_scroll",
                        lambda step, what: steps.append(step))

    live_panel._on_mousewheel(_Wheel(delta=-120))   # macOS/Windows, wheel down
    live_panel._on_mousewheel(_Wheel(delta=120))    # wheel up
    live_panel._on_mousewheel(_Wheel(num=5))        # X11 down
    live_panel._on_mousewheel(_Wheel(num=4))        # X11 up
    assert steps == [1, -1, 1, -1]


def test_the_view_height_is_capped_below_the_screen(live_panel):
    cap = live_panel._max_view_height()
    assert cap < live_panel.win.winfo_screenheight()
    assert cap >= 200


def _chevron(panel):
    for header in panel._group_headers:
        for child in header.winfo_children():
            if isinstance(child, tk.Label) and child.cget("text") in ("▸", "▾"):
                return child, header
    return None, None


def test_the_chevron_sits_left_of_the_count_in_its_own_colour(live_panel):
    _fill(live_panel, 1)
    chev, header = _chevron(live_panel)
    assert chev is not None
    assert chev.cget("fg") == panel_module.CHEVRON_FG
    live_panel._master.update_idletasks()
    # The count is pinned far right; the chevron sits just to its left, so it
    # is right of the project name but left of the count.
    count = next(
        w for w in header.winfo_children()
        if isinstance(w, tk.Label) and "个会话" in w.cget("text")
    )
    name = next(
        w for w in header.winfo_children()
        if isinstance(w, tk.Label) and w.cget("text") == "Alpha"
    )
    assert name.winfo_x() < chev.winfo_x() < count.winfo_x()


def test_the_chevron_still_toggles_from_its_new_place(live_panel):
    _fill(live_panel, 2)
    live_panel._toggle_collapsed("Alpha")
    live_panel._master.update_idletasks()
    assert not live_panel._entries["s0"].frame.winfo_manager()
    chev, _ = _chevron(live_panel)
    assert chev.cget("text") == "▸"


# ======================================================================
# collapse the whole panel to an edge handle
# ======================================================================
def _pump(panel, n=8):
    for _ in range(n):
        panel._master.update_idletasks()


def test_collapsing_swaps_the_panel_for_the_handle(live_panel):
    _fill(live_panel, 2)
    assert bool(live_panel._container.winfo_manager())
    live_panel._collapse_to_edge()
    _pump(live_panel)
    assert not live_panel._container.winfo_manager()
    assert bool(live_panel._handle.winfo_manager())


def test_expanding_swaps_it_back(live_panel):
    _fill(live_panel, 2)
    live_panel._collapse_to_edge()
    live_panel._expand_from_edge()
    _pump(live_panel)
    assert bool(live_panel._container.winfo_manager())
    assert not live_panel._handle.winfo_manager()


def test_the_handle_shows_the_session_count(live_panel):
    _fill(live_panel, 3)
    live_panel._collapse_to_edge()
    assert live_panel._handle_count.cget("text") == "3"


def test_the_handle_dot_takes_the_most_urgent_status(live_panel):
    stamp = "2026-09-11T10:00:00+00:00"
    for sid, st in [("a", "working"), ("b", "idle"), ("c", "error")]:
        live_panel.upsert(sid, {
            "status": st, "session_id": sid, "cwd": "/Users/x/projects/A",
            "project_name": "A", "last_prompt": "q", "agent_kind": "claude",
            "prompt_time": stamp, "timestamp": stamp, "subagents": [],
        })
    assert live_panel._aggregate_status() == "error"
    live_panel._collapse_to_edge()
    assert live_panel._handle_dot.itemcget(live_panel._handle_dot_id, "fill") == \
        panel_module._rgb_hex(panel_module.COLOR_MAP["error"])


def test_a_new_session_while_collapsed_keeps_it_collapsed(live_panel):
    _fill(live_panel, 1)
    live_panel._collapse_to_edge()
    _fill(live_panel, 2)  # another arrives
    _pump(live_panel)
    assert live_panel._edge_collapsed
    assert not live_panel._container.winfo_manager()
    assert live_panel._handle_count.cget("text") == "2"


def test_the_collapsed_state_persists(live_panel):
    _fill(live_panel, 1)
    live_panel._collapse_to_edge()
    assert live_panel._load_config()["edge_collapsed"] is True
    live_panel._expand_from_edge()
    assert live_panel._load_config()["edge_collapsed"] is False


def test_the_handle_docks_flush_to_the_anchored_edge(live_panel):
    _fill(live_panel, 2)
    live_panel._position = "bottom-right"
    live_panel._collapse_to_edge()
    _pump(live_panel)
    geo = live_panel.win.winfo_geometry()
    x = int(geo.split("+")[1])
    w = max(live_panel.win.winfo_width(), live_panel.win.winfo_reqwidth())
    sw = live_panel.win.winfo_screenwidth()
    assert abs((x + w) - sw) <= 2, "handle not flush to the right edge"


def test_the_arrow_points_into_the_screen(live_panel):
    _fill(live_panel, 1)
    live_panel._position = "bottom-right"
    live_panel._collapse_to_edge()
    assert live_panel._handle_arrow.cget("text") == "‹"
    live_panel._expand_from_edge()
    live_panel._position = "top-left"
    live_panel._collapse_to_edge()
    assert live_panel._handle_arrow.cget("text") == "›"


def test_reposition_while_collapsed_docks_rather_than_anchors(live_panel, monkeypatch):
    _fill(live_panel, 1)
    live_panel._collapse_to_edge()
    docked, anchored = [], []
    monkeypatch.setattr(live_panel, "_dock_handle", lambda: docked.append(True))
    monkeypatch.setattr(live_panel, "_anchor_to", lambda pos: anchored.append(pos))
    live_panel._user_dragged = False
    live_panel._run_reposition()
    assert docked and not anchored


# ======================================================================
# layout toggle button + per-session expand in horizontal mode
# ======================================================================
def test_the_layout_button_reflects_and_toggles_the_layout(live_panel):
    _fill(live_panel, 2)
    assert live_panel._vertical
    assert live_panel._layout_btn.cget("text") == "≣"
    live_panel._layout_btn.event_generate("<Button-1>")
    live_panel._master.update_idletasks()
    assert not live_panel._vertical
    assert live_panel._layout_btn.cget("text") == "⋯"


def test_the_layout_choice_persists(live_panel):
    _fill(live_panel, 1)
    live_panel._toggle_layout()
    assert live_panel._load_config()["vertical"] is False
    live_panel._toggle_layout()
    assert live_panel._load_config()["vertical"] is True


def test_horizontal_shows_dots_and_clicking_one_expands_only_it(live_panel):
    _fill(live_panel, 3)  # s0, s1, s2
    live_panel._toggle_layout()  # go horizontal
    live_panel._master.update_idletasks()
    assert all(not live_panel._entries[s]._detail_mode for s in ("s0", "s1", "s2"))

    live_panel._toggle_h_expand("s1")
    live_panel._master.update_idletasks()
    assert live_panel._entries["s1"]._detail_mode
    assert not live_panel._entries["s0"]._detail_mode
    assert not live_panel._entries["s2"]._detail_mode


def test_clicking_an_expanded_dot_folds_it_back(live_panel):
    _fill(live_panel, 2)
    live_panel._toggle_layout()
    live_panel._toggle_h_expand("s0")
    live_panel._toggle_h_expand("s0")
    live_panel._master.update_idletasks()
    assert not live_panel._entries["s0"]._detail_mode


def test_several_sessions_can_be_expanded_at_once_in_horizontal(live_panel):
    _fill(live_panel, 3)
    live_panel._toggle_layout()
    live_panel._toggle_h_expand("s0")
    live_panel._toggle_h_expand("s2")
    live_panel._master.update_idletasks()
    assert live_panel._entries["s0"]._detail_mode
    assert not live_panel._entries["s1"]._detail_mode
    assert live_panel._entries["s2"]._detail_mode


def test_switching_to_vertical_shows_full_detail_for_every_session(live_panel):
    _fill(live_panel, 3)
    live_panel._toggle_layout()          # horizontal, dots
    live_panel._toggle_layout()          # back to vertical
    live_panel._master.update_idletasks()
    assert all(live_panel._entries[s]._detail_mode for s in ("s0", "s1", "s2"))


def test_the_dot_toggles_expand_in_horizontal_but_not_in_vertical(live_panel):
    """The dot is a drag grip in vertical and an expand toggle in horizontal."""
    _fill(live_panel, 1)
    entry = live_panel._entries["s0"]
    # vertical: dot click should not create an expansion entry
    entry._canvas.event_generate("<Button-1>")
    live_panel._master.update_idletasks()
    assert "s0" not in live_panel._expanded

    live_panel._toggle_layout()
    live_panel._master.update_idletasks()
    entry._canvas.event_generate("<Button-1>")
    live_panel._master.update_idletasks()
    assert "s0" in live_panel._expanded


# ======================================================================
# vertical layout: entries stay under their own project header
# ======================================================================
def _visual_order(panel):
    """Packed children of the content frame, top-to-bottom, tagged."""
    panel._master.update_idletasks()
    rows = []
    for w in panel._content.winfo_children():
        if not w.winfo_manager():
            continue
        tag = None
        for sid, e in panel._entries.items():
            if e.frame is w:
                tag = ("row", sid)
        if tag is None:
            for c in w.winfo_children():
                if isinstance(c, tk.Label) and c.cget("text") in ("Alpha", "Beta", "Gamma"):
                    tag = ("header", c.cget("text"))
        if tag:
            rows.append((w.winfo_y(), tag))
    return [tag for _y, tag in sorted(rows)]


def test_each_entry_sits_under_its_own_project_header(live_panel):
    """Regression: pooled headers were left packed on top, rows all below them."""
    stamp = "2026-09-12T10:{:02d}:00+00:00"
    for sid, proj, i in [("a1", "Alpha", 1), ("a2", "Alpha", 2),
                         ("b1", "Beta", 3), ("b2", "Beta", 4)]:
        live_panel.upsert(sid, {
            "status": "working", "session_id": sid,
            "cwd": f"/Users/x/projects/{proj}", "project_name": proj,
            "last_prompt": "q", "agent_kind": "claude",
            "prompt_time": stamp.format(i), "timestamp": stamp.format(i),
            "subagents": [],
        })
    order = _visual_order(live_panel)
    # A header must be immediately followed by rows of that same project, never
    # two headers in a row (which is what "all rows at the bottom" looks like).
    headers = [t for t in order if t[0] == "header"]
    assert len(headers) == 2
    for i, tag in enumerate(order):
        if tag[0] == "header":
            assert order[i + 1][0] == "row", "a header is not followed by its rows"


def test_interleaving_survives_repacks(live_panel):
    stamp = "2026-09-12T10:{:02d}:00+00:00"
    for sid, proj, i in [("a1", "Alpha", 1), ("b1", "Beta", 2)]:
        live_panel.upsert(sid, {
            "status": "working", "session_id": sid,
            "cwd": f"/Users/x/projects/{proj}", "project_name": proj,
            "last_prompt": "q", "agent_kind": "claude",
            "prompt_time": stamp.format(i), "timestamp": stamp.format(i),
            "subagents": [],
        })
    # New prompts on both keep triggering repacks.
    for n in range(5):
        for sid, proj in [("a1", "Alpha"), ("b1", "Beta")]:
            live_panel.upsert(sid, {
                "status": "working", "session_id": sid,
                "cwd": f"/Users/x/projects/{proj}", "project_name": proj,
                "last_prompt": "q", "agent_kind": "claude",
                "prompt_time": stamp.format(30 + n), "timestamp": stamp.format(30 + n),
                "subagents": [],
            })
    order = _visual_order(live_panel)
    for i, tag in enumerate(order):
        if tag[0] == "header":
            assert order[i + 1][0] == "row"


@pytest.fixture()
def sub_light():
    try:
        root = tk.Tk()
    except tk.TclError:  # no display available
        pytest.skip("Tk display unavailable")
    root.withdraw()
    frame = tk.Frame(root)
    yield panel_module._SubLight(frame, "medium")
    root.destroy()


def test_a_running_task_shows_what_it_last_printed_and_for_how_long(sub_light):
    sub_light.update({
        "kind": "task", "status": "working", "name": "Wait for the build",
        "detail": "test 7/10", "started_at": time.time() - 125,
    })

    assert sub_light._lbl_detail.cget("text") == "test 7/10"
    assert sub_light._lbl_detail.winfo_manager() == "pack"
    assert sub_light._lbl_state.cget("text") == "运行中 2m"


def test_a_task_with_no_output_yet_shows_its_command(sub_light):
    sub_light.update({
        "kind": "task", "status": "working", "name": "Open the tunnel",
        "command": "ssh -N devbox", "started_at": time.time() - 5,
    })

    assert sub_light._lbl_detail.cget("text") == "$ ssh -N devbox"


def test_a_finished_task_drops_its_detail_line(sub_light):
    sub_light.update({"kind": "task", "status": "working", "name": "job",
                      "detail": "halfway", "started_at": time.time()})
    sub_light.update({"kind": "task", "status": "stale", "name": "job",
                      "detail": "halfway", "started_at": time.time()})

    assert sub_light._lbl_detail.winfo_manager() == ""
    assert sub_light._lbl_state.cget("text") == "已终止"
