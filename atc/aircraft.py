"""Flight plans (from recorded data) and the simulated aircraft that fly them.

Each aircraft follows its recorded trajectory ("own navigation", time-synchronised with the
recording) until a controller issues a clearance. From then on the affected axis (lateral,
vertical, speed) is flown by a simple autopilot with performance limits:

    lateral : PLAN (follow route) | HDG (fly heading, optional turn direction) | DCT (direct to fix)
    vertical: PLAN (recorded profile) | ALT (cleared flight level)
    speed   : PLAN (recorded ground speed) | SPD (cleared IAS)
"""

import copy
import math
import random
from bisect import bisect_right

from .clearances import Clearance
from .geo import angle_diff, bearing_deg, distance_nm, move, wrap360
from .performance import PERF, classify, climb_rate, ias_to_tas, tas_to_ias

G = 9.80665


class FlightPlan:
    """Recorded trajectory of one airframe: points (t, lat, lon, alt_ft, gs_kt, trk, vs_fpm)."""

    def __init__(self, icao24):
        self.icao24 = icao24
        self.callsign = None
        self.country = None
        self.category = None
        self.squawk = None
        self.points = []
        self.times = []
        self.terminated_at = None   # set when the airframe vanished from later snapshots
        self.spawned = False        # an Aircraft has been created from this plan
        self.done = False           # stale or finished; revived if new data arrives

    def add(self, row):
        t, icao24, callsign, country, lat, lon, alt, gs, trk, vs, category, squawk = row
        if self.times and t <= self.times[-1]:
            return
        self.callsign = callsign or self.callsign
        self.country = country or self.country
        self.category = category if category else self.category
        self.squawk = squawk or self.squawk
        self.points.append((t, lat, lon, alt, gs, trk, vs))
        self.times.append(t)
        self.terminated_at = None
        if self.done:               # e.g. a recording gap: the flight is back, let it respawn
            self.done = self.spawned = False

    def prune_before(self, t):
        """Forget points older than t (keeps one point before t for interpolation)."""
        i = bisect_right(self.times, t) - 1
        if i > 0:
            del self.points[:i]
            del self.times[:i]
            return i
        return 0

    def state_at(self, t):
        """Interpolated (lat, lon, alt, hdg, gs, vs) at time t, clamped to the recorded span."""
        pts = self.points
        i = bisect_right(self.times, t)
        if i == 0:
            p = pts[0]
            return p[1], p[2], p[3], p[5] or 0.0, p[4] or 250.0, p[6] or 0.0
        if i >= len(pts):
            p = pts[-1]
            return p[1], p[2], p[3], p[5] or 0.0, p[4] or 250.0, p[6] or 0.0
        a, b = pts[i - 1], pts[i]
        f = (t - a[0]) / float(b[0] - a[0])
        lat = a[1] + (b[1] - a[1]) * f
        lon = a[2] + (b[2] - a[2]) * f
        alt = a[3] + (b[3] - a[3]) * f
        d = distance_nm(a[1], a[2], b[1], b[2])
        hdg = bearing_deg(a[1], a[2], b[1], b[2]) if d > 0.5 else (a[5] or 0.0)
        gs = d / ((b[0] - a[0]) / 3600.0) if d > 0.5 else (a[4] or 250.0)
        vs = (b[3] - a[3]) / ((b[0] - a[0]) / 60.0)
        return lat, lon, alt, hdg, gs, vs


class Aircraft:
    def __init__(self, plan, t, rng=None):
        self.id = plan.icao24
        self.plan = plan
        self.rng = rng or random.Random(hash(plan.icao24))
        self.callsign = plan.callsign or plan.icao24.upper()
        lat, lon, alt, hdg, gs, vs = plan.state_at(t)
        self.cls = classify(plan.category, gs, alt)
        self.perf = PERF[self.cls]
        self.lat, self.lon, self.alt, self.hdg, self.tas, self.vs = lat, lon, alt, hdg, gs, vs
        self.spawn_t = t

        self.lat_mode, self.vert_mode, self.spd_mode = "PLAN", "PLAN", "PLAN"
        self.tgt_hdg = hdg
        self.turn_dir = None
        self.turn_remaining = None     # degrees left for relative turns
        self.dct = None                # fix dict
        self.cleared_alt = None        # ft
        self.cleared_ias = None
        self.diverged = False          # lost time-sync with the recording
        self.route_idx = 0             # next route point when re-joining after divergence
        self.pending = []              # [(exec_t, Clearance)]
        self.controller = None         # None | "human" | "ai:<name>"
        self.last_clearance_t = None
        self.hdg_since = None
        self.assigned = {"alt": None, "hdg": None, "dct": None, "spd": None}

    # ------------------------------------------------------------------ clearances
    def issue(self, clr, t, navdata, issuer):
        """Validate and queue a clearance. Returns (accepted, pilot_reply)."""
        clr.validate()
        p = self.perf
        k = clr.kind
        if k in ("CLIMB", "DESCEND"):
            target = clr.value * 100.0
            if target > p["ceiling"]:
                return False, "unable flight level %03d, %s" % (clr.value, self.callsign)
            if k == "CLIMB" and target < self.alt - 200:
                return False, "negative, we are above flight level %03d, %s" % (clr.value, self.callsign)
            if k == "DESCEND" and target > self.alt + 200:
                return False, "negative, we are below flight level %03d, %s" % (clr.value, self.callsign)
            self.assigned["alt"] = target
        elif k == "SPEED":
            if not p["ias_min"] <= clr.value <= p["ias_max"]:
                return False, "unable speed %d knots, %s" % (clr.value, self.callsign)
            self.assigned["spd"] = clr.value
        elif k == "DIRECT":
            fix = navdata.find(clr.value, self.lat, self.lon)
            if fix is None:
                return False, "say again the fix, %s" % self.callsign
            if distance_nm(self.lat, self.lon, fix["lat"], fix["lon"]) > 600:
                return False, "%s is too far, unable, %s" % (clr.value, self.callsign)
            clr = Clearance("DIRECT", fix)   # resolved fix travels with the clearance
            self.assigned["dct"] = fix["ident"]
            self.assigned["hdg"] = None
        elif k == "HEADING":
            self.assigned["hdg"] = clr.value
            self.assigned["dct"] = None
        elif k == "TURN":
            sign = -1 if clr.direction == "L" else 1
            self.assigned["hdg"] = round(wrap360(self.hdg + sign * clr.value)) or 360
            self.assigned["dct"] = None
        elif k == "RESUME":
            self.assigned = {"alt": None, "hdg": None, "dct": None, "spd": None}

        delay = self.rng.uniform(3.0, 8.0)
        self.pending.append((t + delay, clr))
        self.controller = issuer
        self.last_clearance_t = t
        return True, None

    def _apply(self, clr, t):
        k = clr.kind
        if k in ("CLIMB", "DESCEND"):
            self.vert_mode = "ALT"
            self.cleared_alt = clr.value * 100.0
        elif k == "HEADING":
            self.lat_mode, self.tgt_hdg, self.turn_dir = "HDG", float(clr.value % 360), clr.direction
            self.turn_remaining = None
            self.diverged = True
            self.hdg_since = t
        elif k == "TURN":
            sign = -1 if clr.direction == "L" else 1
            self.lat_mode = "HDG"
            self.tgt_hdg = wrap360(self.hdg + sign * clr.value)
            self.turn_dir, self.turn_remaining = clr.direction, float(clr.value)
            self.diverged = True
            self.hdg_since = t
        elif k == "DIRECT":
            self.lat_mode, self.dct, self.turn_dir = "DCT", clr.value, None
            self.diverged = True
            self.hdg_since = None
        elif k == "SPEED":
            self.spd_mode, self.cleared_ias = "SPD", float(clr.value)
            self.diverged = True
        elif k == "RESUME":
            self.lat_mode = self.vert_mode = self.spd_mode = "PLAN"
            self.cleared_alt = self.cleared_ias = self.dct = None
            self.turn_dir = None
            self.hdg_since = None
            self._rejoin_route(t)

    # ------------------------------------------------------------------ route following
    def _rejoin_route(self, t):
        """Pick the route point to re-join: the one after the closest point, looking ahead."""
        pts = self.plan.points
        self.diverged = True
        if not pts:
            self.route_idx = 0
            return
        lo = max(0, bisect_right(self.plan.times, t - 1800) - 1)
        hi = min(len(pts), bisect_right(self.plan.times, t + 3600) + 1)
        best, best_d = lo, 1e9
        for i in range(lo, hi):
            d = distance_nm(self.lat, self.lon, pts[i][1], pts[i][2])
            if d < best_d:
                best, best_d = i, d
        self.route_idx = best + 1 if best_d < 15 or best + 1 < len(pts) else best

    def _route_target(self, t):
        pts = self.plan.points
        if not self.diverged:
            i = bisect_right(self.plan.times, t + 2)
            while i < len(pts):
                p = pts[i]
                d = distance_nm(self.lat, self.lon, p[1], p[2])
                rel = abs(angle_diff(bearing_deg(self.lat, self.lon, p[1], p[2]), self.hdg))
                if d < 2.0 or (rel > 100 and d < 25):
                    i += 1
                    continue
                return p
            return None
        # diverged: sequence spatially
        while self.route_idx < len(pts):
            p = pts[self.route_idx]
            d = distance_nm(self.lat, self.lon, p[1], p[2])
            rel = abs(angle_diff(bearing_deg(self.lat, self.lon, p[1], p[2]), self.hdg))
            if d < 3.0 or (rel > 95 and d < 12):
                self.route_idx += 1
                continue
            return p
        return None

    def planned_alt(self, t):
        """Altitude the flight 'wants' according to its recorded plan (for pilot requests)."""
        tgt = self._route_target(t) if self.lat_mode == "PLAN" else None
        if tgt is not None:
            return tgt[3]
        pts = self.plan.points
        i = bisect_right(self.plan.times, t)
        return pts[min(i, len(pts) - 1)][3] if pts else self.alt

    def is_landing(self):
        last = self.plan.points[-1] if self.plan.points else None
        return last is not None and (last[6] or 0) < -300 and last[3] < 15000

    # ------------------------------------------------------------------ integration
    def step(self, t, dt):
        """Advance to time t + dt."""
        if self.pending:
            due = [c for c in self.pending if c[0] <= t]
            if due:
                self.pending = [c for c in self.pending if c[0] > t]
                for _, clr in due:
                    self._apply(clr, t)

        p = self.perf
        target = self._route_target(t) if (self.lat_mode == "PLAN" or self.vert_mode == "PLAN"
                                           or self.spd_mode == "PLAN") else None

        # ---- lateral
        if self.lat_mode == "PLAN":
            if target is not None:
                self.tgt_hdg = bearing_deg(self.lat, self.lon, target[1], target[2])
            else:
                self.tgt_hdg = self.hdg
        elif self.lat_mode == "DCT":
            fix = self.dct
            d = distance_nm(self.lat, self.lon, fix["lat"], fix["lon"])
            if d < max(1.5, self.tas * dt / 3600.0 * 1.5):
                self.lat_mode, self.dct = "PLAN", None
                self.assigned["dct"] = None
                self._rejoin_route(t)
                target = self._route_target(t)
                self.tgt_hdg = (bearing_deg(self.lat, self.lon, target[1], target[2])
                                if target is not None else self.hdg)
            else:
                self.tgt_hdg = bearing_deg(self.lat, self.lon, fix["lat"], fix["lon"])

        v_ms = max(self.tas, 60.0) * 0.514444
        rate = min(3.0, math.degrees(G * math.tan(math.radians(p["bank"])) / v_ms))
        max_turn = rate * dt
        diff = angle_diff(self.tgt_hdg, self.hdg)
        if self.lat_mode == "HDG" and self.turn_dir:
            if self.turn_remaining is not None:
                diff = self.turn_remaining * (-1 if self.turn_dir == "L" else 1)
            elif self.turn_dir == "L" and diff > 0:
                diff -= 360.0
            elif self.turn_dir == "R" and diff < 0:
                diff += 360.0
        turn = max(-max_turn, min(max_turn, diff))
        self.hdg = wrap360(self.hdg + turn)
        if self.turn_remaining is not None:
            self.turn_remaining -= abs(turn)
            if self.turn_remaining <= 0.01:
                self.turn_remaining, self.turn_dir = None, None
        elif abs(diff) <= max_turn:
            self.turn_dir = None

        # ---- vertical
        max_clb = climb_rate(p, self.alt)
        max_des = p["descent"]
        if self.vert_mode == "ALT":
            tgt_alt = self.cleared_alt
            req = None
        elif target is not None:
            tgt_alt = target[3]
            req = (tgt_alt - self.alt) / max(30.0, target[0] - t) * 60.0 if not self.diverged else None
        elif self.is_landing():
            tgt_alt, req = 0.0, -1500.0
        else:
            tgt_alt, req = self.alt, None
        err = tgt_alt - self.alt
        want = err * 6.0                            # ~10 s exponential capture near the level
        if req is not None and abs(req) < abs(want):
            floor = 0.6 * (max_clb if err > 0 else max_des)
            want = math.copysign(max(abs(req), floor), err) if abs(err) > 300 else want
        want = max(-max_des, min(max_clb, want))
        dv = 200.0 * dt                              # fpm per second response
        self.vs += max(-dv, min(dv, want - self.vs))
        new_alt = self.alt + self.vs * dt / 60.0
        # don't overshoot the target level
        if (self.alt - tgt_alt) * (new_alt - tgt_alt) < 0:
            new_alt, self.vs = tgt_alt, 0.0
        self.alt = max(0.0, new_alt)

        # ---- speed (tas == ground speed; no wind model)
        lo = ias_to_tas(p["ias_min"], self.alt)
        hi = min(ias_to_tas(p["ias_max"], self.alt), p["tas_max"])
        if self.spd_mode == "SPD":
            want_gs = max(lo, min(hi, ias_to_tas(self.cleared_ias, self.alt)))
        elif target is not None:
            rec_gs = target[4] or self.tas
            if not self.diverged and target[0] - t > 5:
                d = distance_nm(self.lat, self.lon, target[1], target[2])
                want_gs = d / ((target[0] - t) / 3600.0)
                want_gs = max(0.75 * rec_gs, min(1.3 * rec_gs, want_gs))
            else:
                want_gs = rec_gs
            want_gs = max(0.6 * lo, min(hi + 160.0, want_gs))  # recorded GS includes wind
        else:
            want_gs = self.tas
        ds = p["accel"] * dt
        self.tas += max(-ds, min(ds, want_gs - self.tas))

        self.lat, self.lon = move(self.lat, self.lon, self.hdg, self.tas * dt / 3600.0)

    # ------------------------------------------------------------------ helpers
    def clone(self):
        c = copy.copy(self)
        c.pending = list(self.pending)
        c.assigned = dict(self.assigned)
        c.rng = random.Random(0)
        return c

    @property
    def ias(self):
        return tas_to_ias(self.tas, self.alt)

    def lateral_text(self):
        if self.lat_mode == "DCT" and self.dct:
            return "DCT " + self.dct["ident"]
        if self.lat_mode == "HDG":
            return "H%03d" % (round(self.tgt_hdg) % 360 or 360)
        return "ROUTE"
