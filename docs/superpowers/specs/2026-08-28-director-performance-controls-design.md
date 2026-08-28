# Director Performance Guidance and Per-Utterance Controls

## Goal

Add an optional project-wide performance direction to Director mode and replace the expanding
inline translation editor with a compact per-utterance modal that supports editing, previewing,
saving, local regeneration, and recomposition throughout the Director workflow.

## User Experience

### Project-wide performance direction

The Director project creation form gains an optional `全局表演指导` textarea directly below the
article/script input. It accepts at most 2,000 Unicode characters. Example:

> 本篇人物整体语气偏平静，避免夸张情绪；女主在后半段逐渐表现出压抑的悲伤。

The value is stored with the project and survives refreshes and service restarts. It is a soft
performance constraint, not additional source text. It may influence only the contextual emotion
vector, speed factor, and pause after the utterance. It must never change source slices, working
text, speaker assignments, translated synthesis text, or Chinese reference text.

The field remains editable before translation. Changing it after translation requires an explicit
`重新应用到全部语句` confirmation because it makes all directed performance parameters stale and,
after generation has started, invalidates affected audio.

### Per-utterance modal

Every spoken utterance card from translation review onward has one compact `调整配音` button. The
existing expanding `编辑译文与情绪` details element is removed so cards no longer grow vertically.
The button opens one shared accessible dialog populated for the selected utterance.

The dialog contains:

- immutable original/working source context;
- editable target-language synthesis text;
- editable Chinese IndexTTS reference text;
- speed slider synchronized with a numeric input (`0.5..2.0`);
- pause input (`0..30000` milliseconds);
- eight synchronized emotion sliders and numeric inputs in the established order;
- live emotion total with the existing `<= 0.8` limit;
- current reference and GSV audio players when versions exist;
- generation state, version identifiers, and the last item failure;
- actions `保存`, `保存并生成参考`, `保存并生成 GSV`, `保存并全部重新生成`, and
  `仅重新拼接`.

Closing the dialog does not cancel background work. The timeline and an open dialog continue using
the existing Director polling loop and refresh when item state changes. Unsaved dialog fields are
kept locally across passive polling; closing with a dirty draft asks for confirmation.

## Performance-Direction Data Flow

`director_projects` gains nullable `performance_direction`. Creation and patch APIs normalize a
whitespace-only value to `null` and reject more than 2,000 characters. Project responses include
the field. Changes use the project's optimistic revision and append an audit event.

The contextual emotion client receives the direction once per request as a top-level field, beside
the contextual utterance list. The response for every utterance contains:

- the unchanged utterance ID and revision;
- an eight-value emotion vector;
- speed factor;
- pause after milliseconds.

The orchestration validates complete one-to-one identity and revision coverage before applying any
result. It copies only those three performance fields onto provisional translations. Text fields
remain exactly the translation pass output. A uniform or all-zero vector still normalizes to calm;
speed and pause retain existing schema bounds.

The LLM prompt treats the project direction as a soft global bias. Explicit scene evidence and
dramatic turns may override it. Short reactions inherit scene and role continuity. Empty direction
uses the current contextual behavior unchanged.

## Adjustment and Regeneration Semantics

One Director adjustment request contains the expected project revision, expected utterance revision,
editable values, and one action:

- `save`: save values and mark dependent outputs stale without starting model work;
- `reference`: regenerate reference audio and leave GSV stale;
- `gsv`: regenerate GSV while reusing a valid current reference;
- `both`: regenerate reference then GSV;
- `recompose`: update pause and rebuild the final composition from ready GSV versions.

Dependency escalation is deterministic:

- synthesis text or speed changes invalidate GSV;
- reference text or emotion changes invalidate reference and GSV;
- pause-only changes invalidate only the final composition;
- `gsv` automatically becomes `both` when its current reference is invalid;
- an action weaker than the detected invalidation saves successfully but leaves the item incomplete;
- a no-change regeneration action is allowed and rebuilds the requested audio from current values.

For generated projects, the adjustment service updates the active Director generation item rather
than creating a new project-wide generation. Only one local operation per generation/utterance may
run at a time. Duplicate submissions return the existing accepted operation instead of creating a
second GPU job. The generation becomes `generation_incomplete` while required current output is
missing and returns to `succeeded` after the adjusted utterance is ready and recomposition succeeds.

Short utterances retain the existing pooled-reference policy. A reference regeneration rebuilds or
resolves the applicable role/emotion pool entry, then GSV regeneration consumes that reference.
Previously published versions remain immutable; only active version bindings change.

Before a project has a generation, the same dialog uses the ordinary utterance patch semantics.
Regeneration buttons remain disabled until generation exists, while `保存` remains available during
translation review.

## API Boundaries

- Project creation accepts `performance_direction`.
- A revision-guarded project endpoint updates `performance_direction` and optionally reapplies it.
- A revision-guarded Director adjustment endpoint atomically saves an utterance edit and schedules
  the requested local action.
- Existing progress and audio endpoints remain the source of dialog status and playback URLs.
- Existing whole-project generation, resume, recompose, and export contracts remain compatible.

API errors use existing structured `PipelineError` responses. Blank required synthesis/reference
text, invalid vectors, invalid bounds, unavailable current references, stale revisions, illegal
project states, and duplicate active operations produce specific non-destructive responses. User
drafts remain in the dialog after any failed request.

## State and Audit Rules

All writes use optimistic revisions. Project direction updates and utterance adjustments append
events naming changed fields and requested/effective actions without storing secrets. Generated
artifacts are never overwritten in place.

Reapplying a changed project direction reruns only contextual emotion direction, not script
analysis or translation. It preserves synthesis and reference text exactly. It updates emotion,
speed, and pause for every spoken utterance and invalidates audio according to the same dependency
rules. The UI requires confirmation and shows the number of affected spoken items.

## Accessibility and Layout

Use the native `dialog` element with a labelled heading, focus restoration, Escape handling, and a
single scrollable body. The footer remains visible. Controls have explicit labels and keyboard-
operable sliders. On narrow screens the two-column fields collapse to one column. No new permanent
side panel, timeline column, or navigation item is introduced.

## Verification

Test-first coverage includes:

- migration, create/read/update persistence, length validation, revision conflicts, and audit;
- exact LLM payload and prompt boundaries;
- contextual replacement of vector, speed, and pause without text changes;
- dialog rendering, dirty-draft retention, control synchronization, and operation selection;
- dependency invalidation and automatic `gsv` to `both` escalation;
- save-only, reference-only, GSV-only, both, and recompose execution;
- duplicate submission, failure persistence, retry, pooled short-reference behavior, and version
  immutability;
- Director API contracts and complete non-GPU regression;
- installed real-service smoke validation of persistence, modal assets, and one local regeneration.

## Compatibility

Existing projects migrate with `performance_direction = null`. Existing stored text, emotion,
audio versions, and generation history are preserved. Projects without a direction behave as they
do after the contextual-emotion release. No model files, external services, or new frontend
frameworks are introduced.
