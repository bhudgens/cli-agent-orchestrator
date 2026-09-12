"""Terminal commands for CLI Agent Orchestrator."""

import json
import os

import click
import requests

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import API_BASE_URL, TERMINAL_LOG_DIR
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.utils.orchestration import _auth_headers
from cli_agent_orchestrator.utils.terminal import sync_backend_from_server


@click.group()
def terminal():
    """Manage CAO terminals."""


@terminal.command("restore")
@click.argument("terminal_id")
def restore(terminal_id: str):
    """Restore a deleted terminal from its snapshot.

    Creates a plain shell window in the original session at the original
    working directory and loads the saved scrollback history into the pane.
    The session must still exist.
    """
    snapshot_path = TERMINAL_LOG_DIR / f"{terminal_id}.snapshot.json"
    scrollback_path = TERMINAL_LOG_DIR / f"{terminal_id}.scrollback"

    if not snapshot_path.exists():
        raise click.ClickException(f"No snapshot found for terminal {terminal_id}")

    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise click.ClickException(f"Failed to read snapshot: {e}")

    session_name = snapshot["session_name"]
    working_directory = snapshot.get("working_directory")
    original_window = snapshot.get("window_name", terminal_id)

    # Verify session exists
    try:
        response = requests.get(f"{API_BASE_URL}/sessions/{session_name}")
        if response.status_code == 404:
            raise click.ClickException(
                f"Session '{session_name}' no longer exists. Cannot restore."
            )
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        raise click.ClickException("Failed to connect to cao-server")

    # Create a plain window (no agent) in the existing session
    # Pass the scrollback file as the initial command: cat prints it as output,
    # then exec replaces cat with the user's login shell (tmux-resurrect pattern).
    window_name = f"restored-{original_window}"
    login_shell = os.environ.get("SHELL", "bash")

    if scrollback_path.exists():
        window_shell = f"cat '{scrollback_path}'; exec {login_shell} -l"
    else:
        window_shell = f"exec {login_shell} -l"

    try:
        sync_backend_from_server()
        get_backend().create_window(
            session_name,
            window_name,
            terminal_id,
            working_directory,
            window_shell=window_shell,
        )
    except Exception as e:
        raise click.ClickException(f"Failed to create window: {e}")

    click.echo(
        f"Restored terminal {terminal_id} as window '{window_name}' in session '{session_name}'"
    )
    click.echo(
        f"Original agent: {snapshot.get('agent_profile', 'N/A')} | Original window: {original_window}"
    )
    if working_directory:
        click.echo(f"Working directory: {working_directory}")


@terminal.command("recover")
@click.option("--apply", "token", default=None, help="Apply an unexpired preview token")
@click.option("--all", "all_sessions", is_flag=True, help="Scan all CAO sessions (the default)")
@click.option("--dry-run", is_flag=True, help="Preview only (the default)")
@click.option("--exact-only", is_flag=True, help="Require exact evidence (always enforced)")
@click.option("--hold-inbox", is_flag=True, help="Hold recovered inboxes (always enforced)")
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text")
@click.option("--json", "as_json", is_flag=True, help="Print the structured recovery report")
def recover(token, all_sessions, dry_run, exact_only, hold_inbox, output_format, as_json) -> None:
    """Bring discoverable live CAO windows back under management.

    Unmanaged or ambiguous windows are reported for explicit `terminal adopt`;
    no identities are guessed and no sessions are restarted.
    """
    if token and dry_run:
        raise click.UsageError("--dry-run cannot be combined with --apply")
    try:
        response = requests.post(
            f"{API_BASE_URL}/terminals/recovery/{'apply' if token else 'plan'}",
            json={"token": token} if token else None,
            headers=_auth_headers(),
            timeout=120,
        )
        response.raise_for_status()
        report = response.json()
    except (requests.exceptions.RequestException, ValueError) as exc:
        raise click.ClickException(f"Recovery request failed: {exc}") from exc
    if as_json or output_format == "json":
        click.echo(json.dumps(report, indent=2))
    else:
        click.echo("Recovery applied; inboxes held" if token else "Recovery preview")
        click.echo(json.dumps(report, indent=2))
        if not token:
            click.echo(
                f"Apply: cao terminal recover --apply {report['token']} --exact-only --hold-inbox"
            )


@terminal.command("adopt")
@click.option("--terminal-id", required=True, help="Existing CAO terminal id to restore")
@click.option("--session-name", required=True, help="Existing CAO tmux session name")
@click.option("--window-name", required=True, help="Existing tmux window name")
@click.option(
    "--provider",
    type=click.Choice([provider.value for provider in ProviderType]),
    required=True,
    help="Provider already running in the live window",
)
@click.option("--agent-profile", required=True, help="Profile used by the running provider")
@click.option("--working-directory", required=True, help="Working directory of the live pane")
@click.option("--caller-id", default=None, help="Supervisor terminal id for callback routing")
@click.option(
    "--allowed-tool",
    "allowed_tools",
    multiple=True,
    help="Allowed tool (repeat for multiple tools)",
)
@click.option(
    "--engine",
    type=click.Choice([engine.value for engine in KiroEngine]),
    default=None,
    help="Resolved Kiro engine (only for provider kiro_cli)",
)
@click.option("--shell-command", default=None, help="Captured shell command, if known")
@click.option("--group", "group_levels", multiple=True, help="Discovery group level (repeatable)")
@click.option(
    "--metadata-json",
    default=None,
    help="JSON object containing consumer-defined terminal metadata",
)
def adopt(
    terminal_id: str,
    session_name: str,
    window_name: str,
    provider: str,
    agent_profile: str,
    working_directory: str,
    caller_id: str | None,
    allowed_tools: tuple[str, ...],
    engine: str | None,
    shell_command: str | None,
    group_levels: tuple[str, ...],
    metadata_json: str | None,
) -> None:
    """Re-adopt a named live tmux window into CAO after a server restart.

    The server validates that the exact session/window exists and restores
    runtime plumbing.  It never creates, renames, or kills the tmux resource.
    """
    metadata = None
    if metadata_json is not None:
        try:
            metadata = json.loads(metadata_json)
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"--metadata-json must be valid JSON: {exc.msg}") from exc
        if not isinstance(metadata, dict):
            raise click.ClickException("--metadata-json must contain a JSON object")

    body = {
        "terminal_id": terminal_id,
        "session_name": session_name,
        "window_name": window_name,
        "provider": provider,
        "agent_profile": agent_profile,
        "working_directory": working_directory,
        "caller_id": caller_id,
        "allowed_tools": list(allowed_tools) or None,
        "engine": engine,
        "shell_command": shell_command,
        "group": list(group_levels) or None,
        "metadata": metadata,
    }
    try:
        response = requests.post(
            f"{API_BASE_URL}/terminals/adopt",
            json=body,
            headers=_auth_headers(),
            timeout=30,
        )
    except requests.exceptions.ConnectionError as exc:
        raise click.ClickException("Failed to connect to cao-server") from exc
    except requests.exceptions.RequestException as exc:
        raise click.ClickException(f"Failed to adopt terminal: {exc}") from exc

    if response.status_code >= 400:
        try:
            payload = response.json()
            detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
        except (ValueError, requests.exceptions.RequestException):
            detail = response.text or f"HTTP {response.status_code}"
        raise click.ClickException(f"Adoption failed: {detail}")

    try:
        adopted_terminal = response.json()
    except ValueError as exc:
        raise click.ClickException("cao-server returned invalid adoption JSON") from exc
    adopted_id = adopted_terminal.get("id", terminal_id)
    click.echo(
        f"Adopted terminal {adopted_id} as window '{window_name}' in session '{session_name}'"
    )
