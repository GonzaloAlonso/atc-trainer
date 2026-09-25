"""Visor ATC simulator package.

Copyright (c) 2026 Gonzalo Alonso. All rights reserved.
"""

import os

# Injected at image build time (see Dockerfile / release workflow); "dev" for local runs.
__version__ = os.environ.get("VISOR_VERSION") or "dev"
__author__ = "Gonzalo Alonso"
__copyright__ = "Copyright (c) 2026 Gonzalo Alonso. All rights reserved."
