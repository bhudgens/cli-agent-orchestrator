"""Unit tests for durable terminal recovery metadata."""

import pytest

from cli_agent_orchestrator.services.terminal_recovery import (
    build_recovery_metadata,
    merge_recovery_metadata,
    recovery_metadata_from_tmux,
    recovery_policy_is_complete,
    tmux_recovery_metadata,
)


def _record(**overrides):
    record = {
        "terminal_id": "c8397e50",
        "tmux_session": "cao-live",
        "tmux_window": "anchor-dev-new",
        "provider": "codex",
        "agent_profile": "developer",
        "working_directory": "/workspace",
        "caller_id": "5a88b9bb",
    }
    record.update(overrides)
    return record


def test_recovery_metadata_round_trips_through_tmux_copy():
    record = build_recovery_metadata(_record(metadata={"role": "anchor-dev"}))

    tmux_record = recovery_metadata_from_tmux(tmux_recovery_metadata(record))

    assert tmux_record is not None
    assert tmux_record["terminal_id"] == "c8397e50"
    assert tmux_record["tmux_session"] == "cao-live"
    assert tmux_record["tmux_window"] == "anchor-dev-new"
    assert tmux_record["provider"] == "codex"
    assert tmux_record["agent_profile"] == "developer"
    assert tmux_record["working_directory"] == "/workspace"
    assert tmux_record["caller_id"] == "5a88b9bb"
    assert tmux_record["allowed_tools"] is None
    assert tmux_record["group"] is None
    assert tmux_record["metadata"] == {"role": "anchor-dev"}
    assert recovery_policy_is_complete(tmux_record)


def test_restricted_policy_round_trips_through_tmux_only_copy():
    record = build_recovery_metadata(
        _record(
            allowed_tools=["read", "discovery"],
            group=["sailpoint", "project-a"],
            metadata={"role": "anchor-dev"},
        )
    )

    tmux_record = recovery_metadata_from_tmux(tmux_recovery_metadata(record))

    assert tmux_record is not None
    assert tmux_record["allowed_tools"] == ["read", "discovery"]
    assert tmux_record["group"] == ["sailpoint", "project-a"]
    assert tmux_record["metadata"] == {"role": "anchor-dev"}
    assert recovery_policy_is_complete(tmux_record)


def test_missing_tmux_policy_is_not_treated_as_explicit_unrestricted_access():
    values = tmux_recovery_metadata(build_recovery_metadata(_record()))
    values.pop("allowed_tools")

    tmux_record = recovery_metadata_from_tmux(values)

    assert tmux_record is not None
    assert "allowed_tools" not in tmux_record
    assert not recovery_policy_is_complete(tmux_record)


def test_recovery_metadata_merge_rejects_identity_disagreement():
    manifest = build_recovery_metadata(_record())
    tmux_copy = recovery_metadata_from_tmux(
        tmux_recovery_metadata(_record(agent_profile="reviewer"))
    )

    assert tmux_copy is not None
    assert merge_recovery_metadata(manifest, tmux_copy) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allowed_tools", ["read"]),
        ("group", ["project-a"]),
        ("metadata", {"role": "anchor-dev"}),
    ],
)
@pytest.mark.parametrize("null_source", ["manifest", "tmux"])
def test_recovery_metadata_merge_rejects_explicit_null_value_conflicts(field, value, null_source):
    policy = {
        "allowed_tools": ["read", "discovery"],
        "group": ["project-a", "team-a"],
        "metadata": {"role": "anchor-dev", "consumer": "sailpoint"},
    }
    manifest_policy = dict(policy)
    tmux_policy = dict(policy)
    if null_source == "manifest":
        manifest_policy[field] = None
        tmux_policy[field] = value
    else:
        manifest_policy[field] = value
        tmux_policy[field] = None

    manifest = build_recovery_metadata(_record(**manifest_policy))
    tmux_copy = build_recovery_metadata(_record(**tmux_policy))

    assert merge_recovery_metadata(manifest, tmux_copy) is None


@pytest.mark.parametrize("field", ["allowed_tools", "group", "metadata"])
def test_recovery_metadata_merge_accepts_same_explicit_null(field):
    policy = {
        "allowed_tools": ["read"],
        "group": ["project-a"],
        "metadata": {"role": "anchor-dev"},
    }
    policy[field] = None
    manifest = build_recovery_metadata(_record(**policy))
    tmux_copy = build_recovery_metadata(_record(**policy))

    merged = merge_recovery_metadata(manifest, tmux_copy)

    assert merged is not None
    assert merged[field] is None
    assert recovery_policy_is_complete(merged)


def test_recovery_metadata_merge_keeps_manifest_value_when_tmux_field_is_missing():
    manifest = build_recovery_metadata(_record(allowed_tools=["read"]))
    tmux_copy = build_recovery_metadata(_record(allowed_tools=["read"]))
    tmux_copy.pop("allowed_tools")

    merged = merge_recovery_metadata(manifest, tmux_copy)

    assert merged is not None
    assert merged["allowed_tools"] == ["read"]
    assert recovery_policy_is_complete(merged)


def test_recovery_metadata_merge_keeps_explicit_null_when_tmux_field_is_missing():
    manifest = build_recovery_metadata(_record(allowed_tools=None))
    tmux_copy = build_recovery_metadata(_record(allowed_tools=None))
    tmux_copy.pop("allowed_tools")

    merged = merge_recovery_metadata(manifest, tmux_copy)

    assert merged is not None
    assert "allowed_tools" in merged
    assert merged["allowed_tools"] is None
    assert recovery_policy_is_complete(merged)


def test_recovery_metadata_merge_accepts_explicit_null_from_only_source():
    manifest = build_recovery_metadata(_record())
    manifest.pop("allowed_tools")
    tmux_copy = build_recovery_metadata(_record(allowed_tools=None))

    merged = merge_recovery_metadata(manifest, tmux_copy)

    assert merged is not None
    assert "allowed_tools" in merged
    assert merged["allowed_tools"] is None
    assert recovery_policy_is_complete(merged)


def test_incomplete_tmux_metadata_is_not_a_candidate():
    assert (
        recovery_metadata_from_tmux({"terminal_id": "c8397e50", "tmux_session": "cao-live"}) is None
    )


def test_recovery_metadata_rejects_non_cao_session():
    with pytest.raises(ValueError, match="not a CAO session"):
        build_recovery_metadata(_record(tmux_session="user-session"))
