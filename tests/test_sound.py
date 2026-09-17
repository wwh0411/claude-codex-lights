"""Tests for the yellow/red audio cues."""
import pytest

from claude_eyes import sound as sound_module


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def cues(monkeypatch):
    """A cue tracker past its warm-up, recording plays instead of making noise."""
    played: list[str] = []
    monkeypatch.setattr(
        sound_module, "play", lambda status: played.append(status) or True
    )
    clock = FakeClock()
    instance = sound_module.SoundCues(enabled=True, clock=clock)
    clock.advance(sound_module.SOUND_WARMUP + 1)
    return instance, played, clock


def test_only_yellow_and_red_make_a_sound(cues):
    instance, played, _ = cues
    for status in ("working", "idle", "stale", "waiting", "error"):
        instance.observe("s1", status)
    assert played == ["waiting", "error"]


def test_a_light_that_stays_yellow_sounds_once(cues):
    """The manager re-reads every status file every 200 ms."""
    instance, played, clock = cues
    for _ in range(30):
        instance.observe("s1", "waiting")
        clock.advance(0.2)
    assert played == ["waiting"]


def test_returning_to_yellow_after_working_sounds_again(cues):
    instance, played, clock = cues
    instance.observe("s1", "waiting")
    instance.observe("s1", "working")
    clock.advance(sound_module.SOUND_COOLDOWN + 1)
    instance.observe("s1", "waiting")
    assert played == ["waiting", "waiting"]


def test_a_flapping_light_is_rate_limited(cues):
    """working↔waiting churn inside the cooldown must not machine-gun."""
    instance, played, clock = cues
    for _ in range(10):
        instance.observe("s1", "working")
        instance.observe("s1", "waiting")
        clock.advance(0.2)
    assert played == ["waiting"]


def test_each_light_alerts_independently(cues):
    instance, played, _ = cues
    instance.observe("s1", "waiting")
    instance.observe("s2", "waiting")
    instance.observe("s1/task:abc", "error")
    assert played == ["waiting", "waiting", "error"]


def test_sessions_already_alerting_at_startup_stay_silent(monkeypatch):
    """Three waiting sessions on the panel at launch are state, not events."""
    played: list[str] = []
    monkeypatch.setattr(
        sound_module, "play", lambda status: played.append(status) or True
    )
    clock = FakeClock()
    instance = sound_module.SoundCues(enabled=True, clock=clock)
    instance.observe("s1", "waiting")
    instance.observe("s2", "error")
    assert played == []
    # ...but a transition after the warm-up window is a real event.
    clock.advance(sound_module.SOUND_WARMUP + 1)
    instance.observe("s3", "waiting")
    assert played == ["waiting"]


def test_muting_silences_cues_without_losing_track_of_state(cues):
    instance, played, clock = cues
    instance.enabled = False
    instance.observe("s1", "waiting")
    assert played == []
    # Re-enabling must not replay the transition that was muted.
    instance.enabled = True
    instance.observe("s1", "waiting")
    assert played == []
    clock.advance(sound_module.SOUND_COOLDOWN + 1)
    instance.observe("s1", "error")
    assert played == ["error"]


def test_a_failed_player_is_not_recorded_as_played(cues):
    """A machine with no audio must retry rather than silently give up."""
    instance, played, clock = cues
    instance.observe("s1", "working")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sound_module, "play", lambda status: False)
        instance.observe("s1", "waiting")
    instance.observe("s1", "working")
    clock.advance(0.2)  # inside the cooldown, which the failure never started
    instance.observe("s1", "waiting")
    assert played == ["waiting"]


def test_closing_a_session_forgets_it_and_its_subagents(cues):
    instance, played, clock = cues
    instance.observe("s1", "waiting")
    instance.observe("s1/task:abc", "error")
    instance.observe("s10", "waiting")  # shares no boundary with "s1"
    instance.forget("s1")
    assert set(instance._last_status) == {"s10"}


def test_forget_leaves_a_similarly_named_session_alone(cues):
    instance, _, _ = cues
    instance.observe("s1", "waiting")
    instance.observe("s1-b", "waiting")
    instance.forget("s1")
    assert set(instance._last_status) == {"s1-b"}
