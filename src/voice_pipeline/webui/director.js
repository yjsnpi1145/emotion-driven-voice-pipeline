import {
  buildAssignmentPatch,
  canEditRoleReview,
  contiguousMergePair,
  toggleSelection,
} from "./director-dnd.js?v=20260828a";
import {
  buildAdjustmentPayload,
  changedAdjustmentFields,
  createAdjustmentDraft,
  deriveAdjustmentAvailability,
  emotionVectorTotal,
  isEmotionVectorValid,
  normalizeAdjustmentNumber,
  preserveAdjustmentDraft,
} from "./director-adjustment.js?v=20260828a";
import {
  directorActivityView,
  directorOperationLabels,
} from "./director-llm-activity.js?v=20260828a";
import {
  canConfirmPreprocessing,
  nextPreprocessOffset,
  preprocessDraftState,
  preprocessStatusLabel,
} from "./director-preprocessing.js?v=20260828a";
import {
  canSplitWorkingText,
  hasUnsavedDirectorDrafts,
  isWorkingTextDirty,
} from "./director-working-text.js?v=20260828a";

const $ = (selector) => document.querySelector(selector);
const directorState = {
  projects: [],
  project: null,
  roles: [],
  utterances: [],
  presets: [],
  profiles: [],
  progress: null,
  selected: new Set(),
  refreshToken: 0,
  polling: false,
  dirtyWorkingTexts: new Map(),
  dirtyTranslations: new Map(),
  activeAdjustmentId: null,
  adjustmentReturnFocus: null,
  performanceDirectionDirty: false,
  preprocessItems: [],
  preprocessTotal: 0,
  preprocessNextOffset: null,
  preprocessLoading: false,
  dirtyPreprocessTexts: new Map(),
  preprocessScrollTop: 0,
  preprocessObserver: null,
  pollTimer: null,
  llmActivity: { active: false, active_operation: null, active_since_utc: null, events: [] },
  llmActivityLoading: false,
  llmActivityUnavailable: false,
};

const statusLabels = {
  draft: "等待预处理",
  preprocessing: "文本预处理中",
  preprocess_review: "预处理校对",
  analyzing: "LLM 分析中",
  role_review: "角色复核",
  translating: "LLM 翻译中",
  translation_review: "翻译校对",
  voice_mapping: "音色映射",
  ready: "可以生成",
  generating: "正在配音",
  generation_incomplete: "部分失败",
  succeeded: "已完成",
};

const kindLabels = {
  dialogue: "对白",
  narration: "旁白",
  stage_direction: "舞台说明",
};
const emotionLabels = ["愉悦", "愤怒", "悲伤", "恐惧", "厌恶", "忧郁", "惊讶", "平静"];

const stageOrder = [
  ["preprocessing", "文本预处理", ["draft", "preprocessing", "preprocess_review"]],
  ["analysis", "分析剧本", ["analyzing"]],
  ["roles", "角色复核", ["role_review"]],
  ["translation", "翻译校对", ["translating", "translation_review"]],
  ["mapping", "音色映射", ["voice_mapping", "ready"]],
  ["generation", "多角色生成", ["generating", "generation_incomplete", "succeeded"]],
];

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: options.body ? { "content-type": "application/json" } : {},
  });
  if (!response.ok) {
    let payload;
    try { payload = await response.json(); } catch { payload = null; }
    const detail = payload?.detail;
    const error = payload?.error || detail?.error || detail || payload;
    const message = typeof error === "string" ? error : error?.message;
    throw new Error(message || `请求失败（HTTP ${response.status}）`);
  }
  return response.json();
}

function notify(message, error = false) {
  const region = $("#toast-region");
  const toast = document.createElement("div");
  toast.className = error ? "toast error-toast" : "toast";
  toast.textContent = String(message);
  region.append(toast);
  window.setTimeout(() => toast.remove(), 4200);
}

async function busy(button, label, callback) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = label;
  try { return await callback(); }
  finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function persistProject() {
  try {
    if (directorState.project) {
      sessionStorage.setItem(
        "voice-pipeline.director-project",
        directorState.project.project_id,
      );
    } else {
      sessionStorage.removeItem("voice-pipeline.director-project");
    }
  } catch { /* sessionStorage is optional. */ }
}

function preprocessSessionKey(projectId) {
  return `voice-pipeline.director-preprocess.${projectId}`;
}

function persistPreprocessSession() {
  const projectId = directorState.project?.project_id;
  if (!projectId) return;
  const root = $("#director-preprocess-list");
  try {
    sessionStorage.setItem(preprocessSessionKey(projectId), JSON.stringify({
      drafts: Object.fromEntries(directorState.dirtyPreprocessTexts),
      scrollTop: root?.scrollTop || 0,
    }));
  } catch { /* sessionStorage is optional. */ }
}

function restorePreprocessSession(projectId) {
  directorState.dirtyPreprocessTexts = new Map();
  directorState.preprocessScrollTop = 0;
  try {
    const value = JSON.parse(sessionStorage.getItem(preprocessSessionKey(projectId)) || "null");
    if (value?.drafts && typeof value.drafts === "object") {
      directorState.dirtyPreprocessTexts = new Map(
        Object.entries(value.drafts).map(([key, text]) => [key, String(text)]),
      );
    }
    directorState.preprocessScrollTop = Number(value?.scrollTop) || 0;
  } catch { /* Ignore corrupt or unavailable session state. */ }
}

function hasUnsavedDirectorChanges() {
  return hasUnsavedDirectorDrafts(directorState)
    || directorState.dirtyPreprocessTexts.size > 0
    || directorState.performanceDirectionDirty;
}

async function loadProjects({ preserveSelection = true } = {}) {
  directorState.projects = await api("/api/v1/director-projects");
  $("#director-project-count").textContent = `${directorState.projects.length} 个`;
  renderProjectList();
  if (!preserveSelection || directorState.project) return;
  let preferred = null;
  try { preferred = sessionStorage.getItem("voice-pipeline.director-project"); } catch { /* noop */ }
  const initial = directorState.projects.find((item) => item.project_id === preferred)
    || directorState.projects[0];
  if (initial) await selectProject(initial.project_id);
}

function renderProjectList() {
  const root = $("#director-project-list");
  const fragment = document.createDocumentFragment();
  for (const project of directorState.projects) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "director-project-row";
    button.dataset.active = String(project.project_id === directorState.project?.project_id);
    const title = document.createElement("strong");
    title.textContent = project.title;
    const meta = document.createElement("span");
    meta.textContent = `${statusLabels[project.status] || project.status} · ${project.target_language.toUpperCase()}`;
    button.append(title, meta);
    button.onclick = () => selectProject(project.project_id).catch((error) => notify(error, true));
    fragment.append(button);
  }
  if (!directorState.projects.length) {
    const empty = document.createElement("p");
    empty.className = "help";
    empty.textContent = "尚未创建导演项目";
    fragment.append(empty);
  }
  root.replaceChildren(fragment);
}

async function selectProject(projectId) {
  if (
    directorState.project
    && directorState.project.project_id !== projectId
    && hasUnsavedDirectorChanges()
    && !window.confirm("当前有未保存的修改，仍要切换项目吗？")
  ) return;
  const changedProject = directorState.project?.project_id !== projectId;
  if (changedProject) {
    persistPreprocessSession();
    directorState.dirtyWorkingTexts.clear();
    directorState.dirtyTranslations.clear();
    directorState.activeAdjustmentId = null;
    directorState.performanceDirectionDirty = false;
    restorePreprocessSession(projectId);
  }
  const token = ++directorState.refreshToken;
  const [project, roles, utterances, progress, preprocessPage] = await Promise.all([
    api(`/api/v1/director-projects/${projectId}`),
    api(`/api/v1/director-projects/${projectId}/roles`),
    api(`/api/v1/director-projects/${projectId}/utterances`),
    api(`/api/v1/director-projects/${projectId}/progress`),
    api(`/api/v1/director-projects/${projectId}/preprocess?offset=0&limit=20`),
  ]);
  if (token !== directorState.refreshToken) return;
  directorState.project = project;
  directorState.projects = directorState.projects.map((item) => (
    item.project_id === project.project_id ? project : item
  ));
  directorState.roles = roles;
  directorState.utterances = utterances;
  const liveUtteranceIds = new Set(utterances.map((item) => item.utterance_id));
  for (const id of directorState.dirtyWorkingTexts.keys()) {
    if (!liveUtteranceIds.has(id)) directorState.dirtyWorkingTexts.delete(id);
  }
  for (const id of directorState.dirtyTranslations.keys()) {
    if (!liveUtteranceIds.has(id)) directorState.dirtyTranslations.delete(id);
  }
  directorState.progress = progress;
  directorState.preprocessItems = preprocessPage.items || [];
  directorState.preprocessTotal = preprocessPage.total_count || 0;
  directorState.preprocessNextOffset = nextPreprocessOffset(preprocessPage);
  directorState.selected = new Set(
    [...directorState.selected].filter((id) => utterances.some((row) => row.utterance_id === id)),
  );
  persistProject();
  renderDirector();
  renderProjectList();
  if (changedProject && directorState.preprocessScrollTop) {
    window.requestAnimationFrame(() => {
      const root = $("#director-preprocess-list");
      if (root) root.scrollTop = directorState.preprocessScrollTop;
    });
  }
}

async function refreshCurrent() {
  await Promise.all([loadProjects(), loadPresets(), loadProfiles(), loadLlmState()]);
  if (directorState.project) await selectProject(directorState.project.project_id);
}

function renderDirector() {
  const project = directorState.project;
  const chip = $("#director-status-chip");
  if (!project) {
    chip.textContent = "未选择项目";
    $("#director-project-title").textContent = "选择或创建一个项目";
    return;
  }
  chip.textContent = statusLabels[project.status] || project.status;
  chip.dataset.state = project.status === "succeeded" || project.status === "ready"
    ? "ready" : project.status === "generation_incomplete" ? "degraded" : "active";
  $("#director-project-title").textContent = project.title;
  $("#director-project-meta").textContent = `${project.source_language.toUpperCase()} → ${project.target_language.toUpperCase()} · 修订 ${project.revision} · ${project.source_text.length} 字符`;
  const narration = $("#director-narration-enabled");
  narration.disabled = !["role_review", "translation_review", "voice_mapping", "ready"].includes(project.status);
  narration.checked = project.narration_enabled;
  renderPreprocessing();
  renderStages();
  renderActions();
  renderRoles();
  renderUtterances();
  renderGenerationProgress();
  renderPerformanceDirection();
  refreshOpenAdjustmentDialog();
}

function renderPerformanceDirection() {
  const project = directorState.project;
  const panel = $("#director-performance-panel");
  if (!project) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  const input = $("#director-performance-direction");
  const button = $("#director-save-performance");
  const busyStatus = ["preprocessing", "analyzing", "translating", "generating"].includes(
    project.status,
  );
  const needsReapply = [
    "translation_review", "voice_mapping", "ready", "generation_incomplete", "succeeded",
  ].includes(project.status);
  if (!directorState.performanceDirectionDirty) input.value = project.performance_direction || "";
  input.disabled = busyStatus;
  button.disabled = busyStatus || !directorState.performanceDirectionDirty;
  button.textContent = needsReapply ? "重新应用到全部语句" : "保存指导";
  $("#director-performance-status").textContent = busyStatus
    ? "当前任务运行中，完成后可编辑"
    : needsReapply
      ? "保存后仅重算情绪、语速和停顿；已有音频会进入待重新生成状态"
      : "仅影响后续 LLM 的情绪向量、语速和句后停顿";
}

function preprocessParagraphCard(paragraph) {
  const card = document.createElement("article");
  card.className = "director-preprocess-card";
  card.dataset.state = paragraph.rewrite_state;

  const heading = document.createElement("header");
  heading.className = "director-preprocess-card-heading";
  const ordinal = document.createElement("strong");
  ordinal.textContent = `段落 ${paragraph.ordinal + 1}`;
  const state = document.createElement("span");
  state.className = "badge";
  state.dataset.state = paragraph.rewrite_state === "fallback" ? "warning" : "ready";
  state.textContent = preprocessStatusLabel(paragraph.rewrite_state);
  heading.append(ordinal, state);

  const grid = document.createElement("div");
  grid.className = "director-preprocess-grid";
  const originalLabel = document.createElement("label");
  originalLabel.append(document.createTextNode("导入原文（只读）"));
  const original = document.createElement("textarea");
  original.readOnly = true;
  original.rows = 6;
  original.value = paragraph.source_text;
  originalLabel.append(original);

  const currentLabel = document.createElement("label");
  currentLabel.append(document.createTextNode("当前预处理稿"));
  const current = document.createElement("textarea");
  current.rows = 6;
  current.value = directorState.dirtyPreprocessTexts.get(paragraph.paragraph_id)
    ?? paragraph.preprocessed_text;
  current.readOnly = directorState.project.status !== "preprocess_review";
  currentLabel.append(current);
  grid.append(originalLabel, currentLabel);

  const status = document.createElement("small");
  status.className = "director-preprocess-save-state";
  const actions = document.createElement("div");
  actions.className = "director-preprocess-card-actions button-row";
  const save = actionButton("保存本段", async () => {
    const updated = await api(
      `/api/v1/director-projects/${directorState.project.project_id}`
      + `/preprocess-paragraphs/${paragraph.paragraph_id}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          expected_project_revision: directorState.project.revision,
          expected_revision: paragraph.revision,
          preprocessed_text: current.value,
        }),
      },
    );
    directorState.dirtyPreprocessTexts.delete(paragraph.paragraph_id);
    persistPreprocessSession();
    notify("预处理段落已保存");
    await refreshPreprocessRow(updated);
  }, "secondary-button");
  const restoreSource = actionButton("恢复原文", async () => {
    await restorePreprocessParagraph(paragraph, "source");
  }, "secondary-button");
  const restoreStructural = actionButton("恢复本地清洗", async () => {
    await restorePreprocessParagraph(paragraph, "structural");
  }, "secondary-button");
  const rewrite = actionButton("重新运行本段 LLM", async () => {
    const updated = await api(
      `/api/v1/director-projects/${directorState.project.project_id}`
      + `/preprocess-paragraphs/${paragraph.paragraph_id}/rewrite`,
      {
        method: "POST",
        body: JSON.stringify({
          expected_project_revision: directorState.project.revision,
          expected_revision: paragraph.revision,
        }),
      },
    );
    directorState.dirtyPreprocessTexts.delete(paragraph.paragraph_id);
    persistPreprocessSession();
    notify("本段 LLM 改写已完成");
    await refreshPreprocessRow(updated);
  }, "secondary-button");
  rewrite.hidden = directorState.project.preprocessing_mode !== "rewrite";
  actions.append(save, restoreSource, restoreStructural, rewrite);

  const details = document.createElement("details");
  details.className = "director-preprocess-diff";
  const summary = document.createElement("summary");
  summary.textContent = "查看本地清洗稿与校验详情";
  const structural = document.createElement("pre");
  structural.textContent = paragraph.structural_text;
  details.append(summary, structural);
  if (paragraph.validation) {
    const validation = document.createElement("pre");
    validation.className = "director-preprocess-validation";
    validation.textContent = JSON.stringify(paragraph.validation, null, 2);
    details.append(validation);
  }

  const updateDraft = () => {
    const draft = preprocessDraftState(paragraph, current.value);
    if (draft.dirty) {
      directorState.dirtyPreprocessTexts.set(paragraph.paragraph_id, current.value);
    } else {
      directorState.dirtyPreprocessTexts.delete(paragraph.paragraph_id);
    }
    current.dataset.dirty = String(draft.dirty);
    status.dataset.state = draft.blank || draft.dirty ? "warning" : "ready";
    status.textContent = draft.blank
      ? "文本不能为空"
      : draft.dirty ? "有未保存修改" : "已保存";
    save.disabled = !draft.canSave || current.readOnly;
    restoreSource.disabled = current.readOnly;
    restoreStructural.disabled = current.readOnly;
    rewrite.disabled = current.readOnly;
    persistPreprocessSession();
    renderPreprocessSummary();
  };
  current.oninput = updateDraft;
  updateDraft();
  card.append(heading, grid, status, actions, details);
  return card;
}

async function restorePreprocessParagraph(paragraph, target) {
  const updated = await api(
    `/api/v1/director-projects/${directorState.project.project_id}`
    + `/preprocess-paragraphs/${paragraph.paragraph_id}/restore`,
    {
      method: "POST",
      body: JSON.stringify({
        expected_project_revision: directorState.project.revision,
        expected_revision: paragraph.revision,
        target,
      }),
    },
  );
  directorState.dirtyPreprocessTexts.delete(paragraph.paragraph_id);
  persistPreprocessSession();
  await refreshPreprocessRow(updated);
}

async function refreshPreprocessRow(updated) {
  const projectId = directorState.project.project_id;
  const root = $("#director-preprocess-list");
  const scrollTop = root?.scrollTop || 0;
  directorState.preprocessItems = directorState.preprocessItems.map((item) => (
    item.paragraph_id === updated.paragraph_id ? updated : item
  ));
  const project = await api(`/api/v1/director-projects/${projectId}`);
  if (directorState.project?.project_id !== projectId) return;
  directorState.project = project;
  directorState.projects = directorState.projects.map((item) => (
    item.project_id === projectId ? project : item
  ));
  renderDirector();
  renderProjectList();
  window.requestAnimationFrame(() => {
    const refreshed = $("#director-preprocess-list");
    if (refreshed) refreshed.scrollTop = scrollTop;
  });
}

function renderPreprocessSummary() {
  const fallbacks = directorState.preprocessItems.filter(
    (item) => item.rewrite_state === "fallback",
  ).length;
  $("#director-preprocess-count").textContent = `${directorState.preprocessTotal} 段`;
  $("#director-preprocess-fallbacks").textContent = fallbacks
    ? `${fallbacks} 段回退（已加载）` : "无回退";
  const drafts = directorState.dirtyPreprocessTexts.size;
  const draftChip = $("#director-preprocess-drafts");
  draftChip.textContent = drafts ? `${drafts} 段未保存` : "无未保存修改";
  draftChip.dataset.state = drafts ? "warning" : "ready";
  const confirm = $("#director-confirm-preprocessing");
  confirm.disabled = !canConfirmPreprocessing(
    directorState.project,
    directorState.dirtyPreprocessTexts,
  );
  confirm.title = drafts ? "请先保存所有段落修改" : "";
}

function renderPreprocessing() {
  const review = $("#director-preprocess-review");
  const visible = ["preprocessing", "preprocess_review"].includes(
    directorState.project?.status,
  );
  review.hidden = !visible;
  if (!visible) return;
  $("#director-preprocess-help").textContent = directorState.project.status === "preprocessing"
    ? "正在生成可校对文本；本地结构清洗无需 GPU，LLM 改写活动会显示在上方。"
    : "逐段核对后确认；角色分析只会读取你确认的预处理稿。";

  const root = $("#director-preprocess-list");
  const fragment = document.createDocumentFragment();
  for (const paragraph of directorState.preprocessItems) {
    fragment.append(preprocessParagraphCard(paragraph));
  }
  if (!directorState.preprocessItems.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state compact";
    empty.textContent = directorState.project.status === "preprocessing"
      ? "正在构建预处理稿…" : "当前项目没有可校对段落";
    fragment.append(empty);
  }
  root.replaceChildren(fragment);
  root.onscroll = persistPreprocessSession;
  renderPreprocessSummary();

  const loadMore = $("#director-preprocess-load-more");
  loadMore.hidden = directorState.preprocessNextOffset === null;
  loadMore.disabled = directorState.preprocessLoading;
  loadMore.textContent = directorState.preprocessLoading ? "加载中…" : "加载更多段落";
  directorState.preprocessObserver?.disconnect();
  directorState.preprocessObserver = null;
  if (!loadMore.hidden && "IntersectionObserver" in window) {
    directorState.preprocessObserver = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        void loadMorePreprocessing().catch((error) => notify(error, true));
      }
    }, { rootMargin: "160px" });
    directorState.preprocessObserver.observe(loadMore);
  }
}

async function loadMorePreprocessing() {
  if (directorState.preprocessLoading || directorState.preprocessNextOffset === null) return;
  directorState.preprocessLoading = true;
  renderPreprocessing();
  try {
    const page = await api(
      `/api/v1/director-projects/${directorState.project.project_id}/preprocess`
      + `?offset=${directorState.preprocessNextOffset}&limit=20`,
    );
    const existing = new Set(directorState.preprocessItems.map((item) => item.paragraph_id));
    directorState.preprocessItems.push(
      ...(page.items || []).filter((item) => !existing.has(item.paragraph_id)),
    );
    directorState.preprocessTotal = page.total_count || directorState.preprocessTotal;
    directorState.preprocessNextOffset = nextPreprocessOffset(page);
  } finally {
    directorState.preprocessLoading = false;
    renderPreprocessing();
  }
}

function renderStages() {
  const current = directorState.project.status;
  const currentIndex = stageOrder.findIndex(([, , states]) => states.includes(current));
  const fragment = document.createDocumentFragment();
  stageOrder.forEach(([key, label], index) => {
    const item = document.createElement("li");
    item.dataset.stage = key;
    item.dataset.state = index < currentIndex || current === "succeeded"
      ? "complete" : index === currentIndex ? "active" : "pending";
    const marker = document.createElement("span");
    marker.textContent = index < currentIndex || current === "succeeded" ? "✓" : String(index + 1);
    const name = document.createElement("strong");
    name.textContent = label;
    item.append(marker, name);
    fragment.append(item);
  });
  $("#director-stage-rail").replaceChildren(fragment);
}

function actionButton(label, handler, kind = "primary-button") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = kind;
  button.textContent = label;
  button.onclick = () => busy(button, "处理中…", handler).catch((error) => notify(error, true));
  return button;
}

function triggerDownload(url) {
  const link = document.createElement("a");
  link.href = url;
  link.download = "";
  document.body.append(link);
  link.click();
  link.remove();
}

function labeledControl(label, control) {
  const wrapper = document.createElement("label");
  wrapper.append(document.createTextNode(label), control);
  return wrapper;
}

function renderActions() {
  const root = $("#director-actions");
  const project = directorState.project;
  const buttons = [];
  if (project.last_error && ["preprocessing", "analyzing", "translating"].includes(project.status)) {
    const failure = document.createElement("span");
    failure.className = "director-command-error";
    failure.textContent = project.last_error.message || "后台任务失败";
    const retryLabels = {
      preprocessing: "重试文本预处理",
      analyzing: "重试剧本分析",
      translating: "重试翻译",
    };
    const retryLabel = retryLabels[project.status];
    buttons.push(failure, actionButton(retryLabel, async () => {
      const endpoints = {
        preprocessing: "preprocess",
        analyzing: "analyze",
        translating: "translate",
      };
      const endpoint = endpoints[project.status];
      if (project.status === "preprocessing") {
        directorState.dirtyPreprocessTexts.clear();
        persistPreprocessSession();
      }
      await api(`/api/v1/director-projects/${project.project_id}/${endpoint}`, {
        method: "POST", body: JSON.stringify({ expected_revision: project.revision }),
      });
      await selectProject(project.project_id);
    }));
  } else if (project.status === "draft") {
    buttons.push(actionButton("开始文本预处理", async () => {
      await api(`/api/v1/director-projects/${project.project_id}/preprocess`, {
        method: "POST", body: JSON.stringify({ expected_revision: project.revision }),
      });
      notify("已开始文本预处理；此阶段不会启动 GPU");
      await selectProject(project.project_id);
    }));
  } else if (["preprocessing", "analyzing", "translating"].includes(project.status)) {
    const labels = {
      preprocessing: "正在预处理…",
      analyzing: "LLM 正在分析…",
      translating: "LLM 正在翻译…",
    };
    const disabled = actionButton(labels[project.status], async () => {});
    disabled.disabled = true;
    buttons.push(disabled);
  } else if (project.status === "role_review") {
    buttons.push(actionButton("确认角色并生成翻译", async () => {
      if (hasUnsavedDirectorDrafts(directorState)) {
        notify("请先保存所有配音文本修改，再进入翻译阶段", true);
        return;
      }
      const confirmed = await api(`/api/v1/director-projects/${project.project_id}/confirm-roles`, {
        method: "POST", body: JSON.stringify({ expected_revision: project.revision }),
      });
      await api(`/api/v1/director-projects/${project.project_id}/translate`, {
        method: "POST", body: JSON.stringify({ expected_revision: confirmed.revision }),
      });
      notify("角色已确认，正在生成目标文本和中文参考文本");
      await selectProject(project.project_id);
    }));
  } else if (project.status === "translation_review") {
    buttons.push(actionButton("确认翻译，进入音色映射", async () => {
      await api(`/api/v1/director-projects/${project.project_id}/confirm-translation`, {
        method: "POST", body: JSON.stringify({ expected_revision: project.revision }),
      });
      await selectProject(project.project_id);
    }));
  } else if (project.status === "voice_mapping") {
    const note = document.createElement("span");
    note.className = "help";
    note.textContent = "为角色选择预设，或选择不予映射以跳过该角色的配音。";
    buttons.push(note);
  } else if (project.status === "ready") {
    buttons.push(actionButton("确认快照并开始多角色生成", async () => {
      await api(`/api/v1/director-projects/${project.project_id}/start-generation`, {
        method: "POST", body: JSON.stringify({ expected_revision: project.revision }),
      });
      notify("已冻结项目快照：先生成全部参考，再按模型分组生成 GSV");
      await selectProject(project.project_id);
    }));
  } else if (project.status === "generation_incomplete") {
    buttons.push(actionButton("继续失败或中断的生成", async () => {
      await api(`/api/v1/director-projects/${project.project_id}/resume-generation`, {
        method: "POST", body: JSON.stringify({ expected_revision: project.revision }),
      });
      await selectProject(project.project_id);
    }));
    buttons.push(actionButton("仅用现有成功版本重新拼接", async () => {
      await api(`/api/v1/director-projects/${project.project_id}/recompose`, {
        method: "POST", body: JSON.stringify({ expected_revision: project.revision }),
      });
      await selectProject(project.project_id);
    }, "secondary-button"));
  } else if (project.status === "succeeded") {
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.preload = "metadata";
    audio.src = `/api/v1/director-projects/${project.project_id}/audio`;
    buttons.push(
      audio,
      actionButton("下载完整 WAV", async () => {
        triggerDownload(`/api/v1/director-projects/${project.project_id}/audio`);
      }, "secondary-button"),
      actionButton("下载逐句 ZIP", async () => {
        triggerDownload(`/api/v1/director-projects/${project.project_id}/sentence-audio.zip`);
      }, "secondary-button"),
      actionButton("重新拼接", async () => {
      await api(`/api/v1/director-projects/${project.project_id}/recompose`, {
        method: "POST", body: JSON.stringify({ expected_revision: project.revision }),
      });
      notify("已按当前成功版本重新拼接");
      await selectProject(project.project_id);
      }, "secondary-button"),
    );
  }
  root.replaceChildren(...buttons);
}

function renderRoles() {
  const root = $("#director-role-list");
  $("#director-role-count").textContent = `${directorState.roles.length} 个`;
  if (["draft", "preprocessing", "preprocess_review", "analyzing"].includes(
    directorState.project.status,
  ) && directorState.roles.length === 0) {
    const waiting = document.createElement("div");
    waiting.className = "empty-state compact";
    waiting.textContent = directorState.project.status === "analyzing"
      ? "LLM 正在发布角色…" : "确认预处理稿后才会分析并发布角色";
    root.replaceChildren(waiting);
    return;
  }
  const fragment = document.createDocumentFragment();
  const roleEditing = canEditRoleReview(directorState.project.status);
  const recoveryHint = directorState.project.status === "translation_review"
    ? "修改后将返回角色复核并需要重新翻译"
    : "";
  for (const role of directorState.roles) {
    const card = document.createElement("article");
    card.className = "director-role-card";
    card.draggable = canEditRoleReview(directorState.project.status);
    card.title = recoveryHint;
    card.dataset.roleId = role.role_id;
    card.ondragstart = (event) => {
      event.dataTransfer.setData("text/director-role-id", role.role_id);
      event.dataTransfer.effectAllowed = "copy";
    };
    const heading = document.createElement("div");
    heading.className = "director-role-heading";
    const name = document.createElement("strong");
    name.textContent = role.canonical_name;
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = role.kind === "narrator" ? "旁白" : role.kind === "character" ? "角色" : "待确认";
    heading.append(name, badge);
    const aliases = document.createElement("small");
    aliases.textContent = role.aliases?.length ? `别名：${role.aliases.join("、")}` : "无别名";
    const select = document.createElement("select");
    select.setAttribute("aria-label", `${role.canonical_name} 的角色预设`);
    select.append(new Option(
      "选择角色预设",
      "",
      false,
      role.dubbing_enabled !== false && !role.preset_id,
    ));
    select.append(new Option(
      "不予映射（跳过配音）",
      "__skip__",
      false,
      role.dubbing_enabled === false,
    ));
    for (const preset of directorState.presets) {
      select.append(new Option(
        preset.name,
        preset.preset_id,
        false,
        role.dubbing_enabled !== false && preset.preset_id === role.preset_id,
      ));
    }
    select.disabled = !["voice_mapping", "ready"].includes(directorState.project.status);
    select.onchange = async () => {
      if (!select.value) return;
      try {
        const skip = select.value === "__skip__";
        const mapping = skip
          ? { mapping_mode: "skip", preset_id: null }
          : { mapping_mode: "preset", preset_id: select.value };
        await api(`/api/v1/director-roles/${role.role_id}/preset`, {
          method: "POST",
          body: JSON.stringify({
            expected_revision: role.revision,
            ...mapping,
          }),
        });
        await selectProject(directorState.project.project_id);
      } catch (error) { notify(error, true); }
    };
    const rename = document.createElement("button");
    rename.type = "button";
    rename.className = "director-inline-button";
    rename.textContent = "重命名";
    rename.disabled = !roleEditing;
    rename.title = recoveryHint;
    rename.onclick = async () => {
      const value = window.prompt("角色名称", role.canonical_name);
      if (!value?.trim()) return;
      try {
        await api(`/api/v1/director-roles/${role.role_id}`, {
          method: "PATCH",
          body: JSON.stringify({ expected_revision: role.revision, canonical_name: value.trim() }),
        });
        await selectProject(directorState.project.project_id);
      } catch (error) { notify(error, true); }
    };
    const split = document.createElement("button");
    split.type = "button";
    split.className = "director-inline-button";
    split.textContent = "把已选语句拆为新角色";
    const selectedForRole = directorState.utterances.filter((item) => (
      directorState.selected.has(item.utterance_id) && item.role_id === role.role_id
    ));
    split.disabled = !roleEditing || !selectedForRole.length;
    split.title = recoveryHint;
    split.onclick = async () => {
      const value = window.prompt("新角色名称");
      if (!value?.trim()) return;
      try {
        await api("/api/v1/director-roles/split", {
          method: "POST",
          body: JSON.stringify({
            project_id: directorState.project.project_id,
            expected_project_revision: directorState.project.revision,
            source_role_id: role.role_id,
            utterance_ids: selectedForRole.map((item) => item.utterance_id),
            canonical_name: value.trim(),
          }),
        });
        directorState.selected = new Set();
        await selectProject(directorState.project.project_id);
      } catch (error) { notify(error, true); }
    };
    const merge = document.createElement("select");
    merge.setAttribute("aria-label", `将 ${role.canonical_name} 合并到其他角色`);
    merge.append(new Option("合并到其他角色…", ""));
    for (const targetRole of directorState.roles.filter((item) => item.role_id !== role.role_id)) {
      merge.append(new Option(targetRole.canonical_name, targetRole.role_id));
    }
    merge.disabled = !roleEditing || directorState.roles.length < 2;
    merge.title = recoveryHint;
    merge.onchange = async () => {
      if (!merge.value || !window.confirm(`确认合并角色“${role.canonical_name}”吗？`)) {
        merge.value = "";
        return;
      }
      try {
        await api("/api/v1/director-roles/merge", {
          method: "POST",
          body: JSON.stringify({
            project_id: directorState.project.project_id,
            expected_project_revision: directorState.project.revision,
            source_role_ids: [role.role_id],
            target_role_id: merge.value,
          }),
        });
        await selectProject(directorState.project.project_id);
      } catch (error) { notify(error, true); }
    };
    card.append(heading, aliases, select, rename, split, merge);
    fragment.append(card);
  }
  root.replaceChildren(fragment);
}

function roleSelect(utterance) {
  const select = document.createElement("select");
  select.setAttribute("aria-label", "语句角色");
  select.append(new Option("未分配角色", ""));
  for (const role of directorState.roles) {
    select.append(new Option(role.canonical_name, role.role_id, false, role.role_id === utterance.role_id));
  }
  select.disabled = !canEditRoleReview(directorState.project.status);
  if (directorState.project.status === "translation_review") {
    select.title = "修改后将返回角色复核并需要重新翻译";
  }
  select.onchange = () => assignRows(new Set([utterance.utterance_id]), select.value);
  return select;
}

function progressItemFor(utteranceId) {
  return directorState.progress?.items?.find((item) => item.utterance_id === utteranceId) || null;
}

function adjustmentControls() {
  return {
    synthesis: $("#director-adjustment-synthesis"),
    reference: $("#director-adjustment-reference"),
    speedRange: $("#director-adjustment-speed-range"),
    speed: $("#director-adjustment-speed"),
    pause: $("#director-adjustment-pause"),
    emotions: [...document.querySelectorAll("[data-director-emotion-number]")],
    emotionRanges: [...document.querySelectorAll("[data-director-emotion-range]")],
  };
}

function currentAdjustmentDraft() {
  const utterance = directorState.utterances.find(
    (item) => item.utterance_id === directorState.activeAdjustmentId,
  );
  if (!utterance) return null;
  return {
    utterance,
    draft: directorState.dirtyTranslations.get(utterance.utterance_id)
      || createAdjustmentDraft(utterance),
  };
}

function readAdjustmentDraft() {
  const controls = adjustmentControls();
  return {
    synthesis_text: controls.synthesis.value,
    ref_text_cn: controls.reference.value,
    speed_factor: controls.speed.value,
    pause_after_ms: controls.pause.value,
    emotion_vector: controls.emotions.map((input) => input.value),
  };
}

function renderAdjustmentEmotionTotal() {
  const fieldset = $(".director-adjustment-emotions");
  const total = emotionVectorTotal(adjustmentControls().emotions.map((input) => input.value));
  const valid = total <= 0.800001;
  fieldset.dataset.valid = String(valid);
  $("#director-adjustment-emotion-total").textContent = `合计 ${total.toFixed(2)} / 0.80`;
  return valid;
}

function rememberAdjustmentDraft() {
  if (!directorState.activeAdjustmentId) return;
  const current = currentAdjustmentDraft();
  if (!current) return;
  const draft = readAdjustmentDraft();
  const dirty = changedAdjustmentFields(current.utterance, draft).length > 0;
  if (dirty) directorState.dirtyTranslations.set(current.utterance.utterance_id, draft);
  else directorState.dirtyTranslations.delete(current.utterance.utterance_id);
  renderAdjustmentEmotionTotal();
  renderAdjustmentAvailability(current.utterance, draft);
}

function assignAudio(playerSelector, versionSelector, versionId) {
  const player = $(playerSelector);
  const label = $(versionSelector);
  if (versionId) {
    const next = `/api/v1/versions/${versionId}/audio`;
    if (!player.src.endsWith(next)) player.src = next;
    player.hidden = false;
    label.textContent = String(versionId).slice(0, 8);
  } else {
    player.removeAttribute("src");
    player.load();
    player.hidden = true;
    label.textContent = "暂无";
  }
}

function renderAdjustmentAvailability(utterance, draft) {
  const item = progressItemFor(utterance.utterance_id);
  const fields = changedAdjustmentFields(utterance, draft);
  const availability = deriveAdjustmentAvailability(
    directorState.project,
    item,
    fields,
    Boolean(utterance.reference_version_id),
  );
  for (const button of document.querySelectorAll("[data-adjustment-action]")) {
    button.disabled = !availability[button.dataset.adjustmentAction];
  }
  const state = $("#director-adjustment-state");
  state.textContent = item?.status || (directorState.project.current_generation_id ? "等待生成" : "尚未生成");
  state.dataset.state = item?.status === "ready" ? "ready" : item?.status === "failed" ? "degraded" : "active";
  const messages = [];
  if (fields.length) messages.push(`未保存：${fields.join("、")}`);
  if (availability.gsvEscalatesToBoth) messages.push("生成 GSV 时将先重建参考音频");
  if (item?.error?.message) messages.push(`上次失败：${item.error.message}`);
  if (!directorState.project.current_generation_id && directorState.project.status !== "translation_review") {
    messages.push("当前阶段不能保存调整；请完成音色映射并开始生成");
  }
  $("#director-adjustment-message").textContent = messages.join(" · ") || "修改后选择下方操作。";
}

function populateAdjustmentDialog(utterance, { preserveDirty = true } = {}) {
  const saved = createAdjustmentDraft(utterance);
  const existing = directorState.dirtyTranslations.get(utterance.utterance_id);
  const draft = preserveAdjustmentDraft(existing || saved, saved, preserveDirty && Boolean(existing));
  const controls = adjustmentControls();
  $("#director-adjustment-title").textContent = `调整配音 · #${utterance.ordinal + 1}`;
  $("#director-adjustment-meta").textContent = `${kindLabels[utterance.kind] || utterance.kind} · 语句修订 ${utterance.revision}`;
  $("#director-adjustment-source").value = utterance.source_text;
  $("#director-adjustment-working").value = utterance.working_text;
  controls.synthesis.value = draft.synthesis_text;
  controls.reference.value = draft.ref_text_cn;
  controls.speed.value = draft.speed_factor;
  controls.speedRange.value = draft.speed_factor;
  controls.pause.value = draft.pause_after_ms;
  controls.emotions.forEach((input, index) => { input.value = draft.emotion_vector[index] ?? "0"; });
  controls.emotionRanges.forEach((input, index) => { input.value = draft.emotion_vector[index] ?? "0"; });
  assignAudio("#director-adjustment-reference-audio", "#director-adjustment-reference-version", utterance.reference_version_id);
  assignAudio("#director-adjustment-gsv-audio", "#director-adjustment-gsv-version", utterance.gsv_version_id);
  renderAdjustmentEmotionTotal();
  renderAdjustmentAvailability(utterance, draft);
}

function openAdjustmentDialog(utterance) {
  const dialog = $("#director-adjustment-dialog");
  directorState.activeAdjustmentId = utterance.utterance_id;
  directorState.adjustmentReturnFocus = document.activeElement;
  populateAdjustmentDialog(utterance);
  if (!dialog.open) dialog.showModal();
  $("#director-adjustment-synthesis").focus();
}

function refreshOpenAdjustmentDialog() {
  const dialog = $("#director-adjustment-dialog");
  if (!dialog?.open || !directorState.activeAdjustmentId) return;
  const utterance = directorState.utterances.find(
    (item) => item.utterance_id === directorState.activeAdjustmentId,
  );
  if (utterance) populateAdjustmentDialog(utterance);
}

function closeAdjustmentDialog() {
  const dialog = $("#director-adjustment-dialog");
  const dirty = directorState.activeAdjustmentId
    && directorState.dirtyTranslations.has(directorState.activeAdjustmentId);
  if (dirty && !window.confirm("这句配音有未保存的调整，仍要关闭吗？")) return;
  dialog.close();
  const focus = directorState.adjustmentReturnFocus;
  directorState.activeAdjustmentId = null;
  directorState.adjustmentReturnFocus = null;
  if (focus?.isConnected) focus.focus();
}

function renderUtterances() {
  const root = $("#director-utterance-list");
  const fragment = document.createDocumentFragment();
  const adjustableStatuses = new Set([
    "translation_review", "voice_mapping", "ready", "generating",
    "generation_incomplete", "succeeded",
  ]);
  const filter = $("#director-utterance-filter").value;
  const visible = directorState.utterances.filter((utterance) => {
    if (filter === "needs_confirmation") return utterance.speak_enabled && !utterance.role_confirmed;
    if (filter === "spoken") return utterance.speak_enabled;
    if (filter === "dialogue" || filter === "narration") return utterance.kind === filter;
    return true;
  });
  for (const utterance of visible) {
    const card = document.createElement("article");
    card.className = "director-utterance-card";
    card.dataset.kind = utterance.kind;
    card.dataset.selected = String(directorState.selected.has(utterance.utterance_id));
    card.dataset.confirmed = String(utterance.role_confirmed);
    card.ondragover = (event) => { event.preventDefault(); card.dataset.dropTarget = "true"; };
    card.ondragleave = () => delete card.dataset.dropTarget;
    card.ondrop = (event) => {
      event.preventDefault();
      delete card.dataset.dropTarget;
      const roleId = event.dataTransfer.getData("text/director-role-id");
      const ids = directorState.selected.has(utterance.utterance_id)
        ? directorState.selected : new Set([utterance.utterance_id]);
      if (roleId) assignRows(ids, roleId);
    };
    const heading = document.createElement("div");
    heading.className = "director-utterance-heading";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = directorState.selected.has(utterance.utterance_id);
    checkbox.onchange = () => {
      directorState.selected = toggleSelection(
        directorState.selected, utterance.utterance_id, checkbox.checked,
      );
      renderRoles();
      renderUtterances();
    };
    const ordinal = document.createElement("strong");
    ordinal.textContent = `#${utterance.ordinal + 1}`;
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = kindLabels[utterance.kind] || utterance.kind;
    const confidence = document.createElement("span");
    confidence.className = "badge";
    confidence.dataset.state = utterance.role_confirmed ? "ready" : "warning";
    confidence.textContent = utterance.role_confirmed
      ? `已确认 ${(utterance.role_confidence * 100).toFixed(0)}%`
      : `待确认 ${(utterance.role_confidence * 100).toFixed(0)}%`;
    heading.append(checkbox, ordinal, badge, confidence);
    const savedWorkingText = utterance.working_text ?? utterance.source_text;
    const workingEditor = document.createElement("div");
    workingEditor.className = "director-working-text-editor";
    const workingLabel = document.createElement("strong");
    workingLabel.textContent = "配音文本";
    const working = document.createElement("textarea");
    working.className = "director-source-slice director-working-text";
    working.value = directorState.dirtyWorkingTexts.get(utterance.utterance_id)
      ?? savedWorkingText;
    working.readOnly = directorState.project.status !== "role_review";
    working.rows = Math.min(5, Math.max(2, working.value.split("\n").length));
    const workingActions = document.createElement("div");
    workingActions.className = "director-working-text-actions";
    const workingStatus = document.createElement("small");
    const saveWorking = document.createElement("button");
    saveWorking.type = "button";
    saveWorking.className = "secondary-button";
    saveWorking.textContent = "保存配音文本";
    saveWorking.hidden = directorState.project.status !== "role_review";
    workingActions.append(workingStatus, saveWorking);
    workingEditor.append(workingLabel, working, workingActions);

    const sourceDetails = document.createElement("details");
    sourceDetails.className = "director-original-source";
    const sourceSummary = document.createElement("summary");
    sourceSummary.textContent = "查看原始切片";
    const source = document.createElement("textarea");
    source.className = "director-source-slice";
    source.value = utterance.source_text;
    source.readOnly = true;
    source.rows = Math.min(5, Math.max(2, utterance.source_text.split("\n").length));
    sourceDetails.append(sourceSummary, source);
    const controls = document.createElement("div");
    controls.className = "director-utterance-controls";
    const select = roleSelect(utterance);
    const speak = document.createElement("label");
    speak.className = "check-row";
    const speakInput = document.createElement("input");
    speakInput.type = "checkbox";
    speakInput.checked = utterance.speak_enabled;
    speakInput.disabled = !canEditRoleReview(directorState.project.status);
    if (directorState.project.status === "translation_review") {
      speak.title = "修改后将返回角色复核并需要重新翻译";
    }
    speakInput.onchange = () => patchUtterance(utterance, { speak_enabled: speakInput.checked });
    speak.append(speakInput, document.createTextNode("配音"));
    const confirm = document.createElement("button");
    confirm.type = "button";
    confirm.className = "director-inline-button";
    confirm.textContent = "确认角色";
    confirm.hidden = utterance.role_confirmed || !canEditRoleReview(directorState.project.status);
    if (directorState.project.status === "translation_review") {
      confirm.title = "确认后将返回角色复核并需要重新翻译";
    }
    confirm.onclick = () => patchUtterance(utterance, { role_confirmed: true });
    const split = document.createElement("button");
    split.type = "button";
    split.className = "director-inline-button";
    split.textContent = "在原文光标处拆分";
    split.onclick = () => splitUtterance(utterance, source.selectionStart);
    const adjust = document.createElement("button");
    adjust.type = "button";
    adjust.className = "director-inline-button director-adjustment-open";
    adjust.textContent = directorState.dirtyTranslations.has(utterance.utterance_id)
      ? "调整配音 · 未保存" : "调整配音";
    adjust.hidden = !utterance.speak_enabled || !adjustableStatuses.has(directorState.project.status);
    adjust.onclick = () => openAdjustmentDialog(utterance);
    const updateWorkingState = () => {
      const dirty = isWorkingTextDirty(
        { ...utterance, working_text: savedWorkingText },
        working.value,
      );
      if (dirty) {
        directorState.dirtyWorkingTexts.set(utterance.utterance_id, working.value);
      } else {
        directorState.dirtyWorkingTexts.delete(utterance.utterance_id);
      }
      working.dataset.dirty = String(dirty);
      workingStatus.textContent = dirty ? "有未保存的修改" : "已保存";
      workingStatus.dataset.state = dirty ? "warning" : "ready";
      saveWorking.disabled = !dirty || !working.value.trim();
      const splitSafe = canSplitWorkingText({
        source_text: utterance.source_text,
        working_text: working.value,
      });
      split.disabled = !canEditRoleReview(directorState.project.status) || !splitSafe;
      split.title = directorState.project.status === "translation_review"
        ? "拆分后将返回角色复核并需要重新翻译"
        : splitSafe ? "请在展开的原始切片中放置光标" : "配音文本修改后不能按原文偏移拆分";
    };
    working.oninput = updateWorkingState;
    saveWorking.onclick = () => busy(saveWorking, "保存中…", async () => {
      if (!working.value.trim()) {
        notify("配音文本不能为空", true);
        return;
      }
      await api(`/api/v1/director-utterances/${utterance.utterance_id}`, {
        method: "PATCH",
        body: JSON.stringify({
          expected_revision: utterance.revision,
          working_text: working.value,
        }),
      });
      directorState.dirtyWorkingTexts.delete(utterance.utterance_id);
      notify("配音文本已保存，后续翻译和配音将使用这段文字");
      await selectProject(directorState.project.project_id);
    }).catch((error) => notify(error, true));
    updateWorkingState();
    controls.append(select, speak, confirm, split, adjust);
    card.append(heading, workingEditor, controls, sourceDetails);
    fragment.append(card);
  }
  if (!visible.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state compact";
    empty.textContent = ["draft", "preprocessing", "preprocess_review"].includes(
      directorState.project.status,
    )
      ? "确认预处理稿后才会生成语句时间线"
      : directorState.project.status === "analyzing" ? "LLM 正在分析剧本…" : "尚无语句";
    fragment.append(empty);
  }
  root.replaceChildren(fragment);
  $("#director-clear-selection").disabled = directorState.selected.size === 0;
  $("#director-merge-selected").disabled = !contiguousMergePair(
    directorState.utterances, directorState.selected,
  ) || !canEditRoleReview(directorState.project.status);
}

async function patchUtterance(utterance, patch) {
  try {
    await api(`/api/v1/director-utterances/${utterance.utterance_id}`, {
      method: "PATCH",
      body: JSON.stringify({ expected_revision: utterance.revision, ...patch }),
    });
    await selectProject(directorState.project.project_id);
  } catch (error) { notify(error, true); }
}

async function assignRows(ids, roleId) {
  if (!roleId) return;
  const patch = buildAssignmentPatch(directorState.utterances, ids, roleId);
  try {
    await api(`/api/v1/director-projects/${directorState.project.project_id}/assign-role`, {
      method: "POST", body: JSON.stringify(patch),
    });
    directorState.selected = new Set();
    await selectProject(directorState.project.project_id);
  } catch (error) { notify(error, true); }
}

async function splitUtterance(utterance, localIndex) {
  if (localIndex <= 0 || localIndex >= utterance.source_text.length) {
    notify("请先把光标放在语句正文内部", true);
    return;
  }
  try {
    await api(`/api/v1/director-utterances/${utterance.utterance_id}/split`, {
      method: "POST",
      body: JSON.stringify({
        expected_revision: utterance.revision,
        split_at: utterance.source_start + localIndex,
      }),
    });
    await selectProject(directorState.project.project_id);
  } catch (error) { notify(error, true); }
}

async function mergeSelected() {
  if (hasUnsavedDirectorDrafts(directorState)) {
    notify("请先保存当前修改，再合并语句", true);
    return;
  }
  const pair = contiguousMergePair(directorState.utterances, directorState.selected);
  if (!pair) return;
  await api("/api/v1/director-utterances/merge", {
    method: "POST",
    body: JSON.stringify({
      left_utterance_id: pair[0].utterance_id,
      right_utterance_id: pair[1].utterance_id,
      expected_left_revision: pair[0].revision,
      expected_right_revision: pair[1].revision,
    }),
  });
  directorState.selected = new Set();
  await selectProject(directorState.project.project_id);
}

function renderGenerationProgress() {
  const root = $("#director-generation-progress");
  const generation = directorState.progress?.generation;
  const items = directorState.progress?.items || [];
  if (!generation) {
    root.replaceChildren();
    return;
  }
  const heading = document.createElement("div");
  heading.className = "panel-heading";
  const title = document.createElement("strong");
  title.textContent = "多角色生成进度";
  const ready = items.filter((item) => item.status === "ready").length;
  const count = document.createElement("span");
  count.className = "status-chip";
  count.textContent = `${ready} / ${items.length}`;
  heading.append(title, count);
  const track = document.createElement("div");
  track.className = "director-generation-track";
  for (const item of items) {
    const marker = document.createElement("span");
    marker.dataset.state = item.status;
    marker.title = `语句 ${item.ordinal + 1} · ${item.status}`;
    track.append(marker);
  }
  const details = document.createElement("div");
  details.className = "director-generation-items";
  for (const item of items) {
    const row = document.createElement("div");
    row.dataset.state = item.status;
    const label = document.createElement("span");
    label.textContent = `#${item.ordinal + 1}`;
    const state = document.createElement("strong");
    state.textContent = item.status;
    row.append(label, state);
    if (item.reference_mode === "pooled" && item.reference_pool) {
      const emotionLabels = {
        joy: "愉悦", anger: "愤怒", sadness: "悲伤", fear: "恐惧",
        disgust: "厌恶", melancholy: "忧郁", surprise: "惊讶", calm: "平静",
      };
      const poolBadge = document.createElement("span");
      poolBadge.className = "director-pool-badge";
      const currentEmotion = emotionLabels[item.reference_emotion_bucket]
        || item.reference_emotion_bucket;
      if (item.reference_degraded_from) {
        const originalEmotion = emotionLabels[item.reference_degraded_from]
          || item.reference_degraded_from;
        poolBadge.textContent = `已降级：${originalEmotion} → ${currentEmotion}`;
      } else {
        poolBadge.textContent = `情绪池：${currentEmotion}`;
      }
      const poolDetails = document.createElement("details");
      poolDetails.className = "director-pool-details";
      const poolSummary = document.createElement("summary");
      poolSummary.textContent = `参考文本 · 版本 ${item.reference_pool.revision + 1}`;
      const prompt = document.createElement("p");
      prompt.textContent = item.reference_pool.prompt_text;
      poolDetails.append(poolSummary, prompt);
      const rebuild = document.createElement("button");
      rebuild.type = "button";
      rebuild.className = "director-inline-button";
      rebuild.textContent = "重建池参考";
      rebuild.onclick = () => busy(rebuild, "提交中…", async () => {
        await api(`/api/v1/director-generations/${generation.generation_id}/utterances/${item.utterance_id}/rebuild-pooled-reference`, {
          method: "POST",
        });
        notify("已安排重建池参考；原参考会保留到新版本成功");
        await selectProject(directorState.project.project_id);
      }).catch((error) => notify(error, true));
      row.append(poolBadge, poolDetails, rebuild);
    }
    if (item.error?.message) {
      const error = document.createElement("small");
      error.textContent = item.error.message;
      row.append(error);
    }
    details.append(row);
  }
  root.replaceChildren(heading, track, details);
}

async function loadPresets() {
  directorState.presets = await api("/api/v1/role-presets");
  $("#director-preset-count").textContent = `${directorState.presets.length} 个`;
  const fragment = document.createDocumentFragment();
  for (const preset of directorState.presets) {
    const card = document.createElement("article");
    card.className = "director-preset-card";
    const heading = document.createElement("div");
    heading.className = "director-role-heading";
    const name = document.createElement("strong");
    name.textContent = preset.name;
    const status = document.createElement("span");
    status.className = "badge";
    status.dataset.state = preset.status === "ready" ? "ready" : "warning";
    status.textContent = preset.status;
    heading.append(name, status);
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.preload = "metadata";
    audio.src = preset.audio_url;
    const meta = document.createElement("small");
    meta.textContent = `${preset.duration_seconds.toFixed(1)}s · ${preset.default_speed.toFixed(2)}x`;
    card.append(heading, audio, meta);
    fragment.append(card);
  }
  $("#director-preset-list").replaceChildren(fragment);
  if (directorState.project) renderRoles();
}

async function loadProfiles() {
  directorState.profiles = await api("/api/v1/model-profiles");
  const select = $("#director-model-profile");
  const value = select.value;
  select.replaceChildren(new Option("选择模型档案", ""));
  for (const profile of directorState.profiles.filter((item) => item.status === "ready")) {
    select.append(new Option(profile.display_name, profile.profile_id));
  }
  if ([...select.options].some((option) => option.value === value)) select.value = value;
}

function formatActivityTime(value) {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return "--:--:--";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(timestamp);
}

function renderDirectorLlmActivity() {
  const log = $("#director-llm-log");
  const status = $("#director-llm-status");
  if (!log || !status) return;
  const shouldFollow = log.scrollHeight - log.scrollTop - log.clientHeight < 32;
  const view = directorActivityView(
    directorState.llmActivity,
    directorState.llmActivityUnavailable,
  );
  status.dataset.state = view.statusState;
  status.textContent = view.statusText;

  if (!view.events.length) {
    const empty = document.createElement("p");
    empty.className = "llm-activity-empty";
    empty.textContent = directorState.llmActivityUnavailable
      ? "活动接口暂时不可用，正在重试"
      : "等待文本预处理、剧本分析或翻译请求";
    log.replaceChildren(empty);
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const event of view.events) {
    const entry = document.createElement("article");
    entry.className = "llm-activity-entry";
    entry.dataset.kind = event.kind;

    const time = document.createElement("time");
    time.className = "llm-activity-time";
    time.dateTime = event.created_at_utc;
    time.textContent = formatActivityTime(event.created_at_utc);

    const operation = document.createElement("span");
    operation.className = "llm-activity-operation";
    operation.textContent = directorOperationLabels[event.operation] || "导演 LLM";

    const message = document.createElement("span");
    message.className = "llm-activity-message";
    message.textContent = event.message;
    entry.append(time, operation, message);

    if (event.content) {
      const output = document.createElement("pre");
      output.textContent = event.content;
      entry.append(output);
    }
    fragment.append(entry);
  }
  log.replaceChildren(fragment);
  if (shouldFollow) {
    window.requestAnimationFrame(() => {
      log.scrollTop = log.scrollHeight;
    });
  }
}

async function loadLlmState() {
  if (directorState.llmActivityLoading) return;
  directorState.llmActivityLoading = true;
  try {
    directorState.llmActivity = await api("/api/v1/llm/activity");
    directorState.llmActivityUnavailable = false;
  } catch {
    directorState.llmActivityUnavailable = true;
  } finally {
    directorState.llmActivityLoading = false;
    renderDirectorLlmActivity();
  }
}

$("#director-project-form").onsubmit = async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const submit = form.querySelector('button[type="submit"]');
  await busy(submit, "创建中…", async () => {
    const source = String(data.get("source_text") || "");
    if (!source.trim()) throw new Error("请先粘贴文章或剧本");
    const project = await api("/api/v1/director-projects", {
      method: "POST",
      body: JSON.stringify({
        title: String(data.get("title") || "新导演项目"),
        source_text: source,
        source_language: data.get("source_language"),
        target_language: data.get("target_language"),
        preprocessing_mode: data.get("preprocessing_mode"),
        narration_enabled: data.get("narration_enabled") === "on",
        performance_direction: String(data.get("performance_direction") || "").trim() || null,
      }),
    });
    await api(`/api/v1/director-projects/${project.project_id}/preprocess`, {
      method: "POST",
      body: JSON.stringify({ expected_revision: project.revision }),
    });
    form.reset();
    form.elements.title.value = "新导演项目";
    form.elements.narration_enabled.checked = true;
    $("#director-skip-preprocessing-warning").hidden = true;
    await loadProjects();
    await selectProject(project.project_id);
    notify("导演项目已创建并开始文本预处理；GPU 不会启动");
  }).catch((error) => notify(error, true));
};

$("#director-preprocessing-mode").onchange = (event) => {
  $("#director-skip-preprocessing-warning").hidden = event.currentTarget.value !== "skip";
};

$("#director-confirm-preprocessing").onclick = (event) => busy(
  event.currentTarget,
  "确认中…",
  async () => {
    if (!canConfirmPreprocessing(
      directorState.project,
      directorState.dirtyPreprocessTexts,
    )) {
      notify("请先保存所有预处理段落，再确认", true);
      return;
    }
    const projectId = directorState.project.project_id;
    await api(`/api/v1/director-projects/${projectId}/confirm-preprocessing`, {
      method: "POST",
      body: JSON.stringify({ expected_revision: directorState.project.revision }),
    });
    try { sessionStorage.removeItem(preprocessSessionKey(projectId)); } catch { /* noop */ }
    directorState.dirtyPreprocessTexts.clear();
    notify("预处理稿已确认，正在分析角色和对白");
    await selectProject(projectId);
  },
).catch((error) => notify(error, true));

$("#director-preprocess-load-more").onclick = () => (
  loadMorePreprocessing().catch((error) => notify(error, true))
);

$("#director-preset-form").onsubmit = async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const submit = form.querySelector('button[type="submit"]');
  await busy(submit, "导入中…", async () => {
    await api("/api/v1/role-presets", {
      method: "POST",
      body: JSON.stringify({
        name: data.get("name"),
        base_voice_path: data.get("base_voice_path"),
        model_profile_id: data.get("model_profile_id"),
        default_speed: Number(data.get("default_speed")),
      }),
    });
    form.reset();
    form.elements.default_speed.value = "1";
    await loadPresets();
    notify("角色预设已复制到本地托管库");
  }).catch((error) => notify(error, true));
};

$("#director-pick-base-voice").onclick = async (event) => {
  await busy(event.currentTarget, "选择中…", async () => {
    const result = await api("/api/v1/local/pick-file", {
      method: "POST", body: JSON.stringify({ kind: "base_voice" }),
    });
    if (result.selected && result.path) $("#director-base-voice-path").value = result.path;
  }).catch((error) => notify(error, true));
};

$("#director-narration-enabled").onchange = async (event) => {
  if (!directorState.project) return;
  try {
    await api(`/api/v1/director-projects/${directorState.project.project_id}/narration`, {
      method: "PATCH",
      body: JSON.stringify({
        expected_revision: directorState.project.revision,
        enabled: event.currentTarget.checked,
      }),
    });
    await selectProject(directorState.project.project_id);
  } catch (error) { notify(error, true); }
};

$("#director-performance-direction").oninput = () => {
  if (!directorState.project) return;
  directorState.performanceDirectionDirty = (
    $("#director-performance-direction").value !== (directorState.project.performance_direction || "")
  );
  renderPerformanceDirection();
};

$("#director-save-performance").onclick = (event) => busy(
  event.currentTarget,
  "保存中…",
  async () => {
    const project = directorState.project;
    const value = $("#director-performance-direction").value.trim();
    const needsReapply = [
      "translation_review", "voice_mapping", "ready", "generation_incomplete", "succeeded",
    ].includes(project.status);
    if (needsReapply && !window.confirm(
      `将为 ${directorState.utterances.filter((item) => item.speak_enabled).length} 条配音语句重新计算情绪、语速和停顿，并使已有音频待重新生成。继续吗？`,
    )) return;
    await api(`/api/v1/director-projects/${project.project_id}/performance-direction`, {
      method: "PATCH",
      body: JSON.stringify({
        expected_revision: project.revision,
        performance_direction: value || null,
        reapply: needsReapply,
      }),
    });
    directorState.performanceDirectionDirty = false;
    notify(needsReapply ? "已重新应用全局表演指导" : "全局表演指导已保存");
    await selectProject(project.project_id);
  },
).catch((error) => notify(error, true));

function initializeAdjustmentDialog() {
  const emotionRoot = $("#director-adjustment-emotion-controls");
  const fragment = document.createDocumentFragment();
  emotionLabels.forEach((label, index) => {
    const row = document.createElement("label");
    row.className = "director-emotion-control";
    const name = document.createElement("span");
    name.textContent = label;
    const range = document.createElement("input");
    range.type = "range";
    range.min = "0";
    range.max = "1";
    range.step = "0.01";
    range.dataset.directorEmotionRange = String(index);
    const numberInput = document.createElement("input");
    numberInput.type = "number";
    numberInput.min = "0";
    numberInput.max = "1";
    numberInput.step = "0.01";
    numberInput.dataset.directorEmotionNumber = String(index);
    range.oninput = () => {
      numberInput.value = range.value;
      rememberAdjustmentDraft();
    };
    numberInput.oninput = () => {
      range.value = String(normalizeAdjustmentNumber("emotion", numberInput.value));
      rememberAdjustmentDraft();
    };
    numberInput.onchange = () => {
      numberInput.value = String(normalizeAdjustmentNumber("emotion", numberInput.value));
      range.value = numberInput.value;
      rememberAdjustmentDraft();
    };
    row.append(name, range, numberInput);
    fragment.append(row);
  });
  emotionRoot.replaceChildren(fragment);

  const controls = adjustmentControls();
  controls.synthesis.oninput = rememberAdjustmentDraft;
  controls.reference.oninput = rememberAdjustmentDraft;
  controls.pause.oninput = rememberAdjustmentDraft;
  controls.speedRange.oninput = () => {
    controls.speed.value = controls.speedRange.value;
    rememberAdjustmentDraft();
  };
  controls.speed.oninput = () => {
    controls.speedRange.value = String(normalizeAdjustmentNumber("speed_factor", controls.speed.value));
    rememberAdjustmentDraft();
  };
  controls.speed.onchange = () => {
    controls.speed.value = String(normalizeAdjustmentNumber("speed_factor", controls.speed.value));
    controls.speedRange.value = controls.speed.value;
    rememberAdjustmentDraft();
  };
  controls.pause.onchange = () => {
    controls.pause.value = String(normalizeAdjustmentNumber("pause_after_ms", controls.pause.value));
    rememberAdjustmentDraft();
  };

  const dialog = $("#director-adjustment-dialog");
  $("#director-adjustment-close").onclick = closeAdjustmentDialog;
  dialog.oncancel = (event) => {
    event.preventDefault();
    closeAdjustmentDialog();
  };
  $("#director-adjustment-form").onsubmit = async (event) => {
    event.preventDefault();
    const action = event.submitter?.dataset.adjustmentAction;
    if (!action) return;
    const current = currentAdjustmentDraft();
    if (!current) return;
    const draft = readAdjustmentDraft();
    if (!draft.synthesis_text.trim() || !draft.ref_text_cn.trim()) {
      notify("目标语言文本和中文参考文本不能为空", true);
      return;
    }
    if (!isEmotionVectorValid(draft.emotion_vector)) {
      notify("八维情绪向量合计必须小于或等于 0.80", true);
      return;
    }
    const button = event.submitter;
    await busy(button, "提交中…", async () => {
      const result = await api(
        `/api/v1/director-projects/${directorState.project.project_id}/utterances/${current.utterance.utterance_id}/adjust`,
        {
          method: "POST",
          body: JSON.stringify(buildAdjustmentPayload(
            current.utterance,
            draft,
            action,
            directorState.project.revision,
          )),
        },
      );
      directorState.dirtyTranslations.delete(current.utterance.utterance_id);
      await selectProject(directorState.project.project_id);
      const escalation = result.effective_action !== result.requested_action
        ? `；已自动升级为 ${result.effective_action}` : "";
      $("#director-adjustment-message").textContent = `操作已接受${escalation}，可关闭窗口或等待状态刷新。`;
      notify(`配音调整已提交${escalation}`);
    }).catch((error) => {
      $("#director-adjustment-message").textContent = error.message;
      notify(error, true);
    });
  };
}

$("#director-merge-selected").onclick = () => mergeSelected().catch((error) => notify(error, true));
$("#director-clear-selection").onclick = () => {
  directorState.selected = new Set();
  renderRoles();
  renderUtterances();
};
$("#director-utterance-filter").onchange = renderUtterances;
$("#director-refresh").onclick = (event) => busy(
  event.currentTarget, "刷新中…", refreshCurrent,
).catch((error) => notify(error, true));

function stopDirectorActivity() {
  persistPreprocessSession();
  directorState.preprocessObserver?.disconnect();
  directorState.preprocessObserver = null;
  if (directorState.pollTimer !== null) {
    window.clearInterval(directorState.pollTimer);
    directorState.pollTimer = null;
  }
  directorState.refreshToken += 1;
}

window.directorWorkbench = {
  refresh: refreshCurrent,
  stop: stopDirectorActivity,
};

async function initializeDirector() {
  initializeAdjustmentDialog();
  try {
    await Promise.all([loadProjects({ preserveSelection: false }), loadPresets(), loadProfiles()]);
  } catch (error) { notify(error, true); }
  directorState.pollTimer = window.setInterval(() => {
    void loadLlmState();
    const status = directorState.project?.status;
    if (["preprocessing", "analyzing", "translating", "generating"].includes(status)) {
      void selectProject(directorState.project.project_id).catch(() => {});
    }
  }, 1200);
}

initializeDirector();
