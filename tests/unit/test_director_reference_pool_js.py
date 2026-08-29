from __future__ import annotations

from pathlib import Path


def test_director_webui_renders_pool_state_prompt_and_rebuild_action() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "voice_pipeline"
        / "webui"
        / "director.js"
    ).read_text(encoding="utf-8")

    assert "情绪池：" in source
    assert "已降级：" in source
    assert "reference_pool.prompt_text" in source
    assert "重建池参考" in source
    assert "rebuild-pooled-reference" in source
