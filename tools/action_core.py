"""Safety layer in front of the Action API: allowlist, dry run, human approval, kill switch, log.

Pure functions (no MCP import) so the MCP server and the human CLI (ictl.py) share one implementation.
The ONLY way the agents may change the system is through execute() here, which calls the Action API.
"""
import getpass
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(os.getenv("INCIDENT_ROOT", Path(__file__).resolve().parent.parent))
CONFIG = ROOT / "tools" / "action_api.json"
KILL_FILE = ROOT / "KILL_SWITCH"
APPROVALS = ROOT / "Approvals"
LOG_FILE = ROOT / "Logs" / "action-log.jsonl"
APPROVAL_TTL = int(os.getenv("APPROVAL_TTL_SECONDS", "900"))
DEFAULT_TOKEN_FILE = Path.home() / ".incident-zero" / "action_api_token"
TOKEN_FILE = Path(os.getenv("ACTION_API_TOKEN_FILE", DEFAULT_TOKEN_FILE))

ALLOWED = ("restart_service", "toggle_feature_flag", "throttle_load_generator", "scale_service")


# ------------------------------------------------------------------ basics
def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def get_token():
    for candidate in (
        TOKEN_FILE,
        Path("C:/Users/vijay/.incident-zero/action_api_token"),
        ROOT / ".incident-zero" / "action_api_token",
        Path.home() / ".incident-zero" / "action_api_token",
    ):
        try:
            if candidate.exists():
                txt = candidate.read_text(encoding="utf-8").strip()
                if txt:
                    return txt
        except Exception:
            pass
    return ""


def log(event, **kw):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now(), "event": event, **kw}, default=str) + "\n")


def kill_switch_on():
    return KILL_FILE.exists()


def set_kill_switch(on, by=None):
    if on:
        KILL_FILE.write_text(f"engaged {now()} by {by or getpass.getuser()}\n")
    elif KILL_FILE.exists():
        KILL_FILE.unlink()
    log("kill_switch", state="on" if on else "off", by=by or getpass.getuser())
    return kill_switch_on()


def load_config():
    return json.loads(CONFIG.read_text())


def canonical_hash(bundle):
    return hashlib.sha256(json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _fill(obj, mapping, is_body=False):
    """Replace {placeholders}. A string that is exactly one placeholder keeps the raw value type."""
    if isinstance(obj, dict):
        res = {}
        for k, v in obj.items():
            try:
                filled = _fill(v, mapping, is_body=is_body)
                if filled is not None:
                    res[k] = filled
            except KeyError:
                if mapping.get("restore_previous"):
                    continue
                raise
        if is_body and mapping.get("restore_previous"):
            res["restore_previous"] = True
        return res
    if isinstance(obj, list):
        return [_fill(v, mapping, is_body=is_body) for v in obj]
    if isinstance(obj, str):
        if obj.startswith("{") and obj.endswith("}") and obj.count("{") == 1:
            key = obj[1:-1]
            if key not in mapping:
                raise KeyError(key)
            return mapping[key]
        out = obj
        for k, v in mapping.items():
            out = out.replace("{" + k + "}", str(v))
        if "{" in out and "}" in out:
            raise KeyError(out)
        return out
    return obj


def _known_containers():
    try:
        p = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"],
                           capture_output=True, text=True, timeout=15)
        return p.stdout.split() if p.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def validate(action, target, params, cfg):
    problems = []
    if action not in ALLOWED:
        problems.append(f"action '{action}' is not in the allowlist {list(ALLOWED)}")
        return problems
    params = params or {}
    if action == "restart_service":
        if not target:
            problems.append("target service required")
        else:
            known = _known_containers()
            if known is not None and target not in known:
                problems.append(f"target '{target}' not found among containers {known[:40]}")
    elif action == "toggle_feature_flag":
        if "flag" not in params:
            problems.append("parameters need 'flag'")
        elif not ("enabled" in params or "variant" in params or params.get("restore_previous") is True):
            problems.append("parameters need 'variant', 'enabled', or 'restore_previous: true'")
    elif action == "throttle_load_generator":
        if not (any(k in params for k in ("users", "spawn_rate", "rate")) or params.get("restore_previous") is True):
            problems.append("parameters need 'users' or 'restore_previous: true'")
    elif action == "scale_service":
        if not cfg.get("kubernetes"):
            problems.append("scale_service is Kubernetes-only; set kubernetes=true in action_api.json if applicable")
        if not target or not isinstance(params.get("replicas"), int) or not 0 <= params["replicas"] <= 10:
            problems.append("need target and integer parameters.replicas between 0 and 10")
    return problems


def render(action, target, params, cfg, dry_run=False, incident_id="", redact_token=False):
    spec = cfg["actions"][action]
    token_val = "[REDACTED]" if redact_token else get_token()
    mapping = {"target": target, "incident_id": incident_id, "token": token_val, **(params or {})}
    url = cfg["base_url"].rstrip("/") + _fill(spec["path"], mapping)
    body = _fill(spec.get("body"), mapping, is_body=True) if spec.get("body") is not None else None
    dr = cfg.get("dry_run", {})
    if dry_run and dr.get("mode") == "param":
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode({dr.get("param", "dry_run"): "true"})
    headers = _fill(cfg.get("headers", {}), mapping, is_body=False)
    return {"method": spec.get("method", "POST"), "url": url, "headers": headers, "body": body}


def _send(req, timeout=20):
    data = json.dumps(req["body"]).encode() if req["body"] is not None else None
    headers = {"Content-Type": "application/json", **req["headers"]}
    r = urllib.request.Request(req["url"], data=data, headers=headers, method=req["method"])
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            body_str = resp.read().decode()
            res = {"http_status": resp.status, "body": body_str[:500]}
            try:
                parsed = json.loads(body_str)
                if isinstance(parsed, dict):
                    res["parsed"] = {k: parsed[k] for k in ("ok", "call_id", "action", "dry_run", "message") if k in parsed}
            except Exception:
                pass
            return res
    except urllib.error.HTTPError as e:
        body_str = e.read().decode()
        res = {"http_status": e.code, "body": body_str[:500]}
        try:
            parsed = json.loads(body_str)
            if isinstance(parsed, dict):
                res["parsed"] = {k: parsed[k] for k in ("ok", "call_id", "action", "dry_run", "message") if k in parsed}
        except Exception:
            pass
        return res
    except Exception as e:  # noqa: BLE001
        return {"http_status": None, "error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------- dry run
def dry_run(incident_id, action, target, parameters, reason,
            rollback_action="", rollback_target="", rollback_parameters=None):
    if kill_switch_on():
        log("dry_run_refused", incident_id=incident_id, why="kill_switch")
        return {"ok": False, "error": "KILL SWITCH is engaged; no actions or dry runs"}
    cfg = load_config()
    parameters = parameters or {}
    problems = validate(action, target, parameters, cfg)
    rollback = None
    if rollback_action:
        rollback_parameters = rollback_parameters or {}
        problems += [f"rollback: {p}" for p in validate(rollback_action, rollback_target, rollback_parameters, cfg)]
        rollback = {"action": rollback_action, "target": rollback_target, "parameters": rollback_parameters}
    else:
        problems.append("a rollback action is required (use the inverse action, or restart_service on the same target)")
    if problems:
        log("dry_run_invalid", incident_id=incident_id, action=action, problems=problems)
        return {"ok": False, "problems": problems}
    bundle = {"incident_id": incident_id,
              "action": {"action": action, "target": target, "parameters": parameters},
              "rollback": rollback}
    digest = canonical_hash(bundle)
    try:
        preview = render(action, target, parameters, cfg, incident_id=incident_id, redact_token=True)
        rb_preview = render(rollback["action"], rollback["target"], rollback["parameters"], cfg, incident_id=incident_id, redact_token=True)
    except KeyError as e:
        return {"ok": False, "problems": [f"action_api.json template needs value {e}"]}
    result = {"mode": "local_simulation_only", "note": "no request was sent"}
    if cfg.get("verified") and cfg.get("dry_run", {}).get("mode") == "param":
        sim = _send(render(action, target, parameters, cfg, dry_run=True, incident_id=incident_id, redact_token=False))
        result = {"mode": "action_api_dry_run", **sim}
    APPROVALS.mkdir(exist_ok=True)
    pending = {"bundle": bundle, "hash": digest, "reason": reason, "created_at": now(),
                "request_preview": preview, "rollback_request_preview": rb_preview,
                "dry_run_result": result, "config_verified": bool(cfg.get("verified"))}
    (APPROVALS / f"{incident_id}.pending.json").write_text(json.dumps(pending, indent=2))
    for stale in ("approved", "action_executed", "rollback_executed", "rejected"):
        p = APPROVALS / f"{incident_id}.{stale}.json"
        if p.exists():
            p.unlink()
    log("dry_run", incident_id=incident_id, action=action, target=target, parameters=parameters,
        hash=digest, result=result)
    return {"ok": True, "hash": digest, "request_preview": preview, "rollback_request_preview": rb_preview,
            "dry_run_result": result, "config_verified": bool(cfg.get("verified")),
            "next": f"Ask the human to run in a separate terminal: python tools/ictl.py approve {incident_id}"}


# -------------------------------------------------------- human approval
def approve(incident_id, by=None):
    """Called ONLY by the human CLI (ictl.py)."""
    pending_path = APPROVALS / f"{incident_id}.pending.json"
    if not pending_path.exists():
        return {"ok": False, "error": "no pending dry run for this incident"}
    pending = json.loads(pending_path.read_text())
    approved = {"hash": pending["hash"], "approved_by": by or getpass.getuser(), "approved_at": now(),
                "expires_at_epoch": time.time() + APPROVAL_TTL}
    (APPROVALS / f"{incident_id}.approved.json").write_text(json.dumps(approved, indent=2))
    log("approved", incident_id=incident_id, hash=pending["hash"], by=approved["approved_by"])
    return {"ok": True, **approved}


def reject(incident_id, by=None):
    (APPROVALS / f"{incident_id}.rejected.json").write_text(json.dumps({"by": by or getpass.getuser(), "at": now()}))
    p = APPROVALS / f"{incident_id}.approved.json"
    if p.exists():
        p.unlink()
    log("rejected", incident_id=incident_id, by=by or getpass.getuser())
    return {"ok": True}


# -------------------------------------------------------------- execute
def execute(incident_id, which="action"):
    if kill_switch_on():
        log("execute_refused", incident_id=incident_id, which=which, why="kill_switch")
        return {"ok": False, "error": "KILL SWITCH is engaged; nothing was sent"}
    pend_p = APPROVALS / f"{incident_id}.pending.json"
    appr_p = APPROVALS / f"{incident_id}.approved.json"
    if not pend_p.exists() or not appr_p.exists():
        log("execute_refused", incident_id=incident_id, which=which, why="no_approval")
        return {"ok": False, "error": "no human approval on file; nothing was sent"}
    pending, approved = json.loads(pend_p.read_text()), json.loads(appr_p.read_text())
    if canonical_hash(pending["bundle"]) != pending["hash"] or approved["hash"] != pending["hash"]:
        log("execute_refused", incident_id=incident_id, which=which, why="hash_mismatch")
        return {"ok": False, "error": "approval does not match the dry-run payload; nothing was sent"}
    if time.time() > approved["expires_at_epoch"]:
        log("execute_refused", incident_id=incident_id, which=which, why="expired")
        return {"ok": False, "error": "approval expired; redo dry run and approval"}
    cfg = load_config()
    if not cfg.get("verified"):
        log("execute_refused", incident_id=incident_id, which=which, why="config_unverified")
        return {"ok": False, "error": "tools/action_api.json is not verified against the real Action API; nothing was sent"}
    done_action = APPROVALS / f"{incident_id}.action_executed.json"
    done_rb = APPROVALS / f"{incident_id}.rollback_executed.json"
    if which == "action":
        if done_action.exists():
            return {"ok": False, "error": "action already executed under this approval"}
        spec = pending["bundle"]["action"]
    elif which == "rollback":
        if not done_action.exists():
            return {"ok": False, "error": "rollback is only available after the action was executed"}
        if done_rb.exists():
            return {"ok": False, "error": "rollback already executed"}
        spec = pending["bundle"]["rollback"]
    else:
        return {"ok": False, "error": "which must be 'action' or 'rollback'"}
    req = render(spec["action"], spec["target"], spec["parameters"], cfg, incident_id=incident_id, redact_token=False)
    result = _send(req)
    # Check success: status < 400 and parsed.ok is True
    parsed_ok = True
    if isinstance(result.get("parsed"), dict):
        parsed_ok = result["parsed"].get("ok", True)
    is_ok = result.get("http_status") is not None and result["http_status"] < 400 and parsed_ok
    # Sanitize request recorded in approval and log (token is never stored)
    safe_req = {k: req[k] for k in ("method", "url", "body")}
    filtered_result = result.get("parsed") or result
    record = {"which": which, "request": safe_req, "result": filtered_result,
              "at": now(), "approved_by": approved["approved_by"], "approved_at": approved["approved_at"]}
    (done_action if which == "action" else done_rb).write_text(json.dumps(record, indent=2))
    log("executed", incident_id=incident_id, **record)
    return {"ok": is_ok, **record}


def status(incident_id=""):
    cfg = load_config()
    out = {"kill_switch": kill_switch_on(), "action_api_config_verified": bool(cfg.get("verified")),
           "allowed_actions": list(ALLOWED), "approval_ttl_seconds": APPROVAL_TTL}
    if incident_id:
        out["files"] = sorted(p.name for p in APPROVALS.glob(f"{incident_id}.*")) if APPROVALS.exists() else []
    if LOG_FILE.exists():
        out["last_log_lines"] = LOG_FILE.read_text().splitlines()[-8:]
    return out


def wait_and_check(seconds):
    seconds = max(1, min(int(seconds), 60))
    waited = 0
    while waited < seconds:
        if kill_switch_on():
            return {"kill_switch": True, "waited": waited}
        time.sleep(min(5, seconds - waited))
        waited += min(5, seconds - waited)
    return {"kill_switch": kill_switch_on(), "waited": waited}


def probe():
    """Human-run discovery: read-only GETs against the configured Action API."""
    cfg = load_config()
    out = {}
    for path in ("/", "/openapi.json", "/docs", "/health", "/actions"):
        r = _send({"method": "GET", "url": cfg["base_url"].rstrip("/") + path, "headers": cfg.get("headers", {}), "body": None})
        out[path] = {"status": r.get("http_status"), "snippet": (r.get("body") or r.get("error") or "")[:300]}
    return out
