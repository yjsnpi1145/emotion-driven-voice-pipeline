from __future__ import annotations

import sys
from pathlib import Path

# Ensure the repository root is importable so tests can import the
# `workers` package (the worker is run as `python -m workers.indextts2`).
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
