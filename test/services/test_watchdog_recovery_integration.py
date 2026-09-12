"""Real SQLite/manifest tests with only provider/backend system boundaries faked."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.backends import registry
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import cleanup_service, terminal_recovery, terminal_service
from cli_agent_orchestrator.services.session_service import get_session, resync_live_terminals
from cli_agent_orchestrator.services.session_watchdog import scan_sessions
from cli_agent_orchestrator.services.terminal_recovery import (
    tmux_recovery_metadata,
    write_recovery_manifest,
)
from cli_agent_orchestrator.services.terminal_recovery_plan import (
    RecoveryPlanConflict,
    apply_recovery,
    plan_recovery,
    restore_registered_terminals,
)


@pytest.fixture
def runtime(isolated_memory_db, tmp_path, monkeypatch):
    backend = MagicMock()
    backend.list_sessions.return_value = [{"id": "cao-test"}, {"id": "cao-server-local"}]
    backend.list_windows.return_value = [{"name": "worker", "index": "0"}]
    backend.get_pane_id.return_value = "%123"
    backend.get_window_metadata.return_value = {}
    backend.get_history.return_value = "Ready for input"
    backend.supports_event_inbox.return_value = True
    backend.session_exists.return_value = True
    backend.session_exists_strict.return_value = True
    monkeypatch.setattr(registry, "_backend", backend)
    monkeypatch.setattr(terminal_recovery, "TERMINAL_RECOVERY_DIR", tmp_path / "recovery")
    provider = MagicMock()
    provider.supports_screen_detection = True
    provider.get_status.return_value = TerminalStatus.IDLE
    provider.get_status_from_screen.return_value = TerminalStatus.IDLE
    provider.extract_current_composer.return_value = None
    monkeypatch.setattr(terminal_service.provider_manager, "get_provider", lambda _: provider)
    monkeypatch.setattr(terminal_service, "inject_memory_context", lambda message, *_: message)
    monkeypatch.setattr(terminal_service, "_runtime_adopted_terminals", set())
    monkeypatch.setattr(cleanup_service, "SessionLocal", database.SessionLocal)
    return backend, provider, str(tmp_path)


def record(cwd, **overrides):
    return (
        dict(
            terminal_id="aabbccdd",
            tmux_session="cao-test",
            tmux_window="worker",
            provider="codex",
            agent_profile="developer",
            working_directory=cwd,
            allowed_tools=["send_message"],
            caller_id="11223344",
            group=["test"],
            metadata={},
        )
        | overrides
    )


def create_row(cwd, **overrides):
    data = record(cwd, **overrides)
    database.create_terminal(**data)


def test_bulk_preview_apply_and_repeat_preserve_policy_without_input(runtime):
    backend, _, cwd = runtime
    write_recovery_manifest(record(cwd))
    preview = plan_recovery()
    assert preview["windows"][0]["state"] == "exact"
    assert database.list_terminal_ids() == []
    backend.set_window_metadata.assert_not_called()
    result = apply_recovery(preview["token"])
    assert result["recovered"] == ["aabbccdd"]
    restored = database.get_terminal_metadata("aabbccdd")
    assert restored["allowed_tools"] == ["send_message"]
    assert restored["caller_id"] == "11223344"
    assert restored["group"] == ["test"]
    assert restored["working_directory"] == cwd
    assert restored["metadata"]["cao_recovery_hold"] is True
    assert apply_recovery(preview["token"]) == result
    backend.send_keys.assert_not_called()
    backend.kill_window.assert_not_called()


def test_replaced_pane_invalidates_preview_before_any_registration(runtime):
    backend, _, cwd = runtime
    write_recovery_manifest(record(cwd))
    preview = plan_recovery()
    backend.get_pane_id.return_value = "%456"
    with pytest.raises(RecoveryPlanConflict, match="changed"):
        apply_recovery(preview["token"])
    assert database.list_terminal_ids() == []


def test_duplicate_tmux_only_ids_have_no_first_winner(runtime):
    backend, _, cwd = runtime
    backend.list_windows.return_value = [
        {"name": "worker", "index": "0"},
        {"name": "other", "index": "1"},
    ]
    backend.get_window_metadata.side_effect = lambda session, window: tmux_recovery_metadata(
        record(cwd, tmux_window=window)
    )
    preview = plan_recovery()
    assert [w["state"] for w in preview["windows"]] == ["ambiguous", "ambiguous"]
    assert apply_recovery(preview["token"])["recovered"] == []
    assert database.list_terminal_ids() == []


def test_get_session_is_observational_and_resync_retains_renamed_row(runtime):
    backend, _, cwd = runtime
    create_row(cwd, tmux_window="old-name")
    assert get_session("cao-test")["terminals"][0]["tmux_window"] == "old-name"
    report = resync_live_terminals()
    assert report["stale"] == ["aabbccdd"]
    assert database.get_terminal_metadata("aabbccdd")["tmux_window"] == "old-name"
    backend.set_window_metadata.assert_not_called()
    backend.send_keys.assert_not_called()


def test_restart_restores_known_registration_and_preserves_row(runtime):
    backend, _, cwd = runtime
    create_row(cwd)
    assert restore_registered_terminals()["restored"] == ["aabbccdd"]
    assert get_session("cao-test")["terminals"][0]["id"] == "aabbccdd"
    assert terminal_service.get_terminal("aabbccdd")["status"] == "idle"
    backend.send_keys.assert_not_called()


def test_age_cleanup_retains_live_idle_worker(runtime):
    _, _, cwd = runtime
    create_row(cwd)
    with database.SessionLocal() as db:
        db.get(database.TerminalModel, "aabbccdd").last_active = datetime.now() - timedelta(days=90)
        db.commit()
    cleanup_service.cleanup_old_data()
    assert database.get_terminal_metadata("aabbccdd") is not None


def test_watchdog_restores_registration_and_delivers_idle_mail_once(runtime):
    backend, _, cwd = runtime
    create_row(cwd)
    database.create_inbox_message("11223344", "aabbccdd", "Please inspect the queued task")
    reports = scan_sessions()
    assert reports[-1]["state"] == "mail_nudged"
    assert database.get_pending_messages("aabbccdd") == []
    first_sends = backend.send_keys.call_count
    assert first_sends > 0
    scan_sessions()
    assert backend.send_keys.call_count == first_sends


@pytest.mark.parametrize("metadata", [{"role": "anchor-dev"}, {"cao_recovery_hold": True}])
def test_all_inbox_paths_hold_manual_and_recovered_panes(runtime, metadata):
    from cli_agent_orchestrator.services.inbox_service import inbox_service

    backend, _, cwd = runtime
    create_row(cwd, metadata=metadata)
    database.create_inbox_message("11223344", "aabbccdd", "Do not inject this")
    inbox_service.deliver_pending("aabbccdd")
    assert scan_sessions()[-1]["state"] == "held"
    assert len(database.get_pending_messages("aabbccdd")) == 1
    backend.send_keys.assert_not_called()


def test_watchdog_surfaces_unmanaged_and_mcp_error_without_nudging(runtime):
    backend, _, cwd = runtime
    assert scan_sessions()[0]["state"] == "unmanaged"
    create_row(cwd)
    backend.get_history.return_value = "MCP startup failed"
    report = scan_sessions()[-1]
    assert report["state"] == "mcp_error"
    assert database.list_watchdog_checks()[-1]["mcp"] == "error_observed"
    backend.send_keys.assert_not_called()
