"""Reference agent: picks the cheapest option whose fast-time prediction is conflict-free.

It exists so the closed loop (situation -> decision -> clearance -> aircraft reaction) works out of
the box, and as a baseline to compare a learned decision model against. The coach uses the same
ranking (`choose`) as its reference answer when it grades and explains a trainee's decisions.
"""

from .base import Agent

URGENCY = [(60, "critical"), (120, "high"), (180, "medium")]


def safe(o):
    return o["predicted"]["los_duration_s"] == 0


def margin(o):
    p = o["predicted"]
    return (p["min_h_nm"] or 99.0) + (p["min_v_ft"] or 0) / 1000.0


def choose(options):
    """(best option, confidence) from options carrying "predicted" and "cost"; (None, 0) if empty."""
    if not options:
        return None, 0.0
    candidates = [o for o in options if safe(o)]
    if candidates:
        best = min(candidates, key=lambda o: (o["cost"], -margin(o)))
        return best, (0.9 if best["cost"] <= 1.0 else 0.75)
    # nothing is fully clean: shortest time below minima, then latest onset
    best = min(options, key=lambda o: (o["predicted"]["los_duration_s"],
                                       -(o["predicted"]["first_los_s"] or 1e9), o["cost"]))
    return best, 0.4


class RuleAgent(Agent):
    name = "rules"

    def decide(self, decision):
        action_q = next(q for q in decision["questions"] if q["id"] == "action")
        options = action_q["options"]
        best, confidence = choose(options)
        if best is None:
            return None
        answer = {"action": best["id"], "confidence": confidence}
        if decision["kind"] == "conflict":
            t_to = decision["state"]["conflict"]["time_to_conflict_s"]
            answer["urgency"] = next((label for limit, label in URGENCY if t_to <= limit), "low")
            nothing = next((o for o in options if not o["clearances"]), None)
            answer["will_lose_separation"] = 0.95 if nothing and nothing["predicted"]["los"] else 0.3
        return answer
