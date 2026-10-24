#!/usr/bin/env python3
"""Human control CLI. The agent must never run this.

  python tools/ictl.py approve <incident_id>   show the dry-run bundle, type APPROVE to allow it
  python tools/ictl.py reject  <incident_id>
  python tools/ictl.py kill on|off|status      emergency stop for all agent actions
  python tools/ictl.py probe                   read-only discovery of the Action API
  python tools/ictl.py status [incident_id]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import action_core as core  # noqa: E402


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1]
    if cmd in ("approve", "reject"):
        iid = argv[2]
        if cmd == "reject":
            print(core.reject(iid))
            return 0
        p = core.APPROVALS / f"{iid}.pending.json"
        if not p.exists():
            print("No pending dry run for", iid)
            return 1
        pend = json.loads(p.read_text())
        print(json.dumps({k: pend[k] for k in ("bundle", "reason", "request_preview",
                                                "rollback_request_preview", "dry_run_result",
                                                "config_verified")}, indent=2))
        print("hash:", pend["hash"][:12], "| kill switch:", "ON" if core.kill_switch_on() else "off")
        if not sys.stdin.isatty():
            print("Refusing: approval needs an interactive terminal (human only).")
            return 2
        if input("Type APPROVE to allow exactly this action and its rollback: ").strip() != "APPROVE":
            print("Not approved.")
            return 1
        print(core.approve(iid))
        return 0
    if cmd == "kill":
        mode = argv[2] if len(argv) > 2 else "status"
        if mode in ("on", "off"):
            core.set_kill_switch(mode == "on")
        print("KILL SWITCH:", "ON (all agent actions blocked)" if core.kill_switch_on() else "off")
        return 0
    if cmd == "probe":
        print(json.dumps(core.probe(), indent=2))
        return 0
    if cmd == "status":
        print(json.dumps(core.status(argv[2] if len(argv) > 2 else ""), indent=2))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
