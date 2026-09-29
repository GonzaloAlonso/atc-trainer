"""Runs an exercise in a trainee's private sandbox: its timeline, rewind points and the end.

The trainee holds the exercise's sector from the start. Every 15 simulated seconds the sandbox
keeps a snapshot, so "Rewind and retry" can go back to just before any decision; a rewind ends
the current attempt (it stays in the history with its grades) and starts a new one linked to it.
When the time is up the simulation pauses, open situations are graded and the session summary
is stored for the debrief.
"""

import logging
import time

from ..coach.journal import summarize
from ..control import human
from ..engine import Engine
from ..errors import Invalid
from ..store import Store
from . import write_flights

log = logging.getLogger("visor.exercise")

SNAP_EVERY_S = 15.0
SNAP_KEEP = 160                  # 40 minutes of simulated time


class ExerciseSandbox:
    kind = "exercise"

    def __init__(self, navdata, exercise, username, coach_store=None, level=None):
        self.created = self.last_used = time.time()
        self.exercise = exercise
        self.username = username
        self.store = Store(":memory:")
        # Scenario time sits hours in the past so it can run at up to 16x without catching up
        # with the wall clock (the engine never runs into the future).
        self.t0 = int(time.time()) - 3 * 3600
        write_flights(self.store, exercise.flights, self.t0, exercise.duration_s + 900)
        self.engine = e = Engine(self.store, navdata, coach_store=coach_store, context="exercise",
                                 exercise=exercise.id)
        e.reset("replay", self.t0)
        if level:
            e.coach.set_level(username, level)
        e.take_sector(exercise.sector, human(username))
        self.session = e.coach.begin(username)
        e.set_speed(exercise.speed)
        self.fired = set()
        self.status = "running"
        self.summary = None
        self.attempt = 1
        self.snaps = []
        self._last_snap = -1e9
        e.tick_hooks.append(self._tick)
        self._publish()
        e.start()

    # ------------------------------------------------------------------ timeline
    def elapsed(self):
        return self.engine.t - self.t0

    def _tick(self, engine):
        el = self.elapsed()
        for i, ev in enumerate(self.exercise.events):
            if i not in self.fired and el >= ev.at_s:
                self.fired.add(i)
                try:
                    self._fire(ev)
                except Exception:
                    log.exception("exercise event failed: %s", ev)
        if el - self._last_snap >= SNAP_EVERY_S and self.status == "running":
            self._last_snap = el
            self.snaps.append((engine.snapshot(), set(self.fired)))
            del self.snaps[:-SNAP_KEEP]
        if self.status == "running" and el >= self.exercise.duration_s:
            self._finish()
        self._publish()

    def _fire(self, ev):
        e = self.engine
        if ev.kind == "request_level":
            ac = e.find(ev.callsign)
            if ac is None:
                return
            # the crew holds its level and now plans the new one: until cleared it stays level,
            # and if the answer is "unable" it will ask again later
            ac.vert_mode, ac.cleared_alt, ac.controller = "ALT", round(ac.alt / 1000.0) * 1000.0, "system"
            ac.assigned["alt"] = ac.cleared_alt
            ac.last_clearance_t = ac.last_request_t = e.t
            plan = ac.plan
            plan.points = [p if p[0] < e.t else (p[0], p[1], p[2], ev.fl * 100.0) + tuple(p[4:])
                           for p in plan.points]
            e.pilot_request_level(ac, ev.fl)

    def _finish(self):
        e = self.engine
        self.status = "completed"
        e.set_paused(True)
        e.coach.finish(self.username)
        sid = e.coach.sessions.get(self.username)
        self.summary = summarize([x for x in e.coach.entries_of(self.username) if x.get("session") == sid],
                                 e.score_of(human(self.username)))
        self._close_session("completed")

    def _close_session(self, status):
        e = self.engine
        c = e.coach
        sid = c.sessions.get(self.username)
        if c.store is None or sid is None:
            return
        summary = self.summary or summarize([x for x in c.entries_of(self.username) if x.get("session") == sid],
                                            e.score_of(human(self.username)))
        summary["attempt"] = self.attempt
        c.store.end_session(sid, status, sim_t1=e.t, summary=summary)
        if c.recording:
            c.store.save_recording(sid, c.recording)

    def _publish(self):
        ex = self.exercise
        self.engine.frame_extra["exercise"] = {
            "id": ex.id, "title": ex.title, "t0": self.t0, "elapsed": round(self.elapsed(), 1),
            "duration": ex.duration_s, "status": self.status, "attempt": self.attempt,
            "session": self.engine.coach.sessions.get(self.username), "summary": self.summary,
            "focus": list(ex.focus), "can_rewind": bool(self.snaps),
        }

    # ------------------------------------------------------------------ rewind
    def rewind(self, to_t):
        """Go back to the latest snapshot at or before sim time to_t (a new attempt)."""
        e = self.engine
        with e.lock:
            cands = [(s, f) for s, f in self.snaps if s["t"] <= to_t]
            if not cands:
                if not self.snaps:
                    raise Invalid("nothing_to_rewind")
                cands = self.snaps[:1]
            snap, fired = cands[-1]
            c = e.coach
            old = c.sessions.get(self.username)
            c.finish(self.username)
            if self.status == "running":
                self._close_session("rewound")
            self.status = "running"
            self.summary = None
            self.attempt += 1
            e.restore(snap)
            self.fired = set(fired)
            self.snaps = [(s, f) for s, f in self.snaps if s["t"] <= snap["t"]]
            self._last_snap = snap["t"] - self.t0
            if c.recording is not None:
                c.recording[:] = [fr for fr in c.recording if fr["t"] <= snap["t"]]
                c._last_rec = snap["t"]
            c.sessions.pop(self.username, None)
            if c.store is not None:
                c.sessions[self.username] = c.store.start_session(
                    self.username, "exercise", self.exercise.id, sim_t0=snap["t"], parent=old)
            e.set_paused(False)
            self._publish()
            e._build_frame()
            return {"t": snap["t"], "attempt": self.attempt, "session": c.sessions.get(self.username)}

    def stop(self):
        self.engine.stop()
        if self.status == "running":
            self.engine.coach.finish(self.username)
            self._close_session("abandoned")
            self.status = "abandoned"
