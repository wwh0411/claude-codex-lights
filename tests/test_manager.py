import json
import os
import time

import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_eyes import manager


def _now():
    return datetime.now(timezone.utc)


class FakePanel:
    def __init__(self):
        self.upserts = []
        self.removes = []

    def upsert(self, sid, data):
        self.upserts.append((sid, data))

    def remove(self, sid):
        self.removes.append(sid)


def test_stale_session_turns_gray_instead_of_being_removed(monkeypatch, tmp_path):
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=manager.STALE_TIMEOUT + 1)
    status_file = status_dir / "sid-stale.json"
    status_file.write_text(json.dumps({
        "status": "idle",
        "session_id": "sid-stale",
        "timestamp": stale_time.isoformat(),
        "prompt_time": stale_time.isoformat(),
    }))
    monkeypatch.setattr(manager, "STATUS_DIR", status_dir)

    instance = manager.Manager.__new__(manager.Manager)
    instance._panel = FakePanel()
    instance._known = set()
    instance._scan()

    assert status_file.exists()
    assert instance._panel.removes == []
    assert instance._panel.upserts[0][0] == "sid-stale"
    assert instance._panel.upserts[0][1]["status"] == "stale"


# ----------------------------------------------------------------------
# subagent discovery (second-level light group)
# ----------------------------------------------------------------------
def _assistant(stop_reason, ts="2026-08-24T09:00:00.000Z"):
    return json.dumps({
        "type": "assistant",
        "timestamp": ts,
        "message": {"stop_reason": stop_reason, "model": "claude-opus-5-0"},
    })


def _user(text, ts="2026-08-24T08:59:00.000Z"):
    return json.dumps({
        "type": "user",
        "timestamp": ts,
        "message": {"content": [{"type": "text", "text": text}]},
    })


def _make_session(tmp_path, monkeypatch, session_id, agents):
    """Build a fake ~/.claude/projects tree. agents: (id, meta, lines, age_s)."""
    projects = tmp_path / "projects"
    project = projects / "-Users-x-proj"
    subagents = project / session_id / "subagents"
    subagents.mkdir(parents=True)
    (project / f"{session_id}.jsonl").write_text("{}\n")
    for agent_id, meta, lines, age in agents:
        transcript = subagents / f"agent-{agent_id}.jsonl"
        transcript.write_text("\n".join(lines) + "\n")
        if meta is not None:
            (subagents / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta))
        stamp = time.time() - age
        os.utime(transcript, (stamp, stamp))
    monkeypatch.setattr(manager, "CLAUDE_PROJECTS_DIR", projects)
    monkeypatch.setattr(manager, "SUBAGENT_DIR", tmp_path / "hook-states")
    manager._SESSION_TRANSCRIPT_CACHE.clear()
    manager._SUBAGENT_CACHE.clear()
    manager._BACKGROUND_STATE.clear()
    manager._SESSION_CWD_CACHE.clear()
    manager._FORK_PARENT_CACHE.clear()
    return subagents


def test_subagent_label_and_status_come_from_meta_and_transcript_tail(monkeypatch, tmp_path):
    _make_session(tmp_path, monkeypatch, "sid-a", [
        ("aaa111", {"agentType": "general-purpose", "description": "Probe dataset samples"},
         [_user("go"), _assistant("end_turn")], 10),
        ("bbb222", {"agentType": "Explore", "description": "Sweep call sites"},
         [_user("go"), _assistant("tool_use")], 10),
    ])
    found = {s["agent_id"]: s for s in manager._discover_subagents("sid-a", _now())}

    assert found["aaa111"]["name"] == "Probe dataset samples"
    assert found["aaa111"]["status"] == "idle"
    assert found["bbb222"]["name"] == "Sweep call sites"
    assert found["bbb222"]["agent_type"] == "Explore"
    assert found["bbb222"]["status"] == "working"


def test_unfinished_but_quiet_subagent_is_not_reported_as_working(monkeypatch, tmp_path):
    _make_session(tmp_path, monkeypatch, "sid-b", [
        ("ccc333", {"description": "Aborted run"},
         [_user("go"), _assistant("tool_use")], manager.SUBAGENT_HANG_TIMEOUT + 60),
    ])
    subs = manager._discover_subagents("sid-b", _now())

    assert [s["status"] for s in subs] == ["stale"]


def test_finished_subagent_disappears_after_done_timeout(monkeypatch, tmp_path):
    _make_session(tmp_path, monkeypatch, "sid-c", [
        ("ddd444", {"description": "Old finished agent"},
         [_user("go"), _assistant("end_turn")], manager.SUBAGENT_DONE_TIMEOUT + 60),
        ("eee555", {"description": "Recent finished agent"},
         [_user("go"), _assistant("end_turn")], 30),
    ])
    subs = manager._discover_subagents("sid-c", _now())

    assert [s["agent_id"] for s in subs] == ["eee555"]


def test_label_falls_back_to_first_prompt_without_meta(monkeypatch, tmp_path):
    _make_session(tmp_path, monkeypatch, "sid-d", [
        ("fff666", None,
         [_user("<system-reminder>ignore me</system-reminder>"),
          _user("你的任务：核对 recipes 与探测结果。然后写报告"),
          _assistant("end_turn")], 10),
    ])
    subs = manager._discover_subagents("sid-d", _now())

    assert subs[0]["name"] == "核对 recipes 与探测结果"


def test_transcript_is_parsed_once_until_it_changes(monkeypatch, tmp_path):
    subagents = _make_session(tmp_path, monkeypatch, "sid-e", [
        ("ggg777", {"description": "Cached agent"},
         [_user("go"), _assistant("tool_use")], 10),
    ])
    calls = []
    real_parse = manager._parse_subagent_transcript
    monkeypatch.setattr(
        manager, "_parse_subagent_transcript",
        lambda path, size: calls.append(path) or real_parse(path, size),
    )

    for _ in range(5):
        manager._discover_subagents("sid-e", _now())
    assert len(calls) == 1

    transcript = subagents / "agent-ggg777.jsonl"
    transcript.write_text(transcript.read_text() + _assistant("end_turn") + "\n")
    assert manager._discover_subagents("sid-e", _now())[0]["status"] == "idle"
    assert len(calls) == 2


def test_subagent_list_is_capped_and_reports_the_remainder(monkeypatch, tmp_path):
    _make_session(tmp_path, monkeypatch, "sid-f", [
        (f"agent{index:03d}", {"description": f"Task {index}"},
         [_user("go"), _assistant("tool_use")], 10)
        for index in range(manager.MAX_SUBAGENTS + 3)
    ])
    subs = manager._second_level_lights("sid-f", _now())

    assert len(subs) == manager.MAX_SUBAGENTS
    assert subs[-1]["hidden_count"] == 3


def test_hook_state_overrides_transcript_status_for_waiting(monkeypatch, tmp_path):
    _make_session(tmp_path, monkeypatch, "sid-g", [
        ("hhh888", {"description": "Needs approval"},
         [_user("go"), _assistant("tool_use")], 10),
    ])
    hook_states = tmp_path / "hook-states"
    hook_states.mkdir()
    (hook_states / "sid-g--hhh888.json").write_text(json.dumps({
        "agent_id": "hhh888",
        "status": "waiting",
        "timestamp": _now().isoformat(),
    }))
    subs = manager._discover_subagents("sid-g", _now())

    assert [s["status"] for s in subs] == ["waiting"]


def test_scan_attaches_subagents_to_the_session_payload(monkeypatch, tmp_path):
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    now_iso = _now().isoformat()
    (status_dir / "sid-h.json").write_text(json.dumps({
        "status": "working",
        "session_id": "sid-h",
        "cwd": str(tmp_path),
        "timestamp": now_iso,
        "prompt_time": now_iso,
    }))
    monkeypatch.setattr(manager, "STATUS_DIR", status_dir)
    _make_session(tmp_path, monkeypatch, "sid-h", [
        ("iii999", {"description": "Live agent"},
         [_user("go"), _assistant("tool_use")], 5),
    ])

    instance = manager.Manager.__new__(manager.Manager)
    instance._panel = FakePanel()
    instance._known = set()
    instance._scan()

    payload = instance._panel.upserts[0][1]
    assert [s["name"] for s in payload["subagents"]] == ["Live agent"]


def test_display_order_is_spawn_order_not_last_write_order(monkeypatch, tmp_path):
    """Rows must not swap places every time an agent logs a line."""
    spawned = ["zzz", "mmm", "aaa"]  # ids sort the other way on purpose
    agents = []
    for agent_id in spawned:
        agents.append((agent_id, {"description": f"Task {agent_id}"},
                       [_user("go"), _assistant("tool_use")], 10))
    subagents = _make_session(tmp_path, monkeypatch, "sid-order", agents[:1])
    time.sleep(0.02)
    for agent_id, meta, lines, _ in agents[1:]:
        (subagents / f"agent-{agent_id}.jsonl").write_text("\n".join(lines) + "\n")
        (subagents / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta))
        time.sleep(0.02)
    # Make the first-spawned agent the most recently written one.
    for offset, agent_id in enumerate(reversed(spawned)):
        stamp = time.time() - offset
        os.utime(subagents / f"agent-{agent_id}.jsonl", (stamp, stamp))
    manager._SUBAGENT_CACHE.clear()

    order = [s["agent_id"] for s in manager._second_level_lights("sid-order", _now())]

    assert order == spawned


def test_hook_state_for_an_agent_without_a_transcript_draws_no_light(monkeypatch, tmp_path):
    """Claude Code emits agent events that never get a transcript on disk."""
    _make_session(tmp_path, monkeypatch, "sid-ghost", [
        ("real01", {"description": "Real agent"},
         [_user("go"), _assistant("tool_use")], 5),
    ])
    hook_states = tmp_path / "hook-states"
    hook_states.mkdir()
    (hook_states / "sid-ghost--ghost01.json").write_text(json.dumps({
        "agent_id": "ghost01",
        "agent_type": "subagent",
        "status": "idle",
        "timestamp": _now().isoformat(),
    }))
    subs = manager._discover_subagents("sid-ghost", _now())

    assert [s["agent_id"] for s in subs] == ["real01"]


# ----------------------------------------------------------------------
# background tasks (Bash run_in_background)
# ----------------------------------------------------------------------
def _bg_start(tool_use_id, description, ts="2026-08-24T13:00:00.000Z"):
    return json.dumps({
        "type": "assistant", "timestamp": ts,
        "message": {"content": [{
            "type": "tool_use", "id": tool_use_id, "name": "Bash",
            "input": {"command": "npm run dev", "description": description,
                      "run_in_background": True},
        }]},
    })


def _bg_ack(tool_use_id, task_id, ts="2026-08-24T13:00:01.000Z"):
    return json.dumps({
        "type": "user", "timestamp": ts,
        "message": {"content": [{
            "type": "tool_result", "tool_use_id": tool_use_id,
            "content": f"Command running in background with ID: {task_id}.",
        }]},
        "toolUseResult": {"backgroundTaskId": task_id, "stdout": "", "stderr": ""},
    })


def _bg_notify(task_id, status="completed", exit_code=0, ts=None):
    # Default to "just finished": finished_at is taken from this timestamp, so a
    # fixed past date would mean the task completed long ago and is not shown.
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    summary = f'Background command "x" completed (exit code {exit_code})'
    return json.dumps({
        "type": "attachment", "timestamp": ts,
        "attachment": {
            "type": "queued_command", "commandMode": "task-notification",
            "prompt": (f"<task-notification>\n<task-id>{task_id}</task-id>\n"
                       f"<status>{status}</status>\n<summary>{summary}</summary>\n"
                       "</task-notification>"),
        },
    })


def _write_transcript(tmp_path, monkeypatch, session_id, lines):
    projects = tmp_path / "projects"
    project = projects / "-Users-x-proj"
    project.mkdir(parents=True, exist_ok=True)
    transcript = project / f"{session_id}.jsonl"
    transcript.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(manager, "CLAUDE_PROJECTS_DIR", projects)
    manager._SESSION_TRANSCRIPT_CACHE.clear()
    manager._BACKGROUND_STATE.clear()
    manager._SESSION_CWD_CACHE.clear()
    manager._FORK_PARENT_CACHE.clear()
    return transcript


def test_background_task_is_labelled_by_its_description(monkeypatch, tmp_path):
    _write_transcript(tmp_path, monkeypatch, "sid-bg", [
        _bg_start("toolu_1", "Run the dev server"),
        _bg_ack("toolu_1", "bfrhesjq8"),
    ])
    tasks = manager._background_tasks("sid-bg", _now())

    assert [(t["kind"], t["name"], t["status"]) for t in tasks] == [
        ("task", "Run the dev server", "working")
    ]


def test_background_task_exit_status_drives_the_light(monkeypatch, tmp_path):
    _write_transcript(tmp_path, monkeypatch, "sid-bg2", [
        _bg_start("toolu_1", "Passing job"), _bg_ack("toolu_1", "task_ok"),
        _bg_start("toolu_2", "Failing job"), _bg_ack("toolu_2", "task_bad"),
        _bg_start("toolu_3", "Killed job"), _bg_ack("toolu_3", "task_kill"),
        _bg_notify("task_ok", "completed", 0),
        _bg_notify("task_bad", "completed", 2),
        _bg_notify("task_kill", "killed", 0),
    ])
    by_name = {t["name"]: t for t in manager._background_tasks("sid-bg2", _now())}

    assert by_name["Passing job"]["status"] == "idle"
    assert by_name["Failing job"]["status"] == "error"
    assert by_name["Failing job"]["exit_code"] == 2
    assert by_name["Killed job"]["status"] == "stale"


def test_subagent_notifications_do_not_create_task_lights(monkeypatch, tmp_path):
    """Subagents emit the same <task-notification>; only Bash tasks are tracked."""
    _write_transcript(tmp_path, monkeypatch, "sid-bg3", [
        _bg_notify("a121099a71e811dfe", "completed", 0),
    ])
    assert manager._background_tasks("sid-bg3", _now()) == []


def test_transcript_is_parsed_incrementally(monkeypatch, tmp_path):
    transcript = _write_transcript(tmp_path, monkeypatch, "sid-bg4", [
        _bg_start("toolu_1", "Long job"), _bg_ack("toolu_1", "task_1"),
    ])
    assert manager._background_tasks("sid-bg4", _now())[0]["status"] == "working"
    consumed = manager._BACKGROUND_STATE["sid-bg4"]["offset"]
    assert consumed == transcript.stat().st_size

    with transcript.open("a") as handle:
        handle.write(_bg_notify("task_1", "completed", 0) + "\n")
    tasks = manager._background_tasks("sid-bg4", _now())

    assert tasks[0]["status"] == "idle"
    # Only the appended bytes were read, not the whole file again.
    assert manager._BACKGROUND_STATE["sid-bg4"]["offset"] > consumed


def test_rewritten_transcript_is_reparsed_from_scratch(monkeypatch, tmp_path):
    """Compaction shrinks the file; the cursor must not point past the end."""
    transcript = _write_transcript(tmp_path, monkeypatch, "sid-bg5", [
        _bg_start("toolu_1", "First job"), _bg_ack("toolu_1", "task_1"),
        _bg_notify("task_1", "completed", 0),
    ])
    manager._background_tasks("sid-bg5", _now())
    transcript.write_text("\n".join([
        _bg_start("toolu_9", "Job after compaction"), _bg_ack("toolu_9", "task_9"),
    ]) + "\n")
    tasks = manager._background_tasks("sid-bg5", _now())

    assert [t["name"] for t in tasks] == ["Job after compaction"]


def test_finished_background_task_disappears_after_done_timeout(monkeypatch, tmp_path):
    _write_transcript(tmp_path, monkeypatch, "sid-bg6", [
        _bg_start("toolu_1", "Old job"), _bg_ack("toolu_1", "task_1"),
        _bg_notify("task_1", "completed", 0),
    ])
    manager._background_tasks("sid-bg6", _now())
    state = manager._BACKGROUND_STATE["sid-bg6"]["tasks"]["task_1"]
    state["finished_at"] = time.time() - manager.SUBAGENT_DONE_TIMEOUT - 60

    assert manager._background_tasks("sid-bg6", _now()) == []


def test_partial_trailing_line_is_not_consumed(monkeypatch, tmp_path):
    """A half-written JSON line must be left for the next scan."""
    transcript = _write_transcript(tmp_path, monkeypatch, "sid-bg7", [
        _bg_start("toolu_1", "Complete job"), _bg_ack("toolu_1", "task_1"),
    ])
    complete = transcript.stat().st_size
    with transcript.open("a") as handle:
        handle.write('{"type": "assistant", "message": {"cont')
    manager._background_tasks("sid-bg7", _now())

    assert manager._BACKGROUND_STATE["sid-bg7"]["offset"] == complete


def test_long_finished_task_is_not_resurrected_by_a_restart(monkeypatch, tmp_path):
    """finished_at must come from the transcript, not from parse time.

    _BACKGROUND_STATE lives in memory, so every manager restart re-reads the
    transcript from offset 0. Stamping wall-clock time on absorption made every
    background task the session ever ran reappear as if it had just finished —
    failed ones included.
    """
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat()
    _write_transcript(tmp_path, monkeypatch, "sid-bg8", [
        _bg_start("toolu_1", "Failing build job"), _bg_ack("toolu_1", "task_1"),
        _bg_notify("task_1", "failed", 3, ts=long_ago),
    ])

    assert manager._background_tasks("sid-bg8", _now()) == []


def test_session_is_grouped_by_where_it_started_not_where_it_wandered(monkeypatch, tmp_path):
    """A session that cds into a nested clone still belongs to its own project."""
    outer = tmp_path / "test"
    nested = outer / "DataFlow-Skills"      # an unrelated clone living inside
    (outer / ".git").mkdir(parents=True)
    (nested / ".git").mkdir(parents=True)

    def entry(cwd, ts):
        return json.dumps({"type": "user", "cwd": str(cwd), "timestamp": ts,
                           "message": {"content": "hi"}})

    _write_transcript(tmp_path, monkeypatch, "sid-wander", [
        entry(outer, "2026-08-24T13:00:00.000Z"),    # launched here
        entry(nested, "2026-08-24T13:30:00.000Z"),   # wandered here
    ])
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    now_iso = _now().isoformat()
    (status_dir / "sid-wander.json").write_text(json.dumps({
        "status": "working", "session_id": "sid-wander",
        "cwd": str(nested),                 # the hook only ever sees this
        "project_name": "DataFlow-Skills",  # ...and guessed wrong from it
        "timestamp": now_iso, "prompt_time": now_iso,
    }))
    monkeypatch.setattr(manager, "STATUS_DIR", status_dir)
    monkeypatch.setattr(manager, "SUBAGENT_DIR", tmp_path / "hook-states")

    instance = manager.Manager.__new__(manager.Manager)
    instance._panel = FakePanel()
    instance._known = set()
    instance._scan()

    payload = instance._panel.upserts[0][1]
    assert payload["project_name"] == "test"
    assert payload["project_root"] == str(outer)


def test_resumed_session_follows_a_renamed_project(monkeypatch, tmp_path):
    """The recorded launch cwd wins only while it still exists.

    Renaming a project directory strands the transcript on a path that resolves
    to nothing — Claude Code keeps reporting the old string for the session's
    whole life. Grouping on it files a resumed session under the dead path's
    parent (a renamed ~/projects/foo shows up as "projects"), so once the
    recorded directory is gone the live cwd from hook events is the answer.
    """
    renamed = tmp_path / "Skill_test"
    (renamed / ".git").mkdir(parents=True)
    gone = tmp_path / "test"                  # the name it was launched under

    _write_transcript(tmp_path, monkeypatch, "sid-renamed", [
        json.dumps({"type": "user", "cwd": str(gone),
                    "timestamp": "2026-08-24T13:00:00.000Z",
                    "message": {"content": "hi"}}),
    ])
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    now_iso = _now().isoformat()
    (status_dir / "sid-renamed.json").write_text(json.dumps({
        "status": "idle", "session_id": "sid-renamed",
        "cwd": str(renamed),                  # where it actually is now
        "timestamp": now_iso, "prompt_time": now_iso,
    }))
    monkeypatch.setattr(manager, "STATUS_DIR", status_dir)
    monkeypatch.setattr(manager, "SUBAGENT_DIR", tmp_path / "hook-states")

    instance = manager.Manager.__new__(manager.Manager)
    instance._panel = FakePanel()
    instance._known = set()
    instance._scan()

    payload = instance._panel.upserts[0][1]
    assert payload["project_name"] == "Skill_test"
    assert payload["project_root"] == str(renamed)


def test_missing_directory_keeps_its_own_name(monkeypatch, tmp_path):
    """A dead path must not be filed under its parent.

    With nothing live to fall back on, ~/projects/test that no longer exists is
    still "test" — reporting "projects" names the container, not the project.
    """
    monkeypatch.setattr(manager.Path, "home", staticmethod(lambda: tmp_path))
    projects = tmp_path / "projects"
    projects.mkdir()

    assert manager._project_identity(str(projects / "test"))[0] == "test"
    assert manager._project_identity(str(projects / "test" / "sub"))[0] == "test"


def test_launch_cwd_is_read_once_per_session(monkeypatch, tmp_path):
    transcript = _write_transcript(tmp_path, monkeypatch, "sid-once", [
        json.dumps({"type": "user", "cwd": "/proj/alpha",
                    "timestamp": "2026-08-24T13:00:00.000Z"}),
    ])
    assert manager._session_launch_cwd("sid-once") == "/proj/alpha"
    transcript.unlink()   # cached answer must survive the file going away
    assert manager._session_launch_cwd("sid-once") == "/proj/alpha"


# ======================================================================
# background command completion notices
# ======================================================================
def _bg_transcript_items(task_id: str, notice_shape: str) -> list[dict]:
    """A transcript that starts a background command and then finishes it."""
    launch = {
        "type": "assistant",
        "timestamp": "2026-08-28T09:23:40.000Z",
        "message": {"content": [{
            "type": "tool_use", "id": "tu-1", "name": "Bash",
            "input": {"run_in_background": True, "description": "Run a job"},
        }]},
    }
    register = {
        "type": "user",
        "timestamp": "2026-08-28T09:23:40.950Z",
        "toolUseResult": {"backgroundTaskId": task_id},
        "message": {"content": [{"type": "tool_result", "tool_use_id": "tu-1"}]},
    }
    notice_text = (
        f"<task-notification>\n<task-id>{task_id}</task-id>\n"
        "<status>completed</status>\n<summary>Background command completed "
        "(exit code 0)</summary>\n</task-notification>"
    )
    if notice_shape == "attachment":
        notice = {
            "type": "attachment",
            "timestamp": "2026-08-28T09:30:00.000Z",
            "attachment": {"commandMode": "task-notification", "prompt": notice_text},
        }
    elif notice_shape == "user-string":
        # The shape a real 2.1.x transcript uses: content is a bare string.
        notice = {
            "type": "user",
            "timestamp": "2026-08-28T09:30:00.000Z",
            "message": {"role": "user", "content": notice_text},
        }
    else:  # user-blocks
        notice = {
            "type": "user",
            "timestamp": "2026-08-28T09:30:00.000Z",
            "message": {"role": "user", "content": [{"type": "text", "text": notice_text}]},
        }
    return [launch, register, notice]


def _absorb(items):
    state = {"offset": 0, "pending": {}, "tasks": {}}
    for item in items:
        manager._absorb_transcript_item(state, item)
    return state


@pytest.mark.parametrize(
    "shape", ["attachment", "user-string", "user-blocks"]
)
def test_a_finished_background_command_leaves_running_in_every_notice_shape(shape):
    """The bug: a notice inlined in a plain user message was never read, so the
    command stayed blue forever — nothing else moves a task off "working"."""
    state = _absorb(_bg_transcript_items("bbmog5k2g", shape))
    task = state["tasks"]["bbmog5k2g"]
    assert task["status"] == "idle", f"{shape} notice was not absorbed"
    assert task["finished_at"] > 0


def test_a_background_command_with_no_notice_yet_stays_running():
    """A command that really is still running must not be marked finished."""
    items = _bg_transcript_items("bbmog5k2g", "user-string")[:2]
    state = _absorb(items)
    assert state["tasks"]["bbmog5k2g"]["status"] == "working"


def test_a_nonzero_exit_code_is_an_error_not_a_clean_finish():
    items = _bg_transcript_items("bbmog5k2g", "user-string")
    items[-1]["message"]["content"] = (
        "<task-notification>\n<task-id>bbmog5k2g</task-id>\n"
        "<status>completed</status>\n<summary>Background command failed with "
        "exit code 1</summary>\n</task-notification>"
    )
    state = _absorb(items)
    assert state["tasks"]["bbmog5k2g"]["status"] == "error"
    assert state["tasks"]["bbmog5k2g"]["exit_code"] == 1


def test_a_notice_for_an_untracked_id_is_ignored():
    """Subagents emit these too; only background commands are tracked here."""
    items = _bg_transcript_items("bbmog5k2g", "user-string")
    items[-1]["message"]["content"] = items[-1]["message"]["content"].replace(
        "bbmog5k2g", "someothertask"
    )
    state = _absorb(items)
    assert state["tasks"]["bbmog5k2g"]["status"] == "working"
    assert "someothertask" not in state["tasks"]


def test_an_ordinary_user_message_is_not_mistaken_for_a_notice():
    state = _absorb(_bg_transcript_items("bbmog5k2g", "user-string")[:2] + [{
        "type": "user",
        "timestamp": "2026-08-28T09:31:00.000Z",
        "message": {"role": "user", "content": "帮我看下 <task-id> 这个格式"},
    }])
    assert state["tasks"]["bbmog5k2g"]["status"] == "working"


# ======================================================================
# sessions killed from the terminal (no SessionEnd)
# ======================================================================
def _owned(pid, cmd="/Users/x/.local/bin/claude --dangerously-skip-permissions"):
    return {"status": "working", "owner_pid": pid, "owner_cmd": cmd}


def test_a_session_whose_process_is_gone_counts_as_dead():
    """Killed from the terminal: no SessionEnd, so only the pid gives it away."""
    dead_pid = 999999  # above the platform maximum, so it can never exist
    assert manager._session_is_dead(_owned(dead_pid)) is True


def test_a_session_whose_process_is_alive_is_left_alone():
    assert manager._session_is_dead(_owned(os.getpid())) is False


def test_a_status_file_without_an_owner_pid_is_left_alone():
    """Files written before this existed must not all vanish at once."""
    assert manager._session_is_dead({"status": "working"}) is False
    assert manager._session_is_dead({"status": "working", "owner_pid": None}) is False


def test_a_parent_that_was_not_claude_is_not_trusted():
    """A shell that exited would otherwise read as a dead session."""
    assert manager._session_is_dead(_owned(999999, cmd="/bin/zsh -l")) is False
    assert manager._session_is_dead(_owned(999999, cmd="")) is False


def test_pid_1_is_never_treated_as_an_owner():
    assert manager._session_is_dead(_owned(1)) is False
    assert manager._session_is_dead(_owned(0)) is False


def test_scan_drops_the_row_and_the_file_for_a_killed_session(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "STATUS_DIR", tmp_path)
    live = tmp_path / "alive.json"
    dead = tmp_path / "killed.json"
    stamp = _now().isoformat()
    live.write_text(json.dumps({
        **_owned(os.getpid()), "session_id": "alive", "cwd": "/Users/x/projects/demo",
        "timestamp": stamp, "prompt_time": stamp,
    }))
    dead.write_text(json.dumps({
        **_owned(999999), "session_id": "killed", "cwd": "/Users/x/projects/demo",
        "timestamp": stamp, "prompt_time": stamp,
    }))

    panel = FakePanel()
    m = manager.Manager.__new__(manager.Manager)
    m._panel, m._known = panel, set()
    m._scan()

    assert [sid for sid, _ in panel.upserts] == ["alive"]
    assert "killed" in panel.removes
    # The file is what the poll rebuilds from, so it has to go too.
    assert not dead.exists()
    assert live.exists()


# ------------------------------------------------------- single manager only


@pytest.fixture()
def singleton_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "MANAGER_SINGLETON", tmp_path / "manager.singleton")
    monkeypatch.setattr(manager, "_SINGLETON_HANDLE", None)
    yield
    handle = manager._SINGLETON_HANDLE
    if handle is not None:
        handle.close()
    manager._SINGLETON_HANDLE = None


def test_the_singleton_lock_is_granted_once(singleton_lock):
    """The heartbeat check is read-then-act; two starts can race past it."""
    assert manager._claim_singleton() is True
    assert manager._claim_singleton(timeout=0.05) is False


def test_the_lock_is_released_when_its_holder_lets_go(singleton_lock):
    assert manager._claim_singleton() is True
    manager._SINGLETON_HANDLE.close()  # what process death does
    manager._SINGLETON_HANDLE = None

    assert manager._claim_singleton(timeout=0.05) is True


def test_an_unusable_lock_file_never_blocks_the_panel(tmp_path, monkeypatch):
    """A lock that cannot be opened must not be read as "someone else has it"."""
    monkeypatch.setattr(manager, "MANAGER_SINGLETON", tmp_path / "nope" / "x" / "l")
    monkeypatch.setattr(manager, "MANAGER_SINGLETON",
                        tmp_path / "file-in-the-way" / "manager.singleton")
    (tmp_path / "file-in-the-way").write_text("not a directory")

    assert manager._claim_singleton(timeout=0.05) is True


def test_the_lock_is_taken_before_anything_is_killed(monkeypatch, singleton_lock):
    """Killing first, then failing to take the lock, leaves no manager at all.

    That is exactly what happened once: a second start shot the healthy
    incumbent, then could not acquire the lock its dying process still held, so
    it exited too — and the panel vanished.
    """
    order = []
    monkeypatch.setattr(manager, "_kill_old_manager", lambda: order.append("kill"))
    monkeypatch.setattr(manager, "_another_manager_running", lambda: True)
    real_claim = manager._claim_singleton

    def claim(timeout=1.0):
        order.append("claim")
        return real_claim(timeout)

    monkeypatch.setattr(manager, "_claim_singleton", claim)
    monkeypatch.setattr(manager, "configure_hooks", lambda: None)
    # Stop __init__ right after the guards; the Tk panel is not under test.
    monkeypatch.setattr(manager.tk, "Tk", _boom)

    with pytest.raises(_Stop):
        manager.Manager()

    assert order == ["claim", "kill"]


def test_a_newcomer_that_loses_the_lock_kills_nobody(monkeypatch, singleton_lock):
    killed = []
    monkeypatch.setattr(manager, "_kill_old_manager", lambda: killed.append(1))
    monkeypatch.setattr(manager, "_claim_singleton", lambda timeout=1.0: False)

    with pytest.raises(SystemExit):
        manager.Manager()

    assert killed == []


class _Stop(Exception):
    pass


def _boom(*_args, **_kwargs):
    raise _Stop()


# ======================================================================
# Codex completion cards (no hook exists, so polling has to spot the turn end)
# ======================================================================
class CardPanel(FakePanel):
    def __init__(self):
        super().__init__()
        self.cards = []

    def show_completion(self, data):
        self.cards.append(data)


def _codex_row(session_id, status, **extra):
    return {
        "agent_kind": "codex", "status": status, "session_id": session_id,
        "cwd": "/Users/x/projects/demo", "conversation_title": "cc_lights",
        "last_prompt": "问题", "model": "gpt-5", "input_tokens": 900,
        "output_tokens": 210, **extra,
    }


def _codex_manager():
    m = manager.Manager.__new__(manager.Manager)
    m._panel = CardPanel()
    m._known = set()
    m._codex_status = {}
    return m


def test_a_finished_codex_turn_raises_one_card():
    m = _codex_manager()
    m._codex_completions([_codex_row("c1", "working")])
    assert m._panel.cards == []
    m._codex_completions([_codex_row("c1", "idle")])
    assert len(m._panel.cards) == 1
    assert m._panel.cards[0]["agent_kind"] == "codex"


def test_a_codex_session_already_idle_at_startup_stays_quiet():
    """Otherwise every open Codex thread announces itself on manager restart."""
    m = _codex_manager()
    m._codex_completions([_codex_row(f"c{i}", "idle") for i in range(3)])
    assert m._panel.cards == []


def test_a_codex_turn_still_running_raises_nothing():
    m = _codex_manager()
    for _ in range(5):
        m._codex_completions([_codex_row("c1", "working")])
    assert m._panel.cards == []


def test_the_card_is_not_repeated_while_the_session_stays_idle():
    """discover_sessions runs on every poll, not once per turn."""
    m = _codex_manager()
    m._codex_completions([_codex_row("c1", "working")])
    for _ in range(10):
        m._codex_completions([_codex_row("c1", "idle")])
    assert len(m._panel.cards) == 1


def test_each_new_turn_raises_its_own_card():
    m = _codex_manager()
    for _ in range(3):
        m._codex_completions([_codex_row("c1", "working")])
        m._codex_completions([_codex_row("c1", "idle")])
    assert len(m._panel.cards) == 3


def test_going_quiet_is_not_a_completion():
    """stale means 30 minutes of silence, not a turn that ended."""
    m = _codex_manager()
    m._codex_completions([_codex_row("c1", "working")])
    m._codex_completions([_codex_row("c1", "stale")])
    assert m._panel.cards == []


def test_a_closed_session_is_forgotten_rather_than_announced_later():
    m = _codex_manager()
    m._codex_completions([_codex_row("c1", "working")])
    m._codex_completions([])                      # Codex released its lock
    assert m._codex_status == {}
    m._codex_completions([_codex_row("c1", "idle")])   # a reused id, first sight
    assert m._panel.cards == []


def test_several_codex_sessions_are_tracked_apart():
    m = _codex_manager()
    m._codex_completions([_codex_row("c1", "working"), _codex_row("c2", "working")])
    m._codex_completions([_codex_row("c1", "idle"), _codex_row("c2", "working")])
    assert [c["session_id"] for c in m._panel.cards] == ["c1"]


def test_a_row_with_no_session_id_is_skipped():
    m = _codex_manager()
    row = _codex_row("", "idle")
    m._codex_completions([row])
    assert m._codex_status == {}
    assert m._panel.cards == []


def test_scan_drops_a_leftover_agent_runner_file_but_keeps_a_fork(tmp_path, monkeypatch):
    """An --agent runner file is cleaned; a --fork-session (live branch) stays."""
    monkeypatch.setattr(manager, "STATUS_DIR", tmp_path)
    stamp = _now().isoformat()
    real = tmp_path / "real.json"
    fork = tmp_path / "branch.json"
    ghost = tmp_path / "agentrun.json"
    for name, sid, cmd in [
        (real, "real", "/x/claude --dangerously-skip-permissions"),
        (fork, "branch", "/x/claude --session-id branch --fork-session --resume"),
        (ghost, "agentrun", "/x/claude --session-id agentrun --agent claude"),
    ]:
        name.write_text(json.dumps({
            "status": "working", "session_id": sid,
            "cwd": "/Users/x/projects/diverseIO", "owner_pid": os.getpid(),
            "owner_cmd": cmd, "timestamp": stamp, "prompt_time": stamp,
        }))

    panel = FakePanel()
    m = manager.Manager.__new__(manager.Manager)
    m._panel, m._known = panel, set()
    m._codex_status = {}
    m._scan()

    shown = {sid for sid, _ in panel.upserts}
    assert shown == {"real", "branch"}, "a live branch was wrongly hidden"
    assert "agentrun" in panel.removes
    assert not ghost.exists()
    assert real.exists() and fork.exists()


# ======================================================================
# sessions continued elsewhere (/branch, continuation) — the parent is dead
# ======================================================================
def test_a_session_continued_into_another_is_superseded(monkeypatch, tmp_path):
    """A parent whose transcript ends with continued-in has moved on."""
    manager._SUPERSEDED_CACHE.clear()
    _write_transcript(tmp_path, monkeypatch, "parent", [
        json.dumps({"type": "user", "timestamp": "2026-09-09T11:00:00Z"}),
        json.dumps({"type": "continued-in", "sessionId": "parent",
                    "continuedInSessionId": "child-xyz",
                    "timestamp": "2026-09-09T11:16:47Z"}),
    ])
    assert manager._session_superseded("parent") is True


def test_a_live_session_is_not_superseded(monkeypatch, tmp_path):
    manager._SUPERSEDED_CACHE.clear()
    _write_transcript(tmp_path, monkeypatch, "live", [
        json.dumps({"type": "user", "timestamp": "2026-09-09T11:00:00Z"}),
        json.dumps({"type": "assistant", "timestamp": "2026-09-09T11:01:00Z"}),
    ])
    assert manager._session_superseded("live") is False


def test_the_continued_branch_child_is_not_itself_superseded(monkeypatch, tmp_path):
    """The child carries no continued-in of its own, so it stays visible."""
    manager._SUPERSEDED_CACHE.clear()
    _write_transcript(tmp_path, monkeypatch, "child-xyz", [
        json.dumps({"type": "user", "timestamp": "2026-09-09T11:17:00Z"}),
    ])
    assert manager._session_superseded("child-xyz") is False


def test_superseded_verdict_is_cached_until_the_transcript_changes(monkeypatch, tmp_path):
    manager._SUPERSEDED_CACHE.clear()
    t = _write_transcript(tmp_path, monkeypatch, "p2", [
        json.dumps({"type": "user", "timestamp": "2026-09-09T11:00:00Z"}),
    ])
    assert manager._session_superseded("p2") is False
    reads = []
    real_open = Path.open
    monkeypatch.setattr(Path, "open",
                        lambda self, *a, **k: reads.append(self) or real_open(self, *a, **k))
    manager._session_superseded("p2")  # same mtime/size → cached, no read
    assert t not in reads


def test_scan_drops_a_superseded_parent_and_deletes_its_file(monkeypatch, tmp_path):
    projects = tmp_path / "projects" / "-Users-x-proj"
    projects.mkdir(parents=True)
    (projects / "gone.jsonl").write_text(
        json.dumps({"type": "continued-in", "sessionId": "gone",
                    "continuedInSessionId": "next"}) + "\n")
    monkeypatch.setattr(manager, "CLAUDE_PROJECTS_DIR", tmp_path / "projects")
    manager._SESSION_TRANSCRIPT_CACHE.clear()
    manager._SUPERSEDED_CACHE.clear()

    status = tmp_path / "status"
    status.mkdir()
    monkeypatch.setattr(manager, "STATUS_DIR", status)
    stamp = _now().isoformat()
    (status / "gone.json").write_text(json.dumps({
        "status": "idle", "session_id": "gone", "cwd": "/Users/x/proj",
        "timestamp": stamp, "prompt_time": stamp,
    }))

    panel = FakePanel()
    m = manager.Manager.__new__(manager.Manager)
    m._panel, m._known = panel, set()
    m._codex_status = {}
    m._scan()

    assert "gone" in panel.removes
    assert not (status / "gone.json").exists()


# ----------------------------------------------------------------------
# monitors (Monitor tool)
# ----------------------------------------------------------------------
def _monitor_start(tool_use_id, description, timeout_ms=300_000, ts=None):
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    return json.dumps({
        "type": "assistant", "timestamp": ts,
        "message": {"content": [{
            "type": "tool_use", "id": tool_use_id, "name": "Monitor",
            "input": {"command": "tail -f app.log | grep ERROR",
                      "description": description, "timeout_ms": timeout_ms},
        }]},
    })


def _monitor_notify(tool_use_id, status="killed", ts=None):
    """A monitor notice names the tool-use id; it carries no backgroundTaskId."""
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    return json.dumps({
        "type": "attachment", "timestamp": ts,
        "attachment": {
            "type": "queued_command", "commandMode": "task-notification",
            "prompt": (f"<task-notification>\n<task-id>bg-unknown-{tool_use_id}</task-id>\n"
                       f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
                       f"<status>{status}</status>\n<summary>Monitor stopped</summary>\n"
                       "</task-notification>"),
        },
    })


def test_a_running_monitor_gets_its_own_light(monkeypatch, tmp_path):
    _write_transcript(tmp_path, monkeypatch, "sid-mon", [
        _monitor_start("toolu_m1", "deploy errors"),
    ])
    tasks = manager._background_tasks("sid-mon", _now())

    assert [(t["kind"], t["name"], t["status"]) for t in tasks] == [
        ("monitor", "deploy errors", "working")
    ]


def test_a_monitor_past_its_timeout_stops_being_working(monkeypatch, tmp_path):
    """No notice is written for every expiry, so the tool's own deadline retires it."""
    started = datetime.now(timezone.utc) - timedelta(minutes=12)
    _write_transcript(tmp_path, monkeypatch, "sid-mon-old", [
        _monitor_start("toolu_m1", "long gone", timeout_ms=300_000,
                       ts=started.isoformat()),
    ])
    tasks = manager._background_tasks("sid-mon-old", _now())

    # Expired at minute 5, hidden by the done-timeout at minute 15: still listed
    # here, but no longer claiming to be running.
    assert [t["status"] for t in tasks] == ["idle"]


def test_an_expired_monitor_disappears_after_the_done_timeout(monkeypatch, tmp_path):
    started = datetime.now(timezone.utc) - timedelta(minutes=40)
    _write_transcript(tmp_path, monkeypatch, "sid-mon-gone", [
        _monitor_start("toolu_m1", "ancient", timeout_ms=300_000,
                       ts=started.isoformat()),
    ])
    assert manager._background_tasks("sid-mon-gone", _now()) == []


def test_a_monitor_notice_is_matched_on_its_tool_use_id(monkeypatch, tmp_path):
    _write_transcript(tmp_path, monkeypatch, "sid-mon-kill", [
        _monitor_start("toolu_m1", "watch the queue"),
        _monitor_notify("toolu_m1", "killed"),
    ])
    tasks = manager._background_tasks("sid-mon-kill", _now())

    assert [(t["name"], t["status"]) for t in tasks] == [("watch the queue", "stale")]


def test_re_arming_a_monitor_replaces_the_finished_one(monkeypatch, tmp_path):
    """A monitor expires and is started again — that is one watch, not two lights."""
    _write_transcript(tmp_path, monkeypatch, "sid-mon-rearm", [
        _monitor_start("toolu_m1", "same watch"),
        _monitor_notify("toolu_m1", "completed"),
        _monitor_start("toolu_m2", "same watch"),
    ])
    tasks = manager._background_tasks("sid-mon-rearm", _now())

    assert [(t["name"], t["status"]) for t in tasks] == [("same watch", "working")]


# ----------------------------------------------------------------------
# forked sessions
# ----------------------------------------------------------------------
def test_a_forked_session_finds_the_subagents_of_the_id_it_forked_from(
    monkeypatch, tmp_path
):
    """`--fork-session` renames the conversation; agent transcripts do not move."""
    _make_session(tmp_path, monkeypatch, "sid-origin", [
        ("agent01", {"description": "Inherited agent"},
         [_user("go"), _assistant("tool_use")], 5),
    ])
    projects = tmp_path / "projects" / "-Users-x-proj"
    (projects / "sid-fork.jsonl").write_text(json.dumps({
        "type": "user", "timestamp": _now().isoformat(),
        "sessionId": "sid-fork",
        "cwd": "/Users/x/proj",
        "forkedFrom": {"sessionId": "sid-origin", "messageUuid": "u-1"},
        "message": {"content": [{"type": "text", "text": "carry on"}]},
    }) + "\n")
    manager._SESSION_TRANSCRIPT_CACHE.clear()
    manager._FORK_PARENT_CACHE.clear()

    assert manager._fork_chain("sid-fork") == ["sid-fork", "sid-origin"]
    assert [s["agent_id"] for s in manager._discover_subagents("sid-fork", _now())] == [
        "agent01"
    ]


def test_a_plain_session_reports_no_fork_parent(monkeypatch, tmp_path):
    _write_transcript(tmp_path, monkeypatch, "sid-plain", [_user("hello")])

    assert manager._fork_chain("sid-plain") == ["sid-plain"]


# ----------------------------------------------------------------------
# background commands that end without a notice, and what they are doing
# ----------------------------------------------------------------------
def _bg_timeout_start(tool_use_id, description, command="make test"):
    """A foreground Bash call — no run_in_background — that will outlive its timeout."""
    return json.dumps({
        "type": "assistant", "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": {"content": [{
            "type": "tool_use", "id": tool_use_id, "name": "Bash",
            "input": {"command": command, "description": description},
        }]},
    })


def _bg_timeout_ack(tool_use_id, task_id, output_path):
    return json.dumps({
        "type": "user", "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": {"content": [{
            "type": "tool_result", "tool_use_id": tool_use_id,
            "content": (f"Command did not complete within its 120s timeout and was "
                        f"moved to the background (ID: {task_id}). Output is being "
                        f"written to: {output_path}. You will be notified."),
        }]},
        "toolUseResult": {"backgroundTaskId": task_id, "timedOutAfterMs": 120000},
    })


def _stop(task_id, tool="TaskStop"):
    key = "shell_id" if tool == "KillShell" else "task_id"
    return json.dumps({
        "type": "assistant", "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": {"content": [{
            "type": "tool_use", "id": f"toolu_stop_{task_id}", "name": tool,
            "input": {key: task_id},
        }]},
    })


def test_a_command_moved_to_the_background_keeps_its_description(monkeypatch, tmp_path):
    _write_transcript(tmp_path, monkeypatch, "sid-auto", [
        _bg_timeout_start("toolu_1", "Wait for the build"),
        _bg_timeout_ack("toolu_1", "bauto1", str(tmp_path / "bauto1.output")),
    ])
    tasks = manager._background_tasks("sid-auto", _now())

    assert [(t["name"], t["status"]) for t in tasks] == [("Wait for the build", "working")]
    assert tasks[0]["output_path"] == str(tmp_path / "bauto1.output")


def test_a_foreground_command_leaves_nothing_pending(monkeypatch, tmp_path):
    """Remembering every Bash call must not grow without bound."""
    _write_transcript(tmp_path, monkeypatch, "sid-fg", [
        _bg_timeout_start("toolu_1", "ls"),
        json.dumps({"type": "user", "timestamp": "2026-08-24T13:00:01.000Z",
                    "message": {"content": [{"type": "tool_result",
                                             "tool_use_id": "toolu_1", "content": "ok"}]},
                    "toolUseResult": {"stdout": "ok"}}),
    ])
    manager._background_tasks("sid-fg", _now())

    assert manager._BACKGROUND_STATE["sid-fg"]["pending"] == {}


@pytest.mark.parametrize("tool", ["TaskStop", "KillShell"])
def test_a_stopped_task_stops_running(monkeypatch, tmp_path, tool):
    _write_transcript(tmp_path, monkeypatch, "sid-stop", [
        _bg_start("toolu_1", "Tail the logs"), _bg_ack("toolu_1", "bstop1"),
        _stop("bstop1", tool),
    ])
    tasks = manager._background_tasks("sid-stop", _now())

    assert [t["status"] for t in tasks] == ["stale"]


def test_a_monitor_stopped_by_its_task_id_stops_running(monkeypatch, tmp_path):
    _write_transcript(tmp_path, monkeypatch, "sid-mon-stop", [
        _monitor_start("toolu_m1", "watch errors"),
        json.dumps({"type": "user", "timestamp": _now().isoformat(),
                    "message": {"content": [{"type": "tool_result",
                                             "tool_use_id": "toolu_m1",
                                             "content": "Monitor started (task bmon1)"}]},
                    "toolUseResult": {"taskId": "bmon1", "timeoutMs": 300000}}),
        _stop("bmon1"),
    ])
    tasks = manager._background_tasks("sid-mon-stop", _now())

    assert [(t["kind"], t["status"]) for t in tasks] == [("monitor", "stale")]


def _aged_task(monkeypatch, tmp_path, sid, output, holders):
    """A running task old enough to be probed, with lsof faked."""
    _write_transcript(tmp_path, monkeypatch, sid, [
        _bg_timeout_start("toolu_1", "Long job"),
        _bg_timeout_ack("toolu_1", "bprobe", str(output)),
    ])
    monkeypatch.setattr(manager, "_output_holders", lambda paths: holders(paths))
    manager._background_tasks(sid, _now())   # parse the transcript
    task = manager._BACKGROUND_STATE[sid]["tasks"]["bprobe"]
    task["started_at"] = time.time() - 600
    task["probed_at"] = 0.0
    return manager._background_tasks(sid, _now())


def test_a_task_whose_output_nobody_holds_is_retired(monkeypatch, tmp_path):
    output = tmp_path / "bprobe.output"
    output.write_text("step 1\nstep 2\n")
    stamp = time.time() - 60
    os.utime(output, (stamp, stamp))
    tasks = _aged_task(monkeypatch, tmp_path, "sid-dead", output, lambda paths: set())

    assert [t["status"] for t in tasks] == ["stale"]
    # Finished when it last wrote, not when the probe noticed.
    assert abs(tasks[0]["age_seconds"] - 60) < 5


def test_a_held_task_stays_running_and_shows_its_last_line(monkeypatch, tmp_path):
    output = tmp_path / "bprobe.output"
    output.write_text("compiling\n\x1b[32mtest 3/10\x1b[0m\rtest 7/10\n\n")
    tasks = _aged_task(monkeypatch, tmp_path, "sid-live", output, lambda paths: set(paths))

    assert [(t["status"], t["detail"]) for t in tasks] == [("working", "test 7/10")]


def test_a_task_whose_output_file_vanished_is_retired(monkeypatch, tmp_path):
    tasks = _aged_task(monkeypatch, tmp_path, "sid-gone", tmp_path / "missing.output",
                       lambda paths: set(paths))

    assert tasks == []   # retired long ago, so already past the done-timeout


def test_lsof_failure_never_retires_a_task(monkeypatch, tmp_path):
    output = tmp_path / "bprobe.output"
    output.write_text("still going\n")
    tasks = _aged_task(monkeypatch, tmp_path, "sid-unknown", output, lambda paths: None)

    assert [t["status"] for t in tasks] == ["working"]
