"""Build and runtime information for the About dialog (and support requests)."""

import datetime
import os
import platform
import subprocess
import time
from importlib import metadata

from . import __author__, __version__, config

OWNER = __author__
FIRST_YEAR = 2026
STARTED_AT = time.time()


def _git(*args):
    try:
        out = subprocess.run(["git", *args], cwd=str(config.ROOT), capture_output=True,
                             text=True, timeout=3)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _build():
    """Commit and build date: baked into release images, discovered from git otherwise."""
    commit = os.environ.get("VISOR_COMMIT")
    build_date = os.environ.get("VISOR_BUILD_DATE") or None
    modified = False
    if not commit or commit == "unknown":
        commit = _git("rev-parse", "HEAD")
        modified = bool(_git("status", "--porcelain", "--untracked-files=no"))
    return commit, build_date, modified


_COMMIT, _BUILD_DATE, _MODIFIED = _build()


def _dep(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def copyright_line():
    year = datetime.datetime.now(datetime.timezone.utc).year
    if _BUILD_DATE:
        year = int(_BUILD_DATE[:4])
    years = str(FIRST_YEAR) if year <= FIRST_YEAR else "%d–%d" % (FIRST_YEAR, year)
    return "© %s %s. All rights reserved." % (years, OWNER)


def about():
    source = (os.environ.get("VISOR_SOURCE_URL") or "").rstrip("/") or None
    release = __version__ != "dev" and "-dev." not in __version__
    return {
        "name": "Visor ATC",
        "description": "3D air traffic control working position on live and recorded OpenSky "
                       "data, with an API for decision-making AI agents.",
        "version": __version__,
        "build": {
            "type": "release" if release else "development",
            "commit": _COMMIT,
            "commit_short": _COMMIT[:7] if _COMMIT else None,
            "commit_url": "%s/commit/%s" % (source, _COMMIT) if source and _COMMIT else None,
            "modified": _MODIFIED,
            "date": _BUILD_DATE,
            "source": source,
        },
        "runtime": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": "%s %s (%s)" % (platform.system(), platform.release(), platform.machine()),
            "started_at": STARTED_AT,
            "dependencies": {n: _dep(n) for n in ("fastapi", "starlette", "uvicorn", "pydantic")},
        },
        "owner": OWNER,
        "credits": "Owner, creator and developer: %s" % OWNER,
        "copyright": copyright_line(),
        "third_party": [
            {"name": "The OpenSky Network", "use": "flight data", "url": "https://opensky-network.org"},
            {"name": "OurAirports", "use": "airports and navaids (public domain)", "url": "https://ourairports.com"},
            {"name": "Esri", "use": "basemap and satellite imagery", "url": "https://www.esri.com"},
            {"name": "Mapzen / AWS Terrain Tiles", "use": "elevation data", "url": "https://registry.opendata.aws/terrain-tiles/"},
            {"name": "three.js", "use": "3D rendering (MIT)", "url": "https://threejs.org"},
            {"name": "FastAPI / Uvicorn", "use": "web server (MIT / BSD)", "url": "https://fastapi.tiangolo.com"},
        ],
    }
