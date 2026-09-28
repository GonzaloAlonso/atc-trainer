from atc import __version__


def test_api_smoke(client):
    st = client.get("/api/status").json()
    assert st["version"] == __version__
    assert st["recording"] is False
    assert "coverage" in st["recorder"]

    assert client.get("/").status_code == 200
    assert "ATC Trainer" in client.get("/").text
    assert client.get("/js/main.js").status_code == 200

    obs = client.get("/api/observation").json()
    assert {"aircraft", "conflicts", "decisions", "score"} <= obs.keys()

    schema = client.get("/api/schema").json()
    assert "CLIMB" in schema["clearance_kinds"]

    assert len(client.get("/api/sectors").json()) >= 5
    assert client.post("/api/command", json={"text": "NOBODY C 350"}).status_code == 400
    assert client.post("/api/sector", json={"sector": "NOPE"}).status_code == 400

    r = client.post("/api/sim", json={"action": "lockstep", "lockstep": True})
    assert r.status_code == 200 and r.json()["sim"]["lockstep"]
    r = client.post("/api/sim", json={"action": "step", "dt": 5})
    assert r.status_code == 200 and "aircraft" in r.json()

    with client.websocket_connect("/ws") as ws:
        frame = ws.receive_json()
        assert frame["type"] == "frame" and "ac" in frame


def test_ui_is_revalidated_after_upgrades(client):
    """Pages and assets must not be served stale from the browser cache after a new release."""
    for path in ("/", "/js/main.js", "/js/tutorial.js", "/css/style.css", "/admin"):
        assert client.get(path).headers.get("cache-control") == "no-cache", path
    assert client.get("/login").headers.get("cache-control") == "no-cache"
    assert "no-cache" not in (client.get("/api/status").headers.get("cache-control") or "")
