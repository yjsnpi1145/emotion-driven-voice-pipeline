from __future__ import annotations

from pathlib import Path

import yaml


REQUIRED = {
    "sqlalchemy": "reuse",
    "alembic": "reuse",
    "aiosqlite": "reuse",
    "portalocker": "reuse",
    "faster_whisper": "reuse",
    "silero_vad": "reuse",
    "rapidfuzz": "reuse",
    "sqlmodel": "rejected",
    "diskcache": "rejected",
}


def test_batch2_reuse_inventory_is_actionable() -> None:
    payload = yaml.safe_load(Path("config/open-source-reuse.yaml").read_text("utf-8"))
    modules = {module["module_id"]: module for module in payload["modules"]}

    for module_id, disposition in REQUIRED.items():
        entry = modules[module_id]
        assert entry["introduced_in_batch"] == 2
        assert entry["disposition"] == disposition
        assert entry["candidates"]
        candidate = entry["candidates"][0]
        assert candidate["repository"].startswith("https://")
        assert candidate["pin"]
        assert entry["wrapper_boundary"]
        assert entry["decision_reason"]
        if disposition == "reuse":
            assert candidate["spdx_license"]
            assert entry["selected"]
            assert entry["lock_reference"]
        else:
            assert entry["selected"] is None
            assert entry["rejected_reasons"]
