from __future__ import annotations

import json
from uuid import uuid4

from voice_pipeline.api.workbench_routes import _sse, progress_rows


def test_progress_sse_and_rows_only_accept_public_path_free_fields() -> None:
    segment_id = uuid4()
    payload = {
        "run_id": str(uuid4()),
        "task_id": str(uuid4()),
        "status": "running",
        "segments": [
            {
                "ordinal": 0,
                "segment_id": str(segment_id),
                "source_summary": "第一句。",
                "reference_job_status": "succeeded",
                "gsv_job_status": "running",
                "active_ref_version_id": None,
                "active_gsv_version_id": None,
            }
        ],
    }

    event = _sse("chapter_progress", payload)
    rows = progress_rows(payload)

    assert event.startswith("event: chapter_progress\ndata: ")
    assert json.loads(event.split("data: ", 1)[1]) == payload
    assert rows[0].segment_id == segment_id
    assert "path" not in event.casefold()
