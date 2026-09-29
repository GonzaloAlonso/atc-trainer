"""The simulation engine: time, traffic life-cycle, clearances, monitoring, decisions and scoring.

Responsibility follows the sectorization (atc/sectors.py): every aircraft is in at most one
sector, and whoever holds that sector (atc/control.py) is responsible for it. Alerts, pilot
requests, decision points, radio messages and scores are routed to that holder; other users
only see them if they hold one of the sectors involved.

Threading: a dedicated thread advances the simulation in real time. Every public method takes
`self.lock`, so the API layer can call them from any thread. After each tick the shared part of
the frame is serialized once; `frame_for(viewer)` adds the viewer-specific part.
"""

import collections
import copy
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
from .coach.journal import Coach
from .control import Control, ControlError, display
from .errors import Denied, Invalid
from .scenario import PlanLoader

log = logging.getLogger("visor.engine")

AI_MODES = ("off", "advisory", "autonomous")
IGNORE_BELOW_FT = 4000.0       # aerodrome traffic is not monitored (tower/approach business)
MARGIN_DEG = 0.5
MAX_NEW_DECISIONS = 2
MAX_OPEN_DECISIONS = 12

# Aircraft row flag bits shared by every viewer (per-viewer flags are derived client-side)
F_HUMAN, F_AI, F_PENDING = 2, 4, 64


def _fl(alt):
    return int(round(alt / 100.0))


def _sentence(text):
    return text[:1].upper() + text[1:]


class Engine:
    def __init__(self, store, navdata, sector_idle_s=None, coach_store=None, context="live", exercise=None):
        """context: live (the shared simulation) | tutorial | exercise (a trainee's sandbox)."""
        self.store = store
        self.navdata = navdata
        self.lock = threading.RLock()
        self.rng = random.Random(7)
        self.speed = 1.0
        self.paused = False
        self.lockstep = False
        self.context = context
        self.control = Control(idle_s=sector_idle_s)
        self.ai_prefs = {}             # username -> {"agent"}: which AI advises / demonstrates
        self.coach = Coach(self, coach_store, context, exercise)
        self.tick_hooks = []           # f(engine) after every step, e.g. an exercise's timeline
        self.frame_extra = {}          # extra keys for every viewer's frame (e.g. exercise status)
        self._agents = {}
        self._agent_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent")
        self._frame_cache = {}
        self.frame_seq = 0
        self._shared = "{}"
        self._running = False
        self._generation = 0           # bumps on every start/stop so a stale loop thread exits
        self.hibernating = False
        self._last_reap = 0.0
        self.reset("live")

    # ------------------------------------------------------------------ scenario control
    def reset(self, mode="live", start_t=None):
        with self.lock:
            first, last, _ = self.store.coverage()
            now = time.time()
            if mode == "replay":
                if first is None:
                    raise Invalid("no_data")
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
            self.events = collections.deque(maxlen=1500)
            self.event_seq = 0
            self.scores = collections.defaultdict(collections.Counter)   # holder -> counters
            self._los_ids = set()
            self._last_conflict_scan = -1e9
            self._last_decision_scan = -1e9
            self.coach.reset()                 # a new scenario: open situations no longer exist
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

    # ------------------------------------------------------------------ sectors & holders
    def holder_of(self, ac):
        return self.control.holder(ac.sector_id)

    def take_sector(self, sector_id, holder, force=False):
        with self.lock:
            prev = self.control.take(sector_id, holder, force)
            if prev != holder:
                name = sectors.BY_ID[sector_id]["name"]
                taken = " from %s" % display(prev) if prev else ""
                self._event("system", "%s takes %s%s" % (display(holder), name, taken), sectors=[sector_id])
                self._reassign_open_decisions()
            self._build_frame()
            return self.control.sectors_of(holder)

    def release_sector(self, sector_id, holder, force=False):
        with self.lock:
            prev = self.control.release(sector_id, holder, force)
            if prev:
                self._event("system", "%s released by %s" % (sectors.BY_ID[sector_id]["name"], display(holder)),
                            sectors=[sector_id])
                self._reassign_open_decisions()
            self._build_frame()
            return self.control.sectors_of(holder)

    def assign_ai(self, sector_id, agent_name, by_holder, force=False):
        with self.lock:
            agents.create(agent_name)                 # validates the name
            self.control.assign_ai(sector_id, agent_name, by_holder, force)
            self._event("system", "%s handed to AI (%s) by %s" % (
                sectors.BY_ID[sector_id]["name"], agent_name, display(by_holder)), sectors=[sector_id])
            self._reassign_open_decisions()
            self._build_frame()

    def _reassign_open_decisions(self):
        """Holders changed: re-route open decision points and let the new holder's AI answer."""
        for dp in self.decisions.values():
            if dp.status == "open":
                dp.holders = self._holders_for(dp.sector_ids)
                dp.suggestion = None
                self.coach.sync(dp)                 # journals follow the change of controller

    def _holders_for(self, sector_ids):
        return sorted({h for h in (self.control.holder(s) for s in sector_ids) if h})

    def _reap_idle(self):
        for sid, h in self.control.reap_idle():
            self._event("system", "%s released: %s went offline" % (sectors.BY_ID[sid]["name"], display(h)),
                        sectors=[sid])
            self._reassign_open_decisions()

    # ------------------------------------------------------------------ AI preferences & coach
    def agent(self, name):
        if name not in self._agents:
            self._agents[name] = agents.create(name)
        return self._agents[name]

    # the AI mode follows the coach level: it advises at Advise and acts at Demonstrate
    _LEVEL_MODE = {"advise": "advisory", "demonstrate": "autonomous"}
    _MODE_LEVEL = {"advisory": "advise", "autonomous": "demonstrate", "off": "off"}

    def ai_pref(self, username):
        pref = self.ai_prefs.get(username, {})
        mode = self._LEVEL_MODE.get(self.coach.level_of(username), "off") if username else "off"
        return {"mode": mode, "agent": pref.get("agent", "rules")}

    def _forget_suggestions(self, username):
        for dp in self.decisions.values():
            if "human:" + username in dp.holders:
                dp.suggestion = None
                dp.pending_agent = False

    def set_ai(self, username, mode=None, agent=None):
        """The user's own AI assistance for decision points in their sectors (kept for API
        compatibility: modes map onto coach levels off / advise / demonstrate)."""
        with self.lock:
            if agent:
                self.agent(agent)                     # validates
                self.ai_prefs[username] = dict(self.ai_prefs.get(username, {}), agent=agent)
            if mode:
                if mode not in AI_MODES:
                    raise Invalid("ai_mode_invalid", modes=", ".join(AI_MODES))
                self.coach.set_level(username, self._MODE_LEVEL[mode])
            self._forget_suggestions(username)
            self._build_frame()
            return self.ai_status(username)

    def set_coach(self, username, level):
        with self.lock:
            self.coach.set_level(username, level)
            self._forget_suggestions(username)
            self._build_frame()
            return self.coach.view(username)

    def hint(self, username, dp_id):
        with self.lock:
            res = self.coach.hint(username, dp_id)
            self._build_frame()
            return res

    def ai_status(self, username=None):
        pref = self.ai_pref(username) if username else {"mode": "off", "agent": "rules"}
        return {"mode": pref["mode"], "agent": self.agent(pref["agent"]).status(),
                "coach": self.coach.level_of(username) if username else None,
                "available_agents": sorted(agents.REGISTRY)}

    # ------------------------------------------------------------------ run loop
    def start(self):
        with self.lock:
            self._generation += 1
            self._running = True
            threading.Thread(target=self._loop, args=(self._generation,), name="sim", daemon=True).start()

    def stop(self):
        with self.lock:
            self._generation += 1
            self._running = False

    def hibernate(self):
        """Stop the loop and drop the scenario (aircraft, plans, alerts) to free CPU and memory.
        Sector assignments and AI preferences are kept; wake() starts a fresh live scenario."""
        with self.lock:
            if self.hibernating:
                return False
            self.stop()
            self.hibernating = True
            self.aircraft = {}
            self.loader.plans.clear()
            self.conflicts = []
            self._conflict_keys = {}
            self._conflict_seen = {}
            self.decisions = {}
            self._decision_by_key = {}
            self._cooldown = {}
            self._los_ids = set()
            self.coach.reset()
            self._event("system", "Nobody online: simulation asleep to save resources")
            self._build_frame()
            return True

    def wake(self):
        with self.lock:
            if not self.hibernating:
                return False
            self.hibernating = False
            self.reset("live")
            self.start()
            return True

    # ------------------------------------------------------------------ rewind (sandboxes)
    def snapshot(self):
        """Everything needed to continue the simulation from now, later (see restore)."""
        with self.lock:
            return {
                "t": self.t,
                "aircraft": {k: a.clone(keep_rng=True) for k, a in self.aircraft.items()},
                # plans are shared with the aircraft; exercises may rewrite their points later
                "plans": {k: (p, p.spawned, p.done, p.terminated_at, list(p.points), list(p.times))
                          for k, p in self.loader.plans.items()},
                "loaded_until": self.loader.loaded_until, "snap_times": list(self.loader.snap_times),
                "next_load": self._next_load, "rng": self.rng.getstate(),
                "scores": copy.deepcopy(self.scores), "los_ids": set(self._los_ids),
                "conflict_seen": copy.deepcopy(self._conflict_seen), "cooldown": dict(self._cooldown),
            }

    def restore(self, snap):
        """Go back to a snapshot. Open decisions and alerts are rebuilt by the next scans."""
        with self.lock:
            self.t = snap["t"]
            self.aircraft = {k: a.clone(keep_rng=True) for k, a in snap["aircraft"].items()}
            self.loader.plans = {}
            for k, (plan, spawned, done, terminated_at, points, times) in snap["plans"].items():
                plan.spawned, plan.done, plan.terminated_at = spawned, done, terminated_at
                plan.points, plan.times = list(points), list(times)
                self.loader.plans[k] = plan
            self.loader.loaded_until = snap["loaded_until"]
            self.loader.snap_times = list(snap["snap_times"])
            self._next_load = snap["next_load"]
            self.rng.setstate(snap["rng"])
            self.scores = copy.deepcopy(snap["scores"])
            self._los_ids = set(snap["los_ids"])
            self._conflict_seen = copy.deepcopy(snap["conflict_seen"])
            self._cooldown = dict(snap["cooldown"])
            self.conflicts, self._conflict_keys = [], {}
            self.decisions, self._decision_by_key = {}, {}
            self.coach.reset()
            self._last_conflict_scan = self._last_decision_scan = -1e9
            self._event("system", "Rewound to %s UTC" % time.strftime("%H:%M:%S", time.gmtime(self.t)))
            self._scan_conflicts()
            self._build_frame()

    def _loop(self, generation):
        while self._running and generation == self._generation:
            t0 = time.monotonic()
            try:
                with self.lock:
                    if not self.paused and not self.lockstep:
                        dt = config.TICK_REAL_S * self.speed
                        dt = min(dt, max(0.0, time.time() - self.t))   # never run into the future
                        if dt > 0:
                            self.step(dt)
                    if t0 - self._last_reap > 15:
                        self._last_reap = t0
                        self._reap_idle()
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
                self._update_sectors()
                self._pilot_requests()
                self._update_decisions()
            for hook in self.tick_hooks:
                hook(self)

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
            ac.sector_id = sectors.sector_at(ac.lat, ac.lon, ac.alt)
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
            elif plan.terminated_at is not None and t > last_t + 1800 and self.holder_of(ac) is None:
                reason = "transferred"
            if reason:
                gone.append((ac, reason))
        for ac, reason in gone:
            del self.aircraft[ac.id]
            ac.plan.done = True
            if self.holder_of(ac):
                self._event("system", "%s %s" % (ac.callsign, reason), callsign=ac.callsign,
                            sectors=[ac.sector_id])
            for dp in self.decisions.values():
                if dp.status == "open" and ac.id in dp.subject_ids:
                    self._close(dp, "expired")

    def neighbours(self, ac, h_nm, v_ft):
        lat_band = h_nm / 60.0
        lon_band = lat_band / max(0.1, math.cos(math.radians(ac.lat)))
        return [o for o in self.aircraft.values()
                if abs(o.lat - ac.lat) < lat_band and abs(o.lon - ac.lon) < lon_band
                and abs(o.alt - ac.alt) < v_ft]

    # ------------------------------------------------------------------ sectors & requests
    def _update_sectors(self):
        """Recompute each aircraft's sector: check-ins, hand-offs and 'handled' credits."""
        for ac in self.aircraft.values():
            sid = sectors.sector_at(ac.lat, ac.lon, ac.alt)
            prev = getattr(ac, "sector_id", None)
            if sid == prev:
                continue
            ac.sector_id = sid
            old_holder = self.control.holder(prev)
            if old_holder:
                if ac.id not in self._los_ids:
                    self.scores[old_holder]["handled"] += 1
                self._event("system", "%s left %s" % (ac.callsign, sectors.BY_ID[prev]["name"]),
                            callsign=ac.callsign, sectors=[prev])
            if self.control.holder(sid):
                self._event("radio", "%s, %s, flight level %03d" % (
                    sectors.BY_ID[sid]["name"], ac.callsign, int(round(ac.alt / 1000.0)) * 10),
                    speaker="PILOT", callsign=ac.callsign, sectors=[sid])

    def pilot_request_level(self, ac, fl):
        """The crew of ac asks for flight level fl: a decision point and a radio call."""
        ac.last_request_t = self.t
        dp = decisions.level_request_decision(self, ac, fl, self.t)
        self._add_decision(dp, ("req", ac.id), [ac.sector_id])
        self._event("radio", "%s, request %s flight level %03d" % (
            ac.callsign, "climb" if fl * 100 > ac.alt else "descent", fl),
            speaker="PILOT", callsign=ac.callsign, sectors=[ac.sector_id])
        return dp

    def _pilot_requests(self):
        t = self.t
        open_req = {sid for dp in self.decisions.values()
                    if dp.status == "open" and dp.kind != "conflict" for sid in dp.subject_ids}
        for ac in list(self.aircraft.values()):
            if ac.controller is None or ac.id in open_req or not self.holder_of(ac):
                continue
            if t - getattr(ac, "last_request_t", -1e9) < 300 or t - (ac.last_clearance_t or t) < 90:
                continue
            if ac.vert_mode == "ALT" and abs(ac.alt - ac.cleared_alt) < 200:
                planned = ac.planned_alt(t)
                if abs(planned - ac.cleared_alt) >= 2000 and planned >= IGNORE_BELOW_FT:
                    fl = min(int(round(planned / 1000.0)) * 10, ac.perf["ceiling"] // 100)
                    if abs(fl * 100 - ac.cleared_alt) < 1000:
                        continue
                    self.pilot_request_level(ac, fl)
                    continue
            if ac.lat_mode == "HDG" and ac.hdg_since is not None and t - ac.hdg_since > 420:
                ac.last_request_t = t
                dp = decisions.route_request_decision(self, ac, t)
                self._add_decision(dp, ("route", ac.id), [ac.sector_id])
                self._event("radio", "%s, request to resume own navigation" % ac.callsign,
                            speaker="PILOT", callsign=ac.callsign, sectors=[ac.sector_id])

    # ------------------------------------------------------------------ monitoring
    def _scan_conflicts(self):
        """Detect conflicts and route them to the holders of the sectors involved.

        Alerts and penalties use hysteresis so a flickering prediction is counted once, and a loss
        of separation only costs points if the controller had a warning (>= 45 s of STCA) or one
        of the aircraft was under control."""
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
            c["sectors"] = sorted({s for s in (a.sector_id, b.sector_id) if s})
            c["holders"] = self._holders_for(c["sectors"])
            rec = seen.get(key)
            if rec is None:
                rec = seen[key] = {"first": t, "last": t, "stca": False, "los": False}
            rec["last"] = t
            if not c["holders"]:
                continue                                  # unmanned airspace: nobody to alert
            if c["kind"] == "LOS" and not rec["los"]:
                rec["los"] = True
                warned = t - rec["first"] >= 45 or a.controller or b.controller
                if warned:
                    for h in c["holders"]:
                        self.scores[h]["los"] += 1
                    self._los_ids.update(key)
                self._event("alert", "SEPARATION LOST %s / %s — %.1f NM %d ft%s" % (
                    a.callsign, b.callsign, c["h_nm"], c["v_ft"], "" if warned else " (no warning, not scored)"),
                    level="alert", sectors=c["sectors"])
            elif c["kind"] == "STCA" and not rec["stca"]:
                rec["stca"] = True
                for h in c["holders"]:
                    self.scores[h]["stca"] += 1
                self._event("alert", "STCA %s / %s in %ds" % (a.callsign, b.callsign, c["t_to"]),
                            level="warning", sectors=c["sectors"])
        for key in [k for k, r in seen.items() if t - r["last"] > 120]:
            del seen[key]
        self.conflicts = found
        self._conflict_keys = keys
        self.coach.on_scan()

    # ------------------------------------------------------------------ decisions
    def _close(self, dp, status, by=None):
        dp.status, dp.updated_t = status, self.t
        if by is not None:
            dp.answered_by = by

    def _add_decision(self, dp, key, sector_ids):
        dp.key = key
        dp.sector_ids = sorted({s for s in sector_ids if s})
        dp.holders = self._holders_for(dp.sector_ids)
        self.decisions[dp.id] = dp
        self._decision_by_key[key] = dp.id
        self.coach.on_decision(dp)

    def _credit(self, dp, counter):
        for h in dp.holders:
            self.scores[h][counter] += 1

    def _update_decisions(self):
        t = self.t
        created = 0
        n_open = sum(1 for d in self.decisions.values() if d.status == "open")
        for c in sorted(self.conflicts, key=lambda c: c["t_to"]):
            key = ("conf", c["a"], c["b"])
            if key in self._decision_by_key or not c.get("holders"):
                continue
            if n_open + created >= MAX_OPEN_DECISIONS:
                break
            a, b = self.aircraft.get(c["a"]), self.aircraft.get(c["b"])
            if a is None or b is None:
                continue
            if self._cooldown.get(key, -1e9) > t or created >= MAX_NEW_DECISIONS:
                continue
            self._add_decision(decisions.conflict_decision(self, a, b, c, t), key, c["sectors"])
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
                        dp.best = fresh.best
                        dp.sector_ids = c["sectors"]
                        dp.holders = self._holders_for(dp.sector_ids)
                        dp.suggestion = None
                        self.coach.on_refresh(dp)
                        refreshed += 1
                elif dp.kind == "level_request":
                    ac = subj[0]
                    assigned = ac.assigned.get("alt")
                    if assigned is not None and abs(assigned - dp.requested_fl * 100) < 1100:
                        self._close(dp, "executed", ac.controller)
                        self._credit(dp, "requests_granted")
                    elif t - dp.created_t > 300:
                        self._close(dp, "expired")
                        self._credit(dp, "requests_expired")
                        self._event("radio", "%s, still waiting for level change" % ac.callsign,
                                    speaker="PILOT", callsign=ac.callsign, sectors=dp.sector_ids)
                elif dp.kind == "route_request":
                    ac = subj[0]
                    if ac.lat_mode != "HDG":
                        self._close(dp, "executed", ac.controller)
                        self._credit(dp, "requests_granted")
                    elif t - dp.created_t > 300:
                        self._close(dp, "expired")
                        self._credit(dp, "requests_expired")
            if dp.status != "open" and t - dp.updated_t > 120:
                self.decisions.pop(dp.id, None)
                if self._decision_by_key.get(dp.key) == dp.id:
                    del self._decision_by_key[dp.key]
            elif dp.status != "open" and self._decision_by_key.get(dp.key) == dp.id:
                del self._decision_by_key[dp.key]

        for dp in self.decisions.values():
            if dp.status != "open" or dp.suggestion is not None or dp.pending_agent:
                continue
            route = self._ai_route(dp)
            if route:
                agent, mode = route
                dp.pending_agent = True
                self._agent_pool.submit(self._ask_agent, dp.id, dp.to_dict(), agent, mode)

    def _ai_route(self, dp):
        """Which agent answers a decision point, and how: an AI holding one of its sectors acts
        autonomously; otherwise the human holder's own AI preference applies."""
        for h in dp.holders:
            if h.startswith("ai:"):
                return self.agent(h[3:]), "autonomous"
        for h in dp.holders:
            pref = self.ai_pref(h[6:])                # follows the coach level (advise / demonstrate)
            if pref["mode"] != "off":
                return self.agent(pref["agent"]), pref["mode"]
        return None

    def _ask_agent(self, dp_id, payload, agent, mode):
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
            route = self._ai_route(dp)
            if answer is None or dp.status != "open" or route is None or route[0] is not agent:
                return
            if route[1] == "autonomous":
                try:
                    self.answer_decision(dp_id, answer, "ai:" + agent.name)
                except ValueError as exc:
                    log.warning("agent answer rejected: %s", exc)
            else:
                dp.suggestion = dict(answer, agent=agent.name)

    def answer_decision(self, dp_id, answer, by, actor=None, enforce_level=True):
        """Execute the option chosen in answer["action"].

        actor: holder key of the user answering (API calls). They must be responsible for the
        decision point. None for the engine's own AI, which only answers what is routed to it.
        """
        with self.lock:
            dp = self.decisions.get(dp_id)
            if dp is None:
                raise Invalid("unknown_decision", id=dp_id)
            if actor is not None and dp.holders and actor not in dp.holders:
                raise Denied("decision_belongs", id=dp.id, holders=", ".join(display(h) for h in dp.holders))
            if dp.status != "open":
                raise Invalid("decision_closed", id=dp_id)
            opt = next((o for o in dp.options if o["id"] == answer.get("action")), None)
            if opt is None:
                raise Invalid("unknown_option", option=answer.get("action"))
            if enforce_level and actor is not None and actor.startswith("human:") and not by.startswith("ai:"):
                reveal = self.coach.reveal_for(actor[6:], dp)
                if reveal is not None and opt["id"] not in reveal:
                    raise Denied("option_hidden")
            results = []
            self.coach.via = "decision"
            try:
                for ac_id, clr in opt["clearances"]:
                    ac = self.aircraft.get(ac_id)
                    if ac is not None:
                        results.append(self._issue(ac, [Clearance(clr.kind, clr.value, clr.direction)], by, actor))
            finally:
                self.coach.via = None
            self.coach.on_answer(dp, opt, by, actor)
            dp.status, dp.answer, dp.answered_by, dp.updated_t = "executed", answer, by, self.t
            if by.startswith("ai:"):
                self._credit(dp, "decisions_ai")
                self._event("ai", "%s → %s (confidence %s)" % (
                    dp.id, opt["label"], answer.get("confidence", "?")), speaker="AI", sectors=dp.sector_ids)
                if actor is None:
                    self.coach.narrate(dp, opt)
            if dp.kind == "conflict":
                self._cooldown[dp.key] = self.t + 90
            elif dp.kind in ("level_request", "route_request") and opt["clearances"]:
                self._credit(dp, "requests_granted")
            if dp.kind != "conflict" and not opt["clearances"]:
                ac = self.aircraft.get(dp.subject_ids[0])
                if ac is not None:
                    self._event("radio", "%s, unable, maintain present clearance" % ac.callsign,
                                speaker="ATC", callsign=ac.callsign, issuer=by, sectors=dp.sector_ids)
            self._build_frame()
            return {"decision": dp.id, "option": opt["label"], "results": results}

    def dismiss_decision(self, dp_id, actor=None):
        with self.lock:
            dp = self.decisions.get(dp_id)
            if dp and dp.status == "open":
                if actor is not None and dp.holders and actor not in dp.holders:
                    raise Denied("decision_belongs", id=dp.id, holders=", ".join(display(h) for h in dp.holders))
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

    def _issue(self, ac, clearances, issuer, actor=None):
        accepted, replies = [], []
        for clr in clearances:
            if clr.kind == "LEVEL":
                clr = Clearance("CLIMB" if clr.value * 100 > ac.alt else "DESCEND", clr.value)
            ok, reply = ac.issue(clr, self.t, self.navdata, issuer)
            if ok:
                accepted.append(clr)
            else:
                replies.append(reply)
        tag = [ac.sector_id] if ac.sector_id else []
        if accepted:
            self._event("radio", "%s, %s" % (ac.callsign, ", ".join(c.phrase() for c in accepted)),
                        speaker="ATC", callsign=ac.callsign, issuer=issuer, by=actor, sectors=tag)
            self._event("radio", _sentence(readback(ac.callsign, accepted)),
                        speaker="PILOT", callsign=ac.callsign, to=issuer, by=actor, sectors=tag)
            self.scores[issuer]["clearances"] += len(accepted)
            self.coach.on_clearance(ac, accepted, issuer, actor)
        for r in replies:
            self._event("radio", _sentence(r), speaker="PILOT", callsign=ac.callsign, to=issuer, by=actor,
                        sectors=tag)
        return {"aircraft": ac.id, "callsign": ac.callsign,
                "accepted": [c.to_dict() for c in accepted], "rejected": replies}

    def check_authority(self, ac, actor):
        """Only the holder of the aircraft's sector may instruct it; unmanned airspace is open."""
        h = self.holder_of(ac)
        if actor is not None and h is not None and h != actor:
            raise Denied("not_your_aircraft", callsign=ac.callsign, sector=sectors.BY_ID[ac.sector_id]["name"],
                         holder=display(h))

    def issue(self, ident, clearances, issuer="human", actor=None):
        with self.lock:
            ac = self.find(ident)
            if ac is None:
                raise Invalid("no_aircraft", ident=ident)
            self.check_authority(ac, actor)
            for c in clearances:
                if c.kind != "LEVEL":
                    c.validate()
            res = self._issue(ac, clearances, issuer, actor)
            self._build_frame()
            return res

    def command(self, text, issuer="human", actor=None):
        callsign, clearances = parse_command(text)
        return self.issue(callsign, clearances, issuer, actor)

    # ------------------------------------------------------------------ events & visibility
    def _event(self, kind, text, speaker="SYSTEM", callsign=None, issuer=None, level=None,
               sectors=None, to=None, by=None, msg=None, grade=None):
        """by: account (holder key) that caused the event, e.g. the user behind an external agent.
        msg: {"key", "p"} of a coach message, so the UI can show it in the user's language."""
        self.event_seq += 1
        e = {"seq": self.event_seq, "t": round(self.t, 1), "kind": kind,
             "speaker": speaker, "callsign": callsign, "issuer": issuer, "to": to, "by": by,
             "level": level, "text": text, "sectors": [s for s in (sectors or []) if s]}
        if msg is not None:
            e["msg"] = {"key": msg["key"], "p": msg["p"]}
        if grade is not None:
            e["grade"] = grade
        self.events.append(e)

    @staticmethod
    def _visible_event(e, viewer, mine):
        if viewer is None:
            return True
        if viewer in (e["issuer"], e["to"], e["by"]):
            return True
        if not e["sectors"]:
            return e["kind"] == "system"                 # global system messages
        return any(s in mine for s in e["sectors"])

    def events_since(self, seq, viewer=None):
        """Events after seq that the viewer (holder key; None = everything) may see."""
        with self.lock:
            mine = set(self.control.sectors_of(viewer)) if viewer else set()
            return [e for e in self.events if e["seq"] > seq and self._visible_event(e, viewer, mine)]

    def visible_conflicts(self, viewer):
        return [c for c in self.conflicts if viewer is None or viewer in c.get("holders", ())]

    def reveal(self, viewer, dp):
        """Which of dp's options the viewer may see (None = all), from their coach level."""
        if viewer and viewer.startswith("human:"):
            return self.coach.reveal_for(viewer[6:], dp)
        return None

    def visible_decisions(self, viewer, open_only=False):
        return [dp for dp in self.decisions.values()
                if (viewer is None or viewer in dp.holders) and (not open_only or dp.status == "open")]

    SCORE_KEYS = ("handled", "requests_granted", "requests_expired", "los", "stca", "clearances", "decisions_ai")

    def score_of(self, holder):
        s = self.scores.get(holder, collections.Counter())
        out = {k: s[k] for k in self.SCORE_KEYS}
        out["points"] = (2 * s["handled"] + 10 * s["requests_granted"] - 50 * s["los"]
                         - 2 * s["stca"] - 10 * s["requests_expired"])
        return out

    # ------------------------------------------------------------------ frames
    def _flags(self, ac):
        f = 0
        if ac.controller and ac.controller.startswith("human"):
            f |= F_HUMAN
        elif ac.controller and ac.controller.startswith("ai:"):
            f |= F_AI
        if ac.pending:
            f |= F_PENDING
        return f

    def _build_frame(self):
        """Serialize the part of the frame every viewer shares (aircraft, sectorization)."""
        counts = collections.Counter(ac.sector_id for ac in self.aircraft.values() if ac.sector_id)
        ac_rows = []
        for ac in self.aircraft.values():
            a = ac.assigned
            ac_rows.append([
                ac.id, ac.callsign, round(ac.lat, 5), round(ac.lon, 5), round(ac.alt), round(ac.hdg, 1),
                round(ac.tas), round(ac.vs), _fl(a["alt"]) if a["alt"] is not None else None,
                self._flags(ac), ac.cls, ac.lateral_text(), a["hdg"], a["dct"], a["spd"],
                ac.sector_id, ac.controller,
            ])
        holders = self.control.snapshot()
        first, last, nsnap = self.store.coverage()
        self.coach.record(ac_rows, self.conflicts)
        shared = {
            "type": "frame", "seq": self.frame_seq + 1, "t": self.t, "mode": self.mode,
            "speed": self.speed, "paused": self.paused, "lockstep": self.lockstep,
            "asleep": self.hibernating, "context": self.context,
            "coverage": {"first": first, "last": last, "snapshots": nsnap},
            "ac": ac_rows,
            "sectorization": [[sid, holders.get(sid), counts.get(sid, 0)] for sid in sectors.BY_ID],
            "points": {h: self.score_of(h)["points"] for h in set(holders.values())},
            "event_seq": self.event_seq,
        }
        shared.update(self.frame_extra)
        self.frame_seq += 1
        self._shared = json.dumps(shared, separators=(",", ":"))
        self._frame_cache = {}

    def frame_for(self, viewer):
        """Complete JSON frame for a viewer (holder key, e.g. "human:alice"; None = everything)."""
        with self.lock:
            cached = self._frame_cache.get(viewer)
            if cached is not None:
                return cached
            username = viewer[6:] if viewer and viewer.startswith("human:") else None
            personal = {
                "me": {"holder": viewer, "sectors": self.control.sectors_of(viewer) if viewer else []},
                "ai": {"mode": self.ai_pref(username)["mode"], "agent": self.ai_pref(username)["agent"]},
                "score": self.score_of(viewer) if viewer else {},
                "conflicts": [[c["a"], c["b"], c["kind"], c["t_to"], c["h_nm"], c["v_ft"], c.get("sectors", [])]
                              for c in self.visible_conflicts(viewer)],
                # without the decision assistant (Evaluate, Hints) a trainee only sees the options
                # a hint has revealed
                "decisions": [dp.to_dict(self.reveal(viewer, dp)) for dp in self.visible_decisions(viewer)],
                "coach": self.coach.view(username) if username else None,
            }
            frame = self._shared[:-1] + "," + json.dumps(personal, separators=(",", ":"))[1:]
            self._frame_cache[viewer] = frame
            return frame

    @property
    def frame(self):
        return self.frame_for(None)

    # ------------------------------------------------------------------ what-if
    def _whatif_view(self, pred, subjects, base=None):
        keep = {a.id for a in subjects} | set(pred["conflicts_with"])
        if pred["cpa"]:
            keep |= {pred["cpa"]["a"], pred["cpa"]["b"]}
        tracks = {k: v for k, v in pred["tracks"].items() if k in keep}
        summary = ("min_h_nm", "min_v_ft", "los", "first_los_s", "los_duration_s", "cpa")
        out = {
            "t": self.t, "step_s": pred["step_s"], "subjects": [a.id for a in subjects],
            "tracks": tracks, "names": {k: self.aircraft[k].callsign for k in tracks if k in self.aircraft},
            "cpa": pred["cpa"], "cpa_at": pred["cpa_at"],
            "predicted": {k: pred[k] for k in summary},
            "conflicts_with": [self.aircraft[i].callsign for i in pred["conflicts_with"] if i in self.aircraft],
            "minima": {"h_nm": conflicts.sep_h(subjects[0].alt, subjects[0].alt), "v_ft": config.SEP_V_FT},
        }
        if base is not None:
            out["baseline"] = {k: base[k] for k in summary}
        return out

    def probe(self, ident, clearances, actor=None):
        """Predicted outcome of clearances for an aircraft, with 3D tracks, without issuing them.
        The aircraft it is in conflict with are flown too, and so is "no action" for comparison."""
        with self.lock:
            ac = self.find(ident)
            if ac is None:
                raise Invalid("no_aircraft", ident=ident)
            resolved = []
            for c in clearances:
                if c.kind == "LEVEL":
                    c = Clearance("CLIMB" if c.value * 100 > ac.alt else "DESCEND", c.value)
                c.validate()
                if c.kind == "DIRECT" and self.navdata.find(c.value, ac.lat, ac.lon) is None:
                    raise Invalid("unknown_fix", fix=c.value)
                resolved.append((ac.id, c))
            others = {x for c in self.conflicts if ac.id in (c["a"], c["b"]) for x in (c["a"], c["b"])}
            subjects = [ac] + [self.aircraft[x] for x in sorted(others) if x != ac.id and x in self.aircraft]
            pred = decisions.predict(self, subjects, resolved, self.t, tracks=True)
            base = decisions.predict(self, subjects, [], self.t)
            out = self._whatif_view(pred, subjects, base)
            out["clearances"] = [c.phrase() for _, c in resolved]
            out["aircraft"] = ac.callsign
            return out

    def whatif(self, dp_id, option_id, viewer=None, enforce_level=True):
        """3D prediction of one option of a decision point (the Decisions list's hover view)."""
        with self.lock:
            dp = self.decisions.get(dp_id)
            if dp is None:
                raise Invalid("unknown_decision", id=dp_id)
            if viewer is not None and dp.holders and viewer not in dp.holders:
                raise Denied("decision_belongs", id=dp.id, holders=", ".join(display(h) for h in dp.holders))
            username = viewer[6:] if viewer and viewer.startswith("human:") else None
            reveal = self.coach.reveal_for(username, dp) if username and enforce_level else None
            if reveal is not None and option_id not in reveal:
                raise Denied("option_hidden")
            opt = next((o for o in dp.options if o["id"] == option_id), None)
            if opt is None:
                raise Invalid("unknown_option", option=option_id)
            subjects = [self.aircraft[i] for i in dp.subject_ids if i in self.aircraft]
            if not subjects:
                raise Invalid("aircraft_left")
            pred = decisions.predict(self, subjects, opt["clearances"], self.t, tracks=True)
            out = self._whatif_view(pred, subjects)
            out.update(decision=dp.id, option=opt["id"], label=opt["label"], why=opt.get("why"))
            return out

    # ------------------------------------------------------------------ agent views
    def aircraft_detail(self, ident, viewer=None):
        with self.lock:
            ac = self.find(ident)
            if ac is None:
                return None
            pts = ac.plan.points
            if ac.diverged:
                route = pts[ac.route_idx:]
            else:
                route = pts[bisect_right(ac.plan.times, self.t):]
            holder = self.holder_of(ac)
            return {
                **decisions.ac_state(ac),
                "icao24": ac.id, "country": ac.plan.country, "squawk": ac.plan.squawk,
                "perf": ac.perf["name"], "ceiling_fl": ac.perf["ceiling"] // 100,
                "ias_kt": round(ac.ias), "modes": {"lateral": ac.lat_mode, "vertical": ac.vert_mode,
                                                   "speed": ac.spd_mode},
                "assigned": ac.assigned, "diverged": ac.diverged,
                "sector": ac.sector_id,
                "sector_name": sectors.BY_ID[ac.sector_id]["name"] if ac.sector_id else None,
                "holder": holder, "holder_name": display(holder),
                "mine": viewer is not None and holder == viewer,
                "can_clear": holder is None or viewer is None or holder == viewer,
                "pending": [{"at": round(t_, 1), **c.to_dict()} if c.kind != "DIRECT"
                            else {"at": round(t_, 1), "kind": "DIRECT", "value": c.value["ident"]}
                            for t_, c in ac.pending],
                "route": [[round(p[1], 4), round(p[2], 4), round(p[3])] for p in route[:80]],
                "fixes": self.navdata.nearby(ac.lat, ac.lon, 250, limit=30),
            }

    def observation(self, viewer=None, mine_only=False, hide=True):
        """Typed state for agents: all traffic (with sector and holder), plus the conflicts and
        decision points the viewer is responsible for (everything when viewer is None)."""
        with self.lock:
            acs = []
            for ac in self.aircraft.values():
                holder = self.holder_of(ac)
                if mine_only and (viewer is None or holder != viewer):
                    continue
                d = decisions.ac_state(ac)
                d.update(sector=ac.sector_id, holder=holder, mine=viewer is not None and holder == viewer,
                         assigned=ac.assigned,
                         modes={"lateral": ac.lat_mode, "vertical": ac.vert_mode, "speed": ac.spd_mode})
                acs.append(d)
            return {
                "t": self.t, "mode": self.mode, "speed": self.speed, "paused": self.paused,
                "lockstep": self.lockstep,
                "me": {"holder": viewer, "sectors": self.control.sectors_of(viewer) if viewer else []},
                "sectors": self.control.snapshot(),
                "aircraft": acs,
                "conflicts": self.visible_conflicts(viewer),
                "decisions": [dp.to_dict(self.reveal(viewer, dp) if hide else None)
                              for dp in self.visible_decisions(viewer, open_only=True)],
                "score": self.score_of(viewer) if viewer else {},
                "event_seq": self.event_seq,
            }


__all__ = ["Engine", "ControlError", "AI_MODES"]
