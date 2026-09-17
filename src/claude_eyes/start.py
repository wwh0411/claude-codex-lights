"""Called from SessionStart hook — ensures manager daemon is running."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from claude_eyes.config import BASE_DIR, LOCK_TIMEOUT, MANAGER_LOCK

HEARTBEAT_FILE = BASE_DIR / "manager.heartbeat"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
BACKUP_PATH = BASE_DIR / "settings_backup.json"

# Commands to inject into settings.json hooks. Use the interpreter running the
# setup/manager so self-healing never falls back to an unrelated system Python.
# Deliberately unresolved: resolve() would follow the venv symlink to uv's
# managed interpreter, which cannot import claude_eyes or AppKit.
PYTHON_COMMAND = sys.executable
REQUIRED_HOOKS: dict[str, list[str]] = {
    "SessionStart": [
        f"{PYTHON_COMMAND} -m claude_eyes.start",
        f"{PYTHON_COMMAND} -m claude_eyes.hook",
    ],
    "SubagentStart": [f"{PYTHON_COMMAND} -m claude_eyes.hook"],
    "SubagentStop": [f"{PYTHON_COMMAND} -m claude_eyes.hook"],
    "UserPromptSubmit": [f"{PYTHON_COMMAND} -m claude_eyes.hook"],
    "PermissionRequest": [f"{PYTHON_COMMAND} -m claude_eyes.hook"],
    "PreToolUse": [f"{PYTHON_COMMAND} -m claude_eyes.hook"],
    "PostToolUse": [f"{PYTHON_COMMAND} -m claude_eyes.hook"],
    "PostToolUseFailure": [f"{PYTHON_COMMAND} -m claude_eyes.hook"],
    "Stop": [f"{PYTHON_COMMAND} -m claude_eyes.hook"],
    "SessionEnd": [f"{PYTHON_COMMAND} -m claude_eyes.hook"],
}


def _is_ours(cmd: str) -> bool:
    return "claude_eyes.hook" in cmd or "claude_eyes.start" in cmd


def is_configured() -> bool:
    """Check that every required hook runs the *current* interpreter.

    Matching the exact command matters: a claude-codex-lights hook left pointing at
    another interpreter (uv's managed python, a deleted venv) still looks
    registered but cannot import claude_eyes, so it must be healed.
    """
    if not SETTINGS_PATH.exists():
        return False
    try:
        config = json.loads(SETTINGS_PATH.read_text())
        hooks = config.get("hooks", {})
    except Exception:
        return False
    if not isinstance(hooks, dict):
        return False
    for event, cmds in REQUIRED_HOOKS.items():
        existing = hooks.get(event, [])
        if not isinstance(existing, list):
            return False
        registered = {
            h.get("command", "")
            for entry in existing
            if isinstance(entry, dict)
            for h in entry.get("hooks", [])
            if isinstance(h, dict)
        }
        if not all(cmd in registered for cmd in cmds):
            return False
    return True


def configure_hooks() -> bool:
    """Add claude-codex-lights hooks to settings.json. Returns True if changes made.

    Backs up the original config first. Idempotent — safe to call repeatedly.
    """
    if not SETTINGS_PATH.exists():
        return False
    if is_configured():
        return False

    BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SETTINGS_PATH, BACKUP_PATH)

    config = json.loads(SETTINGS_PATH.read_text())
    hooks = config.get("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}

    for event, cmds in REQUIRED_HOOKS.items():
        existing = hooks.get(event, [])
        if not isinstance(existing, list):
            existing = []
        # Remove old entries to avoid duplicates
        cleaned = [e for e in existing if not any(
            _is_ours(h.get("command", "")) for h in e.get("hooks", [])
        )]
        for cmd in cmds:
            cleaned.append({
                "matcher": "",
                "hooks": [{"type": "command", "command": cmd}],
            })
        hooks[event] = cleaned

    config["hooks"] = hooks
    SETTINGS_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False))
    return True


def _pid_exists(pid: int) -> bool:
    if sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True,
        )
        return str(pid) in result.stdout
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _manager_alive() -> bool:
    if not HEARTBEAT_FILE.exists():
        return False
    try:
        content = HEARTBEAT_FILE.read_text().strip()
        parts = content.split("\n", 1)
        if len(parts) == 2:
            pid = int(parts[0])
            ts = float(parts[1])
        else:
            # Legacy format: just a timestamp, no PID
            ts = float(parts[0])
            pid = None
        age = time.time() - ts
        if age >= 5:
            return False
        if pid is not None:
            return _pid_exists(pid)
        return True
    except Exception:
        return False


def acquire_startup_lock() -> bool:
    """Atomically acquire the manager startup lock. Returns True if caller
    should proceed to start the manager (either fresh lock acquired, or
    stale lock detected and the caller chooses to proceed).

    Uses O_CREAT|O_EXCL for atomic creation.  Falls back to replacing a
    stale lock file — the os.O_EXCL in the retry ensures at most one
    caller wins.
    """
    try:
        fd = os.open(MANAGER_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        pass
    except OSError:
        return False

    # Lock file exists — check freshness
    try:
        age = time.time() - MANAGER_LOCK.stat().st_mtime
    except OSError:
        return False

    if age < LOCK_TIMEOUT:
        return False  # fresh lock held by another process

    # Stale lock — try to steal it atomically
    try:
        MANAGER_LOCK.unlink(missing_ok=True)
        fd = os.open(MANAGER_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except (FileExistsError, OSError):
        return False


def main() -> None:
    # Self-healing: fix missing hooks on every SessionStart
    if configure_hooks():
        print("claude-codex-lights: hooks registered in settings.json")

    if _manager_alive():
        sys.exit(0)

    MANAGER_LOCK.parent.mkdir(parents=True, exist_ok=True)
    if not acquire_startup_lock():
        sys.exit(0)

    try:
        if _manager_alive():
            sys.exit(0)
        manager_py = Path(__file__).resolve().parent / "manager.py"
        if sys.platform == "win32":
            subprocess.Popen(
                ["pythonw", str(manager_py)],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            subprocess.Popen(
                [sys.executable, str(manager_py)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
