"""Export a normalized PEP 503 environment inventory (name==version).

Uses only ``importlib.metadata`` from the standard library so that the same
wheel installed from different directories yields the same inventory.  Excludes
the pip/setuptools/wheel bootstrap tools.
"""

from __future__ import annotations

import argparse
import importlib.metadata
from pathlib import Path

_BOOTSTRAP = {"pip", "setuptools", "wheel"}


def _normalize(name: str) -> str:
    return name.replace("-", "_").replace(".", "_").lower()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    rows: list[str] = []
    for dist in importlib.metadata.distributions():
        name = (dist.metadata.get("Name") or "").strip()
        if not name or _normalize(name) in _BOOTSTRAP:
            continue
        version = (dist.version or "").strip()
        rows.append(f"{_normalize(name)}=={version}")
    rows.sort()
    Path(args.output).write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
