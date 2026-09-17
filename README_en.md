# Claude Codex Lights

A floating traffic-light panel for your **Claude Code** and **Codex CLI** sessions. One dot per
session, always on top, telling you at a glance which one is working, which one has been waiting
on a permission prompt for ten minutes, and which one died.

*中文文档：[README.md](README.md)*

```
┌─────────────────────────────────────────────────┐
│ 📁 my-project                          2 sessions│  ← project group
├─────────────────────────────────────────────────┤
│  ◉  🅐 refactor_pipeline           22:09:52     │  ← session card
│     split the pipeline in three        working  │
│     ● Write pipeline task recipes      working  │  ← sub-light: subagent
│     ■ npm run dev                      running  │  ← sub-light: background task
│     ▁▁▁▁▁▁▁▁▃▃▃ 213k/1.0M 21%  ⟲2  ≈$1.7       │  ← context meter
├─────────────────────────────────────────────────┤
│  ●  🅞 codex_eval_sweep            22:04:14     │
│     run the sweep once more            done     │
└─────────────────────────────────────────────────┘
```

## Why

Once you keep seven or eight agent sessions open, the expensive part is not waiting — it is not
knowing **which window to go back to**. The panel flattens that into one glance: one light per
session, grouped by project.

## Features

**Three levels**

| Level | Content |
|---|---|
| Project group | Grouped by the session's launch directory (Git root), with session count |
| Session card | Status light + CLI badge (Claude orange / Codex white) + conversation title + latest user prompt + time + state |
| Sub-lights | Subagents (**dots**) and background tasks (**squares**) spawned by that session, with short labels |

**What the colors mean**

| Color | Session | Subagent | Background task |
|---|---|---|---|
| 🔵 blue (breathing) | working | working | running |
| 🟡 yellow (blinking) | waiting for your confirmation | waiting | — |
| 🟢 green (solid) | turn complete | done | exit code 0 |
| 🔴 red (blinking) | error | error | exit code N |
| 🟠 orange (blinking) | compacting context | — | — |
| ⚪ gray | idle > 30 min | stuck > 5 min | terminated |

Rows disappear when the session ends; going idle only turns them gray. Finished subagents and
background tasks stay for 10 minutes, capped at 8 per session ("N more" beyond that).

**Context meter** — for both Claude and Codex sessions: `used/limit percent`, amber at 70%, red at
90%, `⟲N` for sessions that have been compacted, and an `≈$` figure only when the transcript
actually records a cost (never guessed from public price lists).

**Completion card** — when a turn finishes, a card appears top-right for 12 seconds with the turn
duration, conversation turn count, input/output tokens and model name. Click anywhere on it to
dismiss immediately.

**Sound cues** — only two transitions make a sound: turning yellow (blocked on a confirmation) and
turning red (failure). Those are exactly the states where a light you are not looking at is
useless. Fired on transition only, never per poll.

**Codex support** — read-only and zero-touch: it polls the rollout transcripts under
`$CODEX_HOME/sessions/**/rollout-*.jsonl` and uses the flock on
`thread-writer-locks/<id>.lock` for liveness, so a Codex killed outright (lock file left behind
with no holder) does not pin a gray row forever. It never writes Codex's `hooks.json` and never
touches its hook-trust gate.

**Also** — always-on-top and draggable, collapsible to the screen edge, switchable corner /
size / orientation / sort order, persisted preferences, a self-healing daemon (restarts within
seconds of a crash), demo mode for previewing states without real sessions, and zero external
dependencies (Python stdlib + tkinter).

## Install

```bash
uv venv ~/.local/venvs/claude-codex-lights --python 3.13 --seed
~/.local/venvs/claude-codex-lights/bin/pip install git+https://github.com/wwh0411/claude-codex-lights.git
~/.local/venvs/claude-codex-lights/bin/claude-codex-lights-setup
```

Restart Claude Code. The panel shows up bottom-right; send a message and the matching light
lights up.

> **⚠️ The interpreter you install into is the one the hooks are pinned to.**
> `claude-codex-lights-setup` writes the **absolute path of the interpreter that ran it** into
> `~/.claude/settings.json`. Delete or move that environment and the panel stops updating. Re-run
> setup after switching environments.

> **⚠️ macOS**: do not use the system `/usr/bin/python3` (Python 3.9 / Tk 8.5) — it produces a
> window object with no visible pixels. Use uv, Homebrew or python.org Python (Tk ≥ 8.6).
> Installing `pyobjc-framework-Cocoa` lets the panel show across Spaces.

Verify:

```bash
python -m claude_eyes.demo        # panel shows demo lights in every state
python -m claude_eyes.demo clear
```

## How it works

```
Claude Code ──hooks──► hook.py ──► status/<session_id>.json
                                          │
Codex CLI ──rollout+flock──► codex.py ────┤ (polled every 200 ms)
                                          ▼
                                     manager.py ──► panel.py (tkinter)
```

- **Claude Code** goes through hooks: `SessionStart`, `UserPromptSubmit`, `PermissionRequest`,
  `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `SubagentStart`, `SubagentStop`, `Stop`,
  `SessionEnd` — 11 commands registered in `settings.json`. Each event spawns a short-lived Python
  process: read stdin, write one JSON file, exit.
- **Codex** is polled read-only from its own rollout files and lock files.
- The hooks self-heal: every `SessionStart` checks the daemon heartbeat, restarts it if stale, and
  fills in any missing hook entries.
- Panel and daemon are fully decoupled from the CLI processes.

## Configuration

| Environment variable | Default | Purpose |
|---|---|---|
| `CLAUDE_TRAFFIC_LIGHT_DIR` | `~/.claude/traffic-light/` | Status files, logs, panel preferences |
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects/` | Where Claude transcripts live |
| `CLAUDE_SETTINGS` | `~/.claude/settings.json` | Hook registration target |
| `CODEX_HOME` | `~/.codex` | Where Codex session data lives |
| `CLAUDE_EYES_CODEX` | `1` | `0` disables Codex monitoring |
| `CLAUDE_EYES_CODEX_LOCK_PROBE` | `1` | `0` disables flock liveness probing |
| `CLAUDE_EYES_SOUND` | `1` | `0` mutes |
| `CLAUDE_EYES_SOUND_WAITING` | `Submarine` | Sound on turning yellow (macOS system sound name) |
| `CLAUDE_EYES_SOUND_ERROR` | `Basso` | Sound on turning red |

Position, size, sort order and orientation are in the right-click menu and are persisted.

## Footprint

- CPU: event-driven tkinter `after()` polling, no busy-wait, ~0% when idle
- Memory: roughly 10–40 MB for the panel process
- Hook cost: under 50 ms per event, process exits immediately

## Development

```bash
pip install -e .
python -m pytest tests/ -q
python src/claude_eyes/manager.py     # run the panel in the foreground to see tracebacks
```

## Credits

Built on [wzp0514/claude-eyes](https://github.com/wzp0514/claude-eyes) (MIT), extended with Codex
session support, subagent / background-task sub-lights, the context meter, the completion card,
sound cues, and panel collapse / sizing.

## License

MIT — see [LICENSE](LICENSE).
