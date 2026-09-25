#!/usr/bin/env python3
"""Example external decision-making agent for Visor ATC (standard library only).

It signs in (use a dedicated controller account, e.g. "agent"), takes a sector, switches the
simulator to lockstep (the simulation only advances when the agent asks), and then loops:
observe -> answer open decision points -> step.

    VISOR_USER=agent VISOR_PASSWORD=... python examples/agent_client.py --sector ALPS-UPPER --steps 120

Replace `choose()` with a call to your decision model (e.g. Jev): each decision point already
carries the situation `state` and typed `questions`; the "action" question lists the candidate
clearances with the predicted outcome of each one.
"""

import argparse
import json
import os
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
TOKEN = None


def call(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST" if body is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def choose(decision):
    """Pick the cheapest option predicted to keep separation; otherwise the least bad one."""
    options = next(q for q in decision["questions"] if q["id"] == "action")["options"]
    clean = [o for o in options if o["predicted"]["los_duration_s"] == 0]
    if clean:
        best = min(clean, key=lambda o: o["cost"])
        return {"action": best["id"], "confidence": 0.85}
    best = min(options, key=lambda o: (o["predicted"]["los_duration_s"], o["cost"]))
    return {"action": best["id"], "confidence": 0.4}


def main():
    global BASE, TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=BASE)
    ap.add_argument("--user", default=os.environ.get("VISOR_USER"))
    ap.add_argument("--password", default=os.environ.get("VISOR_PASSWORD"))
    ap.add_argument("--sector", default="MUAC-DECO")
    ap.add_argument("--steps", type=int, default=60)
    ap.add_argument("--dt", type=float, default=5.0, help="simulated seconds per step")
    ap.add_argument("--name", default="example")
    args = ap.parse_args()
    BASE = args.url.rstrip("/")
    if not (args.user and args.password):
        ap.error("credentials required: --user/--password or VISOR_USER/VISOR_PASSWORD")
    TOKEN = call("/api/auth/login", {"username": args.user, "password": args.password})["token"]

    call("/api/sector", {"sector": args.sector})
    call("/api/ai", {"mode": "off"})                       # we are the AI now
    call("/api/sim", {"action": "lockstep", "lockstep": True})
    try:
        for i in range(args.steps):
            obs = call("/api/sim", {"action": "step", "dt": args.dt})
            for d in obs["decisions"]:
                answer = choose(d)
                try:
                    res = call("/api/decisions/%s" % d["id"], {"answers": answer, "by": "ai:" + args.name})
                    print("t=%d %s %-13s -> %s" % (obs["t"], d["id"], d["kind"], res["option"]))
                except urllib.error.HTTPError as exc:
                    print("  rejected:", exc.read().decode())
            if i % 12 == 0:
                s = obs["score"]
                print("t=%d aircraft in sector=%d conflicts=%d score=%s" % (
                    obs["t"], sum(a["in_sector"] for a in obs["aircraft"]), len(obs["conflicts"]), s.get("points")))
    finally:
        call("/api/sim", {"action": "lockstep", "lockstep": False})
    print("final score:", call("/api/observation")["score"])


if __name__ == "__main__":
    main()
