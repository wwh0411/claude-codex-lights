"""One-time hook setup: register claude-codex-lights hooks in settings.json.

Usage:
    python -m claude_eyes.setup
"""

import sys

from claude_eyes.start import configure_hooks, is_configured


def main() -> None:
    if is_configured():
        print("claude-codex-lights hooks are already configured.")
    elif configure_hooks():
        print("claude-codex-lights hooks registered. Restart Claude Code to activate.")
    else:
        print("No settings.json found. Skipped hook registration.")
    sys.exit(0)


if __name__ == "__main__":
    main()
