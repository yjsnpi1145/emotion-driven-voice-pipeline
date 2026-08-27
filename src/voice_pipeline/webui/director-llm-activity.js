const DIRECTOR_OPERATIONS = new Set([
  "script_preprocessing",
  "script_analysis",
  "cast_reconciliation",
  "script_translation",
]);

const TERMINAL_KINDS = new Set(["completed", "failed"]);

export const directorOperationLabels = {
  script_preprocessing: "文本预处理",
  script_analysis: "剧本分析",
  cast_reconciliation: "角色归并",
  script_translation: "台词翻译",
};

function elapsedSeconds(startedAtUtc, nowMs) {
  const startedAt = Date.parse(startedAtUtc);
  if (!Number.isFinite(startedAt)) return 0;
  return Math.max(0, Math.floor((nowMs - startedAt) / 1000));
}

export function directorActivityView(snapshot, unavailable = false, nowMs = Date.now()) {
  const events = Array.isArray(snapshot?.events)
    ? snapshot.events.filter((event) => DIRECTOR_OPERATIONS.has(event.operation))
    : [];
  const lifecycles = new Map();
  for (const event of events) {
    const lifecycle = lifecycles.get(event.operation_id) || {
      firstCreatedAtUtc: event.created_at_utc,
      latestKind: event.kind,
    };
    lifecycle.latestKind = event.kind;
    lifecycles.set(event.operation_id, lifecycle);
  }

  const activeStarts = [...lifecycles.values()]
    .filter((item) => !TERMINAL_KINDS.has(item.latestKind))
    .map((item) => item.firstCreatedAtUtc);
  if (
    activeStarts.length === 0
    && snapshot?.active
    && DIRECTOR_OPERATIONS.has(snapshot.active_operation)
    && snapshot.active_since_utc
  ) {
    activeStarts.push(snapshot.active_since_utc);
  }
  activeStarts.sort((left, right) => Date.parse(left) - Date.parse(right));

  const activeSinceUtc = activeStarts[0] || null;
  const active = activeSinceUtc !== null;
  const latest = events.at(-1);
  let statusState = "idle";
  let statusText = "空闲";
  if (unavailable) {
    statusState = "degraded";
    statusText = "连接异常";
  } else if (active) {
    statusState = "active";
    statusText = `正在工作 · ${elapsedSeconds(activeSinceUtc, nowMs)}s`;
  } else if (latest?.kind === "failed") {
    statusState = "degraded";
    statusText = "失败";
  } else if (latest?.kind === "completed") {
    statusState = "ready";
    statusText = "已完成";
  }

  return { events, active, activeSinceUtc, statusState, statusText };
}
