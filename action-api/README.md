# Incident Zero Action API
#Team 37's plan

Lightweight local Action API service for the Incident Zero hackathon.
The agent may change the system ONLY through this Action API. Every call is logged.

## Architecture

- Binds to `127.0.0.1:8099` (loopback only).
- Authenticated via Bearer token loaded from `%USERPROFILE%\.incident-zero\action_api_token` (or `ACTION_API_TOKEN_FILE`).
- Protected by workspace `KILL_SWITCH` file: any presence of `KILL_SWITCH` rejects state-changing calls immediately.
- Strict response schema: `{"ok": bool, "call_id": str, "action": str, "dry_run": bool, "message": str}`.
- State-change serialization via mutex lock.
- Logs all calls to `Logs/action-api-calls.jsonl` (sanitized: tokens and flag values are never logged).

## Endpoints

- `GET /health`: Health probe.
- `GET /actions`: List of allowed action names.
- `POST /actions/restart`: Restart an allowed application container (`docker restart`).
- `POST /actions/flag`: Toggle a feature flag to `"off"`, or restore previous journaled state (`restore_previous: true`).
- `POST /actions/throttle`: Throttle the load generator user count, or restore previous journaled count (`restore_previous: true`).
- `POST /actions/scale`: Service scaling (returns 501 when Kubernetes is unavailable).

Every POST endpoint supports query parameter `?dry_run=true` to validate prerequisites without modifying state.

## Running the Service

```powershell
python action-api\server.py
```

To stop: Press `Ctrl+C` in the running terminal, or run:
```powershell
Get-Process python | Where-Object { $_.CommandLine -match "action-api" } | Stop-Process
```
