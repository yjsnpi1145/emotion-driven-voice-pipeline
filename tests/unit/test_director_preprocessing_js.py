from __future__ import annotations

import json
import subprocess
from pathlib import Path

MODULE = (
    Path(__file__).parents[2]
    / "src"
    / "voice_pipeline"
    / "webui"
    / "director-preprocessing.js"
)


def test_director_preprocessing_helpers_track_drafts_confirmation_and_pages() -> None:
    script = f"""
import {{
  preprocessDraftState,
  canConfirmPreprocessing,
  nextPreprocessOffset,
  preprocessStatusLabel,
}} from {json.dumps(MODULE.as_uri())};
const paragraph = {{
  paragraph_id:'p1',
  preprocessed_text:'清洗稿',
  structural_text:'清洗稿',
}};
const payload = {{
  dirty: preprocessDraftState(paragraph, '用户稿'),
  clean: preprocessDraftState(paragraph, '清洗稿'),
  confirmClean: canConfirmPreprocessing(
    {{status:'preprocess_review'}}, new Map()
  ),
  confirmDirty: canConfirmPreprocessing(
    {{status:'preprocess_review'}}, new Map([['p1', '未保存']])
  ),
  confirmWrongStage: canConfirmPreprocessing(
    {{status:'preprocessing'}}, new Map()
  ),
  next: nextPreprocessOffset({{next_offset:20}}),
  done: nextPreprocessOffset({{next_offset:null}}),
  fallback: preprocessStatusLabel('fallback'),
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
    payload = json.loads(result.stdout)

    assert payload["dirty"] == {"dirty": True, "blank": False, "canSave": True}
    assert payload["clean"] == {"dirty": False, "blank": False, "canSave": False}
    assert payload["confirmClean"] is True
    assert payload["confirmDirty"] is False
    assert payload["confirmWrongStage"] is False
    assert payload["next"] == 20
    assert payload["done"] is None
    assert payload["fallback"] == "已回退到本地清洗稿"
