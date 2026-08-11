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
            shutdown_script = await client.get("/ui/service-shutdown.js")
            stage_script = await client.get("/ui/stage-progress.js")
            selection_script = await client.get("/ui/selection-state.js")
            stylesheet = await client.get("/ui/styles.css")
            listing = await client.get("/api/v1/chapters")
            llm_activity = await client.get("/api/v1/llm/activity")
            traversal = await client.get("/ui/../api/app.py")

    assert page.status_code == 200
    assert 'data-theme="dark-console"' in page.text
    assert 'class="brand-mark"' not in page.text
    assert ">声</div>" not in page.text
    assert "20260811b" in page.text
    assert 'id="segment-list"' in page.text
    assert 'id="segment-editor"' in page.text
    assert 'id="chapter-form"' in page.text
    assert 'id="chapter-form" class="stack-form" novalidate' in page.text
    assert 'id="chapter-summary"' in page.text
    assert 'id="chapter-progress"' in page.text
    assert 'id="llm-activity-console"' in page.text
    assert 'id="llm-activity-status"' in page.text
    assert 'id="llm-activity-log"' in page.text
    assert page.text.index('id="llm-activity-console"') < page.text.index('id="chapter-progress"')
    assert page.text.index('id="chapter-progress"') < page.text.index("<h3>章节历史</h3>")
    assert 'id="chapter-audio"' in page.text
    assert 'id="export-gsv-archive"' in page.text
    assert "导出全部分块 GSV" in page.text
    assert 'id="resume-chapter"' in page.text
    assert "修复后继续章节" in page.text
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
    assert 'id="shutdown-services"' in page.text
    assert 'id="shutdown-overlay"' in page.text
    assert "关闭所有服务" in page.text
    assert "所有服务已关闭，可以关闭此页面" in page.text
    assert script.status_code == 200
    assert shutdown_script.status_code == 200
    assert stage_script.status_code == 200
    assert selection_script.status_code == 200
    assert stylesheet.status_code == 200
    assert re.search(r"\.workbench\s*\{[^}]*grid-template-columns", stylesheet.text)
    assert "color-scheme: dark" in stylesheet.text
    assert "--bg: #080d15" in stylesheet.text
    assert ".brand-mark" not in stylesheet.text
    assert "background: #fff;" not in stylesheet.text
    assert "background: rgb(255 255 255" not in stylesheet.text
    assert "scrollbar-width: none" in stylesheet.text
    assert ".primary-tabs::-webkit-scrollbar" in stylesheet.text
    assert ".primary-tabs" in stylesheet.text
    assert ".health-grid" in stylesheet.text
    assert "/api/v1/chapters" in script.text
    assert "/progress" in script.text
    assert "/events" in script.text
    assert "/regenerate-reference" in script.text
    assert "/regenerate-gsv" in script.text
    assert "/regenerate-both" in script.text
    assert "留空则复用章节总参考音色" in script.text
    assert "请先填写重新生成参考所用音色路径" not in script.text
    assert "/history" in script.text
    assert "/compose" in script.text
    assert "/resume`" in script.text
    assert "/export/gsv" in script.text
    assert "active_gsv_version_id" in script.text
    assert "exportChapterGsvArchive" in script.text
    assert "resumeChapter" in script.text
    assert 'state.run.status === "failed"' in script.text
    assert 'state.run.status === "interrupted"' in script.text
    assert "/api/v1/model-profiles/import" in script.text
    assert "/model-profiles/${profileId}/activate" in script.text
    assert "renderVirtualRows" in script.text
    assert "normalizeEmotionVector" in script.text
    assert "renderChapterSummary" in script.text
    assert 'from "./stage-progress.js"' in script.text
    assert 'from "./selection-state.js"' in script.text
    assert "readWorkbenchSelection(window.localStorage)" in script.text
    assert "chooseInitialRunId(state.chapters, savedSelection.runId)" in script.text
    assert "preferredSegmentId = null" in script.text
    assert "writeWorkbenchSelection(window.localStorage" in script.text
    assert "clearWorkbenchSelection(window.localStorage)" in script.text
    assert "renderChapterProgress" in script.text
    assert "creationProgress" in script.text
    select_run_source = script.text.split(
        "async function selectRun(runId, { preferredSegmentId = null } = {})", 1
    )[1].split("async function refreshRun()", 1)[0]
    assert "state.creationProgress = null" in select_run_source
    assert 'setAttribute("role", "progressbar")' in script.text
    assert "aria-valuenow" in script.text
    assert "文本规划" in stage_script.text
    assert "参考音频" in stage_script.text
    assert "GSV 合成" in stage_script.text
    assert "整篇拼接" in stage_script.text
    assert ".chapter-progress" in stylesheet.text
    assert ".llm-activity-console" in stylesheet.text
    assert ".llm-activity-log" in stylesheet.text
    assert re.search(r"\.llm-activity-log\s*\{[^}]*overflow-y:\s*auto", stylesheet.text)
    assert ".stage-progress-track" in stylesheet.text
    assert '[data-state="active"]' in stylesheet.text
    assert '[data-state="complete"]' in stylesheet.text
    assert '[data-state="failed"]' in stylesheet.text
    assert "/api/v1/settings/llm" in script.text
    assert "/api/v1/settings/llm/test" in script.text
    assert "/api/v1/llm/activity" in script.text
    assert "renderLlmActivity" in script.text
    assert "750" in script.text
    assert ".textContent = event.content" in script.text
    assert "/api/v1/local/open-folder" in script.text
    assert "/api/v1/local/pick-file" in script.text
    assert "/open-folder`" in script.text
    assert "renderSystemHealth" in script.text
    assert "activateView" in script.text
    assert "formatApiError" in script.text
    assert 'from "./service-shutdown.js"' in script.text
    assert "/api/v1/control/shutdown" in shutdown_script.text
    assert "confirmAndShutdown" in script.text
    assert "enterShutdownState" in script.text
    assert "state.events?.close()" in script.text
    assert "window.clearInterval(state.refreshTimer)" in script.text
    assert "window.clearInterval(state.llmActivityTimer)" in script.text
    assert 'document.querySelectorAll("button, input, textarea, select")' in script.text
    assert "schema_errors" in script.text
    assert "目标语言合成文本" in script.text
    assert 'method: "DELETE"' in script.text
    assert "从章节历史中删除" in script.text
    assert "chapter-delete" in script.text
    assert "chapter-delete" in stylesheet.text
    assert ".danger-button" in stylesheet.text
    assert ".shutdown-overlay" in stylesheet.text
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
    assert llm_activity.status_code == 200
    assert llm_activity.json() == {
        "active": False,
        "active_operation": None,
        "active_since_utc": None,
        "events": [],
    }
    assert traversal.status_code == 404


@pytest.mark.asyncio
async def test_llm_activity_endpoint_exposes_recent_director_lifecycle(fake_settings) -> None:
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        await app.state.plane.llm_client.create_plan(
            source_text="第一句。第二句。",
            target_language="zh",
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/llm/activity")

    assert response.status_code == 200
    payload = response.json()
    assert payload["active"] is False
    assert [event["kind"] for event in payload["events"]] == ["started", "completed"]
    assert payload["events"][0]["operation"] == "chapter_plan"
    assert "segments" in payload["events"][-1]["content"]
