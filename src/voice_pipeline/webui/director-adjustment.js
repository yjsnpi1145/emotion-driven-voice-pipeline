const limits = {
  speed_factor: [0.5, 2],
  pause_after_ms: [0, 30000],
  emotion: [0, 1],
};

const editableFields = [
  "synthesis_text",
  "ref_text_cn",
  "emotion_vector",
  "speed_factor",
  "pause_after_ms",
];

function number(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function normalizedVector(value) {
  return Array.from({ length: 8 }, (_, index) => String(number(value?.[index], 0)));
}

export function createAdjustmentDraft(utterance) {
  return {
    synthesis_text: String(utterance?.synthesis_text ?? ""),
    ref_text_cn: String(utterance?.ref_text_cn ?? ""),
    speed_factor: String(number(utterance?.speed_factor, 1)),
    pause_after_ms: String(Math.round(number(utterance?.pause_after_ms, 0))),
    emotion_vector: normalizedVector(utterance?.emotion_vector),
  };
}

export function normalizeAdjustmentNumber(kind, value) {
  const [minimum, maximum] = limits[kind] || limits.emotion;
  const fallback = kind === "speed_factor" ? 1 : 0;
  let result = Math.min(maximum, Math.max(minimum, number(value, fallback)));
  if (kind === "pause_after_ms") result = Math.round(result);
  return result;
}

export function emotionVectorTotal(vector) {
  return Number((vector || []).reduce((sum, value) => sum + number(value, 0), 0).toFixed(6));
}

export function isEmotionVectorValid(vector) {
  return Array.isArray(vector)
    && vector.length === 8
    && vector.every((value) => number(value, -1) >= 0 && number(value, 2) <= 1)
    && emotionVectorTotal(vector) <= 0.800001;
}

function currentValue(utterance, field) {
  if (field === "emotion_vector") return (utterance.emotion_vector || []).map(Number);
  if (field === "speed_factor" || field === "pause_after_ms") return Number(utterance[field]);
  return String(utterance[field] ?? "");
}

function draftValue(draft, field) {
  if (field === "emotion_vector") return (draft.emotion_vector || []).map(Number);
  if (field === "speed_factor" || field === "pause_after_ms") return Number(draft[field]);
  return String(draft[field] ?? "");
}

export function changedAdjustmentFields(utterance, draft) {
  return editableFields.filter((field) => (
    JSON.stringify(currentValue(utterance, field)) !== JSON.stringify(draftValue(draft, field))
  ));
}

export function buildAdjustmentPayload(utterance, draft, action, projectRevision) {
  return {
    expected_project_revision: Number(projectRevision),
    expected_utterance_revision: Number(utterance.revision),
    synthesis_text: String(draft.synthesis_text ?? ""),
    ref_text_cn: String(draft.ref_text_cn ?? ""),
    speed_factor: normalizeAdjustmentNumber("speed_factor", draft.speed_factor),
    pause_after_ms: normalizeAdjustmentNumber("pause_after_ms", draft.pause_after_ms),
    emotion_vector: normalizedVector(draft.emotion_vector).map((value) => (
      normalizeAdjustmentNumber("emotion", value)
    )),
    action,
  };
}

export function deriveAdjustmentAvailability(
  project,
  progressItem,
  dirtyFields,
  referenceValid,
) {
  const generated = Boolean(project?.current_generation_id);
  const review = project?.status === "translation_review";
  const running = project?.status === "generating"
    || progressItem?.status === "reference_running"
    || progressItem?.status === "gsv_running";
  const referenceDirty = dirtyFields.includes("ref_text_cn")
    || dirtyFields.includes("emotion_vector");
  return {
    save: (review || generated) && !running,
    reference: generated && !running,
    gsv: generated && !running,
    both: generated && !running,
    recompose: generated && !running,
    gsvEscalatesToBoth: referenceDirty || !referenceValid,
  };
}

export function preserveAdjustmentDraft(currentDraft, refreshedDraft, dirty) {
  return dirty ? currentDraft : refreshedDraft;
}
