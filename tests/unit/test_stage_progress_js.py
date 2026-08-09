from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

MODULE = (
    Path(__file__).parents[2] / "src" / "voice_pipeline" / "webui" / "stage-progress.js"
)


def _derive(
    run: dict[str, Any] | None,
    progress: dict[str, Any] | None,
    creation_state: dict[str, str] | None = None,
) -> dict[str, Any]:
    assert MODULE.is_file(), "stage-progress.js must provide the pure stage derivation"
    source = f"""
import {{ deriveChapterStageProgress }} from {json.dumps(MODULE.as_uri())};
const result = deriveChapterStageProgress(
  {json.dumps(run)},
  {json.dumps(progress)},
  {json.dumps(creation_state)}
);
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", source],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


def test_stage_progress_has_an_idle_and_local_planning_state() -> None:
    idle = _derive(None, None)
    planning = _derive(None, None, {"status": "planning"})

    assert idle["overallPercent"] is None
    assert idle["statusLabel"] == "尚未选择任务"
    assert all(stage["state"] == "pending" for stage in idle["stages"])
    assert planning["overallPercent"] == 0
    assert planning["statusLabel"] == "文本规划中"
    assert planning["activeStage"] == "planning"
    assert planning["stages"][0]["state"] == "active"
    assert planning["stages"][0]["indeterminate"] is True


def test_stage_progress_counts_reference_and_gsv_versions_independently() -> None:
    run = {"status": "running", "final_audio_url": None}
    progress = {
        "status": "running",
        "segments": [
            {
                "active_ref_version_id": "ref-1",
                "active_gsv_version_id": "gsv-1",
                "reference_job_status": "succeeded",
                "gsv_job_status": "succeeded",
            },
            {
                "active_ref_version_id": None,
                "active_gsv_version_id": None,
                "reference_job_status": "running",
                "gsv_job_status": None,
            },
        ],
    }

    result = _derive(run, progress)

    assert result["overallPercent"] == 50
    assert result["activeStage"] == "reference"
    assert result["stages"][1]["detail"] == "1/2"
    assert result["stages"][1]["ratio"] == 0.5
    assert result["stages"][1]["state"] == "active"
    assert result["stages"][2]["detail"] == "1/2"
    assert result["stages"][2]["ratio"] == 0.5
    assert result["stages"][2]["state"] == "partial"


def test_stage_progress_waits_for_composition_after_all_segments_finish() -> None:
    run = {"status": "running", "final_audio_url": None}
    progress = {
        "status": "running",
        "segments": [
            {
                "active_ref_version_id": "ref-1",
                "active_gsv_version_id": "gsv-1",
                "reference_job_status": "succeeded",
                "gsv_job_status": "succeeded",
            }
        ],
    }

    result = _derive(run, progress)

    assert result["overallPercent"] == 75
    assert result["activeStage"] == "compose"
    assert result["stages"][3]["detail"] == "拼接中"
    assert result["stages"][3]["state"] == "active"


def test_stage_progress_marks_success_and_failure_without_losing_completed_work() -> None:
    succeeded = _derive(
        {"status": "succeeded", "final_audio_url": "/audio"},
        {"status": "succeeded", "segments": []},
    )
    failed = _derive(
        {"status": "failed", "final_audio_url": None},
        {
            "status": "failed",
            "segments": [
                {
                    "active_ref_version_id": None,
                    "active_gsv_version_id": None,
                    "reference_job_status": "failed",
                    "gsv_job_status": None,
                }
            ],
        },
    )

    assert succeeded["overallPercent"] == 100
    assert succeeded["statusLabel"] == "已完成"
    assert all(stage["state"] == "complete" for stage in succeeded["stages"])
    assert failed["overallPercent"] == 25
    assert failed["statusLabel"] == "任务失败"
    assert failed["activeStage"] == "reference"
    assert failed["stages"][0]["state"] == "complete"
    assert failed["stages"][1]["state"] == "failed"
