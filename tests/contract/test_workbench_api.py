from __future__ import annotations

import re

import httpx
import pytest

from voice_pipeline.api.app import create_app


@pytest.mark.asyncio
async def test_workbench_serves_local_static_shell_and_public_chapter_listing(
    fake_settings,
) -> None:
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            page = await client.get("/")
            script = await client.get("/ui/app.js")
            stylesheet = await client.get("/ui/styles.css")
            listing = await client.get("/api/v1/chapters")
            traversal = await client.get("/ui/../api/app.py")

    assert page.status_code == 200
    assert 'id="segment-list"' in page.text
    assert 'id="segment-editor"' in page.text
    assert 'id="chapter-form"' in page.text
    assert 'id="chapter-form" class="stack-form" novalidate' in page.text
    assert 'id="chapter-summary"' in page.text
    assert 'id="chapter-audio"' in page.text
    assert 'id="segment-state-filter"' in page.text
    assert 'id="model-profile-form"' in page.text
    assert 'id="model-profile-list"' in page.text
    assert 'data-view="workbench"' in page.text
    assert 'data-view="models"' in page.text
    assert 'data-view="llm"' in page.text
    assert 'data-view="system"' in page.text
    assert 'id="llm-settings-form"' in page.text
    assert 'id="system-health-grid"' in page.text
    assert 'id="open-model-library"' in page.text
    assert 'id="pick-gpt-weight"' in page.text
    assert 'id="pick-sovits-weight"' in page.text
    assert 'id="pick-base-voice"' in page.text
    assert "用于 IndexTTS2 音色克隆的 WAV（可长音频）" in page.text
    assert "3–10 秒的参考音色" not in page.text
    assert "原文可使用中文、日语、英语、韩语或其他语言" in page.text
    assert "与原文不同时自动翻译" in page.text
    assert "IndexTTS2 始终使用中文情绪参考文本" in page.text
    assert 'id="toast-region"' in page.text
    assert script.status_code == 200
    assert stylesheet.status_code == 200
    assert re.search(r"\.workbench\s*\{[^}]*grid-template-columns", stylesheet.text)
    assert "color-scheme: light" in stylesheet.text
    assert ".primary-tabs" in stylesheet.text
    assert ".health-grid" in stylesheet.text
    assert "/api/v1/chapters" in script.text
    assert "/progress" in script.text
    assert "/events" in script.text
    assert "/regenerate-reference" in script.text
    assert "/regenerate-gsv" in script.text
    assert "/regenerate-both" in script.text
    assert "/history" in script.text
    assert "/compose" in script.text
    assert "/api/v1/model-profiles/import" in script.text
    assert "/model-profiles/${profileId}/activate" in script.text
    assert "renderVirtualRows" in script.text
    assert "normalizeEmotionVector" in script.text
    assert "renderChapterSummary" in script.text
    assert "/api/v1/settings/llm" in script.text
    assert "/api/v1/settings/llm/test" in script.text
    assert "/api/v1/local/open-folder" in script.text
    assert "/api/v1/local/pick-file" in script.text
    assert "/open-folder`" in script.text
    assert "renderSystemHealth" in script.text
    assert "activateView" in script.text
    assert "formatApiError" in script.text
    assert "schema_errors" in script.text
    assert "目标语言合成文本" in script.text
    assert 'method: "DELETE"' in script.text
    assert "从章节历史中删除" in script.text
    assert "chapter-delete" in script.text
    assert "chapter-delete" in stylesheet.text
    assert 'preload="metadata"' in script.text
    assert 'preload="none"' not in script.text
    assert 'player.preload = "metadata"' in script.text
    assert "用于 IndexTTS2 音色克隆的参考 WAV" in script.text
    assert "3–10 秒的参考音色" not in script.text
    assert 'withBusy(submit, "正在规划分块…"' in script.text
    assert "report(sanitizeMessage(error), true)" in script.text
    assert "const formElement = event.currentTarget;" in script.text
    assert 'if (kind !== "reference") body.model_profile_id = profile;' in script.text
    assert 'id="normalize-vector"' in script.text
    assert 'item.status === "ready"' in script.text
    assert (
        'const vectorNames = ["愉悦", "愤怒", "悲伤", "恐惧", "厌恶", "忧郁", "惊讶", "平静"]'
        in script.text
    )
    assert "19871" not in script.text
    assert "Bearer sk-" not in script.text
    assert "cdn" not in page.text.casefold()
    assert listing.status_code == 200
    assert listing.json() == []
    assert traversal.status_code == 404
