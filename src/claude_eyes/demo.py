"""Demo mode — write fake status files to preview any state without real sessions.

Usage:
    python -m claude_eyes.demo              # create full demo (4 states)
    python -m claude_eyes.demo clear        # remove all demo entries
    python -m claude_eyes.demo add <name> <status>  # add one entry
"""

import json
import sys
import uuid
from datetime import datetime, timezone

from claude_eyes.config import CONTEXT_LIMIT_DEFAULT, STATUS_DIR

VALID_STATUSES = {"working", "compacting", "waiting", "idle", "error"}

# Context fill per demo state, chosen to walk the meter through all three of its
# colours so one preview shows every band.
DEMO_CONTEXT = {
    "working": (82_000, 0, 3.4),
    "compacting": (196_000, 1, 61.2),
    "waiting": (150_000, 0, 18.9),
    "idle": (188_000, 2, 244.0),
    "error": (40_000, 0, 0.0),
}


def _write(sid: str, project: str, status: str, cwd: str = "") -> None:
    now = datetime.now(timezone.utc).isoformat()
    context_tokens, compactions, cost = DEMO_CONTEXT.get(status, (0, 0, 0.0))
    payload = {
        "status": status,
        "session_id": sid,
        "tool_name": "",
        "last_event": "demo",
        "cwd": cwd or f"/home/{project}",
        "context_tokens": context_tokens,
        "context_limit": CONTEXT_LIMIT_DEFAULT,
        "compact_count": compactions,
        "compact_trigger": "auto" if compactions else "",
        "cost_usd": cost,
        "prompt_time": now,
        "done_time": now,
        "timestamp": now,
    }
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    (STATUS_DIR / f"{sid}.json").write_text(json.dumps(payload, ensure_ascii=False))
    print(f"  + {project:20s} [{status}]")


def cmd_add(args: list[str]) -> None:
    if len(args) < 2:
        print("Usage: python -m claude_eyes.demo add <project_name> <status>")
        print(f"  status: {', '.join(sorted(VALID_STATUSES))}")
        return
    name, status = args[0], args[1]
    if status not in VALID_STATUSES:
        print(f"Invalid status '{status}'. Choose: {', '.join(sorted(VALID_STATUSES))}")
        return
    sid = f"demo-{uuid.uuid4().hex[:8]}"
    _write(sid, name, status)


def cmd_demo() -> None:
    cmd_clear()
    print("Creating demo entries...")
    _write("demo-working", "my-app", "working")
    _write("demo-compacting", "long-session", "compacting")
    _write("demo-waiting", "deploy-script", "waiting")
    _write("demo-idle", "utils-lib", "idle")
    _write("demo-error", "broken-service", "error")
    print("Done — panel should show 5 lights now.")


def cmd_clear() -> None:
    count = 0
    for fp in STATUS_DIR.glob("demo-*.json"):
        fp.unlink(missing_ok=True)
        count += 1
    if count:
        print(f"Removed {count} demo entries.")


def main() -> None:
    if len(sys.argv) < 2:
        cmd_demo()
        return
    cmd = sys.argv[1]
    if cmd == "demo":
        cmd_demo()
    elif cmd == "clear":
        cmd_clear()
    elif cmd == "add":
        cmd_add(sys.argv[2:])
    else:
        print(f"Usage: python -m claude_eyes.demo [demo|clear|add <name> <status>]")
        print(f"  status: {', '.join(sorted(VALID_STATUSES))}")


if __name__ == "__main__":
    main()
