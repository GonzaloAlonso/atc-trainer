from fastapi.testclient import TestClient

from atc import __version__
from atc.api import create_app


def test_api_smoke():
    with TestClient(create_app()) as client:
        st = client.get("/api/status").json()
        assert st["version"] == __version__
        assert st["recording"] is False
        assert "coverage" in st["recorder"]

        assert client.get("/").status_code == 200
        assert "Visor" in client.get("/").text
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
