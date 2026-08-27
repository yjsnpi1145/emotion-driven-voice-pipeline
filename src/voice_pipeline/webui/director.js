import {
  buildAssignmentPatch,
  canEditRoleReview,
  contiguousMergePair,
  toggleSelection,
} from "./director-dnd.js?v=20260828a";
import { syncLazyEditor } from "./director-lazy-editor.js?v=20260828a";
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
    || directorState.dirtyPreprocessTexts.size > 0;
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
    note.textContent = "为所有有效角色选择右侧角色预设后即可生成。";
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
    buttons.push(audio, actionButton("重新拼接", async () => {
      await api(`/api/v1/director-projects/${project.project_id}/recompose`, {
        method: "POST", body: JSON.stringify({ expected_revision: project.revision }),
      });
      notify("已按当前成功版本重新拼接");
      await selectProject(project.project_id);
    }, "secondary-button"));
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
    select.append(new Option("选择角色预设", ""));
    for (const preset of directorState.presets) {
      select.append(new Option(preset.name, preset.preset_id, false, preset.preset_id === role.preset_id));
    }
    select.disabled = !["voice_mapping", "ready"].includes(directorState.project.status);
    select.onchange = async () => {
      if (!select.value) return;
      try {
        await api(`/api/v1/director-roles/${role.role_id}/preset`, {
          method: "POST",
          body: JSON.stringify({ expected_revision: role.revision, preset_id: select.value }),
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

function translatedEditor(utterance) {
  const form = document.createElement("form");
  form.className = "director-translation-editor";
  const target = document.createElement("textarea");
  target.name = "synthesis_text";
  target.rows = 2;
  const draft = directorState.dirtyTranslations.get(utterance.utterance_id);
  target.value = draft?.synthesis_text ?? utterance.synthesis_text ?? "";
  target.placeholder = "目标语言配音文本";
  const reference = document.createElement("textarea");
  reference.name = "ref_text_cn";
  reference.rows = 2;
  reference.value = draft?.ref_text_cn ?? utterance.ref_text_cn ?? "";
  reference.placeholder = "IndexTTS2 中文情绪参考文本";
  const speed = document.createElement("input");
  speed.name = "speed_factor";
  speed.type = "number";
  speed.min = "0.5";
  speed.max = "2";
  speed.step = "0.05";
  speed.value = draft?.speed_factor ?? utterance.speed_factor;
  const pause = document.createElement("input");
  pause.name = "pause_after_ms";
  pause.type = "number";
  pause.min = "0";
  pause.max = "30000";
  pause.step = "50";
  pause.value = draft?.pause_after_ms ?? utterance.pause_after_ms;
  const emotionBox = document.createElement("fieldset");
  emotionBox.className = "director-emotion-vector";
  const legend = document.createElement("legend");
  legend.textContent = "八维情绪向量（LLM 基准，可微调）";
  emotionBox.append(legend);
  const emotionValues = draft?.emotion_vector ?? utterance.emotion_vector ?? Array(8).fill(0);
  const emotionInputs = emotionLabels.map((label, index) => {
    const input = document.createElement("input");
    input.type = "number";
    input.min = "0";
    input.max = "1";
    input.step = "0.01";
    input.value = emotionValues[index] ?? 0;
    emotionBox.append(labeledControl(label, input));
    return input;
  });
  const updateEmotionTotal = () => {
    const total = emotionInputs.reduce((sum, input) => sum + Number(input.value || 0), 0);
    legend.textContent = `八维情绪向量（LLM 基准，可微调）· 合计 ${total.toFixed(2)} / 0.80`;
    emotionBox.dataset.valid = String(total <= 0.800001);
  };
  updateEmotionTotal();
  const save = document.createElement("button");
  save.type = "submit";
  save.className = "secondary-button";
  save.textContent = "保存译文";
  form.append(
    labeledControl("目标语言文本", target),
    labeledControl("中文情绪参考", reference),
    labeledControl("语速（1.0 使用预设默认）", speed),
    labeledControl("句后停顿（ms）", pause),
    emotionBox,
    save,
  );
  const rememberDraft = () => {
    updateEmotionTotal();
    directorState.dirtyTranslations.set(utterance.utterance_id, {
      synthesis_text: target.value,
      ref_text_cn: reference.value,
      speed_factor: speed.value,
      pause_after_ms: pause.value,
      emotion_vector: emotionInputs.map((input) => input.value),
    });
  };
  for (const control of [target, reference, speed, pause, ...emotionInputs]) {
    control.oninput = rememberDraft;
  }
  form.onsubmit = async (event) => {
    event.preventDefault();
    await busy(save, "保存中…", async () => {
      await api(`/api/v1/director-utterances/${utterance.utterance_id}`, {
        method: "PATCH",
        body: JSON.stringify({
          expected_revision: utterance.revision,
          synthesis_text: target.value,
          ref_text_cn: reference.value,
          speed_factor: Number(speed.value),
          pause_after_ms: Number(pause.value),
          emotion_vector: emotionInputs.map((input) => Number(input.value)),
        }),
      });
      directorState.dirtyTranslations.delete(utterance.utterance_id);
      notify("语句译文已保存");
      await selectProject(directorState.project.project_id);
    }).catch((error) => notify(error, true));
  };
  return form;
}

function lazyTranslatedEditor(utterance) {
  const details = document.createElement("details");
  details.className = "director-translation-details";
  const summary = document.createElement("summary");
  let editor = null;
  const updateSummary = () => {
    summary.textContent = directorState.dirtyTranslations.has(utterance.utterance_id)
      ? "编辑译文与情绪 · 有未保存修改"
      : "编辑译文与情绪";
  };
  details.append(summary);
  details.ontoggle = () => {
    editor = syncLazyEditor({
      open: details.open,
      mounted: editor,
      mount: () => {
        const mounted = translatedEditor(utterance);
        details.append(mounted);
        return mounted;
      },
      unmount: (mounted) => mounted.remove(),
    });
    updateSummary();
  };
  updateSummary();
  return details;
}

function renderUtterances() {
  const root = $("#director-utterance-list");
  const fragment = document.createDocumentFragment();
  const editableTranslation = directorState.project.status === "translation_review";
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
    controls.append(select, speak, confirm, split);
    card.append(heading, workingEditor, controls, sourceDetails);
    if (editableTranslation && utterance.speak_enabled) {
      card.append(lazyTranslatedEditor(utterance));
    }
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
