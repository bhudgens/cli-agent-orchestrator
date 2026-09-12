"""API-to-database recovery and visibility checks with a fake terminal backend."""

from test.services.test_watchdog_recovery_integration import record, runtime

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services.session_watchdog import scan_sessions
from cli_agent_orchestrator.services.terminal_recovery import write_recovery_manifest


def test_plan_apply_and_watchdog_api(client, runtime):
    _, _, cwd = runtime
    write_recovery_manifest(record(cwd))
    response = client.post("/terminals/recovery/plan")
    assert response.status_code == 200
    assert database.list_terminal_ids() == []
    result = client.post("/terminals/recovery/apply", json={"token": response.json()["token"]})
    assert result.status_code == 200
    assert result.json()["held"] == ["aabbccdd"]
    scan_sessions()
    checks = client.get("/watchdog")
    assert checks.status_code == 200
    assert any(check["state"] == "held" for check in checks.json())


def test_unknown_recovery_token_is_conflict(client, runtime):
    response = client.post("/terminals/recovery/apply", json={"token": "unknown"})
    assert response.status_code == 409


def test_recovery_requires_admin(client, runtime, monkeypatch):
    monkeypatch.setenv("AUTH0_DOMAIN", "test.local")
    monkeypatch.setenv("AUTH0_AUDIENCE", "cao://test")
    response = client.post("/terminals/recovery/plan")
    assert response.status_code in (401, 403)
