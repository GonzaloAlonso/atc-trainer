"""Visor ATC simulator package."""

import os

# Injected at image build time (see Dockerfile / release workflow); "dev" for local runs.
__version__ = os.environ.get("VISOR_VERSION") or "dev"
