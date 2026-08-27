export function isWorkingTextDirty(utterance, draft) {
  return typeof draft === "string" && draft !== utterance.working_text;
}

export function canSplitWorkingText(utterance) {
  return utterance.working_text === utterance.source_text;
}

export function hasUnsavedDirectorDrafts(state) {
  return Boolean(state.dirtyWorkingTexts?.size || state.dirtyTranslations?.size);
}
