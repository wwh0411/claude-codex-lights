"""Codex CLI session discovery.

Codex is watched read-only, by polling the rollout transcripts it writes under
``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<start>-<session-id>.jsonl``. Codex
does support hooks with the same schema as Claude Code, but registering one
means writing to the user's ``hooks.json`` and clearing Codex's hook-trust
gate; polling costs nothing and touches nothing.

Rollouts are flushed incrementally (a new session's file appears within ~0.3 s
and grows as the turn runs), so polling sees activity without noticeable lag.

Liveness comes from ``$CODEX_HOME/thread-writer-locks/<session-id>.lock``, which
Codex holds for exactly as long as a session is open. That gives Codex rows the
same lifecycle as Claude rows — visible while open, gray when quiet, removed on
exit — which a rollout mtime alone cannot express.

A lock file is evidence of life only while a process holds its ``flock``: a
Codex killed outright leaves the file behind with no holder, and reading that as
an open session pins a gray row to the panel for good. Verified against Codex
0.149.0-alpha.4.3 — the lock appears when a session opens, its flock is held for
the whole life of the session (by ``codex app-server`` for Codex Desktop
threads, by the CLI process for CLI ones), and the file is removed on a clean
exit.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # non-POSIX: no flock, so a lock file can only be trusted
    fcntl = None

from claude_eyes.config import (
    CODEX_HEAD_BYTES,
    CODEX_HEAD_RECORDS,
    CODEX_HOME,
    CODEX_PROMPT_SCAN_BYTES,
    CODEX_LOCK_GRACE,
    CODEX_LOCK_PROBE,
    CODEX_SCAN_INTERVAL,
    CODEX_SESSION_TIMEOUT,
    CODEX_TAIL_BYTES,
    CODEX_TURN_TIMEOUT,
    MAX_SUBAGENTS,
    STALE_TIMEOUT,
    SUBAGENT_DONE_TIMEOUT,
)

ROLLOUT_RE = re.compile(r"^rollout-\d{4}-\d{2}-\d{2}T[\dT:-]+?-(?P<sid>[0-9a-f-]{36})\.jsonl$")

# Codex injects these into the user turn the way Claude injects
# <system-reminder>; none of them is something the user typed.
INJECTED_PREFIXES = (
    "<environment_context>",
    "<user_instructions>",
    "<EXTERNAL SESSION IMPORTED>",
    "<stdin>",
    "Tip:",
    "⚠",
)

_ROLLOUT_CACHE: dict[str, tuple[float, int, dict]] = {}
_LISTING: dict[str, object] = {"at": 0.0, "paths": []}


def _sessions_dir() -> Path:
    return CODEX_HOME / "sessions"


def _lock_is_abandoned(path: Path) -> bool:
    """True when nothing holds path's flock — the session that owned it is gone.

    Only an exclusive, non-blocking attempt is made, so a live session is never
    waited on. Acquiring means no one held it; the lock is released immediately.
    A file younger than CODEX_LOCK_GRACE is trusted without probing, so the
    instant between Codex creating the file and locking it can never be read as
    death (and a session being born is never briefly interfered with).
    """
    if fcntl is None or not CODEX_LOCK_PROBE:
        return False
    try:
        if time.time() - path.stat().st_mtime < CODEX_LOCK_GRACE:
            return False
        with path.open("rb") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False  # a live Codex is holding it
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return False  # unreadable: fall back to trusting the file
    return True


def open_session_ids() -> set[str] | None:
    """Session ids Codex currently holds a writer lock for.

    Returns None when the lock directory does not exist, so callers can fall
    back to treating recent rollout writes as the liveness signal.
    """
    directory = CODEX_HOME / "thread-writer-locks"
    if not directory.is_dir():
        return None
    found: set[str] = set()
    try:
        for entry in directory.iterdir():
            name = entry.name
            if name.startswith(".") or not name.endswith(".lock"):
                continue  # .coordination.lock is not a session
            if _lock_is_abandoned(entry):
                continue  # leaked by a crash — the session is not open
            found.add(name[: -len(".lock")])
    except OSError:
        return None
    return found


def _session_id(path: Path) -> str:
    match = ROLLOUT_RE.match(path.name)
    return match.group("sid") if match else path.stem


def _iter_json(lines: list[str]):
    for raw in lines:
        try:
            item = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(item, dict):
            yield item


def _thread_spawn(meta: dict) -> dict:
    """Normalised spawn facts for a subagent thread, {} for a user's own thread.

    Codex writes ``source`` in more than one shape, and reading the parent out
    of it is what made a review thread land on the panel as its own nameless
    row:

        {"subagent": {"thread_spawn": {"parent_thread_id": ..., "agent_path":
         "/root/evolution_design", "agent_nickname": "Dirac", "depth": 1}}}
        {"subagent": "review"}          <- built-in /review; no dict at all

    So the parent comes from the top-level ``parent_thread_id``, which both
    shapes carry, and ``source`` is only consulted for what to call the thing.
    Anything with thread_source "subagent" returns a truthy dict even when the
    parent is unknown, so the caller can still tell it apart from a session the
    user started and label it rather than showing it unnamed.
    """
    if meta.get("thread_source") != "subagent":
        return {}
    parent = str(meta.get("parent_thread_id") or "")
    label = ""
    nickname = ""
    source = meta.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    if isinstance(subagent, dict):
        spawn = subagent.get("thread_spawn")
        if isinstance(spawn, dict):
            parent = str(spawn.get("parent_thread_id") or parent)
            label = str(spawn.get("agent_path") or "")
            nickname = str(spawn.get("agent_nickname") or "")
    elif isinstance(subagent, str):
        label = subagent.strip()
    return {
        "parent_thread_id": parent,
        "agent_path": label,
        "agent_nickname": nickname,
        "subagent": True,
    }


def _head_identity(path: Path) -> tuple[str, str, dict]:
    """(cwd, model, thread_spawn) from the rollout's opening records.

    ``thread_spawn`` is empty for a thread the user started and, for one Codex
    spawned as a subagent, carries who spawned it and what it was called::

        {"parent_thread_id": "01a07ae5-...", "depth": 1,
         "agent_path": "/root/evolution_design", "agent_nickname": "Dirac"}

    It is read here rather than from a second pass because it lives in
    session_meta, the very first record, which this already reads.

    Read line by line rather than as a fixed slice of bytes: Codex 0.150 embeds
    the full base-instructions text in session_meta (18 KB observed), and a read
    capped below that returns half a line, which does not parse — taking the cwd
    with it, so the session lands in the "其他" group. The model comes from the
    first turn_context, a few records further in; on a long session neither
    record is anywhere near the tail window.

    Stops at the first record that completes the pair rather than at a fixed
    record count fitted to where those records happen to sit today — that kind
    of number is exactly what broke when Codex made session_meta bigger.
    """
    cwd = ""
    model = ""
    spawn: dict = {}
    try:
        with path.open("rb") as fh:
            for _ in range(CODEX_HEAD_RECORDS):
                raw = fh.readline(CODEX_HEAD_BYTES)
                if not raw:
                    break
                try:
                    item = json.loads(raw.decode("utf-8", "replace"))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if not isinstance(item, dict):
                    continue
                payload = item.get("payload")
                if not isinstance(payload, dict):
                    continue
                if item.get("type") == "session_meta":
                    spawn = _thread_spawn(payload) or spawn
                if item.get("type") in ("session_meta", "turn_context"):
                    cwd = str(payload.get("cwd") or cwd)
                    model = str(payload.get("model") or model)
                    if cwd and model:
                        # Both answers are in; reading further would only cost
                        # time. This is the normal exit — the record bound is
                        # for a rollout that never names one of them.
                        break
    except OSError:
        return cwd, model, spawn
    return cwd, model, spawn


def _read_tail(path: Path, size: int, limit: int) -> list[str]:
    try:
        with path.open("rb") as fh:
            if size > limit:
                fh.seek(size - limit)
                fh.readline()  # discard the partial line
            payload = fh.read()
    except OSError:
        return []
    return payload.decode("utf-8", "replace").splitlines()


def thread_names() -> dict[str, str]:
    """session_id -> newest thread name; Codex's equivalent of custom-title."""
    names: dict[str, str] = {}
    try:
        lines = (CODEX_HOME / "session_index.jsonl").read_text(errors="replace").splitlines()
    except OSError:
        return names
    for item in _iter_json(lines):
        session_id = item.get("id")
        name = item.get("thread_name")
        if isinstance(session_id, str) and isinstance(name, str) and name.strip():
            names[session_id] = name.strip()  # later lines win — newest rename
    return names


def _user_text(payload: dict) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    else:
        return ""
    text = " ".join(text.split())
    if not text or text.startswith(INJECTED_PREFIXES):
        return ""
    return text


# Lines that cannot hold anything the deep scan wants. Most of a big rollout is
# megabyte tool outputs; skipping them is what keeps a full pass cheap.
_DEEP_MARKERS = (b'"user"', b'task_started', b'task_complete', b'token_count')


def _deep_scan(path: Path, size: int) -> dict:
    """Everything the tail window may have missed, from the whole rollout.

    A single turn can emit megabytes of tool output, pushing the records that
    describe it — the user's message, the task_started that says a turn is
    running, the token_count carrying the context size — arbitrarily far from
    the end. How far is unbounded, so this streams the file rather than widening
    the window to a number the next long turn will exceed anyway.

    Only reached on first sight of a session whose window already missed
    something; afterwards those values are carried forward poll to poll.
    """
    found = {
        "prompt": "", "turn_seen": False, "turn_running": False,
        "completed_at": 0.0, "started_at": 0.0, "usage_seen": False,
        "input_tokens": 0, "output_tokens": 0, "context_tokens": 0,
        "context_limit": 0,
    }
    if size > CODEX_PROMPT_SCAN_BYTES:
        return found
    try:
        with path.open("rb") as fh:
            for raw in fh:
                if not any(marker in raw for marker in _DEEP_MARKERS):
                    continue
                try:
                    item = json.loads(raw.decode("utf-8", "replace"))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                payload = item.get("payload") if isinstance(item, dict) else None
                if not isinstance(payload, dict):
                    continue
                sub = payload.get("type")
                if payload.get("role") == "user":
                    found["prompt"] = _user_text(payload) or found["prompt"]
                elif sub == "task_started":
                    found["turn_seen"] = True
                    found["turn_running"] = True
                    found["started_at"] = (
                        _payload_epoch(payload, "started_at")
                        or _record_epoch(item)
                        or found["started_at"]
                    )
                elif sub == "task_complete":
                    found["turn_seen"] = True
                    found["turn_running"] = False
                    stamp = payload.get("completed_at")
                    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
                        found["completed_at"] = float(stamp)
                elif sub == "token_count":
                    info = payload.get("info")
                    if not isinstance(info, dict):
                        continue
                    usage = info.get("last_token_usage")
                    if isinstance(usage, dict):
                        found["usage_seen"] = True
                        found["input_tokens"] = int(usage.get("input_tokens") or 0)
                        found["output_tokens"] = int(usage.get("output_tokens") or 0)
                        found["context_tokens"] = found["input_tokens"]
                    window = info.get("model_context_window")
                    if isinstance(window, (int, float)) and not isinstance(window, bool) and window > 0:
                        found["context_limit"] = int(window)
    except OSError:
        return found
    return found


def _parse_rollout(path: Path, size: int, previous: dict | None = None) -> dict:
    """Identity + live state for one rollout.

    Only the head (session_meta, always the first record) and the tail are
    parsed. Turn state is decided by which of task_started / task_complete
    appears last, which is always inside the tail.
    """
    cwd, model, spawn = _head_identity(path)
    prompt = ""
    turn_seen = False   # any task_started / task_complete inside the window
    usage_seen = False  # any token_count inside the window
    last_start = -1
    last_complete = -1
    completed_at = 0.0
    started_at = 0.0
    input_tokens = 0
    output_tokens = 0
    context_tokens = 0
    context_limit = 0
    last_timestamp = ""
    for index, item in enumerate(_iter_json(_read_tail(path, size, CODEX_TAIL_BYTES))):
        payload = item.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        kind = item.get("type")
        sub = payload.get("type")
        if isinstance(item.get("timestamp"), str):
            last_timestamp = item["timestamp"]
        if kind == "turn_context":
            cwd = str(payload.get("cwd") or cwd)
            model = str(payload.get("model") or model)
        elif kind == "session_meta":
            cwd = str(payload.get("cwd") or cwd)
        elif sub == "task_started":
            last_start = index
            turn_seen = True
            # Its own epoch field, the counterpart of task_complete's
            # completed_at; the record's timestamp is the fallback because that
            # one does not survive a rollout migration.
            started_at = _payload_epoch(payload, "started_at") or _record_epoch(item) or started_at
        elif sub == "task_complete":
            last_complete = index
            turn_seen = True
            # The wall-clock moment the turn finished. Unlike every other time
            # in the file this one survives Codex's rollout migration, so it is
            # what a finished conversation is dated by; see _activity_at.
            stamp = payload.get("completed_at")
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and stamp > 0:
                completed_at = float(stamp)
        elif sub == "token_count":
            usage_seen = True
            info = payload.get("info") or {}
            usage = info.get("last_token_usage") if isinstance(info, dict) else None
            if isinstance(usage, dict):
                input_tokens = int(usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or 0)
                # Codex counts cached input inside input_tokens rather than
                # beside it, so the last request's input *is* the context size.
                context_tokens = input_tokens
            window = info.get("model_context_window") if isinstance(info, dict) else None
            if isinstance(window, (int, float)) and not isinstance(window, bool) and window > 0:
                # Codex states its window outright; nothing has to be inferred
                # the way it does for Claude.
                context_limit = int(window)
        elif payload.get("role") == "user":
            prompt = _user_text(payload) or prompt

    previous = previous or {}
    turn_running = last_start > last_complete

    # Whatever the window did not reach keeps the value the last poll derived:
    # a turn whose output has since grown past the window still started where
    # the earlier poll saw it start, and its usage has not moved since. Without
    # this, a turn emitting more than CODEX_TAIL_BYTES reads as finished — both
    # its markers are outside the window, and -1 > -1 is False.
    if not prompt:
        # A new prompt always lands at the very end, so the tail wins whenever
        # it has one; this only covers the case where it has none.
        prompt = str(previous.get("last_prompt") or "")
    if not turn_seen and "turn_running" in previous:
        turn_running = bool(previous["turn_running"])
    if not completed_at:
        completed_at = float(previous.get("completed_at") or 0.0)
    if not started_at:
        started_at = float(previous.get("started_at") or 0.0)
    if not usage_seen:
        input_tokens = int(previous.get("input_tokens") or 0)
        output_tokens = int(previous.get("output_tokens") or 0)
        context_tokens = int(previous.get("context_tokens") or 0)
        context_limit = int(previous.get("context_limit") or 0)

    # First sight of a session already mid-long-turn has nothing to carry
    # forward, so the file is read in full once to recover what the tail missed.
    deep_scanned = bool(previous.get("deep_scanned"))
    incomplete = (
        not prompt
        or (not turn_seen and "turn_running" not in previous)
        or (not usage_seen and not context_limit)
    )
    if incomplete and not deep_scanned:
        deep = _deep_scan(path, size)
        deep_scanned = True
        prompt = prompt or deep["prompt"]
        if not turn_seen and deep["turn_seen"]:
            turn_running = deep["turn_running"]
            completed_at = completed_at or deep["completed_at"]
            started_at = started_at or deep["started_at"]
        if not usage_seen and deep["usage_seen"]:
            input_tokens = deep["input_tokens"]
            output_tokens = deep["output_tokens"]
            context_tokens = deep["context_tokens"]
            context_limit = deep["context_limit"] or context_limit

    return {
        "cwd": cwd,
        "model": model,
        "spawn": spawn,
        "completed_at": completed_at,
        "started_at": started_at,
        "last_prompt": prompt,
        "deep_scanned": deep_scanned,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        # Left at 0 when Codex did not state a window: a percentage against a
        # guessed limit would be worse than showing no meter at all.
        "context_tokens": context_tokens if context_limit else 0,
        "context_limit": context_limit,
        "last_timestamp": last_timestamp,
        "turn_running": turn_running,
    }


def _rollout_summary(path: Path) -> dict | None:
    """Cached summary for one rollout, or None when it cannot be read."""
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _ROLLOUT_CACHE.get(key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        summary = cached[2]
    else:
        summary = _parse_rollout(path, stat.st_size, cached[2] if cached else None)
        _ROLLOUT_CACHE[key] = (stat.st_mtime, stat.st_size, summary)
    result = dict(summary)
    result["path"] = key
    result["mtime"] = stat.st_mtime
    return result


def _payload_epoch(payload: dict, key: str) -> float:
    """An epoch-seconds field off a rollout payload, or 0.0 if unusable."""
    value = payload.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return 0.0


def _record_epoch(item: dict) -> float:
    """A rollout record's own timestamp, in epoch seconds (0.0 if unusable).

    Unlike ``completed_at`` these do NOT survive Codex's rollout migration —
    see _activity_at — so a value derived from them is only trustworthy for a
    conversation that is live right now. Callers must sanity-check anything they
    compute from it.
    """
    stamp = item.get("timestamp")
    if not isinstance(stamp, str) or not stamp:
        return 0.0
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _activity_at(summary: dict) -> float:
    """When this conversation last moved, in epoch seconds.

    The file's mtime is not that. Codex's rollout migration rewrites whole
    conversations at one instant — including every record's own timestamp — so
    threads idle for hours can share a mtime seconds old. Only the
    ``completed_at`` of the last finished turn survives the rewrite (it matches
    the desktop app's thread catalog to the second), so that is what dates a
    conversation whose turn is over.

    A turn still running has no completion to read, and its file is genuinely
    being appended to, so mtime is the honest signal in that case.
    """
    completed = summary.get("completed_at") or 0.0
    if completed and not summary.get("turn_running"):
        return float(completed)
    return float(summary["mtime"])


def _rollout_paths(now: float) -> list[Path]:
    """Rollout files, re-listed at most every CODEX_SCAN_INTERVAL seconds.

    A rollout is named after the session's *start* time but keeps being appended
    to, so a session opened days ago can still be live; the whole tree has to be
    listed rather than just today's directory. That is why it is rate-limited
    instead of run on every 200 ms poll.
    """
    if now - float(_LISTING["at"]) < CODEX_SCAN_INTERVAL and _LISTING["paths"]:
        return list(_LISTING["paths"])  # type: ignore[arg-type]
    directory = _sessions_dir()
    try:
        paths = sorted(directory.glob("*/*/*/rollout-*.jsonl")) if directory.is_dir() else []
    except OSError:
        paths = []
    _LISTING["at"] = now
    _LISTING["paths"] = paths
    return paths


def _row_status(summary: dict, now: float) -> tuple[str, float]:
    """(status, seconds quiet) for one rollout.

    Two different clocks: writes tell whether a turn is still moving, activity
    tells how long the conversation has been quiet.
    """
    write_age = max(0.0, now - summary["mtime"])
    quiet = max(0.0, now - _activity_at(summary))
    working = summary["turn_running"] and write_age <= CODEX_TURN_TIMEOUT
    if quiet > CODEX_SESSION_TIMEOUT:
        return "stale", quiet
    return ("working" if working else "idle"), quiet


def _agent_label(spawn: dict) -> str:
    """What to call a subagent on its light.

    The task name the model chose is the informative half of agent_path
    ("/root/evolution_design"); the nickname Codex assigns ("Dirac") says
    nothing about the work, so it only fills in when there is no path.
    """
    path = str(spawn.get("agent_path") or "")
    if path:
        return path.rsplit("/", 1)[-1] or path
    return str(spawn.get("agent_nickname") or "subagent")


def _subagent_lights(summaries: list[dict], now: float) -> list[dict]:
    """One session's subagent threads, as second-level lights.

    Mirrors the ordering and truncation Claude Code's subagents get: which ones
    survive the cap is decided by how interesting they are, but they are drawn
    in spawn order so the row never reshuffles under the user.
    """
    lights = []
    for summary in summaries:
        status, quiet = _row_status(summary, now)
        # Mirror Claude Code's _subagent_visible exactly. Two rules, and the
        # second is the one a Codex subagent used to slip through: a finished
        # thread hides after 10 minutes while it is idle, but at 30 minutes it
        # turns stale, so hiding only idle threads let it reappear grey and sit
        # there forever. Any thread quiet longer than STALE_TIMEOUT is gone,
        # whatever its status.
        if quiet > STALE_TIMEOUT:
            continue
        if status == "idle" and quiet > SUBAGENT_DONE_TIMEOUT:
            continue  # finished a while ago; same 10 minutes Claude's get
        spawn = summary.get("spawn") or {}
        lights.append({
            "agent_id": f"codex:{summary['session_id']}",
            "kind": "agent",
            "name": _agent_label(spawn),
            "agent_type": str(spawn.get("agent_nickname") or "subagent"),
            "model": summary["model"],
            "status": status,
            "started_at": float(summary.get("started_at") or 0.0),
            "timestamp": datetime.fromtimestamp(
                _activity_at(summary), timezone.utc
            ).isoformat(),
            "age_seconds": quiet,
        })

    priority = {"error": 0, "waiting": 1, "working": 2, "stale": 3, "idle": 4}
    spawn_key = lambda info: (info["started_at"], info["agent_id"])
    lights.sort(key=lambda info: (priority.get(info["status"], 9), spawn_key(info)))
    hidden = max(0, len(lights) - MAX_SUBAGENTS)
    lights = lights[:MAX_SUBAGENTS]
    lights.sort(key=spawn_key)
    if hidden:
        lights[-1] = dict(lights[-1], hidden_count=hidden)
    return lights


def _turn_elapsed(summary: dict) -> float:
    """How long the last finished turn took, or 0.0 when that cannot be trusted.

    Derived from record timestamps, which Codex's rollout migration rewrites,
    so an implausible span is reported as "unknown" rather than shown. The card
    simply omits the duration when this is 0.
    """
    started = float(summary.get("started_at") or 0.0)
    completed = float(summary.get("completed_at") or 0.0)
    if started <= 0 or completed <= 0:
        return 0.0
    elapsed = completed - started
    # A migrated rollout can put these in either order or hours apart.
    if not 0 < elapsed < 86400:
        return 0.0
    return elapsed


def discover_sessions() -> list[dict]:
    """Open Codex sessions as status payloads, newest activity first.

    A session is listed while Codex holds its writer lock: working during a
    turn, green between turns, gray after CODEX_SESSION_TIMEOUT of silence.
    Releasing the lock is Codex's SessionEnd, and drops the row; so does a lock
    left behind by a crash, which no longer holds its flock. Without a lock
    directory at all, liveness falls back to how long ago the conversation last
    moved (_activity_at, not the file's mtime).
    """
    now = time.time()
    open_ids = open_session_ids()
    names = thread_names()
    newest: dict[str, dict] = {}
    for path in _rollout_paths(now):
        session_id = _session_id(path)
        if open_ids is not None and session_id not in open_ids:
            continue
        summary = _rollout_summary(path)
        if summary is None:
            continue
        if open_ids is None and now - _activity_at(summary) > CODEX_SESSION_TIMEOUT:
            # No lock directory: fall back to recent activity as liveness. It
            # has to be _activity_at rather than the file's mtime, or a rollout
            # migration — which restamps long-finished conversations all at once
            # — reads as every one of them having just been active.
            continue
        # Forking a session produces a second rollout under the same id; the
        # most recently written one is the live conversation.
        previous = newest.get(session_id)
        if previous is None or summary["mtime"] > previous["mtime"]:
            summary["session_id"] = session_id
            newest[session_id] = summary

    # A subagent runs as its own thread, with its own rollout and its own
    # writer lock, so it arrives here looking exactly like a session the user
    # started. Left alone it lands on the panel as a nameless sibling row; it
    # belongs under its parent as a second-level light, the way Claude Code's
    # subagents do.
    children: dict[str, list[dict]] = {}
    parents: dict[str, dict] = {}
    orphans: list[dict] = []
    for session_id, summary in newest.items():
        spawn = summary.get("spawn") or {}
        parent_id = str(spawn.get("parent_thread_id") or "")
        if spawn and parent_id and parent_id != session_id:
            children.setdefault(parent_id, []).append(summary)
        elif spawn:
            # A subagent naming no parent still must not pose as a session the
            # user started: it has no entry in Codex's thread index, so it would
            # render with no name at all.
            orphans.append(summary)
        else:
            parents[session_id] = summary

    payloads: list[dict] = []
    for session_id, summary in parents.items():
        status, quiet = _row_status(summary, now)
        stamp = datetime.fromtimestamp(_activity_at(summary), timezone.utc).isoformat()
        payloads.append({
            "agent_kind": "codex",
            "status": status,
            "session_id": session_id,
            "cwd": summary["cwd"],
            "conversation_title": names.get(session_id, ""),
            "last_prompt": summary["last_prompt"],
            "model": summary["model"],
            "input_tokens": summary["input_tokens"],
            "output_tokens": summary["output_tokens"],
            "context_tokens": summary["context_tokens"],
            "context_limit": summary["context_limit"],
            "prompt_time": stamp,
            "done_time": stamp,
            "timestamp": stamp,
            "age_seconds": quiet,
            "elapsed_seconds": _turn_elapsed(summary),
            "subagents": _subagent_lights(children.pop(session_id, []), now),
        })

    # A subagent whose parent is gone would otherwise vanish silently. Showing
    # it at top level, named, beats dropping a thread that is still running.
    for leftover in children.values():
        orphans.extend(leftover)
    for summary in orphans:
        status, quiet = _row_status(summary, now)
        stamp = datetime.fromtimestamp(_activity_at(summary), timezone.utc).isoformat()
        spawn = summary.get("spawn") or {}
        payloads.append({
            "agent_kind": "codex",
            "status": status,
            "session_id": summary["session_id"],
            "cwd": summary["cwd"],
            "conversation_title": _agent_label(spawn),
            "last_prompt": summary["last_prompt"],
            "model": summary["model"],
            "input_tokens": summary["input_tokens"],
            "output_tokens": summary["output_tokens"],
            "context_tokens": summary["context_tokens"],
            "context_limit": summary["context_limit"],
            "prompt_time": stamp,
            "done_time": stamp,
            "timestamp": stamp,
            "age_seconds": quiet,
            "elapsed_seconds": _turn_elapsed(summary),
            "subagents": [],
        })

    payloads.sort(key=lambda item: item["timestamp"], reverse=True)
    return payloads
