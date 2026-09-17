"""Tests for hook event → status mapping."""
import json

from claude_eyes import hook


def test_all_mapped_events():
    assert hook.EVENT_STATUS["UserPromptSubmit"] == "working"
    assert hook.EVENT_STATUS["PostToolUse"] == "working"
    assert hook.EVENT_STATUS["PermissionRequest"] == "waiting"
    assert hook.EVENT_STATUS["Stop"] == "idle"
    assert hook.EVENT_STATUS["SessionStart"] == "idle"
    assert hook.EVENT_STATUS["SessionEnd"] == "closed"
    assert hook.EVENT_STATUS["PostToolUseFailure"] == "error"
    assert hook.EVENT_STATUS["PreToolUse"] == "working"
    assert hook.EVENT_STATUS["PreCompact"] == "compacting"
    assert hook.EVENT_STATUS["SubagentStop"] == "working"


def test_hook_writes_file(monkeypatch, tmp_path):
    status_dir = tmp_path / "status"
    logs_dir = tmp_path / "logs"
    monkeypatch.setattr(hook, "STATUS_DIR", status_dir)
    monkeypatch.setattr(hook, "LOG_DIR", logs_dir)

    result = hook.handle(json.dumps({
        "hook_event_name": "PostToolUse",
        "session_id": "sid-001",
        "tool_name": "Bash",
        "cwd": "/tmp",
    }))

    assert result["status"] == "working"
    assert result["session_id"] == "sid-001"
    assert "prompt_time" in result
    assert "done_time" in result
    assert (status_dir / "sid-001.json").exists()
    assert len((logs_dir / "history.jsonl").read_text().strip().split("\n")) == 1


def test_hook_empty_stdin_skipped(monkeypatch, tmp_path):
    status_dir = tmp_path / "status"
    logs_dir = tmp_path / "logs"
    monkeypatch.setattr(hook, "STATUS_DIR", status_dir)
    monkeypatch.setattr(hook, "LOG_DIR", logs_dir)

    result = hook.handle("")

    assert result["status"] == "skipped"
    assert not list(status_dir.glob("*.json"))


def test_hook_unknown_session_skipped(monkeypatch, tmp_path):
    status_dir = tmp_path / "status"
    logs_dir = tmp_path / "logs"
    monkeypatch.setattr(hook, "STATUS_DIR", status_dir)
    monkeypatch.setattr(hook, "LOG_DIR", logs_dir)

    result = hook.handle(json.dumps({
        "hook_event_name": "Stop",
        "session_id": "unknown",
    }))

    assert result["status"] == "skipped"
    assert not list(status_dir.glob("*.json"))


def test_hook_permission_request(monkeypatch, tmp_path):
    status_dir = tmp_path / "status"
    monkeypatch.setattr(hook, "STATUS_DIR", status_dir)
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")

    result = hook.handle(json.dumps({
        "hook_event_name": "PermissionRequest",
        "session_id": "sid-002",
        "tool_name": "Write",
    }))

    assert result["status"] == "waiting"


def test_hook_post_tool_use_failure(monkeypatch, tmp_path):
    status_dir = tmp_path / "status"
    monkeypatch.setattr(hook, "STATUS_DIR", status_dir)
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")

    result = hook.handle(json.dumps({
        "hook_event_name": "PostToolUseFailure",
        "session_id": "sid-003",
        "tool_name": "Bash",
    }))

    assert result["status"] == "error"


def test_hook_reads_title_and_last_prompt(monkeypatch, tmp_path):
    status_dir = tmp_path / "status"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "custom-title", "customTitle": "修复登录问题"}),
        json.dumps({"type": "last-prompt", "lastPrompt": "为什么登录一直超时？"}),
    ]), encoding="utf-8")
    monkeypatch.setattr(hook, "STATUS_DIR", status_dir)
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", tmp_path / "notifications")

    result = hook.handle(json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "session_id": "sid-title",
        "cwd": "/tmp/project",
        "transcript_path": str(transcript),
    }))

    assert result["conversation_title"] == "修复登录问题"
    assert result["last_prompt"] == "为什么登录一直超时？"


def test_hook_reads_turn_count_and_deduplicated_usage(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    assistant = {
        "type": "assistant",
        "message": {
            "id": "msg-1",
            "model": "claude-opus-5-0",
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 30,
                "output_tokens": 40,
            },
        },
    }
    transcript.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "第一问"}}),
        json.dumps(assistant),
        json.dumps(assistant),  # repeated streamed snapshot: count once
        json.dumps({"type": "user", "message": {"content": "第二问"}}),
        json.dumps({
            "type": "assistant",
            "message": {
                "id": "msg-2",
                "model": "claude-opus-5-0",
                "usage": {"input_tokens": 5, "output_tokens": 7},
            },
        }),
    ]), encoding="utf-8")
    monkeypatch.setattr(hook, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", tmp_path / "notifications")

    result = hook.handle(json.dumps({
        "hook_event_name": "Stop",
        "session_id": "sid-stats",
        "cwd": "/tmp/project",
        "transcript_path": str(transcript),
    }))

    assert result["user_turns"] == 2
    assert result["last_prompt"] == "第二问"
    assert result["model"] == "claude-opus-5-0"
    # Usage is scoped to the final user turn.
    assert result["input_tokens"] == 5
    assert result["output_tokens"] == 7


def test_stop_writes_one_completion_notification(monkeypatch, tmp_path):
    status_dir = tmp_path / "status"
    notification_dir = tmp_path / "notifications"
    monkeypatch.setattr(hook, "STATUS_DIR", status_dir)
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", notification_dir)

    hook.handle(json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "session_id": "sid-stop",
        "cwd": "/tmp/project",
        "prompt": "帮我检查构建失败原因",
    }))
    result = hook.handle(json.dumps({
        "hook_event_name": "Stop",
        "session_id": "sid-stop",
        "cwd": "/tmp/project",
    }))
    hook.handle(json.dumps({
        "hook_event_name": "Stop",
        "session_id": "sid-stop",
        "cwd": "/tmp/project",
    }))

    files = list(notification_dir.glob("*.json"))
    assert len(files) == 1
    notification = json.loads(files[0].read_text())
    assert notification["last_prompt"] == "帮我检查构建失败原因"
    assert notification["elapsed_seconds"] is not None
    assert result["status"] == "idle"


def test_system_messages_are_not_used_as_user_prompt():
    assert not hook._is_real_user_prompt("<system-reminder>hidden</system-reminder>")
    assert not hook._is_real_user_prompt("<task-notification>forked</task-notification>")
    assert not hook._is_real_user_prompt("<local-command-stdout>ok</local-command-stdout>")
    assert hook._is_real_user_prompt("请继续处理这个问题")


def test_project_identity_uses_git_root(tmp_path):
    root = tmp_path / "review"
    nested = root / "src" / "feature"
    nested.mkdir(parents=True)
    (root / ".git").mkdir()

    name, project_root = hook._project_identity(str(nested))

    assert name == "review"
    assert project_root == str(root)


def test_agent_events_never_overwrite_the_parent_session_status(monkeypatch, tmp_path):
    """A subagent hook must land in subagents/, not clobber the parent light."""
    status_dir = tmp_path / "status"
    subagent_dir = tmp_path / "subagents"
    monkeypatch.setattr(hook, "STATUS_DIR", status_dir)
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "SUBAGENT_DIR", subagent_dir)

    result = hook.handle(json.dumps({
        "hook_event_name": "PreToolUse",
        "session_id": "sid-parent",
        "agent_id": "agent-77",
        "agent_type": "Explore",
        "cwd": "/tmp",
    }))

    assert result["agent_id"] == "agent-77"
    assert result["parent_session_id"] == "sid-parent"
    assert not (status_dir / "sid-parent.json").exists()
    written = json.loads((subagent_dir / "sid-parent--agent-77.json").read_text())
    assert written["status"] == "working"
    assert written["agent_type"] == "Explore"


def test_permission_request_from_an_agent_is_recorded_as_waiting(monkeypatch, tmp_path):
    monkeypatch.setattr(hook, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "SUBAGENT_DIR", tmp_path / "subagents")

    result = hook.handle(json.dumps({
        "hook_event_name": "PermissionRequest",
        "session_id": "sid-parent",
        "agentId": "agent-88",
    }))

    assert result["status"] == "waiting"


def test_slash_commands_are_shown_as_the_typed_command():
    assert hook._user_prompt_text(
        "<command-name>/agents</command-name>\n"
        "            <command-message>agents</command-message>\n"
        "            <command-args></command-args>"
    ) == "/agents"
    assert hook._user_prompt_text(
        "<command-name>/loop</command-name>"
        "<command-message>loop</command-message>"
        "<command-args>5m /babysit-prs</command-args>"
    ) == "/loop 5m /babysit-prs"
    assert hook._user_prompt_text("<system-reminder>hidden</system-reminder>") == ""
    assert hook._user_prompt_text("  请继续  ") == "请继续"


def test_command_wrapper_never_becomes_the_displayed_prompt(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "先看下构建日志"}}),
        json.dumps({"type": "user", "message": {"content": (
            "<command-name>/resume</command-name>"
            "<command-message>resume</command-message>"
            "<command-args></command-args>")}}),
    ]), encoding="utf-8")
    monkeypatch.setattr(hook, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", tmp_path / "notifications")

    result = hook.handle(json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "session_id": "sid-cmd",
        "cwd": "/tmp/project",
        "transcript_path": str(transcript),
    }))

    assert result["last_prompt"] == "/resume"
    assert result["user_turns"] == 2


def test_metadata_recovers_when_hooks_were_installed_mid_session(monkeypatch, tmp_path):
    """A tool event must backfill metadata the missed UserPromptSubmit never wrote."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "custom-title", "customTitle": "补齐子灯组"}),
        json.dumps({"type": "user", "message": {"content": "支持子 agent 的二级灯组"}}),
    ]), encoding="utf-8")
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    (status_dir / "sid-mid.json").write_text(json.dumps({
        "status": "working", "session_id": "sid-mid", "last_prompt": "", "user_turns": 0,
    }))
    monkeypatch.setattr(hook, "STATUS_DIR", status_dir)
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")

    result = hook.handle(json.dumps({
        "hook_event_name": "PostToolUse",
        "session_id": "sid-mid",
        "cwd": "/tmp/project",
        "transcript_path": str(transcript),
    }))

    assert result["last_prompt"] == "支持子 agent 的二级灯组"
    assert result["conversation_title"] == "补齐子灯组"
    assert result["user_turns"] == 1


# ======================================================================
# owner pid — lets the manager spot a session killed from the terminal
# ======================================================================
def test_the_status_file_records_the_claude_process_that_owns_the_session(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(hook, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", tmp_path / "notifications")
    monkeypatch.setattr(hook.os, "getppid", lambda: 4242)
    monkeypatch.setattr(hook, "_owner_command", lambda pid: f"claude --pid-{pid}")

    payload = hook.handle(json.dumps({
        "hook_event_name": "PostToolUse", "session_id": "s1",
        "cwd": "/Users/x/projects/demo", "tool_name": "Bash",
    }))
    assert payload["owner_pid"] == 4242
    assert "claude" in payload["owner_cmd"]


def test_the_owner_command_is_only_read_once_per_session(tmp_path, monkeypatch):
    """It costs a subprocess, and this runs on every single tool call."""
    monkeypatch.setattr(hook, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", tmp_path / "notifications")
    monkeypatch.setattr(hook.os, "getppid", lambda: 4242)
    calls = []
    monkeypatch.setattr(hook, "_owner_command",
                        lambda pid: calls.append(pid) or "claude x")

    for _ in range(5):
        hook.handle(json.dumps({
            "hook_event_name": "PostToolUse", "session_id": "s1",
            "cwd": "/Users/x/projects/demo", "tool_name": "Bash",
        }))
    assert calls == [4242], "the command line was re-read on later events"


def test_a_changed_owner_pid_is_re_identified(tmp_path, monkeypatch):
    """A resumed session runs under a new process, so the old pid is worthless."""
    monkeypatch.setattr(hook, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", tmp_path / "notifications")
    calls = []
    monkeypatch.setattr(hook, "_owner_command",
                        lambda pid: calls.append(pid) or f"claude {pid}")

    monkeypatch.setattr(hook.os, "getppid", lambda: 111)
    hook.handle(json.dumps({"hook_event_name": "PostToolUse", "session_id": "s1",
                            "cwd": "/Users/x/projects/demo", "tool_name": "Bash"}))
    monkeypatch.setattr(hook.os, "getppid", lambda: 222)
    payload = hook.handle(json.dumps({"hook_event_name": "PostToolUse", "session_id": "s1",
                                      "cwd": "/Users/x/projects/demo", "tool_name": "Bash"}))
    assert calls == [111, 222]
    assert payload["owner_pid"] == 222
    assert payload["owner_cmd"] == "claude 222"


def _assistant(msg_id, ctx, output=10, sidechain=False):
    return json.dumps({
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {
            "id": msg_id,
            "model": "claude-opus-5-0",
            "usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 8,
                "cache_read_input_tokens": ctx - 10,
                "output_tokens": output,
            },
        },
    })


def test_hook_reports_context_size_from_the_latest_request(monkeypatch, tmp_path):
    """Context size is the last request's input total, not a running sum."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "第一问"}}),
        _assistant("msg-1", 40_000),
        json.dumps({"type": "user", "message": {"content": "第二问"}}),
        _assistant("msg-2", 90_000),
        # A subagent's request carries its own context, not this session's.
        _assistant("msg-sub", 5_000, sidechain=True),
    ]), encoding="utf-8")
    monkeypatch.setattr(hook, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", tmp_path / "notifications")

    result = hook.handle(json.dumps({
        "hook_event_name": "Stop",
        "session_id": "sid-ctx",
        "transcript_path": str(transcript),
    }))

    assert result["context_tokens"] == 90_000
    assert result["context_limit"] == 200_000


def test_hook_infers_the_long_context_window(monkeypatch, tmp_path):
    """The `[1m]` suffix Claude Code uses in its cost accounting is the signal."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "问"}}),
        _assistant("msg-1", 120_000),
        json.dumps({
            "type": "cost-state",
            "totalCostUSD": 12.5,
            "modelUsage": {"claude-opus-5-0[1m]": {"costUSD": 12.5}},
        }),
    ]), encoding="utf-8")
    monkeypatch.setattr(hook, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", tmp_path / "notifications")

    result = hook.handle(json.dumps({
        "hook_event_name": "Stop",
        "session_id": "sid-1m",
        "transcript_path": str(transcript),
    }))

    assert result["context_limit"] == 1_000_000
    assert result["cost_usd"] == 12.5


def test_hook_infers_the_long_window_from_an_oversized_context(monkeypatch, tmp_path):
    """No cost-state yet, but a 600K context proves the window is not 200K."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "问"}}),
        _assistant("msg-1", 600_000),
    ]), encoding="utf-8")
    monkeypatch.setattr(hook, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", tmp_path / "notifications")

    result = hook.handle(json.dumps({
        "hook_event_name": "Stop",
        "session_id": "sid-big",
        "transcript_path": str(transcript),
    }))

    assert result["context_limit"] == 1_000_000


def test_hook_counts_compactions_and_ignores_the_summary_they_leave(
    monkeypatch, tmp_path,
):
    """The summary is a user message, but not one the user ever typed."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "真实问题"}}),
        _assistant("msg-1", 190_000),
        json.dumps({
            "type": "system",
            "subtype": "compact_boundary",
            "compactMetadata": {
                "trigger": "auto",
                "preTokens": 190_000,
                "postTokens": 8_000,
                "cumulativeDroppedTokens": 182_000,
            },
        }),
        json.dumps({
            "type": "user",
            "isCompactSummary": True,
            "isVisibleInTranscriptOnly": True,
            "message": {"content": "This session is being continued from..."},
        }),
        _assistant("msg-2", 8_000),
    ]), encoding="utf-8")
    monkeypatch.setattr(hook, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", tmp_path / "notifications")

    result = hook.handle(json.dumps({
        "hook_event_name": "Stop",
        "session_id": "sid-compact",
        "transcript_path": str(transcript),
    }))

    assert result["compact_count"] == 1
    assert result["compact_trigger"] == "auto"
    assert result["compact_dropped"] == 182_000
    # Compaction shrank the window, and the meter follows it down.
    assert result["context_tokens"] == 8_000
    assert result["user_turns"] == 1
    assert result["last_prompt"] == "真实问题"


def test_hook_keeps_the_last_known_cost_when_the_transcript_has_none(
    monkeypatch, tmp_path,
):
    """cost-state is written rarely, so a gap must not blank the figure."""
    status_dir = tmp_path / "status"
    monkeypatch.setattr(hook, "STATUS_DIR", status_dir)
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", tmp_path / "notifications")
    status_dir.mkdir(parents=True)
    (status_dir / "sid-cost.json").write_text(json.dumps({
        "cost_usd": 4.25, "last_prompt": "旧问题", "last_event": "Stop",
    }))

    transcript = tmp_path / "session.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "新问题"}}),
        _assistant("msg-1", 12_000),
    ]), encoding="utf-8")

    result = hook.handle(json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "session_id": "sid-cost",
        "transcript_path": str(transcript),
    }))

    assert result["cost_usd"] == 4.25


def _settings(tmp_path, monkeypatch, config):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(config))
    monkeypatch.setattr(hook, "CLAUDE_SETTINGS", path)
    hook._SETTINGS_CACHE.update({"key": None, "models": frozenset()})
    return path


def _session(tmp_path, monkeypatch, context, model="claude-opus-5-0"):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "问"}}),
        json.dumps({
            "type": "assistant",
            "message": {"id": "m1", "model": model,
                        "usage": {"input_tokens": context, "output_tokens": 5}},
        }),
    ]), encoding="utf-8")
    monkeypatch.setattr(hook, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", tmp_path / "notifications")
    return hook.handle(json.dumps({
        "hook_event_name": "Stop",
        "session_id": "sid-window",
        "transcript_path": str(transcript),
    }))


def test_settings_reveal_the_long_window_before_any_cost_state(tmp_path, monkeypatch):
    """65K against a 200K default reads as 33% full; it is really 7%."""
    _settings(tmp_path, monkeypatch, {"model": "claude-opus-5-0[1m]"})

    result = _session(tmp_path, monkeypatch, 65_528)

    assert result["context_limit"] == 1_000_000


def test_the_window_comes_from_env_overrides_too(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch,
              {"env": {"ANTHROPIC_MODEL": "claude-opus-5-0[1m]"}})

    assert _session(tmp_path, monkeypatch, 65_528)["context_limit"] == 1_000_000


def test_a_session_on_another_model_keeps_the_standard_window(tmp_path, monkeypatch):
    """/model switched away from the configured default; it is not 1M."""
    _settings(tmp_path, monkeypatch, {"model": "claude-opus-5-0[1m]"})

    result = _session(tmp_path, monkeypatch, 65_528, model="claude-sonnet-5")

    assert result["context_limit"] == 200_000


def test_settings_without_the_suffix_prove_nothing(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, {"model": "claude-opus-5-0"})

    assert _session(tmp_path, monkeypatch, 65_528)["context_limit"] == 200_000


def test_unreadable_settings_fall_back_to_the_old_signals(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "CLAUDE_SETTINGS", tmp_path / "missing.json")
    hook._SETTINGS_CACHE.update({"key": None, "models": frozenset()})

    assert _session(tmp_path, monkeypatch, 65_528)["context_limit"] == 200_000
    # The oversized-context signal still stands on its own.
    assert _session(tmp_path, monkeypatch, 600_000)["context_limit"] == 1_000_000


# ------------------------------------------------------- incremental parsing


def _grow(transcript, records):
    with transcript.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def _read(monkeypatch, tmp_path, transcript, session_id="sid-inc"):
    monkeypatch.setattr(hook, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", tmp_path / "notifications")
    return hook.handle(json.dumps({
        "hook_event_name": "Stop",
        "session_id": session_id,
        "transcript_path": str(transcript),
    }))


def _conversation(turns):
    """A transcript with `turns` user turns, one assistant reply each."""
    records = []
    for index in range(turns):
        records.append({"type": "user", "message": {"content": f"问题 {index}"}})
        records.append({
            "type": "assistant",
            "message": {"id": f"m{index}", "model": "claude-opus-5-0",
                        "usage": {"input_tokens": 1000 * (index + 1),
                                  "output_tokens": 10}},
        })
    return records


def test_reading_in_pieces_matches_reading_it_whole(monkeypatch, tmp_path):
    """The whole point of the fold: resuming must not change the answer."""
    piecemeal = tmp_path / "piecemeal.jsonl"
    piecemeal.write_text("", encoding="utf-8")
    records = _conversation(6) + [
        {"type": "system", "subtype": "compact_boundary",
         "compactMetadata": {"trigger": "auto", "cumulativeDroppedTokens": 900}},
        {"type": "cost-state", "totalCostUSD": 3.5,
         "modelUsage": {"claude-opus-5-0[1m]": {}}},
    ] + _conversation(2)
    for chunk_start in range(0, len(records), 3):
        _grow(piecemeal, records[chunk_start:chunk_start + 3])
        incremental = _read(monkeypatch, tmp_path, piecemeal)

    at_once = tmp_path / "at-once.jsonl"
    at_once.write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    full = _read(monkeypatch, tmp_path, at_once, session_id="sid-full")

    fields = ("user_turns", "last_prompt", "model", "input_tokens", "output_tokens",
              "context_tokens", "context_limit", "compact_count", "compact_trigger",
              "compact_dropped", "cost_usd")
    assert {k: incremental[k] for k in fields} == {k: full[k] for k in fields}
    assert incremental["user_turns"] == 8


def test_only_the_new_bytes_are_read_on_the_next_event(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    _grow(transcript, _conversation(3))
    _read(monkeypatch, tmp_path, transcript)
    first_pass = transcript.stat().st_size

    _grow(transcript, _conversation(1))
    _read(monkeypatch, tmp_path, transcript)
    state = json.loads((tmp_path / "parse" / "sid-inc.json").read_text())

    assert state["offset"] == transcript.stat().st_size > first_pass


def test_a_rewritten_transcript_is_parsed_from_scratch(monkeypatch, tmp_path):
    """A resumed offset is only meaningful inside the file it was measured in."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    _grow(transcript, _conversation(4))
    assert _read(monkeypatch, tmp_path, transcript)["user_turns"] == 4

    transcript.write_text(
        "".join(json.dumps(r) + "\n" for r in _conversation(2)), encoding="utf-8")

    assert _read(monkeypatch, tmp_path, transcript)["user_turns"] == 2


def test_a_half_written_record_counts_now_but_is_read_again_later(
    monkeypatch, tmp_path,
):
    """A Stop can land while Claude Code is still flushing the turn."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    _grow(transcript, _conversation(1))
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "message": {"content": "半句话"}}))

    assert _read(monkeypatch, tmp_path, transcript)["user_turns"] == 2
    state = json.loads((tmp_path / "parse" / "sid-inc.json").read_text())
    assert state["user_turns"] == 1  # not committed: the line has no newline yet

    with transcript.open("a", encoding="utf-8") as fh:
        fh.write("\n")

    # Re-read from the same offset — counted once, not twice.
    assert _read(monkeypatch, tmp_path, transcript)["user_turns"] == 2


def test_the_parse_state_is_cleaned_up_when_the_session_ends(monkeypatch, tmp_path):
    """Nothing else would ever delete it, and every session leaves one."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    _grow(transcript, _conversation(1))
    _read(monkeypatch, tmp_path, transcript)
    assert (tmp_path / "parse" / "sid-inc.json").exists()

    hook.handle(json.dumps({
        "hook_event_name": "SessionEnd",
        "session_id": "sid-inc",
        "transcript_path": str(transcript),
    }))

    assert not (tmp_path / "parse" / "sid-inc.json").exists()


# ======================================================================
# /branch side sessions (fork-session / agent processes)
# ======================================================================
def test_a_fork_session_is_not_a_side_session():
    """A continued branch (`--fork-session --resume`) is the user's live session.

    Filtering it once hid active conversations; the superseded parent is dropped
    by its own continued-in record instead.
    """
    assert hook.is_side_session(
        "/x/versions/2.1.266 --session-id b2ea247c --fork-session --resume"
    ) is False


def test_an_agent_process_is_recognised_as_a_side_session():
    assert hook.is_side_session("/x/versions/2.1.266 --session-id 9dc --agent claude") is True


def test_a_normal_interactive_session_is_not_a_side_session():
    assert hook.is_side_session("/Users/x/.local/bin/claude --dangerously-skip-permissions") is False
    assert hook.is_side_session("/x/versions/2.1.266 --session-id abc") is False
    assert hook.is_side_session("") is False


def test_a_flag_only_as_a_substring_does_not_trip_the_check():
    """A cwd or prompt echoing the word must not read as the flag."""
    assert hook.is_side_session("/x/claude --dir /home/my--agent-notes") is False


def test_an_agent_runner_writes_no_status_file(tmp_path, monkeypatch):
    """The fix at the source: the throwaway agent runner never gets a row."""
    monkeypatch.setattr(hook, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", tmp_path / "notifications")
    monkeypatch.setattr(hook.os, "getppid", lambda: 5555)
    monkeypatch.setattr(hook, "_owner_command",
                        lambda pid: "/x/claude --session-id z --agent claude")

    result = hook.handle(json.dumps({
        "hook_event_name": "PostToolUse", "session_id": "agentrun",
        "cwd": "/Users/x/projects/diverseIO", "tool_name": "Bash",
    }))
    assert result["status"] == "skipped"
    assert not (tmp_path / "status" / "agentrun.json").exists()


def test_a_fork_session_still_writes_its_status_file(tmp_path, monkeypatch):
    """A continued branch is a real session and must appear on the panel."""
    monkeypatch.setattr(hook, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", tmp_path / "notifications")
    monkeypatch.setattr(hook.os, "getppid", lambda: 5555)
    monkeypatch.setattr(hook, "_owner_command",
                        lambda pid: "/x/claude --session-id z --fork-session --resume")

    hook.handle(json.dumps({
        "hook_event_name": "PostToolUse", "session_id": "branch",
        "cwd": "/Users/x/projects/diverseIO", "tool_name": "Bash",
    }))
    assert (tmp_path / "status" / "branch.json").exists()


def test_a_real_session_still_writes_its_status_file(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", tmp_path / "notifications")
    monkeypatch.setattr(hook.os, "getppid", lambda: 5555)
    monkeypatch.setattr(hook, "_owner_command",
                        lambda pid: "/x/claude --dangerously-skip-permissions")

    hook.handle(json.dumps({
        "hook_event_name": "PostToolUse", "session_id": "real",
        "cwd": "/Users/x/projects/diverseIO", "tool_name": "Bash",
    }))
    assert (tmp_path / "status" / "real.json").exists()


# ======================================================================
# in-progress prompt: UserPromptSubmit must show the just-typed prompt now
# ======================================================================
def test_userpromptsubmit_shows_the_new_prompt_before_it_hits_the_transcript(
    monkeypatch, tmp_path
):
    """The bug: the panel showed the previous prompt until the turn completed.

    UserPromptSubmit fires before the new turn is written, so the transcript's
    newest user message is still the previous turn's. The event carries the
    just-typed prompt, and that must win now rather than at Stop.
    """
    transcript = tmp_path / "session.jsonl"
    # Transcript still holds only the PREVIOUS turn.
    transcript.write_text("\n".join([
        json.dumps({"type": "custom-title", "customTitle": "登录排查"}),
        json.dumps({"type": "user", "message": {"role": "user", "content": "上一个问题"},
                    "timestamp": "2026-09-10T10:00:00Z"}),
    ]), encoding="utf-8")
    monkeypatch.setattr(hook, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", tmp_path / "notifications")

    result = hook.handle(json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "session_id": "sid-live",
        "cwd": "/tmp/project",
        "transcript_path": str(transcript),
        "prompt": "这是我正在问的新问题",
    }))

    assert result["last_prompt"] == "这是我正在问的新问题"
    assert result["status"] == "working"


def test_a_later_event_without_a_prompt_keeps_using_the_transcript(monkeypatch, tmp_path):
    """PreToolUse/Stop carry no prompt, so the transcript stays the source."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "user", "message": {"role": "user", "content": "真实的当前问题"},
                    "timestamp": "2026-09-10T10:00:00Z"}),
    ]), encoding="utf-8")
    monkeypatch.setattr(hook, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", tmp_path / "notifications")

    result = hook.handle(json.dumps({
        "hook_event_name": "Stop",
        "session_id": "sid-stop",
        "cwd": "/tmp/project",
        "transcript_path": str(transcript),
    }))
    assert result["last_prompt"] == "真实的当前问题"


# ======================================================================
# storage caps (memory/disk safety)
# ======================================================================
def test_a_giant_prompt_is_truncated_before_storage(tmp_path, monkeypatch):
    """A 190 KB pasted prompt must not bloat every status file and log line."""
    monkeypatch.setattr(hook, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", tmp_path / "notif")
    result = hook.handle(json.dumps({
        "hook_event_name": "UserPromptSubmit", "session_id": "big",
        "cwd": "/tmp/p", "prompt": "x" * 200000,
    }))
    assert len(result["last_prompt"]) <= hook.PROMPT_STORE_MAX


def test_the_history_log_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(hook, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hook, "NOTIFICATION_DIR", tmp_path / "notif")
    for _ in range(2000):
        hook.handle(json.dumps({
            "hook_event_name": "Stop", "session_id": "s",
            "cwd": "/tmp/p", "prompt": "y" * 4000,
        }))
    hist = tmp_path / "logs" / "history.jsonl"
    assert hist.stat().st_size <= hook.LOG_MAX_BYTES
    # The trim cut on a line boundary, so the last line still parses.
    assert json.loads(hist.read_text().splitlines()[-1])
