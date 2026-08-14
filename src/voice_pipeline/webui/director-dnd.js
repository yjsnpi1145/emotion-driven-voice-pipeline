export function buildAssignmentPatch(rows, selectedIds, roleId) {
  const selected = rows
    .filter((row) => selectedIds.has(row.utterance_id))
    .sort((left, right) => left.ordinal - right.ordinal);
  return {
    utterance_ids: selected.map((row) => row.utterance_id),
    expected_revisions: Object.fromEntries(
      selected.map((row) => [row.utterance_id, row.revision]),
    ),
    role_id: roleId,
    role_confirmed: true,
  };
}

export function contiguousMergePair(rows, selectedIds) {
  const selected = rows
    .filter((row) => selectedIds.has(row.utterance_id))
    .sort((left, right) => left.ordinal - right.ordinal);
  if (selected.length !== 2 || selected[1].ordinal !== selected[0].ordinal + 1) {
    return null;
  }
  if (selected[0].source_end !== undefined
      && selected[1].source_start !== undefined
      && selected[0].source_end !== selected[1].source_start) {
    return null;
  }
  return selected;
}

export function filterNarration(rows, narrationEnabled) {
  return narrationEnabled ? [...rows] : rows.filter((row) => row.kind !== "narration");
}

export function toggleSelection(selectedIds, utteranceId, selected) {
  const next = new Set(selectedIds);
  if (selected) next.add(utteranceId);
  else next.delete(utteranceId);
  return next;
}
