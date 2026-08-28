from __future__ import annotations

import json
import subprocess
from pathlib import Path

MODULE = Path(__file__).parents[2] / "src" / "voice_pipeline" / "webui" / "director-llm-activity.js"


def test_director_activity_view_filters_events_and_tracks_concurrent_operations() -> None:
    script = f"""
import {{ directorActivityView }} from {json.dumps(MODULE.as_uri())};
const event = (sequence, operationId, operation, kind, message, content, createdAt) => ({{
  sequence,
  operation_id: operationId,
  operation,
  kind,
  message,
  content,
  created_at_utc: createdAt,
}});
const events = [
  event(1, 'chapter', 'chapter_plan', 'completed', 'chapter', null, '2026-08-27T10:00:00Z'),
  event(
    2, 'preprocess', 'script_preprocessing', 'completed', 'preprocess done', null,
    '2026-08-27T09:59:00Z',
  ),
  event(
    3, 'analysis', 'script_analysis', 'started', 'analysis start', null,
    '2026-08-27T10:00:00Z',
  ),
  event(
    4, 'analysis', 'script_analysis', 'response', 'analysis response',
    '{{"roles":[]}}', '2026-08-27T10:01:00Z',
  ),
  event(
    5, 'translation', 'script_translation', 'started', 'translation start', null,
    '2026-08-27T10:02:00Z',
  ),
  event(
    6, 'translation', 'script_translation', 'completed', 'translation done',
    '{{"items":[]}}', '2026-08-27T10:03:00Z',
  ),
];
const active = directorActivityView(
  {{active:true, active_operation:'chapter_plan', active_since_utc:'2026-08-27T10:00:00Z', events}},
  false,
  Date.parse('2026-08-27T10:04:00Z'),
);
const failed = directorActivityView({{
  active:false,
  active_operation:null,
  active_since_utc:null,
  events:[
    event(6, 'cast', 'cast_reconciliation', 'started', 'cast start', null, '2026-08-27T10:05:00Z'),
    event(7, 'cast', 'cast_reconciliation', 'failed', 'cast failed', null, '2026-08-27T10:05:05Z'),
  ],
}}, false, Date.parse('2026-08-27T10:06:00Z'));
console.log(JSON.stringify({{active, failed}}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(result.stdout)

    active = payload["active"]
    assert [event["operation"] for event in active["events"]] == [
        "script_preprocessing",
        "script_analysis",
        "script_analysis",
        "script_translation",
        "script_translation",
    ]
    assert active["active"] is True
    assert active["activeSinceUtc"] == "2026-08-27T10:00:00Z"
    assert active["statusState"] == "active"
    assert active["statusText"] == "正在工作 · 240s"

    failed = payload["failed"]
    assert failed["active"] is False
    assert failed["statusState"] == "degraded"
    assert failed["statusText"] == "失败"


def test_director_preprocessing_operation_has_user_facing_label() -> None:
    script = f"""
import {{ directorOperationLabels }} from {json.dumps(MODULE.as_uri())};
console.log(JSON.stringify(directorOperationLabels.script_preprocessing));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert json.loads(result.stdout) == "文本预处理"


def test_director_emotion_direction_is_visible_with_user_facing_label() -> None:
    script = f"""
import {{ directorActivityView, directorOperationLabels }} from {json.dumps(MODULE.as_uri())};
const event = {{
  sequence:1,
  operation_id:'emotion',
  operation:'emotion_direction',
  kind:'response',
  message:'response',
  content:'{{"items":[]}}',
  created_at_utc:'2026-08-27T10:00:00Z',
}};
const view = directorActivityView({{active:true, events:[event]}});
const payload = {{events:view.events, label:directorOperationLabels.emotion_direction}};
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
    assert [item["operation"] for item in payload["events"]] == ["emotion_direction"]
    assert payload["label"] == "上下文情绪"


def test_director_activity_view_preserves_events_when_endpoint_is_unavailable() -> None:
    script = f"""
import {{ directorActivityView }} from {json.dumps(MODULE.as_uri())};
const view = directorActivityView({{
  active:false,
  active_operation:null,
  active_since_utc:null,
  events:[{{
    sequence:1,
    operation_id:'analysis',
    operation:'script_analysis',
    kind:'completed',
    message:'done',
    content:'safe',
    created_at_utc:'2026-08-27T10:00:00Z',
  }}],
}}, true, Date.parse('2026-08-27T10:01:00Z'));
console.log(JSON.stringify(view));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(result.stdout)
    assert payload["events"][0]["content"] == "safe"
    assert payload["statusState"] == "degraded"
    assert payload["statusText"] == "连接异常"
