"""Idle sleep: nobody online -> simulation stops and drops its scenario; a user wakes it."""

import threading
import time

from conftest import ADMIN, login


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeTutorials:
    def __init__(self):
        self.stopped = 0

    def stop_all(self):
        self.stopped += 1


def _sim_threads():
    return [t for t in threading.enumerate() if t.name == "sim" and t.is_alive()]


def test_engine_hibernate_and_wake(engine):
    assert engine.aircraft
    engine.control.take("ALP-U", "human:alice")
    engine.start()
    assert engine.hibernate()
    assert not engine.hibernate()              # already asleep
    assert engine.hibernating and not engine._running
    assert engine.aircraft == {} and engine.loader.plans == {} and engine.conflicts == []
    assert '"asleep":true' in engine.frame_for("human:alice")
    time.sleep(0.8)
    assert not _sim_threads()                  # the loop thread has exited
    assert engine.control.snapshot().get("ALP-U") == "human:alice"   # assignments survive

    assert engine.wake() and not engine.wake()
    assert not engine.hibernating and engine._running and engine.mode == "live"
    engine.stop()


def test_quick_wake_leaves_a_single_loop(engine):
    engine.start()
    engine.hibernate()
    engine.wake()                              # before the old thread noticed the stop
    time.sleep(0.8)
    assert len(_sim_threads()) == 1
    engine.stop()
    time.sleep(0.8)
    assert not _sim_threads()


def test_power_manager_sleeps_after_idle_and_wakes_on_activity(engine):
    from atc.power import PowerManager
    clock, tut = FakeClock(), FakeTutorials()
    pm = PowerManager(engine, tut, idle_s=300, clock=clock)

    clock.now += 299
    assert not pm.check() and not pm.asleep
    clock.now += 120
    assert not pm.check(connected=1)           # an open scope counts as someone online
    clock.now += 299
    assert not pm.check()
    clock.now += 2
    assert pm.check() and pm.asleep and tut.stopped == 1
    assert not pm.check()                      # already asleep

    pm.activity()
    assert not pm.asleep and engine._running
    engine.stop()


def test_power_manager_disabled(engine):
    from atc.power import PowerManager
    clock = FakeClock()
    pm = PowerManager(engine, FakeTutorials(), idle_s=0, clock=clock)
    clock.now += 10 ** 6
    assert not pm.check() and not pm.asleep


def test_api_sleeps_and_sign_in_wakes(fresh_paths):
    from fastapi.testclient import TestClient
    from atc.api import create_app
    app = create_app()
    with TestClient(app) as c:
        power = app.state.power
        power.last_activity -= power.idle_s + 1
        assert power.check()
        assert c.get("/api/health").status_code == 200     # the health probe doesn't wake it
        assert c.get("/login").status_code == 200
        assert power.asleep
        login(c, *ADMIN)
        assert not power.asleep
        st = c.get("/api/status").json()
        assert st["power"]["asleep"] is False and st["power"]["idle_sleep_s"] == power.idle_s

        power.last_activity -= power.idle_s + 1
        assert power.check()
        assert c.get("/api/status").json()["power"]["asleep"] is False   # any signed-in request wakes

        power.last_activity -= power.idle_s + 1
        assert power.check()
        with c.websocket_connect("/ws") as ws:                         # so does opening the scope
            assert ws.receive_json()["asleep"] is False
