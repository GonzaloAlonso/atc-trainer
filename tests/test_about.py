from fastapi.testclient import TestClient

from atc import __version__
from atc.api import create_app


def test_about_requires_login_and_states_ownership(fresh_paths):
    with TestClient(create_app()) as c:
        assert c.get("/api/about").status_code == 401
        health = c.get("/api/health").json()
        assert "Gonzalo Alonso" in health["copyright"]


def test_about_contents(client):
    a = client.get("/api/about").json()
    assert a["name"] == "Visor ATC" and a["version"] == __version__
    assert a["owner"] == "Gonzalo Alonso"
    assert a["copyright"].startswith("© 2026") and "Gonzalo Alonso. All rights reserved." in a["copyright"]
    assert "creator and developer: Gonzalo Alonso" in a["credits"]
    assert a["build"]["type"] in ("release", "development")
    assert {"python", "platform", "dependencies"} <= a["runtime"].keys()
    assert any(t["name"] == "The OpenSky Network" for t in a["third_party"])


def test_build_metadata_from_environment(monkeypatch):
    import importlib
    import atc.about as about_mod
    monkeypatch.setenv("VISOR_COMMIT", "0123456789abcdef0123456789abcdef01234567")
    monkeypatch.setenv("VISOR_BUILD_DATE", "2027-03-01T10:00:00Z")
    monkeypatch.setenv("VISOR_SOURCE_URL", "https://github.com/GonzaloAlonso/visor")
    mod = importlib.reload(about_mod)
    try:
        a = mod.about()
        assert a["build"]["commit_short"] == "0123456"
        assert a["build"]["commit_url"] == "https://github.com/GonzaloAlonso/visor/commit/0123456789abcdef0123456789abcdef01234567"
        assert a["build"]["date"] == "2027-03-01T10:00:00Z" and not a["build"]["modified"]
        assert a["copyright"] == "© 2026–2027 Gonzalo Alonso. All rights reserved."
    finally:
        monkeypatch.undo()
        importlib.reload(about_mod)
