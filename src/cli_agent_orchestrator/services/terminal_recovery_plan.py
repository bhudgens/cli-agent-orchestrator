"""Expiring, exact-evidence bulk recovery plans; no terminal input is sent."""

import hashlib
import json
import secrets
import threading
import time
from collections import Counter
from typing import Any

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import session_service, terminal_service
from cli_agent_orchestrator.services.terminal_recovery import build_recovery_metadata

_lock = threading.Lock()
_plans: dict[str, dict[str, Any]] = {}
PLAN_TTL_SECONDS = 300


class RecoveryPlanConflict(ValueError):
    """The preview expired or its underlying evidence changed."""


def _snapshot() -> dict[str, Any]:
    """Read all claims before choosing any winner, including tmux-only claims.

    Pane ids are included so replacing a window under the same human-readable
    name invalidates a preview. No provider, FIFO, or metadata writes occur.
    """
    backend = get_backend()
    sessions = sorted(s["id"] for s in session_service._live_cao_sessions(backend))
    manifests = session_service._recovery_manifests_by_window()
    rows = database.list_terminals_in_sessions(sessions)
    records = [database.get_terminal_metadata(row["id"]) for row in rows]
    entries: list[dict[str, Any]] = []
    seen_rows: set[str] = set()
    for session in sessions:
        windows = backend.list_windows(session)
        counts = Counter(w["name"] for w in windows)
        for window in sorted(windows, key=lambda w: (w["name"], str(w.get("index")))):
            name = window["name"]
            entry: dict[str, Any] = {"session": session, "window": name, "state": "unmanaged"}
            entries.append(entry)
            matches = [
                r
                for r in records
                if r and r["tmux_session"] == session and r["tmux_window"] == name
            ]
            seen_rows.update(r["id"] for r in matches)
            if counts[name] > 1 or len(matches) > 1:
                entry["state"] = "ambiguous"
                continue
            try:
                entry["pane_id"] = backend.get_pane_id(session, name)
                if not isinstance(entry["pane_id"], str) or not entry["pane_id"]:
                    raise ValueError("live pane identity is unavailable")
                candidate, ambiguity = session_service._recovery_candidate_for_window(
                    backend, session, name, manifests
                )
                if ambiguity:
                    entry.update(state="ambiguous", reason=ambiguity)
                    continue
                if matches:
                    record = build_recovery_metadata(matches[0])
                    # The DB is authoritative, but conflicting independent
                    # evidence means this coordinate must not be rebound.
                    if candidate and any(
                        record.get(k) != candidate.get(k)
                        for k in (
                            "terminal_id",
                            "provider",
                            "agent_profile",
                            "caller_id",
                            "allowed_tools",
                            "group",
                            "metadata",
                            "working_directory",
                            "engine",
                        )
                    ):
                        entry.update(
                            state="ambiguous", reason="database and recovery identity disagree"
                        )
                        continue
                    candidate = record
                if candidate:
                    entry.update(state="exact", record=candidate)
            except Exception as exc:
                entry.update(state="error", reason=str(exc))

    # Preflight global ids before ANY mutation. Otherwise two tmux-only
    # windows claiming one id let the first adopt and fail only on the second.
    claims = Counter(e["record"]["terminal_id"] for e in entries if "record" in e)
    for entry in entries:
        record = entry.get("record")
        if not record:
            continue
        existing = database.get_terminal_metadata(record["terminal_id"])
        if claims[record["terminal_id"]] > 1 or (
            existing
            and (
                existing["tmux_session"] != entry["session"]
                or existing["tmux_window"] != entry["window"]
            )
        ):
            entry.update(state="ambiguous", reason="terminal id has multiple coordinate claims")
    stale = sorted(r["id"] for r in records if r and r["id"] not in seen_rows)
    # Full records/manifests participate in the digest, but are never returned
    # in the operator report (consumer metadata can contain private context).
    return {
        "entries": entries,
        "rows": records,
        "manifests": list(manifests.values()),
        "stale": stale,
    }


def _digest(snapshot: dict[str, Any]) -> str:
    # Activity changes are normal while reviewing; policy/identity changes are
    # not. Omit last_active, which is not an adoption identity field.
    stable = dict(snapshot)
    stable["rows"] = [
        {k: v for k, v in row.items() if k != "last_active"} for row in snapshot["rows"]
    ]
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()


def plan_recovery() -> dict[str, Any]:
    """Preview all CAO windows; the opaque token is valid for five minutes."""
    with _lock:
        now = time.monotonic()
        for token in list(_plans):
            if _plans[token]["expires"] <= now:
                del _plans[token]
        if len(_plans) >= 128:
            raise RecoveryPlanConflict("Too many active plans; wait for expiry")
        snapshot = _snapshot()
        token = secrets.token_urlsafe(24)
        _plans[token] = {"expires": now + PLAN_TTL_SECONDS, "digest": _digest(snapshot)}
        return {
            "token": token,
            "expires_in_seconds": PLAN_TTL_SECONDS,
            "windows": [
                {k: v for k, v in entry.items() if k != "record"}
                | ({"terminal_id": entry["record"]["terminal_id"]} if "record" in entry else {})
                for entry in snapshot["entries"]
            ],
            "stale": snapshot["stale"],
            "excluded": ["cao-server-local"],
        }


def apply_recovery(token: str) -> dict[str, Any]:
    """Apply an unchanged preview once, preserving rows and holding all input.

    A partial failure is reported and cached; a new plan is needed to retry.
    There is no rollback that might delete a recovered user's live registry.
    Tokens are process-local by design: restarting the server requires preview
    again rather than applying a plan made against an earlier process.
    """
    with _lock:
        plan = _plans.get(token)
        if not plan or plan["expires"] <= time.monotonic():
            raise RecoveryPlanConflict("Recovery plan expired or unknown; create a new preview")
        if "result" in plan:
            return plan["result"]
        snapshot = _snapshot()
        if _digest(snapshot) != plan["digest"]:
            raise RecoveryPlanConflict("Recovery evidence changed; create a new preview")
        result: dict[str, Any] = {"recovered": [], "errors": [], "held": [], "skipped": []}
        for entry in snapshot["entries"]:
            if entry["state"] != "exact":
                result["skipped"].append(f"{entry['session']}:{entry['window']}")
                continue
            record = entry["record"]
            try:
                terminal_service.adopt_terminal(
                    terminal_id=record["terminal_id"],
                    session_name=entry["session"],
                    window_name=entry["window"],
                    provider=record["provider"],
                    agent_profile=record["agent_profile"],
                    working_directory=record["working_directory"],
                    caller_id=record.get("caller_id"),
                    allowed_tools=record.get("allowed_tools"),
                    engine=record.get("engine"),
                    shell_command=record.get("shell_command"),
                    group=record.get("group"),
                    metadata=record.get("metadata"),
                    hold_inbox=True,
                    expected_pane_id=entry["pane_id"],
                )
                result["recovered"].append(record["terminal_id"])
                result["held"].append(record["terminal_id"])
            except Exception as exc:
                result["errors"].append({"terminal_id": record["terminal_id"], "reason": str(exc)})
        plan["result"] = result
        return result


def restore_registered_terminals() -> dict[str, Any]:
    """Restart only in-memory adapters for exact, already tracked windows.

    Rowless windows require explicit apply. A complete collision preflight
    happens first, so startup cannot choose a winner for duplicate identities.
    This sends no input and neither deletes nor repairs registry coordinates.
    """
    with _lock:
        snapshot = _snapshot()
        registered = {row["id"] for row in snapshot["rows"]}
        result: dict[str, Any] = {"restored": [], "errors": []}
        for entry in snapshot["entries"]:
            record = entry.get("record")
            if entry["state"] != "exact" or not record or record["terminal_id"] not in registered:
                continue
            try:
                terminal_service.adopt_terminal_runtime(record["terminal_id"])
                result["restored"].append(record["terminal_id"])
            except Exception as exc:
                result["errors"].append({"terminal_id": record["terminal_id"], "reason": str(exc)})
        return result
