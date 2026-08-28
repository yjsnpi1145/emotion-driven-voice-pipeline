# Contextual Emotion Direction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dedicated context-aware LLM emotion-direction pass to Director translation.

**Architecture:** Preserve the current translation contract, then build contextual emotion inputs
from the full reviewed timeline and replace provisional translation vectors before publishing.
Normalize semantically empty uniform vectors locally and expose the new operation in LLM activity.

**Tech Stack:** Python 3.11, Pydantic v2, FastAPI, OpenAI-compatible chat completions, pytest.

## Global Constraints

- Emotion order and total limits remain unchanged.
- Context includes speaker, source excerpt, and three timeline neighbors on each side.
- Translation text and reference text must not be modified by the emotion pass.
- Existing OpenAI-compatible endpoints remain supported.
- Every production change follows RED, GREEN, regression verification, then commit.

---

### Task 1: Context models and vector normalization

**Files:**
- Modify: `src/voice_pipeline/models/director_llm.py`
- Create: `src/voice_pipeline/core/contextual_emotion.py`
- Create: `tests/unit/test_contextual_emotion.py`

**Interfaces:**
- Produces `EmotionContextUnit`, `EmotionDirectionInput`, `EmotionDirectionResultItem`,
  `EmotionDirectionResult`, `build_emotion_inputs(...)`, and `normalize_directed_vector(...)`.
- Consumes reviewed Director utterances and canonical role names.

- [ ] Write tests proving context-window construction and uniform-vector normalization.
- [ ] Run the tests and observe missing-symbol failures.
- [ ] Implement immutable models and pure context construction.
- [ ] Run unit tests and commit.

### Task 2: OpenAI-compatible emotion-direction request

**Files:**
- Modify: `src/voice_pipeline/modules/llm/client.py`
- Modify: `src/voice_pipeline/modules/llm/activity.py`
- Modify: `tests/unit/test_llm_client.py`

**Interfaces:**
- Produces `OpenAiDirectorClient.direct_emotions(*, utterances) -> EmotionDirectionResult`.
- Consumes the Task 1 models and existing `_schema_messages`/`_post_json` pipeline.

- [ ] Write a failing request-contract test inspecting the exact contextual payload and prompt.
- [ ] Add `emotion_direction` activity support and the client method.
- [ ] Verify schema, activity and retry regressions; commit.

### Task 3: Translation orchestration integration

**Files:**
- Modify: `src/voice_pipeline/core/director_analysis.py`
- Modify: fake Director implementations discovered under `src` and `tests`.
- Modify: `tests/integration_cpu/test_director_end_to_end.py`
- Modify: `tests/contract/test_director_api.py`

**Interfaces:**
- Consumes `build_emotion_inputs`, `direct_emotions`, and provisional translation results.
- Produces published translation items whose vectors come only from contextual direction.

- [ ] Write failing integration tests for role/context propagation, replacement, normalization and
  identity mismatch rejection.
- [ ] Run RED.
- [ ] Direct all translated spoken utterances after translation batches finish, validate a complete
  one-to-one result, normalize vectors, and publish copied translation items.
- [ ] Run Director integration/contract regressions and commit.

### Task 4: Full verification and deployment

**Files:**
- Modify only focused regression files if a new defect is first reproduced by a failing test.

- [ ] Run Ruff and all contextual-emotion/LLM/Director focused tests.
- [ ] Run the complete non-GPU, non-quality-model suite.
- [ ] Merge into `main`, build/install the wheel, restart the supported service, and confirm health.
- [ ] Run a real OpenAI-compatible Director translation smoke case and inspect that the resulting
  vectors are non-uniform and contextually plausible before reporting completion.

## Self-review

The plan covers data models, bounded full-timeline context, client transport, activity visibility,
orchestration replacement, invalid-vector fallback, identity validation, regression, deployment and
real-service evidence. It contains no placeholders and uses consistent interface names.
