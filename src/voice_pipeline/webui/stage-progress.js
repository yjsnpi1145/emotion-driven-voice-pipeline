export const TASK_STAGE_DEFINITIONS = Object.freeze([
  Object.freeze({ key: "planning", label: "文本规划" }),
  Object.freeze({ key: "reference", label: "参考音频" }),
  Object.freeze({ key: "gsv", label: "GSV 合成" }),
  Object.freeze({ key: "compose", label: "整篇拼接" }),
]);

const ACTIVE_JOB_STATES = new Set(["queued", "running"]);
const STOPPED_RUN_STATES = new Set(["failed", "cancelled", "interrupted"]);

export function deriveChapterStageProgress(run, progress, creationState = null) {
  if (creationState) return creationProgress(creationState);
  if (!run) return idleProgress();

  const segments = Array.isArray(progress?.segments) ? progress.segments : [];
  const total = segments.length;
  const succeeded = run.status === "succeeded";
  const referenceDone = segments.filter((item) => item.active_ref_version_id).length;
  const gsvDone = segments.filter((item) => item.active_gsv_version_id).length;
  const ratios = {
    planning: 1,
    reference: succeeded ? 1 : fraction(referenceDone, total),
    gsv: succeeded ? 1 : fraction(gsvDone, total),
    compose: succeeded || run.final_audio_url ? 1 : 0,
  };
  const stopped = STOPPED_RUN_STATES.has(run.status);
  const activeStage = succeeded ? null : findActiveStage(segments, ratios, stopped);
  const stages = TASK_STAGE_DEFINITIONS.map(({ key, label }) => ({
    key,
    label,
    ratio: ratios[key],
    detail: stageDetail(key, ratios[key], referenceDone, gsvDone, total, activeStage),
    state: stageState(key, ratios[key], activeStage, stopped),
    indeterminate: false,
  }));
  const average = Object.values(ratios).reduce((sum, value) => sum + value, 0) / 4;
  return {
    overallPercent: Math.round(average * 100),
    statusLabel: runStatusLabel(run.status),
    activeStage,
    stages,
  };
}

function idleProgress() {
  return {
    overallPercent: null,
    statusLabel: "尚未选择任务",
    activeStage: null,
    stages: TASK_STAGE_DEFINITIONS.map(({ key, label }) => ({
      key,
      label,
      ratio: 0,
      detail: "等待",
      state: "pending",
      indeterminate: false,
    })),
  };
}

function creationProgress(creationState) {
  const failed = creationState.status === "failed";
  return {
    overallPercent: 0,
    statusLabel: failed ? "文本规划失败" : "文本规划中",
    activeStage: "planning",
    stages: TASK_STAGE_DEFINITIONS.map(({ key, label }) => ({
      key,
      label,
      ratio: 0,
      detail: key === "planning" ? (failed ? "创建失败" : "处理中") : "等待",
      state: key === "planning" ? (failed ? "failed" : "active") : "pending",
      indeterminate: key === "planning" && !failed,
    })),
  };
}

function findActiveStage(segments, ratios, stopped) {
  const activeReference = segments.some((item) =>
    ACTIVE_JOB_STATES.has(item.reference_job_status));
  const activeGsv = segments.some((item) => ACTIVE_JOB_STATES.has(item.gsv_job_status));
  const failedReference = segments.some((item) => item.reference_job_status === "failed");
  const failedGsv = segments.some((item) => item.gsv_job_status === "failed");
  if (stopped && failedReference) return "reference";
  if (stopped && failedGsv) return "gsv";
  if (activeReference) return "reference";
  if (activeGsv) return "gsv";
  if (ratios.reference < 1) return "reference";
  if (ratios.gsv < 1) return "gsv";
  return "compose";
}

function stageState(key, ratio, activeStage, stopped) {
  if (ratio >= 1) return "complete";
  if (key === activeStage) return stopped ? "failed" : "active";
  return ratio > 0 ? "partial" : "pending";
}

function stageDetail(key, ratio, referenceDone, gsvDone, total, activeStage) {
  if (key === "planning") return "已完成";
  if (key === "reference") return `${referenceDone}/${total}`;
  if (key === "gsv") return `${gsvDone}/${total}`;
  if (ratio >= 1) return "已完成";
  return activeStage === "compose" ? "拼接中" : "等待";
}

function runStatusLabel(status) {
  const labels = {
    queued: "等待生成",
    running: "正在生成",
    succeeded: "已完成",
    failed: "任务失败",
    cancelled: "任务已取消",
    interrupted: "任务已中断",
  };
  return labels[status] || "任务状态未知";
}

function fraction(completed, total) {
  if (total <= 0) return 0;
  return Math.min(1, Math.max(0, completed / total));
}
