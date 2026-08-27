from __future__ import annotations

import json
import subprocess
from pathlib import Path

MODULE = Path(__file__).parents[2] / "src" / "voice_pipeline" / "webui" / "director-dnd.js"


def test_director_drag_helpers_build_occ_patch_and_adjacent_merge() -> None:
    script = f"""
import * as d from {json.dumps(MODULE.as_uri())};
const rows = [
  {{utterance_id:'a', ordinal:0, revision:2, kind:'narration'}},
  {{utterance_id:'b', ordinal:1, revision:4, kind:'dialogue'}},
  {{utterance_id:'c', ordinal:2, revision:5, kind:'dialogue'}},
];
const patch = d.buildAssignmentPatch(rows, new Set(['b','c']), 'role-1');
const pair = d.contiguousMergePair(rows, new Set(['b','c']));
console.log(JSON.stringify({{
  patch,
  pair,
  visible:d.filterNarration(rows, false),
  editableStatuses: {{
    roleReview: d.canEditRoleReview('role_review'),
    translationReview: d.canEditRoleReview('translation_review'),
    translating: d.canEditRoleReview('translating'),
    ready: d.canEditRoleReview('ready'),
  }},
}}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(result.stdout)
    assert payload["patch"] == {
        "utterance_ids": ["b", "c"],
        "expected_revisions": {"b": 4, "c": 5},
        "role_id": "role-1",
        "role_confirmed": True,
    }
    assert payload["pair"][0]["utterance_id"] == "b"
    assert [row["utterance_id"] for row in payload["visible"]] == ["b", "c"]
    assert payload["editableStatuses"] == {
        "roleReview": True,
        "translationReview": True,
        "translating": False,
        "ready": False,
    }
