#!/usr/bin/env python3
"""otel-readonly: read-only telemetry tools for the Incident Zero agents (MCP over stdio).

Sources: Jaeger (traces), Prometheus (metrics), OpenSearch / docker logs (logs),
docker (container health + lifecycle = "deploy history").

Hackathon rules enforced in code:
  * Read-only. No tool changes system state.
  * Diagnosis must come from telemetry, never from feature-flag settings, so every
    tool refuses flagd / flag-UI containers and strips feature_flag* span attributes.
"""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

try:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP
except ImportError:  # mcp 2.x renamed FastMCP
    from mcp.server.mcpserver import MCPServer as FastMCP

JAEGER_API = os.getenv("JAEGER_API", "http://localhost:8080/jaeger/ui/api")
PROM_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://localhost:9200")
OPENSEARCH_INDEX = os.getenv("OPENSEARCH_INDEX", "otel-logs-*")
# Grafana is always published on :8080 by the demo proxy; used as a fallback route to Prometheus/OpenSearch.
GRAFANA_API = os.getenv("GRAFANA_API", "http://localhost:8080/grafana/api")
TIMEOUT = 15

BLOCKED_NAMES = ("flagd",)  # matches flagd and flagd-ui containers/services
BLOCKED_KEY_PREFIXES = ("feature_flag", "feature-flag", "flagd")

mcp = FastMCP("otel-readonly")


# ----------------------------------------------------------------- helpers
def _blocked_name(name):
    n = (name or "").lower()
    return any(b in n for b in BLOCKED_NAMES)


def _blocked_text(text):
    return "flag" in (text or "").lower()


def _http_json(url, params=None, body=None):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data, headers = None, {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code} from {url}", "detail": e.read().decode()[:300]}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "url": url}


_DS = {}


def _grafana_uid(kind):
    if _DS.get(kind):
        return _DS[kind]
    r = _http_json(f"{GRAFANA_API}/datasources")
    if isinstance(r, list):
        for d in r:
            t = str(d.get("type", "")).lower()
            if (kind == "prometheus" and t == "prometheus") or \
               (kind == "opensearch" and ("opensearch" in t or "elasticsearch" in t)):
                _DS[kind] = d.get("uid")
                return _DS[kind]
    return None


def _is_err(r):
    return isinstance(r, dict) and "error" in r


def _prom_get(path, params=None):
    """Prometheus direct; if that port is not published, via Grafana's datasource proxy."""
    r = _http_json(f"{PROM_URL}{path}", params)
    if not _is_err(r):
        return r
    uid = _grafana_uid("prometheus")
    if uid:
        r2 = _http_json(f"{GRAFANA_API}/datasources/proxy/uid/{uid}{path}", params)
        if not _is_err(r2):
            return r2
    return r


def _os_search(body):
    path = f"/{OPENSEARCH_INDEX}/_search"
    r = _http_json(f"{OPENSEARCH_URL}{path}", body=body)
    if not _is_err(r):
        return r
    uid = _grafana_uid("opensearch")
    if uid:
        r2 = _http_json(f"{GRAFANA_API}/datasources/proxy/uid/{uid}{path}", body=body)
        if not _is_err(r2):
            return r2
    return r


def _docker(args, timeout=30):
    try:
        p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"
    if p.returncode != 0:
        return None, p.stderr.strip()[:300]
    return p.stdout, None


def _json_lines(text):
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def _resolve_container(service):
    out, err = _docker(["ps", "-a", "--format", "{{.Names}}"])
    if err:
        return None, err
    names = [n for n in out.split() if not _blocked_name(n)]
    for n in names:
        if n == service:
            return n, None
    matches = [n for n in names if service in n]
    if len(matches) == 1:
        return matches[0], None
    return None, f"no unique container for '{service}'. candidates: {matches or names[:30]}"


_TAG_HINTS = ("error", "status", "exception", "app.", "http.", "rpc.", "db.", "messaging.",
              "peer", "net.", "server.", "url.")


def _tag_dict(items):
    out = {}
    for t in items or []:
        k = str(t.get("key", ""))
        if k.lower().startswith(BLOCKED_KEY_PREFIXES):
            continue
        if any(h in k.lower() for h in _TAG_HINTS):
            out[k] = t.get("value")
    return out


def _events(span):
    evs = []
    for lg in span.get("logs", []):
        f = {str(x.get("key")): x.get("value") for x in lg.get("fields", [])}
        if any(k.lower().startswith(BLOCKED_KEY_PREFIXES) for k in f):
            continue
        if str(f.get("event", "")).lower().startswith(BLOCKED_KEY_PREFIXES):
            continue
        evs.append({k: str(v)[:200] for k, v in f.items()
                    if k in ("event", "exception.type", "exception.message", "message")})
    return evs[:2]


def _summarize_trace(t, full=False):
    procs = {k: v.get("serviceName") for k, v in t.get("processes", {}).items()}
    spans = t.get("spans", [])
    parent = {}
    for s in spans:
        for r in s.get("references", []):
            if r.get("refType") == "CHILD_OF":
                parent[s["spanID"]] = r.get("spanID")

    def depth(sid):
        d, seen = 0, set()
        while sid in parent and sid not in seen:
            seen.add(sid)
            sid = parent[sid]
            d += 1
        return d

    rows, errors = [], []
    for s in spans:
        svc = procs.get(s.get("processID"))
        if _blocked_name(svc):
            continue
        tags = _tag_dict(s.get("tags"))
        is_err = (str(tags.get("error", "")).lower() == "true"
                  or str(tags.get("otel.status_code", "")).upper() == "ERROR")
        row = {"span_id": s["spanID"], "service": svc, "operation": s.get("operationName"),
               "duration_ms": round(s.get("duration", 0) / 1000, 2), "depth": depth(s["spanID"]),
               "error": is_err}
        rows.append(row)
        if is_err:
            errors.append({**row, "tags": tags, "events": _events(s)})
    errors.sort(key=lambda e: -e["depth"])
    starts = [s.get("startTime", 0) for s in spans]
    ends = [s.get("startTime", 0) + s.get("duration", 0) for s in spans]
    slowest = max(rows, key=lambda r: r["duration_ms"], default=None)
    out = {
        "trace_id": t.get("traceID"),
        "total_ms": round((max(ends) - min(starts)) / 1000, 2) if spans else 0,
        "span_count": len(spans),
        "services": sorted({r["service"] for r in rows if r["service"]}),
        "error_span_count": len(errors),
        "deepest_error_span": errors[0] if errors else None,
        "other_error_spans": errors[1:4],
        "slowest_span": slowest,
    }
    if full:
        out["spans"] = sorted(rows, key=lambda r: (r["depth"], -r["duration_ms"]))[:60]
    return out


# ------------------------------------------------------------------- traces
@mcp.tool()
def list_services() -> dict:
    """List services that have reported traces to Jaeger."""
    r = _http_json(f"{JAEGER_API}/services")
    if "error" in r:
        return r
    return {"services": [s for s in r.get("data", []) if not _blocked_name(s)]}


@mcp.tool()
def query_traces(service: str, lookback_minutes: int = 15, only_errors: bool = True,
                 min_duration_ms: int = 0, operation: str = "", tag_filter: str = "",
                 limit: int = 10) -> dict:
    """Search Jaeger traces that include `service`. Returns compact per-trace summaries
    (deepest failing span, slowest span, services touched) plus aggregate error counts per
    service/operation. tag_filter is optional 'key=value' (e.g. 'app.product.id=XYZ').
    Use get_trace(trace_id) to verify any trace you plan to cite."""
    if _blocked_name(service):
        return {"error": "blocked: flag services are out of scope; diagnose from telemetry"}
    tags = {}
    if only_errors:
        tags["error"] = "true"
    if tag_filter and "=" in tag_filter:
        k, v = tag_filter.split("=", 1)
        if _blocked_text(k):
            return {"error": "blocked: flag-related filter"}
        tags[k.strip()] = v.strip()
    params = {"service": service, "lookback": f"{int(lookback_minutes)}m", "limit": int(limit)}
    if tags:
        params["tags"] = json.dumps(tags)
    if min_duration_ms:
        params["minDuration"] = f"{int(min_duration_ms)}ms"
    if operation:
        params["operation"] = operation
    r = _http_json(f"{JAEGER_API}/traces", params)
    if "error" in r:
        return r
    traces = [_summarize_trace(t) for t in r.get("data", [])]
    agg = Counter()
    for t in traces:
        e = t["deepest_error_span"]
        if e:
            agg[f"{e['service']} :: {e['operation']}"] += 1
    return {"query": params, "count": len(traces),
            "deepest_error_by_service_operation": dict(agg.most_common(8)), "traces": traces}


@mcp.tool()
def get_trace(trace_id: str) -> dict:
    """Fetch one trace by ID and return its span tree summary. Use this to verify evidence."""
    r = _http_json(f"{JAEGER_API}/traces/{urllib.parse.quote(trace_id)}")
    if "error" in r:
        return r
    data = r.get("data", [])
    return _summarize_trace(data[0], full=True) if data else {"error": "trace not found"}


# ------------------------------------------------------------------ metrics
@mcp.tool()
def list_metric_names(contains: str = "") -> dict:
    """List Prometheus metric names containing a substring. Use this to discover names
    instead of guessing them (e.g. 'span_metrics', 'memory', 'cpu', 'gc')."""
    r = _prom_get("/api/v1/label/__name__/values")
    if "error" in r:
        return r
    names = [n for n in r.get("data", []) if contains.lower() in n.lower() and "flag" not in n.lower()]
    return {"count": len(names), "names": names[:150]}


@mcp.tool()
def query_metrics(promql: str, lookback_minutes: int = 15, step_seconds: int = 30) -> dict:
    """Run a PromQL range query. Returns per-series first/last/min/max/avg, rising_fraction
    (share of steps that increased; ~1.0 on a steady climb such as a leak) and a short sample."""
    if _blocked_text(promql):
        return {"error": "blocked: flag-related metric; diagnose from telemetry"}
    end = time.time()
    r = _prom_get("/api/v1/query_range",
                  {"query": promql, "start": end - lookback_minutes * 60, "end": end,
                   "step": step_seconds})
    if "error" in r or r.get("status") != "success":
        return r
    series = []
    for s in r["data"]["result"][:25]:
        vals = [float(v[1]) for v in s["values"] if v[1] not in ("NaN", "+Inf", "-Inf")]
        if not vals:
            continue
        diffs = [b - a for a, b in zip(vals, vals[1:])]
        k = max(1, len(vals) // 8)
        series.append({
            "labels": {a: b for a, b in s["metric"].items() if a not in ("job", "instance")},
            "points": len(vals), "first": vals[0], "last": vals[-1], "min": min(vals),
            "max": max(vals), "avg": sum(vals) / len(vals),
            "rising_fraction": round(sum(1 for d in diffs if d > 0) / len(diffs), 2) if diffs else 0,
            "sample": [round(v, 4) for v in vals[::k]][:10]})
    return {"query": promql, "lookback_minutes": lookback_minutes, "series_count": len(series),
            "series": series}



@mcp.tool()
def source_check() -> dict:
    """Run FIRST. Reports which telemetry sources are reachable: Jaeger, Prometheus (direct or via
    Grafana), OpenSearch (direct or via Grafana), Docker. Tells you which tools will work."""
    out = {}
    j = _http_json(f"{JAEGER_API}/services")
    out["jaeger_traces"] = "ok" if not _is_err(j) else j["error"]
    p = _prom_get("/api/v1/query", {"query": "up"})
    out["prometheus_metrics"] = "ok" if (not _is_err(p) and p.get("status") == "success") else (p.get("error") if _is_err(p) else "unexpected response")
    o = _os_search({"size": 1})
    out["opensearch_logs"] = "ok" if not _is_err(o) else o["error"] + " (query_logs via docker still works)"
    _, derr = _docker(["ps", "-q"])
    out["docker"] = "ok" if not derr else derr
    out["note"] = "A source that is not ok means the matching tools return errors; say so in the report instead of guessing."
    return out

# --------------------------------------------------------------------- logs
@mcp.tool()
def query_logs(service: str, lookback_minutes: int = 15, contains: str = "",
               severity: str = "", limit: int = 30) -> dict:
    """Read a service container's recent logs (docker logs). Returns the most frequent
    message patterns (numbers collapsed) and the latest matching raw lines.
    severity examples: 'error', 'warn'."""
    if _blocked_name(service):
        return {"error": "blocked: flag services are out of scope; diagnose from telemetry"}
    name, err = _resolve_container(service)
    if err:
        return {"error": err}
    try:
        p = subprocess.run(["docker", "logs", "--since", f"{int(lookback_minutes)}m", "--tail", "3000", name],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    lines = (p.stdout + p.stderr).splitlines()
    total = len(lines)
    if severity:
        lines = [l for l in lines if severity.lower() in l.lower()]
    if contains:
        lines = [l for l in lines if contains.lower() in l.lower()]
    pats = Counter(re.sub(r"\d+", "N", l[:200]) for l in lines)
    examples = {}
    for l in lines:
        examples.setdefault(re.sub(r"\d+", "N", l[:200]), l[:300])
    return {"container": name, "lines_scanned": total, "lines_matched": len(lines),
            "top_patterns": [{"count": c, "example": examples[k]} for k, c in pats.most_common(8)],
            "latest_lines": [l[:300] for l in lines[-max(1, limit // 2):]]}


@mcp.tool()
def search_opensearch_logs(query: str = "*", lookback_minutes: int = 15, limit: int = 20) -> dict:
    """Lucene query_string search over OpenSearch logs (index otel-logs-* by default).
    Field names depend on the demo version: start with query='*' and limit=3 to see the
    schema, then narrow (e.g. 'severityText:ERROR AND payment')."""
    if _blocked_text(query):
        return {"error": "blocked: flag-related query; diagnose from telemetry"}
    body = {"size": int(limit), "sort": [{"@timestamp": {"order": "desc", "unmapped_type": "date"}}],
            "query": {"bool": {"must": [{"query_string": {"query": query}}],
                               "filter": [{"range": {"@timestamp": {"gte": f"now-{int(lookback_minutes)}m"}}}]}}}
    r = _os_search(body)
    if "error" in r:
        return r
    hits = []
    for h in r.get("hits", {}).get("hits", []):
        src = json.dumps(h.get("_source", {}))
        if "flagd" in src.lower() or "feature_flag" in src.lower():
            continue
        hits.append(src[:600])
    total = r.get("hits", {}).get("total", {})
    return {"total": total.get("value") if isinstance(total, dict) else total, "hits": hits}


# ----------------------------------------------- container health / history
@mcp.tool()
def container_health() -> dict:
    """State, status, CPU % and memory of every container (docker ps + docker stats).
    'attention' lists containers that are not running, restarting or unhealthy."""
    out, err = _docker(["ps", "-a", "--format", "{{json .}}"])
    if err:
        return {"error": err}
    stats_out, _ = _docker(["stats", "--no-stream", "--format", "{{json .}}"], timeout=45)
    stats = {s.get("Name"): s for s in _json_lines(stats_out)}
    rows, attention = [], []
    for c in _json_lines(out):
        name = c.get("Names", "")
        if _blocked_name(name):
            continue
        s = stats.get(name, {})
        row = {"name": name, "state": c.get("State"), "status": c.get("Status"),
               "cpu": s.get("CPUPerc"), "mem": s.get("MemUsage"), "mem_pct": s.get("MemPerc")}
        rows.append(row)
        status = (c.get("Status") or "").lower()
        if c.get("State") != "running" or "unhealthy" in status or "restarting" in status:
            attention.append(row)
    return {"container_count": len(rows), "attention": attention, "containers": rows}


@mcp.tool()
def deploy_history(lookback_minutes: int = 60) -> dict:
    """What changed recently: container image, created/started/finished times, restart
    count, OOM kills, plus docker lifecycle events (start/die/stop/restart/oom) in the
    window. The demo has no CI/CD system, so this is the available 'deployment history'."""
    ids, err = _docker(["ps", "-aq"])
    if err:
        return {"error": err}
    out, err = _docker(["inspect", *ids.split()], timeout=45)
    if err:
        return {"error": err}
    containers = []
    for c in json.loads(out):
        name = c.get("Name", "").lstrip("/")
        if _blocked_name(name):
            continue
        st = c.get("State", {})
        containers.append({
            "container": name, "image": c.get("Config", {}).get("Image"),
            "created": c.get("Created"), "started_at": st.get("StartedAt"),
            "finished_at": st.get("FinishedAt"), "status": st.get("Status"),
            "restart_count": c.get("RestartCount"), "oom_killed": st.get("OOMKilled"),
            "exit_code": st.get("ExitCode"), "health": (st.get("Health") or {}).get("Status")})
    now = int(time.time())
    ev_out, _ = _docker(["events", "--since", str(now - lookback_minutes * 60), "--until", str(now),
                         "--filter", "type=container", "--format", "{{json .}}"], timeout=45)
    keep = ("start", "die", "stop", "restart", "kill", "oom", "health_status")
    events = []
    for e in _json_lines(ev_out):
        name = e.get("Actor", {}).get("Attributes", {}).get("name", "")
        if _blocked_name(name) or not str(e.get("Action", "")).startswith(keep):
            continue
        events.append({"time": e.get("time"), "container": name, "action": e.get("Action"),
                       "exit_code": e.get("Actor", {}).get("Attributes", {}).get("exitCode")})
    flagged = [c for c in containers if c["restart_count"] or c["oom_killed"] or c["status"] != "running"]
    return {"lookback_minutes": lookback_minutes, "restarted_or_not_running": flagged,
            "events": events[-60:], "containers": containers}


if __name__ == "__main__":
    mcp.run()
