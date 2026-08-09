export const WORKBENCH_SELECTION_KEY = "emotion-driven-voice-pipeline.workbench-selection.v1";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function emptySelection() {
  return { runId: null, segmentId: null };
}

function validId(value) {
  return typeof value === "string" && UUID_PATTERN.test(value);
}

export function readWorkbenchSelection(storage) {
  try {
    const raw = storage.getItem(WORKBENCH_SELECTION_KEY);
    if (!raw) return emptySelection();
    const parsed = JSON.parse(raw);
    if (parsed?.schema_version !== 1 || !validId(parsed.run_id)) return emptySelection();
    return {
      runId: parsed.run_id,
      segmentId: validId(parsed.segment_id) ? parsed.segment_id : null,
    };
  } catch {
    return emptySelection();
  }
}

export function writeWorkbenchSelection(storage, { runId, segmentId }) {
  if (!validId(runId) || (segmentId !== null && !validId(segmentId))) return false;
  try {
    storage.setItem(WORKBENCH_SELECTION_KEY, JSON.stringify({
      schema_version: 1,
      run_id: runId,
      segment_id: segmentId,
    }));
    return true;
  } catch {
    return false;
  }
}

export function clearWorkbenchSelection(storage) {
  try {
    storage.removeItem(WORKBENCH_SELECTION_KEY);
    return true;
  } catch {
    return false;
  }
}

export function chooseInitialRunId(chapters, savedRunId) {
  if (!Array.isArray(chapters) || chapters.length === 0) return null;
  const saved = chapters.find((item) => item.run_id === savedRunId);
  if (saved) return saved.run_id;
  const active = chapters.find((item) => ["queued", "running"].includes(item.status));
  return (active || chapters[0]).run_id;
}
