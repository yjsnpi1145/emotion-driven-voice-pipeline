from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from voice_pipeline.core.config import LlmSettings
from voice_pipeline.models.runtime_settings import LlmSettingsUpdate
from voice_pipeline.modules.llm.runtime import RuntimeDirector


@pytest.mark.asyncio
async def test_runtime_director_persists_settings_without_returning_secret(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    director = RuntimeDirector(LlmSettings(), state_dir=state_dir)
    await director.start()

    view = await director.update(
        LlmSettingsUpdate(
            mode="openai",
            base_url="https://llm.example/v1",
            model="director-v2",
            api_key="local-secret",
            timeout_seconds=42,
            max_retries=3,
            max_reference_corrections=1,
        )
    )

    assert view.mode == "openai"
    assert view.api_key_configured is True
    assert "api_key" not in view.model_dump()
    persisted = json.loads((state_dir / "llm-settings.json").read_text(encoding="utf-8"))
    assert persisted["model"] == "director-v2"
    assert "local-secret" not in json.dumps(persisted)
    assert (state_dir / "llm-secret.txt").read_text(encoding="utf-8") == "local-secret"

    restored = RuntimeDirector(LlmSettings(), state_dir=state_dir)
    await restored.start()
    restored_view = restored.view()
    assert restored_view.model == "director-v2"
    assert restored_view.api_key_configured is True
    assert restored.max_reference_corrections == 1

    await director.aclose()
    await restored.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_connection_uses_candidate_without_saving_it(tmp_path: Path) -> None:
    route = respx.post("https://candidate.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok": true}'}}]},
        )
    )
    director = RuntimeDirector(LlmSettings(), state_dir=tmp_path / "state")
    await director.start()

    result = await director.test_connection(
        LlmSettingsUpdate(
            mode="openai",
            base_url="https://candidate.example/v1",
            model="candidate-model",
            api_key="candidate-secret",
            timeout_seconds=10,
            max_retries=0,
            max_reference_corrections=2,
        )
    )

    assert route.called
    assert route.calls[0].request.headers["authorization"] == "Bearer candidate-secret"
    assert result.ok is True
    assert result.model == "candidate-model"
    assert director.view().mode == "fake"
    assert not (tmp_path / "state" / "llm-settings.json").exists()
    await director.aclose()


@pytest.mark.asyncio
async def test_clear_api_key_removes_persisted_secret(tmp_path: Path) -> None:
    director = RuntimeDirector(LlmSettings(), state_dir=tmp_path / "state")
    await director.start()
    await director.update(
        LlmSettingsUpdate(
            mode="openai",
            base_url="http://127.0.0.1:11434/v1",
            model="local-model",
            api_key="temporary",
            max_reference_corrections=2,
        )
    )

    view = await director.update(
        LlmSettingsUpdate(
            mode="fake",
            base_url="http://127.0.0.1:11434/v1",
            model="fake-director",
            clear_api_key=True,
            max_reference_corrections=4,
        )
    )

    assert view.api_key_configured is False
    assert director.max_reference_corrections == 4
    assert not (tmp_path / "state" / "llm-secret.txt").exists()
    await director.aclose()
