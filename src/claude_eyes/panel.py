"""Panel window — groups all session indicators into one draggable container."""
from __future__ import annotations

import json
import math
import sys
import tkinter as tk
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from claude_eyes.config import (
    BLINK_INTERVAL,
    COLOR_MAP,
    CONTEXT_ALERT_RATIO,
    CONTEXT_LIMIT_DEFAULT,
    CONTEXT_WARN_RATIO,
    GRAY,
    INDICATOR_SIZE,
    MAX_SUBAGENTS,
    OFF,
    PANEL_CONFIG,
    RESCUE_INTERVAL,
    RESCUE_MARGIN,
    NOTIFICATION_HEIGHT,
    NOTIFICATION_POOL,
    NOTIFICATION_TTL,
    NOTIFICATION_WIDTH,
    SOUND_ENABLED,
    STATUS_DIR,
)
from claude_eyes import demo as _demo
from claude_eyes.sound import SoundCues
from claude_eyes.agent_icons import AGENT_ICONS

LOCAL_TZ = datetime.now(timezone.utc).astimezone().tzinfo
DOT_SIZE = INDICATOR_SIZE - 2  # 18px dot in 20px frame

PANEL_SIZES = {
    "small": {
        "dot": 12, "title_font": 10, "prompt_font": 8,
        "time_font": 8, "state_font": 8, "gap": 7,
        "row_pad": 3, "title_chars": 24, "prompt_chars": 32,
        "content_width": 210, "meta_width": 64,
        "sub_dot": 7, "sub_font": 8, "sub_chars": 26, "sub_pad": 1, "icon": 14,
    },
    "medium": {
        "dot": 16, "title_font": 11, "prompt_font": 9,
        "time_font": 9, "state_font": 8, "gap": 8,
        "row_pad": 4, "title_chars": 30, "prompt_chars": 42,
        "content_width": 260, "meta_width": 72,
        "sub_dot": 8, "sub_font": 8, "sub_chars": 34, "sub_pad": 2, "icon": 16,
    },
    "large": {
        "dot": 22, "title_font": 14, "prompt_font": 11,
        "time_font": 10, "state_font": 9, "gap": 10,
        "row_pad": 6, "title_chars": 38, "prompt_chars": 56,
        "content_width": 350, "meta_width": 86,
        "sub_dot": 10, "sub_font": 10, "sub_chars": 44, "sub_pad": 2, "icon": 20,
    },
}

STATUS_LABEL: dict[str, str] = {
    "working": "进行中",
    "compacting": "压缩中",
    "waiting": "等待确认",
    "idle": "已完成",
    "stale": "闲置",
    "error": "发生错误",
}

# A background command exits rather than "finishes a reply", and can never be
# waiting on the user, so it gets its own wording.
TASK_STATUS_LABEL: dict[str, str] = {
    "working": "运行中",
    "idle": "已退出",
    "stale": "已终止",
    "error": "失败",
}

PANEL_BG = "#111318"
ROW_BG = "#17191f"
TITLE_FG = "#f1f2f4"
PROMPT_FG = "#858a95"
TIME_FG = "#747984"
# Each agent's own mark, drawn beside the session name. Monochrome per brand so
# the mark never competes with the status colours in the light column.
AGENT_ICON_FG = {"claude": "#d97757", "codex": "#e6e8ec"}

# Which agent finished, on the completion card.
COMPLETION_LABEL = {"claude": "CLAUDE CODE", "codex": "CODEX"}

SUB_RULE = "#262a33"
# Context-usage bar. The fill is deliberately not a status colour until the
# window is actually filling up — a half-used context is not a state to react to.
CTX_TRACK = "#23262e"
CTX_FILL = "#4d7fb8"
CTX_FILL_WARN = "#d9a441"
CTX_FILL_ALERT = "#d9534f"
# Fixed accent for the project icon. Deliberately outside the status palette
# (blue/green/yellow/red/gray) so it never reads as a state.
GROUP_ICON = "#8b8cf0"
# The collapse chevron reads as a control, so it takes a warm amber distinct
# from the purple folder icon rather than blending into the palette.
CHEVRON_FG = "#e0a35c"

# The whole-panel collapse control and its edge handle.
COLLAPSE_FG = "#7b818e"
HANDLE_BG = "#1b1d23"
GROUP_FG = "#dbdfe6"
SUB_NAME_FG = "#9aa0ac"
SUB_META_FG = "#666b76"


class _CompletionPopup:
    """One reusable completion card window.

    `native` is its NSWindow, when AppKit could identify it. That handle is what
    lets the card be shown without activating the app; without it the code falls
    back to Tk's own map, focus theft included.
    """

    __slots__ = ("win", "canvas", "native", "dismiss_job")

    def __init__(self, win: tk.Toplevel, canvas: tk.Canvas) -> None:
        self.win = win
        self.canvas = canvas
        self.native = None
        self.dismiss_job: str | None = None


def _native_window_numbers() -> set:
    """Window numbers of every NSWindow this process currently owns."""
    try:
        import AppKit

        return {
            w.windowNumber()
            for w in AppKit.NSApplication.sharedApplication().windows()
        }
    except Exception:
        return set()


def _claim_new_native_window(before: set):
    """The NSWindow that appeared since `before` was taken.

    Identifying by set difference rather than by matching size or position:
    every pooled card is the same size, so a geometry match would hand back the
    wrong window and leave two cards fighting over one NSWindow.
    """
    try:
        import AppKit

        fresh = [
            w
            for w in AppKit.NSApplication.sharedApplication().windows()
            if w.windowNumber() not in before
        ]
        return fresh[0] if len(fresh) == 1 else None
    except Exception:
        return None


class Panel:
    """A borderless always-on-top container that holds session indicator entries."""

    def __init__(self, master: tk.Tk) -> None:
        self._master = master
        # Sids expanded inline while in horizontal (dots-only) layout.
        self._expanded: set[str] = set()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._group_headers: list[tk.Frame] = []  # frames visible this repack
        # Reusable header widget bundles. Recreating headers each repack leaked
        # Tcl memory steadily; these are configured in place instead.
        self._header_pool: list[dict] = []
        # Completion cards are pooled and reused; see show_completion().
        self._popups: list[_CompletionPopup] = []
        self._active_popups: list[_CompletionPopup] = []
        self._drag_x = 0
        self._drag_y = 0
        self._reposition_job: str | None = None
        # sid -> the row stamp it was closed at, see close_session()
        self._dismissed: dict[str, str] = {}

        cfg = self._load_config()
        self._sort_descending = cfg.get("sort_descending", True)  # True=上新下旧
        self._position = cfg.get("position", "bottom-right")
        self._show_full_name = cfg.get("show_full_name", False)
        self._panel_size = cfg.get("panel_size", "medium")
        if self._panel_size not in PANEL_SIZES:
            self._panel_size = "medium"
        # Project groups the user has collapsed, keyed by group name. Persisted
        # so a folded project stays folded across restarts.
        collapsed = cfg.get("collapsed", [])
        self._collapsed: set[str] = set(collapsed) if isinstance(collapsed, list) else set()
        # The env var is the default; the menu toggle overrides it and persists.
        self._sound_enabled = cfg.get("sound_enabled", SOUND_ENABLED)
        self._sound = SoundCues(self._sound_enabled)
        # Whole panel folded to a small edge handle; restored on next launch.
        self._edge_collapsed = bool(cfg.get("edge_collapsed", False))
        self._vertical = bool(cfg.get("vertical", True))

        # --- window ---
        self.win = tk.Toplevel(master)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-alpha", 0.95)
        except tk.TclError:
            pass
        self.win.configure(bg=PANEL_BG)
        self.win.protocol("WM_DELETE_WINDOW", self.destroy)

        self._container = tk.Frame(
            self.win,
            bg=PANEL_BG,
            highlightbackground="#2d3038",
            highlightthickness=1,
        )
        self._container.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)
        self._user_dragged = False  # stop auto-positioning after user drags

        # --- collapse-to-edge toolbar --------------------------------------
        # One slim row holding a single right-aligned button that folds the
        # whole panel to a small handle at the screen edge. Deliberately thin so
        # it adds almost no chrome to the normal view.
        self._toolbar = tk.Frame(self._container, bg=PANEL_BG)
        self._toolbar.pack(fill=tk.X)
        self._collapse_btn = tk.Label(
            self._toolbar, text="⟩⟩", fg=COLLAPSE_FG, bg=PANEL_BG,
            font=("Helvetica Neue", 11, "bold"), cursor="hand2", padx=8, pady=1,
        )
        self._collapse_btn.pack(side=tk.RIGHT)
        self._collapse_btn.bind("<Button-1>", lambda _e: self._collapse_to_edge())
        # Layout toggle sits just left of the collapse button. Its glyph shows
        # the current layout (≣ rows / ⋯ dots); clicking flips it.
        self._layout_btn = tk.Label(
            self._toolbar, text="", fg=COLLAPSE_FG, bg=PANEL_BG,
            font=("Helvetica Neue", 12, "bold"), cursor="hand2", padx=6, pady=1,
        )
        self._layout_btn.pack(side=tk.RIGHT)
        self._layout_btn.bind("<Button-1>", lambda _e: self._toggle_layout())
        self._refresh_layout_btn()
        # The empty part of the bar still drags the panel; the buttons do not.
        self._toolbar.bind("<ButtonPress-1>", self._on_drag_start)
        self._toolbar.bind("<B1-Motion>", self._on_drag_move)

        # --- edge handle (shown only while collapsed) ----------------------
        # A compact pill: aggregate status dot + session count + an inward arrow
        # to expand. Sibling of _container so one is swapped for the other.
        self._handle = tk.Frame(
            self.win, bg=HANDLE_BG,
            highlightbackground="#2d3038", highlightthickness=1,
        )
        self._handle_dot = tk.Canvas(
            self._handle, width=18, height=18, bg=HANDLE_BG,
            highlightthickness=0, bd=0,
        )
        self._handle_dot_id = self._handle_dot.create_oval(
            2, 2, 16, 16, fill=_rgb_hex(GRAY), outline="",
        )
        self._handle_dot.pack(side=tk.LEFT, padx=(13, 6), pady=11)
        self._handle_count = tk.Label(
            self._handle, text="0", fg="#dbdfe6", bg=HANDLE_BG,
            font=("Helvetica Neue", 16, "bold"),
        )
        self._handle_count.pack(side=tk.LEFT)
        self._handle_arrow = tk.Label(
            self._handle, text="‹", fg=COLLAPSE_FG, bg=HANDLE_BG,
            font=("Helvetica Neue", 17, "bold"), padx=12,
        )
        self._handle_arrow.pack(side=tk.LEFT)
        for _w in (self._handle, self._handle_dot, self._handle_count, self._handle_arrow):
            _w.bind("<Button-1>", lambda _e: self._expand_from_edge())

        # --- scroll surface -------------------------------------------------
        # Rows are packed into _content, which lives inside a canvas. When the
        # stack of sessions is taller than the screen the window is capped and
        # the wheel scrolls the canvas; otherwise the window shrinks to fit and
        # nothing scrolls. Everything that used to parent to _container now
        # parents to _content.
        self._scroll = tk.Canvas(
            self._container, bg=PANEL_BG, highlightthickness=0, bd=0,
        )
        self._scroll.pack(fill=tk.BOTH, expand=True)
        self._content = tk.Frame(self._scroll, bg=PANEL_BG)
        self._content_window = self._scroll.create_window(
            (0, 0), window=self._content, anchor="nw",
        )
        self._content.bind("<Configure>", self._on_content_resize)

        # --- drag bindings: toplevel catches clicks on child widgets ---
        self.win.bind("<ButtonPress-1>", self._on_drag_start)
        self.win.bind("<B1-Motion>", self._on_drag_move)
        self.win.bind("<Button-3>", self._on_right_click)
        self._container.bind("<ButtonPress-1>", self._on_drag_start)
        self._container.bind("<B1-Motion>", self._on_drag_move)
        self._scroll.bind("<ButtonPress-1>", self._on_drag_start)
        self._scroll.bind("<B1-Motion>", self._on_drag_move)
        # Wheel scrolling: macOS/Windows deliver <MouseWheel>, X11 Button-4/5.
        for target in (self.win, self._container, self._scroll):
            target.bind("<MouseWheel>", self._on_mousewheel)
            target.bind("<Button-4>", self._on_mousewheel)
            target.bind("<Button-5>", self._on_mousewheel)

        # --- menu ---
        self._menu = tk.Menu(self.win, tearoff=0)

        # --- blink timer ---
        self._blink_on = True
        self._blink_job: str | None = None
        self._start_blink_timer()

        # --- initial position ---
        if self._edge_collapsed:
            # Start folded if that is how the user left it. No entries yet, so
            # this just readies the handle; the window stays hidden until the
            # first session arrives (see upsert -> _show).
            self.win.after(100, lambda: self._collapse_to_edge(save=False))
        else:
            self.win.after(100, lambda: self._anchor_to(self._position))
        # Warm the completion-card pool while startup is already stealing focus
        # once, so no later notification has to.
        self.win.after(200, self._warm_popup_pool)
        # Display changes are rare, and probing NSScreen is far too expensive to
        # do from _maybe_reposition, which runs once per session per poll.
        self.win.after(RESCUE_INTERVAL, self._rescue_offscreen)
        if sys.platform == "darwin":
            self.win.after(250, self._configure_macos_window)

    def _configure_macos_window(self) -> None:
        """Keep the panel visible across macOS Spaces and fullscreen apps."""
        try:
            import AppKit

            for native_window in AppKit.NSApplication.sharedApplication().windows():
                if native_window.isVisible():
                    _configure_native_window(native_window)
                    native_window.orderFrontRegardless()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def upsert(self, sid: str, data: dict) -> None:
        stamp = _row_stamp(data)
        if self._dismissed.get(sid) == stamp:
            return  # closed from the menu, and nothing new has happened since
        self._dismissed.pop(sid, None)
        self._sound_scan(sid, data)
        was_empty = not self._entries
        is_new = sid not in self._entries
        if is_new:
            entry = _Entry(
                self._content,
                sid,
                data,
                self._show_full_name,
                self._panel_size,
                self._bind_drag_to_widget,
            )
            self._entries[sid] = entry
        else:
            entry = self._entries[sid]
            old_project = entry.project_group
            old_prompt_time = entry._prompt_time
            entry.update(data)
        entry.stamp = stamp
        if was_empty:
            self._show()
        if is_new:
            self._bind_drag_to_widget(entry.frame)
            # Bind the dot once; the handler dispatches drag vs expand by layout.
            entry._canvas.bind(
                "<Button-1>", lambda e, en=entry: self._on_dot_press(e, en)
            )
            self._repack_all()
        elif old_project != entry.project_group or old_prompt_time != entry._prompt_time:
            self._repack_all()
        if self._edge_collapsed:
            self._refresh_handle()
        self._maybe_reposition()

    def close_session(self, sid: str) -> None:
        """Menu action: take this row off the panel and keep it off.

        remove() alone is not enough — it destroys the widget, and then the
        200 ms poll rebuilds the row from the status file still sitting on disk,
        so the row reappears within one tick. Deleting the file is what makes
        closing stick, including across a manager restart.

        The dismissal is keyed by the row's stamp rather than by session id, so
        a live session that goes on to do something new comes back instead of
        being silenced for good. Codex rows have no status file to delete; for
        those the stamp check is the whole mechanism.
        """
        entry = self._entries.get(sid)
        self._dismissed[sid] = entry.stamp if entry is not None else ""
        try:
            (STATUS_DIR / f"{sid}.json").unlink(missing_ok=True)
        except OSError:
            pass
        self.remove(sid)

    def remove(self, sid: str) -> None:
        self._sound.forget(sid)
        entry = self._entries.pop(sid, None)
        if entry is not None:
            entry.frame.destroy()
        if not self._entries:
            self._hide()
        else:
            self._repack_all()
        if self._edge_collapsed:
            self._refresh_handle()
        self._maybe_reposition()

    def clear(self) -> None:
        for sid in self._entries:
            self._sound.forget(sid)
        for entry in self._entries.values():
            entry.frame.destroy()
        self._entries.clear()
        self._clear_group_headers()
        self._hide()
        self._maybe_reposition()

    def show_completion(self, data: dict) -> None:
        """Show a transient, non-interactive completion card.

        The card is drawn into a window from a pre-warmed pool rather than a
        fresh Toplevel. Tk's map path calls activateIgnoringOtherApps: on
        macOS, so creating a window here would pull keyboard focus out of
        whatever the user is typing in — measured, and the whole reason the pool
        exists. Pool windows are mapped once during startup (folded into the
        activation the panel itself already causes) and afterwards only ever
        shown and hidden through AppKit window ordering, which never activates.
        """
        popup = self._acquire_popup()
        if popup is None:  # pool unavailable (no Tk display) — nothing to show
            return
        self._render_completion(popup, data)
        if popup not in self._active_popups:
            self._active_popups.append(popup)
        self._reposition_notifications()
        self._show_popup(popup)
        if popup.dismiss_job is not None:
            try:
                popup.win.after_cancel(popup.dismiss_job)
            except tk.TclError:
                pass
        popup.dismiss_job = popup.win.after(
            NOTIFICATION_TTL, lambda p=popup: self._dismiss_notification(p)
        )

    # ------------------------------------------------------------------
    # completion card pool
    # ------------------------------------------------------------------
    def _warm_popup_pool(self) -> None:
        """Create the completion windows up front, while startup already stole focus.

        Each window has to be mapped once for macOS to give it an NSWindow at
        all — a withdrawn Toplevel has none, so there is nothing to order
        around. Doing that here, in the same moment the panel maps itself, means
        the cost is one activation the user is already paying for instead of one
        per completed turn.
        """
        for _ in range(NOTIFICATION_POOL):
            try:
                popup = self._build_popup()
            except tk.TclError:
                return
            self._popups.append(popup)
            self._hide_popup(popup)

    def _build_popup(self) -> _CompletionPopup:
        native_before = _native_window_numbers()
        win = tk.Toplevel(self._master)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-alpha", 0.92)
        except tk.TclError:
            pass
        win.configure(bg="#0c0d10")
        canvas = tk.Canvas(
            win, width=NOTIFICATION_WIDTH, height=NOTIFICATION_HEIGHT,
            bg="#0c0d10", highlightthickness=0, bd=0,
        )
        canvas.pack(fill=tk.BOTH, expand=True)
        # Click anywhere on the card to dismiss it. The card is topmost and
        # parks in the top-right corner, so without this it would swallow clicks
        # meant for whatever sits behind it until the 12 s timer fired.
        popup_ref = {}
        dismiss = lambda _e: self._dismiss_notification(popup_ref["p"])
        win.bind("<Button-1>", dismiss)
        canvas.bind("<Button-1>", dismiss)
        # Off to one side while it is mapped, so a pool warmed at startup never
        # flashes a blank card where a real notification would appear.
        win.geometry(f"+{-NOTIFICATION_WIDTH - 50}+0")
        win.update_idletasks()
        win.update()

        popup = _CompletionPopup(win, canvas)
        popup_ref["p"] = popup
        popup.native = _claim_new_native_window(native_before)
        if popup.native is not None:
            try:
                _configure_native_window(popup.native)
            except Exception:
                pass
        return popup

    def _acquire_popup(self) -> "_CompletionPopup | None":
        if not self._popups:
            self._warm_popup_pool()
        idle = [p for p in self._popups if p not in self._active_popups]
        if idle:
            return idle[0]
        if self._active_popups:
            # Pool exhausted: the oldest card has been on screen longest, so it
            # is the one to recycle rather than dropping the new notification.
            oldest = self._active_popups.pop(0)
            if oldest.dismiss_job is not None:
                try:
                    oldest.win.after_cancel(oldest.dismiss_job)
                except tk.TclError:
                    pass
                oldest.dismiss_job = None
            return oldest
        return None

    def _show_popup(self, popup: "_CompletionPopup") -> None:
        """Put the card on screen without taking focus from the user."""
        if popup.native is not None:
            try:
                popup.native.orderFrontRegardless()
                return
            except Exception:
                pass
        popup.win.deiconify()  # no AppKit: Tk's own path, focus theft included
        popup.win.lift()

    def _hide_popup(self, popup: "_CompletionPopup") -> None:
        if popup.native is not None:
            try:
                popup.native.orderOut_(None)
                return
            except Exception:
                pass
        popup.win.withdraw()

    def _render_completion(self, popup: "_CompletionPopup", data: dict) -> None:
        """Redraw a recycled card's contents from scratch."""
        canvas = popup.canvas
        canvas.delete("all")
        width = NOTIFICATION_WIDTH
        # The second fact row only exists when there is something to put in it,
        # so the card shrinks back to its old height for a plain turn.
        session_facts = _session_facts(data)
        height = NOTIFICATION_HEIGHT + (18 if session_facts else 0)
        canvas.configure(width=width, height=height)
        canvas.create_rectangle(0, 0, width, height, fill="#15171c", outline="#343843", width=1)
        canvas.create_rectangle(0, 0, 4, height, fill="#32d74b", outline="")

        title = _truncate(_notification_title(data), 34)
        prompt = _truncate(_one_line(data.get("last_prompt", "")) or "未获取到用户问题", 72)
        elapsed = _format_duration(data.get("elapsed_seconds"))
        turns = int(data.get("user_turns", 0) or 0)
        input_tokens = _format_tokens(data.get("input_tokens"), "↑")
        output_tokens = _format_tokens(data.get("output_tokens"), "↓")
        model = _short_model(data.get("model", ""))

        canvas.create_oval(18, 17, 38, 37, fill="#173c24", outline="")
        canvas.create_text(28, 27, text="✓", fill="#4ade80", font=("Helvetica Neue", 12, "bold"))
        canvas.create_text(
            48, 21,
            text=f"{COMPLETION_LABEL.get(_agent_kind(data), 'AGENT')}  ·  COMPLETED",
            fill="#72e394",
            font=("Helvetica Neue", 9, "bold"),
            anchor="nw",
        )
        canvas.create_text(
            18, 49, text=title, fill="#f5f5f7",
            font=("Helvetica Neue", 14, "bold"), anchor="nw",
        )
        canvas.create_text(
            18, 84, text="最后问题", fill="#777b85",
            font=("Helvetica Neue", 9, "bold"), anchor="nw",
        )
        canvas.create_text(
            18, 101, text=prompt, fill="#d2d3d7",
            font=("Helvetica Neue", 11), anchor="nw", width=430,
        )
        canvas.create_line(18, 137, width - 18, 137, fill="#2b2e36")

        facts = []
        if elapsed:
            facts.append(f"◷ {elapsed}")
        if turns:
            facts.append(f"↻ {turns} 轮")
        if input_tokens:
            facts.append(input_tokens)
        if output_tokens:
            facts.append(output_tokens)
        if model:
            facts.append(model)
        canvas.create_text(
            18, 153, text="   ·   ".join(facts), fill="#999da7",
            font=("Helvetica Neue", 10), anchor="w",
        )
        if session_facts:
            # Session-wide numbers, kept off the first row: that one is about
            # the turn that just finished, this one about the whole session.
            canvas.create_text(
                18, 171, text="   ·   ".join(session_facts), fill="#767a84",
                font=("Helvetica Neue", 10), anchor="w",
            )
        popup.win.update_idletasks()

    def _dismiss_notification(self, popup: "_CompletionPopup") -> None:
        if popup in self._active_popups:
            self._active_popups.remove(popup)
        popup.dismiss_job = None
        self._hide_popup(popup)
        self._reposition_notifications()

    def _reposition_notifications(self) -> None:
        screen_w = self._master.winfo_screenwidth()
        y = 42
        for popup in self._active_popups:
            try:
                popup.win.update_idletasks()
                width = max(NOTIFICATION_WIDTH, popup.win.winfo_reqwidth())
                height = popup.win.winfo_reqheight()
                popup.win.geometry(f"{width}x{height}+{screen_w - width - 24}+{y}")
                y += height + 10
            except tk.TclError:
                continue

    def destroy(self) -> None:
        self._cancel_blink()
        if self._reposition_job is not None:
            try:
                self.win.after_cancel(self._reposition_job)
            except tk.TclError:
                pass
            self._reposition_job = None
        for popup in list(self._popups):
            if popup.dismiss_job is not None:
                try:
                    popup.win.after_cancel(popup.dismiss_job)
                except tk.TclError:
                    pass
            try:
                popup.win.destroy()
            except tk.TclError:
                pass
        self._popups.clear()
        self._active_popups.clear()
        self._menu.destroy()
        self.win.destroy()
        self._master.destroy()

    # ------------------------------------------------------------------
    # blink timer (drives all dot blink states)
    # ------------------------------------------------------------------
    def _start_blink_timer(self) -> None:
        self._blink_on = not self._blink_on
        if self._edge_collapsed:
            self._blink_handle()
        else:
            for entry in self._entries.values():
                entry.apply_blink(self._blink_on)
        self._blink_job = self.win.after(BLINK_INTERVAL, self._start_blink_timer)

    def _blink_handle(self) -> None:
        status = self._aggregate_status()
        rgb = COLOR_MAP.get(status, GRAY) if status else GRAY
        # Working/waiting/error pulse like their first-level lights do; a settled
        # green or grey holds steady.
        if status in ("working", "waiting", "error") and not self._blink_on:
            rgb = _dim(rgb, 0.4)
        self._handle_dot.itemconfig(self._handle_dot_id, fill=_rgb_hex(rgb))

    def _cancel_blink(self) -> None:
        if self._blink_job is not None:
            self.win.after_cancel(self._blink_job)
            self._blink_job = None

    # ------------------------------------------------------------------
    # show / hide
    # ------------------------------------------------------------------
    def _on_content_resize(self, _event: tk.Event | None = None) -> None:
        """Match the canvas to its content, capping height so the wheel kicks in.

        Width always tracks the content. Height tracks it too until the stack is
        taller than the screen allows, at which point the canvas holds at the cap
        and the extra rows become scrollable rather than pushing the window off
        the bottom of the display.
        """
        try:
            self._scroll.update_idletasks()
            width = self._content.winfo_reqwidth()
            height = self._content.winfo_reqheight()
        except tk.TclError:
            return
        self._scroll.configure(scrollregion=(0, 0, width, height))
        self._scroll.itemconfigure(self._content_window, width=width)
        self._scroll.configure(width=width, height=min(height, self._max_view_height()))

    def _max_view_height(self) -> int:
        """Tallest the scroll view may grow before it starts scrolling."""
        try:
            screen_h = self.win.winfo_screenheight()
        except tk.TclError:
            screen_h = 1000
        return max(200, screen_h - 120)

    def _on_mousewheel(self, event: tk.Event) -> str:
        """Scroll the session stack, but only when it overflows the view."""
        try:
            first, last = self._scroll.yview()
        except tk.TclError:
            return "break"
        if first <= 0.0 and last >= 1.0:
            return "break"  # everything fits; nothing to scroll
        # macOS/Windows use event.delta (±120 per notch on Windows, small
        # integers on macOS); X11 sends Button-4 (up) / Button-5 (down).
        if event.num == 4:
            step = -1
        elif event.num == 5:
            step = 1
        elif event.delta:
            step = -1 if event.delta > 0 else 1
        else:
            return "break"
        self._scroll.yview_scroll(step, "units")
        return "break"

    def _show(self) -> None:
        self.win.deiconify()

    def _hide(self) -> None:
        self.win.withdraw()

    # ------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------
    def _pack_entry(self, entry: _Entry) -> None:
        row_pad = PANEL_SIZES[self._panel_size]["row_pad"]
        if self._vertical:
            entry.frame.pack(fill=tk.X, pady=row_pad)
            entry.show_details()
        else:
            # anchor="n" so a tall expanded card and the short dots top-align
            # instead of centring the dots against the card.
            entry.frame.pack(side=tk.LEFT, padx=row_pad, anchor="n")
            if entry.sid in self._expanded:
                entry.show_details()
            else:
                entry.hide_details()
        # The dot's click handler is bound once (see upsert); it dispatches on
        # the current layout. Re-binding here per repack leaked a Tcl command
        # each time.

    def _refresh_layout_btn(self) -> None:
        # Glyph reflects the current layout: stacked rows vs a row of dots.
        self._layout_btn.configure(text="≣" if self._vertical else "⋯")

    def _toggle_layout(self) -> None:
        self._vertical = not self._vertical
        self._refresh_layout_btn()
        self._repack_all()
        self._maybe_reposition()
        self._save_config()

    def _on_dot_press(self, event: tk.Event, entry: "_Entry") -> str | None:
        """A session dot: drag grip in vertical, expand toggle in horizontal.

        Bound once per entry so it never accumulates Tcl bind commands.
        """
        if self._vertical:
            self._on_drag_start(event)  # begin a drag; B1-Motion is already bound
            return None
        return self._toggle_h_expand(entry.sid)

    def _toggle_h_expand(self, sid: str) -> str:
        """Horizontal layout: expand one session inline to its full card.

        A dot in the strip is a summary; clicking it unfolds the same detail the
        vertical layout shows for that one session, leaving the others as dots.
        Clicking it again folds it back.
        """
        if sid in self._expanded:
            self._expanded.discard(sid)
        else:
            self._expanded.add(sid)
        self._repack_all()
        self._maybe_reposition()
        return "break"  # a dot click toggles; it must not also start a drag

    # ------------------------------------------------------------------
    # config persist
    # ------------------------------------------------------------------
    def _load_config(self) -> dict:
        try:
            return json.loads(PANEL_CONFIG.read_text())
        except Exception:
            PANEL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
            return {}

    def _save_config(self) -> None:
        PANEL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        PANEL_CONFIG.write_text(json.dumps({
            "sort_descending": self._sort_descending,
            "position": self._position,
            "show_full_name": self._show_full_name,
            "panel_size": self._panel_size,
            "sound_enabled": self._sound_enabled,
            "collapsed": sorted(self._collapsed),
            "edge_collapsed": self._edge_collapsed,
            "vertical": self._vertical,
        }, ensure_ascii=False))

    # ------------------------------------------------------------------
    # positioning
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # collapse the whole panel to an edge handle
    # ------------------------------------------------------------------
    _STATUS_PRIORITY = {"error": 0, "waiting": 1, "working": 2, "idle": 3, "stale": 4}

    def _aggregate_status(self) -> str | None:
        """The most urgent status across all sessions, for the handle dot."""
        if not self._entries:
            return None
        return min(
            (e._status for e in self._entries.values()),
            key=lambda st: self._STATUS_PRIORITY.get(st, 9),
        )

    def _refresh_handle(self) -> None:
        """Keep the collapsed handle's dot and count current."""
        status = self._aggregate_status()
        rgb = COLOR_MAP.get(status, GRAY) if status else GRAY
        self._handle_dot.itemconfig(self._handle_dot_id, fill=_rgb_hex(rgb))
        self._handle_count.configure(text=str(len(self._entries)))
        # The arrow points back toward the screen into which the panel unfolds.
        self._handle_arrow.configure(text="‹" if "right" in self._position else "›")

    def _collapse_to_edge(self, save: bool = True) -> None:
        self._edge_collapsed = True
        if save:
            self._save_config()
        if self._reposition_job is not None:
            try:
                self.win.after_cancel(self._reposition_job)
            except tk.TclError:
                pass
            self._reposition_job = None
        self._container.pack_forget()
        self._handle.pack(fill=tk.BOTH, expand=True)
        self._refresh_handle()
        if self._entries:
            self._show()
        self._dock_handle()

    def _expand_from_edge(self) -> None:
        self._edge_collapsed = False
        self._save_config()
        self._handle.pack_forget()
        self._container.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)
        # A collapse moved the window to the edge; bring it back to its corner.
        self._user_dragged = False
        if self._entries:
            self._show()
        self._anchor_to(self._position)

    def _dock_handle(self) -> None:
        """Sit the handle flush against the screen edge of its anchored side."""
        self.win.update_idletasks()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        w = max(self.win.winfo_width(), self.win.winfo_reqwidth())
        h = max(self.win.winfo_height(), self.win.winfo_reqheight())
        x = sw - w if "right" in self._position else 0
        y = 30 if "top" in self._position else sh - h - 80
        self.win.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _anchor_to(self, position: str) -> None:
        self.win.update_idletasks()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        w = max(self.win.winfo_width(), self.win.winfo_reqwidth())
        h = max(self.win.winfo_height(), self.win.winfo_reqheight())
        x = 30 if "left" in position else sw - w - 30
        y = 30 if "top" in position else sh - h - 80
        # A panel with more rows than the screen is tall computes a negative y,
        # which would hide the top of the list. Losing the bottom rows is the
        # better trade: the newest sessions are the ones worth seeing.
        self.win.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _set_position(self, position: str) -> None:
        self._position = position
        self._user_dragged = False
        self._anchor_to(position)
        self._save_config()

    def _screen_rects(self) -> list[tuple[int, int, int, int]]:
        """Every connected display, in Tk's top-left-origin coordinates.

        Tk's winfo_screenwidth/height report the primary display only, so they
        cannot tell "dragged onto a second monitor" from "left behind where a
        monitor used to be". NSScreen can. Returns [] when it is unavailable,
        which the caller reads as "don't guess".
        """
        try:
            import AppKit

            screens = AppKit.NSScreen.screens()
            if not screens:
                return []
            # Cocoa's origin is the primary display's bottom-left with y growing
            # upwards; Tk counts y downwards from the top-left.
            primary_height = screens[0].frame().size.height
            return [
                (
                    int(frame.origin.x),
                    int(primary_height - (frame.origin.y + frame.size.height)),
                    int(frame.size.width),
                    int(frame.size.height),
                )
                for frame in (screen.frame() for screen in screens)
            ]
        except Exception:
            return []

    def _is_stranded(self) -> bool:
        """True when no display shows enough of the panel to grab it by."""
        rects = self._screen_rects()
        if not rects:
            return False  # never yank the window around on a guess
        try:
            x, y = self.win.winfo_rootx(), self.win.winfo_rooty()
            w = max(self.win.winfo_width(), self.win.winfo_reqwidth())
            h = max(self.win.winfo_height(), self.win.winfo_reqheight())
        except tk.TclError:
            return False
        for sx, sy, sw, sh in rects:
            overlap_w = min(x + w, sx + sw) - max(x, sx)
            overlap_h = min(y + h, sy + sh) - max(y, sy)
            if overlap_w >= RESCUE_MARGIN and overlap_h >= RESCUE_MARGIN:
                return False
        return True

    def _rescue_offscreen(self) -> None:
        """Re-anchor a panel that unplugging a display left nowhere visible.

        Dragging sets _user_dragged, which switches auto-positioning off for
        good — so a panel dragged onto an external monitor stays in that
        monitor's coordinates after it is unplugged, and the only way back (the
        right-click menu) needs a window the user can see to right-click on.
        """
        if self._user_dragged and self._is_stranded():
            self._user_dragged = False
            self._anchor_to(self._position)
        self.win.after(RESCUE_INTERVAL, self._rescue_offscreen)

    def _maybe_reposition(self) -> None:
        """Re-anchor once per burst — the 200 ms poll calls this per session."""
        if self._user_dragged or self._reposition_job is not None:
            return
        self._reposition_job = self.win.after(50, self._run_reposition)

    def _run_reposition(self) -> None:
        self._reposition_job = None
        if self._user_dragged:
            return
        if self._edge_collapsed:
            self._dock_handle()
        else:
            self._anchor_to(self._position)

    # ------------------------------------------------------------------
    # audio cues
    # ------------------------------------------------------------------
    def _sound_scan(self, sid: str, data: dict) -> None:
        """Sound a cue for any light of this session that just turned yellow/red.

        Subagents and background commands are tracked separately from their
        session so a failing background command is heard even while the session
        row itself stays blue.
        """
        self._sound.observe(sid, data.get("status", "idle"))
        for info in data.get("subagents") or []:
            agent_id = info.get("agent_id")
            if agent_id:
                self._sound.observe(f"{sid}/{agent_id}", info.get("status", ""))

    def _toggle_sound(self) -> None:
        self._sound_enabled = not self._sound_enabled
        self._sound.enabled = self._sound_enabled
        self._save_config()

    def _toggle_full_name(self) -> None:
        self._show_full_name = not self._show_full_name
        for entry in self._entries.values():
            entry.set_show_full(self._show_full_name)
        self._maybe_reposition()
        self._save_config()

    def _toggle_sort(self) -> None:
        self._sort_descending = not self._sort_descending
        self._repack_all()
        self._save_config()

    def _set_panel_size(self, size: str) -> None:
        if size not in PANEL_SIZES or size == self._panel_size:
            return
        self._panel_size = size
        for entry in self._entries.values():
            entry.set_size(size)
        self._repack_all()
        self._maybe_reposition()
        self._save_config()

    def _repack_all(self) -> None:
        items = list(self._entries.items())
        for _, entry in items:
            entry.frame.pack_forget()
        # Forget every pooled header too, not just surplus ones. Tk keeps an
        # already-packed widget in its existing slot when re-packed, so a header
        # left packed would stay pinned above the entries that got forgotten and
        # re-added at the end — headers on top, all rows beneath them. Forgetting
        # first lets the interleaved header→rows→header order below take effect.
        self._hide_surplus_headers(0)
        self._group_headers = []
        if not self._vertical:
            items.sort(
                key=lambda x: x[1]._prompt_time,
                reverse=self._sort_descending,
            )
            for _, entry in items:
                self._pack_entry(entry)
            return

        groups: dict[str, list[tuple[str, _Entry]]] = {}
        for item in items:
            groups.setdefault(item[1].project_group, []).append(item)
        grouped = sorted(
            groups.items(),
            key=lambda group: max(entry._prompt_time for _, entry in group[1]),
            reverse=self._sort_descending,
        )
        used = 0
        for index, (project, group_items) in enumerate(grouped):
            collapsed = project in self._collapsed
            header = self._acquire_header(used)
            self._configure_header(
                header, project, len(group_items), index == 0, collapsed
            )
            header["frame"].pack(fill=tk.X)
            self._group_headers.append(header["frame"])
            used += 1
            if collapsed:
                continue  # header only; the rows stay packed-forgotten
            group_items.sort(
                key=lambda x: x[1]._prompt_time,
                reverse=self._sort_descending,
            )
            for _, entry in group_items:
                self._pack_entry(entry)
        self._hide_surplus_headers(used)

    def _hide_surplus_headers(self, used: int) -> None:
        for header in self._header_pool[used:]:
            header["frame"].pack_forget()

    def _clear_group_headers(self) -> None:
        # Hide, don't destroy: the pool is reused across repacks.
        self._hide_surplus_headers(0)
        self._group_headers = []

    def _acquire_header(self, index: int) -> dict:
        """A reusable group-header bundle, built once and reconfigured in place.

        Rebuilding these frames on every repack — a Canvas with a drawn folder,
        two labels and a chevron, several times a second for a day — leaked Tcl
        memory that never came back. Pooling them keeps the daemon flat.
        """
        if index < len(self._header_pool):
            return self._header_pool[index]
        frame = tk.Frame(self._content, bg=PANEL_BG)
        icon = tk.Canvas(frame, bg=PANEL_BG, highlightthickness=0, bd=0)
        icon.pack(side=tk.LEFT, padx=(0, 7))
        label = tk.Label(frame, fg=GROUP_FG, bg=PANEL_BG, anchor="w")
        label.pack(side=tk.LEFT)
        count = tk.Label(frame, fg="#5f6470", bg=PANEL_BG, anchor="e")
        count.pack(side=tk.RIGHT)
        chevron = tk.Label(frame, fg=CHEVRON_FG, bg=PANEL_BG, cursor="hand2")
        chevron.pack(side=tk.RIGHT, padx=(0, 8))
        self._bind_drag_to_widget(frame)
        header = {
            "frame": frame, "icon": icon, "icon_px": 0,
            "label": label, "count": count, "chevron": chevron, "project": "",
        }
        # Bind the toggle ONCE, reading the header's current project rather than
        # a captured closure. Re-.bind()-ing per repack leaked a Tcl command
        # each time (tkinter never frees replaced bind commands), which was the
        # daemon's slow memory climb.
        for widget in (chevron, icon):
            widget.bind("<Button-1>", lambda _e, h=header: self._toggle_collapsed(h["project"]))
        self._header_pool.append(header)
        return header

    def _configure_header(
        self, header: dict, project: str, count: int, first: bool, collapsed: bool
    ) -> None:
        cfg = PANEL_SIZES[self._panel_size]
        size = cfg["title_font"] + 2
        header["frame"].configure(padx=11, pady=9 if first else 15)
        icon_px = size + 3
        if header["icon_px"] != icon_px:
            header["icon"].configure(width=icon_px, height=icon_px)
            header["icon"].delete("all")
            _draw_folder(header["icon"], icon_px)
            header["icon_px"] = icon_px
        header["label"].configure(
            text=project, font=("Helvetica Neue", size, "bold"),
        )
        header["count"].configure(
            text=f"{count} 个会话",
            font=("Helvetica Neue", max(7, cfg["title_font"] - 1)),
        )
        header["chevron"].configure(
            text="▸" if collapsed else "▾",
            font=("Helvetica Neue", size, "bold"),
        )
        # Just data — the click handler bound once in _acquire_header reads this.
        header["project"] = project

    def _toggle_collapsed(self, project: str) -> str:
        if project in self._collapsed:
            self._collapsed.discard(project)
        else:
            self._collapsed.add(project)
        self._repack_all()
        self._maybe_reposition()
        self._save_config()
        return "break"  # a click on the header must not also start a drag

    # ------------------------------------------------------------------
    # drag
    # ------------------------------------------------------------------
    def _on_drag_start(self, event: tk.Event) -> None:
        self._user_dragged = True
        self._drag_x = event.x_root - self.win.winfo_x()
        self._drag_y = event.y_root - self.win.winfo_y()

    def _on_drag_move(self, event: tk.Event) -> None:
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self.win.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------------
    # right-click menu
    # ------------------------------------------------------------------
    def _on_right_click(self, event: tk.Event) -> None:
        self._menu.delete(0, "end")
        self._menu.add_command(
            label="展开面板" if self._edge_collapsed else "收起到边缘",
            command=self._expand_from_edge if self._edge_collapsed
            else self._collapse_to_edge,
        )
        self._menu.add_command(
            label="切换为横排" if self._vertical else "切换为竖排",
            command=self._toggle_layout,
        )
        self._menu.add_command(
            label="切换为上旧下新" if self._sort_descending else "切换为上新下旧",
            command=self._toggle_sort,
        )
        # position submenu
        pos_menu = tk.Menu(self._menu, tearoff=0)
        for pos_value, pos_label in [
            ("bottom-right", "右下角"), ("top-right", "右上角"),
            ("bottom-left", "左下角"), ("top-left", "左上角"),
        ]:
            pos_menu.add_command(
                label=pos_label + (" ✓" if self._position == pos_value else ""),
                command=lambda p=pos_value: self._set_position(p),
            )
        self._menu.add_cascade(label="面板位置", menu=pos_menu)
        size_menu = tk.Menu(self._menu, tearoff=0)
        for size_value, size_label in [
            ("small", "小"), ("medium", "中"), ("large", "大"),
        ]:
            size_menu.add_command(
                label=size_label + (" ✓" if self._panel_size == size_value else ""),
                command=lambda s=size_value: self._set_panel_size(s),
            )
        self._menu.add_cascade(label="面板大小", menu=size_menu)
        self._menu.add_command(
            label="显示全名" if not self._show_full_name else "截断名称",
            command=self._toggle_full_name,
        )
        self._menu.add_command(
            label="关闭提示音" if self._sound_enabled else "开启提示音",
            command=self._toggle_sound,
        )
        # demo submenu
        demo_menu = tk.Menu(self._menu, tearoff=0)
        demo_menu.add_command(label="🔵 蓝灯（工作中）", command=lambda: _demo.cmd_add(["demo-working", "working"]))
        demo_menu.add_command(label="🟠 橙灯（压缩上下文）", command=lambda: _demo.cmd_add(["demo-compacting", "compacting"]))
        demo_menu.add_command(label="🟡 黄灯（等待确认）", command=lambda: _demo.cmd_add(["demo-waiting", "waiting"]))
        demo_menu.add_command(label="🟢 绿灯（已完成）", command=lambda: _demo.cmd_add(["demo-idle", "idle"]))
        demo_menu.add_command(label="🔴 红灯（出错了）", command=lambda: _demo.cmd_add(["demo-error", "error"]))
        demo_menu.add_command(label="🎨 全部演示状态", command=_demo.cmd_demo)
        demo_menu.add_separator()
        demo_menu.add_command(label="清除所有演示灯", command=_demo.cmd_clear)
        self._menu.add_cascade(label="演示模式", menu=demo_menu)
        self._menu.add_separator()
        if self._entries:
            for sid, e in self._entries.items():
                self._menu.add_command(
                    label=f"关闭 {e.project_name}",
                    command=lambda s=sid: self.close_session(s),
                )
        self._menu.add_separator()
        self._menu.add_command(label="全部关闭", command=self.destroy)
        self._menu.post(event.x_root, event.y_root)

    def _bind_drag_to_widget(self, w: tk.Widget) -> None:
        """Bind drag start + move (and wheel scroll) to a widget and its children.

        The wheel has to be bound per widget: a Toplevel-level bind does not
        reach children that carry their own bindings, and every row does.
        """
        w.bind("<ButtonPress-1>", self._on_drag_start)
        w.bind("<B1-Motion>", self._on_drag_move)
        w.bind("<MouseWheel>", self._on_mousewheel)
        w.bind("<Button-4>", self._on_mousewheel)
        w.bind("<Button-5>", self._on_mousewheel)
        for child in w.winfo_children():
            self._bind_drag_to_widget(child)


# ======================================================================
class _SubLight:
    """One second-level light: small dot, task label, state — a subagent."""

    def __init__(self, parent: tk.Frame, size: str) -> None:
        self._status = "working"
        self._color_rgb = GRAY
        self._blinking = False
        self._size = size
        self._kind = "agent"

        self.frame = tk.Frame(parent, bg=ROW_BG)
        self._canvas = tk.Canvas(
            self.frame, bg=ROW_BG, highlightthickness=0, bd=0,
        )
        self._dot = self._canvas.create_oval(0, 0, 0, 0, fill="", outline="")
        self._canvas.pack(side=tk.LEFT)
        self._lbl_name = tk.Label(
            self.frame, text="", fg=SUB_NAME_FG, bg=ROW_BG, anchor="w",
        )
        self._lbl_name.pack(side=tk.LEFT, padx=(5, 0))
        self._lbl_state = tk.Label(
            self.frame, text="", fg=SUB_META_FG, bg=ROW_BG, anchor="e",
        )
        self._lbl_state.pack(side=tk.RIGHT)
        self.set_size(size)

    # ------------------------------------------------------------------
    def update(self, info: dict) -> None:
        status = info.get("status", "working")
        rgb = COLOR_MAP.get(status, GRAY)
        self._status = status
        self._color_rgb = rgb
        self._blinking = status in ("working", "waiting", "error")

        kind = info.get("kind", "agent")
        if kind != self._kind:
            self._kind = kind
            self._draw_marker()

        chars = PANEL_SIZES[self._size]["sub_chars"]
        name = _one_line(info.get("name", "")) or "subagent"
        self._lbl_name.configure(text=_truncate(name, chars))

        if kind == "task":
            state = TASK_STATUS_LABEL.get(status, status)
            exit_code = info.get("exit_code")
            if status == "error" and isinstance(exit_code, int):
                state = f"退出码 {exit_code}"
        else:
            state = STATUS_LABEL.get(status, status)
        hidden = info.get("hidden_count")
        if hidden:
            state = f"{state} · 另 {hidden} 个"
        self._lbl_state.configure(
            text=state,
            fg="#69a8ff" if status == "working" else _rgb_hex(rgb),
        )
        self._canvas.itemconfig(self._dot, fill=_rgb_hex(rgb))

    def set_size(self, size: str) -> None:
        self._size = size if size in PANEL_SIZES else "medium"
        cfg = PANEL_SIZES[self._size]
        self._canvas.configure(width=cfg["sub_dot"] + 2, height=cfg["sub_dot"] + 2)
        self._draw_marker()
        self._lbl_name.configure(font=("Helvetica Neue", cfg["sub_font"]))
        self._lbl_state.configure(font=("Helvetica Neue", cfg["sub_font"]))

    def _draw_marker(self) -> None:
        """Round marker for a subagent, square for a background command."""
        dot = PANEL_SIZES[self._size]["sub_dot"]
        self._canvas.delete(self._dot)
        draw = self._canvas.create_rectangle if self._kind == "task" else self._canvas.create_oval
        inset = 1 if self._kind == "agent" else 2
        self._dot = draw(
            inset, inset, dot + 2 - inset, dot + 2 - inset,
            fill=_rgb_hex(self._color_rgb), outline="",
        )

    def apply_blink(self, on: bool) -> None:
        if not self._blinking:
            return
        # Sub-dots are too small to carry a ring, and blinking them fully dark
        # would make yellow/red indistinguishable from a gray stale dot. They
        # pulse between full and dimmed so the hue always reads.
        factor = 0.45 if self._status == "working" else 0.3
        rgb = self._color_rgb if on else _dim(self._color_rgb, factor)
        self._canvas.itemconfig(self._dot, fill=_rgb_hex(rgb))


# ======================================================================
class _Entry:
    """One glass-style row: aligned dot, title/prompt, and time/state."""

    def __init__(
        self,
        parent: tk.Frame,
        sid: str,
        data: dict,
        show_full: bool = False,
        size: str = "medium",
        bind_widget=None,
    ) -> None:
        self.sid = sid
        # Sub-lights are created lazily, after the panel has already bound drag
        # handlers to this row, so they must be bound as they appear.
        self._bind_widget = bind_widget or (lambda widget: None)
        self.project_name = _display_name(data)
        self.project_group = _project_group(data)
        self._prompt_time = ""
        self._blinking = False
        self._color_rgb = GRAY
        self._ts_str = ""
        self._show_full = show_full
        self._size = size
        self._status = "idle"
        self._detail_mode = True
        # Set by Panel.upsert; read only by Panel.close_session.
        self.stamp = ""
        self.agent_kind = _agent_kind(data)

        self.frame = tk.Frame(parent, bg=PANEL_BG, padx=7, pady=0)

        self._inner = tk.Frame(
            self.frame,
            bg=ROW_BG,
            highlightbackground="#24272e",
            highlightthickness=1,
            padx=10,
            pady=7,
        )
        self._inner.pack(expand=True, fill=tk.X)

        # Fixed columns keep every session dot vertically aligned regardless
        # of title or timestamp length: dot | title/prompt | time/state.
        self._inner.grid_columnconfigure(0, weight=0)
        self._inner.grid_columnconfigure(1, weight=1)
        self._inner.grid_columnconfigure(2, weight=0)

        self._lbl_time = tk.Label(
            self._inner, text="",
            fg=TIME_FG, bg=ROW_BG, font=("Menlo", 8),
            anchor="e",
        )

        self._canvas = tk.Canvas(
            self._inner, width=DOT_SIZE + 2, height=DOT_SIZE + 2,
            bg=ROW_BG, highlightthickness=0, bd=0,
        )
        # Every light is a circle regardless of agent: the light column encodes
        # status only, and the agent's mark beside the title carries identity.
        self._ring = self._canvas.create_oval(1, 1, 2, 2, fill="", outline="")
        self._dot = self._canvas.create_oval(4, 4, 5, 5, fill=_rgb_hex(GRAY), outline="")

        # The mark is packed with the title rather than gridded on its own, so
        # the three-column layout (and the dot alignment it guarantees) is left
        # untouched.
        self._title_row = tk.Frame(self._inner, bg=ROW_BG)
        self._icon = tk.Canvas(
            self._title_row, bg=ROW_BG, highlightthickness=0, bd=0, width=1, height=1,
        )
        self._icon.pack(side=tk.LEFT, padx=(0, 6))
        self._lbl_name = tk.Label(
            self._title_row, text=self.project_name,
            fg=TITLE_FG, bg=ROW_BG, font=("Helvetica Neue", 11, "bold"),
            anchor="w",
        )
        self._lbl_name.pack(side=tk.LEFT)
        self._lbl_prompt = tk.Label(
            self._inner, text="",
            fg=PROMPT_FG, bg=ROW_BG, font=("Helvetica Neue", 9),
            anchor="w",
        )
        self._lbl_state = tk.Label(
            self._inner, text="",
            fg=TIME_FG, bg=ROW_BG, font=("Helvetica Neue", 8),
            anchor="e",
        )

        # --- context-window meter ---
        # The bar stretches with the card, so its width is only known once Tk
        # has laid the row out; it redraws itself on every <Configure>.
        self._ctx_ratio: float | None = None
        self._ctx_frame = tk.Frame(self._inner, bg=ROW_BG)
        self._ctx_bar = tk.Canvas(
            # width=1, not Tk's 378px default: the bar takes whatever the label
            # leaves over, so a wider label shortens the bar instead of being
            # clipped off the end of the row.
            self._ctx_frame, bg=ROW_BG, highlightthickness=0, bd=0,
            width=1, height=3,
        )
        self._ctx_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=(3, 0))
        self._ctx_bar.bind("<Configure>", lambda _e: self._draw_context_bar())
        self._lbl_ctx = tk.Label(
            self._ctx_frame, text="",
            fg=TIME_FG, bg=ROW_BG, font=("Menlo", 8), anchor="e",
        )
        self._lbl_ctx.pack(side=tk.RIGHT, padx=(8, 0))

        # --- second-level light group: this session's subagents ---
        self._sub_frame = tk.Frame(self._inner, bg=ROW_BG)
        self._sub_rule = tk.Frame(self._sub_frame, bg=SUB_RULE, height=1)
        self._sub_rule.pack(fill=tk.X, pady=(0, 4))
        self._sub_lights: list[_SubLight] = []
        self._sub_infos: list[dict] = []
        self._sub_visible = 0
        # None, not (): an empty tuple equals the signature of a session with no
        # subagents, which would early-return and leave the row never laid out.
        self._sub_signature: tuple | None = None
        self._strip = tk.Canvas(
            self._inner, bg=ROW_BG, highlightthickness=0, bd=0, width=1, height=1,
        )
        self._strip_dots: list[tuple[int, str]] = []

        self.set_size(size)
        self.update(data)
        # Lay the row out here rather than relying on Panel._pack_entry: a row
        # with no subagents triggers no layout pass of its own, and would render
        # as an empty card if the caller ever packed it without calling one.
        self._relayout()

    # ------------------------------------------------------------------
    def update(self, data: dict) -> None:
        status = data.get("status", "idle")
        rgb = COLOR_MAP.get(status, GRAY)
        self._status = status
        self.project_group = _project_group(data)

        self._prompt_time = data.get("prompt_time", "")
        pt = _format_ts(data.get("prompt_time", ""))
        dt = _format_ts(data.get("done_time", ""))

        if status == "working" or not dt or dt == pt:
            self._ts_str = pt
        else:
            self._ts_str = f"{pt} | {dt}"

        raw = _notification_title(data)
        self.project_name = raw
        cfg = PANEL_SIZES[self._size]
        if self._show_full:
            self._lbl_name.configure(text=raw)
        else:
            self._lbl_name.configure(text=_truncate(raw, cfg["title_chars"]))
        prompt = _one_line(data.get("last_prompt", ""))
        if prompt:
            self._lbl_prompt.configure(text=_truncate(prompt, cfg["prompt_chars"]))
        else:
            self._lbl_prompt.configure(text=_project_name(data.get("cwd", "")))
        display_time = dt if status == "idle" and dt else pt
        self._lbl_time.configure(text=display_time)
        self._lbl_state.configure(
            text=STATUS_LABEL.get(status, status),
            fg=_rgb_hex(rgb) if status != "working" else "#69a8ff",
        )

        self._set_context(data)
        self.set_subagents(data.get("subagents") or [])

        self._color_rgb = rgb
        self._blinking = status in ("working", "compacting", "waiting", "error")
        self._canvas.itemconfig(self._dot, fill=_rgb_hex(rgb))
        self._canvas.itemconfig(
            self._ring,
            outline=_rgb_hex(rgb) if status in ("working", "compacting") else "",
        )

    def _set_context(self, data: dict) -> None:
        """Show how full the context window is, and how often it was compacted."""
        ratio, text = _context_view(data)
        compactions = int(data.get("compact_count", 0) or 0)
        if text and compactions:
            text = f"{text} ⟲{compactions}"
        cost = _format_cost_short(data.get("cost_usd"))
        if text and cost:
            text = f"{text}  {cost}"
        appeared = (self._ctx_ratio is None) != (ratio is None)
        self._ctx_ratio = ratio
        self._lbl_ctx.configure(text=text)
        self._draw_context_bar()
        # Same rule as the subagent group: re-grid only when the row gains or
        # loses the meter, never on a mere value change, or the panel jumps.
        if appeared and self._sub_signature is not None:
            self._relayout()

    def _draw_context_bar(self) -> None:
        canvas = self._ctx_bar
        canvas.delete("all")
        if self._ctx_ratio is None:
            return
        width = canvas.winfo_width()
        if width <= 1:  # not laid out yet; the <Configure> binding redraws
            return
        height = int(canvas.cget("height"))
        canvas.create_rectangle(0, 0, width, height, fill=CTX_TRACK, outline="")
        filled = max(2, round(width * min(1.0, self._ctx_ratio)))
        canvas.create_rectangle(
            0, 0, filled, height, fill=_context_color(self._ctx_ratio), outline="",
        )

    def set_subagents(self, infos: list[dict]) -> None:
        """Draw the second-level light group for this session's subagents."""
        infos = list(infos)[:MAX_SUBAGENTS]
        signature = tuple(
            (
                info.get("agent_id", ""),
                info.get("kind", "agent"),
                info.get("status", ""),
                info.get("name", ""),
                info.get("exit_code"),
                info.get("hidden_count") or 0,
            )
            for info in infos
        )
        if signature == self._sub_signature:
            return  # nothing changed — skip all widget work on this poll
        self._sub_signature = signature
        self._sub_infos = infos

        while len(self._sub_lights) < len(infos):
            light = _SubLight(self._sub_frame, self._size)
            self._bind_widget(light.frame)
            self._sub_lights.append(light)
        pad = PANEL_SIZES[self._size]["sub_pad"]
        previously_visible = self._sub_visible
        for index, light in enumerate(self._sub_lights):
            if index < len(infos):
                light.update(infos[index])
                if index >= previously_visible:
                    light.frame.pack(fill=tk.X, pady=pad)
            elif index < previously_visible:
                light.frame.pack_forget()
        self._sub_visible = len(infos)
        self._render_strip()
        # Re-gridding the whole row makes the panel visibly jump as it re-anchors,
        # so only do it when the group appears or disappears. A status or label
        # change needs no layout work at all.
        if bool(infos) != bool(previously_visible):
            self._relayout()

    def _render_strip(self) -> None:
        """Compact dot strip used when the panel is in horizontal layout."""
        self._strip.delete("all")
        self._strip_dots = []
        if not self._sub_infos:
            self._strip.configure(width=1, height=1)
            return
        cfg = PANEL_SIZES[self._size]
        size = max(4, cfg["sub_dot"] - 2)
        step = size + 3
        for index, info in enumerate(self._sub_infos):
            status = info.get("status", "working")
            left = index * step
            draw = (
                self._strip.create_rectangle
                if info.get("kind") == "task"
                else self._strip.create_oval
            )
            item = draw(
                left, 0, left + size, size,
                fill=_rgb_hex(COLOR_MAP.get(status, GRAY)),
                outline="",
            )
            self._strip_dots.append((item, status))
        width = len(self._sub_infos) * step - 3
        hidden = self._sub_infos[-1].get("hidden_count") if self._sub_infos else 0
        if hidden:
            # Collapsed mode has no room for labels, so the truncation count
            # would otherwise be invisible here.
            self._strip.create_text(
                width + 4, size / 2,
                text=f"+{hidden}", anchor="w",
                fill=SUB_META_FG, font=("Helvetica Neue", cfg["sub_font"]),
            )
            width += 6 + 7 * len(str(hidden))
        self._strip.configure(width=max(1, width), height=size + 1)

    def _relayout(self) -> None:
        if self._detail_mode:
            self.show_details()
        else:
            self.hide_details()

    def set_show_full(self, flag: bool) -> None:
        self._show_full = flag
        if flag:
            self._lbl_name.configure(text=self.project_name)
        else:
            max_chars = PANEL_SIZES[self._size]["title_chars"]
            self._lbl_name.configure(text=_truncate(self.project_name, max_chars))

    def set_size(self, size: str) -> None:
        self._size = size if size in PANEL_SIZES else "medium"
        cfg = PANEL_SIZES[self._size]
        dot_size = cfg["dot"]
        self._lbl_time.configure(font=("Menlo", cfg["time_font"]))
        self._lbl_name.configure(font=("Helvetica Neue", cfg["title_font"], "bold"))
        self._lbl_prompt.configure(font=("Helvetica Neue", cfg["prompt_font"]))
        self._lbl_state.configure(font=("Helvetica Neue", cfg["state_font"]))
        self._lbl_ctx.configure(font=("Menlo", cfg["state_font"]))
        self._canvas.coords(self._ring, 1, 1, dot_size + 1, dot_size + 1)
        inset = max(4, dot_size // 4)
        self._canvas.coords(
            self._dot, inset, inset, dot_size + 2 - inset, dot_size + 2 - inset,
        )
        self._canvas.configure(width=dot_size + 2, height=dot_size + 2)
        _draw_agent_icon(self._icon, self.agent_kind, cfg["icon"])
        # Sub-light text truncation and row padding are size-dependent and are
        # only applied while rebuilding, so force a rebuild rather than waiting
        # for the next subagent status change to repair the layout.
        for light in self._sub_lights:
            light.set_size(self._size)
            light.frame.pack_forget()
        self._sub_visible = 0
        self._sub_signature = None
        self.set_subagents(list(self._sub_infos))

    def apply_blink(self, on: bool) -> None:
        if self._blinking:
            if self._status in ("working", "compacting"):
                ring = self._color_rgb if on else OFF
                self._canvas.itemconfig(self._ring, outline=_rgb_hex(ring), width=2)
                self._canvas.itemconfig(self._dot, fill=_rgb_hex(self._color_rgb))
            else:
                color = self._color_rgb if on else OFF
                self._canvas.itemconfig(self._ring, outline="")
                self._canvas.itemconfig(self._dot, fill=_rgb_hex(color))
        # Sub-lights blink on their own status: a subagent can still be running
        # while the parent session already sits idle waiting for the user.
        for index, light in enumerate(self._sub_lights):
            if index < len(self._sub_infos):
                light.apply_blink(on)
        for item, status in self._strip_dots:
            if status not in ("working", "waiting", "error"):
                continue
            rgb = COLOR_MAP.get(status, GRAY)
            factor = 0.45 if status == "working" else 0.3
            self._strip.itemconfig(
                item, fill=_rgb_hex(rgb if on else _dim(rgb, factor)),
            )

    def show_details(self) -> None:
        self._detail_mode = True
        self._hide_all()
        cfg = PANEL_SIZES[self._size]
        gap = cfg["gap"]
        self._inner.grid_columnconfigure(1, minsize=cfg["content_width"])
        self._inner.grid_columnconfigure(2, minsize=cfg["meta_width"])
        self._canvas.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, gap))
        self._lbl_name.pack(side=tk.LEFT)
        self._title_row.grid(row=0, column=1, sticky="sw")
        self._lbl_prompt.grid(row=1, column=1, sticky="nw", pady=(2, 0))
        self._lbl_time.grid(row=0, column=2, sticky="se", padx=(gap * 2, 0))
        self._lbl_state.grid(row=1, column=2, sticky="ne", padx=(gap * 2, 0), pady=(2, 0))
        if self._ctx_ratio is not None:
            self._ctx_frame.grid(
                row=2, column=1, columnspan=2, sticky="we", pady=(4, 0),
            )
        if self._sub_infos:
            self._sub_frame.grid(
                row=3, column=1, columnspan=2, sticky="we", pady=(6, 0),
            )

    def hide_details(self) -> None:
        self._detail_mode = False
        self._hide_all()
        # Collapsed mode shows dots only, so the text columns must give up
        # their reserved width or every card stays as wide as its title.
        self._inner.grid_columnconfigure(1, minsize=0)
        self._inner.grid_columnconfigure(2, minsize=0)
        # sticky="w" matters here: a wide strip would otherwise stretch column 0
        # and float the main dot to the card's centre, breaking dot alignment.
        self._canvas.grid(row=0, column=0, sticky="w")
        # Titles are hidden here, so the mark is all that identifies the agent.
        self._lbl_name.pack_forget()
        self._title_row.grid(row=0, column=1, sticky="w", padx=(6, 0))
        if self._sub_infos:
            self._strip.grid(row=1, column=0, sticky="w", pady=(3, 0))

    def _hide_all(self) -> None:
        for w in (
            self._lbl_time,
            self._canvas,
            self._title_row,
            self._lbl_prompt,
            self._lbl_state,
            self._ctx_frame,
            self._sub_frame,
            self._strip,
        ):
            w.grid_forget()


def _draw_agent_icon(canvas: tk.Canvas, kind: str, size: int) -> None:
    """Draw the agent's mark: Claude Code's robot, Codex's knot.

    The shapes come from the official SVGs, pre-flattened into polygons by
    .debug/build_agent_icons.py, so this needs no SVG parser and no image
    library. Holes (the robot's eyes) are painted in the row background rather
    than punched out, which Tk's canvas cannot do.
    """
    canvas.delete("agent-icon")
    icon = AGENT_ICONS.get(kind) or AGENT_ICONS["claude"]
    colour = AGENT_ICON_FG.get(kind, AGENT_ICON_FG["claude"])
    for key, fill in (("outlines", colour), ("holes", ROW_BG)):
        for subpath in icon.get(key, ()):
            points = [value * size + 1 for point in subpath for value in point]
            canvas.create_polygon(
                *points, fill=fill, outline="", tags="agent-icon",
            )
    canvas.configure(width=size + 2, height=size + 2)


def _agent_kind(data: dict) -> str:
    kind = data.get("agent_kind")
    return kind.strip().lower() if isinstance(kind, str) and kind.strip() else "claude"


def _draw_folder(canvas: tk.Canvas, size: float) -> None:
    """Folder silhouette: a tab on the top left, then the body."""
    outline = [
        (0.00, 0.92), (0.00, 0.16), (0.40, 0.16),
        (0.52, 0.34), (1.00, 0.34), (1.00, 0.92),
    ]
    canvas.create_polygon(
        [coord * size for point in outline for coord in point],
        fill=GROUP_ICON,
        outline="",
    )


def _configure_native_window(native) -> None:
    """Float above other apps, join every Space, round the corners.

    The floating level is what keeps a window on top of whatever the user is
    working in. It is independent of app activation, which is why a card can be
    both always-visible and never focus-stealing.
    """
    import AppKit

    native.setCollectionBehavior_(
        AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
        | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
    )
    native.setLevel_(AppKit.NSFloatingWindowLevel)
    native.setHidesOnDeactivate_(False)
    native.setOpaque_(False)
    native.setHasShadow_(True)
    content = native.contentView()
    content.setWantsLayer_(True)
    content.layer().setCornerRadius_(14.0)
    content.layer().setMasksToBounds_(True)


def _rgb_hex(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def _dim(rgb: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """Scale a colour toward black for the dim phase of a blink."""
    return (int(rgb[0] * factor), int(rgb[1] * factor), int(rgb[2] * factor))


def _format_ts(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        utc_time = datetime.fromisoformat(iso_str)
        return utc_time.astimezone(LOCAL_TZ).strftime("%H:%M:%S")
    except Exception:
        return ""


def _row_stamp(data: dict) -> str:
    """What makes a row's content "the same" for dismissal purposes.

    Every hook write bumps the timestamp, so this changes exactly when the
    session does something new. Codex rows carry both fields too.
    """
    return str(data.get("timestamp") or data.get("prompt_time") or "")


def _project_name(cwd: str) -> str:
    if not cwd:
        return "?"
    return cwd.replace("\\", "/").rstrip("/").split("/")[-1] or cwd


def _project_group(data: dict) -> str:
    explicit = data.get("project_name")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    cwd = data.get("cwd", "")
    if not isinstance(cwd, str) or not cwd:
        return "其他"
    path = Path(cwd).expanduser().resolve(strict=False)
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate.name or str(candidate)
    projects_root = Path.home() / "projects"
    try:
        relative = path.relative_to(projects_root)
        if relative.parts:
            return relative.parts[0]
    except ValueError:
        pass
    return path.name or str(path)


def _display_name(data: dict) -> str:
    title = data.get("conversation_title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    prompt = _one_line(data.get("last_prompt", ""))
    if prompt:
        return _truncate(prompt, 28)
    return _project_name(data.get("cwd", ""))


def _notification_title(data: dict) -> str:
    """Notification heading must never duplicate the last user prompt."""
    title = data.get("conversation_title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return _project_name(data.get("cwd", ""))


def _one_line(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _format_duration(value: object) -> str:
    try:
        seconds = max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return ""
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {seconds} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def _format_tokens(value: object, prefix: str) -> str:
    try:
        count = max(0, int(value))
    except (TypeError, ValueError):
        return ""
    if count == 0:
        return ""
    if count >= 1_000_000:
        shown = f"{count / 1_000_000:.1f}M"
    elif count >= 1_000:
        shown = f"{count / 1_000:.1f}K"
    else:
        shown = str(count)
    return f"{prefix} {shown} tok"


def _session_facts(data: dict) -> list[str]:
    """Whole-session figures for the completion card's second row."""
    facts = []
    ratio, text = _context_view(data)
    if ratio is not None:
        facts.append(f"▤ 上下文 {text}")
    compactions = _format_compactions(data)
    if compactions:
        facts.append(compactions)
    cost = _format_cost(data.get("cost_usd"))
    if cost:
        facts.append(cost)
    return facts


def _context_view(data: dict) -> tuple[float | None, str]:
    """(fill ratio, label) for the context meter, or (None, "") when unknown.

    Unknown is the normal state for a session whose transcript has not been read
    yet, and for Codex rows, which report no context size at all.
    """
    try:
        used = int(data.get("context_tokens", 0) or 0)
    except (TypeError, ValueError):
        return None, ""
    if used <= 0:
        return None, ""
    try:
        limit = int(data.get("context_limit", 0) or 0)
    except (TypeError, ValueError):
        limit = 0
    limit = limit or CONTEXT_LIMIT_DEFAULT
    ratio = used / limit
    return ratio, f"{_compact_count(used)}/{_compact_count(limit)} {round(ratio * 100)}%"


def _context_color(ratio: float) -> str:
    if ratio >= CONTEXT_ALERT_RATIO:
        return CTX_FILL_ALERT
    if ratio >= CONTEXT_WARN_RATIO:
        return CTX_FILL_WARN
    return CTX_FILL


def _compact_count(count: int) -> str:
    """Token counts as K/M, dropping the decimal once it would just add noise."""
    if count >= 1_000_000:
        millions = count / 1_000_000
        return f"{millions:.0f}M" if millions >= 10 else f"{millions:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}K"
    return str(count)


def _format_cost(value: object) -> str:
    """Claude Code's own running estimate, marked as one — it is not a bill."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ""
    if value <= 0:
        return ""
    return f"≈ ${value:,.2f}"


def _format_cost_short(value: object) -> str:
    """Row-sized cost: enough digits to compare sessions, no more."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return ""
    if value >= 10_000:
        return f"≈${value / 1000:.1f}K"
    if value >= 100:
        return f"≈${value:.0f}"
    return f"≈${value:.1f}"


def _format_compactions(data: dict) -> str:
    count = int(data.get("compact_count", 0) or 0)
    if not count:
        return ""
    trigger = {"auto": "自动", "manual": "手动"}.get(data.get("compact_trigger", ""), "")
    dropped = int(data.get("compact_dropped", 0) or 0)
    text = f"⟲ 已压缩 {count} 次"
    if trigger:
        text += f"（{trigger}）"
    if dropped:
        text += f" 丢弃 {_compact_count(dropped)}"
    return text


def _short_model(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    name = value.removeprefix("claude-")
    return _truncate(name, 18)


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"
