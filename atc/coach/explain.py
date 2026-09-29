"""Why each option of a decision point is, or isn't, the answer.

The reference answer is the rules agent's choice (agents.rules.choose): the cheapest option whose
fast-time prediction keeps separation, preferring the widest margin. Every option gets a short
explanation built from its own prediction and from how it compares with that answer, so a trainee
(or an agent) sees consequences, not just a recommendation.
"""

from ..agents.rules import choose, safe
from .text import msg


def _name(engine, ac_id):
    ac = engine.aircraft.get(ac_id)
    return ac.callsign if ac is not None else ac_id


def _other(engine, dp, pred):
    """Callsign of the first aircraft an option conflicts with, other than the requester."""
    for i in pred.get("conflicts_with") or []:
        if i not in dp.subject_ids:
            return _name(engine, i)
    return _name(engine, pred["conflicts_with"][0]) if pred.get("conflicts_with") else "traffic"


def _sep(prefix, pred, **extra):
    """<prefix>_h with the horizontal margin, or <prefix>_v when only vertical separation remains."""
    if pred["min_h_nm"] is None:
        return msg(prefix + "_v", **extra)
    return msg(prefix + "_h", h=pred["min_h_nm"], **extra)


def why(dp, o, best, engine):
    pred = o["predicted"]
    role = o.get("role")
    t, d = pred["first_los_s"], pred["los_duration_s"]
    is_best = best is not None and o["id"] == best["id"]
    if dp.kind == "conflict":
        if role == "monitor":
            if not safe(o):
                return msg("why.monitor_bad", t=t)
            if pred["min_h_nm"] is None:
                return msg("why.monitor_ok_v")
            return msg("why.monitor_ok", h=pred["min_h_nm"])
        if not safe(o):
            return msg("why.least_bad", d=d) if is_best else msg("why.unsafe", t=t, d=d)
        if is_best:
            return _sep("why.best", pred)
        if o["cost"] > best["cost"]:
            return msg("why.costlier", best=best["label"])
        # "tighter" only when the margin is actually small (within 3 NM of the minimum)
        mine, ref = pred["min_h_nm"], best["predicted"]["min_h_nm"]
        minimum = dp.state.get("separation_minima", {}).get("h_nm", 5.0)
        if mine is not None and mine < minimum + 3.0 and (ref is None or ref - mine >= 1.0):
            return msg("why.tighter", h=mine)
        return _sep("why.equal", pred)
    # pilot requests: approve / intermediate / deny
    approve = next((x for x in dp.options if x.get("role") == "approve"), None)
    approve_safe = approve is not None and safe(approve)
    if role == "approve":
        return msg("why.approve_ok") if safe(o) else msg("why.approve_bad", other=_other(engine, dp, pred), t=t)
    if role == "intermediate":
        if not safe(o):
            return msg("why.unsafe", t=t, d=d)
        return msg("why.intermediate") if approve_safe else msg("why.intermediate_ok")
    if approve_safe:
        return msg("why.deny_worse")
    return msg("why.deny_ok", other=_other(engine, dp, approve["predicted"]) if approve else "traffic")


def annotate(dp, engine):
    """Set dp.best and each option's "why"."""
    best, _ = choose(dp.options)
    dp.best = best["id"] if best is not None else None
    for o in dp.options:
        o["why"] = why(dp, o, best, engine)
