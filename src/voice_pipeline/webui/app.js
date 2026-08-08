const state = {
  chapters: [],
  profiles: [],
  run: null,
  progress: null,
  segments: [],
  selected: null,
  history: null,
  events: null,
  refreshTimer: null,
  lifecycleBySegment: new Map(),
  pendingKinds: new Map(),
  requestedRunId: null,
  runSelectionGeneration: 0,
  segmentSelectionGeneration: 0,
  refreshGeneration: 0,
  editorDraftDirty: false,
  refreshDeferred: false,
  saveInFlight: false,
};

const $ = (selector) => document.querySelector(selector);
const vectorNames = ["愉悦", "愤怒", "悲伤", "恐惧", "厌恶", "忧郁", "惊讶", "平静"];
const regenerationPaths = {
  reference: "/regenerate-reference",
  gsv: "/regenerate-gsv",
  both: "/regenerate-both",
};
const ROW_HEIGHT = 96;
const ROW_OVERSCAN = 8;

async function request(path, options = {}) {
  const headers = options.body ? { "content-type": "application/json" } : {};
  const response = await fetch(path, { headers, ...options });
  if (!response.ok) {
    const payload = await response.text();
    throw new Error(sanitizeMessage(payload || `HTTP ${response.status}`));
  }
  return response.json();
}

function sanitizeMessage(message) {
  return String(message)
    .replace(/[A-Za-z]:[\\/][^\s"']+/g, "[本地路径]")
    .replace(/\\\\[^\s"']+/g, "[本地路径]");
}

function setStatus(text, error = false) {
  const element = $("#run-status");
  element.textContent = text;
  element.classList.toggle("error", error);
}

function shortId(value) {
  return value ? String(value).slice(0, 8) : "—";
}

function progressFor(segmentId) {
  return (state.progress?.segments || []).find((item) => item.segment_id === segmentId) || {};
}

function sameNumbers(left, right) {
  return left.length === right.length && left.every((value, index) => Math.abs(value - right[index]) < 0.000001);
}

function sumVector(values) {
  return values.reduce((sum, value) => sum + value, 0);
}

function emotionLabel(segment) {
  const values = segment.current_emotion_vector || [];
  const largest = values.reduce((best, value, index) => value > values[best] ? index : best, 0);
  return values[largest] > 0 ? vectorNames[largest] : "平静";
}

function segmentStatus(segment) {
  const progress = progressFor(segment.segment_id);
  const lifecycle = state.lifecycleBySegment.get(segment.segment_id);
  const referenceState = progress.reference_state || lifecycle?.reference;
  const gsvState = progress.gsv_state || lifecycle?.gsv;
  const jobs = [progress.reference_job_status, progress.gsv_job_status];
  if (jobs.some((job) => ["failed", "cancelled", "interrupted"].includes(job))) {
    return { key: "failed", label: "失败" };
  }
  if (state.pendingKinds.has(segment.segment_id) || jobs.some((job) => ["queued", "running"].includes(job))) {
    return { key: "generating", label: "生成中" };
  }
  if (referenceState === "draft_pending") return { key: "draft_pending", label: "参考参数未应用" };
  if (gsvState === "stale") return { key: "stale", label: "GSV 过期" };
  if (!progress.active_ref_version_id) return { key: "waiting", label: "等待参考" };
  if (!progress.active_gsv_version_id) return { key: "waiting", label: "等待 GSV" };
  return { key: "ready", label: "完成" };
}

function filteredSegments() {
  const query = $("#segment-search").value.trim().toLocaleLowerCase();
  const selectedState = $("#segment-state-filter").value;
  return state.segments.filter((segment) => {
    const status = segmentStatus(segment);
    return (!query || segment.source_text.toLocaleLowerCase().includes(query))
      && (selectedState === "all" || status.key === selectedState);
  });
}

async function loadProfiles() {
  state.profiles = await request("/api/v1/model-profiles");
  const select = $("#model-profile");
  const readyProfiles = state.profiles.filter((item) => item.status === "ready");
  select.replaceChildren(...readyProfiles.map((item) => {
    const option = document.createElement("option");
    option.value = item.profile_id;
    option.textContent = item.active ? `${item.display_name}（当前）` : item.display_name;
    option.selected = item.active;
    return option;
  }));
  select.disabled = readyProfiles.length === 0;
  renderProfiles();
}

function renderProfiles() {
  const list = $("#model-profile-list");
  list.replaceChildren(...state.profiles.map((profile) => {
    const card = document.createElement("article");
    card.className = "model-profile-card";
    const title = document.createElement("strong");
    title.textContent = `${profile.display_name}${profile.active ? "（当前）" : ""}`;
    const detail = document.createElement("span");
    detail.className = "status";
    detail.textContent = `${profile.status}${profile.declared_family ? ` · ${profile.declared_family}` : ""}`;
    card.append(title, detail);
    if (profile.status === "ready" && !profile.active) {
      const activate = document.createElement("button");
      activate.type = "button";
      activate.textContent = "设为当前模型";
      activate.onclick = () => activateProfile(profile.profile_id);
      card.append(activate);
    }
    return card;
  }));
}

async function importModelProfile(event) {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  const body = Object.fromEntries(form);
  if (!body.declared_family) delete body.declared_family;
  try {
    const profile = await request("/api/v1/model-profiles/import", {
      method: "POST", body: JSON.stringify(body),
    });
    formElement.reset();
    await loadProfiles();
    setStatus(`模型档案“${profile.display_name}”已导入；请显式设为当前模型`);
  } catch (error) {
    setStatus(sanitizeMessage(error), true);
  }
}

async function activateProfile(profileId) {
  try {
    await request(`/api/v1/model-profiles/${profileId}/activate`, { method: "POST", body: "{}" });
    await loadProfiles();
    setStatus("当前 GPT-SoVITS 模型已切换；已入队任务不受影响");
  } catch (error) {
    setStatus(sanitizeMessage(error), true);
  }
}

async function loadChapters() {
  state.chapters = await request("/api/v1/chapters");
  const list = $("#chapter-list");
  list.replaceChildren(...state.chapters.map((run) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chapter-row";
    button.dataset.selected = String(run.run_id === state.run?.run_id);
    button.textContent = `${run.status}: ${run.title || run.run_id.slice(0, 8)}`;
    button.onclick = () => selectRun(run.run_id);
    return button;
  }));
}

function isCurrentRunRequest(runId, runToken) {
  return state.requestedRunId === runId && state.runSelectionGeneration === runToken;
}

function isCurrentSegmentRequest(runId, runToken, segmentId, segmentToken) {
  return isCurrentRunRequest(runId, runToken)
    && state.selected?.segment_id === segmentId
    && state.segmentSelectionGeneration === segmentToken;
}

async function selectRun(runId) {
  if (state.saveInFlight) {
    setStatus("草稿正在保存，完成后可切换章节", true);
    return;
  }
  const runToken = ++state.runSelectionGeneration;
  state.requestedRunId = runId;
  ++state.segmentSelectionGeneration;
  ++state.refreshGeneration;
  state.editorDraftDirty = false;
  state.refreshDeferred = false;
  closeEvents();
  state.lifecycleBySegment.clear();
  state.pendingKinds.clear();
  const run = await request(`/api/v1/chapters/${runId}`);
  if (!isCurrentRunRequest(runId, runToken)) return;
  const [progress, segments] = await Promise.all([
    request(`/api/v1/chapters/${runId}/progress`),
    request(`/api/v1/tasks/${run.task_id}/segments`),
  ]);
  if (!isCurrentRunRequest(runId, runToken)) return;
  state.run = run;
  state.progress = progress;
  state.segments = segments;
  state.selected = state.segments[0] || null;
  state.history = null;
  const segmentToken = ++state.segmentSelectionGeneration;
  await loadHistory({ runId, runToken, segmentId: state.selected?.segment_id, segmentToken });
  if (!isCurrentRunRequest(runId, runToken)) return;
  renderRunDetails();
  renderVirtualRows();
  renderEditor();
  await loadChapters();
  if (!isCurrentRunRequest(runId, runToken)) return;
  connectEvents(runId, runToken);
}

async function refreshRun() {
  if (!state.run) return false;
  if (state.editorDraftDirty) {
    state.refreshDeferred = true;
    return false;
  }
  const runId = state.run.run_id;
  const runToken = state.runSelectionGeneration;
  const refreshToken = ++state.refreshGeneration;
  const selectedId = state.selected?.segment_id;
  const segmentToken = state.segmentSelectionGeneration;
  const run = await request(`/api/v1/chapters/${runId}`);
  if (!isCurrentRunRequest(runId, runToken) || refreshToken !== state.refreshGeneration
    || segmentToken !== state.segmentSelectionGeneration) return false;
  const [progress, segments] = await Promise.all([
    request(`/api/v1/chapters/${runId}/progress`),
    request(`/api/v1/tasks/${run.task_id}/segments`),
  ]);
  if (!isCurrentRunRequest(runId, runToken) || refreshToken !== state.refreshGeneration
    || segmentToken !== state.segmentSelectionGeneration) return false;
  state.run = run;
  state.progress = progress;
  state.segments = segments;
  state.selected = state.segments.find((item) => item.segment_id === selectedId) || state.segments[0] || null;
  state.history = null;
  const historySegmentToken = state.selected?.segment_id === selectedId
    ? segmentToken
    : ++state.segmentSelectionGeneration;
  await loadHistory({ runId, runToken, segmentId: state.selected?.segment_id, segmentToken: historySegmentToken });
  if (!isCurrentRunRequest(runId, runToken) || refreshToken !== state.refreshGeneration) return false;
  renderRunDetails();
  renderVirtualRows();
  renderEditor();
  await loadChapters();
  return true;
}

async function loadHistory({
  runId = state.run?.run_id,
  runToken = state.runSelectionGeneration,
  segmentId = state.selected?.segment_id,
  segmentToken = state.segmentSelectionGeneration,
} = {}) {
  if (!segmentId) {
    if (isCurrentRunRequest(runId, runToken) && state.segmentSelectionGeneration === segmentToken) {
      state.history = null;
    }
    return true;
  }
  const history = await request(`/api/v1/segments/${segmentId}/history`);
  if (!isCurrentSegmentRequest(runId, runToken, segmentId, segmentToken)) return false;
  state.history = history;
  state.lifecycleBySegment.set(history.segment_id, history.state);
  return true;
}

async function selectSegment(segment) {
  if (segment.segment_id === state.selected?.segment_id) return;
  if (state.saveInFlight) {
    setStatus("草稿正在保存，完成后可切换分块", true);
    return;
  }
  if (state.editorDraftDirty) {
    setStatus("当前分块的草稿尚未保存；请先保存或恢复草稿后再切换", true);
    return;
  }
  const runId = state.run?.run_id;
  const runToken = state.runSelectionGeneration;
  const segmentToken = ++state.segmentSelectionGeneration;
  state.selected = segment;
  state.history = null;
  renderVirtualRows();
  renderEditor();
  await loadHistory({ runId, runToken, segmentId: segment.segment_id, segmentToken });
  if (!isCurrentSegmentRequest(runId, runToken, segment.segment_id, segmentToken)) return;
  renderEditor();
}

function renderRunDetails() {
  const title = $("#chapter-title");
  const summary = $("#chapter-summary");
  const audio = $("#chapter-audio");
  const compose = $("#compose-chapter");
  if (!state.run) {
    title.textContent = "选择或创建一个章节任务";
    summary.textContent = "尚未选择章节";
    audio.replaceChildren();
    compose.disabled = true;
    return;
  }
  title.textContent = state.run.title;
  const counts = { ready: 0, generating: 0, failed: 0, stale: 0, draft_pending: 0, waiting: 0 };
  state.segments.forEach((segment) => { counts[segmentStatus(segment).key] += 1; });
  renderChapterSummary(counts);
  audio.replaceChildren();
  if (state.run.final_audio_url) {
    const label = document.createElement("label");
    label.textContent = "当前整篇成品";
    const player = document.createElement("audio");
    player.controls = true;
    player.preload = "none";
    player.src = state.run.final_audio_url;
    label.append(player);
    audio.append(label);
  } else {
    audio.textContent = "尚无整篇成品";
  }
  compose.disabled = state.segments.length === 0 || counts.generating > 0 || counts.waiting > 0;
  compose.title = compose.disabled ? "所有分块需要有可用的当前 GSV 音频后才能拼接" : "按当前 GSV 版本和停顿重新拼接";
}

function renderChapterSummary(counts) {
  const summary = $("#chapter-summary");
  const cards = [
    ["分块", state.segments.length],
    ["完成", counts.ready],
    ["生成中", counts.generating],
    ["需处理", counts.failed + counts.stale + counts.draft_pending + counts.waiting],
  ];
  summary.replaceChildren(...cards.map(([label, value]) => {
    const item = document.createElement("div");
    const number = document.createElement("strong");
    number.textContent = String(value);
    const caption = document.createElement("span");
    caption.textContent = String(label);
    item.append(number, caption);
    return item;
  }));
}

function renderVirtualRows() {
  const scroll = $("#segment-scroll");
  const list = $("#segment-list");
  const rows = filteredSegments();
  $("#segment-count").textContent = `${rows.length} / ${state.segments.length} 个`;
  list.style.height = `${rows.length * ROW_HEIGHT}px`;
  list.replaceChildren();
  const viewportHeight = scroll.clientHeight || 560;
  const start = Math.max(0, Math.floor(scroll.scrollTop / ROW_HEIGHT) - ROW_OVERSCAN);
  const end = Math.min(rows.length, Math.ceil((scroll.scrollTop + viewportHeight) / ROW_HEIGHT) + ROW_OVERSCAN);
  for (let index = start; index < end; index += 1) {
    const segment = rows[index];
    const progress = progressFor(segment.segment_id);
    const status = segmentStatus(segment);
    const row = $("#segment-row-template").content.firstElementChild.cloneNode(true);
    row.style.transform = `translateY(${index * ROW_HEIGHT}px)`;
    row.dataset.selected = String(segment.segment_id === state.selected?.segment_id);
    row.dataset.state = status.key;
    row.setAttribute("aria-current", segment.segment_id === state.selected?.segment_id ? "true" : "false");
    const title = document.createElement("strong");
    title.textContent = `${segment.ordinal + 1}. ${progress.source_summary || segment.source_text}`;
    const meta = document.createElement("span");
    meta.className = "row-meta";
    meta.textContent = `${emotionLabel(segment)} · 参考 ${shortId(progress.active_ref_version_id)} · GSV ${shortId(progress.active_gsv_version_id)}`;
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = status.label;
    row.append(title, meta, badge);
    row.onclick = () => selectSegment(segment);
    list.append(row);
  }
}

function renderEditor() {
  const segment = state.selected;
  const root = $("#segment-editor");
  if (!segment) {
    root.innerHTML = "<p>从左侧选择分块以编辑草稿。保存不会触发推理。</p>";
    return;
  }
  const progress = progressFor(segment.segment_id);
  const lifecycle = state.history?.state || {
    reference: progress.reference_state || "missing",
    gsv: progress.gsv_state || "missing",
  };
  const frozenReference = progress.active_ref_version_id ? shortId(progress.active_ref_version_id) : "无可用参考音频";
  root.innerHTML = `<form id="segment-form"><div class="editor-heading"><div><p class="eyebrow">分块 ${segment.ordinal + 1}</p><h2>编辑配音草稿</h2></div><div class="state-pills"><span class="badge">参考：${escapeHtml(lifecycle.reference)}</span><span class="badge">GSV：${escapeHtml(lifecycle.gsv)}</span></div></div>
    <label>原文 <textarea readonly>${escapeHtml(segment.source_text)}</textarea></label>
    <label>合成文本 <textarea name="synthesis_text">${escapeHtml(segment.synthesis_text)}</textarea></label>
    <label>中文参考文本 <textarea name="ref_text_cn">${escapeHtml(segment.ref_text_cn)}</textarea></label>
    <div class="parameter-grid"><label>速度 <input name="speed_factor" type="number" min="0.5" max="2" step="0.05" value="${segment.speed_factor}"></label>
    <label>停顿(ms) <input name="pause_after_ms" type="number" min="0" max="30000" value="${segment.pause_after_ms}"></label>
    <label>随机种子 <input name="seed" type="number" value="${segment.seed}"></label></div>
    <section class="emotion-panel" aria-label="情绪向量"><div class="section-heading"><div><p class="eyebrow">LLM 情绪基准</p><h3>八维情绪向量</h3></div><output id="vector-total"></output></div>
      <p class="help">默认值来自 LLM。可在此微调；保存草稿不会调用模型。</p>
      <div class="vector-grid">${vectorNames.map((name, index) => `<label class="emotion-control"><span>${name}</span><input data-vector-slider="${index}" type="range" min="0" max="1" step="0.01" value="${segment.current_emotion_vector[index]}"><input data-vector-number="${index}" type="number" min="0" max="1" step="0.01" value="${segment.current_emotion_vector[index]}"></label>`).join("")}</div>
      <div class="button-row"><button type="button" id="restore-vector">恢复 LLM 值</button><button type="button" id="normalize-vector">按比例归一化到 0.80</button></div>
    </section>
    <div class="button-row"><button id="save-segment">保存草稿</button><span class="help">保存后仅更新草稿和状态。</span></div>
    <section class="audio-grid" aria-label="当前音频">${audioControl("当前参考", progress.active_ref_version_id)}${audioControl("当前 GSV", progress.active_gsv_version_id)}</section>
    <section class="command-grid" aria-label="局部重生成"><h3>局部重新生成</h3><p id="command-hint" class="help">仅重新生成 GSV 时将固定使用当前参考版本：${frozenReference}。</p><label>重新生成参考所用音色路径 <input id="regeneration-base-voice" placeholder="仅在参考生成时发送给本地控制面"></label>
      <div class="button-row"><button type="button" id="regenerate-reference">重新生成参考音频</button>
      <button type="button" id="regenerate-gsv" ${progress.active_ref_version_id ? "" : "disabled"}>重新生成 GSV</button>
      <button type="button" id="regenerate-both">重新生成两者</button></div></section>
    <section id="version-history" aria-label="版本历史"></section></form>`;
  bindVectorControls();
  $("#restore-vector").onclick = () => restoreLlmVector(segment);
  $("#normalize-vector").onclick = normalizeEmotionVector;
  $("#segment-form").onsubmit = (event) => saveSegmentDraft(event, segment);
  $("#regenerate-reference").onclick = () => regenerate(segment, "reference");
  $("#regenerate-gsv").onclick = () => regenerate(segment, "gsv");
  $("#regenerate-both").onclick = () => regenerate(segment, "both");
  root.querySelectorAll("[name]").forEach((input) => {
    input.addEventListener("input", markEditorDirty);
  });
  refreshVectorTotal();
  renderHistory();
}

function markEditorDirty() {
  if (!state.selected) return;
  state.editorDraftDirty = true;
  const saveHint = $("#save-segment")?.nextElementSibling;
  if (saveHint) saveHint.textContent = "草稿有未保存修改；保存不会触发推理。";
}

function setEditorSaving(saving) {
  const form = $("#segment-form");
  form?.querySelectorAll("textarea:not([readonly]), input:not([readonly]), button").forEach((input) => {
    input.disabled = saving;
  });
}

function bindVectorControls() {
  $("#segment-editor").querySelectorAll("[data-vector-slider]").forEach((slider) => {
    slider.oninput = () => {
      $(`[data-vector-number="${slider.dataset.vectorSlider}"]`).value = slider.value;
      refreshVectorTotal();
      markEditorDirty();
    };
  });
  $("#segment-editor").querySelectorAll("[data-vector-number]").forEach((input) => {
    input.oninput = () => {
      const value = Math.min(1, Math.max(0, Number(input.value) || 0));
      $(`[data-vector-slider="${input.dataset.vectorNumber}"]`).value = value;
      refreshVectorTotal();
      markEditorDirty();
    };
  });
}

function vectorValues() {
  return [...$("#segment-editor").querySelectorAll("[data-vector-slider]")].map((input) => Number(input.value));
}

function setVectorValues(values) {
  values.forEach((value, index) => {
    $(`[data-vector-slider="${index}"]`).value = value;
    $(`[data-vector-number="${index}"]`).value = value.toFixed(2);
  });
  refreshVectorTotal();
}

function refreshVectorTotal() {
  const values = vectorValues();
  const total = sumVector(values);
  const output = $("#vector-total");
  const invalid = total > 0.8 + 0.000001;
  output.textContent = `向量总和: ${total.toFixed(2)}${invalid ? "（必须不大于 0.80）" : " / 0.80"}`;
  output.classList.toggle("error", invalid);
  $("#save-segment").disabled = invalid;
  $("#regenerate-reference").disabled = invalid;
  $("#regenerate-both").disabled = invalid;
}

function restoreLlmVector(segment) {
  setVectorValues(segment.llm_emotion_vector);
  markEditorDirty();
  setStatus("已恢复 LLM 基准向量；点击保存草稿后才会写入");
}

function normalizeEmotionVector() {
  const values = vectorValues();
  const total = sumVector(values);
  if (total === 0) {
    setStatus("向量总和为 0，无需归一化");
    return;
  }
  setVectorValues(values.map((value) => Math.min(1, value * 0.8 / total)));
  markEditorDirty();
  setStatus("已按比例归一化到 0.80；点击保存草稿后才会写入");
}

function draftPayload(segment, form) {
  const body = {
    expected_ref_draft_revision: segment.ref_draft_revision,
    expected_gsv_draft_revision: segment.gsv_draft_revision,
  };
  const currentVector = vectorValues();
  const refText = String(form.get("ref_text_cn")).trim();
  const synthesisText = String(form.get("synthesis_text")).trim();
  const speed = Number(form.get("speed_factor"));
  const pause = Number(form.get("pause_after_ms"));
  const seed = Number(form.get("seed"));
  if (refText !== segment.ref_text_cn) body.ref_text_cn = refText;
  if (!sameNumbers(currentVector, segment.current_emotion_vector)) body.current_emotion_vector = currentVector;
  if (synthesisText !== segment.synthesis_text) body.synthesis_text = synthesisText;
  if (speed !== segment.speed_factor) body.speed_factor = speed;
  if (pause !== segment.pause_after_ms) body.pause_after_ms = pause;
  if (seed !== segment.seed) body.seed = seed;
  return Object.keys(body).length === 2 ? null : body;
}

async function saveSegmentDraft(event, segment) {
  event.preventDefault();
  const body = draftPayload(segment, new FormData(event.currentTarget));
  if (!body) {
    state.editorDraftDirty = false;
    if (state.refreshDeferred) {
      state.refreshDeferred = false;
      await refreshRun();
    }
    setStatus("草稿没有变化");
    return;
  }
  state.saveInFlight = true;
  setEditorSaving(true);
  try {
    const updated = await request(`/api/v1/segments/${segment.segment_id}/inputs`, { method: "PATCH", body: JSON.stringify(body) });
    state.segments = state.segments.map((item) => item.segment_id === updated.segment_id ? updated : item);
    state.selected = updated;
    state.editorDraftDirty = false;
    state.refreshDeferred = false;
    await refreshRun();
    setStatus("草稿已保存；未触发推理");
  } catch (error) {
    setStatus(sanitizeMessage(error), true);
  } finally {
    state.saveInFlight = false;
    setEditorSaving(false);
  }
}

function canReplaceEditorDraft() {
  if (state.saveInFlight) {
    setStatus("草稿正在保存，完成后可执行此操作", true);
    return false;
  }
  if (!state.editorDraftDirty) return true;
  setStatus("当前分块的草稿尚未保存；请先保存或恢复草稿后再执行此操作", true);
  return false;
}

async function regenerate(segment, kind) {
  if (!canReplaceEditorDraft()) return;
  const profile = $("#model-profile").value || null;
  const baseVoice = $("#regeneration-base-voice").value.trim();
  const path = `/api/v1/segments/${segment.segment_id}${regenerationPaths[kind]}`;
  const body = { request_id: crypto.randomUUID() };
  if (kind !== "reference") body.model_profile_id = profile;
  if (kind !== "gsv") body.base_voice_path = baseVoice;
  if (kind !== "gsv" && !baseVoice) {
    setStatus("请先填写重新生成参考所用音色路径", true);
    return;
  }
  try {
    const accepted = await request(path, { method: "POST", body: JSON.stringify(body) });
    state.pendingKinds.set(segment.segment_id, kind);
    renderRunDetails();
    renderVirtualRows();
    setStatus(`${kind === "reference" ? "参考音频" : kind === "gsv" ? "GSV" : "两阶段生成"}已排队：${shortId(accepted.job_id)}`);
  } catch (error) {
    setStatus(sanitizeMessage(error), true);
  }
}

function renderHistory() {
  const root = $("#version-history");
  if (!state.history) {
    root.textContent = "版本历史加载中";
    return;
  }
  root.innerHTML = `<div class="editor-heading"><div><p class="eyebrow">不可变音频</p><h3>版本历史</h3></div><span class="status">切换需要版本并发校验</span></div><div class="history-list">${historySection("参考", state.history.reference)}${historySection("GSV", state.history.gsv)}</div>`;
  root.querySelectorAll("[data-activate]").forEach((button) => { button.onclick = () => activateVersion(button.dataset.activate); });
  root.querySelectorAll("[data-restore]").forEach((button) => { button.onclick = () => restoreVersion(button.dataset.restore); });
}

function historySection(label, versions) {
  const rows = versions.map((version) => `<article class="history-item"><strong>${label} ${shortId(version.version_id)}${version.active ? "（当前）" : ""}</strong>
    <span class="status">${version.ref_version_id ? `使用参考 ${shortId(version.ref_version_id)}` : ""}</span>
    <audio controls preload="none" src="${escapeHtml(version.audio_url)}"></audio>
    <details><summary>查看冻结参数</summary><pre>${escapeHtml(JSON.stringify(version.input_snapshot, null, 2))}</pre></details>
    <div class="button-row"><button type="button" data-activate="${escapeHtml(version.version_id)}">设为当前</button>
    <button type="button" data-restore="${escapeHtml(version.version_id)}">恢复此版本参数</button></div></article>`).join("");
  return `<section><h4>${label}</h4>${rows || "暂无版本"}</section>`;
}

async function activateVersion(versionId) {
  if (!canReplaceEditorDraft()) return;
  try {
    await request(`/api/v1/segments/${state.selected.segment_id}/versions/${versionId}/activate`, {
      method: "POST", body: JSON.stringify({ expected_selection_revision: state.history.selection_revision }),
    });
    await refreshRun();
    setStatus("历史版本已设为当前；需要时请显式重新拼接整篇");
  } catch (error) {
    setStatus(sanitizeMessage(error), true);
  }
}

async function restoreVersion(versionId) {
  if (!canReplaceEditorDraft()) return;
  const segment = state.selected;
  try {
    await request(`/api/v1/segments/${segment.segment_id}/versions/${versionId}/restore-inputs`, {
      method: "POST", body: JSON.stringify({ expected_ref_draft_revision: segment.ref_draft_revision, expected_gsv_draft_revision: segment.gsv_draft_revision }),
    });
    await refreshRun();
    setStatus("已恢复历史版本参数；未触发推理");
  } catch (error) {
    setStatus(sanitizeMessage(error), true);
  }
}

async function composeChapter() {
  if (!state.run) return;
  if (!canReplaceEditorDraft()) return;
  try {
    await request(`/api/v1/chapters/${state.run.run_id}/compose`, { method: "POST", body: "{}" });
    await refreshRun();
    setStatus("整篇已按当前 GSV 选择重新拼接");
  } catch (error) {
    setStatus(sanitizeMessage(error), true);
  }
}

function closeEvents() {
  state.events?.close();
  state.events = null;
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  state.refreshTimer = null;
}

function connectEvents(runId, runToken) {
  const events = new EventSource(`/api/v1/chapters/${runId}/events`);
  state.events = events;
  events.addEventListener("open", () => {
    if (!isCurrentRunRequest(runId, runToken)) return;
    if (state.refreshTimer) clearInterval(state.refreshTimer);
    state.refreshTimer = null;
  });
  events.addEventListener("chapter_progress", async (event) => {
    if (!isCurrentRunRequest(runId, runToken)) return;
    state.progress = JSON.parse(event.data);
    state.pendingKinds.clear();
    if (state.editorDraftDirty) {
      state.refreshDeferred = true;
      setStatus("后台任务状态已更新；当前草稿尚未保存，列表将在保存或恢复草稿后刷新");
      return;
    }
    await refreshRun();
    if (!isCurrentRunRequest(runId, runToken)) return;
    setStatus(`任务 ${state.progress.status}`);
  });
  events.onerror = () => {
    if (state.refreshTimer || !isCurrentRunRequest(runId, runToken)) return;
    state.refreshTimer = setInterval(
      () => refreshRun().catch((error) => setStatus(sanitizeMessage(error), true)),
      2000,
    );
  };
}

function audioControl(label, versionId) {
  return versionId ? `<label>${label}<audio controls preload="none" src="/api/v1/versions/${escapeHtml(versionId)}/audio"></audio><span class="status">版本 ${shortId(versionId)}</span></label>` : `<p class="empty-audio">${label}尚未生成</p>`;
}

function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = value ?? "";
  return node.innerHTML;
}

$("#chapter-form").onsubmit = async (event) => {
  event.preventDefault();
  const data = new FormData(event.currentTarget);
  if (!data.get("model_profile_id")) {
    setStatus("请先导入并选择一个可用的 GPT-SoVITS 模型档案", true);
    return;
  }
  try {
    const result = await request("/api/v1/chapters", { method: "POST", body: JSON.stringify({ ...Object.fromEntries(data), request_id: crypto.randomUUID() }) });
    await loadChapters();
    await selectRun(result.run_id);
    setStatus("整篇任务已创建，正在按分块顺序生成");
  } catch (error) {
    setStatus(sanitizeMessage(error), true);
  }
};

$("#compose-chapter").onclick = composeChapter;
$("#model-profile-form").onsubmit = importModelProfile;
$("#segment-search").oninput = () => { $("#segment-scroll").scrollTop = 0; renderVirtualRows(); };
$("#segment-state-filter").onchange = () => { $("#segment-scroll").scrollTop = 0; renderVirtualRows(); };
$("#segment-scroll").onscroll = renderVirtualRows;

async function initialize() {
  try {
    await Promise.all([loadProfiles(), loadChapters()]);
    if (state.chapters[0]) await selectRun(state.chapters[0].run_id);
    else renderRunDetails();
  } catch (error) {
    setStatus(sanitizeMessage(error), true);
  }
}

initialize();
