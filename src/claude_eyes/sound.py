"""Audio cues for the states worth interrupting the user over.

Only two transitions make a sound: a light turning yellow (Claude is blocked on
a confirmation) or red (something failed). Those are the states where the panel
alone is useless if the user is looking at another window.

Sounds are fired transition-only, never per poll: the manager re-reads every
status file every 200 ms, so playing on "is yellow" instead of "just turned
yellow" would be a siren. Playback is fire-and-forget in a child process — the
Tk mainloop that calls this must never block on audio.
"""
from __future__ import annotations

import subprocess
import sys
import time
from collections import deque
from pathlib import Path

from claude_eyes.config import (
    SOUND_COOLDOWN,
    SOUND_ENABLED,
    SOUND_FILES,
    SOUND_WARMUP,
)

# Child players are never waited on, so they are reaped opportunistically to
# keep a long-lived manager from collecting zombies.
_PLAYERS: deque[subprocess.Popen] = deque()

# Linux has no single system player; the first one present wins.
_LINUX_PLAYERS = ("paplay", "pw-play", "aplay", "canberra-gtk-play")


def _reap() -> None:
    for _ in range(len(_PLAYERS)):
        proc = _PLAYERS.popleft()
        if proc.poll() is None:
            _PLAYERS.append(proc)


def _spawn(cmd: list[str]) -> bool:
    try:
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            # Detach so a manager shutdown does not kill a half-played cue and
            # so the player never inherits the Tk process group's signals.
            kwargs["start_new_session"] = True
        _PLAYERS.append(subprocess.Popen(cmd, **kwargs))
        return True
    except Exception:
        return False


def play(status: str) -> bool:
    """Play the cue for `status`. Returns whether a player was started."""
    _reap()
    name = SOUND_FILES.get(status)
    if not name:
        return False

    if sys.platform == "darwin":
        path = Path("/System/Library/Sounds") / f"{name}.aiff"
        if not path.exists():
            return False
        return _spawn(["afplay", str(path)])

    if sys.platform == "win32":
        # MessageBeep returns immediately and needs no file on disk.
        try:
            import winsound

            winsound.MessageBeep(
                winsound.MB_ICONHAND if status == "error"
                else winsound.MB_ICONEXCLAMATION
            )
            return True
        except Exception:
            return False

    for candidate in _linux_sound_files(status):
        for player in _LINUX_PLAYERS:
            if _spawn([player, str(candidate)]):
                return True
    return False


def _linux_sound_files(status: str) -> list[Path]:
    stem = "dialog-error" if status == "error" else "dialog-warning"
    base = Path("/usr/share/sounds/freedesktop/stereo")
    return [base / f"{stem}.oga", base / f"{stem}.wav"]


class SoundCues:
    """Turns a stream of per-light statuses into transition-triggered sounds.

    One instance tracks every light the panel knows about, keyed by an opaque
    string (session id, or session id + subagent id for the second-level
    lights), so a session and its subagents alert independently.
    """

    def __init__(self, enabled: bool = SOUND_ENABLED, clock=time.monotonic) -> None:
        self.enabled = enabled
        self._clock = clock
        self._last_status: dict[str, str] = {}
        # Keyed by (light, status): the cooldown exists to stop one alert
        # repeating, so an escalation from yellow to red is never swallowed by
        # the yellow cue that preceded it.
        self._last_played: dict[tuple[str, str], float] = {}
        # Sessions already yellow or red when the manager starts are existing
        # state, not events: chiming once per row on launch is pure noise.
        self._started_at = clock()

    def observe(self, key: str, status: str) -> bool:
        """Record `key`'s status, sounding a cue if it just became alertable."""
        previous = self._last_status.get(key)
        self._last_status[key] = status
        if previous == status or status not in SOUND_FILES:
            return False
        if not self.enabled:
            return False
        now = self._clock()
        if now - self._started_at < SOUND_WARMUP:
            return False
        # A light that flaps between working and waiting must not machine-gun.
        stamp = (key, status)
        if now - self._last_played.get(stamp, now - SOUND_COOLDOWN - 1) < SOUND_COOLDOWN:
            return False
        if not play(status):
            return False
        self._last_played[stamp] = now
        return True

    def forget(self, prefix: str) -> None:
        """Drop a closed session's light and any subagent lights beneath it."""
        def owned(key: str) -> bool:
            return key == prefix or key.startswith(f"{prefix}/")

        for key in [k for k in self._last_status if owned(k)]:
            del self._last_status[key]
        for stamp in [s for s in self._last_played if owned(s[0])]:
            del self._last_played[stamp]
