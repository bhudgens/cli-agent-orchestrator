"""API wiring tests for callback task visibility and callback closure."""

from datetime import datetime, timezone
from unittest.mock import patch

from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus


def test_list_callback_tasks_endpoint_defaults_to_active(client):
    """The API exposes active ledger rows and forwards the default filter."""
    task = {
        "id": "task-1",
        "worker_terminal_id": "deadbeef",
        "caller_terminal_id": "a1b2c3d4",
        "created_at": datetime.now(timezone.utc),
        "deadline_at": datetime.now(timezone.utc),
        "stale_after_at": datetime.now(timezone.utc),
        "state": "assigned",
        "nudge_count": 0,
    }
    with patch(
        "cli_agent_orchestrator.api.main.callback_watchdog.list_tasks",
        return_value=[task],
    ) as list_tasks:
        response = client.get("/callback-tasks")

    assert response.status_code == 200
    assert response.json()[0]["state"] == "assigned"
    list_tasks.assert_called_once_with(active_only=True)


def test_callback_inbox_post_marks_matching_callback_task(client):
    """A worker→caller inbox message invokes callback matching before delivery."""
    inbox_message = InboxMessage(
        id=1,
        sender_id="deadbeef",
        receiver_id="a1b2c3d4",
        message="results",
        status=MessageStatus.PENDING,
        created_at=datetime.now(timezone.utc),
    )
    with (
        patch("cli_agent_orchestrator.api.main.create_inbox_message", return_value=inbox_message),
        patch(
            "cli_agent_orchestrator.api.main.callback_watchdog.record_callback"
        ) as record_callback,
        patch("cli_agent_orchestrator.api.main.inbox_service.deliver_pending"),
    ):
        response = client.post(
            "/terminals/a1b2c3d4/inbox/messages",
            params={"sender_id": "deadbeef", "message": "results"},
        )

    assert response.status_code == 200
    record_callback.assert_called_once_with("deadbeef", "a1b2c3d4")
