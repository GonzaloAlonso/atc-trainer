import time

import pytest
from fastapi.testclient import TestClient

from atc.api import create_app
from atc.auth import AuthError, AuthStore, hash_password, verify_password

from conftest import ADMIN, login, write_scenario


def test_password_hashing():
    h = hash_password("correct horse battery")
    assert h.startswith("pbkdf2_sha256$") and "correct" not in h
    assert verify_password("correct horse battery", h)
    assert not verify_password("wrong", h)
    assert not verify_password("x", "garbage")


def test_bootstrap_generates_password_when_none_given(tmp_path):
    store = AuthStore(tmp_path / "u.db")
    generated = store.bootstrap(username="root", password="")
    assert generated and len(generated) >= 12
    user = store.find_user("root")
    assert user["role"] == "admin" and user["must_change"]
    assert store.authenticate("root", generated)["username"] == "root"
    # a second bootstrap never touches existing accounts
    assert store.bootstrap(username="other", password="whatever-123") is None
    assert [u["username"] for u in store.list_users()] == ["root"]


def test_everything_requires_login(fresh_paths):
    with TestClient(create_app()) as c:
        assert c.get("/api/health").json()["ok"] is True
        assert c.get("/login").status_code == 200
        assert c.get("/css/style.css").status_code == 200
        assert c.get("/api/status").status_code == 401
        assert c.post("/api/command", json={"text": "X C 350"}).status_code == 401
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/login?next=/"
        assert c.get("/js/main.js", follow_redirects=False).status_code == 302
        with c.websocket_connect("/ws") as ws:
            with pytest.raises(Exception):
                ws.receive_text()          # closed with 4401


def test_login_logout_and_throttling(fresh_paths):
    with TestClient(create_app()) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code == 401
        body = login(c, *ADMIN)
        assert body["user"]["role"] == "admin" and body["token"]
        assert c.get("/api/status").status_code == 200
        assert c.get("/api/auth/me").json()["username"] == "admin"
        c.post("/api/auth/logout")
        assert c.get("/api/status").status_code == 401

        for _ in range(5):
            c.post("/api/auth/login", json={"username": "admin", "password": "bad-guess"})
        r = c.post("/api/auth/login", json={"username": "admin", "password": ADMIN[1]})
        assert r.status_code == 429


def test_bearer_token_for_agents(client):
    token = client.post("/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]}).json()["token"]
    client.cookies.clear()
    assert client.get("/api/observation").status_code == 401
    assert client.get("/api/observation", headers={"Authorization": "Bearer " + token}).status_code == 200


def test_cross_origin_post_refused(client):
    r = client.post("/api/sim", json={"action": "pause"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/sim", json={"action": "pause"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200


def test_user_administration_and_first_login(client):
    r = client.post("/api/admin/users", json={"username": "alice", "password": "alice-initial-pw", "role": "controller"})
    assert r.status_code == 201 and r.json()["must_change"]
    alice_id = r.json()["id"]
    assert client.post("/api/admin/users", json={"username": "ALICE", "password": "x" * 12}).status_code == 400
    assert client.post("/api/admin/users", json={"username": "bob", "password": "short"}).status_code == 400

    with TestClient(client.app) as a:
        login(a, "alice", "alice-initial-pw")
        # password change is mandatory before anything else
        r = a.get("/api/status")
        assert r.status_code == 403 and r.json()["detail"] == "password change required"
        assert a.get("/", follow_redirects=False).headers["location"] == "/login?change=1"
        r = a.post("/api/auth/password", json={"current_password": "wrong", "new_password": "alice-new-password"})
        assert r.status_code == 400
        r = a.post("/api/auth/password", json={"current_password": "alice-initial-pw", "new_password": "alice-new-password"})
        assert r.status_code == 200
        assert a.get("/api/status").status_code == 200
        # controllers can't administer users
        assert a.get("/api/admin/users").status_code == 403
        assert a.get("/admin", follow_redirects=False).headers["location"] == "/"

        # admin resets alice's password: her sessions are revoked
        client.patch(f"/api/admin/users/{alice_id}", json={"password": "reset-by-admin-1", "must_change": False})
        assert a.get("/api/status").status_code == 401
        login(a, "alice", "reset-by-admin-1")

        # disabling signs her out and blocks login
        client.patch(f"/api/admin/users/{alice_id}", json={"disabled": True})
        assert a.get("/api/status").status_code == 401
        assert a.post("/api/auth/login", json={"username": "alice", "password": "reset-by-admin-1"}).status_code == 401

    # promote, then delete
    assert client.patch(f"/api/admin/users/{alice_id}", json={"disabled": False, "role": "admin"}).json()["role"] == "admin"
    assert client.delete(f"/api/admin/users/{alice_id}").status_code == 200
    assert "alice" not in [u["username"] for u in client.get("/api/admin/users").json()]
    assert client.delete(f"/api/admin/users/{alice_id}").status_code == 404


def test_admin_cannot_lock_everyone_out(client):
    me = client.get("/api/auth/me").json()
    assert client.delete(f"/api/admin/users/{me['id']}").status_code == 400
    assert client.patch(f"/api/admin/users/{me['id']}", json={"role": "controller"}).status_code == 400
    assert client.patch(f"/api/admin/users/{me['id']}", json={"disabled": True}).status_code == 400


def test_last_admin_rule(tmp_path):
    store = AuthStore(tmp_path / "u.db")
    a = store.create_user("admin1", "password-0001", "admin")
    b = store.create_user("admin2", "password-0002", "admin")
    store.update_user(b["id"], actor_id=a["id"], role="controller")
    with pytest.raises(AuthError):
        store.update_user(a["id"], actor_id=b["id"], role="controller")
    with pytest.raises(AuthError):
        store.delete_user(a["id"], actor_id=b["id"])


def test_users_and_sessions_survive_restart(fresh_paths):
    with TestClient(create_app()) as c:
        login(c, *ADMIN)
        c.post("/api/admin/users", json={"username": "carol", "password": "carol-password", "must_change": False})
        token = c.post("/api/auth/login", json={"username": "carol", "password": "carol-password"}).json()["token"]

    with TestClient(create_app()) as c2:          # same data directory, new process state
        assert c2.get("/api/auth/me", headers={"Authorization": "Bearer " + token}).json()["username"] == "carol"
        login(c2, *ADMIN)                         # the env password did not re-create/reset anything
        assert {u["username"] for u in c2.get("/api/admin/users").json()} == {"admin", "carol"}


def test_clearances_are_attributed_to_the_user(client, fresh_paths):
    from atc.store import Store
    t0 = int(time.time()) - 7200
    write_scenario(Store(), t0)
    assert client.post("/api/sim", json={"action": "reset", "mode": "replay", "start": t0}).status_code == 200
    r = client.post("/api/command", json={"text": "TST003 D 370", "issuer": "human"})
    assert r.status_code == 200, r.text
    ev = [e for e in client.get("/api/events").json() if e["speaker"] == "ATC"][-1]
    assert ev["issuer"] == "human:admin"
    assert client.get("/api/aircraft/TST003").json()["controller"] == "human:admin"
    # external agents may still identify themselves
    r = client.post("/api/command", json={"text": "TST003 S 280", "issuer": "ai:test-agent"})
    ev = [e for e in client.get("/api/events").json() if e["speaker"] == "ATC"][-1]
    assert ev["issuer"] == "ai:test-agent"


def test_throttle_uses_forwarded_client_behind_proxy(fresh_paths, monkeypatch):
    """Behind Caddy every request comes from the proxy; one attacker must not lock out everyone."""
    from atc import config
    monkeypatch.setattr(config, "TRUST_PROXY", True)
    with TestClient(create_app()) as c:
        for i in range(20):                       # 20 failures from one client, varied usernames
            c.post("/api/auth/login", json={"username": "user%d" % i, "password": "bad"},
                   headers={"X-Forwarded-For": "203.0.113.9"})
        blocked = c.post("/api/auth/login", json={"username": "admin", "password": ADMIN[1]},
                         headers={"X-Forwarded-For": "203.0.113.9"})
        assert blocked.status_code == 429
        ok = c.post("/api/auth/login", json={"username": "admin", "password": ADMIN[1]},
                    headers={"X-Forwarded-For": "198.51.100.7"})
        assert ok.status_code == 200
