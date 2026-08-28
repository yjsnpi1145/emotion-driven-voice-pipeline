from __future__ import annotations

import json
import subprocess
from pathlib import Path

MODULE = (
    Path(__file__).parents[2]
    / "src"
    / "voice_pipeline"
    / "webui"
    / "director-adjustment.js"
)


def test_director_adjustment_helpers_build_safe_payloads_and_availability() -> None:
    script = f"""
import * as adjustment from {json.dumps(MODULE.as_uri())};
const utterance = {{
  utterance_id: 'u1', revision: 4,
  synthesis_text: 'Hello', ref_text_cn: '你好。',
  speed_factor: 1, pause_after_ms: 200,
  emotion_vector: [0,0,0.1,0,0,0,0,0.2],
  reference_version_id: 'ref-1', gsv_version_id: 'gsv-1',
}};
const original = adjustment.createAdjustmentDraft(utterance);
const edited = {{...original, speed_factor: '1.15', pause_after_ms: '450'}};
const refEdited = {{...edited, ref_text_cn: '新的参考。'}};
const payload = adjustment.buildAdjustmentPayload(utterance, edited, 'gsv', 9);
const generated = adjustment.deriveAdjustmentAvailability(
  {{status:'succeeded', current_generation_id:'gen-1'}},
  {{status:'ready'}},
  adjustment.changedAdjustmentFields(utterance, edited),
  true,
);
const escalation = adjustment.deriveAdjustmentAvailability(
  {{status:'succeeded', current_generation_id:'gen-1'}},
  {{status:'ready'}},
  adjustment.changedAdjustmentFields(utterance, refEdited),
  true,
);
const review = adjustment.deriveAdjustmentAvailability(
  {{status:'translation_review', current_generation_id:null}}, null, [], false,
);
const running = adjustment.deriveAdjustmentAvailability(
  {{status:'generating', current_generation_id:'gen-1'}},
  {{status:'gsv_running'}}, [], true,
);
const refreshed = adjustment.preserveAdjustmentDraft(
  original,
  adjustment.createAdjustmentDraft({{...utterance, speed_factor: 1.4}}),
  false,
);
const preserved = adjustment.preserveAdjustmentDraft(
  edited,
  adjustment.createAdjustmentDraft({{...utterance, speed_factor: 1.4}}),
  true,
);
console.log(JSON.stringify({{
  fields: adjustment.changedAdjustmentFields(utterance, edited),
  payload,
  vectorTotal: adjustment.emotionVectorTotal(original.emotion_vector),
  validVector: adjustment.isEmotionVectorValid(original.emotion_vector),
  invalidVector: adjustment.isEmotionVectorValid([.2,.2,.2,.2,.01,0,0,0]),
  generated, escalation, review, running, refreshed, preserved,
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
    assert payload["fields"] == ["speed_factor", "pause_after_ms"]
    assert payload["payload"] == {
        "expected_project_revision": 9,
        "expected_utterance_revision": 4,
        "synthesis_text": "Hello",
        "ref_text_cn": "你好。",
        "speed_factor": 1.15,
        "pause_after_ms": 450,
        "emotion_vector": [0, 0, 0.1, 0, 0, 0, 0, 0.2],
        "action": "gsv",
    }
    assert payload["vectorTotal"] == 0.3
    assert payload["validVector"] is True
    assert payload["invalidVector"] is False
    assert payload["generated"]["save"] is True
    assert payload["generated"]["gsv"] is True
    assert payload["generated"]["recompose"] is True
    assert payload["escalation"]["gsvEscalatesToBoth"] is True
    assert payload["review"] == {
        "save": True,
        "reference": False,
        "gsv": False,
        "both": False,
        "recompose": False,
        "gsvEscalatesToBoth": True,
    }
    assert payload["running"]["save"] is False
    assert payload["running"]["both"] is False
    assert payload["refreshed"]["speed_factor"] == "1.4"
    assert payload["preserved"]["speed_factor"] == "1.15"


def test_director_adjustment_helpers_normalize_linked_controls() -> None:
    script = f"""
import * as adjustment from {json.dumps(MODULE.as_uri())};
console.log(JSON.stringify({{
  lowSpeed: adjustment.normalizeAdjustmentNumber('speed_factor', '0.1'),
  highSpeed: adjustment.normalizeAdjustmentNumber('speed_factor', '2.7'),
  pause: adjustment.normalizeAdjustmentNumber('pause_after_ms', '123.6'),
  emotion: adjustment.normalizeAdjustmentNumber('emotion', '1.7'),
  nonNumber: adjustment.normalizeAdjustmentNumber('emotion', 'x'),
}}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert json.loads(result.stdout) == {
        "lowSpeed": 0.5,
        "highSpeed": 2,
        "pause": 124,
        "emotion": 1,
        "nonNumber": 0,
    }
