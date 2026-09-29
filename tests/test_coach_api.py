"""Coach, exercises and sessions through the HTTP API."""

import sqlite3

from atc.auth import AuthStore, hash_password

EX = {"X-ATC-Context": "exercise"}


def test_prefs_are_remembered(client):
    me = client.get("/api/auth/me").json()
    assert me["coach_level"] == "hints" and me["lang"] is None
    r = client.post("/api/auth/prefs", json={"lang": "de", "coach_level": "advise"})
    assert r.status_code == 200 and r.json()["lang"] == "de" and r.json()["coach_level"] == "advise"
    assert client.get("/api/auth/me").json()["coach_level"] == "advise"
    assert client.get("/api/coach").json()["level"] == "advise"
    assert client.post("/api/auth/prefs", json={"lang": "fr"}).status_code == 422
    assert client.post("/api/coach", json={"level": "evaluate"}).json()["level"] == "evaluate"
    assert client.get("/api/auth/me").json()["coach_level"] == "evaluate"
    assert client.post("/api/coach/hint", json={"decision": "D999"}).status_code == 404


def test_existing_accounts_keep_the_coach_off(tmp_path):
    db = tmp_path / "users.db"
    old = sqlite3.connect(str(db))       # schema as released in 2.0
    old.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL, role TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0,
            must_change INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL, last_login REAL,
            tutorial_state TEXT, tutorial_step INTEGER NOT NULL DEFAULT 0, tutorial_updated REAL);
    """)
    old.execute("INSERT INTO users (username, password_hash, role, created, updated) VALUES (?, ?, 'admin', 0, 0)",
                ("veteran", hash_password("veteran-password")))
    old.commit()
    old.close()
    store = AuthStore(db)
    assert store.find_user("veteran")["coach_level"] == "off"
    assert store.create_user("rookie", "rookie-password", "controller")["coach_level"] is None


def test_exercise_end_to_end(client):
    cat = client.get("/api/exercises").json()["exercises"]
    assert [e["id"] for e in cat][:2] == ["head-on", "crossing"] and cat[0]["best"] is None
    assert client.post("/api/exercises/nope/start").status_code == 404

    client.post("/api/coach", json={"level": "advise"})
    r = client.post("/api/exercises/head-on/start")
    assert r.status_code == 200
    sid = r.json()["session"]
    obs = client.get("/api/observation", headers=EX).json()
    assert {"TRN101", "TRN202"} <= {a["callsign"] for a in obs["aircraft"]}
    assert obs["me"]["sectors"] == ["ALP-U"]
    assert client.get("/api/observation", headers={"X-ATC-Context": "tutorial"}).status_code == 409

    # the trainee owns the sandbox's clock: step it to the alert
    assert client.post("/api/sim", json={"action": "lockstep", "lockstep": True}, headers=EX).status_code == 200
    dec = []
    for _ in range(40):
        client.post("/api/sim", json={"action": "step", "dt": 10}, headers=EX)
        dec = [d for d in client.get("/api/decisions", headers=EX).json() if d["kind"] == "conflict"]
        if dec:
            break
    assert dec, "the head-on alert should have fired"
    d = dec[0]
    wi = client.get("/api/decisions/%s/whatif" % d["id"], params={"option": d["best"]}, headers=EX)
    assert wi.status_code == 200
    body = wi.json()
    assert set(d["subjects"]) <= set(body["tracks"]) and body["why"]["key"].startswith("why.")
    probe = client.post("/api/probe", json={"command": "TRN101 D 300"}, headers=EX).json()
    assert probe["aircraft"] == "TRN101" and "baseline" in probe
    assert client.post("/api/probe", json={"command": "TRN101"}, headers=EX).status_code == 400

    # hints at the Hints level; the answer counts
    client.post("/api/coach", json={"level": "hints"})
    h = client.post("/api/coach/hint", json={"decision": d["id"]}, headers=EX).json()
    assert h["tier"] == 1 and h["message"]["key"] == "hint.c1"
    listed = next(x for x in client.get("/api/decisions", headers=EX).json() if x["id"] == d["id"])
    assert listed["questions"][0]["options"] == [] and listed["hidden_options"] > 0
    r = client.post("/api/decisions/%s" % d["id"], json={"answers": {"action": d["best"]}}, headers=EX)
    assert r.status_code == 403 and "hidden" in r.json()["detail"]
    for tier in (2, 3):
        h = client.post("/api/coach/hint", json={"decision": d["id"]}, headers=EX).json()
        assert h["tier"] == tier
    assert h["revealed"] == [d["best"]]
    r = client.post("/api/decisions/%s" % d["id"], json={"answers": {"action": d["best"]}}, headers=EX)
    assert r.status_code == 200

    # to the end of the exercise: graded, stored, debriefed
    for _ in range(40):
        st = client.post("/api/sim", json={"action": "step", "dt": 10}, headers=EX).json()
        if client.get("/api/coach", headers=EX).json()["graded"]:
            pass
    ses = client.get("/api/sessions").json()["sessions"]
    mine = next(s for s in ses if s["id"] == sid)
    assert mine["context"] == "exercise" and mine["exercise"] == "head-on"
    full = client.get("/api/sessions/%d" % sid).json()
    (entry,) = [e for e in full["entries"] if e["kind"] == "conflict"]
    assert entry["hint_tier"] == 3 and entry["grade"]["parts"]["hints"] == -15
    assert full["replay"] is True
    frames = client.get("/api/sessions/%d/replay" % sid).json()["frames"]
    assert frames and {"t", "ac", "conflicts"} <= set(frames[0])

    deb = client.post("/api/sessions/%d/debrief" % sid, json={"lang": "es"}).json()
    assert deb["provider"] == "template" and deb["messages"][0]["key"] in ("db.verdict", "db.empty")
    ans = client.post("/api/sessions/%d/ask" % sid, json={"question": "Why?", "lang": "de"}).json()
    assert ans["messages"][0]["key"] == "ask.unavailable"

    # rewind to just before the situation: a new attempt
    rw = client.post("/api/exercise/rewind", json={"entry": entry["id"]}, headers=EX)
    assert rw.status_code == 200 and rw.json()["attempt"] == 2 and rw.json()["session"] != sid
    assert client.post("/api/exercise/stop").json()["stopped"] is True
    assert client.get("/api/observation", headers=EX).status_code == 409
    other = client.get("/api/sessions/%d" % rw.json()["session"]).json()["session"]
    assert other["parent"] == sid and other["status"] == "abandoned"


def test_agents_with_a_token_see_every_option(client):
    """The coach level shapes what a person sees; agents (bearer token) always get all options."""
    tok = client.post("/api/auth/login", json={"username": "admin", "password": "admin-password-123"}).json()["token"]
    client.post("/api/coach", json={"level": "evaluate"})
    client.post("/api/exercises/head-on/start")
    client.post("/api/sim", json={"action": "lockstep", "lockstep": True}, headers=EX)
    for _ in range(40):
        client.post("/api/sim", json={"action": "step", "dt": 10}, headers=EX)
        if client.get("/api/decisions", headers=EX).json():
            break
    ui = client.get("/api/decisions", headers=EX).json()[0]
    assert ui["questions"][0]["options"] == []
    agent = {**EX, "Authorization": "Bearer " + tok}
    from fastapi.testclient import TestClient
    raw = TestClient(client.app)                  # no cookie: only the token
    d = raw.get("/api/decisions", headers=agent).json()[0]
    assert d["questions"][0]["options"] and d["best"]
    assert raw.post("/api/decisions/%s" % d["id"], json={"answers": {"action": d["best"]}, "by": "ai:jev"},
                    headers=agent).status_code == 200


def test_old_ai_modes_are_remembered_as_coach_levels(client):
    assert client.post("/api/ai", json={"mode": "advisory"}).status_code == 200
    assert client.get("/api/auth/me").json()["coach_level"] == "advise"
    client.post("/api/ai", json={"mode": "off"})
    assert client.get("/api/auth/me").json()["coach_level"] == "off"


def test_errors_speak_the_users_language(client):
    de = {"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}
    r = client.post("/api/command", json={"text": "NOBODY C 350"}, headers=de)
    assert r.status_code == 400 and r.json()["code"] == "no_aircraft"
    assert r.json()["detail"] == "Kein Luftfahrzeug NOBODY" and r.json()["params"] == {"ident": "NOBODY"}
    r = client.post("/api/command", json={"text": "X"}, headers={"Accept-Language": "es"})
    assert r.json()["detail"].startswith("Formato esperado")
    assert client.post("/api/exercises/nope/start", headers=de).json()["detail"] == "Übung nicht gefunden"
    # without a header: the account's language, else English (agents)
    assert client.post("/api/exercises/nope/start").json()["detail"] == "no such exercise"
    client.post("/api/auth/prefs", json={"lang": "es"})
    assert client.post("/api/exercises/nope/start").json()["detail"] == "Ejercicio no encontrado"
    # validation errors keep FastAPI's list, with a readable message next to it
    r = client.post("/api/coach", json={"level": "wizard"}, headers=de)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)
    assert r.json()["message"] == "ungültige Anfrage: prüfe level"
    # signed out, the browser's language decides
    from fastapi.testclient import TestClient
    anon = TestClient(client.app)
    assert anon.get("/api/status", headers=de).json()["detail"] == "Anmeldung erforderlich"
    bad = anon.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"}, headers={"Accept-Language": "es"})
    assert bad.status_code == 401 and bad.json()["detail"] == "Usuario o contraseña incorrectos"


def test_pick_lang():
    from atc.errors import pick_lang
    assert pick_lang("fr-FR,fr;q=0.9,es;q=0.8,en;q=0.5") == "es"
    assert pick_lang("en;q=0.2,de;q=0.9") == "de"
    assert pick_lang("fr", "de") == "de"
    assert pick_lang(None, None) == "en"
