# Director Reference Expansion and Sentence Export Design

## Goal

Prevent short Director-mode dialogue from failing IndexTTS reference validation without changing the reviewed dialogue or performance controls, allow whole roles to be explicitly skipped, and add a sentence-by-sentence ZIP export while retaining the mixed WAV export.

## Locked Product Decisions

- The reviewed Chinese source slice, target-language synthesis text, role assignment, emotion vector, speed, pause, and seed are immutable during reference preparation.
- Only `ref_text_cn` may be expanded or shortened by the LLM.
- Generated IndexTTS reference audio must remain in the closed interval `3.0..10.0` seconds.
- The system may make at most two LLM reference corrections after the initial reference text.
- A role mapping has three distinct states: unconfigured, mapped to a preset, or explicitly skipped.
- An explicitly skipped role creates no segment, IndexTTS job, GPT-SoVITS job, mixed-audio entry, or ZIP entry.
- Existing mixed WAV export remains available.
- Sentence export is a ZIP ordered by source order. Each filename is `NNNN_<Chinese source sentence>.wav`, with Windows-invalid characters removed, whitespace collapsed, trailing dots/spaces removed, and overlong names truncated.

## Architecture

### Reference preparation

Director translation continues to produce the initial `ref_text_cn`, but its prompt must explicitly expand very short dialogue into a natural Chinese performance reference while preserving the reviewed emotion. Director generation then uses the existing `ReferenceTextDirector` duration-feedback loop before submitting the durable reference job. The loop synthesizes a probe using the role preset base voice, measures it, and calls the runtime LLM corrector with `lengthen` or `shorten` until the duration is valid or the two-correction budget is exhausted.

The resolved text is persisted through `SegmentStore.patch_inputs` before the durable reference job is submitted. This makes resume deterministic: a failed or interrupted generation continues from the latest persisted reference text, while the source and target dialogue remain unchanged.

The Director generation service receives the same reference-preparation dependencies already used by `ChapterService`: the runtime LLM director, synthesis service, serial GPU queue, jobs root, and configured correction budget. LLM correction activity therefore appears in the existing Director LLM activity feed.

### Explicit role skipping

Add `dubbing_enabled` to Director roles, defaulting to `true`. A role with `dubbing_enabled=true` and no preset remains unconfigured and blocks generation. Selecting a preset sets `dubbing_enabled=true`; selecting “不予映射（跳过配音）” sets `dubbing_enabled=false` and clears the preset.

Generation filters out utterances whose assigned role has `dubbing_enabled=false` before snapshotting and materializing segments. If no mapped spoken utterances remain, generation returns an explicit review error. Existing projects migrate with `dubbing_enabled=true`, preserving their prior behavior.

### Sentence ZIP export

After all mapped utterances have ready GSV versions, generation writes `sentences.zip` next to the generated `final.wav`. ZIP entries use the immutable Chinese `source_text` and the generation ordinal. Each standalone ZIP WAV is rendered from the ready GSV blob with that utterance's configured `pause_after_ms` appended as trailing silence, so playback does not stop abruptly at the last voiced frame. The immutable ready GSV blob is never modified. Skipped roles remain excluded by construction.

The generation API adds a sentence-archive download endpoint with path-containment and symlink checks equivalent to the mixed-WAV endpoint. The WebUI shows both download controls after successful generation.

## Data Model and Compatibility

- Migration `0007_director_role_dubbing` adds non-null `director_roles.dubbing_enabled` with default `1`.
- `DirectorRoleRecord` exposes `dubbing_enabled: bool`.
- `BindRolePresetRequest` accepts a discriminated mapping mode (`preset` or `skip`) and validates the presence/absence of `preset_id`.
- The existing preset mapping payload remains accepted as the default `preset` mode.
- No existing project or audio artifact is deleted.
- Existing failed Director generations can be resumed and use the new reference correction path.

## Failure Semantics

- Invalid or unavailable role presets fail before generation starts.
- An unconfigured spoken role still blocks generation; an explicitly skipped role does not.
- LLM correction failure or exhausted duration correction fails only that utterance and preserves successful utterances.
- ZIP construction failure fails composition rather than publishing a generation with a missing advertised archive.
- Filename sanitization always produces a non-empty name; empty results fall back to `句子`.

## Verification

- Unit tests prove reference correction changes only `ref_text_cn` and preserves source text, synthesis text, emotion, speed, pause, and seed.
- Integration tests prove short dialogue is expanded before the durable reference job and that resumed generations reuse persisted corrections.
- Store/API/WebUI tests cover all three role-mapping states and prove skipped roles create no work.
- ZIP tests cover ordering, duplicate dialogue, invalid characters, truncation, UTF-8 Chinese names, and exclusion of skipped roles.
- Existing mixed-WAV and Director end-to-end tests continue to pass.
- Deployment acceptance performs a real stop/start cycle, health check, WebUI load, and download-route smoke test.
