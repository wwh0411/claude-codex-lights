"""Tests for Codex session discovery (read-only rollout polling)."""
import fcntl
import json
import os
import time

import pytest

from claude_eyes import codex

# Lock files handed out by _home. A held one keeps its file object open for the
# duration of the test, because the flock lives on the open descriptor.
_HELD = []


@pytest.fixture(autouse=True)
def _release_held_locks():
    yield
    while _HELD:
        _HELD.pop().close()

TS = "2026-08-24T14:00:00.000Z"


def _row(kind, payload, timestamp=TS):
    return json.dumps({"timestamp": timestamp, "type": kind, "payload": payload})


def _rollout(prompt="修一下登录超时", running=False, cwd="/Users/x/projects/demo",
             model="gpt-5.6-sol", tokens=(20707, 325), completed_ago=1):
    rows = [
        _row("session_meta", {"cwd": cwd}),
        _row("turn_context", {"cwd": cwd, "model": model}),
        _row("event_msg", {"type": "task_started"}),
        # Codex injects these into the user turn; none was typed by the user.
        _row("response_item", {"type": "message", "role": "user",
                               "content": [{"type": "text",
                                            "text": "<environment_context>\n  <cwd>/x</cwd>"}]}),
        _row("response_item", {"type": "message", "role": "user",
                               "content": [{"type": "text", "text": "Tip: Press Tab to queue"}]}),
        _row("response_item", {"type": "message", "role": "user",
                               "content": [{"type": "text", "text": prompt}]}),
        _row("event_msg", {"type": "token_count",
                           "info": {"last_token_usage": {"input_tokens": tokens[0],
                                                         "output_tokens": tokens[1]},
                                    "total_token_usage": {"input_tokens": 999999}}}),
    ]
    if not running:
        # completed_at is the only clock in a rollout that a migration does not
        # restamp, so it is what dates a finished conversation.
        rows.append(_row("event_msg", {"type": "task_complete",
                                       "completed_at": time.time() - completed_ago}))
    return "\n".join(rows) + "\n"


def _home(tmp_path, monkeypatch, sessions, locked=(), names=None, day="24",
          leaked=(), lock_age=None, lock_dir=True):
    """Build a fake CODEX_HOME. sessions: {session_id: (text, age_seconds)}."""
    if lock_age is None:
        lock_age = codex.CODEX_LOCK_GRACE + 5
    home = tmp_path / "codex"
    home.mkdir(parents=True, exist_ok=True)
    if lock_dir:  # absent on a Codex that does not keep writer locks
        (home / "thread-writer-locks").mkdir(parents=True)
        (home / "thread-writer-locks" / ".coordination.lock").write_text("")
    directory = home / "sessions" / "2026" / "08" / day
    directory.mkdir(parents=True)
    for session_id, (text, age) in sessions.items():
        path = directory / f"rollout-2026-08-{day}T14-00-00-{session_id}.jsonl"
        path.write_text(text)
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    # `locked` models a session Codex still has open: the lock file exists *and*
    # a live process holds its flock. It is aged past CODEX_LOCK_GRACE so the
    # probe really runs instead of being waved through as newly created.
    # `leaked` is the same file with no holder — what a crashed Codex leaves.
    for session_id, hold in [(s, True) for s in locked] + [(s, False) for s in leaked]:
        path = home / "thread-writer-locks" / f"{session_id}.lock"
        handle = path.open("wb")
        if hold:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _HELD.append(handle)
        else:
            handle.close()
        stamp = time.time() - lock_age
        os.utime(path, (stamp, stamp))
    if names:
        (home / "session_index.jsonl").write_text("\n".join(
            json.dumps({"id": sid, "thread_name": name}) for sid, name in names.items()
        ) + "\n", encoding="utf-8")
    monkeypatch.setattr(codex, "CODEX_HOME", home)
    codex._ROLLOUT_CACHE.clear()
    codex._LISTING.update({"at": 0.0, "paths": []})
    return home


SID_A = "01a03416-50a9-7b52-ab1c-a0cfc4fd7cc7"
SID_B = "01a028d5-c6f9-7513-9dfd-6b12bad8fd07"


def test_open_session_is_reported_with_title_prompt_and_usage(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch,
          {SID_A: (_rollout(running=True), 5)},
          locked=[SID_A], names={SID_A: "修登录"})
    rows = codex.discover_sessions()

    assert len(rows) == 1
    row = rows[0]
    assert row["agent_kind"] == "codex"
    assert row["session_id"] == SID_A
    assert row["status"] == "working"
    assert row["conversation_title"] == "修登录"
    assert row["last_prompt"] == "修一下登录超时"
    assert row["cwd"] == "/Users/x/projects/demo"
    assert row["model"] == "gpt-5.6-sol"
    # Per-turn usage, not the session total.
    assert (row["input_tokens"], row["output_tokens"]) == (20707, 325)


def test_completed_turn_is_idle_not_working(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, {SID_A: (_rollout(running=False), 5)}, locked=[SID_A])

    assert codex.discover_sessions()[0]["status"] == "idle"


def test_session_without_a_writer_lock_is_not_shown(tmp_path, monkeypatch):
    """Releasing the lock is Codex's SessionEnd."""
    _home(tmp_path, monkeypatch,
          {SID_A: (_rollout(running=True), 5), SID_B: (_rollout(running=True), 5)},
          locked=[SID_A])

    assert [r["session_id"] for r in codex.discover_sessions()] == [SID_A]


def test_coordination_lock_is_not_mistaken_for_a_session(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, {SID_A: (_rollout(), 5)}, locked=[])

    assert codex.open_session_ids() == set()
    assert codex.discover_sessions() == []


def test_lock_left_behind_by_a_crash_is_not_a_session(tmp_path, monkeypatch):
    """The phantom row: a killed Codex leaves the file, but holds no flock.

    Without the flock probe this session stays on the panel forever — gray, but
    never removed, because removal only happens when the file disappears.
    """
    _home(tmp_path, monkeypatch,
          {SID_A: (_rollout(running=True), 5), SID_B: (_rollout(running=True), 5)},
          locked=[SID_A], leaked=[SID_B])

    assert codex.open_session_ids() == {SID_A}
    assert [r["session_id"] for r in codex.discover_sessions()] == [SID_A]


def test_crashed_session_is_dropped_however_long_ago_it_died(tmp_path, monkeypatch):
    """Age is not what disqualifies it — nothing holding the lock is."""
    _home(tmp_path, monkeypatch,
          {SID_A: (_rollout(running=True), codex.CODEX_SESSION_TIMEOUT + 3600)},
          leaked=[SID_A])

    assert codex.discover_sessions() == []


def test_lock_is_trusted_during_the_instant_before_codex_locks_it(tmp_path, monkeypatch):
    """A session being born must not read as one that died."""
    _home(tmp_path, monkeypatch, {SID_A: (_rollout(running=True), 0)},
          leaked=[SID_A], lock_age=0)

    assert [r["session_id"] for r in codex.discover_sessions()] == [SID_A]


def test_probe_can_be_switched_off(tmp_path, monkeypatch):
    """Escape hatch for a Codex build that stops taking the flock."""
    monkeypatch.setattr(codex, "CODEX_LOCK_PROBE", False)
    _home(tmp_path, monkeypatch, {SID_A: (_rollout(running=True), 5)}, leaked=[SID_A])

    assert [r["session_id"] for r in codex.discover_sessions()] == [SID_A]


def test_platform_without_flock_trusts_the_lock_file(tmp_path, monkeypatch):
    """Windows has no fcntl; the file's existence is all there is to go on."""
    monkeypatch.setattr(codex, "fcntl", None)
    _home(tmp_path, monkeypatch, {SID_A: (_rollout(running=True), 5)}, leaked=[SID_A])

    assert [r["session_id"] for r in codex.discover_sessions()] == [SID_A]


def test_quiet_open_session_goes_gray_rather_than_vanishing(tmp_path, monkeypatch):
    """A session left open for hours stays on the panel, gray."""
    _home(tmp_path, monkeypatch,
          {SID_A: (_rollout(running=True), codex.CODEX_SESSION_TIMEOUT + 60)},
          locked=[SID_A])

    assert codex.discover_sessions()[0]["status"] == "stale"


def test_interrupted_turn_stops_being_reported_as_working(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch,
          {SID_A: (_rollout(running=True), codex.CODEX_TURN_TIMEOUT + 60)},
          locked=[SID_A])

    assert codex.discover_sessions()[0]["status"] == "idle"


def test_migrated_rollout_is_dated_by_its_last_turn_not_its_file(tmp_path, monkeypatch):
    """Without a lock directory, a migration must not resurrect old threads.

    Codex's rollout migration rewrites finished conversations into brand-new
    files — every record restamped — so mtime says "seconds ago" for a thread
    that stopped hours ago. Read that as liveness and every migrated thread
    lights up at once.
    """
    _home(tmp_path, monkeypatch, {
        SID_A: (_rollout(completed_ago=codex.CODEX_SESSION_TIMEOUT + 3600), 2),
        SID_B: (_rollout(prompt="刚问的问题", completed_ago=30), 2),
    }, lock_dir=False)

    assert [r["session_id"] for r in codex.discover_sessions()] == [SID_B]


def test_lockless_session_mid_turn_is_live_even_with_no_completion_yet(tmp_path, monkeypatch):
    """A first turn still running has no completed_at; its writes are the signal."""
    _home(tmp_path, monkeypatch, {SID_A: (_rollout(running=True), 2)}, lock_dir=False)

    assert [r["session_id"] for r in codex.discover_sessions()] == [SID_A]


def test_migrated_thread_still_open_shows_its_real_age(tmp_path, monkeypatch):
    """Holding a lock keeps the row; the migration must not paint it fresh green."""
    _home(tmp_path, monkeypatch,
          {SID_A: (_rollout(completed_ago=codex.CODEX_SESSION_TIMEOUT + 3600), 2)},
          locked=[SID_A])

    row = codex.discover_sessions()[0]
    assert row["status"] == "stale"
    assert row["age_seconds"] > codex.CODEX_SESSION_TIMEOUT


def test_long_turn_stays_working_however_old_the_last_completion_is(tmp_path, monkeypatch):
    """The two clocks must not be confused.

    A session that sat idle for an hour and is now mid-turn has an ancient
    completed_at and a file being appended to right now. Dating it by the
    completion would gray out a session that is visibly working.
    """
    text = _rollout(completed_ago=3600) + _row("event_msg", {"type": "task_started"}) + "\n"
    _home(tmp_path, monkeypatch, {SID_A: (text, 2)}, locked=[SID_A])

    assert codex.discover_sessions()[0]["status"] == "working"


def test_forked_session_uses_its_newest_rollout(tmp_path, monkeypatch):
    """A fork writes a second rollout under the same session id."""
    home = _home(tmp_path, monkeypatch, {SID_A: (_rollout(prompt="旧的那条"), 600)},
                 locked=[SID_A])
    newer = home / "sessions" / "2026" / "08" / "24" / f"rollout-2026-08-24T18-00-00-{SID_A}.jsonl"
    newer.write_text(_rollout(prompt="fork 之后的问题"))
    codex._LISTING.update({"at": 0.0, "paths": []})

    rows = codex.discover_sessions()
    assert len(rows) == 1
    assert rows[0]["last_prompt"] == "fork 之后的问题"


def test_rollout_is_parsed_once_until_it_changes(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, {SID_A: (_rollout(running=True), 5)}, locked=[SID_A])
    path = home / "sessions" / "2026" / "08" / "24" / f"rollout-2026-08-24T14-00-00-{SID_A}.jsonl"
    calls = []
    real = codex._parse_rollout
    monkeypatch.setattr(codex, "_parse_rollout",
                        lambda p, size, prev=None: calls.append(p) or real(p, size, prev))

    for _ in range(5):
        codex.discover_sessions()
    assert len(calls) == 1

    path.write_text(path.read_text() + _row("event_msg", {"type": "task_complete"}) + "\n")
    assert codex.discover_sessions()[0]["status"] == "idle"
    assert len(calls) == 2


def test_directory_listing_is_rate_limited(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, {SID_A: (_rollout(), 5)}, locked=[SID_A])
    globs = []
    original = codex._sessions_dir
    monkeypatch.setattr(codex, "_sessions_dir", lambda: globs.append(1) or original())
    for _ in range(6):
        codex.discover_sessions()

    # One listing for the burst, not one per call.
    assert len(globs) == 1


def test_missing_codex_home_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_HOME", tmp_path / "nope")
    codex._LISTING.update({"at": 0.0, "paths": []})

    assert codex.open_session_ids() is None
    assert codex.discover_sessions() == []


# ------------------------------------------------ records outside the windows


def _padding(size):
    """A tool output big enough to push earlier records out of the tail window.

    Newline-terminated: without it, anything appended afterwards lands on the
    same line and both records are lost to a parse error.
    """
    return _row("response_item", {"type": "custom_tool_call_output",
                                  "output": "x" * size}) + "\n"


def test_cwd_survives_a_session_meta_bigger_than_the_head_budget(tmp_path, monkeypatch):
    """Codex 0.150 embeds 18 KB of base instructions in that one record.

    Read as a byte slice it comes back half a line, which does not parse, and
    the session lands in the "其他" group with no project name.
    """
    # No turn_context: session_meta is then the only thing carrying the cwd,
    # so a truncated read of it has nowhere to fall back to.
    rollout = "\n".join([
        _row("session_meta", {"cwd": "/Users/x/projects/RAg",
                              "base_instructions": {"text": "y" * 20000}}),
        _row("event_msg", {"type": "task_started"}),
    ]) + "\n"
    _home(tmp_path, monkeypatch, {SID_A: (rollout, 5)}, locked=[SID_A])

    assert codex.discover_sessions()[0]["cwd"] == "/Users/x/projects/RAg"


def test_model_is_read_from_the_head_when_the_tail_has_no_turn_context(
    tmp_path, monkeypatch,
):
    """turn_context is written once per turn, not once per record."""
    rollout = _rollout(running=True, model="gpt-5.6-sol") + _padding(codex.CODEX_TAIL_BYTES)
    _home(tmp_path, monkeypatch, {SID_A: (rollout, 5)}, locked=[SID_A])

    assert codex.discover_sessions()[0]["model"] == "gpt-5.6-sol"


def test_prompt_is_found_however_far_the_turn_pushed_it_back(tmp_path, monkeypatch):
    """A turn emitting megabytes of output must not blank the row's question."""
    rollout = _rollout(prompt="继续优化文档", running=True)
    rollout += _padding(codex.CODEX_TAIL_BYTES * 2)
    _home(tmp_path, monkeypatch, {SID_A: (rollout, 5)}, locked=[SID_A])

    assert codex.discover_sessions()[0]["last_prompt"] == "继续优化文档"


def test_the_deep_scan_runs_once_and_then_rides_the_cache(tmp_path, monkeypatch):
    rollout = _rollout(prompt="继续优化文档", running=True)
    rollout += _padding(codex.CODEX_TAIL_BYTES * 2)
    home = _home(tmp_path, monkeypatch, {SID_A: (rollout, 5)}, locked=[SID_A])
    path = home / "sessions" / "2026" / "08" / "24" / f"rollout-2026-08-24T14-00-00-{SID_A}.jsonl"
    scans = []
    real = codex._deep_scan
    monkeypatch.setattr(codex, "_deep_scan",
                        lambda p, size: scans.append(p) or real(p, size))

    codex.discover_sessions()
    path.write_text(path.read_text() + _padding(2048))
    row = codex.discover_sessions()[0]

    assert row["last_prompt"] == "继续优化文档"  # carried forward, not re-derived
    assert len(scans) == 1


def test_a_rollout_too_large_to_scan_is_given_up_on(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "CODEX_PROMPT_SCAN_BYTES", 1024)
    rollout = _rollout(prompt="继续优化文档", running=True)
    rollout += _padding(codex.CODEX_TAIL_BYTES * 2)
    _home(tmp_path, monkeypatch, {SID_A: (rollout, 5)}, locked=[SID_A])

    assert codex.discover_sessions()[0]["last_prompt"] == ""


# ------------------------------------------------------------ context window


def _with_window(window):
    return _row("event_msg", {"type": "token_count",
                              "info": {"last_token_usage": {"input_tokens": 142065,
                                                            "output_tokens": 207},
                                       "model_context_window": window}}) + "\n"


def test_codex_reports_its_context_window_exactly(tmp_path, monkeypatch):
    """Codex states the window outright — nothing is inferred, unlike Claude."""
    _home(tmp_path, monkeypatch,
          {SID_A: (_rollout(running=True) + _with_window(855000), 5)}, locked=[SID_A])
    row = codex.discover_sessions()[0]

    assert row["context_tokens"] == 142065
    assert row["context_limit"] == 855000


def test_no_stated_window_means_no_meter_rather_than_a_guessed_one(
    tmp_path, monkeypatch,
):
    _home(tmp_path, monkeypatch, {SID_A: (_rollout(running=True), 5)}, locked=[SID_A])
    row = codex.discover_sessions()[0]

    assert row["context_tokens"] == 0
    assert row["context_limit"] == 0


# --------------------------------------- records the tail window cannot reach


def _mid_turn(output_size):
    """A turn still running, with output_size bytes written since it started."""
    return "\n".join([
        _row("session_meta", {"cwd": "/Users/x/projects/RAg"}),
        _row("turn_context", {"cwd": "/Users/x/projects/RAg", "model": "gpt-5.6-sol"}),
        _row("event_msg", {"type": "task_started"}),
        _row("response_item", {"type": "message", "role": "user",
                               "content": [{"type": "text", "text": "跑个长任务"}]}),
        _row("event_msg", {"type": "token_count",
                           "info": {"last_token_usage": {"input_tokens": 142065,
                                                         "output_tokens": 207},
                                    "model_context_window": 855000}}),
    ]) + "\n" + _padding(output_size)


def test_a_long_turn_still_reads_as_running(tmp_path, monkeypatch):
    """Both markers fall outside the window, and -1 > -1 is False — idle.

    The row went green mid-turn, which is the one thing the light exists to
    get right.
    """
    _home(tmp_path, monkeypatch,
          {SID_A: (_mid_turn(codex.CODEX_TAIL_BYTES * 2), 5)}, locked=[SID_A])

    assert codex.discover_sessions()[0]["status"] == "working"


def test_a_long_turn_keeps_its_context_meter(tmp_path, monkeypatch):
    """token_count scrolls out of the window with everything else."""
    _home(tmp_path, monkeypatch,
          {SID_A: (_mid_turn(codex.CODEX_TAIL_BYTES * 2), 5)}, locked=[SID_A])
    row = codex.discover_sessions()[0]

    assert (row["context_tokens"], row["context_limit"]) == (142065, 855000)
    assert (row["input_tokens"], row["output_tokens"]) == (142065, 207)


def test_state_is_carried_forward_rather_than_rescanned(tmp_path, monkeypatch):
    """The common case: output grows past the window while the turn runs."""
    home = _home(tmp_path, monkeypatch, {SID_A: (_mid_turn(1024), 5)}, locked=[SID_A])
    path = home / "sessions" / "2026" / "08" / "24" / f"rollout-2026-08-24T14-00-00-{SID_A}.jsonl"
    scans = []
    real = codex._deep_scan
    monkeypatch.setattr(codex, "_deep_scan",
                        lambda p, size: scans.append(p) or real(p, size))

    assert codex.discover_sessions()[0]["status"] == "working"
    path.write_text(path.read_text() + _padding(codex.CODEX_TAIL_BYTES * 2))
    row = codex.discover_sessions()[0]

    assert row["status"] == "working"
    assert row["context_tokens"] == 142065
    assert scans == []  # nothing had to be re-read: the window covered it first


def test_a_finished_turn_inside_the_window_still_wins(tmp_path, monkeypatch):
    """Carrying forward must not outlive the completion that ends the turn."""
    home = _home(tmp_path, monkeypatch,
                 {SID_A: (_mid_turn(codex.CODEX_TAIL_BYTES * 2), 5)}, locked=[SID_A])
    path = home / "sessions" / "2026" / "08" / "24" / f"rollout-2026-08-24T14-00-00-{SID_A}.jsonl"

    assert codex.discover_sessions()[0]["status"] == "working"
    path.write_text(path.read_text() + _row("event_msg", {"type": "task_complete",
                                                          "completed_at": time.time()}) + "\n")

    assert codex.discover_sessions()[0]["status"] == "idle"


def test_identity_is_found_however_late_the_turn_context_appears(tmp_path, monkeypatch):
    """Codex may insert records ahead of it; the scan must not be fitted to a
    record number the way the old byte budget was fitted to a record size."""
    rows = [_row("session_meta", {"cwd": "/Users/x/projects/RAg"})]
    rows += [_row("event_msg", {"type": "item_completed"}) for _ in range(60)]
    rows += [_row("turn_context", {"cwd": "/Users/x/projects/RAg",
                                   "model": "gpt-5.6-sol"}),
             _row("event_msg", {"type": "task_started"})]
    _home(tmp_path, monkeypatch, {SID_A: ("\n".join(rows) + "\n", 5)}, locked=[SID_A])
    row = codex.discover_sessions()[0]

    assert (row["cwd"], row["model"]) == ("/Users/x/projects/RAg", "gpt-5.6-sol")


def test_the_head_scan_stops_as_soon_as_it_has_both_answers(tmp_path, monkeypatch):
    """The common shape: identity settled by record 2, whatever follows it."""
    rows = [_row("session_meta", {"cwd": "/Users/x/projects/RAg",
                                  "model": "gpt-5.6-sol"})]
    rows += [_row("response_item", {"type": "custom_tool_call_output",
                                    "output": "x" * 200000}) for _ in range(3)]
    _home(tmp_path, monkeypatch, {SID_A: ("\n".join(rows) + "\n", 5)}, locked=[SID_A])

    assert codex._head_identity(
        tmp_path / "codex" / "sessions" / "2026" / "08" / "24"
        / f"rollout-2026-08-24T14-00-00-{SID_A}.jsonl"
    ) == ("/Users/x/projects/RAg", "gpt-5.6-sol", {})


# ======================================================================
# turn duration for the completion card
# ======================================================================
def test_a_normal_turn_reports_how_long_it_took():
    assert codex._turn_elapsed({"started_at": 1000.0, "completed_at": 1012.5}) == 12.5


def test_a_turn_missing_either_end_reports_unknown():
    assert codex._turn_elapsed({"started_at": 0.0, "completed_at": 1012.5}) == 0.0
    assert codex._turn_elapsed({"started_at": 1000.0, "completed_at": 0.0}) == 0.0
    assert codex._turn_elapsed({}) == 0.0


def test_an_implausible_span_reports_unknown_rather_than_a_wrong_number():
    """Codex's rollout migration rewrites record timestamps; completed_at survives.

    That can leave the two ends in either order, or a day apart, so the card is
    given nothing instead of a fabricated duration.
    """
    assert codex._turn_elapsed({"started_at": 2000.0, "completed_at": 1000.0}) == 0.0
    assert codex._turn_elapsed({"started_at": 1.0, "completed_at": 90000.0}) == 0.0


def test_a_record_timestamp_becomes_epoch_seconds():
    assert codex._record_epoch({"timestamp": "2026-08-29T06:00:00.000Z"}) > 0
    assert codex._record_epoch({"timestamp": "not a date"}) == 0.0
    assert codex._record_epoch({}) == 0.0


# ======================================================================
# subagent threads (Codex spawns them as their own thread + rollout + lock)
# ======================================================================
SID_C = "01a07efc-575c-7653-b9ca-2477c2ef3ec8"


def _subagent_rollout(parent, agent_path="/root/evolution_design",
                      nickname="Dirac", running=False, **kw):
    """A rollout of the shape Codex writes for a spawned subagent."""
    text = _rollout(running=running, **kw)
    head, rest = text.split("\n", 1)
    meta = json.loads(head)
    meta["payload"].update({
        "thread_source": "subagent",
        "source": {"subagent": {"thread_spawn": {
            "parent_thread_id": parent, "depth": 1,
            "agent_path": agent_path, "agent_nickname": nickname,
        }}},
    })
    return json.dumps(meta) + "\n" + rest


def test_a_subagent_hangs_under_its_parent_instead_of_beside_it(tmp_path, monkeypatch):
    """The bug: a subagent thread looks exactly like a session the user started."""
    _home(tmp_path, monkeypatch, {
        SID_A: (_rollout(prompt="父任务", running=True), 1),
        SID_C: (_subagent_rollout(SID_A, running=True), 1),
    }, locked=[SID_A, SID_C])

    rows = codex.discover_sessions()
    assert [r["session_id"] for r in rows] == [SID_A], "subagent leaked to top level"
    lights = rows[0]["subagents"]
    assert [l["name"] for l in lights] == ["evolution_design"]
    assert lights[0]["kind"] == "agent"
    assert lights[0]["status"] == "working"
    assert lights[0]["agent_id"] == f"codex:{SID_C}"


def test_a_subagent_light_follows_its_own_thread_state(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, {
        SID_A: (_rollout(running=True), 1),
        SID_C: (_subagent_rollout(SID_A, running=False, completed_ago=2), 1),
    }, locked=[SID_A, SID_C])

    assert codex.discover_sessions()[0]["subagents"][0]["status"] == "idle"


def test_a_subagent_quiet_past_stale_timeout_disappears(tmp_path, monkeypatch):
    """The bug: a finished subagent hid at 10 min, then reappeared grey at 30.

    Hiding only idle threads left it visible again the moment it turned stale,
    so it sat under its parent as a grey light for hours. Anything quiet past
    STALE_TIMEOUT must be gone regardless of status, exactly as Claude Code's
    subagents are.
    """
    old = codex.STALE_TIMEOUT + 3600  # long stale — over a day in the report
    _home(tmp_path, monkeypatch, {
        SID_A: (_rollout(running=True), 1),
        SID_C: (_subagent_rollout(SID_A, completed_ago=old), 1),
    }, locked=[SID_A, SID_C])

    rows = codex.discover_sessions()
    assert [r["session_id"] for r in rows] == [SID_A]
    assert rows[0]["subagents"] == []


def test_a_subagent_that_finished_long_ago_drops_off(tmp_path, monkeypatch):
    """Same 10 minutes a finished Claude Code subagent gets."""
    done = codex.SUBAGENT_DONE_TIMEOUT + 60
    _home(tmp_path, monkeypatch, {
        SID_A: (_rollout(running=True), 1),
        SID_C: (_subagent_rollout(SID_A, completed_ago=done), 1),
    }, locked=[SID_A, SID_C])

    rows = codex.discover_sessions()
    assert [r["session_id"] for r in rows] == [SID_A]
    assert rows[0]["subagents"] == []


def test_the_light_is_named_after_the_task_not_the_nickname(tmp_path, monkeypatch):
    """"Dirac" says nothing about the work; the task name the model chose does."""
    _home(tmp_path, monkeypatch, {
        SID_A: (_rollout(running=True), 1),
        SID_C: (_subagent_rollout(SID_A, agent_path="/root/probe_dataset",
                                  nickname="Dirac", running=True), 1),
    }, locked=[SID_A, SID_C])

    light = codex.discover_sessions()[0]["subagents"][0]
    assert light["name"] == "probe_dataset"
    assert light["agent_type"] == "Dirac"


def test_a_subagent_whose_parent_is_gone_is_shown_rather_than_dropped(
    tmp_path, monkeypatch
):
    """Losing a running thread entirely would be worse than an odd-looking row."""
    _home(tmp_path, monkeypatch, {
        SID_C: (_subagent_rollout(SID_A, running=True), 1),
    }, locked=[SID_C])

    rows = codex.discover_sessions()
    assert [r["session_id"] for r in rows] == [SID_C]
    assert rows[0]["conversation_title"] == "evolution_design"


def test_a_users_own_thread_is_never_mistaken_for_a_subagent(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, {SID_A: (_rollout(running=True), 1)}, locked=[SID_A])
    row = codex.discover_sessions()[0]
    assert row["subagents"] == []
    assert row["session_id"] == SID_A


def test_a_malformed_spawn_record_leaves_the_thread_at_top_level(tmp_path, monkeypatch):
    """Never swallow a session because its metadata was an unexpected shape."""
    text = _rollout(running=True)
    head, rest = text.split("\n", 1)
    meta = json.loads(head)
    meta["payload"].update({"thread_source": "subagent", "source": "not-a-dict"})
    _home(tmp_path, monkeypatch, {SID_C: (json.dumps(meta) + "\n" + rest, 1)},
          locked=[SID_C])

    assert [r["session_id"] for r in codex.discover_sessions()] == [SID_C]


def _sids(n):
    return [f"01a07efc-575c-7653-b9ca-{i:012d}" for i in range(n)]


def test_running_subagents_win_the_cap_and_draw_in_spawn_order(tmp_path, monkeypatch):
    """Which ones survive truncation is by interest; the drawing order is stable."""
    ids = _sids(codex.MAX_SUBAGENTS + 2)
    sessions = {SID_A: (_rollout(running=True), 1)}
    for i, sid in enumerate(ids):
        # The last two are idle, so they are the ones the cap should drop.
        running = i < codex.MAX_SUBAGENTS
        sessions[sid] = (_subagent_rollout(
            SID_A, agent_path=f"/root/agent_{i:02d}", running=running,
            completed_ago=2), 1)
    _home(tmp_path, monkeypatch, sessions, locked=[SID_A, *ids])

    lights = codex.discover_sessions()[0]["subagents"]
    assert len(lights) == codex.MAX_SUBAGENTS
    assert all(l["status"] == "working" for l in lights)
    assert lights[-1]["hidden_count"] == 2
    # Drawn in spawn order, not in the priority order used to pick them.
    assert [l["name"] for l in lights] == sorted(l["name"] for l in lights)


def _review_rollout(parent, kind="review", running=True, **kw):
    """Codex's built-in /review spawns a subagent whose source is a bare string."""
    text = _rollout(running=running, **kw)
    head, rest = text.split("\n", 1)
    meta = json.loads(head)
    meta["payload"].update({
        "thread_source": "subagent",
        "parent_thread_id": parent,
        "source": {"subagent": kind},
    })
    return json.dumps(meta) + "\n" + rest


def test_a_review_subagent_nests_even_though_its_source_is_a_bare_string(
    tmp_path, monkeypatch
):
    """The ghost row: source is {"subagent": "review"}, not a thread_spawn dict.

    Reading the parent out of `source` found nothing, so the thread posed as a
    session of its own — and having no entry in Codex's thread index, it drew
    with no name at all.
    """
    _home(tmp_path, monkeypatch, {
        SID_A: (_rollout(prompt="父任务", running=True), 1),
        SID_C: (_review_rollout(SID_A), 1),
    }, locked=[SID_A, SID_C])

    rows = codex.discover_sessions()
    assert [r["session_id"] for r in rows] == [SID_A], "review thread leaked to top level"
    assert [l["name"] for l in rows[0]["subagents"]] == ["review"]


def test_the_parent_comes_from_the_top_level_field_both_shapes_carry():
    spawn = codex._thread_spawn({
        "thread_source": "subagent", "parent_thread_id": "P",
        "source": {"subagent": "review"},
    })
    assert spawn["parent_thread_id"] == "P"
    assert codex._agent_label(spawn) == "review"

    nested = codex._thread_spawn({
        "thread_source": "subagent", "parent_thread_id": "P",
        "source": {"subagent": {"thread_spawn": {
            "parent_thread_id": "P", "agent_path": "/root/probe",
            "agent_nickname": "Dirac"}}},
    })
    assert nested["parent_thread_id"] == "P"
    assert codex._agent_label(nested) == "probe"
    assert nested["agent_nickname"] == "Dirac"


def test_a_users_own_thread_never_looks_spawned():
    assert codex._thread_spawn({"thread_source": "user", "source": {}}) == {}
    assert codex._thread_spawn({}) == {}


def test_a_subagent_naming_no_parent_is_labelled_rather_than_left_nameless(
    tmp_path, monkeypatch
):
    """It has no thread-index entry, so top level would render it with no name."""
    text = _rollout(running=True)
    head, rest = text.split("\n", 1)
    meta = json.loads(head)
    meta["payload"].update({"thread_source": "subagent", "source": {"subagent": "review"}})
    _home(tmp_path, monkeypatch, {SID_C: (json.dumps(meta) + "\n" + rest, 1)},
          locked=[SID_C])

    rows = codex.discover_sessions()
    assert [r["session_id"] for r in rows] == [SID_C]
    assert rows[0]["conversation_title"] == "review"


def test_an_unknown_source_shape_still_nests_on_the_parent_field(tmp_path, monkeypatch):
    """Codex has changed this shape twice; the top-level parent is the constant."""
    text = _rollout(running=True)
    head, rest = text.split("\n", 1)
    meta = json.loads(head)
    meta["payload"].update({
        "thread_source": "subagent", "parent_thread_id": SID_A,
        "source": {"subagent": {"something_new": {"x": 1}}},
    })
    _home(tmp_path, monkeypatch, {
        SID_A: (_rollout(running=True), 1),
        SID_C: (json.dumps(meta) + "\n" + rest, 1),
    }, locked=[SID_A, SID_C])

    rows = codex.discover_sessions()
    assert [r["session_id"] for r in rows] == [SID_A]
    assert rows[0]["subagents"][0]["name"] == "subagent"
