"""Decision points: situations that need a controller decision, expressed for decision-making AIs.

Each decision point follows the "state + typed questions -> typed answers" shape used by decision
models such as Jev:

    {
      "id": "D12", "kind": "conflict" | "level_request" | "route_request",
      "state":     {...typed description of the situation...},
      "questions": [
        {"id": "action",  "type": "choice", "prompt": "...", "options": [{"id", "label", "clearances", "predicted"}]},
        {"id": "urgency", "type": "score",  "prompt": "...", "scale": ["low", "medium", "high", "critical"]},
        {"id": "will_lose_separation", "type": "probability", "statement": "..."}
      ]
    }

An agent answers with {"action": "<option id>", ...}. Only the "action" answer is executed; the
other questions are informational (logged and shown in the UI) and useful to evaluate a model.

Every option carries the outcome of a fast-time prediction (the involved aircraft and their
neighbours are cloned and flown forward), so an agent can decide from explicit consequences.
"""

import itertools
import math

from . import config
from .clearances import Clearance
from .conflicts import sep_h
from .geo import distance_nm, local_xy_nm

PREDICT_S = 240.0
PREDICT_DT = 10.0
VERTICAL_OK_FT = 950.0   # levelling off approaches a level asymptotically: 999 ft is 1000 ft
_ids = itertools.count(1)


def _fl(alt):
    return int(round(alt / 100.0))


def _level(alt):
    """Nearest 1000 ft flight level (in FL units)."""
    return int(round(alt / 1000.0)) * 10


def ac_state(ac):
    return {
        "id": ac.id, "callsign": ac.callsign, "class": ac.cls, "wake": ac.perf["wake"],
        "lat": round(ac.lat, 4), "lon": round(ac.lon, 4),
        "fl": _fl(ac.alt), "cleared_fl": _fl(ac.cleared_alt) if ac.cleared_alt is not None else None,
        "heading": round(ac.hdg), "gs_kt": round(ac.tas), "vs_fpm": round(ac.vs),
        "lateral": ac.lateral_text(), "controller": ac.controller,
        "sector": getattr(ac, "sector_id", None),
    }


def predict(engine, subjects, option_clearances, t, tracks=False):
    """Fly subjects + neighbours forward with the option applied.

    Returns dict(min_h_nm, min_v_ft, los, first_los_s, los_duration_s, conflicts_with, cpa):
    min_h/min_v describe the tightest geometry among vertically-close pairs; los_duration_s is
    how long (within the horizon) any subject is below minima — 0 means the option is clean.
    cpa is the closest horizontal approach of a subject to any aircraft within 3000 ft
    ({"t", "a", "b", "h", "v"}), which is what the 3D what-if view marks. With tracks=True the
    result also holds "tracks": {aircraft id: [[lat, lon, alt_ft], ...]} every PREDICT_DT from
    now, and "cpa_at": the two positions at the closest approach.
    """
    subj_ids = {a.id for a in subjects}
    pool = {}
    for s in subjects:
        for n in engine.neighbours(s, 80.0, 8000.0):
            pool[n.id] = n
        pool[s.id] = s
    clones = {k: v.clone() for k, v in pool.items()}
    for ac_id, clr in option_clearances:
        c = clones.get(ac_id)
        if c is not None:
            try:
                c.issue(Clearance(clr.kind, clr.value, clr.direction), t, engine.navdata, "predict")
            except ValueError:
                pass
    paths = {k: [[round(c.lat, 4), round(c.lon, 4), round(c.alt)]] for k, c in clones.items()} if tracks else None
    min_h, min_v, los_t, los_s = 1e9, 1e9, None, 0.0
    cpa, cpa_at = None, None
    offenders = set()
    tt = t
    steps = int(PREDICT_S / PREDICT_DT)
    for k in range(steps):
        for c in clones.values():
            c.step(tt, PREDICT_DT)
        tt += PREDICT_DT
        if paths is not None:
            for key, c in clones.items():
                paths[key].append([round(c.lat, 4), round(c.lon, 4), round(c.alt)])
        in_los = False
        for sid in subj_ids:
            a = clones[sid]
            for oid, b in clones.items():
                if oid == sid or (oid in subj_ids and oid < sid):
                    continue
                v = abs(a.alt - b.alt)
                if v > 3000:
                    continue
                x, y = local_xy_nm(a.lat, a.lon, b.lat, b.lon)
                h = math.hypot(x, y)
                if h < sep_h(a.alt, b.alt) and v < config.LOS_V_FT:
                    offenders.add(oid if oid not in subj_ids else sid)
                    in_los = True
                    if los_t is None:
                        los_t = (k + 1) * PREDICT_DT
                # track the tightest geometry among vertically-close pairs
                if v < VERTICAL_OK_FT and h < min_h:
                    min_h, min_v = h, v
                if cpa is None or h < cpa["h"]:
                    cpa = {"t": (k + 1) * PREDICT_DT, "a": sid, "b": oid, "h": round(h, 1), "v": round(v)}
                    if tracks:
                        cpa_at = [[round(a.lat, 4), round(a.lon, 4), round(a.alt)],
                                  [round(b.lat, 4), round(b.lon, 4), round(b.alt)]]
        if in_los:
            los_s += PREDICT_DT
    out = {
        "min_h_nm": round(min_h, 1) if min_h < 1e8 else None,
        "min_v_ft": round(min_v) if min_v < 1e8 else None,
        "los": los_t is not None,
        "first_los_s": los_t,
        "los_duration_s": los_s,
        "conflicts_with": sorted(offenders),
        "cpa": cpa,
    }
    if tracks:
        out["tracks"] = paths
        out["cpa_at"] = cpa_at
        out["step_s"] = PREDICT_DT
    return out


def _vertical_options(ac):
    opts = []
    lvl = _level(ac.alt)
    if abs(ac.vs) > 300:   # in a climb/descent: level off at the next level in that direction
        stop = int(math.ceil(ac.alt / 1000.0)) * 10 if ac.vs > 0 else int(math.floor(ac.alt / 1000.0)) * 10
        kind = "CLIMB" if ac.vs > 0 else "DESCEND"
        opts.append((("stop %s at FL%03d" % ("climb" if ac.vs > 0 else "descent", stop)),
                     Clearance(kind, stop), 1.0))
    for delta, cost in ((10, 1.0), (20, 1.5), (-10, 1.0), (-20, 1.5)):
        fl = lvl + delta
        if fl * 100 > ac.perf["ceiling"] or fl < 50:
            continue
        kind = "CLIMB" if delta > 0 else "DESCEND"
        opts.append(("%s FL%03d" % (kind.lower(), fl), Clearance(kind, fl), cost))
    return opts


def _lateral_options(ac):
    return [("turn %s 30°" % ("left" if d == "L" else "right"), Clearance("TURN", 30, d), 2.0)
            for d in ("L", "R")]


class DecisionPoint:
    def __init__(self, kind, subjects, state, prompt, t):
        self.id = "D%d" % next(_ids)
        self.kind = kind
        self.subject_ids = [s.id for s in subjects]
        self.state = state
        self.prompt = prompt
        self.created_t = t
        self.updated_t = t
        self.options = []          # dicts with id/label/clearances/predicted/cost
        self.status = "open"       # open | executed | expired | dismissed
        self.suggestion = None     # agent answer (advisory mode)
        self.answer = None
        self.answered_by = None
        self.pending_agent = False
        self.key = None
        self.sector_ids = []       # sectors involved (set by the engine)
        self.holders = []          # holder keys responsible for this decision point
        self.best = None           # reference answer (option id), see coach.explain

    def questions(self, reveal=None):
        """reveal: None = every option; otherwise only the option ids in it (a trainee working
        without the decision assistant sees the options a hint has revealed, nothing else)."""
        opt_view = [{
            "id": o["id"], "label": o["label"], "role": o.get("role"),
            "clearances": [{"aircraft": cs, **c.to_dict()} for cs, c in o["clearances_view"]],
            "predicted": o["predicted"], "cost": o["cost"], "why": o.get("why"),
        } for o in self.options if reveal is None or o["id"] in reveal]
        qs = [{"id": "action", "type": "choice", "prompt": self.prompt, "options": opt_view}]
        if self.kind == "conflict":
            qs.append({"id": "urgency", "type": "score",
                       "prompt": "How urgent is this conflict?",
                       "scale": ["low", "medium", "high", "critical"]})
            qs.append({"id": "will_lose_separation", "type": "probability",
                       "statement": "If no action is taken, separation will be lost."})
        return qs

    def to_dict(self, reveal=None):
        """reveal: see questions(). With options hidden the AI's best answer and suggestion are
        hidden too, and "hidden_options" says how many there are."""
        d = {
            "id": self.id, "kind": self.kind, "status": self.status,
            "created_t": self.created_t, "updated_t": self.updated_t,
            "subjects": self.subject_ids, "state": self.state,
            "sectors": self.sector_ids, "holders": self.holders,
            "questions": self.questions(reveal),
            "best": self.best if reveal is None else None,
            "suggestion": self.suggestion if reveal is None else None,
            "answer": self.answer, "answered_by": self.answered_by,
        }
        if reveal is not None:
            d["hidden_options"] = len(self.options) - len(d["questions"][0]["options"])
        return d


def option_role(clearances):
    """What kind of change an option makes: monitor | vertical | lateral | speed | route."""
    kinds = {c.kind for _, c in clearances}
    if not kinds:
        return "monitor"
    if kinds & {"CLIMB", "DESCEND", "LEVEL"}:
        return "vertical"
    if kinds & {"TURN", "HEADING"}:
        return "lateral"
    if kinds == {"SPEED"}:
        return "speed"
    return "route"


def build_options(engine, dp, subjects, raw_options, t):
    """raw_options: [(label, [(aircraft, Clearance)], cost[, role])] -> evaluated option dicts,
    each explained (why it is or isn't the answer) by the coach."""
    from .coach import explain              # late import: the coach package imports this module
    out = []
    for i, raw in enumerate(raw_options):
        label, clrs, cost = raw[:3]
        pred = predict(engine, subjects, [(ac.id, c) for ac, c in clrs], t)
        out.append({
            "id": "o%d" % i, "label": label, "cost": cost,
            "role": raw[3] if len(raw) > 3 else option_role(clrs),
            "clearances": [(ac.id, c) for ac, c in clrs],
            "clearances_view": [(ac.callsign, c) for ac, c in clrs],
            "predicted": pred,
        })
    dp.options = out
    dp.updated_t = t
    explain.annotate(dp, engine)


def conflict_decision(engine, a, b, conflict, t):
    state = {
        "time": t,
        "sectors": sorted({x.sector_id for x in (a, b) if x.sector_id}),
        "conflict": {"type": conflict["kind"], "time_to_conflict_s": conflict["t_to"],
                     "predicted_h_nm": conflict["h_nm"], "predicted_v_ft": conflict["v_ft"],
                     "current_h_nm": round(distance_nm(a.lat, a.lon, b.lat, b.lon), 1),
                     "current_v_ft": round(abs(a.alt - b.alt))},
        "aircraft": [ac_state(a), ac_state(b)],
        "neighbours": [ac_state(n) for n in engine.neighbours(a, 40.0, 5000.0)
                       if n.id not in (a.id, b.id)][:8],
        "separation_minima": {"h_nm": sep_h(a.alt, b.alt), "v_ft": config.SEP_V_FT},
    }
    prompt = ("Choose the clearance that resolves the predicted conflict between %s and %s "
              "with the least disruption." % (a.callsign, b.callsign))
    dp = DecisionPoint("conflict", [a, b], state, prompt, t)
    raw = [("no action (monitor)", [], 0.0)]
    for ac in (a, b):
        if ac.alt < 1500:
            continue
        for label, clr, cost in _vertical_options(ac) + _lateral_options(ac):
            raw.append(("%s %s" % (ac.callsign, label), [(ac, clr)], cost))
    build_options(engine, dp, [a, b], raw, t)
    return dp


def level_request_decision(engine, ac, requested_fl, t):
    cur = _fl(ac.cleared_alt if ac.cleared_alt is not None else ac.alt)
    kind = "CLIMB" if requested_fl > cur else "DESCEND"
    state = {
        "time": t, "sectors": [ac.sector_id] if ac.sector_id else [],
        "request": {"type": kind.lower(), "requested_fl": requested_fl, "current_fl": _fl(ac.alt)},
        "aircraft": [ac_state(ac)],
        "neighbours": [ac_state(n) for n in engine.neighbours(ac, 40.0, 8000.0) if n.id != ac.id][:8],
    }
    prompt = "%s requests %s to FL%03d. Choose a response." % (ac.callsign, kind.lower(), requested_fl)
    dp = DecisionPoint("level_request", [ac], state, prompt, t)
    raw = [("%s approve FL%03d" % (ac.callsign, requested_fl), [(ac, Clearance(kind, requested_fl))], 0.0,
            "approve")]
    mid = (_level(ac.alt) + requested_fl) // 20 * 10
    if mid not in (_level(ac.alt), requested_fl) and abs(mid - _level(ac.alt)) >= 10:
        raw.append(("%s intermediate FL%03d" % (ac.callsign, mid), [(ac, Clearance(kind, mid))], 0.5,
                    "intermediate"))
    raw.append(("deny (maintain level)", [], 1.0, "deny"))
    build_options(engine, dp, [ac], raw, t)
    dp.requested_fl = requested_fl
    return dp


def route_request_decision(engine, ac, t):
    state = {
        "time": t, "sectors": [ac.sector_id] if ac.sector_id else [],
        "request": {"type": "resume_navigation", "on_heading_s": round(t - (ac.hdg_since or t))},
        "aircraft": [ac_state(ac)],
        "neighbours": [ac_state(n) for n in engine.neighbours(ac, 40.0, 5000.0) if n.id != ac.id][:8],
    }
    prompt = "%s requests to resume own navigation. Choose a response." % ac.callsign
    dp = DecisionPoint("route_request", [ac], state, prompt, t)
    raw = [("%s resume own navigation" % ac.callsign, [(ac, Clearance("RESUME"))], 0.0, "approve"),
           ("deny (continue present heading)", [], 1.0, "deny")]
    build_options(engine, dp, [ac], raw, t)
    return dp
