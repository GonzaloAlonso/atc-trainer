"""Multi-controller behaviour through the API: responsibility, authority and visibility."""
import json
import time

from fastapi.testclient import TestClient

from conftest import login, write_scenario

TUT = {"X-ATC-Context": "tutorial"}


def _setup(client):
    """Scenario: TST001/TST002 head-on at FL350 and TST003 at FL390, all in RHN-H (Rhine High).
    Admin (the client) holds RHN-U; bob holds RHN-H and is responsible for the whole scenario."""
    from atc.store import Store
    t0 = int(time.time()) - 7200
    write_scenario(Store(), t0)
    assert client.post("/api/sim", json={"action": "reset", "mode": "replay", "start": t0}).status_code == 200
    assert client.post("/api/sim", json={"action": "lockstep", "lockstep": True}).status_code == 200
    client.post("/api/admin/users", json={"username": "bob", "password": "bob-password-1", "must_change": False})
    bob = TestClient(client.app)
    bob.__enter__()
    login(bob, "bob", "bob-password-1")
    assert bob.post("/api/sectors/RHN-H/take").json()["sectors"] == ["RHN-H"]
    assert client.post("/api/sectors/RHN-U/take").json()["sectors"] == ["RHN-U"]
    return bob


def _run_until(client, pred, dt=10, limit=120):
    for _ in range(limit):
        obs = client.post("/api/sim", json={"action": "step", "dt": dt}).json()
        if pred(obs):
            return obs
    return None


def test_authority_visibility_and_scores(client):
    bob = _setup(client)
    try:
        # authority: only the holder of the aircraft's sector may clear it
        r = client.post("/api/command", json={"text": "TST003 C 400"})
        assert r.status_code == 403 and "controlled by bob" in r.json()["detail"]
        assert bob.post("/api/command", json={"text": "TST003 C 400"}).status_code == 200
        assert client.post("/api/sectors/RHN-H/take").status_code == 409

        # the head-on conflict in RHN-H is bob's: he sees it, the admin doesn't
        obs = _run_until(client, lambda o: bool(bob.get("/api/observation").json()["conflicts"]))
        assert obs is not None, "bob should get the STCA"
        assert client.get("/api/observation").json()["conflicts"] == []
        assert client.get("/api/observation?scope=all").json()["conflicts"], "admins can look at everything"
        assert bob.get("/api/observation?scope=all").json()["conflicts"] == bob.get("/api/observation").json()["conflicts"]

        alerts = lambda c: [e for e in c.get("/api/events").json() if e["kind"] == "alert"]
        assert alerts(bob) and not alerts(client)

        with bob.websocket_connect("/ws") as ws:
            assert json.loads(json.dumps(ws.receive_json()))["conflicts"]
        with client.websocket_connect("/ws") as ws:
            frame = ws.receive_json()
            assert frame["conflicts"] == [] and frame["me"]["sectors"] == ["RHN-U"]
            assert all(row[9] & (8 | 16 | 32 | 1) == 0 for row in frame["ac"]), "no per-viewer flags in shared rows"
            assert {"RHN-H": "human:bob", "RHN-U": "human:admin"}.items() <= {s: h for s, h, _ in frame["sectorization"] if h}.items()

        _run_until(client, lambda o: bool(bob.get("/api/decisions").json()), dt=5, limit=40)
        bob.post("/api/coach", json={"level": "off"})     # every option visible (the decision assistant)
        dps = bob.get("/api/decisions").json()
        assert dps and all("human:bob" in d["holders"] for d in dps)
        assert client.get("/api/decisions").json() == []
        dp = dps[0]
        opt = next(o for o in dp["questions"][0]["options"] if o["clearances"])
        assert client.post(f"/api/decisions/{dp['id']}", json={"answers": {"action": opt["id"]}}).status_code == 403

        assert bob.get("/api/observation").json()["score"]["stca"] >= 1
        assert client.get("/api/observation").json()["score"]["stca"] == 0
    finally:
        bob.__exit__(None, None, None)


def test_shared_sim_control_is_admin_only(client):
    bob = _setup(client)
    try:
        r = bob.post("/api/sim", json={"action": "pause"})
        assert r.status_code == 403
        assert bob.post("/api/tutorial/start").status_code == 200
        assert bob.post("/api/sim", json={"action": "speed", "speed": 4}, headers=TUT).status_code == 200
        assert bob.post("/api/sectors/ALP-U/take", headers=TUT).json()["sectors"] == ["ALP-U"]
        assert bob.get("/api/sectors").json()["me"]["sectors"] == ["RHN-H"], "sandbox sectors are separate"
    finally:
        bob.__exit__(None, None, None)


def test_ai_held_sector_controls_itself(client):
    bob = _setup(client)
    try:
        # admin hands bob's sector to the AI (forced), bob can no longer clear there
        assert client.post("/api/sectors/RHN-H/assign-ai", json={"agent": "rules", "force": True}).status_code == 200
        r = bob.post("/api/command", json={"text": "TST001 C 360"})
        assert r.status_code == 403 and "AI (rules)" in r.json()["detail"]

        eng = client.app.state.engine
        ai_done = lambda: [d for d in list(eng.decisions.values()) if d.answered_by == "ai:rules"]
        for _ in range(150):
            client.post("/api/sim", json={"action": "step", "dt": 5})
            time.sleep(0.02)                              # the agent answers from its worker thread
            if ai_done():
                break
        executed = ai_done()
        assert executed, "the AI holding RHN-H should resolve the head-on conflict itself"
        assert executed[0].holders == ["ai:rules"]
        ai_events = [e for e in client.get("/api/events?since=0").json() if e["kind"] == "ai"]
        assert ai_events == [], "the AI's radio in RHN-H is not the admin's business"

        # a human can take the sector back from the AI
        assert bob.post("/api/sectors/RHN-H/take").status_code == 200
        assert bob.post("/api/command", json={"text": "TST003 C 400"}).status_code == 200
    finally:
        bob.__exit__(None, None, None)
