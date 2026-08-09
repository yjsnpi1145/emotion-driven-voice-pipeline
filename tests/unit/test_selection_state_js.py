from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

MODULE = (
    Path(__file__).parents[2] / "src" / "voice_pipeline" / "webui" / "selection-state.js"
)


def _run(expression: str) -> Any:
    source = f"""
import {{
  readWorkbenchSelection,
  writeWorkbenchSelection,
  clearWorkbenchSelection,
  chooseInitialRunId,
}} from {json.dumps(MODULE.as_uri())};
{expression}
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", source],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


def test_selection_state_round_trips_and_clears_only_ids() -> None:
    result = _run(
        """
const values = new Map();
const storage = {
  getItem: (key) => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, value),
  removeItem: (key) => values.delete(key),
};
const runId = "11111111-2222-4333-8444-555555555555";
const segmentId = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const wrote = writeWorkbenchSelection(storage, { runId, segmentId });
const raw = JSON.parse([...values.values()][0]);
const restored = readWorkbenchSelection(storage);
const cleared = clearWorkbenchSelection(storage);
const afterClear = readWorkbenchSelection(storage);
process.stdout.write(JSON.stringify({ wrote, raw, restored, cleared, afterClear }));
"""
    )

    assert result["wrote"] is True
    assert result["raw"] == {
        "schema_version": 1,
        "run_id": "11111111-2222-4333-8444-555555555555",
        "segment_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    }
    assert result["restored"] == {
        "runId": "11111111-2222-4333-8444-555555555555",
        "segmentId": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    }
    assert result["cleared"] is True
    assert result["afterClear"] == {"runId": None, "segmentId": None}


def test_selection_state_ignores_corruption_and_storage_failures() -> None:
    result = _run(
        """
const corrupt = { getItem: () => "{bad json" };
const invalid = { getItem: () => JSON.stringify({
  schema_version: 1,
  run_id: "not-a-uuid",
  segment_id: "also-invalid",
}) };
const blocked = {
  getItem: () => { throw new Error("blocked"); },
  setItem: () => { throw new Error("blocked"); },
  removeItem: () => { throw new Error("blocked"); },
};
process.stdout.write(JSON.stringify({
  corrupt: readWorkbenchSelection(corrupt),
  invalid: readWorkbenchSelection(invalid),
  blockedRead: readWorkbenchSelection(blocked),
  blockedWrite: writeWorkbenchSelection(blocked, {
    runId: "11111111-2222-4333-8444-555555555555",
    segmentId: null,
  }),
  blockedClear: clearWorkbenchSelection(blocked),
}));
"""
    )

    empty = {"runId": None, "segmentId": None}
    assert result == {
        "corrupt": empty,
        "invalid": empty,
        "blockedRead": empty,
        "blockedWrite": False,
        "blockedClear": False,
    }


def test_initial_run_prefers_saved_then_active_then_newest() -> None:
    result = _run(
        """
const chapters = [
  { run_id: "11111111-2222-4333-8444-555555555555", status: "succeeded" },
  { run_id: "22222222-3333-4444-8555-666666666666", status: "running" },
  { run_id: "33333333-4444-4555-8666-777777777777", status: "failed" },
];
process.stdout.write(JSON.stringify({
  saved: chooseInitialRunId(chapters, chapters[2].run_id),
  missing: chooseInitialRunId(chapters, "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
  newest: chooseInitialRunId(chapters.filter((item) => item.status !== "running"), null),
  empty: chooseInitialRunId([], null),
}));
"""
    )

    assert result == {
        "saved": "33333333-4444-4555-8666-777777777777",
        "missing": "22222222-3333-4444-8555-666666666666",
        "newest": "11111111-2222-4333-8444-555555555555",
        "empty": None,
    }
