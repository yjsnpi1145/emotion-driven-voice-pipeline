from __future__ import annotations

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
            listing = await client.get("/api/v1/chapters")
            traversal = await client.get("/ui/../api/app.py")

    assert page.status_code == 200
    assert 'id="segment-list"' in page.text
    assert 'id="segment-editor"' in page.text
    assert 'id="chapter-form"' in page.text
    assert script.status_code == 200
    assert "/api/v1/chapters" in script.text
    assert "/progress" in script.text
    assert "/events" in script.text
    assert "/regenerate-reference" in script.text
    assert "/regenerate-gsv" in script.text
    assert "/regenerate-both" in script.text
    assert "/history" in script.text
    assert "/compose" in script.text
    assert (
        'const vectorNames = ["愉悦", "愤怒", "悲伤", "恐惧", "厌恶", "忧郁", "惊讶", "平静"]'
        in script.text
    )
    assert "19871" not in script.text
    assert "api_key" not in script.text
    assert "cdn" not in page.text.casefold()
    assert listing.status_code == 200
    assert listing.json() == []
    assert traversal.status_code == 404
