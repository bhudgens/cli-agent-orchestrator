"""Durable metadata used to recover live CAO terminal windows.

CAO keeps terminal rows in SQLite and provider/FIFO state in memory.  A
``cao-server`` restart therefore has two independent recovery problems: the
row may be missing, and the in-memory runtime must be rebuilt.  This module
owns the small, versioned metadata document that bridges the first problem.

The document is intentionally separate from the terminal log and snapshot
files.  A snapshot describes a terminal *after* teardown; a recovery manifest
describes a terminal that is expected to remain live.  It is written by the
normal terminal lifecycle and is removed only after a supported terminal
delete succeeds.

The tmux backend mirrors the identity fields into per-window ``@cao-*`` user
options.  The file is the CAO-owned fallback (and carries fields too large or
too free-form for tmux options), while the tmux copy makes a live window
self-describing even if a manifest file was lost.  Neither store is treated
as authority on its own when the two disagree: callers must resolve that as
an ambiguity and leave the window untouched.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from cli_agent_orchestrator.constants import (
    SESSION_PREFIX,
    TERMINAL_RECOVERY_DIR,
)
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.utils.atomic_file import locked_atomic_delete, locked_atomic_write

logger = logging.getLogger(__name__)

RECOVERY_MANIFEST_VERSION = 1
RECOVERY_MANIFEST_SUFFIX = ".recovery.json"
TMUX_RECOVERY_OPTION_PREFIX = "@cao-recovery-"

_TERMINAL_ID_RE = re.compile(r"^[a-f0-9]{8}$")
_TMUX_OPTION_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Recovery policy is mirrored into tmux as JSON strings.  A present ``null``
# means the original terminal explicitly had no restriction/group/metadata;
# an absent key means the independent source cannot safely reconstruct that
# part of the row and therefore cannot be used for tmux-only adoption.
_JSON_FIELDS = ("allowed_tools", "group", "metadata")
_TMUX_SCALAR_FIELDS = (
    "version",
    "terminal_id",
    "tmux_session",
    "tmux_window",
    "provider",
    "agent_profile",
    "working_directory",
    "caller_id",
    "engine",
    "shell_command",
)


def _terminal_manifest_path(terminal_id: str) -> Path:
    """Return the manifest path after applying the terminal-id path fence."""
    if not isinstance(terminal_id, str) or not _TERMINAL_ID_RE.fullmatch(terminal_id):
        raise ValueError(
            "invalid terminal_id for recovery manifest; expected an 8-character lowercase hex id"
        )
    return Path(str(TERMINAL_RECOVERY_DIR)) / f"{terminal_id}{RECOVERY_MANIFEST_SUFFIX}"


def _as_optional_string(value: Any, field: str) -> Optional[str]:
    """Normalize optional scalar fields without accepting surprising values."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"recovery field {field!r} must be a string or null")
    if len(value) > 4096:
        raise ValueError(f"recovery field {field!r} is too long")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"recovery field {field!r} contains control characters")
    return value


def validate_recovery_metadata(value: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate and normalize one recovery manifest.

    This is deliberately stricter than the free-form terminal ``metadata``
    column.  Auto-adoption is allowed only when the identity needed to bind a
    row to a live window is complete and internally consistent.
    """
    if not isinstance(value, Mapping):
        raise ValueError("recovery metadata must be a mapping")

    version = value.get("version", RECOVERY_MANIFEST_VERSION)
    if version != RECOVERY_MANIFEST_VERSION:
        raise ValueError(f"unsupported recovery metadata version: {version!r}")

    normalized: Dict[str, Any] = dict(value)
    normalized["version"] = RECOVERY_MANIFEST_VERSION

    terminal_id = normalized.get("terminal_id")
    if not isinstance(terminal_id, str) or not _TERMINAL_ID_RE.fullmatch(terminal_id):
        raise ValueError("recovery metadata has an invalid terminal_id")

    for field in ("tmux_session", "tmux_window"):
        candidate = normalized.get(field)
        if not isinstance(candidate, str) or not candidate:
            raise ValueError(f"recovery metadata has no {field}")
        # Import lazily to keep this module usable from low-level startup code
        # without introducing another import cycle through terminal_service.
        from cli_agent_orchestrator.utils.terminal import validate_tmux_name

        validate_tmux_name(candidate, field)

    if not str(normalized["tmux_session"]).startswith(SESSION_PREFIX):
        raise ValueError("recovery metadata tmux_session is not a CAO session")

    provider = normalized.get("provider")
    if isinstance(provider, ProviderType):
        provider = provider.value
    if not isinstance(provider, str) or provider not in {p.value for p in ProviderType}:
        raise ValueError(f"recovery metadata has an unsupported provider: {provider!r}")
    normalized["provider"] = provider

    profile = normalized.get("agent_profile")
    if not isinstance(profile, str) or not profile:
        raise ValueError("recovery metadata has no agent_profile")
    if len(profile) > 256 or any(ord(char) < 0x20 or ord(char) == 0x7F for char in profile):
        raise ValueError("recovery metadata agent_profile is invalid")

    working_directory = normalized.get("working_directory")
    if not isinstance(working_directory, str) or not working_directory:
        raise ValueError("recovery metadata has no working_directory")

    caller_id = _as_optional_string(normalized.get("caller_id"), "caller_id")
    normalized["caller_id"] = caller_id

    engine = _as_optional_string(normalized.get("engine"), "engine")
    if engine not in (None, "v2", "kas"):
        raise ValueError(f"recovery metadata has an unsupported engine: {engine!r}")
    normalized["engine"] = engine
    normalized["shell_command"] = _as_optional_string(
        normalized.get("shell_command"), "shell_command"
    )
    normalized["model"] = _as_optional_string(normalized.get("model"), "model")

    for field in _JSON_FIELDS:
        candidate = normalized.get(field)
        if candidate is not None:
            expected = list if field in ("allowed_tools", "group") else dict
            if not isinstance(candidate, expected):
                raise ValueError(f"recovery metadata field {field!r} has the wrong type")
            # Confirm the object is JSON serializable before it can be written
            # to either SQLite or a manifest.  This also rejects non-string
            # list members in the two fields whose DB schema expects strings.
            if field in ("allowed_tools", "group") and not all(
                isinstance(item, str) for item in candidate
            ):
                raise ValueError(f"recovery metadata field {field!r} must contain strings")
            json.dumps(candidate)

    return normalized


def build_recovery_metadata(metadata: Mapping[str, Any]) -> Dict[str, Any]:
    """Build a manifest from a terminal-row-shaped mapping.

    ``get_terminal_metadata`` uses ``id``/``tmux_session``/``tmux_window``;
    adoption request bodies use ``terminal_id``.  Accept both spellings at
    this boundary so the persistence code stays independent of either API
    representation.
    """
    record: Dict[str, Any] = {
        "version": RECOVERY_MANIFEST_VERSION,
        "terminal_id": metadata.get("terminal_id", metadata.get("id")),
        "tmux_session": metadata.get("tmux_session", metadata.get("session_name")),
        "tmux_window": metadata.get("tmux_window", metadata.get("window_name")),
        "provider": metadata.get("provider"),
        "agent_profile": metadata.get("agent_profile"),
        "working_directory": metadata.get("working_directory"),
        "allowed_tools": metadata.get("allowed_tools"),
        "shell_command": metadata.get("shell_command"),
        "caller_id": metadata.get("caller_id"),
        "engine": metadata.get("engine"),
        "group": metadata.get("group"),
        "metadata": metadata.get("metadata"),
        "model": metadata.get("model"),
    }
    return validate_recovery_metadata(record)


def recovery_policy_is_complete(metadata: Mapping[str, Any]) -> bool:
    """Return whether recovery metadata carries every policy field.

    ``None`` is a valid, explicit unrestricted/opted-out value when the key is
    present.  Only a missing key is unknown.  This distinction is essential for
    tmux-only recovery: turning an unknown ``allowed_tools`` value into ``None``
    would silently grant unrestricted discovery tools.
    """
    return all(field in metadata for field in _JSON_FIELDS)


def write_recovery_manifest(metadata: Mapping[str, Any]) -> Dict[str, Any]:
    """Atomically write one validated recovery manifest and return its record."""
    record = build_recovery_metadata(metadata)
    path = _terminal_manifest_path(record["terminal_id"])
    content = json.dumps(record, indent=2, sort_keys=True) + "\n"
    locked_atomic_write(path, content)
    return record


def read_recovery_manifest(terminal_id: str) -> Optional[Dict[str, Any]]:
    """Read one manifest, treating missing/corrupt files as no candidate."""
    path = _terminal_manifest_path(terminal_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return validate_recovery_metadata(raw)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        # Do not include manifest content in logs: consumer ``metadata`` may
        # contain prompt-adjacent or provider-sensitive values.
        logger.warning("Ignoring invalid recovery manifest for terminal %s: %s", terminal_id, exc)
        return None


def list_recovery_manifests() -> list[Dict[str, Any]]:
    """Return all valid manifests in deterministic order.

    Invalid files are ignored rather than allowing one damaged manifest to
    prevent recovery of unrelated windows.  The filename is also checked
    against the document's terminal id to prevent accidental cross-binding.
    """
    try:
        paths = sorted(TERMINAL_RECOVERY_DIR.glob(f"*{RECOVERY_MANIFEST_SUFFIX}"))
    except OSError as exc:
        logger.warning("Unable to list terminal recovery manifests: %s", exc)
        return []

    manifests: list[Dict[str, Any]] = []
    for path in paths:
        filename_id = path.name[: -len(RECOVERY_MANIFEST_SUFFIX)]
        if not _TERMINAL_ID_RE.fullmatch(filename_id):
            logger.warning("Ignoring recovery manifest with invalid filename: %s", path.name)
            continue
        manifest = read_recovery_manifest(filename_id)
        if manifest and manifest["terminal_id"] == filename_id:
            manifests.append(manifest)
    return manifests


def delete_recovery_manifest(terminal_id: str) -> bool:
    """Delete a manifest after a supported terminal teardown.

    Missing files are success: older terminals predate restart metadata, and
    teardown must remain idempotent for them.
    """
    path = _terminal_manifest_path(terminal_id)
    try:
        locked_atomic_delete(path, must_exist=False)
    except OSError as exc:
        logger.warning("Failed to delete recovery manifest for terminal %s: %s", terminal_id, exc)
        return False
    return True


def tmux_recovery_metadata(metadata: Mapping[str, Any]) -> Dict[str, str]:
    """Encode recovery identity and policy into tmux user options.

    Tmux options are strings, so list/dict policy values use compact JSON.  All
    three policy keys are always emitted, including ``null`` for an explicit
    unrestricted/opted-out value; old or hand-written options that omit one of
    them remain distinguishable as incomplete by ``recovery_metadata_from_tmux``.
    """
    record = build_recovery_metadata(metadata)
    values = {
        field: str(record[field])
        for field in _TMUX_SCALAR_FIELDS
        if record.get(field) not in (None, "")
    }
    values.update(
        {
            field: json.dumps(record[field], separators=(",", ":"), sort_keys=True)
            for field in _JSON_FIELDS
        }
    )
    return values


def recovery_metadata_from_tmux(values: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Decode a tmux option mapping into a validated recovery record.

    Tmux options carry scalar identity fields plus JSON-encoded policy.  A
    policy key with JSON ``null`` is explicit; a missing policy key remains
    absent so callers can refuse unsafe tmux-only recovery while still merging
    the known identity fields with a complete manifest.
    """
    if not values:
        return None

    raw: Dict[str, Any] = {key: values.get(key) for key in _TMUX_SCALAR_FIELDS if key in values}
    if raw.get("version") is not None:
        try:
            raw["version"] = int(raw["version"])
        except (TypeError, ValueError):
            return None

    for field in _JSON_FIELDS:
        if field not in values:
            continue
        encoded = values[field]
        if not isinstance(encoded, str):
            return None
        try:
            raw[field] = json.loads(encoded)
        except (TypeError, ValueError):
            return None

    # Validate exactly as a file-backed candidate; incomplete identity is
    # rejected, while an incomplete policy is retained as an explicit signal
    # for the resolver rather than collapsed into unrestricted ``None``.
    try:
        return validate_recovery_metadata(raw)
    except (TypeError, ValueError):
        return None


def merge_recovery_metadata(
    primary: Mapping[str, Any], secondary: Mapping[str, Any]
) -> Optional[Dict[str, Any]]:
    """Merge two copies only when overlapping identity fields agree.

    ``primary`` is normally a manifest and ``secondary`` is the tmux copy.
    User metadata/list fields may be absent from tmux and are filled from the
    primary.  When both copies contain a JSON policy field, including an
    explicit ``null``, a disagreement is a hard ambiguity.
    """
    merged = dict(primary)
    for key, value in secondary.items():
        if key in _JSON_FIELDS:
            if key in primary:
                if primary[key] != value:
                    return None
            else:
                merged[key] = value
            continue
        if value in (None, ""):
            continue
        existing = merged.get(key)
        if existing not in (None, "") and existing != value:
            return None
        merged[key] = value
    try:
        return validate_recovery_metadata(merged)
    except (TypeError, ValueError):
        return None


def is_tmux_option_key_safe(key: str) -> bool:
    """Return whether a generated recovery key is safe to pass to tmux."""
    return bool(_TMUX_OPTION_KEY_RE.fullmatch(key))
