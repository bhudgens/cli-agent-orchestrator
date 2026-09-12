"""Deterministic registry/runtime/mail supervision with bounded delivery retries."""

import logging
from collections import Counter
from datetime import datetime, timezone

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services.callback_watchdog import is_human_or_persistent_terminal
from cli_agent_orchestrator.services.status_monitor import status_monitor

logger = logging.getLogger(__name__)


def scan_sessions() -> list[dict]:
    """Scan all registered and live CAO windows, not just assigned workers.

    Queued mail is itself the actionable nudge. Retry it through InboxService
    only after a direct provider screen check, preserving its ordering/lock.
    Never claim MCP health from an idle screen: only explicit failures are
    classified, and absent protocol evidence is reported as unknown.
    """
    from cli_agent_orchestrator.services import terminal_service
    from cli_agent_orchestrator.services.inbox_service import inbox_service

    backend = get_backend()
    reports = []
    now = datetime.now(timezone.utc).isoformat()

    def emit(key, **fields):
        report = {"key": key, "checked_at": now, **fields}
        database.record_watchdog_check(key, report)
        reports.append(report)
        if report.get("state") in {"unmanaged", "missing", "error", "mcp_error", "delivery_failed"}:
            logger.warning("CAO watchdog: %s", report)

    try:
        sessions = [
            s["id"]
            for s in backend.list_sessions()
            if s["id"].startswith("cao-") and s["id"] != "cao-server-local"
        ]
        live = {}
        for session in sessions:
            for window in backend.list_windows(session):
                live.setdefault((session, window["name"]), []).append(window)
    except Exception as exc:
        emit("runtime", state="error", reason=f"Runtime inventory failed: {exc}")
        return reports

    rows = []
    for terminal_id in database.list_terminal_ids():
        try:
            row = database.get_terminal_metadata(terminal_id)
            if row and row["tmux_session"] != "cao-server-local":
                rows.append(row)
        except Exception as exc:
            emit(terminal_id, state="error", reason=f"Registry record unreadable: {exc}")
    coordinates = Counter((r["tmux_session"], r["tmux_window"]) for r in rows)
    from cli_agent_orchestrator.services.terminal_recovery_plan import _snapshot

    try:
        preflight = _snapshot()
        exact_ids = {
            e["record"]["terminal_id"] for e in preflight["entries"] if e["state"] == "exact"
        }
    except Exception as exc:
        emit("recovery", state="error", reason=f"Recovery preflight failed: {exc}")
        return reports
    for coordinate in live:
        if coordinate not in coordinates:
            emit(
                ":".join(coordinate),
                state="unmanaged",
                reason="Live window has no CAO registration; run terminal recover",
            )

    for row in rows:
        terminal_id = row["id"]
        coordinate = (row["tmux_session"], row["tmux_window"])
        if coordinate not in live:
            emit(
                terminal_id,
                state="missing",
                reason="Registered window not present; record retained",
            )
            continue
        if coordinates[coordinate] != 1 or len(live[coordinate]) != 1:
            emit(terminal_id, state="error", reason="Ambiguous live/registered window identity")
            continue
        if terminal_id not in exact_ids:
            emit(
                terminal_id,
                state="error",
                reason="Recovery identity is conflicting or incomplete; no automatic input",
            )
            continue
        if is_human_or_persistent_terminal(row):
            emit(
                terminal_id,
                state="held",
                mcp="unknown",
                reason="Human, persistent, or recovery inbox hold",
            )
            continue
        try:
            # Restore only the exact known row; no name guesses, new provider
            # process, or rowless adoption. The helper is idempotent.
            terminal_service.adopt_terminal_runtime(terminal_id)
            provider = provider_manager.get_provider(terminal_id)
            if provider is None:
                raise RuntimeError("Provider registration could not be restored")
            output = backend.get_history(*coordinate, tail_lines=80)
            lowered = output.lower()
            mcp_error = any(
                marker in lowered
                for marker in (
                    "mcp startup failed",
                    "mcp server failed",
                    "mcp client failed",
                    "transport closed",
                )
            )
            if mcp_error:
                emit(
                    terminal_id,
                    state="mcp_error",
                    mcp="error_observed",
                    reason="MCP failure visible in recent pane output; operator action required",
                )
                continue
            # Only screen-aware providers can provide a fresh idle verdict;
            # cached IDLE alone does not authorize typing into the pane.
            if getattr(provider, "supports_screen_detection", False) is True:
                observed = provider.get_status_from_screen(output.splitlines())
            else:
                observed = TerminalStatus.UNKNOWN
            messages = database.get_pending_messages(terminal_id, limit=1)
            state = "tracked"
            reason = (
                "No pending mail" if not messages else "Pending mail; worker not confirmed idle"
            )
            if messages and observed in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}:
                if provider.extract_current_composer(output):
                    emit(
                        terminal_id,
                        state="held",
                        mcp="unknown",
                        reason="Unsubmitted input in composer; no automatic paste",
                    )
                    continue
                message = messages[0]
                if database.claim_inbox_watchdog_nudge(message.id):
                    # InboxService rechecks ownership under its delivery lock;
                    # pass the direct observation to avoid a stale cache gate.
                    inbox_service.deliver_pending(terminal_id, observed_status=observed)
                    remaining = database.get_inbox_messages(terminal_id, status=None)
                    sent = next((m for m in remaining if m.id == message.id), None)
                    state = (
                        "mail_nudged"
                        if sent and sent.status.value == "delivered"
                        else "delivery_failed"
                    )
                    reason = "Queued mail delivery retried after registration recovery"
                else:
                    state, reason = (
                        "delivery_failed",
                        "Mail remains pending after the bounded watchdog attempt",
                    )
            emit(
                terminal_id,
                state=state,
                status=observed.value,
                pending_mail=bool(messages),
                mcp="unknown",
                reason=reason,
            )
        except Exception as exc:
            emit(
                terminal_id,
                state="error",
                mcp="unknown",
                reason=f"Registration/delivery recovery failed: {exc}",
            )
    current_keys = {report["key"] for report in reports}
    for previous in database.list_watchdog_checks():
        if previous["key"] not in current_keys and previous["state"] != "resolved":
            database.record_watchdog_check(
                previous["key"], {**previous, "state": "resolved", "checked_at": now}
            )
    return reports
