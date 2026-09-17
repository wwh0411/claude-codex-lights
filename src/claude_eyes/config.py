"""Configuration constants. Override via env vars for custom setups."""
import os
from pathlib import Path

BASE_DIR = Path(
    os.environ.get("CLAUDE_TRAFFIC_LIGHT_DIR", Path.home() / ".claude" / "traffic-light")
)

# Where Claude Code keeps session transcripts. Overridable so the manager can
# be exercised end-to-end against a sandbox tree.
CLAUDE_PROJECTS_DIR = Path(
    os.environ.get("CLAUDE_PROJECTS_DIR", Path.home() / ".claude" / "projects")
)

# Claude Code's own settings. Read (never written) for the configured model
# name, which is the only place the 1M-context suffix shows up before Claude
# Code gets around to writing a cost-state record.
CLAUDE_SETTINGS = Path(
    os.environ.get("CLAUDE_SETTINGS", Path.home() / ".claude" / "settings.json")
)

STATUS_DIR = BASE_DIR / "status"
# Per-session parse state, so a hook event only reads the transcript bytes that
# appeared since the last one. A 16 MB transcript costs 55 ms to parse in full,
# and SessionStart / UserPromptSubmit / Stop each paid it.
PARSE_DIR = BASE_DIR / "parse"
PARSE_FINGERPRINT_BYTES = 4096  # head slice hashed to notice a rewritten file
# The panel only ever shows the first few dozen chars of a prompt, so storing
# the whole thing (a pasted prompt can be 190 KB) bloats every status file and
# every history line for nothing. Cap what we persist.
PROMPT_STORE_MAX = 1000
# history.jsonl is an append-only audit trail nothing reads back; cap it so it
# cannot grow without bound (it had reached 1.1 GB).
LOG_MAX_BYTES = 5_000_000
LOG_DIR = BASE_DIR / "logs"
NOTIFICATION_DIR = BASE_DIR / "notifications"
SUBAGENT_DIR = BASE_DIR / "subagents"
PANEL_CONFIG = BASE_DIR / "panel_config.json"
MANAGER_LOCK = BASE_DIR / "manager.lock"
# Held with flock for the manager's whole life, unlike MANAGER_LOCK, which is a
# create-then-delete startup handshake. A lock the OS hands out cannot be won
# twice, so two managers starting in the same instant cannot both proceed.
MANAGER_SINGLETON = BASE_DIR / "manager.singleton"

POLL_INTERVAL = 200       # ms, status file poll rate
BLINK_INTERVAL = 500      # ms, on/off toggle for blinking
STALE_TIMEOUT = 1800      # seconds — show sessions as inactive after 30 minutes
LOCK_TIMEOUT = 10         # seconds — startup lock considered stale after this
INDICATOR_SIZE = 20       # pixels

# --- second-level light group (subagents + background commands) ---
MAX_SUBAGENTS = 8            # combined cap on sub-lights under one session card
SUBAGENT_DONE_TIMEOUT = 600  # seconds — hide finished subagents after 10 minutes
SUBAGENT_HANG_TIMEOUT = 300  # seconds — an unfinished transcript this quiet is
                             # treated as abandoned rather than still working
SUBAGENT_TAIL_BYTES = 65536  # only the transcript tail is parsed for status
SUBAGENT_HEAD_BYTES = 32768  # only the head is parsed for the task label

# --- Codex CLI sessions ---
# Codex is watched by polling its rollout transcripts. Nothing under CODEX_HOME
# is ever written: registering a hook there would need Codex's hook-trust gate.
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
CODEX_ENABLED = os.environ.get("CLAUDE_EYES_CODEX", "1") != "0"
CODEX_SCAN_INTERVAL = 2.0    # seconds between rollout directory re-scans
CODEX_SESSION_TIMEOUT = 1800 # seconds — drop a Codex session after 30 min quiet
CODEX_TURN_TIMEOUT = 300     # seconds — an unfinished turn this quiet is over
CODEX_TAIL_BYTES = 262144    # rollout tail parsed for status/tokens/prompt
# session_meta is one line, but Codex embeds the whole base-instructions text in
# it: 18 KB on 0.150, against the 8 KB this used to allow. A truncated line does
# not parse, and the cwd goes with it, so the cap is now only a sanity bound on
# a single readline rather than a budget the record has to fit inside.
CODEX_HEAD_BYTES = 1_048_576
# session_meta is the first record, but the turn_context that names the model
# sits a few records in (line 8 on 0.150) and, on a long session, never appears
# again inside the tail window. The scan stops as soon as it has both fields,
# so this bound is only reached by a rollout that never states one of them —
# picking it generously costs nothing in the normal case.
CODEX_HEAD_RECORDS = 200
# A rollout whose last turn produced megabytes of tool output can push the most
# recent user message arbitrarily far from the end — one observed session put it
# 3 MB back within half an hour — so the fallback scan streams the whole file
# rather than a window sized by guesswork. Paid once per session: the prompt is
# then carried forward across polls. This cap only gives up on a pathological
# rollout, where scanning would cost more than the row is worth.
CODEX_PROMPT_SCAN_BYTES = 67_108_864

# A Codex killed outright leaves its writer lock file behind. The file only
# means "session open" while a live process holds its flock, so an unheld lock
# is read as death rather than as a session that is merely quiet — otherwise it
# pins a permanent gray row to the panel. Set the env var to 0 to fall back to
# trusting the file's existence alone.
CODEX_LOCK_PROBE = os.environ.get("CLAUDE_EYES_CODEX_LOCK_PROBE", "1") != "0"
CODEX_LOCK_GRACE = 5.0       # seconds — a lock file this new is trusted without
                             # probing, covering the gap between Codex creating
                             # it and taking the flock on it

# --- completion cards ---
# Cards are drawn into pooled windows rather than fresh ones: on macOS, Tk maps
# a new window by activating the app, which pulls keyboard focus out of whatever
# the user is typing in. Pool windows are mapped once at startup and afterwards
# only shown/hidden through AppKit ordering, which never activates.
NOTIFICATION_POOL = 3      # concurrent cards; beyond this the oldest is recycled
NOTIFICATION_TTL = 12000   # ms a card stays on screen
NOTIFICATION_WIDTH = 468
NOTIFICATION_HEIGHT = 174

# --- off-screen rescue ---
# Unplugging a display the panel was dragged onto strands it in coordinates no
# monitor covers any more. Checked on its own slow timer, not from the 200 ms
# poll: it has to ask NSScreen for the display layout.
RESCUE_INTERVAL = 2000  # ms between off-screen checks
RESCUE_MARGIN = 60      # px of the panel that must show on some display for it
                        # to count as reachable — enough to grab and drag back

# --- audio cues ---
# Only the two states a user must react to make a sound. Set the env var to 0
# to mute globally; the panel's right-click menu mutes it per machine.
SOUND_ENABLED = os.environ.get("CLAUDE_EYES_SOUND", "1") != "0"
SOUND_COOLDOWN = 3.0   # seconds — floor between two cues for the same light
SOUND_WARMUP = 2.0     # seconds — sessions already alerting at manager start
                       # are pre-existing state, so stay silent this long

# macOS system sound names (/System/Library/Sounds/<name>.aiff). Basso is the
# platform's own error tone; Submarine is a soft ping that survives being
# heard many times a day. Other platforms map these states to their own cues.
SOUND_FILES = {
    "waiting": os.environ.get("CLAUDE_EYES_SOUND_WAITING", "Submarine"),
    "error": os.environ.get("CLAUDE_EYES_SOUND_ERROR", "Basso"),
}

# --- context window ---
# Claude Code does not record the window size in the transcript, so it is
# inferred: the `[1m]` model suffix in a cost-state record marks a long-context
# session, and a context already past the standard limit proves one too.
CONTEXT_LIMIT_DEFAULT = 200_000
CONTEXT_LIMIT_LONG = 1_000_000
CONTEXT_WARN_RATIO = 0.70   # bar turns amber — compaction is coming
CONTEXT_ALERT_RATIO = 0.90  # bar turns red — compaction is imminent

GREEN = (50, 215, 75)
BLUE = (45, 135, 255)
YELLOW = (255, 200, 0)
RED = (220, 30, 30)
GRAY = (140, 140, 140)
OFF = (60, 60, 60)
# Compaction is a working sub-state, but it blocks the reply for minutes, so it
# gets its own colour. Orange rather than violet: violet is already the project
# icon's accent, which must never read as a status.
ORANGE = (255, 138, 20)

COLOR_MAP = {
    "working": BLUE,
    "compacting": ORANGE,
    "waiting": YELLOW,
    "idle": GREEN,
    "stale": GRAY,
    "error": RED,
}
