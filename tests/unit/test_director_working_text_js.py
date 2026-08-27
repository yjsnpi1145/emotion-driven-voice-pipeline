from __future__ import annotations

import json
import subprocess
from pathlib import Path

MODULE = (
    Path(__file__).parents[2]
    / "src"
    / "voice_pipeline"
    / "webui"
    / "director-working-text.js"
)


def test_director_working_text_helpers_track_drafts_and_split_safety() -> None:
    script = f"""
import * as working from {json.dumps(MODULE.as_uri())};
const unchanged = {{source_text:'原文', working_text:'原文'}};
const edited = {{source_text:'原文', working_text:'修改'}};
const payload = {{
  dirty: working.isWorkingTextDirty(unchanged, '修改'),
  clean: working.isWorkingTextDirty(unchanged, '原文'),
  canSplit: working.canSplitWorkingText(unchanged),
  cannotSplit: working.canSplitWorkingText(edited),
  unsavedWorking: working.hasUnsavedDirectorDrafts({{
    dirtyWorkingTexts: new Map([['u1', '修改']]),
    dirtyTranslations: new Map(),
  }}),
  unsavedTranslation: working.hasUnsavedDirectorDrafts({{
    dirtyWorkingTexts: new Map(),
    dirtyTranslations: new Map([['u2', {{synthesis_text:'译文'}}]]),
  }}),
  allSaved: working.hasUnsavedDirectorDrafts({{
    dirtyWorkingTexts: new Map(),
    dirtyTranslations: new Map(),
  }}),
}};
console.log(JSON.stringify(payload));
"""

    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert json.loads(result.stdout) == {
        "dirty": True,
        "clean": False,
        "canSplit": True,
        "cannotSplit": False,
        "unsavedWorking": True,
        "unsavedTranslation": True,
        "allSaved": False,
    }
