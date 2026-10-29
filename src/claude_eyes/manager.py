"""Daemon that monitors status/ dir and manages the indicator panel."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # non-POSIX: no flock, so the heartbeat check stands alone
    fcntl = None

from claude_eyes import codex
from claude_eyes.config import (
    MANAGER_SINGLETON,
    CLAUDE_PROJECTS_DIR,
    CODEX_ENABLED,
    MANAGER_LOCK,
    MAX_SUBAGENTS,
    NOTIFICATION_DIR,
    POLL_INTERVAL,
    STALE_TIMEOUT,
    STATUS_DIR,
    SUBAGENT_DIR,
    SUBAGENT_DONE_TIMEOUT,
    SUBAGENT_HANG_TIMEOUT,
    SUBAGENT_HEAD_BYTES,
    SUBAGENT_TAIL_BYTES,
)
from claude_eyes.panel import Panel
from claude_eyes.hook import is_side_session
from claude_eyes.start import _pid_exists, configure_hooks

HEARTBEAT_FILE = STATUS_DIR.parent / "manager.heartbeat"


def _project_identity(cwd: str) -> tuple[str, str]:
    if not cwd:
        return "其他", ""
    path = Path(cwd).expanduser()
    # Only a *file* path should resolve to its parent. A directory that no
    # longer exists (renamed, deleted) keeps its own name: walking up would file
    # the session under the parent — a dead ~/projects/foo becoming "projects".
    current = path.parent if path.is_file() else path
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate.name or str(candidate), str(candidate)
    projects_root = Path.home() / "projects"
    try:
        relative = current.resolve().relative_to(projects_root.resolve())
        if relative.parts:
            root = projects_root / relative.parts[0]
            return relative.parts[0], str(root)
    except (OSError, ValueError):
        pass
    return current.name or str(current), str(current)


def _message_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    return "\n".join(
        item.get("text", "").strip()
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
        and item.get("text", "").strip()
    ).strip()


def _agent_label(text: str, fallback: str) -> str:
    cleaned = re.sub(r"[`*_#]", "", text)
    cleaned = re.sub(r"^你的任务[：:]\s*", "", cleaned.strip())
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return fallback
    for separator in ("。", "；", "\n", "##"):
        if separator in cleaned:
            cleaned = cleaned.split(separator, 1)[0]
    return cleaned[:42]


# Transcript parsing is cached on (mtime, size) so the 200 ms poll never
# re-reads a multi-megabyte subagent transcript that has not changed.
_SUBAGENT_CACHE: dict[str, tuple[float, int, dict | None]] = {}
_SESSION_TRANSCRIPT_CACHE: dict[str, Path] = {}


def _session_transcript(session_id: str) -> Path | None:
    """Locate a session transcript, remembering the hit to skip the glob."""
    cached = _SESSION_TRANSCRIPT_CACHE.get(session_id)
    if cached is not None:
        if cached.is_file():
            return cached
        _SESSION_TRANSCRIPT_CACHE.pop(session_id, None)
    try:
        found = next(CLAUDE_PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    except (StopIteration, OSError):
        return None
    _SESSION_TRANSCRIPT_CACHE[session_id] = found
    return found


_SESSION_CWD_CACHE: dict[str, str] = {}


def _session_launch_cwd(session_id: str) -> str:
    """The directory the session was started in — its real project.

    Claude Code files a transcript under the cwd the session launched in, and
    that never changes for the session's life. The cwd carried by hook events
    is the *current* one, which drifts as the session cds around, so grouping
    on it files a session under whatever directory it last visited (a nested
    clone, a subdirectory) instead of the project it belongs to.
    """
    cached = _SESSION_CWD_CACHE.get(session_id)
    if cached is not None:
        return cached
    transcript = _session_transcript(session_id)
    if transcript is None:
        return ""
    for item in _iter_json_lines(_read_head(transcript, SUBAGENT_HEAD_BYTES)):
        launch_cwd = item.get("cwd")
        if isinstance(launch_cwd, str) and launch_cwd:
            # Only cache a real answer; an empty read may just be a transcript
            # that has not been flushed yet.
            _SESSION_CWD_CACHE[session_id] = launch_cwd
            return launch_cwd
    return ""


_FORK_PARENT_CACHE: dict[str, str] = {}


def _fork_parent(session_id: str) -> str:
    """The session this one was forked from, per the transcript's `forkedFrom`.

    `--fork-session` — a resumed conversation that branches, and how Claude Code
    relaunches a background job — gives the continuation its own session id
    while agent transcripts keep landing under the id the job was created with.
    Looking for subagents under the live id alone therefore finds nothing, and
    the session's whole second-level light group silently disappears.
    """
    cached = _FORK_PARENT_CACHE.get(session_id)
    if cached is not None:
        return cached
    transcript = _session_transcript(session_id)
    if transcript is None:
        return ""
    items = list(_iter_json_lines(_read_head(transcript, SUBAGENT_HEAD_BYTES)))
    for item in items:
        forked = item.get("forkedFrom")
        parent = forked.get("sessionId") if isinstance(forked, dict) else None
        if isinstance(parent, str) and parent and parent != session_id:
            _FORK_PARENT_CACHE[session_id] = parent
            return parent
    if items:
        # Settled: the head parsed and carries no fork marker. Caching the
        # negative keeps a 32 KB read off every poll for every plain session.
        _FORK_PARENT_CACHE[session_id] = ""
    return ""


def _fork_chain(session_id: str, limit: int = 4) -> list[str]:
    """The session, then the ones it was forked from — nearest ancestor first."""
    chain = [session_id]
    current = session_id
    for _ in range(limit):
        current = _fork_parent(current)
        if not current or current in chain:
            break
        chain.append(current)
    return chain


def _live_project_cwd(session_id: str, event_cwd: str) -> str:
    """The directory to group a session under.

    The launch cwd is preferred (see _session_launch_cwd) but only while it
    still exists. A renamed or deleted project directory leaves the transcript
    recording a path that resolves to nothing — Claude Code keeps reporting the
    old string forever — so a resumed session would be filed under whatever the
    dead path's parent happens to be called. The cwd carried by hook events is
    the live one, so it is the better answer once the recorded one is gone.
    """
    launch = _session_launch_cwd(session_id)
    if launch and Path(launch).expanduser().is_dir():
        return launch
    return event_cwd or launch


def _iter_json_lines(lines: list[str]):
    for raw in lines:
        try:
            item = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(item, dict):
            yield item


def _read_tail(path: Path, size: int, limit: int) -> list[str]:
    """Read the last `limit` bytes, dropping the leading partial line."""
    try:
        with path.open("rb") as fh:
            if size > limit:
                fh.seek(size - limit)
                fh.readline()
            payload = fh.read()
    except OSError:
        return []
    return payload.decode("utf-8", "replace").splitlines()


def _read_head(path: Path, limit: int) -> list[str]:
    try:
        with path.open("rb") as fh:
            payload = fh.read(limit)
    except OSError:
        return []
    return payload.decode("utf-8", "replace").splitlines()


def _subagent_meta(path: Path) -> dict:
    """Read the sidecar written when the Agent tool spawns a subagent."""
    meta_path = path.parent / f"{path.stem}.meta.json"
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _parse_subagent_transcript(path: Path, size: int) -> dict | None:
    """Extract identity + completion from a subagent transcript.

    Only the transcript tail is parsed: it carries the newest assistant
    message, which is all that decides running vs finished. The head is
    touched only when the meta sidecar has no description to label with.
    """
    meta = _subagent_meta(path)
    agent_id = path.stem.removeprefix("agent-")
    agent_type = str(meta.get("agentType") or "").strip()
    model = str(meta.get("model") or "").strip()
    label = str(meta.get("description") or "").strip()

    last_stop_reason = ""
    last_timestamp = ""
    for item in _iter_json_lines(_read_tail(path, size, SUBAGENT_TAIL_BYTES)):
        if isinstance(item.get("agentId"), str) and item["agentId"]:
            agent_id = item["agentId"]
        if item.get("type") == "agent-name" and isinstance(item.get("agentName"), str):
            label = item["agentName"].strip() or label
        elif item.get("type") == "custom-title" and isinstance(item.get("customTitle"), str):
            label = item["customTitle"].strip() or label
        elif item.get("type") == "assistant":
            message = item.get("message")
            if isinstance(message, dict) and isinstance(message.get("stop_reason"), str):
                last_stop_reason = message["stop_reason"]
            if isinstance(message, dict) and isinstance(message.get("model"), str):
                model = model or message["model"]
        if isinstance(item.get("timestamp"), str):
            last_timestamp = item["timestamp"]

    if not label:
        for item in _iter_json_lines(_read_head(path, SUBAGENT_HEAD_BYTES)):
            if item.get("type") != "user" or item.get("toolUseResult"):
                continue
            text = _message_text(item.get("message"))
            if text and not text.lstrip().startswith("<"):
                label = _agent_label(text, "")
                break

    return {
        "agent_id": agent_id,
        "kind": "agent",
        "name": label or agent_type or f"agent-{agent_id[-6:]}",
        "agent_type": agent_type,
        "model": model,
        "finished": last_stop_reason == "end_turn",
        "timestamp": last_timestamp,
    }


def _subagent_transcript_summary(path: Path, now: datetime | None = None) -> dict | None:
    """Cached summary for one subagent transcript, or None if unreadable."""
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _SUBAGENT_CACHE.get(key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        summary = cached[2]
    else:
        summary = _parse_subagent_transcript(path, stat.st_size)
        if len(_SUBAGENT_CACHE) > 512:
            _SUBAGENT_CACHE.clear()
        _SUBAGENT_CACHE[key] = (stat.st_mtime, stat.st_size, summary)
    if summary is None:
        return None
    age = max(0.0, time.time() - stat.st_mtime)
    result = dict(summary)
    result["age_seconds"] = age
    # Ordering must not depend on last-write time, or agents swap places every
    # time one of them logs a line. Creation time is stable for the agent's life
    # (st_ctime is not — it moves on every write).
    started = float(getattr(stat, "st_birthtime", 0.0) or 0.0)
    if not started:
        try:
            started = (path.parent / f"{path.stem}.meta.json").stat().st_mtime
        except OSError:
            started = stat.st_mtime
    result["started_at"] = started
    if summary["finished"]:
        result["status"] = "idle"
    elif age > SUBAGENT_HANG_TIMEOUT:
        # No end_turn and nothing written for minutes: the agent was aborted
        # or its parent died. A forever-blue light would misreport that.
        result["status"] = "stale"
    else:
        result["status"] = "working"
    return result


_HOOK_STATE_CACHE: dict[str, tuple[float, dict | None]] = {}


def _hook_states_by_session(now: datetime) -> dict[str, list[dict]]:
    """Read every subagent hook state file once, grouped by parent session.

    Called once per scan rather than once per session: every session used to
    scandir the same directory and re-parse every file in it, which dominated
    the poll cost as soon as a handful of agents had run.
    """
    grouped: dict[str, list[dict]] = {}
    if not SUBAGENT_DIR.is_dir():
        _HOOK_STATE_CACHE.clear()
        return grouped
    live: set[str] = set()
    for state_path in SUBAGENT_DIR.glob("*.json"):
        key = str(state_path)
        try:
            mtime = state_path.stat().st_mtime
        except OSError:
            continue
        live.add(key)
        cached = _HOOK_STATE_CACHE.get(key)
        if cached is not None and cached[0] == mtime:
            state = cached[1]
        else:
            try:
                state = json.loads(state_path.read_text())
            except (OSError, json.JSONDecodeError, ValueError):
                state = None
            if not isinstance(state, dict):
                state = None
            _HOOK_STATE_CACHE[key] = (mtime, state)
        if state is None:
            continue
        try:
            age = (now - datetime.fromisoformat(state.get("timestamp", ""))).total_seconds()
        except (TypeError, ValueError):
            continue
        if age > STALE_TIMEOUT:
            state_path.unlink(missing_ok=True)
            _HOOK_STATE_CACHE.pop(key, None)
            live.discard(key)
            continue
        # The filename is `<parent_session_id>--<agent_id>.json`; prefer the
        # recorded field but fall back to the name so an older or partial state
        # file still reaches the right session.
        session_id = str(state.get("parent_session_id", "")) or state_path.stem.split("--")[0]
        if session_id:
            grouped.setdefault(session_id, []).append({**state, "age_seconds": age})
    for stale_key in _HOOK_STATE_CACHE.keys() - live:
        _HOOK_STATE_CACHE.pop(stale_key, None)
    return grouped


# ----------------------------------------------------------------------
# Background tasks (Bash run_in_background)
#
# Unlike subagents these leave no metadata on disk — /tmp only holds the raw
# output file. Command, label and exit status live in the parent transcript,
# which is multi-megabyte, so it is consumed incrementally: each scan parses
# only the bytes appended since the last one.
# ----------------------------------------------------------------------
_TASK_ID_RE = re.compile(r"<task-id>(.*?)</task-id>", re.S)
_TASK_STATUS_RE = re.compile(r"<status>(.*?)</status>", re.S)
_TASK_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.S)
_EXIT_CODE_RE = re.compile(r"exit code (\d+)")
# A monitor reports no backgroundTaskId, so its notification is matched on the
# tool-use id it carries instead.
_TOOL_USE_ID_RE = re.compile(r"<tool-use-id>(.*?)</tool-use-id>", re.S)
_OUTPUT_PATH_RE = re.compile(r"Output is being written to: (\S+?\.output)")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Tools that end a background task outright. A stopped task gets no
# <task-notification>, so without reading these its light runs forever.
_STOP_TOOLS = ("TaskStop", "KillShell", "KillBash")
# A running task is checked against its output file — whether any process
# still holds it, and what it last printed — at most this often.
TASK_PROBE_INTERVAL = 15.0
# How long after its ack a task must be before a missing holder retires it:
# the harness opens the output file a moment after acknowledging.
TASK_PROBE_GRACE = 30.0

# The Monitor tool kills a watch at its own timeout_ms (30 minutes at most), so
# a monitor past its deadline is gone whether or not a notice was ever written.
MONITOR_DEFAULT_TIMEOUT_MS = 300_000.0
MONITOR_MAX_TIMEOUT_MS = 1_800_000.0

_BACKGROUND_STATE: dict[str, dict] = {}


def _iso_epoch(value: object) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _content_blocks(item: dict) -> list:
    message = item.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    return content if isinstance(content, list) else []


def _user_message_text(item: dict) -> str:
    """The text of a user transcript item, whichever shape its content uses."""
    message = item.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _task_label(description: object, command: object) -> str:
    text = description if isinstance(description, str) else ""
    if not text.strip() and isinstance(command, str):
        # Fall back to the first meaningful line of the command itself.
        for line in command.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                text = stripped
                break
    return " ".join(text.split())[:60]


def _absorb_notification(state: dict, content: object, timestamp: object = None) -> None:
    """Apply a <task-notification> block to a tracked background task.

    `timestamp` is the transcript record's own time. It matters because
    _BACKGROUND_STATE lives in memory: every manager restart re-reads the whole
    transcript from offset 0, so stamping wall-clock time here would resurrect
    every background task the session ever ran as if it had just finished.
    """
    if not isinstance(content, str) or "<task-notification>" not in content:
        return
    task_id = _TASK_ID_RE.search(content)
    tool_use_id = _TOOL_USE_ID_RE.search(content)
    task = None
    if task_id is not None:
        task = state["tasks"].get(task_id.group(1).strip())
    if task is None and tool_use_id is not None:
        # Monitors are keyed by tool-use id: the harness assigns them a task id
        # only in the notification, long after the light has to exist.
        task = state["tasks"].get(tool_use_id.group(1).strip())
    if task is None:
        return  # subagents emit these too; only background work is tracked
    status = _TASK_STATUS_RE.search(content)
    summary = _TASK_SUMMARY_RE.search(content)
    reported = (status.group(1).strip() if status else "").lower()
    text = " ".join(summary.group(1).split()) if summary else ""
    exit_code = _EXIT_CODE_RE.search(text)
    if reported == "completed" and (exit_code is None or exit_code.group(1) == "0"):
        task["status"] = "idle"
    elif reported in ("killed", "cancelled", "stopped"):
        task["status"] = "stale"
    else:
        task["status"] = "error"
    task["exit_code"] = int(exit_code.group(1)) if exit_code else None
    task["finished_at"] = _iso_epoch(timestamp) or time.time()


def _command_summary(command: object) -> str:
    """The command's first meaningful line, shown when it has no description."""
    if not isinstance(command, str):
        return ""
    for line in command.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return " ".join(stripped.split())[:120]
    return ""


def _tool_result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return ""


def _absorb_stop(state: dict, payload: dict, timestamp: object) -> None:
    """A TaskStop/KillShell call ends the background task or monitor it names."""
    target = payload.get("task_id") or payload.get("shell_id")
    if not isinstance(target, str) or not target:
        return
    task = state["tasks"].get(target)
    if task is None:
        # Monitors are keyed by tool-use id, but are stopped by task id.
        task = next(
            (t for t in state["tasks"].values() if t.get("task_id") == target), None
        )
    if task is None or task["status"] != "working":
        return
    task["status"] = "stale"
    task["finished_at"] = _iso_epoch(timestamp) or time.time()


def _register_monitor(state: dict, block: dict, timestamp: object) -> None:
    """Track a Monitor call as a running light, keyed by its tool-use id.

    A monitor differs from `Bash(run_in_background=True)` in the two places
    that matter here: it never reports a `backgroundTaskId`, and its only
    completion notice is a <task-notification> carrying the tool-use id. So the
    tool-use id is claimed at call time — waiting for a notification instead
    would mean a monitor is first drawn at the moment it stops existing.

    Re-arming is the normal life of a monitor (it expires after minutes and is
    started again), so a finished monitor watching the same thing is dropped
    rather than left to sit out its done-timeout beside its own replacement.
    """
    tool_use_id = block.get("id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return
    payload = block.get("input") or {}
    name = _task_label(payload.get("description"), payload.get("command")) or "monitor"
    for key, task in list(state["tasks"].items()):
        if (
            task.get("kind") == "monitor"
            and task.get("name") == name
            and task.get("status") != "working"
        ):
            del state["tasks"][key]
    try:
        timeout_ms = float(payload.get("timeout_ms") or MONITOR_DEFAULT_TIMEOUT_MS)
    except (TypeError, ValueError):
        timeout_ms = MONITOR_DEFAULT_TIMEOUT_MS
    timeout_ms = min(max(timeout_ms, 0.0), MONITOR_MAX_TIMEOUT_MS)
    state["tasks"][tool_use_id] = {
        "agent_id": f"monitor:{tool_use_id}",
        "kind": "monitor",
        "timeout_ms": timeout_ms,
        "name": name,
        "agent_type": "monitor",
        "model": "",
        "status": "working",
        "timestamp": timestamp if isinstance(timestamp, str) else "",
        "started_at": _iso_epoch(timestamp),
        "finished_at": 0.0,
    }


def _absorb_transcript_item(state: dict, item: dict) -> None:
    kind = item.get("type")
    if kind == "assistant":
        for block in _content_blocks(item):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            payload = block.get("input")
            if not isinstance(payload, dict):
                continue
            if block.get("name") == "Monitor":
                _register_monitor(state, block, item.get("timestamp"))
                continue
            if block.get("name") in _STOP_TOOLS:
                _absorb_stop(state, payload, item.get("timestamp"))
                continue
            if block.get("name") != "Bash":
                continue
            # Every Bash call is remembered, not only run_in_background ones: a
            # foreground command that outlives its timeout is moved to the
            # background by the harness, and its ack is the first sign of it.
            # Without the call's description that task could only be shown by
            # its opaque id. Entries are dropped again when the result arrives.
            state["pending"][block.get("id")] = {
                "name": _task_label(payload.get("description"), payload.get("command")),
                "command": _command_summary(payload.get("command")),
                "started_at": _iso_epoch(item.get("timestamp")),
            }
    elif kind == "user":
        # A completion notice reaches the transcript in more than one shape:
        # as an attachment (below), or inlined into a plain user message. Only
        # reading the attachment shape leaves a command that finished 20 hours
        # ago pinned to "running" forever, because nothing else ever moves a
        # background task off that status.
        _absorb_notification(state, _user_message_text(item), item.get("timestamp"))
        result = item.get("toolUseResult")
        task_id = result.get("backgroundTaskId") if isinstance(result, dict) else None
        for block in _content_blocks(item):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            monitor = state["tasks"].get(block.get("tool_use_id"))
            if monitor is not None and monitor.get("kind") == "monitor":
                # "Monitor started (task <id>, ...)": the id TaskStop will use.
                if isinstance(result, dict) and isinstance(result.get("taskId"), str):
                    monitor["task_id"] = result["taskId"]
                continue
            started = state["pending"].pop(block.get("tool_use_id"), {})
            if not isinstance(task_id, str) or not task_id:
                continue  # an ordinary command that finished in the foreground
            output = _OUTPUT_PATH_RE.search(_tool_result_text(block))
            state["tasks"][task_id] = {
                "agent_id": f"task:{task_id}",
                "kind": "task",
                "name": started.get("name") or f"后台任务 {task_id}",
                "command": started.get("command", ""),
                "output_path": output.group(1) if output else "",
                "agent_type": "background",
                "model": "",
                "status": "working",
                "timestamp": item.get("timestamp", ""),
                "started_at": started.get("started_at") or _iso_epoch(item.get("timestamp")),
                "finished_at": 0.0,
            }
            break
    elif kind == "attachment":
        attachment = item.get("attachment")
        if isinstance(attachment, dict) and attachment.get("commandMode") == "task-notification":
            _absorb_notification(state, attachment.get("prompt"), item.get("timestamp"))
    elif kind == "queue-operation" and item.get("operation") == "enqueue":
        _absorb_notification(state, item.get("content"), item.get("timestamp"))


def _session_is_dead(data: dict) -> bool:
    """True when the Claude Code process that owned this session is gone.

    A session killed from the terminal (Ctrl+C, closing the window, kill) never
    fires SessionEnd, so its status file stays on disk and the row lives on —
    blue until STALE_TIMEOUT, then gray forever. Staleness cannot tell that
    apart from a session that is simply idle, but the owner pid can.

    Deliberately conservative: only a status file whose recorded parent actually
    looked like Claude Code is trusted, so a wrong pid (a shell that exited, an
    older hook that recorded nothing) leaves the row alone instead of making it
    vanish. Getting it wrong is self-healing either way — the next hook event
    rewrites the status file and the row comes back.
    """
    pid = data.get("owner_pid")
    if not isinstance(pid, int) or pid <= 1:
        return False
    if "claude" not in str(data.get("owner_cmd", "")).lower():
        return False
    return not _pid_exists(pid)


# A continued-in session is dead the moment its transcript records the move,
# and that transcript stops changing afterwards, so one check per mtime is
# enough — cache the verdict keyed on (path, size, mtime).
_SUPERSEDED_CACHE: dict[str, tuple] = {}


def _session_superseded(session_id: str) -> bool:
    """True when this session was `continued-in` to another (e.g. via /branch).

    Claude Code writes a `continued-in` record — always at the very end — into
    the parent transcript when a conversation moves to a new session id. The
    parent is dead from that point: its hook never fires again, so its status
    file freezes and the row would otherwise linger grey forever while the live
    continuation runs under a different id.

    Only the transcript tail is read, and the verdict is cached per file
    signature, so a settled superseded transcript is parsed once.
    """
    path = _session_transcript(session_id)
    if path is None:
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    sig = (str(path), stat.st_size, stat.st_mtime)
    cached = _SUPERSEDED_CACHE.get(session_id)
    if cached is not None and cached[0] == sig:
        return cached[1]
    verdict = False
    try:
        with path.open("rb") as fh:
            if stat.st_size > 4096:
                fh.seek(-4096, 2)
            tail = fh.read()
        # Cheap prefilter before paying for any JSON parsing.
        if b'"continued-in"' in tail:
            for raw in tail.split(b"\n"):
                if b'"continued-in"' not in raw:
                    continue
                try:
                    item = json.loads(raw)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if (
                    isinstance(item, dict)
                    and item.get("type") == "continued-in"
                    and item.get("continuedInSessionId")
                ):
                    verdict = True
                    break
    except OSError:
        return False
    _SUPERSEDED_CACHE[session_id] = (sig, verdict)
    return verdict


def _output_holders(paths: list[str]) -> set[str] | None:
    """Which of these files some process still has open, or None if unknown.

    The harness hands a background command its output file as stdout, so the
    file stays open exactly as long as the command lives. One lsof call covers
    every path; a failure to run it returns None, which means "do not judge",
    never "all dead".
    """
    if not paths:
        return set()
    try:
        out = subprocess.run(
            ["lsof", "-F", "n", "--", *paths],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    wanted = set(paths)
    held = set()
    for line in out.splitlines():
        if not line.startswith("n"):
            continue
        name = line[1:]
        # lsof reports /private/tmp paths resolved; the transcript may not.
        for path in wanted:
            if name == path or name.endswith(path.removeprefix("/private")):
                held.add(path)
    return held


def _last_output_line(path: str) -> str:
    """What a running command printed last — the closest thing to "doing what"."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 4096))
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        # Progress bars rewrite one line with \r; only the latest frame counts.
        text = _ANSI_RE.sub("", line.rsplit("\r", 1)[-1]).strip()
        if text:
            return " ".join(text.split())[:120]
    return ""


def _probe_tasks(tasks, now_ts: float) -> None:
    """Retire background commands that died silently, and refresh their output.

    A command ends without any notice when it is killed from outside, when the
    session that owned it exits, or when the harness simply does not write one;
    reading only notifications pinned such a light to "running" for weeks.
    """
    due = [
        task for task in tasks
        if task.get("kind") == "task"
        and task["status"] == "working"
        and task.get("output_path")
        and now_ts - task.get("probed_at", 0.0) >= TASK_PROBE_INTERVAL
    ]
    if not due:
        return
    existing = [t["output_path"] for t in due if os.path.exists(t["output_path"])]
    held = _output_holders(existing)
    for task in due:
        task["probed_at"] = now_ts
        path = task["output_path"]
        if now_ts - (task.get("started_at") or now_ts) < TASK_PROBE_GRACE:
            continue
        if not os.path.exists(path):
            # The temp directory was cleaned (reboot, harness cleanup).
            task["status"] = "stale"
            task["finished_at"] = task.get("started_at") or now_ts
            continue
        task["detail"] = _last_output_line(path)
        if held is not None and path not in held:
            task["status"] = "stale"
            try:
                # The file's last write is when the command last did anything,
                # so a manager restart does not show weeks-old tasks as fresh.
                task["finished_at"] = os.path.getmtime(path)
            except OSError:
                task["finished_at"] = now_ts


def _monitor_deadline(task: dict) -> float | None:
    """When a monitor is guaranteed to be dead, or None for anything else."""
    if task.get("kind") != "monitor":
        return None
    started = task.get("started_at") or 0.0
    if not started:
        return None
    timeout_ms = task.get("timeout_ms") or MONITOR_DEFAULT_TIMEOUT_MS
    # A little slack: the runtime kills the watch at the deadline, and the
    # notification that confirms it lands a moment later.
    return started + timeout_ms / 1000.0 + 30.0


def _background_tasks(session_id: str, now: datetime) -> list[dict]:
    transcript = _session_transcript(session_id)
    if transcript is None:
        return []
    try:
        size = transcript.stat().st_size
    except OSError:
        return []
    state = _BACKGROUND_STATE.setdefault(
        session_id, {"offset": 0, "pending": {}, "tasks": {}}
    )
    if size < state["offset"]:
        # The transcript was rewritten (compaction) — start over.
        state.update(offset=0, pending={}, tasks={})
    if size > state["offset"]:
        try:
            with transcript.open("rb") as handle:
                handle.seek(state["offset"])
                chunk = handle.read(size - state["offset"])
        except OSError:
            return []
        end = chunk.rfind(b"\n")
        if end != -1:
            state["offset"] += end + 1
            for item in _iter_json_lines(
                chunk[:end].decode("utf-8", "replace").splitlines()
            ):
                _absorb_transcript_item(state, item)

    visible = []
    now_ts = time.time()
    _probe_tasks(state["tasks"].values(), now_ts)
    for task in state["tasks"].values():
        if task["status"] == "working":
            deadline = _monitor_deadline(task)
            if deadline is None or now_ts <= deadline:
                visible.append(dict(task, age_seconds=0.0))
                continue
            # Expired: the harness writes a notification for some monitors and
            # not others, so the tool's own guarantee is what retires the light.
            task["status"] = "idle"
            task["finished_at"] = deadline
        age = max(0.0, now_ts - task["finished_at"])
        if age <= SUBAGENT_DONE_TIMEOUT:
            visible.append(dict(task, age_seconds=age))
    return visible


def _subagent_visible(info: dict) -> bool:
    age = info.get("age_seconds", 0.0)
    if age > STALE_TIMEOUT:
        return False
    if info.get("status") == "idle" and age > SUBAGENT_DONE_TIMEOUT:
        return False
    return True


def _discover_subagents(
    session_id: str, now: datetime, hook_states: dict[str, list[dict]] | None = None
) -> list[dict]:
    transcript = _session_transcript(session_id)
    if transcript is None:
        return []
    discovered: dict[str, dict] = {}
    for ancestor in _fork_chain(session_id):
        root = transcript if ancestor == session_id else _session_transcript(ancestor)
        if root is None:
            continue
        subagent_dir = root.with_suffix("") / "subagents"
        for path in sorted(subagent_dir.glob("*.jsonl")) if subagent_dir.is_dir() else []:
            summary = _subagent_transcript_summary(path, now)
            # The live session's own copy of an agent wins over an ancestor's.
            if summary and summary["agent_id"] not in discovered:
                discovered[summary["agent_id"]] = summary

    # Hook events carry precise waiting/error/stop states; overlay them when
    # available, while transcript discovery covers agents already running.
    if hook_states is None:
        hook_states = _hook_states_by_session(now)
    for state in hook_states.get(session_id, []):
        age = state["age_seconds"]
        agent_id = str(state.get("agent_id", ""))
        current = discovered.get(agent_id)
        if not agent_id or current is None:
            # Hook events also arrive for agents that never get a transcript on
            # disk. Inventing a light for those produces unlabelled ghost rows,
            # so hook state may only refine an agent transcript discovery found.
            continue
        current["age_seconds"] = min(current.get("age_seconds", age), age)
        if state.get("status") in ("waiting", "error"):
            # Only the hook can observe a permission prompt or a tool failure.
            current["status"] = state["status"]

    return [info for info in discovered.values() if _subagent_visible(info)]


def _spawn_key(info: dict) -> tuple:
    return (info.get("started_at", 0.0), info.get("agent_id", ""))


def _second_level_lights(
    session_id: str, now: datetime, hook_states: dict[str, list[dict]] | None = None
) -> list[dict]:
    """Subagents and background commands, as one ordered group of small lights."""
    visible = _discover_subagents(session_id, now, hook_states)
    visible += _background_tasks(session_id, now)

    # Which entries survive truncation is decided by how interesting they are;
    # the order they are drawn in is spawn order, so rows never jump around.
    priority = {"error": 0, "waiting": 1, "working": 2, "stale": 3, "idle": 4}
    visible.sort(key=lambda info: (priority.get(info.get("status", "working"), 9),
                                   _spawn_key(info)))
    hidden = max(0, len(visible) - MAX_SUBAGENTS)
    visible = visible[:MAX_SUBAGENTS]
    visible.sort(key=_spawn_key)
    if hidden:
        visible[-1] = dict(visible[-1], hidden_count=hidden)
    return visible


# Kept open for the manager's lifetime: closing the file releases its flock.
_SINGLETON_HANDLE = None


def _claim_singleton(timeout: float = 1.0) -> bool:
    """Take the lock that makes this process the only manager.

    Backs up _another_manager_running(), which reads the heartbeat and then
    acts on what it read. Two managers starting in the same instant both see
    the same stale heartbeat and both proceed — observed when a manual start
    raced the hook's self-healing, leaving two panels stacked on screen and a
    heartbeat file the two of them took turns overwriting, so neither could
    detect the other afterwards. An flock is atomic, and it is held by the
    process rather than written by it, so a crash cannot leak it either.

    The wait covers the gap after _kill_old_manager(): the signal has been sent,
    but the dying process still holds its descriptor for a moment.
    """
    global _SINGLETON_HANDLE
    if fcntl is None:
        return True  # no flock here; the heartbeat check is all there is
    try:
        MANAGER_SINGLETON.parent.mkdir(parents=True, exist_ok=True)
        handle = MANAGER_SINGLETON.open("wb")
    except OSError:
        return True  # unreadable lock must not stop the panel from starting
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if time.time() >= deadline:
                handle.close()
                return False
            time.sleep(0.05)
            continue
        _SINGLETON_HANDLE = handle
        return True


def _another_manager_running() -> bool:
    """Check if a different manager process is already alive (defense in depth)."""
    if not HEARTBEAT_FILE.exists():
        return False
    try:
        content = HEARTBEAT_FILE.read_text().strip()
        parts = content.split("\n", 1)
        if len(parts) != 2:
            return False
        pid = int(parts[0])
        ts = float(parts[1])
        if pid == os.getpid():
            return False
        if time.time() - ts >= 5:
            return False
        return _pid_exists(pid)
    except Exception:
        return False


def _kill_old_manager() -> None:
    """Kill any previous manager process found in the heartbeat file."""
    if not HEARTBEAT_FILE.exists():
        return
    try:
        content = HEARTBEAT_FILE.read_text().strip()
        parts = content.split("\n", 1)
        if len(parts) != 2:
            return
        pid = int(parts[0])
        if pid == os.getpid():
            return
        if not _pid_exists(pid):
            return
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            os.kill(pid, 9)
    except Exception:
        pass


class Manager:
    def __init__(self) -> None:
        # The lock comes first, and losing it means leaving the incumbent alone.
        # Killing first and checking afterwards is what produced the failure
        # this ordering exists to prevent: the newcomer shot a perfectly healthy
        # manager, then could not take the lock the dying process still held,
        # and exited — leaving no panel at all.
        if not _claim_singleton():
            sys.exit(0)

        # Holding the lock does not prove we are alone: a manager started before
        # this guard existed takes no lock. The heartbeat is the only handle on
        # that one, so it is cleared out now that we own the lock.
        if _another_manager_running():
            _kill_old_manager()

        # One-time hook registration on first manager start
        try:
            configure_hooks()
        except Exception:
            pass

        self._root = tk.Tk()
        self._root.withdraw()

        STATUS_DIR.mkdir(parents=True, exist_ok=True)
        NOTIFICATION_DIR.mkdir(parents=True, exist_ok=True)
        SUBAGENT_DIR.mkdir(parents=True, exist_ok=True)

        self._panel = Panel(self._root)
        self._known: set[str] = set()
        # Codex has no hook to write a completion notification, so its cards are
        # raised from polled state; see _codex_completions().
        self._codex_status: dict[str, str] = {}

        self._heartbeat()
        self._poll()
        self._root.mainloop()
        try:
            HEARTBEAT_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        MANAGER_LOCK.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # heartbeat
    # ------------------------------------------------------------------
    def _heartbeat(self) -> None:
        HEARTBEAT_FILE.write_text(f"{os.getpid()}\n{time.time()}")
        MANAGER_LOCK.unlink(missing_ok=True)
        self._root.after(2000, self._heartbeat)

    # ------------------------------------------------------------------
    # poll
    # ------------------------------------------------------------------
    def _poll(self) -> None:
        try:
            self._scan()
            self._scan_notifications()
        except Exception:
            pass
        self._root.after(POLL_INTERVAL, self._poll)

    def _scan(self) -> None:
        if not STATUS_DIR.exists():
            return

        now = datetime.now(timezone.utc)
        hook_states = _hook_states_by_session(now)
        seen: set[str] = set()
        entries: list[tuple[str, dict]] = []

        for fp in sorted(STATUS_DIR.glob("*.json")):
            sid = fp.stem

            try:
                data = json.loads(fp.read_text())
            except Exception:
                continue

            status = data.get("status", "idle")

            # SessionEnd → remove immediately. A session whose owning process
            # is gone gets the same treatment: it ended, it just never got to
            # say so.
            # SessionEnd, a dead owner, or a /branch fork/agent side session
            # (written by an older hook, before it learned to skip these) all
            # mean "no row": drop it and delete the file so it stays gone.
            if (
                status == "closed"
                or _session_is_dead(data)
                or is_side_session(data.get("owner_cmd", ""))
                or _session_superseded(sid)
            ):
                self._panel.remove(sid)
                self._known.discard(sid)
                fp.unlink(missing_ok=True)
                continue

            # staleness — crash safety net (600 s)
            try:
                ts = datetime.fromisoformat(data.get("timestamp", ""))
                age = (now - ts).total_seconds()
            except Exception:
                age = 99999

            if age > STALE_TIMEOUT:
                # Keep inactive sessions visible as gray instead of removing
                # them. SessionEnd remains the only event that removes a row.
                data = dict(data)
                data["status"] = "stale"

            # Overrides whatever the hook guessed: the hook only sees the
            # drifting cwd, while the transcript records where we started.
            project_name, project_root = _project_identity(
                _live_project_cwd(sid, data.get("cwd", ""))
            )
            data["project_name"] = project_name
            data["project_root"] = project_root
            data["agent_kind"] = data.get("agent_kind") or "claude"
            # A session idle for STALE_TIMEOUT cannot have a fresh subagent,
            # so skip its transcript scan entirely.
            data["subagents"] = (
                []
                if data["status"] == "stale"
                else _second_level_lights(sid, now, hook_states)
            )

            seen.add(sid)
            entries.append((sid, data))

        if CODEX_ENABLED:
            # Codex is polled rather than hooked, so its rows are produced here
            # instead of arriving as status files. Keys are namespaced because a
            # Codex session id could otherwise collide with a Claude one.
            try:
                discovered = codex.discover_sessions()
                for payload in discovered:
                    project_name, project_root = _project_identity(payload.get("cwd", ""))
                    payload["project_name"] = project_name
                    payload["project_root"] = project_root
                    key = f"codex:{payload['session_id']}"
                    seen.add(key)
                    entries.append((key, payload))
                self._codex_completions(discovered)
            except Exception:
                pass

        # Sort by prompt_time DESC — newest packed first (at top), oldest at bottom
        entries.sort(key=lambda x: x[1].get("prompt_time", ""), reverse=True)

        for sid, data in entries:
            self._panel.upsert(sid, data)

        # clean up entries for files that disappeared
        gone = self._known - seen
        for sid in gone:
            self._panel.remove(sid)
        self._known = seen

    def _codex_completions(self, payloads: list[dict]) -> None:
        """Raise a completion card for each Codex turn that just finished.

        Claude Code gets its card from a hook: Stop fires, the hook drops a
        notification file, _scan_notifications picks it up. Codex is watched by
        polling only — nothing is ever written into CODEX_HOME, and registering
        a Codex hook would need its trust gate — so the same transition has to
        be spotted here, from working to idle between two polls.

        A session is only ever announced once its previous status is known, so
        the sessions already sitting idle when the manager starts do not all
        announce themselves at once.
        """
        live = set()
        for payload in payloads:
            session_id = payload.get("session_id", "")
            if not session_id:
                continue
            live.add(session_id)
            status = payload.get("status", "")
            previous = self._codex_status.get(session_id)
            self._codex_status[session_id] = status
            if previous == "working" and status == "idle":
                self._panel.show_completion(dict(payload))
        # Forget sessions that closed, so a reused id is treated as first sight.
        for session_id in set(self._codex_status) - live:
            del self._codex_status[session_id]

    def _scan_notifications(self) -> None:
        for fp in sorted(NOTIFICATION_DIR.glob("*.json")):
            try:
                data = json.loads(fp.read_text())
                self._panel.show_completion(data)
            except Exception:
                pass
            finally:
                fp.unlink(missing_ok=True)


def main() -> None:
    HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    Manager()


if __name__ == "__main__":
    main()
