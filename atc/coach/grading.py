"""Scores one decision out of 100: safety (50), timeliness (25) and efficiency (25), minus the
hints used. The reference is the AI's answer (the rules agent's choice), so a trainee who does what
a careful controller would do early scores in the A/B range, and a loss of separation caps the
grade at 20.

Letters: A >= 85, B >= 70, C >= 55, D >= 40, E below.
"""

from .text import msg

HINT_PENALTY = {0: 0, 1: 3, 2: 7, 3: 15}
COMPETENCIES = {
    "conflict": ["situation_awareness", "separation", "decision"],
    "level_request": ["traffic_management", "decision", "communication"],
    "route_request": ["traffic_management", "decision", "communication"],
}


def letter(score):
    for limit, name in ((85, "A"), (70, "B"), (55, "C"), (40, "D")):
        if score >= limit:
            return name
    return "E"


def clearance_cost(kind, value, direction, alt_ft, hdg):
    """Size of a clearance on the same scale as decision option costs (1000 ft = 1, 30° = 2)."""
    if kind in ("CLIMB", "DESCEND"):
        steps = abs(value * 100 - alt_ft) / 1000.0
        return 1.0 if steps <= 1.2 else (1.5 if steps <= 2.2 else 2.0)
    if kind == "TURN":
        return min(3.0, 2.0 * value / 30.0)
    if kind == "HEADING":
        diff = abs((value - hdg + 540) % 360 - 180)
        return min(3.0, 2.0 * diff / 30.0)
    if kind == "DIRECT":
        return 1.5
    if kind == "SPEED":
        return 1.0
    return 0.5          # RESUME


def _timeliness(lead):
    if lead is None:
        return 25
    for limit, pts in ((120, 25), (90, 22), (60, 17), (30, 10)):
        if lead >= limit:
            return pts
    return 5 if lead > 0 else 0


def grade_conflict(e):
    """e: journal entry dict (see journal.Entry.data)."""
    acts = e["actions"]
    actual = e["actual"]
    best = e.get("best") or {}
    minima = e.get("minima", 5.0)
    los = actual["los"]
    n = sum(len(a["clearances"]) for a in acts)
    lead = acts[0]["lead_s"] if acts else None
    unneeded = bool(acts) and best.get("role") == "monitor" and e.get("baseline_first_los") is None

    if los:
        safety = 0
    elif actual["min_h"] is None or (actual["min_v"] or 0) >= 950:
        safety = 50
    else:
        safety = round(50 * max(0.6, min(1.0, 0.6 + 0.2 * (actual["min_h"] - minima))))
    if acts:
        timeliness = _timeliness(lead)
        you = sum(a["cost"] for a in acts)
        eff = 25 - 8 * max(0.0, you - best.get("cost", 1.0)) - 3 * max(0, n - 1) - (8 if unneeded else 0)
        efficiency = round(max(0.0, min(25.0, eff)))
    else:
        timeliness = 0 if los else 25
        efficiency = 0 if los else 25
    penalty = HINT_PENALTY[min(3, e.get("hint_tier", 0))]
    score = max(0, safety + timeliness + efficiency - penalty)
    if los:
        score = min(score, 20)

    alerted = e.get("alerted_t") or e["opened_t"]
    if los and not acts:
        fb = msg("fb.no_action_los", best=best.get("label", "a level change"))
    elif los:
        fb = msg("fb.los", h=actual["los_h"], v=actual["los_v"], lead=max(0, round(actual["los_t"] - alerted)))
    elif acts and safety < 45:
        fb = msg("fb.tight", h=actual["min_h"], v=actual["min_v"])       # the tightest moment
    elif acts and lead is not None and lead < 60:
        fb = msg("fb.late", lead=round(lead))
    elif unneeded:
        fb = msg("fb.unneeded", h=e.get("baseline_min_h") or 5)
    elif acts and sum(a["cost"] for a in acts) > best.get("cost", 1.0) + 0.25 and best.get("label"):
        fb = msg("fb.costly", best=best["label"])
    elif n > 1:
        fb = msg("fb.extra", n=n)
    elif acts:
        ahead = round(lead) if lead is not None else round(acts[0]["t"] - e["opened_t"])
        vertical = (actual.get("cpa_v") or 0) >= 900 or actual["min_h"] is None
        fb = msg("fb.good_v", lead=ahead) if vertical else msg("fb.good_h", lead=ahead, h=actual["min_h"])
    else:
        fb = msg("fb.monitor_good")
    return {"score": score, "letter": letter(score), "feedback": fb,
            "parts": {"safety": safety, "timeliness": timeliness, "efficiency": efficiency, "hints": -penalty}}


def grade_request(e):
    resp = e.get("response")
    los = e["actual"]["los"]
    approve_safe = e.get("approve_safe", True)
    if resp is None:
        safety, timeliness, efficiency = (0 if los else 50), 0, 0
        fb = msg("fb.req_expired")
    else:
        rt = max(0.0, resp["t"] - e["opened_t"])
        unsafe = los or resp.get("los_predicted", False)
        safety = 0 if unsafe else 50
        timeliness = next((pts for limit, pts in ((30, 25), (60, 20), (120, 12)) if rt <= limit), 5)
        role = resp["role"]
        if approve_safe:
            efficiency = {"approve": 25, "intermediate": 18, "deny": 8}.get(role, 15)
        else:
            efficiency = {"approve": 0, "intermediate": 25, "deny": 25}.get(role, 15)
        if unsafe:
            fb = msg("fb.req_unsafe", other=resp.get("other") or "traffic")
        elif role == "deny":
            fb = msg("fb.req_denied_bad") if approve_safe else msg("fb.req_denied_ok")
        elif role == "intermediate":
            fb = msg("fb.req_partial") if approve_safe else msg("fb.req_denied_ok")
        elif rt > 60:
            fb = msg("fb.req_slow", rt=round(rt))
        else:
            fb = msg("fb.req_granted", rt=round(rt))
    penalty = HINT_PENALTY[min(3, e.get("hint_tier", 0))]
    score = max(0, safety + timeliness + efficiency - penalty)
    if los:
        score = min(score, 20)
    return {"score": score, "letter": letter(score), "feedback": fb,
            "parts": {"safety": safety, "timeliness": timeliness, "efficiency": efficiency, "hints": -penalty}}


def grade(e):
    g = grade_conflict(e) if e["kind"] == "conflict" else grade_request(e)
    g["competencies"] = COMPETENCIES.get(e["kind"], [])
    return g
