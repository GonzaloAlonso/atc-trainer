"""Exercises: short scripted scenarios, each built to practise one kind of decision.

An exercise is declarative: the sector the trainee works, how long it lasts, the flights (straight
legs, optionally climbing or descending), and timed events such as a pilot request. It runs in the
trainee's private sandbox (runner.py), where the coach grades every decision, and ends with a
debrief. Geometry is defined around meeting points, so a conflict happens exactly when intended:
`toward(point, track, speed, meet_s)` starts an aircraft so that it reaches `point` after meet_s.

Titles and briefs here are English (for agents and the API); the UI shows them in the user's
language from public/locales (keys "ex.<id>.title" / ".brief" / ".goal").
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..training import FT, fly, state

SNAPSHOT_EVERY_S = 30


@dataclass
class Flight:
    icao: str
    callsign: str
    lat: float
    lon: float
    alt_ft: float
    gs_kt: float
    trk: float
    start_s: float = 0.0              # appears this long after the exercise starts
    vs_fpm: float = 0.0               # climbing (+) or descending (-) until target_ft
    target_ft: Optional[float] = None
    category: int = 4                 # OpenSky emitter category: 4 medium, 6 heavy

    def at(self, dt):
        """(lat, lon, alt_ft, vs_fpm) dt seconds after the flight appeared."""
        lat, lon = fly(self.lat, self.lon, self.trk, self.gs_kt, dt)
        alt, vs = self.alt_ft, 0.0
        if self.vs_fpm and self.target_ft is not None:
            alt = self.alt_ft + self.vs_fpm * dt / 60.0
            if (self.vs_fpm > 0 and alt >= self.target_ft) or (self.vs_fpm < 0 and alt <= self.target_ft):
                alt = self.target_ft
            else:
                vs = self.vs_fpm
        return lat, lon, alt, vs


@dataclass
class Event:
    at_s: float
    kind: str                         # "request_level"
    callsign: str = ""
    fl: int = 0


@dataclass
class Exercise:
    id: str
    title: str
    brief: str
    goal: str
    sector: str
    duration_s: float
    flights: List[Flight]
    events: List[Event] = field(default_factory=list)
    competencies: List[str] = field(default_factory=list)
    level: str = "easy"               # easy | medium | hard
    coach: str = "hints"              # recommended coach level
    speed: float = 1.0
    focus: Tuple[float, float, float] = (44.5, 6.5, 650.0)   # lat, lon, camera distance
    pack: str = "drills"
    order: int = 0

    def public(self):
        return {"id": self.id, "pack": self.pack, "order": self.order, "title": self.title,
                "brief": self.brief, "goal": self.goal, "sector": self.sector,
                "duration_s": self.duration_s, "competencies": self.competencies, "level": self.level,
                "coach": self.coach, "focus": list(self.focus),
                "aircraft": len(self.flights)}


def toward(icao, callsign, point, trk, gs_kt, alt_ft, meet_s, offset_nm=0.0, start_s=0.0, **kw):
    """A flight on track `trk` that passes `point` (shifted offset_nm to the right) meet_s seconds
    after the exercise starts; it appears start_s seconds after the start."""
    lat, lon = point
    if offset_nm:
        lat, lon = fly(lat, lon, trk + 90.0, offset_nm * 3600.0, 1.0)
    lat, lon = fly(lat, lon, trk + 180.0, gs_kt, meet_s - start_s)
    return Flight(icao, callsign, lat, lon, alt_ft, gs_kt, trk, start_s=start_s, **kw)


def write_flights(store, flights, t0, duration_s, every_s=SNAPSHOT_EVERY_S):
    """Record the flights as OpenSky snapshots, as the recorder would have stored them."""
    for k in range(int(duration_s // every_s) + 1):
        t = t0 + k * every_s
        states = []
        for f in flights:
            dt = t - t0 - f.start_s
            if dt < 0:
                continue
            lat, lon, alt, vs = f.at(dt)
            states.append(state(f.icao, f.callsign, lat, lon, alt, f.gs_kt, f.trk, vs_fpm=vs, t=t,
                                category=f.category))
        store.insert_snapshot({"time": t, "states": states})


def _registry():
    from . import drills
    return {e.id: e for e in sorted(drills.EXERCISES, key=lambda e: (e.pack, e.order))}


BY_ID = _registry()


def catalogue():
    return [e.public() for e in BY_ID.values()]


__all__ = ["Exercise", "Flight", "Event", "toward", "write_flights", "BY_ID", "catalogue", "FT"]
