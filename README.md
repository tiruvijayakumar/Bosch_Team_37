# Incident Zero - AI On-Call Engineer

An agent pipeline (Antigravity skills + two MCP tool servers) that investigates incidents in the OpenTelemetry Astronomy Shop demo from telemetry, proposes an allowlisted fix, waits for human approval, acts only through the Action API, verifies recovery, and reports.

## Setup
1. Docker with Compose v2 and Python 3.11. Start the demo: `cd opentelemetry-demo && docker compose up -d`
2. `pip install "mcp[cli]" jsonschema`
3. Open this folder in Antigravity. `.agents/mcp_config.json` registers:
   - `otel-readonly` (`tools/otel_mcp.py`): traces (Jaeger), metrics (Prometheus), logs (OpenSearch / docker logs), container health, deploy history. Read-only.
   - `incident-actions` (`tools/otel_actions_mcp.py`): dry run, execute, kill-switch aware waiting. The only state-changing server.
4. Edit `tools/action_api.json` to match the real Action API, check with `python tools/ictl.py probe`, then set `"verified": true`.

## How to run
Human injects a fault on `http://localhost:8080/feature` (never the agent), waits 2-3 minutes, then in Antigravity chat:

    /incident-detect        symptom summary, writes Incident/incident-<id>.md
    /incident-diagnose <id> root cause + evidence + confidence, JSON in Diagnosis/
    /incident-planner <id>  allowlisted remediation plan (Action Spec)
    /incident-task-planner <id>
    /incident-fix-implementer <id>   dry run -> human approval -> execute -> verify -> rollback if needed
    /incident-report <id>   Reports/report-<id>.md and .json     (/incident-report all -> EVALUATION_NOTE.md)

## Approval gate
`action_dry_run` writes a pending bundle (action + rollback) and its SHA-256 hash. The human approves in a separate terminal: `python tools/ictl.py approve <id>` (shows the exact request, needs an interactive terminal, you type APPROVE). `execute_action` refuses unless an unexpired approval matching the hash exists, is single-use, and the config is verified. A chat "yes" does not count and the agent has no approval tool. Approval expires after 15 minutes (`APPROVAL_TTL_SECONDS`).

## Kill switch
`python tools/ictl.py kill on` creates `KILL_SWITCH`. While present, dry runs, execute and rollback are refused and `wait_and_check` aborts immediately. `kill off` re-enables. Every refusal is logged.

## Dry run
Validates the action against the allowlist, renders the exact Action API request for the action and its rollback, sends nothing (or, if the Action API supports a dry-run parameter and the config says so, a read-only dry-run request), and stores the bundle for approval.

## Action log
Every dry run, approval, refusal, execution and kill-switch change is appended to `Logs/action-log.jsonl`.

## Rules compliance
Telemetry-only diagnosis (flag services, flag metrics and `feature_flag*` span attributes are blocked in the tools); no edits to demo source/config, `faultctl` or the scoring harness; no hard-coded answers; everything runs on the local laptop; allowlisted actions only.

## Known limitations
- Approval and the "agent never runs ictl.py" rule are enforced by code for the tools, but an agent with unrestricted terminal access could in principle run the CLI; keep Antigravity's terminal in review mode.
- `tools/action_api.json` must be verified against the real Action API before execution.
- Deploy history is container lifecycle data; the demo has no CI/CD system.
- Log field names in OpenSearch vary by demo version; the agent discovers them at run time.
- Alert intake is manual (`/incident-detect`); there is no automatic threshold watcher.
