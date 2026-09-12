"""Focused tests for the callback watchdog's durable ledger seam."""

from datetime import datetime, timedelta, timezone

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.callback import CallbackTaskState
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.callback_watchdog import (
    CallbackWatchdog,
    WorkerInspection,
)


def test_register_assignment_persists_callback_task(isolated_memory_db):
    """An assignment creates one restart-visible callback obligation."""
    del isolated_memory_db
    created_at = datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc)
    watchdog = CallbackWatchdog(stale_after_seconds=60)

    task = watchdog.register_assignment(
        "deadbeef",
        "a1b2c3d4",
        created_at=created_at,
    )

    assert task["worker_terminal_id"] == "deadbeef"
    assert task["caller_terminal_id"] == "a1b2c3d4"
    assert task["state"] == CallbackTaskState.ASSIGNED.value
    assert task["deadline_at"] == created_at + timedelta(seconds=60)
    assert database.list_callback_tasks(active_only=True) == [task]


def test_callback_marks_matching_task_received(isolated_memory_db):
    """A worker callback closes only the matching worker/caller obligation."""
    del isolated_memory_db
    watchdog = CallbackWatchdog(stale_after_seconds=60)
    watchdog.register_assignment("deadbeef", "a1b2c3d4")

    assert watchdog.record_callback("deadbeef", "a1b2c3d4") is True
    assert watchdog.record_callback("deadbeef", "a1b2c3d4") is False

    tasks = watchdog.list_tasks(active_only=False)
    assert len(tasks) == 1
    assert tasks[0]["state"] == CallbackTaskState.CALLBACK_RECEIVED.value
    assert tasks[0]["callback_received_at"] is not None


def _live_inspection(task, *, status=TerminalStatus.IDLE, metadata=None):
    """Build the positive runtime evidence needed for a test nudge."""
    del task
    return WorkerInspection(
        terminal_exists=True,
        session_exists=True,
        window_exists=True,
        status=status,
        output_fresh=True,
        metadata=metadata or {},
        reason="test runtime is present",
    )


def test_overdue_worker_is_nudged_at_most_once_and_survives_reload(isolated_memory_db, monkeypatch):
    """An overdue live worker gets one reminder, even after a watchdog reload."""
    now = datetime(2026, 9, 12, 14, 2, tzinfo=timezone.utc)
    sent = []

    watchdog = CallbackWatchdog(
        stale_after_seconds=60,
        inspector=lambda task: _live_inspection(task),
        nudge_sender=lambda task, message: sent.append((task["id"], message)),
    )
    task = watchdog.register_assignment(
        "deadbeef",
        "a1b2c3d4",
        created_at=now - timedelta(seconds=61),
    )

    first = watchdog.evaluate_once(now=now)
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    database_url = isolated_memory_db.url
    isolated_memory_db.dispose()
    reopened_engine = create_engine(database_url)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=reopened_engine))
    second_watchdog = CallbackWatchdog(
        stale_after_seconds=60,
        inspector=lambda row: _live_inspection(row),
        nudge_sender=lambda row, message: sent.append((row["id"], message)),
    )
    second = second_watchdog.evaluate_once(now=now + timedelta(seconds=1))

    assert first[0]["state"] == CallbackTaskState.OVERDUE.value
    assert first[0]["nudged"] is True
    assert second[0]["nudged"] is False
    assert len(sent) == 1
    persisted = database.get_callback_task(task["id"])
    assert persisted["state"] == CallbackTaskState.OVERDUE.value
    assert persisted["nudge_count"] == 1
    assert persisted["last_nudge_at"] == now
    reopened_engine.dispose()


def test_callback_after_nudge_closes_task_and_stops_future_nudges(isolated_memory_db):
    """A callback racing the reminder is authoritative and prevents repeats."""
    del isolated_memory_db
    now = datetime.now(timezone.utc)
    sent = []
    watchdog = CallbackWatchdog(
        stale_after_seconds=0,
        inspector=lambda task: _live_inspection(task),
        nudge_sender=lambda task, message: sent.append(message),
    )
    watchdog.register_assignment("deadbeef", "a1b2c3d4", created_at=now - timedelta(seconds=1))

    watchdog.evaluate_once(now=now)
    assert len(sent) == 1
    assert watchdog.record_callback("deadbeef", "a1b2c3d4") is True
    watchdog.evaluate_once(now=now + timedelta(seconds=1))

    task = watchdog.list_tasks(active_only=False)[0]
    assert task["state"] == CallbackTaskState.CALLBACK_RECEIVED.value
    assert task["nudge_count"] == 1
    assert len(sent) == 1


def test_missing_worker_is_not_deliverable_without_a_nudge(isolated_memory_db):
    """A missing durable row is classified without attempting input delivery."""
    del isolated_memory_db
    sent = []
    watchdog = CallbackWatchdog(
        stale_after_seconds=60,
        inspector=lambda task: WorkerInspection(
            terminal_exists=False,
            reason="terminal row is missing",
        ),
        nudge_sender=lambda task, message: sent.append(message),
    )
    watchdog.register_assignment("deadbeef", "a1b2c3d4")

    watchdog.evaluate_once(now=datetime.now(timezone.utc))

    task = watchdog.list_tasks(active_only=False)[0]
    assert task["state"] == CallbackTaskState.NOT_DELIVERABLE.value
    assert task["nudge_count"] == 0
    assert sent == []


def test_error_worker_is_failed_without_a_nudge(isolated_memory_db):
    """A known provider error is distinct from a missing runtime."""
    del isolated_memory_db
    sent = []
    watchdog = CallbackWatchdog(
        stale_after_seconds=0,
        inspector=lambda task: _live_inspection(task, status=TerminalStatus.ERROR),
        nudge_sender=lambda task, message: sent.append(message),
    )
    watchdog.register_assignment("deadbeef", "a1b2c3d4")

    watchdog.evaluate_once(now=datetime.now(timezone.utc))

    task = watchdog.list_tasks(active_only=False)[0]
    assert task["state"] == CallbackTaskState.FAILED.value
    assert task["nudge_count"] == 0
    assert sent == []


def test_human_or_anchor_metadata_suppresses_nudge(isolated_memory_db):
    """Human/manual/persistent anchor terminals are never auto-nudged."""
    del isolated_memory_db
    sent = []
    watchdog = CallbackWatchdog(
        stale_after_seconds=0,
        inspector=lambda task: _live_inspection(
            task,
            metadata={"role": "anchor-dev", "persistent": True},
        ),
        nudge_sender=lambda task, message: sent.append(message),
    )
    watchdog.register_assignment("deadbeef", "a1b2c3d4")

    watchdog.evaluate_once(now=datetime.now(timezone.utc))

    task = watchdog.list_tasks(active_only=False)[0]
    assert task["state"] == CallbackTaskState.OVERDUE.value
    assert task["nudge_count"] == 0
    assert "suppressed" in task["last_error"]
    assert sent == []
