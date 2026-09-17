"""Shared test guards."""
import pytest

from claude_eyes import codex, hook, manager, panel, sound


@pytest.fixture(autouse=True)
def isolate_codex(monkeypatch, tmp_path_factory):
    """Keep every test out of the developer's real ~/.codex.

    manager._scan() merges live Codex sessions, so without this a manager test
    would see whatever Codex sessions happen to be open on the machine.
    """
    monkeypatch.setattr(manager, "CODEX_ENABLED", False)
    monkeypatch.setattr(codex, "CODEX_HOME", tmp_path_factory.mktemp("no-codex"))
    codex._ROLLOUT_CACHE.clear()
    codex._LISTING.update({"at": 0.0, "paths": []})


@pytest.fixture(autouse=True)
def silence_audio(monkeypatch):
    """No test run should ever make the developer's machine chirp.

    Panel builds a real SoundCues, so the cue tracker's logic still runs and
    stays exercised — only the player is stubbed out.
    """
    monkeypatch.setattr(sound, "play", lambda status: True)


@pytest.fixture(autouse=True)
def isolate_panel_config(monkeypatch, tmp_path):
    """Keep the panel's persisted preferences out of the developer's real home.

    Any test that flips a menu toggle calls _save_config(), which would
    otherwise rewrite ~/.claude/traffic-light/panel_config.json.
    """
    monkeypatch.setattr(panel, "PANEL_CONFIG", tmp_path / "panel_config.json")


@pytest.fixture(autouse=True)
def isolate_claude_settings(monkeypatch, tmp_path):
    """Keep the developer's own ~/.claude/settings.json out of the window check.

    hook._context_limit() reads it for the `[1m]` model suffix, so without this
    every context test silently depends on which model the machine happens to
    be configured with.
    """
    monkeypatch.setattr(hook, "CLAUDE_SETTINGS", tmp_path / "absent-settings.json")
    hook._SETTINGS_CACHE.update({"key": None, "models": frozenset()})


@pytest.fixture(autouse=True)
def isolate_parse_cache(monkeypatch, tmp_path):
    """Give every test its own transcript parse state.

    hook._conversation_metadata() resumes from a cached byte offset, so a shared
    directory would let one test's session id pick up another's state — and
    would write into the developer's real ~/.claude/traffic-light/parse.
    """
    monkeypatch.setattr(hook, "PARSE_DIR", tmp_path / "parse")


@pytest.fixture(autouse=True)
def neutral_owner_cmd(monkeypatch):
    """Keep hook.handle() from consulting the real parent process.

    handle() skips writing a status file when its owner command line looks like
    a `/branch` agent runner (`--agent`). In tests the owner is whatever shell
    launched pytest — and this repo's own Bash tool wraps every command in
    `zsh -c "<the whole command>"`, so a test command that merely mentions
    `--agent` (a grep, an edit, another test's text) would make handle() skip
    and unrelated tests fail. Pin a plain interactive command line; the
    side-session tests override this with their own monkeypatch.
    """
    monkeypatch.setattr(
        hook, "_owner_command",
        lambda pid: "/Users/x/.local/bin/claude --dangerously-skip-permissions",
    )
