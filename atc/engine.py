"""The simulation engine: time, traffic life-cycle, clearances, monitoring, decisions and scoring.

Threading: a dedicated thread advances the simulation in real time. Every public method takes
`self.lock`, so the API layer can call them from any thread. After each tick a serialized frame
is published in `self.frame` for the WebSocket broadcaster.
"""

import collections
from bisect import bisect_right
import json
import logging
import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import agents, config, conflicts, decisions, sectors
from .aircraft import Aircraft
from .clearances import Clearance, parse_command, readback
from .scenario import PlanLoader

log = logging.getLogger("visor.engine")

AI_MODES = ("off", "advisory", "autonomous")
IGNORE_BELOW_FT = 4000.0       # aerodrome traffic is not monitored (tower/approach business)
MARGIN_DEG = 0.5
MAX_NEW_DECISIONS = 2
MAX_OPEN_DECISIONS = 12


def _fl(alt):
    return int(round(alt / 100.0))


def _sentence(text):
    return text[:1].upper() + text[1:]


class Engine:
    def __init__(self, store, navdata):
        self.store = store
        self.navdata = navdata
        self.lock = threading.RLock()
        self.rng = random.Random(7)
        self.speed = 1.0
        self.paused = False
        self.lockstep = False
        self.sector = None
        self.ai_mode = "off"
        self.agent = agents.create("rules")
        self._agent_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent")
        self.frame = None
        self.frame_seq = 0
        self._running = False
        self.reset("live")

    # ------------------------------------------------------------------ scenario control
    def reset(self, mode="live", start_t=None):
        with self.lock:
            first, last, _ = self.store.coverage()
            now = time.time()
            if mode == "replay":
                if first is None:
                    raise ValueError("no recorded data yet")
                start_t = float(start_t if start_t is not None else first)
                start_t = max(first, min(start_t, last or now))
            else:
                # Live runs just behind the newest snapshot; if the store is stale (server was
                # off) start at "now" and let the recorder's next snapshot populate the sky.
                mode = "live"
                fresh = last is not None and now - last < 1.5 * config.POLL_INTERVAL_S + 120
                start_t = float(last) if fresh else now
            self.mode = mode
            self.t = start_t
            self.start_t = start_t
            if mode == "live":
                self.speed = 1.0
            self.aircraft = {}
            # After a recording gap, live mode ignores the old backlog instead of spawning
            # aircraft from positions that are half an hour old.
            stale_live = mode == "live" and start_t == now
            self.loader = PlanLoader(self.store, start_t, backlog_s=120 if stale_live else 3600)
            self.loader.load(start_t + 1800)
            self._next_load = start_t + 20
            self.conflicts = []
            self._conflict_keys = {}
            self._conflict_seen = {}
            self.decisions = {}
            self._decision_by_key = {}
            self._cooldown = {}
            self.events = collections.deque(maxlen=800)
            self.event_seq = 0
            self.score = collections.Counter()
            self._in_sector = set()
            self._los_ids = set()
            self._last_conflict_scan = -1e9
            self._last_decision_scan = -1e9
            self._spawn()
            self._event("system", "%s scenario started at %s UTC with %d aircraft" % (
                mode.upper(), time.strftime("%H:%M:%S", time.gmtime(start_t)), len(self.aircraft)))
            self._scan_conflicts()
            self._build_frame()

    def set_speed(self, speed):
        with self.lock:
            self.speed = max(0.25, min(32.0, float(speed)))

    def set_paused(self, paused):
        with self.lock:
            self.paused = bool(paused)

    def set_lockstep(self, on):
        with self.lock:
            self.lockstep = bool(on)

    def set_sector(self, sector_id):
        with self.lock:
            self.sector = sectors.BY_ID.get(sector_id) if sector_id else None
            self._in_sector = set()
            self._update_sector()
            self._scan_conflicts()
            self._event("system", "Sector %s" % (self.sector["name"] if self.sector else "none (all Europe)"))
            self._build_frame()

    def set_ai(self, mode=None, agent=None):
        with self.lock:
            if agent and agent != self.agent.name:
                self.agent = agents.create(agent)
            if mode:
                if mode not in AI_MODES:
                    raise ValueError("mode must be one of %s" % (AI_MODES,))
                self.ai_mode = mode
                for dp in self.decisions.values():
                    dp.suggestion = None
                    dp.pending_agent = False
            self._event("system", "AI %s (%s)" % (self.ai_mode, self.agent.name))
            self._build_frame()
            return self.ai_status()

    def ai_status(self):
        return {"mode": self.ai_mode, "agent": self.agent.status(),
                "available_agents": sorted(agents.REGISTRY)}

    # ------------------------------------------------------------------ run loop
    def start(self):
        self._running = True
        threading.Thread(target=self._loop, name="sim", daemon=True).start()

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            t0 = time.monotonic()
            try:
                with self.lock:
                    if not self.paused and not self.lockstep:
                        dt = config.TICK_REAL_S * self.speed
                        dt = min(dt, max(0.0, time.time() - self.t))   # never run into the future
                        if dt > 0:
                            self.step(dt)
                    self._build_frame()
            except Exception:
                log.exception("simulation tick failed")
            time.sleep(max(0.0, config.TICK_REAL_S - (time.monotonic() - t0)))

    def step(self, dt):
        """Advance the simulation by dt sim-seconds (also used directly in lockstep mode)."""
        with self.lock:
            n = max(1, int(math.ceil(dt / config.MAX_SUBSTEP_S)))
            h = dt / n
            for _ in range(n):
                for ac in self.aircraft.values():
                    ac.step(self.t, h)
                self.t += h
            if self.t >= self._next_load or not self.aircraft:   # empty sky: pick up data at once
                self.loader.load(self.t + 1800)
                self.loader.prune(self.t, {a.plan for a in self.aircraft.values()})
                self._next_load = self.t + 20
            self._spawn()
            self._despawn()
            if self.t - self._last_conflict_scan >= config.CONFLICT_PERIOD_S:
                self._scan_conflicts()
            if self.t - self._last_decision_scan >= config.DECISION_PERIOD_S:
                self._last_decision_scan = self.t
                self._update_sector()
                self._pilot_requests()
                self._update_decisions()

    # ------------------------------------------------------------------ traffic life-cycle
    def _spawn(self):
        t = self.t
        interval = self.loader.interval
        for icao, plan in self.loader.plans.items():
            if plan.spawned or plan.done:
                continue
            if not plan.times or plan.times[0] > t or icao in self.aircraft:
                continue
            has_future = plan.times[-1] > t
            grace = (0.5 if plan.terminated_at is not None else 2.5) * interval + 60
            if not has_future and t - plan.times[-1] > grace:
                plan.done = True
                continue
            plan.spawned = True
            ac = Aircraft(plan, t, random.Random(self.rng.random()))
            if not self._in_area(ac):
                continue
            self.aircraft[icao] = ac

    def _in_area(self, ac):
        b = config.EUROPE
        return (b["lat_min"] - MARGIN_DEG <= ac.lat <= b["lat_max"] + MARGIN_DEG
                and b["lon_min"] - MARGIN_DEG <= ac.lon <= b["lon_max"] + MARGIN_DEG)

    def _despawn(self):
        t = self.t
        interval = self.loader.interval
        gone = []
        for ac in self.aircraft.values():
            plan = ac.plan
            last_t = plan.times[-1] if plan.times else t
            reason = None
            if not self._in_area(ac):
                reason = "left the area"
            elif ac.alt < 300 and ac.vs < -100:
                reason = "landed"
            elif ac.controller is None:
                if plan.terminated_at is not None and t > last_t + min(0.5 * interval, 450):
                    if not (ac.is_landing() and t < last_t + 1200):
                        reason = "left radar coverage"
                elif plan.terminated_at is None and t > last_t + 2.5 * interval + 60:
                    reason = "no recent data"
            elif plan.terminated_at is not None and t > last_t + 1800 and ac.id not in self._in_sector:
                reason = "transferred"
            if reason:
                gone.append((ac, reason))
        for ac, reason in gone:
            del self.aircraft[ac.id]
            plan = ac.plan
            plan.done = True
            if ac.id in self._in_sector:
                self._in_sector.discard(ac.id)
                self._event("system", "%s %s" % (ac.callsign, reason), callsign=ac.callsign)
            for dp in self.decisions.values():
                if dp.status == "open" and ac.id in dp.subject_ids:
                    self._close(dp, "expired")

    def neighbours(self, ac, h_nm, v_ft):
        lat_band = h_nm / 60.0
        lon_band = lat_band / max(0.1, math.cos(math.radians(ac.lat)))
        return [o for o in self.aircraft.values()
                if abs(o.lat - ac.lat) < lat_band and abs(o.lon - ac.lon) < lon_band
                and abs(o.alt - ac.alt) < v_ft]

    # ------------------------------------------------------------------ sector & requests
    def _relevant(self, ac):
        return ac.id in self._in_sector if self.sector else True

    def _update_sector(self):
        if not self.sector:
            self._in_sector = set()
            return
        now_in = {ac.id for ac in self.aircraft.values()
                  if sectors.contains(self.sector, ac.lat, ac.lon, ac.alt)}
        for i in now_in - self._in_sector:
            ac = self.aircraft[i]
            self._event("radio", "%s, %s, flight level %03d" % (
                self.sector["name"], ac.callsign, int(round(ac.alt / 1000.0)) * 10),
                speaker="PILOT", callsign=ac.callsign)
        for i in self._in_sector - now_in:
            if i in self.aircraft:
                if i not in self._los_ids:
                    self.score["handled"] += 1
                self._event("system", "%s left the sector" % self.aircraft[i].callsign,
                            callsign=self.aircraft[i].callsign)
        self._in_sector = now_in

    def _pilot_requests(self):
        t = self.t
        open_req = {sid for dp in self.decisions.values()
                    if dp.status == "open" and dp.kind != "conflict" for sid in dp.subject_ids}
        for ac in list(self.aircraft.values()):
            if ac.controller is None or ac.id in open_req or not self._relevant(ac):
                continue
            if t - getattr(ac, "last_request_t", -1e9) < 300 or t - (ac.last_clearance_t or t) < 90:
                continue
            if ac.vert_mode == "ALT" and abs(ac.alt - ac.cleared_alt) < 200:
                planned = ac.planned_alt(t)
                if abs(planned - ac.cleared_alt) >= 2000 and planned >= IGNORE_BELOW_FT:
                    fl = min(int(round(planned / 1000.0)) * 10, ac.perf["ceiling"] // 100)
                    if abs(fl * 100 - ac.cleared_alt) < 1000:
                        continue
                    ac.last_request_t = t
                    dp = decisions.level_request_decision(self, ac, fl, t)
                    self._add_decision(dp, ("req", ac.id))
                    self._event("radio", "%s, request %s flight level %03d" % (
                        ac.callsign, "climb" if fl * 100 > ac.alt else "descent", fl),
                        speaker="PILOT", callsign=ac.callsign)
                    continue
            if ac.lat_mode == "HDG" and ac.hdg_since is not None and t - ac.hdg_since > 420:
                ac.last_request_t = t
                dp = decisions.route_request_decision(self, ac, t)
                self._add_decision(dp, ("route", ac.id))
                self._event("radio", "%s, request to resume own navigation" % ac.callsign,
                            speaker="PILOT", callsign=ac.callsign)

    # ------------------------------------------------------------------ monitoring
    def _scan_conflicts(self):
        """Detect conflicts. Alerts and penalties use hysteresis so a flickering prediction is
        counted once, and a loss of separation only costs points if the controller had a warning
        (>= 45 s of STCA) or one of the aircraft was under control."""
        t = self.t
        self._last_conflict_scan = t
        monitored = [a for a in self.aircraft.values() if a.alt >= IGNORE_BELOW_FT]
        found = conflicts.detect(monitored)
        keys = {}
        seen = self._conflict_seen
        for c in found:
            key = (c["a"], c["b"])
            keys[key] = c
            a, b = self.aircraft[c["a"]], self.aircraft[c["b"]]
            relevant = self._relevant(a) or self._relevant(b)
            c["relevant"] = relevant
            rec = seen.get(key)
            if rec is None:
                rec = seen[key] = {"first": t, "last": t, "stca": False, "los": False}
            rec["last"] = t
            if not relevant:
                continue
            if c["kind"] == "LOS" and not rec["los"]:
                rec["los"] = True
                warned = t - rec["first"] >= 45 or a.controller or b.controller
                if warned:
                    self.score["los"] += 1
                    self._los_ids.update(key)
                self._event("alert", "SEPARATION LOST %s / %s — %.1f NM %d ft%s" % (
                    a.callsign, b.callsign, c["h_nm"], c["v_ft"], "" if warned else " (no warning, not scored)"),
                    level="alert")
            elif c["kind"] == "STCA" and not rec["stca"]:
                rec["stca"] = True
                self.score["stca"] += 1
                self._event("alert", "STCA %s / %s in %ds" % (a.callsign, b.callsign, c["t_to"]),
                            level="warning")
        for key in [k for k, r in seen.items() if t - r["last"] > 120]:
            del seen[key]
        self.conflicts = found
        self._conflict_keys = keys

    # ------------------------------------------------------------------ decisions
    def _close(self, dp, status, by=None):
        dp.status, dp.updated_t = status, self.t
        if by is not None:
            dp.answered_by = by

    def _add_decision(self, dp, key):
        dp.key = key
        self.decisions[dp.id] = dp
        self._decision_by_key[key] = dp.id

    def _update_decisions(self):
        t = self.t
        created = 0
        n_open = sum(1 for d in self.decisions.values() if d.status == "open")
        for c in sorted(self.conflicts, key=lambda c: c["t_to"]):
            key = ("conf", c["a"], c["b"])
            if key in self._decision_by_key:
                continue
            if n_open + created >= MAX_OPEN_DECISIONS:
                break
            a, b = self.aircraft.get(c["a"]), self.aircraft.get(c["b"])
            if a is None or b is None or not (self._relevant(a) or self._relevant(b)):
                continue
            # Without a sector the whole of Europe is "relevant": only build decision points
            # when an AI is working or when the controller already handles one of the aircraft.
            if (not self.sector and self.ai_mode == "off" and a is not None and b is not None
                    and a.controller is None and b.controller is None):
                continue
            if self._cooldown.get(key, -1e9) > t or created >= MAX_NEW_DECISIONS:
                continue
            if a is None or b is None:
                continue
            self._add_decision(decisions.conflict_decision(self, a, b, c, t), key)
            created += 1

        refreshed = 0
        for dp in list(self.decisions.values()):
            if dp.status == "open":
                subj = [self.aircraft.get(i) for i in dp.subject_ids]
                if any(s is None for s in subj):
                    self._close(dp, "expired")
                elif dp.kind == "conflict":
                    if (dp.key[1], dp.key[2]) not in self._conflict_keys:
                        self._close(dp, "expired")
                    elif t - dp.updated_t > 30 and refreshed < 1 and not dp.pending_agent:
                        c = self._conflict_keys[(dp.key[1], dp.key[2])]
                        fresh = decisions.conflict_decision(self, subj[0], subj[1], c, t)
                        dp.state, dp.options, dp.updated_t = fresh.state, fresh.options, t
                        dp.suggestion = None
                        refreshed += 1
                elif dp.kind == "level_request":
                    ac = subj[0]
                    assigned = ac.assigned.get("alt")
                    if assigned is not None and abs(assigned - dp.requested_fl * 100) < 1100:
                        self._close(dp, "executed", ac.controller)
                        self.score["requests_granted"] += 1
                    elif t - dp.created_t > 300:
                        self._close(dp, "expired")
                        self.score["requests_expired"] += 1
                        self._event("radio", "%s, still waiting for level change" % ac.callsign,
                                    speaker="PILOT", callsign=ac.callsign)
                elif dp.kind == "route_request":
                    ac = subj[0]
                    if ac.lat_mode != "HDG":
                        self._close(dp, "executed", ac.controller)
                        self.score["requests_granted"] += 1
                    elif t - dp.created_t > 300:
                        self._close(dp, "expired")
                        self.score["requests_expired"] += 1
            if dp.status != "open" and t - dp.updated_t > 120:
                self.decisions.pop(dp.id, None)
                if self._decision_by_key.get(dp.key) == dp.id:
                    del self._decision_by_key[dp.key]
            elif dp.status != "open" and self._decision_by_key.get(dp.key) == dp.id:
                del self._decision_by_key[dp.key]

        if self.ai_mode != "off":
            for dp in self.decisions.values():
                if dp.status == "open" and dp.suggestion is None and not dp.pending_agent:
                    dp.pending_agent = True
                    self._agent_pool.submit(self._ask_agent, dp.id, dp.to_dict(), self.agent)

    def _ask_agent(self, dp_id, payload, agent):
        try:
            answer = agent.decide(payload)
        except Exception as exc:
            log.warning("agent %s failed: %s", agent.name, exc)
            answer = None
        with self.lock:
            dp = self.decisions.get(dp_id)
            if dp is None:
                return
            dp.pending_agent = False
            if answer is None or dp.status != "open" or self.ai_mode == "off":
                return
            if self.ai_mode == "autonomous":
                try:
                    self.answer_decision(dp_id, answer, "ai:" + agent.name)
                except ValueError as exc:
                    log.warning("agent answer rejected: %s", exc)
            else:
                dp.suggestion = dict(answer, agent=agent.name)

    def answer_decision(self, dp_id, answer, by):
        """Execute the option chosen in answer["action"]."""
        with self.lock:
            dp = self.decisions.get(dp_id)
            if dp is None:
                raise ValueError("unknown decision %s" % dp_id)
            if dp.status != "open":
                raise ValueError("decision %s is %s" % (dp_id, dp.status))
            opt = next((o for o in dp.options if o["id"] == answer.get("action")), None)
            if opt is None:
                raise ValueError("unknown option %r" % answer.get("action"))
            results = []
            for ac_id, clr in opt["clearances"]:
                ac = self.aircraft.get(ac_id)
                if ac is not None:
                    results.append(self._issue(ac, [Clearance(clr.kind, clr.value, clr.direction)], by))
            dp.status, dp.answer, dp.answered_by, dp.updated_t = "executed", answer, by, self.t
            if by.startswith("ai:"):
                self.score["decisions_ai"] += 1
                self._event("ai", "%s → %s (confidence %s)" % (
                    dp.id, opt["label"], answer.get("confidence", "?")), speaker="AI")
            if dp.kind == "conflict":
                self._cooldown[dp.key] = self.t + 90
            elif dp.kind == "level_request" and opt["clearances"]:
                self.score["requests_granted"] += 1
            elif dp.kind == "route_request" and opt["clearances"]:
                self.score["requests_granted"] += 1
            if dp.kind != "conflict" and not opt["clearances"]:
                ac = self.aircraft.get(dp.subject_ids[0])
                if ac is not None:
                    self._event("radio", "%s, unable, maintain present clearance" % ac.callsign,
                                speaker="ATC", callsign=ac.callsign, issuer=by)
            self._build_frame()
            return {"decision": dp.id, "option": opt["label"], "results": results}

    def dismiss_decision(self, dp_id):
        with self.lock:
            dp = self.decisions.get(dp_id)
            if dp and dp.status == "open":
                dp.status, dp.updated_t = "dismissed", self.t
                if dp.kind == "conflict":
                    self._cooldown[dp.key] = self.t + 120

    # ------------------------------------------------------------------ clearances
    def find(self, ident):
        ident = ident.upper()
        ac = self.aircraft.get(ident.lower())
        if ac:
            return ac
        return next((a for a in self.aircraft.values() if a.callsign.upper() == ident), None)

    def _issue(self, ac, clearances, issuer):
        accepted, replies = [], []
        for clr in clearances:
            if clr.kind == "LEVEL":
                clr = Clearance("CLIMB" if clr.value * 100 > ac.alt else "DESCEND", clr.value)
            ok, reply = ac.issue(clr, self.t, self.navdata, issuer)
            if ok:
                accepted.append(clr)
            else:
                replies.append(reply)
        if accepted:
            self._event("radio", "%s, %s" % (ac.callsign, ", ".join(c.phrase() for c in accepted)),
                        speaker="ATC", callsign=ac.callsign, issuer=issuer)
            self._event("radio", _sentence(readback(ac.callsign, accepted)),
                        speaker="PILOT", callsign=ac.callsign)
            self.score["clearances_ai" if issuer.startswith("ai:") else "clearances_human"] += len(accepted)
        for r in replies:
            self._event("radio", _sentence(r), speaker="PILOT", callsign=ac.callsign)
        return {"aircraft": ac.id, "callsign": ac.callsign,
                "accepted": [c.to_dict() for c in accepted], "rejected": replies}

    def issue(self, ident, clearances, issuer="human"):
        with self.lock:
            ac = self.find(ident)
            if ac is None:
                raise ValueError("no aircraft %s" % ident)
            for c in clearances:
                if c.kind != "LEVEL":
                    c.validate()
            res = self._issue(ac, clearances, issuer)
            self._build_frame()
            return res

    def command(self, text, issuer="human"):
        callsign, clearances = parse_command(text)
        return self.issue(callsign, clearances, issuer)

    # ------------------------------------------------------------------ events & output
    def _event(self, kind, text, speaker="SYSTEM", callsign=None, issuer=None, level=None):
        self.event_seq += 1
        self.events.append({"seq": self.event_seq, "t": round(self.t, 1), "kind": kind,
                            "speaker": speaker, "callsign": callsign, "issuer": issuer,
                            "level": level, "text": text})

    def events_since(self, seq):
        with self.lock:
            return [e for e in self.events if e["seq"] > seq]

    def points(self):
        s = self.score
        return (2 * s["handled"] + 10 * s["requests_granted"] - 50 * s["los"]
                - 2 * s["stca"] - 10 * s["requests_expired"])

    def _flags(self, ac, conf):
        f = 0
        if ac.id in self._in_sector:
            f |= 1
        if ac.controller and ac.controller.startswith("human"):
            f |= 2
        elif ac.controller and ac.controller.startswith("ai:"):
            f |= 4
        lvl = conf.get(ac.id)
        if lvl == "STCA":
            f |= 8
        elif lvl == "LOS":
            f |= 16
        if ac.pending:
            f |= 64
        return f

    def _build_frame(self):
        conf = {}
        for c in self.conflicts:
            for i in (c["a"], c["b"]):
                if conf.get(i) != "LOS":
                    conf[i] = c["kind"]
        req_ids = {sid for dp in self.decisions.values()
                   if dp.status == "open" and dp.kind != "conflict" for sid in dp.subject_ids}
        ac_rows = []
        for ac in self.aircraft.values():
            flags = self._flags(ac, conf) | (32 if ac.id in req_ids else 0)
            a = ac.assigned
            ac_rows.append([
                ac.id, ac.callsign, round(ac.lat, 5), round(ac.lon, 5), round(ac.alt), round(ac.hdg, 1),
                round(ac.tas), round(ac.vs), _fl(a["alt"]) if a["alt"] is not None else None,
                flags, ac.cls, ac.lateral_text(), a["hdg"], a["dct"], a["spd"],
            ])
        first, last, nsnap = self.store.coverage()
        frame = {
            "type": "frame", "seq": self.frame_seq + 1, "t": self.t, "mode": self.mode,
            "speed": self.speed, "paused": self.paused, "lockstep": self.lockstep,
            "sector": self.sector["id"] if self.sector else None,
            "ai": {"mode": self.ai_mode, "agent": self.agent.name},
            "score": dict(self.score, points=self.points()),
            "coverage": {"first": first, "last": last, "snapshots": nsnap},
            "ac": ac_rows,
            "conflicts": [[c["a"], c["b"], c["kind"], c["t_to"], c["h_nm"], c["v_ft"], c.get("relevant", False)]
                          for c in self.conflicts],
            "decisions": [dp.to_dict() for dp in self.decisions.values()],
            "event_seq": self.event_seq,
        }
        self.frame_seq += 1
        self.frame = json.dumps(frame, separators=(",", ":"))

    def aircraft_detail(self, ident):
        with self.lock:
            ac = self.find(ident)
            if ac is None:
                return None
            pts = ac.plan.points
            if ac.diverged:
                route = pts[ac.route_idx:]
            else:
                route = pts[bisect_right(ac.plan.times, self.t):]
            return {
                **decisions.ac_state(ac),
                "icao24": ac.id, "country": ac.plan.country, "squawk": ac.plan.squawk,
                "perf": ac.perf["name"], "ceiling_fl": ac.perf["ceiling"] // 100,
                "ias_kt": round(ac.ias), "modes": {"lateral": ac.lat_mode, "vertical": ac.vert_mode,
                                                   "speed": ac.spd_mode},
                "assigned": ac.assigned, "diverged": ac.diverged,
                "in_sector": ac.id in self._in_sector,
                "pending": [{"at": round(t_, 1), **c.to_dict()} if c.kind != "DIRECT"
                            else {"at": round(t_, 1), "kind": "DIRECT", "value": c.value["ident"]}
                            for t_, c in ac.pending],
                "route": [[round(p[1], 4), round(p[2], 4), round(p[3])] for p in route[:80]],
                "fixes": self.navdata.nearby(ac.lat, ac.lon, 250, limit=30),
            }

    def observation(self, sector_only=False):
        """Full typed state for external agents."""
        with self.lock:
            conf = {}
            for c in self.conflicts:
                for i in (c["a"], c["b"]):
                    conf.setdefault(i, []).append(c)
            acs = []
            for ac in self.aircraft.values():
                if sector_only and ac.id not in self._in_sector:
                    continue
                d = decisions.ac_state(ac)
                d.update(in_sector=ac.id in self._in_sector, assigned=ac.assigned,
                         modes={"lateral": ac.lat_mode, "vertical": ac.vert_mode, "speed": ac.spd_mode})
                acs.append(d)
            return {
                "t": self.t, "mode": self.mode, "speed": self.speed, "paused": self.paused,
                "lockstep": self.lockstep,
                "sector": self.sector, "aircraft": acs,
                "conflicts": [c for c in self.conflicts if not sector_only or c.get("relevant")],
                "decisions": [dp.to_dict() for dp in self.decisions.values() if dp.status == "open"],
                "score": dict(self.score, points=self.points()),
                "event_seq": self.event_seq,
            }
