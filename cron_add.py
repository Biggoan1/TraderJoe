#!/usr/bin/env python3
"""List Hermes cron jobs; with --add, append the fastre-trader job (9:35 ET wkdays).
Run with the gateway STOPPED."""
import json, sys, uuid
from datetime import datetime, timezone, timedelta

P = "/root/hermes-trader/.hermes-profile/cron/jobs.json"
data = json.load(open(P))
for j in data["jobs"]:
    print(f'{j["name"]:34} {j["schedule"]["expr"]:20} enabled={j["enabled"]} state={j["state"]} script={j["script"]}')

if "--add" in sys.argv:
    if any(j["name"] == "fastre-trader" for j in data["jobs"]):
        print("fastre-trader already present; not adding")
        sys.exit(0)
    tmpl = next(j for j in data["jobs"] if j["name"] == "challenger-shadow")
    job = json.loads(json.dumps(tmpl))
    job.update({
        "id": uuid.uuid4().hex[:12],
        "name": "fastre-trader",
        "script": "traderjoe_fastre_trader.sh",
        "schedule": {"kind": "cron", "expr": "35 9 * * 1-5", "display": "35 9 * * 1-5"},
        "schedule_display": "35 9 * * 1-5",
        "repeat": {"times": None, "completed": 0},
        "enabled": True, "state": "scheduled",
        "paused_at": None, "paused_reason": None,
        "created_at": datetime.now(timezone(timedelta(hours=-4))).isoformat(),
        "next_run_at": None, "last_run_at": None,
        "last_status": None, "last_error": None, "fire_claim": None,
    })
    data["jobs"].append(job)
    json.dump(data, open(P, "w"), indent=1)
    print(f'ADDED fastre-trader (id {job["id"]}) 35 9 * * 1-5 -> {job["script"]}')
