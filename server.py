#!/usr/bin/env python3
"""Visor ATC — start the recorder, simulator, API and web UI.

    .venv/bin/python server.py [--port 8000]

Optional environment:
    OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET   OpenSky API client (more data, 90 s resolution)
    JEV_ENDPOINT / JEV_API_KEY / JEV_MODEL      decision-model endpoint for the "jev" agent
"""

import argparse
import logging
import os

import uvicorn

from atc import __version__, config
from atc.api import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("VISOR_PORT", 8000)))
    parser.add_argument("--host", default=os.environ.get("VISOR_HOST", "127.0.0.1"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("visor").info(
        "Visor ATC %s — OpenSky %s, polling every %ds%s", __version__,
        "authenticated" if config.OPENSKY_AUTHENTICATED else "anonymous", config.POLL_INTERVAL_S,
        "" if config.RECORD else " (recording disabled)")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
