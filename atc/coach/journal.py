"""The per-engine coach: a journal of every decision a human controller faces.

When a decision point (conflict or pilot request) opens in a controller's sector, the coach opens
an entry: the situation, the options with their predictions, the AI's answer and the "no action"
baseline. It then follows what the controller does (clearances on the aircraft involved, answers
in the Decisions list), what happens (closest separation, any loss) and the hints used, and grades
the entry when the situation is over (grading.py). Graded entries are stored per session
(store.py) and reported back to the controller according to their coach level:

    off          nothing reported; the decision assistant shows every option (as before)
    evaluate     each decision is graded; no options, no hints (exam conditions)
    hints        graded; hints on request (H) or automatically when a situation lingers
    advise       graded; every option with its prediction and explanation, plus the AI's advice
    demonstrate  the AI resolves the controller's situations and explains each move (not graded)

All methods are called with the engine lock held.
"""

import collections
import itertools

from .. import config, conflicts, decisions
from ..errors import Invalid
from ..geo import distance_nm
from . import DEFAULT_LEVEL, LEVELS, grading
from .text import msg

MAX_ENTRY_S = 900.0          # grade a situation after 15 min at the latest
CLEAR_FOR_S = 30.0           # a conflict is over once the pair has been out of conflict this long
REQUEST_SETTLE_S = 30.0      # watch a granted request this long for a resulting conflict
EARLY_LOOKAHEAD_S = 360.0    # conflicts are followed from this far ahead (STCA warns at 120 s)
VERTICAL_OK_FT = 950.0       # aircraft levelling off approach a level asymptotically: 1000 ft - noise
LEVEL_ORDER = {name: i for i, name in enumerate(LEVELS)}


def _compact_pred(p):
    return {k: p.get(k) for k in ("los_duration_s", "first_los_s", "min_h_nm", "min_v_ft")}


def _compact_option(o):
    return {"id": o["id"], "label": o["label"], "role": o.get("role"), "cost": o["cost"],
            "predicted": _compact_pred(o["predicted"]), "why": o.get("why")}


class Coach:
    def __init__(self, engine, store=None, context="live", exercise=None):
        self.engine = engine
        self.store = store                  # CoachStore, or None: nothing is persisted
        self.context = context              # live | tutorial | exercise
        self.exercise = exercise
        self.levels = {}                    # username -> coach level
        self.sessions = {}                  # username -> session id
        self.open = {}                      # entry id -> entry
        self.closed = collections.deque(maxlen=300)
        self._by_dp = {}                    # (decision id, holder) -> entry id
        self._by_pair = {}                  # ((a, b), holder) -> entry id of a conflict
        self._early_keys = set()            # pairs in conflict within EARLY_LOOKAHEAD_S (last scan)
        self._ids = itertools.count(1)
        self.via = None                     # set while a decision answer issues its clearances
        self.recording = [] if context != "live" else None
        self._last_rec = -1e9

    def reset(self):
        """The scenario changed (reset, rewind, sleep): open situations no longer exist."""
        self.open.clear()
        self._by_dp.clear()
        self._by_pair.clear()
        self._early_keys = set()

    # ------------------------------------------------------------------ levels & sessions
    def level_of(self, username):
        return self.levels.get(username, DEFAULT_LEVEL)

    def set_level(self, username, level):
        if level not in LEVELS:
            raise Invalid("coach_level_invalid", levels=", ".join(LEVELS))
        self.levels[username] = level

    def at_least(self, username, level):
        return LEVEL_ORDER[self.level_of(username)] >= LEVEL_ORDER[level]

    def reveal_for(self, username, dp):
        """Which options of dp the user's Decisions list shows: None = all of them."""
        if self.level_of(username) not in ("evaluate", "hints"):
            return None
        eid = self._by_dp.get((dp.id, "human:" + username))
        e = self.open.get(eid) if eid else None
        return set(e["revealed"]) if e else set()

    def begin(self, username):
        """Start the user's session now (sandboxes), so a debrief exists even without decisions."""
        return self.session_for(username)

    def session_for(self, username):
        sid = self.sessions.get(username)
        if sid is None and self.store is not None:
            if self.context == "live":
                sid = self.store.live_session(username, self.engine.t)
            else:
                sid = self.store.start_session(username, self.context, self.exercise, sim_t0=self.engine.t)
            self.sessions[username] = sid
        return sid

    # ------------------------------------------------------------------ recording (sandboxes)
    def record(self, rows, conflicts):
        """Keep the traffic every 5 simulated seconds for the debrief's 3D replay."""
        if self.recording is None or self.engine.t - self._last_rec < 5.0:
            return
        self._last_rec = self.engine.t
        self.recording.append({"t": round(self.engine.t, 1), "ac": rows,
                               "conflicts": [[c["a"], c["b"], c["kind"]] for c in conflicts]})
        if len(self.recording) > 4000:           # ~5.5 h of simulated time
            del self.recording[:len(self.recording) - 4000]

    # ------------------------------------------------------------------ journal: opening
    def _new_entry(self, holder, kind, subj, sector_ids, situation, minima, dp_id=None):
        user = holder[6:]
        eid = "E%d" % next(self._ids)
        e = {
            "id": eid, "session": self.session_for(user), "holder": holder, "user": user,
            "context": self.context, "exercise": self.exercise,
            "kind": kind, "dp": dp_id, "subjects": [a.id for a in subj],
            "callsigns": [a.callsign for a in subj], "sectors": list(sector_ids),
            "opened_t": self.engine.t, "closed_t": None, "situation": situation,
            "aircraft": [decisions.ac_state(a) for a in subj], "minima": minima,
            "focus": {"lat": round(sum(a.lat for a in subj) / len(subj), 4),
                      "lon": round(sum(a.lon for a in subj) / len(subj), 4)},
            "actions": [], "response": None, "hint_tier": 0, "hints": [], "revealed": [],
            "options": [], "best": None, "early": dp_id is None,
            "actual": {"min_h": None, "min_v": None, "cpa_h": None, "cpa_v": None,
                       "los": False, "los_t": None, "los_h": None, "los_v": None},
            "by_ai": False, "outcome": None, "grade": None, "clear_since": None,
        }
        self.open[eid] = e
        if kind == "conflict":
            self._by_pair[(tuple(sorted(e["subjects"])), holder)] = eid
        return e

    def on_decision(self, dp):
        e_ = self.engine
        for holder in dp.holders:
            if not holder.startswith("human:") or (dp.id, holder) in self._by_dp:
                continue
            subj = [e_.aircraft[i] for i in dp.subject_ids if i in e_.aircraft]
            if not subj:
                continue
            # a conflict the coach already follows since it was detected early: same situation
            eid = self._by_pair.get((tuple(sorted(dp.subject_ids)), holder)) if dp.kind == "conflict" else None
            e = self.open.get(eid) if eid else None
            if e is None or e["dp"] is not None:
                e = self._new_entry(holder, dp.kind, subj, dp.sector_ids,
                                    {k: dp.state[k] for k in ("conflict", "request") if k in dp.state},
                                    dp.state.get("separation_minima", {}).get("h_nm", config.SEP_H_NM), dp.id)
                e["requested_fl"] = getattr(dp, "requested_fl", None)
            else:
                e["dp"] = dp.id
                e["sectors"] = list(dp.sector_ids)
            e.setdefault("alerted_t", e_.t)         # when the controller was alerted
            self._refresh(e, dp)
            self._by_dp[(dp.id, holder)] = e["id"]

    def _detect_early(self):
        """Follow conflicts in human-held sectors from ~6 min ahead, well before the STCA (2 min),
        so that a controller who resolves one early gets the credit for it."""
        e_ = self.engine
        held = {sid: h for sid, h in e_.control.snapshot().items() if h.startswith("human:")}
        self._early_keys = set()
        if not held:
            return
        pool = [a for a in e_.aircraft.values() if a.alt >= 4000.0 and a.sector_id in held]
        near = {a.id for a in pool}
        if not pool:
            return
        lat0, lat1 = min(a.lat for a in pool) - 1.5, max(a.lat for a in pool) + 1.5
        lon0, lon1 = min(a.lon for a in pool) - 2.0, max(a.lon for a in pool) + 2.0
        pool += [a for a in e_.aircraft.values() if a.id not in near and a.alt >= 4000.0
                 and lat0 <= a.lat <= lat1 and lon0 <= a.lon <= lon1]
        for c in conflicts.detect(pool, lookahead=EARLY_LOOKAHEAD_S):
            key = (c["a"], c["b"])
            self._early_keys.add(key)
            if key in e_._conflict_keys:
                continue                                # already an STCA: the decision point opens it
            a, b = e_.aircraft[c["a"]], e_.aircraft[c["b"]]
            for holder in {held.get(a.sector_id), held.get(b.sector_id)} - {None}:
                if (key, holder) in self._by_pair and self._by_pair[(key, holder)] in self.open:
                    continue
                e = self._new_entry(holder, "conflict", [a, b], sorted({a.sector_id, b.sector_id} - {None}),
                                    {"conflict": {"type": "MTCD", "time_to_conflict_s": c["t_to"],
                                                  "predicted_h_nm": c["h_nm"], "predicted_v_ft": c["v_ft"]}},
                                    conflicts.sep_h(a.alt, b.alt))
                e["baseline_first_los"], e["baseline_t"] = c["t_to"], e_.t
                e["baseline_min_h"] = c["h_nm"]

    def _refresh(self, e, dp):
        """Options, the AI's answer and the no-action baseline, as of the latest prediction."""
        if e["actions"] or e["response"] or e["actual"]["los"]:
            return                                  # judge against what was known in time
        e["options"] = [_compact_option(o) for o in dp.options]
        best = next((o for o in dp.options if o["id"] == dp.best), None)
        # keep the last answer that kept separation: once it is lost, every option "fails"
        if best is not None and (e["best"] is None or best["predicted"]["los_duration_s"] == 0):
            e["best"] = _compact_option(best)
        if dp.kind == "conflict":
            mon = next((o for o in dp.options if o.get("role") == "monitor"), None)
            e["baseline_first_los"] = mon["predicted"]["first_los_s"] if mon else None
            e["baseline_min_h"] = mon["predicted"]["min_h_nm"] if mon else None
            e["baseline_t"] = dp.updated_t
        else:
            approve = next((o for o in dp.options if o.get("role") == "approve"), None)
            e["approve_safe"] = bool(approve and approve["predicted"]["los_duration_s"] == 0)

    def sync(self, dp):
        """dp's controllers changed: open journals for new ones, close the others' (ungraded)."""
        for (dp_id, holder), eid in list(self._by_dp.items()):
            if dp_id == dp.id and holder not in dp.holders and eid in self.open:
                self._close(self.open[eid], "handed_over")
        self.on_decision(dp)

    def on_refresh(self, dp):
        for holder in dp.holders:
            eid = self._by_dp.get((dp.id, holder))
            if eid in self.open:
                self._refresh(self.open[eid], dp)

    # ------------------------------------------------------------------ journal: actions
    def on_clearance(self, ac, accepted, issuer, actor):
        """Clearances were issued to ac: attach them to the open entries it is part of."""
        if not accepted:
            return
        e_ = self.engine
        who = actor or issuer
        for e in list(self.open.values()):
            if ac.id not in e["subjects"]:
                continue
            if issuer.startswith("ai:") and actor is None:
                e["by_ai"] = True                   # the engine's AI handled it (demonstration)
                continue
            if who != e["holder"]:
                continue
            if e["kind"] == "conflict" and e["clear_since"] is not None and e["actions"]:
                continue                            # they solved it already: a later clearance is another matter
            subj = [e_.aircraft[i] for i in e["subjects"] if i in e_.aircraft]
            pred = decisions.predict(e_, subj, [], e_.t)
            clrs = [{"kind": c.kind, "value": c.value.get("ident") if isinstance(c.value, dict) else c.value,
                     "direction": c.direction} for c in accepted]
            cost = sum(grading.clearance_cost(c.kind, c.value if c.kind != "DIRECT" else 0, c.direction,
                                              ac.alt, ac.hdg) for c in accepted)
            if e["kind"] == "conflict":
                base = e.get("baseline_first_los")
                lead = None if base is None else base - (e_.t - e.get("baseline_t", e["opened_t"]))
                e["actions"].append({"t": e_.t, "callsign": ac.callsign, "clearances": clrs,
                                     "phrases": [c.phrase() for c in accepted], "cost": round(cost, 2),
                                     "lead_s": None if lead is None else round(lead), "via": self.via or "clearance",
                                     "predicted": _compact_pred(pred)})
            elif e["response"] is None:
                e["response"] = {"t": e_.t, "role": self._request_role(e, accepted),
                                 "phrases": [c.phrase() for c in accepted], "via": self.via or "clearance",
                                 "los_predicted": pred["los"],
                                 "other": next((e_.aircraft[i].callsign for i in pred["conflicts_with"]
                                                if i in e_.aircraft and i != ac.id), None)}

    @staticmethod
    def _request_role(e, accepted):
        if e["kind"] == "route_request":
            return "approve" if any(c.kind == "RESUME" for c in accepted) else "other"
        want = e.get("requested_fl")
        for c in accepted:
            if c.kind in ("CLIMB", "DESCEND") and want is not None:
                if abs(c.value - want) < 10:
                    return "approve"
                return "intermediate"
        return "other"

    def on_answer(self, dp, opt, by, actor):
        """An option was chosen in the Decisions list; options without clearances are decisions too."""
        if opt["clearances"]:
            return                                  # recorded through on_clearance
        holder = actor or by
        eid = self._by_dp.get((dp.id, holder))
        e = self.open.get(eid) if eid else None
        if e is None:
            if by.startswith("ai:"):
                for h in dp.holders:
                    x = self.open.get(self._by_dp.get((dp.id, h)))
                    if x is not None:
                        x["by_ai"] = True
            return
        if e["kind"] != "conflict" and e["response"] is None:
            e["response"] = {"t": self.engine.t, "role": opt.get("role") or "deny", "phrases": [],
                             "via": "decision", "los_predicted": False, "other": None}
        elif e["kind"] == "conflict":
            e["monitor_chosen"] = True

    # ------------------------------------------------------------------ journal: following up
    def on_scan(self):
        """After every separation scan: follow open situations, hint, and grade finished ones."""
        e_ = self.engine
        t = e_.t
        self._detect_early()
        for e in list(self.open.values()):
            subj = [e_.aircraft.get(i) for i in e["subjects"]]
            if any(a is None for a in subj):
                self._close(e, "left")
                continue
            if e["kind"] == "conflict":
                a, b = subj
                h = distance_nm(a.lat, a.lon, b.lat, b.lon)
                v = abs(a.alt - b.alt)
                act = e["actual"]
                if v < VERTICAL_OK_FT and (act["min_h"] is None or h < act["min_h"]):
                    act["min_h"], act["min_v"] = round(h, 1), round(v)
                if act["cpa_h"] is None or h < act["cpa_h"]:
                    act["cpa_h"], act["cpa_v"] = round(h, 1), round(v)     # closest, at any level
                if h < e["minima"] and v < config.LOS_V_FT and not act["los"]:
                    act.update(los=True, los_t=t, los_h=round(h, 1), los_v=round(v))
                key = tuple(sorted(e["subjects"]))
                diverging = e.get("last_h") is not None and h > e["last_h"]
                e["last_h"] = h
                if key in e_._conflict_keys or key in self._early_keys:
                    e["clear_since"] = None
                elif e["clear_since"] is None:
                    e["clear_since"] = t
                elif t - e["clear_since"] >= CLEAR_FOR_S and diverging:
                    # out of conflict and past each other: the closest approach is known
                    self._close(e, "los" if act["los"] else "resolved")
                    continue
            else:
                ac = subj[0]
                if any(ac.id in (c["a"], c["b"]) and c["kind"] == "LOS" for c in e_.conflicts):
                    if not e["actual"]["los"]:
                        e["actual"].update(los=True, los_t=t)
                dp = e_.decisions.get(e["dp"])
                done = dp is None or dp.status != "open"
                if e["response"] is not None and t - e["response"]["t"] >= REQUEST_SETTLE_S and done:
                    self._close(e, "answered")
                    continue
                if done and e["response"] is None and (dp is None or dp.status == "expired"):
                    self._close(e, "expired")
                    continue
            if t - e["opened_t"] > MAX_ENTRY_S:
                self._close(e, "timeout")
                continue
            self._auto_hint(e)

    def _auto_hint(self, e):
        """At the Hints level, point at a situation the controller hasn't acted on yet."""
        if e["hint_tier"] or e["actions"] or e["response"] or e["by_ai"]:
            return
        if self.level_of(e["user"]) != "hints":
            return
        if e["dp"] is None:
            return                                  # not alerted yet: nothing to hint at
        t = self.engine.t
        if e["kind"] == "conflict":
            base = e.get("baseline_first_los")
            remaining = 120.0 if base is None else base - (t - e.get("baseline_t", e["opened_t"]))
            due = remaining < 105.0                 # about 15 s after the alert
        else:
            due = t - e["opened_t"] >= 20.0
        if due:
            self._give_hint(e, auto=True)

    # ------------------------------------------------------------------ grading
    def _close(self, e, outcome):
        e_ = self.engine
        e["closed_t"] = e_.t
        e["outcome"] = outcome
        self.open.pop(e["id"], None)
        self._by_dp.pop((e["dp"], e["holder"]), None)
        if e["kind"] == "conflict":
            self._by_pair.pop((tuple(sorted(e["subjects"])), e["holder"]), None)
        if e["dp"] is None and not e["actions"] and not e["actual"]["los"]:
            return                                  # predicted early, then solved itself: not a situation
        if outcome == "handed_over":
            e["grade"] = {"score": None, "letter": None, "feedback": None, "parts": {}, "competencies": []}
        elif e["by_ai"] and not e["actions"] and not e["response"]:
            e["grade"] = {"score": None, "letter": None, "feedback": msg("fb.demo"), "parts": {},
                          "competencies": []}
        else:
            e["grade"] = grading.grade(e)
        self.closed.append(e)
        if self.store is not None and e.get("session"):
            e["db_id"] = self.store.add_entry(e["session"], e)
            if self.context == "live":
                self.store.touch(e["session"], e_.t)
        if self.level_of(e["user"]) != "off" and e["grade"]["feedback"] is not None:
            g = e["grade"]
            head = ("%s %d · " % (g["letter"], g["score"])) if g["score"] is not None else ""
            e_._event("coach", head + g["feedback"]["text"], speaker="COACH", callsign=e["callsigns"][0],
                      to=e["holder"], msg=g["feedback"], grade=g["letter"])

    # ------------------------------------------------------------------ hints
    def hint(self, username, dp_id):
        """Next hint for the user's decision point: tier 1 where, 2 what, 3 how."""
        eid = self._by_dp.get((dp_id, "human:" + username))
        e = self.open.get(eid) if eid else None
        if e is None:
            raise Invalid("no_open_situation", 404, id=dp_id)
        return self._give_hint(e)

    def _give_hint(self, e, auto=False):
        e_ = self.engine
        tier = min(3, e["hint_tier"] + 1)
        dp = e_.decisions.get(e["dp"])
        subj = [e_.aircraft.get(i) for i in e["subjects"]]
        cs = e["callsigns"]
        if tier == 1:
            if e["kind"] == "conflict":
                m = msg("hint.c1", a=cs[0], b=cs[1])
            elif e["kind"] == "level_request":
                m = msg("hint.r1_level", a=cs[0], fl=e.get("requested_fl"))
            else:
                m = msg("hint.r1_route", a=cs[0])
        elif tier == 2:
            if e["kind"] == "conflict" and all(subj):
                mon = next((o for o in (dp.options if dp else []) if o.get("role") == "monitor"), None)
                p = mon["predicted"] if mon else {}
                if p.get("los"):
                    m = msg("hint.c2", a=cs[0], b=cs[1], fa=int(round(subj[0].alt / 100.0)),
                            fb=int(round(subj[1].alt / 100.0)), h=(p.get("cpa") or {}).get("h", p.get("min_h_nm")),
                            t=p.get("first_los_s"), min=e["minima"])
                else:
                    m = msg("hint.c2_clear", a=cs[0], b=cs[1], h=p.get("min_h_nm") or (p.get("cpa") or {}).get("h"))
            else:
                approve = next((o for o in (dp.options if dp else []) if o.get("role") == "approve"), None)
                if approve is not None and approve["predicted"]["los_duration_s"] > 0:
                    other = next((e_.aircraft[i].callsign for i in approve["predicted"]["conflicts_with"]
                                  if i in e_.aircraft and i not in e["subjects"]), "traffic")
                    m = msg("hint.r2_bad", other=other)
                else:
                    m = msg("hint.r2_ok")
        else:
            best = next((o for o in (dp.options if dp else []) if o["id"] == dp.best), None) if dp else None
            if best is None:
                best = e.get("best")
            if best is None:
                m = msg("hint.c1", a=cs[0], b=cs[-1])
            else:
                m = msg("hint.solution", option=best["label"], why=best.get("why") or msg("why.approve_ok"))
                e["revealed"] = [best["id"]]
        e["hint_tier"] = tier
        e["hints"].append({"tier": tier, "t": e_.t, "auto": auto, "msg": m})
        live = [a for a in subj if a is not None]
        focus = ({"lat": sum(a.lat for a in live) / len(live), "lon": sum(a.lon for a in live) / len(live)}
                 if live else e["focus"])
        return {"decision": e["dp"], "tier": tier, "auto": auto, "message": m, "focus": focus,
                "aircraft": e["subjects"], "revealed": e["revealed"]}

    # ------------------------------------------------------------------ demonstration
    def narrate(self, dp, opt):
        """The AI just acted on dp for a human in Demonstrate mode: say what and why."""
        for holder in dp.holders:
            if holder.startswith("human:") and self.level_of(holder[6:]) == "demonstrate":
                m = msg("narr.act", option=opt["label"], why=opt.get("why") or msg("why.approve_ok"))
                self.engine._event("coach", m["text"], speaker="COACH", to=holder, msg=m,
                                   sectors=dp.sector_ids)

    # ------------------------------------------------------------------ views
    def view(self, username):
        """Coach part of the user's frame: level, hints on open situations, latest grades."""
        holder = "human:" + username
        opened = [{"dp": e["dp"], "tier": e["hint_tier"], "hint": e["hints"][-1]["msg"] if e["hints"] else None,
                   "auto": bool(e["hints"] and e["hints"][-1]["auto"]), "revealed": e["revealed"]}
                  for e in self.open.values() if e["holder"] == holder and e["dp"] is not None]
        graded = [self.brief(e) for e in reversed(self.closed) if e["holder"] == holder][:6]
        return {"level": self.level_of(username), "open": opened, "graded": graded,
                "session": self.sessions.get(username)}

    @staticmethod
    def brief(e):
        g = e["grade"] or {}
        return {"id": e["id"], "dp": e["dp"], "kind": e["kind"], "callsigns": e["callsigns"],
                "t": e["closed_t"], "score": g.get("score"), "letter": g.get("letter"),
                "feedback": g.get("feedback"), "outcome": e["outcome"]}

    def entries_of(self, username):
        holder = "human:" + username
        return [e for e in self.closed if e["holder"] == holder]

    def finish(self, username=None):
        """Grade whatever is still open (end of an exercise or of a sandbox)."""
        for e in list(self.open.values()):
            if username is None or e["user"] == username:
                self._close(e, "ended")


def summarize(entries, score=None):
    """Session summary from graded entries (+ the engine's score counters, if given)."""
    graded = [e for e in entries if (e.get("grade") or {}).get("score") is not None]
    avg = round(sum(e["grade"]["score"] for e in graded) / len(graded)) if graded else None
    comp = collections.defaultdict(list)
    for e in graded:
        for c in e["grade"].get("competencies", []):
            comp[c].append(e["grade"]["score"])
    parts = collections.defaultdict(list)
    for e in graded:
        for k, v in e["grade"]["parts"].items():
            parts[k].append(v)
    out = {
        "decisions": len(entries), "graded": len(graded),
        "average": avg, "letter": grading.letter(avg) if avg is not None else None,
        "los": sum(1 for e in entries if (e.get("actual") or {}).get("los")),
        "hints": sum(e.get("hint_tier", 0) for e in entries),
        "demonstrated": sum(1 for e in entries if e.get("by_ai")),
        "competencies": {k: round(sum(v) / len(v)) for k, v in comp.items()},
        "parts": {k: round(sum(v) / len(v), 1) for k, v in parts.items()},
    }
    if score:
        out["score"] = score
    out["passed"] = bool(graded) and out["los"] == 0 and (avg or 0) >= 55
    return out
