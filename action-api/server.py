#!/usr/bin/env python3
"""Action API Server for Incident Zero Hackathon.

Allowed operations:
- restart a service (compose application services only)
- toggle a feature flag (read-modify-write via flagd-ui; fault-off only, or restore_previous)
- throttle the load generator (Locust /swarm; reduce user count only, or restore_previous)
- scale a service (Kubernetes only; returns 501)

Safety invariants:
- Binds to 127.0.0.1 only.
- Strict response schema: {"ok": bool, "call_id": str, "action": str, "dry_run": bool, "message": str}.
- Authorization: Bearer <token> read from ACTION_API_TOKEN_FILE outside workspace.
- Kill switch: Refuses state-changing and dry-run POST requests if KILL_SWITCH exists.
- Serialized execution via threading lock.
- Logs every call to Logs/action-api-calls.jsonl without token or flag values.
- Never reveals flag state, flag values, or flag lists to callers.
"""

import http.server
import json
import os
import pathlib
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# Paths
ROOT = pathlib.Path(os.getenv("INCIDENT_ROOT", pathlib.Path(__file__).resolve().parent.parent))
KILL_FILE = ROOT / "KILL_SWITCH"
LOG_FILE = ROOT / "Logs" / "action-api-calls.jsonl"
STATE_DIR = pathlib.Path(__file__).resolve().parent / "state"
JOURNAL_FILE = STATE_DIR / "journal.jsonl"
DEFAULT_TOKEN_FILE = pathlib.Path.home() / ".incident-zero" / "action_api_token"
TOKEN_FILE = pathlib.Path(os.getenv("ACTION_API_TOKEN_FILE", DEFAULT_TOKEN_FILE))

# Service Allowlist & Protection
APPLICATION_SERVICES = {
    "frontend", "checkout", "recommendation", "product-catalog", "cart",
    "quote", "email", "shipping", "currency", "ad", "payment", "image-provider"
}
PROTECTED_SERVICES = {
    "otel-collector", "jaeger", "prometheus", "grafana", "opensearch",
    "telemetry-docs", "flagd", "flagd-ui", "load-generator", "frontend-proxy",
    "action-api", "valkey-cart", "astronomy-db", "opamp-server"
}

# Feature Flags (documented) & fault-off variants
KNOWN_FLAGS = {
    "adFailure", "adHighCpu", "adManualGc", "aiRunawayAgent", "aiSlowResponse",
    "cartFailure", "emailMemoryLeak", "failedReadinessProbe", "imageSlowLoad",
    "intlShippingSlowdown", "kafkaConsumerDead", "kafkaQueueProblems",
    "loadGeneratorTraffic", "loadGeneratorVUs", "paymentFailure", "paymentUnreachable",
    "productCatalogFailure", "productCatalogLockContention", "recommendationCacheFailure"
}
FAULT_OFF_VARIANTS = {"off", "false", "0"}

# Upstream proxy endpoints
FLAG_READ_URL = "http://localhost:8080/feature/api/read"
FLAG_WRITE_URL = "http://localhost:8080/feature/api/write"
LOADGEN_STATS_URL = "http://localhost:8080/loadgen/stats/requests"
LOADGEN_SWARM_URL = "http://localhost:8080/loadgen/swarm"

# Lock for serialized mutations
MUTEX = threading.Lock()


def now_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def get_expected_token():
    if not TOKEN_FILE.exists():
        return None
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return None


def kill_switch_engaged():
    return KILL_FILE.exists()


def log_call(call_id, incident_id, action, dry_run, params, http_status, ok, message):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    sanitized_params = {}
    if isinstance(params, dict):
        for k, v in params.items():
            if k in ("token", "authorization", "Authorization"):
                continue
            if k in ("variant", "flag_value", "value") and action == "toggle_feature_flag":
                sanitized_params[k] = "[REDACTED]"
            else:
                sanitized_params[k] = v
    entry = {
        "ts": now_utc(),
        "call_id": call_id,
        "incident_id": incident_id or "unknown",
        "action": action,
        "dry_run": dry_run,
        "parameters": sanitized_params,
        "http_status": http_status,
        "ok": ok,
        "message": message,
    }
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def append_journal(entry):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with JOURNAL_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now_utc(), **entry}) + "\n")


def get_last_journal(incident_id, action_type, key):
    if not JOURNAL_FILE.exists():
        return None
    lines = JOURNAL_FILE.read_text(encoding="utf-8").splitlines()
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if item.get("incident_id") == incident_id and item.get("action_type") == action_type and item.get("key") == key:
                return item
        except Exception:
            continue
    return None


class ActionAPIHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send_strict_response(self, status_code, ok, call_id, action, dry_run, message):
        body_dict = {
            "ok": ok,
            "call_id": call_id,
            "action": action,
            "dry_run": dry_run,
            "message": message,
        }
        body_bytes = json.dumps(body_dict).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def verify_auth(self):
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        received_token = auth_header[7:].strip()
        expected = get_expected_token()
        if not expected or not secrets.compare_digest(received_token, expected):
            return False
        return True

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        call_id = f"call-{uuid.uuid4().hex[:8]}"

        if parsed.path == "/health":
            self.send_strict_response(200, True, call_id, "health", False, "Action API is healthy and listening on 127.0.0.1:8099.")
            log_call(call_id, "", "health", False, {}, 200, True, "healthy")
            return

        if parsed.path == "/actions":
            self.send_strict_response(200, True, call_id, "actions", False, "Allowed actions: restart_service, toggle_feature_flag, throttle_load_generator, scale_service.")
            log_call(call_id, "", "actions", False, {}, 200, True, "actions list returned")
            return

        self.send_strict_response(404, False, call_id, "unknown", False, f"Endpoint {parsed.path} not found.")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        dry_run = query.get("dry_run", ["false"])[0].lower() in ("true", "1")
        call_id = f"call-{uuid.uuid4().hex[:8]}"

        # Action mapping
        action_map = {
            "/actions/restart": "restart_service",
            "/actions/flag": "toggle_feature_flag",
            "/actions/throttle": "throttle_load_generator",
            "/actions/scale": "scale_service",
        }

        if parsed.path not in action_map:
            self.send_strict_response(404, False, call_id, "unknown", dry_run, f"Action endpoint '{parsed.path}' is not allowlisted.")
            log_call(call_id, "", "unknown", dry_run, {}, 404, False, "unknown endpoint")
            return

        action = action_map[parsed.path]

        # Auth check
        if not self.verify_auth():
            msg = "Unauthorized: missing or invalid Bearer token."
            self.send_strict_response(401, False, call_id, action, dry_run, msg)
            log_call(call_id, "", action, dry_run, {}, 401, False, msg)
            return

        # Kill switch check (applies to all POST requests, including dry-run)
        if kill_switch_engaged():
            msg = "Refused: KILL_SWITCH is engaged. All Action API POST calls are blocked."
            self.send_strict_response(403, False, call_id, action, dry_run, msg)
            log_call(call_id, "", action, dry_run, {}, 403, False, msg)
            return

        # Read JSON body
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_length)
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except Exception as e:
            msg = f"Bad request: malformed JSON body ({type(e).__name__})."
            self.send_strict_response(400, False, call_id, action, dry_run, msg)
            log_call(call_id, "", action, dry_run, {}, 400, False, msg)
            return

        incident_id = str(body.get("incident_id", "")).strip()
        if not incident_id:
            msg = "Bad request: 'incident_id' is mandatory in request body."
            self.send_strict_response(400, False, call_id, action, dry_run, msg)
            log_call(call_id, "", action, dry_run, body, 400, False, msg)
            return

        # Route to handler
        if action == "restart_service":
            self.handle_restart(call_id, incident_id, action, dry_run, body)
        elif action == "toggle_feature_flag":
            self.handle_flag(call_id, incident_id, action, dry_run, body)
        elif action == "throttle_load_generator":
            self.handle_throttle(call_id, incident_id, action, dry_run, body)
        elif action == "scale_service":
            self.handle_scale(call_id, incident_id, action, dry_run, body)

    def handle_restart(self, call_id, incident_id, action, dry_run, body):
        service = str(body.get("service", "")).strip()
        if not service:
            msg = "Bad request: 'service' parameter is required for restart_service."
            self.send_strict_response(400, False, call_id, action, dry_run, msg)
            log_call(call_id, incident_id, action, dry_run, body, 400, False, msg)
            return

        if service in PROTECTED_SERVICES:
            msg = f"Refused: Service '{service}' is protected infrastructure and cannot be restarted."
            self.send_strict_response(403, False, call_id, action, dry_run, msg)
            log_call(call_id, incident_id, action, dry_run, body, 403, False, msg)
            return

        if service not in APPLICATION_SERVICES:
            msg = f"Refused: Service '{service}' is not an allowlisted application service."
            self.send_strict_response(400, False, call_id, action, dry_run, msg)
            log_call(call_id, incident_id, action, dry_run, body, 400, False, msg)
            return

        if dry_run:
            msg = f"Dry run: Service '{service}' restart validated. Container would be restarted via docker CLI."
            self.send_strict_response(200, True, call_id, action, True, msg)
            log_call(call_id, incident_id, action, True, body, 200, True, msg)
            return

        with MUTEX:
            if kill_switch_engaged():
                msg = "Refused: KILL_SWITCH was engaged before execution."
                self.send_strict_response(403, False, call_id, action, False, msg)
                log_call(call_id, incident_id, action, False, body, 403, False, msg)
                return

            try:
                res = subprocess.run(
                    ["docker", "restart", service],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False
                )
                if res.returncode == 0:
                    msg = f"Service '{service}' restarted successfully."
                    self.send_strict_response(200, True, call_id, action, False, msg)
                    log_call(call_id, incident_id, action, False, body, 200, True, msg)
                else:
                    err = res.stderr.strip() or res.stdout.strip() or f"exit code {res.returncode}"
                    msg = f"Failed to restart service '{service}': {err}"
                    self.send_strict_response(500, False, call_id, action, False, msg)
                    log_call(call_id, incident_id, action, False, body, 500, False, msg)
            except Exception as e:
                msg = f"Error executing docker restart for '{service}': {e}"
                self.send_strict_response(500, False, call_id, action, False, msg)
                log_call(call_id, incident_id, action, False, body, 500, False, msg)

    def handle_flag(self, call_id, incident_id, action, dry_run, body):
        flag = str(body.get("flag", "")).strip()
        variant = str(body.get("variant", "")).strip()
        restore_previous = bool(body.get("restore_previous", False))

        if not flag or (not variant and not restore_previous):
            msg = "Bad request: 'flag' and either 'variant' or 'restore_previous: true' are required."
            self.send_strict_response(400, False, call_id, action, dry_run, msg)
            log_call(call_id, incident_id, action, dry_run, body, 400, False, msg)
            return

        if flag not in KNOWN_FLAGS:
            msg = "Invalid flag or variant."
            self.send_strict_response(400, False, call_id, action, dry_run, msg)
            log_call(call_id, incident_id, action, dry_run, body, 400, False, msg)
            return

        if not restore_previous and variant.lower() not in FAULT_OFF_VARIANTS:
            msg = "Invalid flag or variant: direct changes may only set the fault-off variant."
            self.send_strict_response(400, False, call_id, action, dry_run, msg)
            log_call(call_id, incident_id, action, dry_run, body, 400, False, msg)
            return

        if dry_run:
            mode = "restore_previous" if restore_previous else "fault-off set"
            msg = f"Dry run: Feature flag request for '{flag}' ({mode}) validated."
            self.send_strict_response(200, True, call_id, action, True, msg)
            log_call(call_id, incident_id, action, True, body, 200, True, msg)
            return

        with MUTEX:
            if kill_switch_engaged():
                msg = "Refused: KILL_SWITCH was engaged before execution."
                self.send_strict_response(403, False, call_id, action, False, msg)
                log_call(call_id, incident_id, action, False, body, 403, False, msg)
                return

            # Read current flagd config
            try:
                read_req = urllib.request.Request(FLAG_READ_URL, headers={"Accept": "application/json"})
                with urllib.request.urlopen(read_req, timeout=10) as r:
                    config = json.loads(r.read().decode("utf-8"))
            except Exception as e:
                msg = f"Failed to read current feature flag configuration: {type(e).__name__}"
                self.send_strict_response(500, False, call_id, action, False, msg)
                log_call(call_id, incident_id, action, False, body, 500, False, msg)
                return

            flags_dict = config.get("flags")
            if not isinstance(flags_dict, dict) or flag not in flags_dict:
                msg = "Invalid flag or variant."
                self.send_strict_response(400, False, call_id, action, False, msg)
                log_call(call_id, incident_id, action, False, body, 400, False, msg)
                return

            flag_entry = flags_dict[flag]
            current_variant = flag_entry.get("defaultVariant")
            # If targeting exists with ["if", [cond, on_val, off_val]], that holds the active variant
            targeting_if = None
            if isinstance(flag_entry.get("targeting"), dict) and isinstance(flag_entry["targeting"].get("if"), list):
                if len(flag_entry["targeting"]["if"]) >= 2:
                    targeting_if = flag_entry["targeting"]["if"]
                    current_variant = targeting_if[1]

            target_variant = variant
            if restore_previous:
                journal_item = get_last_journal(incident_id, "toggle_feature_flag", flag)
                if not journal_item or "previous_variant" not in journal_item:
                    msg = f"Cannot restore previous state: no journal entry found for flag '{flag}' under incident '{incident_id}'."
                    self.send_strict_response(400, False, call_id, action, False, msg)
                    log_call(call_id, incident_id, action, False, body, 400, False, msg)
                    return
                target_variant = journal_item["previous_variant"]
            else:
                append_journal({
                    "incident_id": incident_id,
                    "action_type": "toggle_feature_flag",
                    "key": flag,
                    "previous_variant": current_variant
                })

            # Check target variant in variants map
            variants_map = flag_entry.get("variants", {})
            if target_variant not in variants_map:
                msg = "Invalid flag or variant."
                self.send_strict_response(400, False, call_id, action, False, msg)
                log_call(call_id, incident_id, action, False, body, 400, False, msg)
                return

            # Apply change in-memory
            if targeting_if is not None:
                targeting_if[1] = target_variant
            else:
                flag_entry["defaultVariant"] = target_variant

            # Write updated config back to flagd-ui
            try:
                write_payload = json.dumps({"data": config}).encode("utf-8")
                write_req = urllib.request.Request(
                    FLAG_WRITE_URL,
                    data=write_payload,
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(write_req, timeout=10) as r:
                    if r.status >= 400:
                        msg = f"Feature flag write returned HTTP {r.status}."
                        self.send_strict_response(500, False, call_id, action, False, msg)
                        log_call(call_id, incident_id, action, False, body, 500, False, msg)
                        return
            except Exception as e:
                msg = f"Failed to write feature flag configuration: {type(e).__name__}"
                self.send_strict_response(500, False, call_id, action, False, msg)
                log_call(call_id, incident_id, action, False, body, 500, False, msg)
                return

            msg = f"Feature flag '{flag}' updated successfully."
            self.send_strict_response(200, True, call_id, action, False, msg)
            log_call(call_id, incident_id, action, False, body, 200, True, msg)

    def handle_throttle(self, call_id, incident_id, action, dry_run, body):
        users_val = body.get("users")
        spawn_rate = int(body.get("spawn_rate", 1))
        restore_previous = bool(body.get("restore_previous", False))

        if users_val is None and not restore_previous:
            msg = "Bad request: 'users' integer or 'restore_previous: true' is required."
            self.send_strict_response(400, False, call_id, action, dry_run, msg)
            log_call(call_id, incident_id, action, dry_run, body, 400, False, msg)
            return

        # Query current user count from load generator
        try:
            req = urllib.request.Request(LOADGEN_STATS_URL, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                stats = json.loads(r.read().decode("utf-8"))
            current_users = int(stats.get("user_count", 0))
        except Exception as e:
            msg = f"Failed to retrieve current user count from load generator: {type(e).__name__}"
            self.send_strict_response(500, False, call_id, action, dry_run, msg)
            log_call(call_id, incident_id, action, dry_run, body, 500, False, msg)
            return

        target_users = None
        if restore_previous:
            if dry_run:
                msg = "Dry run: Load generator throttle (restore_previous) validated."
                self.send_strict_response(200, True, call_id, action, True, msg)
                log_call(call_id, incident_id, action, True, body, 200, True, msg)
                return
            journal_item = get_last_journal(incident_id, "throttle_load_generator", "users")
            if not journal_item or "previous_users" not in journal_item:
                msg = f"Cannot restore previous state: no journal entry found for throttle under incident '{incident_id}'."
                self.send_strict_response(400, False, call_id, action, dry_run, msg)
                log_call(call_id, incident_id, action, dry_run, body, 400, False, msg)
                return
            target_users = int(journal_item["previous_users"])
        else:
            try:
                target_users = int(users_val)
            except Exception:
                msg = "Bad request: 'users' must be an integer."
                self.send_strict_response(400, False, call_id, action, dry_run, msg)
                log_call(call_id, incident_id, action, dry_run, body, 400, False, msg)
                return

            if target_users < 0:
                msg = "Bad request: 'users' cannot be negative."
                self.send_strict_response(400, False, call_id, action, dry_run, msg)
                log_call(call_id, incident_id, action, dry_run, body, 400, False, msg)
                return

            if target_users > current_users:
                msg = f"Refused: Throttling can only reduce traffic. Requested users ({target_users}) exceeds current users ({current_users})."
                self.send_strict_response(400, False, call_id, action, dry_run, msg)
                log_call(call_id, incident_id, action, dry_run, body, 400, False, msg)
                return

        if dry_run:
            msg = f"Dry run: Load generator throttle validated (current users: {current_users}, target users: {target_users})."
            self.send_strict_response(200, True, call_id, action, True, msg)
            log_call(call_id, incident_id, action, True, body, 200, True, msg)
            return

        with MUTEX:
            if kill_switch_engaged():
                msg = "Refused: KILL_SWITCH was engaged before execution."
                self.send_strict_response(403, False, call_id, action, False, msg)
                log_call(call_id, incident_id, action, False, body, 403, False, msg)
                return

            if not restore_previous:
                append_journal({
                    "incident_id": incident_id,
                    "action_type": "throttle_load_generator",
                    "key": "users",
                    "previous_users": current_users
                })

            form_data = urllib.parse.urlencode({"user_count": target_users, "spawn_rate": spawn_rate}).encode("utf-8")
            swarm_req = urllib.request.Request(
                LOADGEN_SWARM_URL,
                data=form_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            try:
                with urllib.request.urlopen(swarm_req, timeout=10) as r:
                    if r.status >= 400:
                        msg = f"Load generator swarm endpoint returned HTTP {r.status}."
                        self.send_strict_response(500, False, call_id, action, False, msg)
                        log_call(call_id, incident_id, action, False, body, 500, False, msg)
                        return
            except Exception as e:
                msg = f"Failed to send swarm update to load generator: {type(e).__name__}"
                self.send_strict_response(500, False, call_id, action, False, msg)
                log_call(call_id, incident_id, action, False, body, 500, False, msg)
                return

            msg = f"Load generator throttled successfully to {target_users} users."
            self.send_strict_response(200, True, call_id, action, False, msg)
            log_call(call_id, incident_id, action, False, body, 200, True, msg)

    def handle_scale(self, call_id, incident_id, action, dry_run, body):
        msg = "scale_service is not implemented: Kubernetes cluster is not available (501)."
        self.send_strict_response(501, False, call_id, action, dry_run, msg)
        log_call(call_id, incident_id, action, dry_run, body, 501, False, msg)

    def log_message(self, format, *args):
        # Suppress default stderr logging so console output stays silent and clean
        pass


def run_server(port=8099, host="127.0.0.1"):
    server = http.server.ThreadingHTTPServer((host, port), ActionAPIHandler)
    print(f"Action API listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()
