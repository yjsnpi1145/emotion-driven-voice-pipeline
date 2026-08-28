# Contextual Emotion Direction Design

## Goal

Replace the Director translation stage's incidental per-line emotion guesses with a dedicated
context-aware emotion-direction pass. Each spoken utterance must be directed using its speaker,
nearby narration and stage directions, adjacent utterances, and the local scene excerpt.

## Current failure

The translation request receives batches of spoken `source_text` values only. It omits speaker
identity and non-spoken context, runs batches independently, and merely asks for a schema-valid
vector. This allowed meaningless vectors such as `[0.1] * 8` and implausible classifications such
as hesitation being directed as disgust.

## Architecture

Translation remains responsible for `synthesis_text` and Chinese reference text. After all
translation batches complete, `ScriptAnalysisService` builds one `EmotionDirectionInput` for every
spoken utterance from the complete reviewed Director timeline. Each input contains the stable
utterance identity, speaker name, source text, a bounded source excerpt including narration and
stage directions, and up to three preceding/following timeline units with role and kind.

The LLM receives these self-contained inputs in ordered batches and returns a dedicated
`EmotionDirectionResult`. The prompt explicitly requires contextual intent, character continuity,
low intensity under ambiguity, and inheritance for interjections. The final published translation
replaces the translation response's provisional vector with the directed vector.

## Vector policy

- Order remains joy, anger, sadness, fear, disgust, melancholy, surprise, calm.
- Values remain in `0.0..1.0` with sum at most `0.8`.
- A uniform non-zero vector is invalid direction, not a meaningful blended performance.
- Uniform or all-zero results are deterministically normalized to calm `0.20` so a malformed model
  choice does not fail the whole translation run or leak into reference generation.
- No generic smoothing is applied: legitimate abrupt emotional turns must remain possible.

## Context boundaries

- Scene excerpt: at most 600 characters before and after the utterance from the reviewed source.
- Timeline neighbors: up to three before and three after, including non-spoken units.
- Speaker names come from the reviewed canonical role mapping.
- Every batch item is self-contained, so batch boundaries do not remove local context.

## Compatibility and observability

The existing translation schema remains accepted for OpenAI-compatible providers, but its
`emotion_vector` is provisional. A new `emotion_direction` LLM activity operation appears in the
Director live console. Fake/test directors implement the same method. Existing stored projects are
unchanged; newly run translations use the new pass.

## Error handling

HTTP and schema failures from the emotion pass fail the translation command with the existing LLM
error envelope and leave the project recoverable. Invalid vector semantics are normalized locally
and do not trigger a second network request. Identity/revision mismatches fail closed before
publishing any translation.

## Acceptance

Tests must prove that the LLM receives speaker, narration/stage context and cross-batch neighbors;
translation vectors are replaced by emotion-direction results; uniform vectors become calm; item
identity mismatches are rejected; existing Director API and generation tests continue to pass.
