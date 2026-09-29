"""Who controls which sector.

A holder is a string key: "human:<username>" for a signed-in controller or "ai:<agent>" for an
AI agent run by the engine. A holder may control several sectors ("bandboxing"). Assignments
live in memory: after a restart controllers take their sectors again.
"""

import threading
import time

from . import sectors
from .errors import UserError


class ControlError(UserError):
    """Refused sector operation. `status` is the HTTP code the API should answer with."""
    status = 409


def human(username):
    return "human:" + username


def display(holder):
    """"human:alice" -> "alice", "ai:rules" -> "AI (rules)"."""
    if not holder:
        return None
    kind, _, name = holder.partition(":")
    return "AI (%s)" % name if kind == "ai" else name


class Control:
    def __init__(self, idle_s=None, clock=time.time):
        self.holders = {}            # sector id -> holder key
        self.idle_s = idle_s         # release human sectors after this long without presence
        self.clock = clock
        self._seen = {}              # username -> last presence
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ queries
    def holder(self, sector_id):
        return self.holders.get(sector_id) if sector_id else None

    def sectors_of(self, holder):
        with self._lock:
            return sorted(s for s, h in self.holders.items() if h == holder)

    def snapshot(self):
        with self._lock:
            return dict(self.holders)

    # ------------------------------------------------------------------ changes
    def _check(self, sector_id):
        if sector_id not in sectors.BY_ID:
            raise ControlError("unknown_sector", 404, sector=sector_id)

    def take(self, sector_id, holder, force=False):
        """Assign a sector to holder. Returns the previous holder (or None)."""
        self._check(sector_id)
        with self._lock:
            cur = self.holders.get(sector_id)
            if cur == holder:
                return cur
            if cur and cur.startswith("human:") and not force:
                raise ControlError("sector_held", sector=sectors.BY_ID[sector_id]["name"], holder=display(cur))
            self.holders[sector_id] = holder
            if holder.startswith("human:"):
                self._seen[holder[6:]] = self.clock()
            return cur

    def release(self, sector_id, holder, force=False):
        """Free a sector. Only its holder may release it, unless forced (admins)."""
        self._check(sector_id)
        with self._lock:
            cur = self.holders.get(sector_id)
            if cur is None:
                return None
            if cur != holder and not force:
                raise ControlError("sector_held", 403, sector=sectors.BY_ID[sector_id]["name"], holder=display(cur))
            del self.holders[sector_id]
            return cur

    def assign_ai(self, sector_id, agent, by_holder, force=False):
        """Hand a sector to an AI agent: free sectors, your own, or (forced) anyone's."""
        self._check(sector_id)
        with self._lock:
            cur = self.holders.get(sector_id)
            if cur and cur.startswith("human:") and cur != by_holder and not force:
                raise ControlError("sector_held", sector=sectors.BY_ID[sector_id]["name"], holder=display(cur))
            self.holders[sector_id] = "ai:" + agent
            return cur

    # ------------------------------------------------------------------ presence
    def seen(self, username):
        self._seen[username] = self.clock()

    def reap_idle(self):
        """Release sectors of human holders not seen for idle_s. Returns [(sector, holder)]."""
        if not self.idle_s:
            return []
        now = self.clock()
        released = []
        with self._lock:
            for sid, h in list(self.holders.items()):
                if h.startswith("human:") and now - self._seen.get(h[6:], 0) > self.idle_s:
                    del self.holders[sid]
                    released.append((sid, h))
        return released
