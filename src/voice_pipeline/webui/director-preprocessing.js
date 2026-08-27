export function preprocessDraftState(paragraph, draftText) {
  const saved = paragraph?.preprocessed_text ?? paragraph?.current_text ?? "";
  const value = String(draftText ?? "");
  const dirty = value !== saved;
  const blank = value.trim().length === 0;
  return { dirty, blank, canSave: dirty && !blank };
}

export function canConfirmPreprocessing(project, dirtyDrafts) {
  return project?.status === "preprocess_review" && dirtyDrafts?.size === 0;
}

export function nextPreprocessOffset(page) {
  const value = page?.next_offset;
  return Number.isInteger(value) && value >= 0 ? value : null;
}

const PREPROCESS_STATUS_LABELS = {
  pending: "等待处理",
  local: "本地结构清洗",
  succeeded: "LLM 改写完成",
  fallback: "已回退到本地清洗稿",
  user_edited: "用户已编辑",
};

export function preprocessStatusLabel(status) {
  return PREPROCESS_STATUS_LABELS[status] || status || "未知状态";
}
