"""Claude Code hook entry point. Reads stdin JSON, writes status + history."""
# Required for the `X | None` annotations below: hook.py is what settings.json
# runs on every event, so it must import on the oldest interpreter we claim to
# support (3.9). Without this, every light silently stops updating there.
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from claude_eyes.config import (
    CLAUDE_SETTINGS, CONTEXT_LIMIT_DEFAULT, CONTEXT_LIMIT_LONG,
    PARSE_DIR, PARSE_FINGERPRINT_BYTES,
    STATUS_DIR, LOG_DIR, MANAGER_LOCK, NOTIFICATION_DIR, SUBAGENT_DIR,
    PROMPT_STORE_MAX, LOG_MAX_BYTES,
)
from claude_eyes.start import acquire_startup_lock, _manager_alive


def _ensure_manager() -> None:
    """Start manager daemon if heartbeat is stale or missing."""
    if _manager_alive():
        return

    MANAGER_LOCK.parent.mkdir(parents=True, exist_ok=True)
    if not acquire_startup_lock():
        return

    try:
        if _manager_alive():
            return
        manager_py = Path(__file__).resolve().parent / "manager.py"
        if sys.platform == "win32":
            subprocess.Popen(["pythonw", str(manager_py)], creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            subprocess.Popen(
                [sys.executable, str(manager_py)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception:
        pass

EVENT_STATUS = {
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "PostToolUse": "working",
    "PreCompact": "compacting",
    "SubagentStop": "working",
    "SubagentStart": "working",
    "PermissionRequest": "waiting",
    "Stop": "idle",
    "SessionStart": "idle",
    "SessionEnd": "closed",
    "PostToolUseFailure": "error",
}

# Events that count as "user spoke"
USER_EVENTS = {"UserPromptSubmit"}
# Events that count as "claude finished"
CLAUDE_EVENTS = {"Stop", "PostToolUse", "PostToolUseFailure"}


def _text_content(content: object) -> str:
    """Extract readable text from a Claude transcript message payload."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts).strip()


def _is_real_user_prompt(text: str) -> bool:
    normalized = text.lstrip()
    ignored_prefixes = (
        "<system-reminder>",
        "<local-command-stdout>",
        "<local-command-caveat>",
        "<task-notification>",
        "<tool-use-result>",
        "<command-name>",
        "<command-message>",
        "<command-args>",
    )
    return bool(normalized) and not normalized.startswith(ignored_prefixes)


_COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)


def _slash_command_text(text: str) -> str:
    """Render a slash-command invocation as the line the user typed."""
    name = _COMMAND_NAME_RE.search(text)
    if name is None:
        return ""
    command = " ".join(name.group(1).split())
    if not command:
        return ""
    args_match = _COMMAND_ARGS_RE.search(text)
    args = " ".join(args_match.group(1).split()) if args_match else ""
    return f"{command} {args}".strip()


def _user_prompt_text(text: str) -> str:
    """Displayable form of a user message, or "" when it is machine-injected.

    Slash commands arrive wrapped in <command-name> tags; they are real user
    input, so they are shown as the typed command rather than dropped.
    """
    normalized = text.lstrip()
    if normalized.startswith("<command-name>"):
        return _slash_command_text(normalized)
    return normalized.strip() if _is_real_user_prompt(normalized) else ""


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


def _find_transcript(data: dict, session_id: str) -> Path | None:
    explicit = data.get("transcript_path") or data.get("transcriptPath")
    if isinstance(explicit, str) and explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return candidate
    root = Path.home() / ".claude" / "projects"
    try:
        return next(root.glob(f"*/{session_id}.jsonl"))
    except (StopIteration, OSError):
        return None


# (settings path, mtime) -> base model names configured with a 1M window.
_SETTINGS_CACHE: dict = {"key": None, "models": frozenset()}


def _long_context_models() -> frozenset:
    """Base model names that settings.json configures with the `[1m]` suffix.

    Needed because the transcript states the window nowhere, and its own model
    field drops the suffix: the same request is `claude-opus-5-0` in the
    assistant message and `claude-opus-5-0[1m]` in cost-state. Settings carries
    the suffixed form from the session's first turn, whereas cost-state is
    written rarely and late — leaving early turns to be measured against a
    200K default that inflates the meter fivefold.

    Matching is on the base name rather than "settings say 1M, so this session
    is 1M": a session switched to another model with /model must not inherit the
    default's window.
    """
    try:
        key = (str(CLAUDE_SETTINGS), CLAUDE_SETTINGS.stat().st_mtime)
    except OSError:
        return frozenset()
    if _SETTINGS_CACHE["key"] == key:
        return _SETTINGS_CACHE["models"]
    try:
        config = json.loads(CLAUDE_SETTINGS.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    candidates = [config.get("model")]
    env = config.get("env")
    if isinstance(env, dict):
        candidates.extend(env.values())
    models = frozenset(
        name.split("[")[0]
        for name in candidates
        if isinstance(name, str) and "[1m]" in name and name.split("[")[0]
    )
    _SETTINGS_CACHE.update({"key": key, "models": models})
    return models


def _context_limit(model: str, long_context: bool, context_tokens: int) -> int:
    """The session's context window, which the transcript never states outright.

    Four signals, strongest first: a cost-state record naming a `[1m]` model,
    the suffix on the model itself, the model being one settings configures as
    long-context, and a context that has already outgrown the standard window —
    which only a long-context session could have done.
    """
    if long_context or "[1m]" in model:
        return CONTEXT_LIMIT_LONG
    if model and model in _long_context_models():
        return CONTEXT_LIMIT_LONG
    if context_tokens > CONTEXT_LIMIT_DEFAULT:
        return CONTEXT_LIMIT_LONG
    return CONTEXT_LIMIT_DEFAULT


def _usage_input_total(usage: dict) -> int:
    """Every input token the request carried, cached or not."""
    return sum(
        int(usage.get(key, 0) or 0)
        for key in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )


def _empty_metadata(title: str, latest_prompt: str) -> dict:
    return {
        "conversation_title": title,
        "last_prompt": latest_prompt,
        "user_turns": 0,
        "model": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "context_tokens": 0,
        "context_limit": CONTEXT_LIMIT_DEFAULT,
        "compact_count": 0,
        "compact_trigger": "",
        "compact_dropped": 0,
        "cost_usd": None,
    }


def _new_parse_state() -> dict:
    """Everything derived from a transcript, as a value that can be resumed.

    Folding record by record into a plain dict is what lets one hook event pick
    up where the last one stopped: the full and the incremental parse run the
    same _fold, so they cannot drift apart.
    """
    return {
        "title": "",
        "last_prompt": "",
        "user_turns": 0,
        "model": "",
        # message id -> usage, cleared at each user turn: this is the only piece
        # of state scoped to a turn rather than to the whole conversation.
        "turn_usage": {},
        "context_tokens": 0,
        "compact_count": 0,
        "compact_trigger": "",
        "compact_dropped": 0,
        "cost_usd": None,
        "long_context": False,
        "offset": 0,
        "fingerprint": "",
    }


def _fold(state: dict, item: dict) -> None:
    """Apply one transcript record to the running state."""
    kind = item.get("type")
    if kind == "custom-title" and isinstance(item.get("customTitle"), str):
        state["title"] = item["customTitle"].strip()
    elif kind == "agent-name" and not state["title"] and isinstance(item.get("agentName"), str):
        state["title"] = item["agentName"].strip()
    elif kind == "last-prompt" and isinstance(item.get("lastPrompt"), str):
        candidate = _user_prompt_text(item["lastPrompt"])
        if candidate:
            state["last_prompt"] = candidate
    elif kind == "system" and item.get("subtype") == "compact_boundary":
        state["compact_count"] += 1
        meta = item.get("compactMetadata")
        if isinstance(meta, dict):
            if isinstance(meta.get("trigger"), str):
                state["compact_trigger"] = meta["trigger"]
            state["compact_dropped"] = int(meta.get("cumulativeDroppedTokens", 0) or 0)
    elif kind == "cost-state":
        # Claude Code writes this sporadically (not per turn), so the last one
        # seen is the freshest figure available — it may lag the session.
        total = item.get("totalCostUSD")
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            state["cost_usd"] = float(total)
        usage_by_model = item.get("modelUsage")
        if isinstance(usage_by_model, dict):
            state["long_context"] = state["long_context"] or any(
                "[1m]" in name for name in usage_by_model if isinstance(name, str)
            )
    elif (
        kind == "user"
        and not item.get("toolUseResult")
        and not item.get("sourceToolAssistantUUID")
        # The summary compaction leaves behind is a plain user message; left in,
        # it would be shown as the user's latest question and counted as a turn
        # they never took.
        and not item.get("isCompactSummary")
        and not item.get("isVisibleInTranscriptOnly")
    ):
        message = item.get("message")
        if isinstance(message, dict):
            text = _user_prompt_text(_text_content(message.get("content")))
            if text:
                state["last_prompt"] = text
                state["user_turns"] += 1
                state["turn_usage"] = {}
    elif kind == "assistant":
        message = item.get("message")
        if not isinstance(message, dict):
            return
        if isinstance(message.get("model"), str):
            state["model"] = message["model"]
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return
        if not item.get("isSidechain"):
            # A subagent's request carries its own context, not this session's,
            # so it must not overwrite the main thread's size.
            state["context_tokens"] = _usage_input_total(usage)
        message_id = message.get("id") or item.get("uuid")
        if isinstance(message_id, str):
            # Claude may append multiple snapshots for the same streamed
            # message. Keeping the latest snapshot avoids double-counting.
            state["turn_usage"][message_id] = usage


def _fingerprint(head: bytes) -> str:
    """Identity of a transcript's opening bytes.

    Resuming at a byte offset is only safe while the file is the same one that
    offset was measured in. A shrunk file is caught by the size check; this
    catches a rewrite that kept the length or grew.
    """
    return hashlib.sha1(head).hexdigest()


def _parse_state_path(session_id: str) -> Path:
    return PARSE_DIR / f"{session_id}.json"


def _load_parse_state(session_id: str, size: int, fingerprint: str) -> dict:
    """Resumable state for this transcript, or a fresh one to parse it whole."""
    try:
        cached = json.loads(_parse_state_path(session_id).read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return _new_parse_state()
    if not isinstance(cached, dict) or cached.get("fingerprint") != fingerprint:
        return _new_parse_state()
    if size < int(cached.get("offset", 0) or 0):
        return _new_parse_state()  # truncated: whatever it is now, it is new
    state = _new_parse_state()
    state.update(cached)
    if not isinstance(state.get("turn_usage"), dict):
        state["turn_usage"] = {}
    return state


def _save_parse_state(session_id: str, state: dict) -> None:
    try:
        PARSE_DIR.mkdir(parents=True, exist_ok=True)
        _parse_state_path(session_id).write_text(json.dumps(state, ensure_ascii=False))
    except OSError:
        pass


def _finalize(state: dict, direct_prompt: str) -> dict:
    input_tokens = 0
    output_tokens = 0
    for usage in state["turn_usage"].values():
        if not isinstance(usage, dict):
            continue
        input_tokens += _usage_input_total(usage)
        output_tokens += int(usage.get("output_tokens", 0) or 0)
    model = state["model"]
    context_tokens = state["context_tokens"]
    return {
        "conversation_title": state["title"],
        # The event's own prompt wins when it has one. UserPromptSubmit carries
        # the just-typed prompt, and it fires BEFORE that turn is written to the
        # transcript — so the transcript's newest user message is still the
        # PREVIOUS turn's. Preferring it would pin the panel to the last prompt
        # until Stop. The transcript is the source only when the event carries
        # no prompt of its own (every event except UserPromptSubmit).
        "last_prompt": direct_prompt or state["last_prompt"],
        "user_turns": state["user_turns"],
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "context_tokens": context_tokens,
        "context_limit": _context_limit(model, state["long_context"], context_tokens),
        "compact_count": state["compact_count"],
        "compact_trigger": state["compact_trigger"],
        "compact_dropped": state["compact_dropped"],
        "cost_usd": state["cost_usd"],
    }


def _conversation_metadata(data: dict, session_id: str) -> dict:
    """Read title, prompt, turns, model, usage, context size, compaction, cost.

    Only the bytes appended since the previous hook event are parsed; the state
    they fold into is kept beside the status file. A full re-read costs 55 ms on
    a 16 MB transcript and used to happen on every SessionStart, UserPromptSubmit
    and Stop.
    """
    direct_prompt = data.get("prompt")
    latest_prompt = (
        _user_prompt_text(direct_prompt) if isinstance(direct_prompt, str) else ""
    )
    transcript = _find_transcript(data, session_id)
    if transcript is None:
        return _empty_metadata("", latest_prompt)

    # One open for both jobs: on a large transcript the open itself dominates,
    # and the fingerprint used to pay for a second one.
    try:
        with transcript.open("rb") as fh:
            head = fh.read(PARSE_FINGERPRINT_BYTES)
            fingerprint = _fingerprint(head)
            size = fh.seek(0, os.SEEK_END)
            state = _load_parse_state(session_id, size, fingerprint)
            fh.seek(state["offset"])
            chunk = fh.read()
    except OSError:
        return _empty_metadata("", latest_prompt)

    # Only whole records advance the offset: a hook can run while Claude Code is
    # midway through writing a line, and the rest arrives on the next event.
    end = chunk.rfind(b"\n")
    if end != -1:
        for raw in chunk[: end + 1].decode("utf-8", "replace").splitlines():
            try:
                item = json.loads(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if isinstance(item, dict):
                _fold(state, item)
        state["offset"] += end + 1
    state["fingerprint"] = fingerprint
    _save_parse_state(session_id, state)

    # A half-written record is not committed — the next event re-reads it from
    # the same offset — but it does count towards the answer this event returns,
    # so a Stop landing mid-flush still reports the turn that just ended. Folded
    # after the save on purpose: the state on disk must stay at the last record
    # known to be whole.
    tail = chunk[end + 1:].strip()
    if tail:
        try:
            item = json.loads(tail.decode("utf-8", "replace"))
        except (json.JSONDecodeError, TypeError, ValueError):
            item = None
        if isinstance(item, dict):
            _fold(state, item)
    return _finalize(state, latest_prompt)


def _elapsed_seconds(start_iso: str | None, end: datetime) -> float | None:
    if not start_iso:
        return None
    try:
        start = datetime.fromisoformat(start_iso)
        return max(0.0, (end - start).total_seconds())
    except (TypeError, ValueError):
        return None


def _cap_history(path: Path) -> None:
    """Keep history.jsonl under LOG_MAX_BYTES by dropping its oldest lines.

    Nothing reads this file back for logic, so trimming is safe. Only rewrites
    when the cap is crossed; then keeps roughly the last half, cut at a line
    boundary so no partial JSON survives.
    """
    try:
        if path.stat().st_size <= LOG_MAX_BYTES:
            return
        with open(path, "rb") as fh:
            fh.seek(-LOG_MAX_BYTES // 2, 2)
            tail = fh.read()
        cut = tail.find(b"\n")
        tail = tail[cut + 1:] if cut != -1 else tail
        with open(path, "wb") as fh:
            fh.write(tail)
    except OSError:
        pass


def _write_completion_notification(payload: dict) -> None:
    NOTIFICATION_DIR.mkdir(parents=True, exist_ok=True)
    stamp = int(datetime.now(timezone.utc).timestamp() * 1000)
    path = NOTIFICATION_DIR / f"{stamp}-{payload['session_id']}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def _handle_subagent_event(data: dict, event: str, session_id: str, now_iso: str) -> dict:
    agent_id = str(data.get("agent_id") or data.get("agentId") or "").strip()
    if not agent_id:
        return {"status": "skipped", "reason": "invalid agent_id"}
    status = {
        "PermissionRequest": "waiting",
        "PostToolUseFailure": "error",
        "SubagentStop": "idle",
    }.get(event, "working")
    transcript = data.get("agent_transcript_path") or data.get("transcript_path") or ""
    payload = {
        "parent_session_id": session_id,
        "agent_id": agent_id,
        "agent_type": data.get("agent_type") or data.get("agentType") or "subagent",
        "status": status,
        "timestamp": now_iso,
        "transcript_path": transcript,
    }
    SUBAGENT_DIR.mkdir(parents=True, exist_ok=True)
    safe_agent_id = agent_id.replace("/", "_")
    path = SUBAGENT_DIR / f"{session_id}--{safe_agent_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def _owner_command(pid: int) -> str:
    """The command line of `pid`, or "" if it cannot be read.

    Costs a subprocess, so callers must do this once per session rather than on
    every hook event.
    """
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
        return " ".join(out.stdout.split())
    except Exception:
        return ""


def is_side_session(owner_cmd: str) -> bool:
    """Whether this Claude process is a throwaway agent runner, not a session.

    `/branch` spawns an `--agent <name>` process under the daemon's PTY host to
    do the branching; it runs the hook and would otherwise write a top-level
    status file with an empty title — a ghost row the user never opened.

    NOTE: `--fork-session` is deliberately NOT treated as machinery. Filtering
    it once looked right — a fresh branch has an empty title — but continuing to
    work in a branch (`/branch` then keep going, launched as
    `--fork-session --resume`) makes that fork the user's real, live session.
    Hiding it hid active conversations. A superseded parent is dropped instead
    by its own `continued-in` record; see Manager._session_superseded.

    A subagent is not caught here and does not need to be: it takes the
    agent-event path before this, and its light is drawn under its parent.
    """
    return "--agent" in owner_cmd.split()


def _owner_identity(prev: dict) -> dict:
    """Identify the Claude Code process this hook is running under.

    Claude Code spawns hooks as direct children — verified on 2.1.250, where
    getppid() is the `claude` process itself with no shell in between. Recording
    it lets the manager tell "session killed from the terminal" (its pid is
    gone) from "session merely idle", which is otherwise indistinguishable: a
    session that never gets a SessionEnd leaves its status file behind forever.

    The command line is only read once per session — reading it costs a
    subprocess, and this runs on every tool call. A parent that does not look
    like Claude Code is recorded as such, so the manager knows not to trust the
    pid rather than treating a dead shell's pid as a dead session.
    """
    pid = os.getppid()
    if prev.get("owner_pid") == pid and prev.get("owner_cmd"):
        return {"owner_pid": pid, "owner_cmd": prev["owner_cmd"]}
    return {"owner_pid": pid, "owner_cmd": _owner_command(pid)}


def handle(stdin_text: str = "") -> dict:
    """Process hook input and return the status payload written to disk."""
    if not stdin_text:
        try:
            stdin_text = sys.stdin.read()
        except Exception:
            pass

    try:
        data = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        data = {}

    event = data.get("hook_event_name", "")
    session_id = data.get("session_id", "")
    if not session_id or session_id == "unknown":
        return {"status": "skipped", "reason": "invalid session_id"}

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    if data.get("agent_id") or data.get("agentId"):
        return _handle_subagent_event(data, event, session_id, now_iso)

    tool_name = data.get("tool_name", "")
    cwd = data.get("cwd", "")

    status = EVENT_STATUS.get(event, "working")

    # --- read existing file to preserve dual timestamps ---
    status_file = STATUS_DIR / f"{session_id}.json"
    prev: dict = {}
    try:
        prev = json.loads(status_file.read_text())
    except Exception:
        pass

    prompt_time = prev.get("prompt_time")
    done_time = prev.get("done_time")
    conversation_title = prev.get("conversation_title", "")
    last_prompt = prev.get("last_prompt", "")
    user_turns = prev.get("user_turns", 0)
    model = prev.get("model", "")
    input_tokens = prev.get("input_tokens", 0)
    output_tokens = prev.get("output_tokens", 0)
    context_tokens = prev.get("context_tokens", 0)
    context_limit = prev.get("context_limit", CONTEXT_LIMIT_DEFAULT)
    compact_count = prev.get("compact_count", 0)
    compact_trigger = prev.get("compact_trigger", "")
    compact_dropped = prev.get("compact_dropped", 0)
    cost_usd = prev.get("cost_usd")
    effective_cwd = cwd or prev.get("cwd", "")
    project_name, project_root = _project_identity(effective_cwd)

    # Metadata normally refreshes on turn boundaries. The extra `not last_prompt`
    # case recovers a session whose hooks were installed (or repaired) midway,
    # so its UserPromptSubmit was never seen; it stops re-reading once filled.
    if event in {"SessionStart", "UserPromptSubmit", "Stop"} or not last_prompt:
        metadata = _conversation_metadata(data, session_id)
        conversation_title = metadata["conversation_title"] or conversation_title
        last_prompt = metadata["last_prompt"] or last_prompt
        user_turns = metadata["user_turns"] or user_turns
        model = metadata["model"] or model
        # `or` fallback matters: a failed/empty parse must not wipe counts we
        # already have, which would also re-arm the `not last_prompt` recovery.
        input_tokens = metadata["input_tokens"] or input_tokens
        output_tokens = metadata["output_tokens"] or output_tokens
        context_tokens = metadata["context_tokens"] or context_tokens
        context_limit = metadata["context_limit"] or context_limit
        compact_count = metadata["compact_count"] or compact_count
        compact_trigger = metadata["compact_trigger"] or compact_trigger
        compact_dropped = metadata["compact_dropped"] or compact_dropped
        # cost-state is written rarely, so a transcript that has none yet must
        # keep the last figure rather than blanking the card mid-session.
        if metadata["cost_usd"] is not None:
            cost_usd = metadata["cost_usd"]

    if event in USER_EVENTS:
        prompt_time = now_iso
    if event in CLAUDE_EVENTS:
        done_time = now_iso
    if event == "SessionStart":
        prompt_time = now_iso
        done_time = now_iso

    payload = {
        "status": status,
        "session_id": session_id,
        "tool_name": tool_name,
        "last_event": event,
        "cwd": effective_cwd,
        "project_name": project_name,
        "project_root": project_root,
        "conversation_title": conversation_title,
        "last_prompt": last_prompt[:PROMPT_STORE_MAX] if isinstance(last_prompt, str) else last_prompt,
        "user_turns": user_turns,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "context_tokens": context_tokens,
        "context_limit": context_limit,
        "compact_count": compact_count,
        "compact_trigger": compact_trigger,
        "compact_dropped": compact_dropped,
        "cost_usd": cost_usd,
        "prompt_time": prompt_time or now_iso,
        "done_time": done_time or now_iso,
        "timestamp": now_iso,
        **_owner_identity(prev),
    }

    if event == "SessionEnd":
        # The status file is the manager's to remove; the parse state is ours,
        # and nothing else would ever clean it up.
        try:
            _parse_state_path(session_id).unlink(missing_ok=True)
        except OSError:
            pass

    if is_side_session(payload.get("owner_cmd", "")):
        # /branch fork/agent process — never a session the user is watching, so
        # it gets no status file and no row.
        return {"status": "skipped", "reason": "branch side session"}

    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    history = LOG_DIR / "history.jsonl"
    with open(history, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    _cap_history(history)

    # A Stop event means the current assistant turn completed. Avoid duplicate
    # popups if Claude emits the same Stop event more than once.
    if event == "Stop" and prev.get("last_event") != "Stop":
        notification = dict(payload)
        notification["elapsed_seconds"] = _elapsed_seconds(prompt_time, now)
        _write_completion_notification(notification)

    return payload


def main() -> None:
    _ensure_manager()
    handle()
    sys.exit(0)


if __name__ == "__main__":
    main()
