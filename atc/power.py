"""Idle sleep: when nobody is online, stop the simulation (and tutorial sandboxes) to save CPU
and memory. The OpenSky recorder keeps running so the 24 h history stays complete.

"Online" means a signed-in user made a request or has a scope (WebSocket) open. The engine
wakes with a fresh live scenario on the next sign-in, authenticated request or scope.
"""

import ctypes
import gc
import logging
import threading
import time

log = logging.getLogger("atc.power")


def release_memory():
    """Collect garbage and hand freed heap pages back to the OS (glibc only, e.g. in Docker)."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


class PowerManager:
    def __init__(self, engine, tutorials, idle_s, clock=time.monotonic):
        self.engine = engine
        self.tutorials = tutorials
        self.idle_s = idle_s              # 0 disables sleeping
        self.clock = clock
        self.last_activity = clock()
        self._lock = threading.Lock()

    @property
    def enabled(self):
        return self.idle_s > 0

    @property
    def asleep(self):
        return self.engine.hibernating

    def activity(self):
        """A signed-in user is around: remember it and wake the simulation if it sleeps."""
        self.last_activity = self.clock()
        if self.engine.hibernating:
            with self._lock:              # one waker; the others find it already awake
                if self.engine.wake():
                    log.info("user online: simulation resumed")

    def check(self, connected=0):
        """Called periodically. Open scopes count as activity. Returns True if it fell asleep."""
        if connected:
            self.last_activity = self.clock()
            return False
        if not self.enabled or self.engine.hibernating:
            return False
        if self.clock() - self.last_activity < self.idle_s:
            return False
        with self._lock:
            if self.engine.hibernate():
                self.tutorials.stop_all()
                release_memory()
                log.info("nobody online for %d s: simulation asleep", self.idle_s)
                return True
        return False

    def status(self):
        return {"asleep": self.asleep, "idle_sleep_s": self.idle_s,
                "idle_for_s": round(self.clock() - self.last_activity, 1)}
