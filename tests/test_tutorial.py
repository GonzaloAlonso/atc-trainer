import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from atc.api import create_app
from atc.auth import AuthStore, hash_password

from conftest import ADMIN, FakeNav, login

TUT = {"X-ATC-Context": "tutorial"}


# ---------------------------------------------------------------------------- per-user state
def test_existing_accounts_are_marked_trained_by_the_migration(tmp_path):
    db = tmp_path / "users.db"
    old = sqlite3.connect(str(db))       # schema as released before the tutorial existed
    old.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL, role TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0,
            must_change INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL, last_login REAL);
    """)
    old.execute("INSERT INTO users (username, password_hash, role, created, updated) VALUES (?, ?, 'admin', 0, 0)",
                ("veteran", hash_password("veteran-password")))
    old.commit()
    old.close()

    store = AuthStore(db)
    assert store.find_user("veteran")["tutorial_state"] == "completed"
    newbie = store.create_user("newbie", "newbie-password", "controller")
    assert newbie["tutorial_state"] is None and newbie["tutorial_step"] == 0
    AuthStore(db)                        # re-opening doesn't migrate twice
    assert store.find_user("newbie")["tutorial_state"] is None


def test_tutorial_state_endpoints(client):
    me = client.get("/api/auth/me").json()
    assert me["tutorial_state"] is None, "a fresh install's admin is offered the tutorial"
    assert client.post("/api/tutorial/progress", json={"step": 4}).json()["tutorial_step"] == 4
    assert client.post("/api/tutorial/dismiss").json()["tutorial_state"] == "dismissed"
    done = client.post("/api/tutorial/complete").json()
    assert done["tutorial_state"] == "completed"
    assert client.get("/api/auth/me").json()["tutorial_state"] == "completed"

    uid = me["id"]
    reset = client.patch(f"/api/admin/users/{uid}", json={"tutorial_reset": True}).json()
    assert reset["tutorial_state"] is None and reset["tutorial_step"] == 0
    assert client.get("/api/admin/users").json()[0]["tutorial_state"] is None


# ---------------------------------------------------------------------------- sandbox isolation
def test_sandbox_is_private_and_separate_from_live(client):
    assert client.get("/api/observation", headers=TUT).status_code == 409   # no session yet
    assert client.post("/api/tutorial/start").status_code == 200
    obs = client.get("/api/observation", headers=TUT).json()
    calls = {a["callsign"] for a in obs["aircraft"]}
    assert "TRN303" in calls

    r = client.post("/api/command", json={"text": "TRN303 C 370"}, headers=TUT)
    assert r.status_code == 200 and r.json()["accepted"]
    assert client.get("/api/aircraft/TRN303", headers=TUT).json()["assigned"]["alt"] == 37000
    # the shared simulation has no training traffic and is untouched
    assert client.post("/api/command", json={"text": "TRN303 C 370"}).status_code == 400
    assert "TRN303" not in {a["callsign"] for a in client.get("/api/observation").json()["aircraft"]}
    # sandboxes can't be reset into live traffic
    assert client.post("/api/sim", json={"action": "reset", "mode": "live"}, headers=TUT).status_code == 400

    with client.websocket_connect("/ws?ctx=tutorial") as ws:
        frame = ws.receive_json()
        assert "TRN303" in {row[1] for row in frame["ac"]}

    inj = client.post("/api/tutorial/scenario", json={"event": "conflict"}).json()
    assert inj["aircraft"] == ["TRN101", "TRN202"]
    calls = {a["callsign"] for a in client.get("/api/observation", headers=TUT).json()["aircraft"]}
    assert {"TRN101", "TRN202"} <= calls

    assert client.post("/api/tutorial/stop").json()["stopped"] is True
    assert client.get("/api/observation", headers=TUT).status_code == 409


def test_sandboxes_are_per_user(client):
    client.post("/api/admin/users", json={"username": "trainee", "password": "trainee-password", "must_change": False})
    with TestClient(client.app) as other:
        login(other, "trainee", "trainee-password")
        client.post("/api/tutorial/start")
        client.post("/api/command", json={"text": "TRN303 C 370"}, headers=TUT)
        other.post("/api/tutorial/start")
        mine = other.get("/api/aircraft/TRN303", headers=TUT).json()
        assert mine["assigned"]["alt"] is None, "another trainee's clearance must not leak"


# ---------------------------------------------------------------------------- scripted scenario
def _sandbox():
    from atc.training import Sandbox
    box = Sandbox(FakeNav())
    box.engine.stop()                    # drive the engine by hand, deterministically
    time.sleep(0.6)
    return box


def _advance(e, seconds):
    for _ in range(int(seconds / 2)):
        e.step(2.0)


def test_scenario_produces_conflict_and_pilot_request():
    box = _sandbox()
    e = box.engine
    e.set_sector("ALPS-UPPER")
    assert e.find("TRN303") is not None and len(e._in_sector) >= 5

    assert e.command("TRN303 C 370", "human:trainee")["accepted"]
    box.inject_conflict()
    pair = {e.find("TRN101").id, e.find("TRN202").id}
    seen = False
    for _ in range(150):                 # STCA within ~5 minutes
        _advance(e, 2)
        if any({c["a"], c["b"]} == pair for c in e.conflicts):
            seen = True
            break
    assert seen, "the injected head-on pair must trigger STCA"

    e.command("TRN101 C 360", "human:trainee")
    request = None
    for _ in range(600):
        _advance(e, 2)
        request = next((d for d in e.decisions.values()
                        if d.kind == "level_request" and d.status == "open"), None)
        if request:
            break
    assert request is not None and request.subject_ids == [e.find("TRN303").id]
    assert e.score["los"] == 0, "the vertical solution keeps them separated"


# ---------------------------------------------------------------------------- lifecycle limits
def test_idle_reaper_and_cap():
    from atc.training import TutorialManager
    mgr = TutorialManager(FakeNav(), max_sandboxes=1, idle_s=10)
    try:
        mgr.start(1)
        with pytest.raises(OverflowError):
            mgr.start(2)
        assert mgr.reap(now=time.time() + 5) == 0
        assert mgr.reap(now=time.time() + 60) == 1
        assert mgr.get(1) is None
        mgr.start(2)                     # capacity freed
    finally:
        mgr.stop_all()
