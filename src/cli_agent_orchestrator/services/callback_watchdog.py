"""Durable watchdog for assignment callbacks.

The watchdog answers one deliberately narrow question: did an assigned worker
send a callback to the terminal that assigned it? It does not infer semantic
completion from model output and it never reassigns work. The ledger is stored
in the normal CAO database so a server restart can resume the same bounded
obligation without repeating a nudge that was already claimed.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.constants import (
    CALLBACK_TASK_TIMEOUT_SECONDS,
    CALLBACK_WATCHDOG_INTERVAL_SECONDS,
    CALLBACK_WATCHDOG_MAX_NUDGES,
    TERMINAL_LOG_DIR,
)
from cli_agent_orchestrator.models.callback import CallbackTaskState
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.status_monitor import status_monitor

logger = logging.getLogger(__name__)
_callback_action_lock = threading.RLock()

DEFAULT_CALLBACK_TIMEOUT_SECONDS = CALLBACK_TASK_TIMEOUT_SECONDS
DEFAULT_CALLBACK_MAX_NUDGES = CALLBACK_WATCHDOG_MAX_NUDGES


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalize SQLite's sometimes-naive datetime values for comparisons."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _truthy(value: Any) -> bool:
    """Interpret the small set of boolean spellings used in free-form metadata."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def is_human_or_persistent_terminal(metadata: Optional[Dict[str, Any]]) -> bool:
    """Return whether automatic nudging must be suppressed for a terminal.

    Metadata is intentionally consumer-defined. The watchdog therefore uses a
    conservative allowlist of common ownership/role keys and protects explicit
    ``human``, ``manual``, ``persistent``, and ``anchor`` labels. An unknown
    metadata shape is not treated as protected; only positive signals suppress
    the one-shot nudge.
    """
    if not isinstance(metadata, dict):
        return False

    protected_words = ("human", "manual", "persistent", "anchor")
    protected_key_words = (
        "human",
        "manual",
        "persistent",
        "anchor",
        "operator",
        "ownership",
    )
    mappings = [metadata]
    nested = metadata.get("metadata")
    if isinstance(nested, dict):
        mappings.append(nested)

    for mapping in mappings:
        if mapping.get("cao_recovery_hold") is True:
            return True
        for key, value in mapping.items():
            normalized_key = str(key).strip().lower().replace("-", "_")
            if any(word in normalized_key for word in protected_key_words):
                if _truthy(value) or (
                    isinstance(value, str)
                    and any(word in value.strip().lower() for word in protected_words)
                ):
                    return True

            # These fields commonly carry a role or lifecycle label rather than
            # a boolean flag (for example ``profile: anchor-dev``).
            if normalized_key in {
                "profile",
                "agent_profile",
                "role",
                "kind",
                "lifecycle",
                "disposition",
                "group",
            }:
                values = value if isinstance(value, (list, tuple, set)) else [value]
                for item in values:
                    if isinstance(item, str):
                        lowered = item.strip().lower()
                        if any(word in lowered for word in protected_words):
                            return True

    group = metadata.get("group")
    if isinstance(group, (list, tuple, set)):
        if any(
            isinstance(item, str) and any(word in item.strip().lower() for word in protected_words)
            for item in group
        ):
            return True
    return False


@dataclass(frozen=True)
class WorkerInspection:
    """Best-effort evidence gathered for one worker evaluation."""

    terminal_exists: bool
    session_exists: Optional[bool] = None
    window_exists: Optional[bool] = None
    status: Optional[TerminalStatus] = None
    output_fresh: Optional[bool] = None
    reason: str = ""
    metadata: Optional[Dict[str, Any]] = None

    @property
    def definitively_missing(self) -> bool:
        """Whether a missing row/session/window is positively established."""
        return (
            not self.terminal_exists or self.session_exists is False or self.window_exists is False
        )

    @property
    def deliverable(self) -> bool:
        """Whether all runtime identity checks needed for a nudge are positive."""
        return self.terminal_exists and self.session_exists is True and self.window_exists is True


class CallbackWatchdog:
    """Ledger facade and bounded evaluator for assignment callbacks."""

    def __init__(
        self,
        *,
        interval_seconds: float = CALLBACK_WATCHDOG_INTERVAL_SECONDS,
        stale_after_seconds: float = DEFAULT_CALLBACK_TIMEOUT_SECONDS,
        max_nudges: int = DEFAULT_CALLBACK_MAX_NUDGES,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        inspector: Optional[Callable[[Dict[str, Any]], WorkerInspection]] = None,
        nudge_sender: Optional[Callable[[Dict[str, Any], str], Any]] = None,
    ) -> None:
        self.interval_seconds = max(float(interval_seconds), 0.1)
        self.stale_after_seconds = max(float(stale_after_seconds), 0.0)
        self.max_nudges = max(int(max_nudges), 0)
        self._now_fn = now_fn
        self._inspector = inspector
        self._nudge_sender = nudge_sender

    def register_assignment(
        self,
        worker_terminal_id: str,
        caller_terminal_id: str,
        *,
        created_at: Optional[datetime] = None,
        deadline_at: Optional[datetime] = None,
        stale_after_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Persist the callback obligation for a newly created local worker."""
        return database.create_callback_task(
            worker_terminal_id,
            caller_terminal_id,
            created_at=created_at,
            deadline_at=deadline_at,
            stale_after_at=stale_after_at,
            stale_after_seconds=self.stale_after_seconds,
        )

    def record_callback(self, worker_terminal_id: str, caller_terminal_id: str) -> bool:
        """Mark the newest matching task complete when its callback arrives."""
        with _callback_action_lock:
            return database.mark_callback_received(worker_terminal_id, caller_terminal_id)

    def list_tasks(self, *, active_only: bool = True) -> List[Dict[str, Any]]:
        """Return ledger rows for the API and CLI visibility surfaces."""
        return database.list_callback_tasks(active_only=active_only)

    def inspect_worker(self, task: Dict[str, Any]) -> WorkerInspection:
        """Inspect durable terminal metadata and live backend state conservatively."""
        worker_id = str(task["worker_terminal_id"])
        metadata = database.get_terminal_metadata(worker_id)
        if not metadata:
            return WorkerInspection(
                terminal_exists=False,
                reason="terminal row is missing",
            )

        status: Optional[TerminalStatus]
        try:
            status_value = status_monitor.get_status(worker_id)
            if isinstance(status_value, TerminalStatus):
                status = status_value
            else:
                try:
                    status = TerminalStatus(str(status_value))
                except ValueError:
                    status = None
        except Exception as exc:
            status = None
            status_error = f"status lookup failed: {type(exc).__name__}"
        else:
            status_error = ""

        session_name = metadata.get("tmux_session")
        window_name = metadata.get("tmux_window")
        session_exists: Optional[bool] = None
        window_exists: Optional[bool] = None
        runtime_errors: List[str] = []
        try:
            backend = get_backend()
            session_checker = getattr(backend, "session_exists_strict", None)
            if not callable(session_checker):
                session_checker = getattr(backend, "session_exists", None)
            if callable(session_checker) and session_name:
                session_exists = session_checker(session_name)

            if session_exists is True:
                list_windows = getattr(backend, "list_windows", None)
                if callable(list_windows):
                    windows = list_windows(session_name)
                    if windows:
                        window_exists = any(
                            str(window.get("name")) == str(window_name)
                            for window in windows
                            if isinstance(window, dict)
                        )
                    else:
                        # A backend that uses socket/event workspaces may not
                        # expose windows; tmux-like backends can answer with an
                        # empty list, which is a definitive missing-window read.
                        supports_event_inbox = getattr(backend, "supports_event_inbox", None)
                        event_backend = (
                            callable(supports_event_inbox) and supports_event_inbox() is True
                        )
                        window_exists = None if event_backend else False
        except Exception as exc:
            runtime_errors.append(f"runtime lookup failed: {type(exc).__name__}")
            # Unknown backend errors are fail-closed for action: they do not
            # prove the worker is dead and therefore must not trigger a nudge.
            session_exists = None
            window_exists = None

        output_fresh = self._output_is_fresh(task, metadata)
        reasons = [part for part in (status_error, *runtime_errors) if part]
        if not reasons:
            reasons.append("worker runtime inspected")
        if session_exists is False:
            reasons.append("session is missing")
        if window_exists is False:
            reasons.append("window is missing")
        if output_fresh is True:
            reasons.append("recent output or activity observed")
        elif output_fresh is False:
            reasons.append("no output newer than assignment observed")

        return WorkerInspection(
            terminal_exists=True,
            session_exists=session_exists,
            window_exists=window_exists,
            status=status,
            output_fresh=output_fresh,
            reason="; ".join(reasons),
            metadata=metadata,
        )

    @staticmethod
    def _output_is_fresh(task: Dict[str, Any], metadata: Dict[str, Any]) -> Optional[bool]:
        """Compare known activity timestamps without reading terminal output."""
        created_at = _as_utc(task.get("created_at"))
        if created_at is None:
            return None

        observed: List[datetime] = []
        last_active = _as_utc(metadata.get("last_active"))
        if last_active is not None:
            observed.append(last_active)

        try:
            log_path = Path(TERMINAL_LOG_DIR) / f"{task['worker_terminal_id']}.log"
            observed.append(datetime.fromtimestamp(log_path.stat().st_mtime, tz=timezone.utc))
        except (FileNotFoundError, OSError, KeyError, TypeError, ValueError):
            pass

        return max(observed) >= created_at if observed else None

    def _send_nudge(self, task: Dict[str, Any], message: str) -> Any:
        """Send the single reminder through the normal terminal input seam."""
        if self._nudge_sender is not None:
            return self._nudge_sender(task, message)

        # Imported lazily: terminal_service imports much of the provider stack,
        # while this module is imported by terminal_service during assignment.
        from cli_agent_orchestrator.providers.manager import provider_manager
        from cli_agent_orchestrator.services.terminal_service import send_input

        metadata = database.get_terminal_metadata(task["worker_terminal_id"])
        provider = provider_manager.get_provider(task["worker_terminal_id"])
        if not metadata or is_human_or_persistent_terminal(metadata) or provider is None:
            raise RuntimeError("Worker input is protected or provider registration is missing")
        if getattr(provider, "supports_screen_detection", False) is not True:
            raise RuntimeError("Provider cannot confirm idle from a live screen")
        output = get_backend().get_history(
            metadata["tmux_session"], metadata["tmux_window"], tail_lines=80
        )
        if provider.get_status_from_screen(output.splitlines()) not in {
            TerminalStatus.IDLE,
            TerminalStatus.COMPLETED,
        }:
            raise RuntimeError("Worker is no longer idle")
        if provider.extract_current_composer(output):
            raise RuntimeError("Worker has unsubmitted input; refusing to overwrite it")

        return send_input(
            task["worker_terminal_id"],
            message,
            sender_id=task["caller_terminal_id"],
            orchestration_type=OrchestrationType.SEND_MESSAGE,
        )

    @staticmethod
    def _nudge_message(task: Dict[str, Any]) -> str:
        """Build a bounded reminder that carries no task prompt or model claim."""
        return (
            "Callback watchdog reminder: send your final results to supervisor "
            f"terminal {task['caller_terminal_id']} using send_message. "
            "This is the only automated reminder; do not treat it as task completion."
        )

    def _observation_changes(
        self,
        inspection: WorkerInspection,
        now: datetime,
        *,
        state: Optional[CallbackTaskState] = None,
        last_error: Optional[str] = None,
    ) -> Dict[str, Any]:
        changes: Dict[str, Any] = {
            "last_checked_at": now,
            "last_observed_status": inspection.status.value if inspection.status else None,
            "last_inspection": inspection.reason,
            "output_fresh": inspection.output_fresh,
        }
        if state is not None:
            changes["state"] = state.value
        if last_error is not None:
            changes["last_error"] = last_error
        return changes

    def evaluate_once(self, *, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Evaluate active tasks once and return a compact evaluation report."""
        observed_at = _as_utc(now or self._now_fn())
        assert observed_at is not None  # ``now_fn`` is typed to return datetime.
        outcomes: List[Dict[str, Any]] = []

        for task in database.list_callback_tasks(active_only=True):
            task_id = task["id"]
            try:
                inspection = (
                    self._inspector(task)
                    if self._inspector is not None
                    else self.inspect_worker(task)
                )
            except Exception as exc:
                logger.warning("Callback watchdog inspection failed for %s: %s", task_id, exc)
                inspection = WorkerInspection(
                    terminal_exists=True,
                    reason=f"inspection failed: {type(exc).__name__}",
                )

            if inspection.definitively_missing:
                updated = database.update_callback_task(
                    task_id,
                    **self._observation_changes(
                        inspection,
                        observed_at,
                        state=CallbackTaskState.NOT_DELIVERABLE,
                        last_error=inspection.reason or "worker is not deliverable",
                    ),
                )
                outcomes.append(
                    {
                        "task_id": task_id,
                        "state": CallbackTaskState.NOT_DELIVERABLE.value,
                        "nudged": False,
                        "task": updated,
                    }
                )
                continue

            if inspection.status == TerminalStatus.ERROR:
                updated = database.update_callback_task(
                    task_id,
                    **self._observation_changes(
                        inspection,
                        observed_at,
                        state=CallbackTaskState.FAILED,
                        last_error=inspection.reason or "worker reported an error",
                    ),
                )
                outcomes.append(
                    {
                        "task_id": task_id,
                        "state": CallbackTaskState.FAILED.value,
                        "nudged": False,
                        "task": updated,
                    }
                )
                continue

            deadlines = [
                value
                for value in (
                    _as_utc(task.get("deadline_at")),
                    _as_utc(task.get("stale_after_at")),
                )
                if value is not None
            ]
            deadline = min(deadlines) if deadlines else None
            due = deadline is not None and observed_at >= deadline

            if not due:
                next_state = (
                    CallbackTaskState.PICKED_UP
                    if inspection.status == TerminalStatus.PROCESSING
                    else None
                )
                updated = database.update_callback_task(
                    task_id,
                    **self._observation_changes(inspection, observed_at, state=next_state),
                )
                outcomes.append(
                    {
                        "task_id": task_id,
                        "state": (updated or task).get("state"),
                        "nudged": False,
                        "task": updated,
                    }
                )
                continue

            # A callback can arrive between the active-task read and this
            # deadline evaluation. Re-read before transitioning to overdue;
            # the DAL also guards the state update atomically for the remaining
            # race with the inbox endpoint.
            latest_task = database.get_callback_task(task_id)
            if latest_task is None or latest_task["state"] not in {
                state.value
                for state in (
                    CallbackTaskState.ASSIGNED,
                    CallbackTaskState.PICKED_UP,
                    CallbackTaskState.OVERDUE,
                )
            }:
                outcomes.append(
                    {
                        "task_id": task_id,
                        "state": latest_task["state"] if latest_task else None,
                        "nudged": False,
                        "task": latest_task,
                    }
                )
                continue
            task = latest_task

            # Deadline reached with a live, non-error worker. Mark overdue
            # before trying the reminder; callback arrival can then atomically
            # close it while this process is sending the nudge.
            updated = database.update_callback_task(
                task_id,
                **self._observation_changes(
                    inspection,
                    observed_at,
                    state=CallbackTaskState.OVERDUE,
                ),
            )

            suppression_reason: Optional[str] = None
            if is_human_or_persistent_terminal(inspection.metadata):
                suppression_reason = (
                    "automatic nudge suppressed for human/persistent anchor terminal"
                )
            elif inspection.status == TerminalStatus.WAITING_USER_ANSWER:
                suppression_reason = (
                    "automatic nudge suppressed while terminal awaits a user answer"
                )
            elif not inspection.deliverable:
                suppression_reason = (
                    "automatic nudge suppressed because runtime identity is unknown"
                )
            elif inspection.status not in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}:
                suppression_reason = "automatic nudge suppressed because worker status is not ready"
            elif self.max_nudges <= 0:
                suppression_reason = "automatic nudge disabled by configuration"

            nudged = False
            if suppression_reason is None:
                claimed = database.claim_callback_nudge(
                    task_id,
                    nudged_at=observed_at,
                    max_nudges=self.max_nudges,
                )
                if claimed is not None:
                    nudged = True
                    try:
                        # Do not send a stale reminder if the callback endpoint
                        # won the race after the nudge slot was claimed.
                        with _callback_action_lock:
                            current = database.get_callback_task(task_id)
                            if current is not None and current["state"] != (
                                CallbackTaskState.CALLBACK_RECEIVED.value
                            ):
                                self._send_nudge(claimed, self._nudge_message(claimed))
                            else:
                                nudged = False
                    except Exception as exc:
                        # The claim is intentionally not rolled back. It is an
                        # at-most-once reminder, so a transient sender failure
                        # cannot become spam after every watchdog restart.
                        logger.warning("Callback watchdog nudge failed for %s: %s", task_id, exc)
                        database.update_callback_task(
                            task_id,
                            last_error=f"nudge delivery failed: {type(exc).__name__}",
                        )
            else:
                database.update_callback_task(task_id, last_error=suppression_reason)

            final_task = database.get_callback_task(task_id)
            outcomes.append(
                {
                    "task_id": task_id,
                    "state": (final_task or updated or task).get(
                        "state", CallbackTaskState.OVERDUE.value
                    ),
                    "nudged": nudged,
                    "task": final_task or updated,
                }
            )

        return outcomes

    async def run(self) -> None:
        """Run the low-frequency, cancellation-safe watchdog loop."""
        logger.info("Callback watchdog started")
        while True:
            # Wait for the cadence before the first sweep. Startup already has
            # several reconciliation jobs; delaying this low-priority safety
            # net avoids a database/backend burst while those jobs settle.
            await asyncio.sleep(self.interval_seconds)
            try:
                from cli_agent_orchestrator.services.session_watchdog import scan_sessions

                await asyncio.to_thread(scan_sessions)
                await asyncio.to_thread(self.evaluate_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One malformed row or backend outage must not kill future
                # evaluations; the next loop can recover after the outage.
                logger.exception("Callback watchdog evaluation failed")


callback_watchdog = CallbackWatchdog()
