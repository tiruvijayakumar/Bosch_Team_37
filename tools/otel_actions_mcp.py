#!/usr/bin/env python3
"""incident-actions: the ONLY state-changing tool server, gated by dry run + human approval + kill switch.

Approval is deliberately NOT a tool. The human approves in a separate terminal:
    python tools/ictl.py approve <incident_id>
"""
import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import action_core as core  # noqa: E402

try:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP
except ImportError:  # mcp 2.x renamed FastMCP
    from mcp.server.mcpserver import MCPServer as FastMCP

mcp = FastMCP("incident-actions")


@mcp.tool()
def action_status(incident_id: str = "") -> dict:
    """Kill-switch state, whether action_api.json is verified, approval files for the incident,
    and the last action-log lines. Call this before proposing and before every verification step."""
    importlib.reload(core)
    return core.status(incident_id)


@mcp.tool()
def action_dry_run(incident_id: str, action: str, target: str, parameters: dict, reason: str,
                   rollback_action: str, rollback_target: str, rollback_parameters: dict) -> dict:
    """Read-only dry run. Validates the action against the allowlist (restart_service,
    toggle_feature_flag, throttle_load_generator, scale_service), renders the exact Action API
    request for the action AND its rollback, writes a pending approval bundle with a hash, and
    returns a preview. Changes nothing. A rollback is mandatory."""
    importlib.reload(core)
    return core.dry_run(incident_id, action, target, parameters, reason,
                        rollback_action, rollback_target, rollback_parameters)


@mcp.tool()
def execute_action(incident_id: str, which: str = "action") -> dict:
    """Send the human-approved request to the Action API. which='action' or 'rollback'.
    Refuses without a matching human approval, when the kill switch is on, when the approval
    expired, or when action_api.json is unverified."""
    importlib.reload(core)
    return core.execute(incident_id, which)


@mcp.tool()
def wait_and_check(seconds: int = 30) -> dict:
    """Wait up to 60s (checking the kill switch every 5s). Use between recovery checks.
    If kill_switch is true, stop everything and escalate."""
    importlib.reload(core)
    return core.wait_and_check(seconds)


if __name__ == "__main__":
    mcp.run()
