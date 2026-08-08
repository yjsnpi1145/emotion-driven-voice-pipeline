const state = {
  chapters: [], run: null, progress: null, segments: [], selected: null, history: null, events: null,
};
const $ = (selector) => document.querySelector(selector);
const vectorNames = ["愉悦", "愤怒", "悲伤", "恐惧", "厌恶", "忧郁", "惊讶", "平静"];
const regenerationPaths = {
  reference: "/regenerate-reference",
  gsv: "/regenerate-gsv",
  both: "/regenerate-both",
};

async function request(path, options = {}) {
  const response = await fetch(path, { headers: { "content-type": "application/json" }, ...options });
  if (!response.ok) throw new Error((await response.text()) || `HTTP ${response.status}`);
  return response.json();
}

function setStatus(text, error = false) {
  const element = $("#run-status");
  element.textContent = text;
  element.classList.toggle("error", error);
}

async function loadProfiles() {
  const profiles = await request("/api/v1/model-profiles");
  const select = $("#model-profile");
  select.replaceChildren(...profiles.filter((item) => item.status === "active").map((item) => {
    const option = document.createElement("option");
    option.value = item.profile_id;
    option.textContent = item.display_name;
    return option;
  }));
}

async function loadChapters() {
  state.chapters = await request("/api/v1/chapters");
  const list = $("#chapter-list");
  list.replaceChildren(...state.chapters.map((run) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${run.status}: ${run.run_id.slice(0, 8)}`;
    button.onclick = () => selectRun(run.run_id);
    return button;
  }));
}

async function selectRun(runId) {
  state.events?.close();
  state.run = await request(`/api/v1/chapters/${runId}`);
  state.progress = await request(`/api/v1/chapters/${runId}/progress`);
  state.segments = await request(`/api/v1/tasks/${state.run.task_id}/segments`);
  state.selected = state.segments[0] || null;
  await loadHistory();
  $("#compose-chapter").disabled = state.run.status !== "succeeded";
  setStatus(`任务 ${state.run.status}`);
  renderRows();
  renderEditor();
  connectEvents(runId);
}

async function refreshRun() {
  if (!state.run) return;
  const selectedId = state.selected?.segment_id;
  state.run = await request(`/api/v1/chapters/${state.run.run_id}`);
  state.progress = await request(`/api/v1/chapters/${state.run.run_id}/progress`);
  state.segments = await request(`/api/v1/tasks/${state.run.task_id}/segments`);
  state.selected = state.segments.find((item) => item.segment_id === selectedId) || state.segments[0] || null;
  await loadHistory();
  $("#compose-chapter").disabled = state.run.status !== "succeeded";
  renderRows();
  renderEditor();
}

async function loadHistory() {
  state.history = state.selected ? await request(`/api/v1/segments/${state.selected.segment_id}/history`) : null;
}

async function selectSegment(segment) {
  state.selected = segment;
  await loadHistory();
  renderRows();
  renderEditor();
}

function filteredSegments() {
  const query = $("#segment-search").value.trim().toLowerCase();
  return state.segments.filter((item) => !query || item.source_text.toLowerCase().includes(query));
}

function renderRows() {
  const progress = new Map((state.progress?.segments || []).map((item) => [item.segment_id, item]));
  $("#segment-list").replaceChildren(...filteredSegments().map((segment) => {
    const row = $("#segment-row-template").content.firstElementChild.cloneNode(true);
    const info = progress.get(segment.segment_id) || {};
    const stateLabel = segment.segment_id === state.selected?.segment_id && state.history
      ? `${state.history.state.reference}/${state.history.state.gsv}` : info.gsv_job_status || "草稿";
    row.dataset.selected = String(segment.segment_id === state.selected?.segment_id);
    row.innerHTML = `<strong>${segment.ordinal + 1}. ${escapeHtml(segment.source_text)}</strong><span class="status">${stateLabel}</span>`;
    row.onclick = () => selectSegment(segment);
    return row;
  }));
}

function renderEditor() {
  const segment = state.selected;
  const root = $("#segment-editor");
  if (!segment) { root.textContent = "暂无分块"; return; }
  const progress = (state.progress?.segments || []).find((item) => item.segment_id === segment.segment_id) || {};
  const lifecycle = state.history?.state || { reference: "missing", gsv: "missing" };
  root.innerHTML = `<form id="segment-form"><h2>分块 ${segment.ordinal + 1}</h2>
    <p class="status">参考：${lifecycle.reference}　GSV：${lifecycle.gsv}</p>
    <label>原文 <textarea readonly>${escapeHtml(segment.source_text)}</textarea></label>
    <label>合成文本 <textarea name="synthesis_text">${escapeHtml(segment.synthesis_text)}</textarea></label>
    <label>中文参考文本 <textarea name="ref_text_cn">${escapeHtml(segment.ref_text_cn)}</textarea></label>
    <label>速度 <input name="speed_factor" type="number" min="0.5" max="2" step="0.05" value="${segment.speed_factor}"></label>
    <label>停顿(ms) <input name="pause_after_ms" type="number" min="0" max="30000" value="${segment.pause_after_ms}"></label>
    <label>随机种子 <input name="seed" type="number" value="${segment.seed}"></label>
    <div class="vector-grid">${vectorNames.map((name, index) => `<label>${name}<input data-vector="${index}" type="number" min="0" max="1" step="0.01" value="${segment.current_emotion_vector[index]}"></label>`).join("")}</div>
    <output id="vector-total">向量总和: ${sumVector(segment.current_emotion_vector).toFixed(2)}</output>
    <button type="button" id="restore-vector">恢复 LLM 值</button><button id="save-segment">保存草稿</button>
    <div class="audio-grid">${audioControl("当前参考", progress.active_ref_version_id)}${audioControl("当前 GSV", progress.active_gsv_version_id)}</div>
    <div class="command-grid"><label>重新生成参考所用音色路径 <input id="regeneration-base-voice" placeholder="仅在生成 reference 时发送给本地控制面"></label>
      <button type="button" id="regenerate-reference">重新生成参考音频</button>
      <button type="button" id="regenerate-gsv">重新生成 GSV</button>
      <button type="button" id="regenerate-both">重新生成两者</button></div>
    <section id="version-history" aria-label="版本历史"></section></form>`;
  root.querySelectorAll("[data-vector]").forEach((input) => { input.oninput = refreshVectorTotal; });
  $("#restore-vector").onclick = () => restoreLlmVector(segment);
  $("#segment-form").onsubmit = (event) => saveSegmentDraft(event, segment);
  $("#regenerate-reference").onclick = () => regenerate(segment, "reference");
  $("#regenerate-gsv").onclick = () => regenerate(segment, "gsv");
  $("#regenerate-both").onclick = () => regenerate(segment, "both");
  refreshVectorTotal();
  renderHistory();
}

function refreshVectorTotal() {
  const values = [...document.querySelectorAll("[data-vector]")].map((input) => Number(input.value));
  const total = sumVector(values);
  const output = $("#vector-total");
  output.textContent = `向量总和: ${total.toFixed(2)}${total > 0.8 ? "（必须不大于 0.8）" : ""}`;
  $("#save-segment").disabled = total > 0.8;
  $("#regenerate-reference").disabled = total > 0.8;
  $("#regenerate-both").disabled = total > 0.8;
}

function restoreLlmVector(segment) {
  $("#segment-editor").querySelectorAll("[data-vector]").forEach((input, index) => {
    input.value = segment.llm_emotion_vector[index];
  });
  refreshVectorTotal();
}

async function saveSegmentDraft(event, segment) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const body = {
    expected_ref_draft_revision: segment.ref_draft_revision,
    expected_gsv_draft_revision: segment.gsv_draft_revision,
    synthesis_text: form.get("synthesis_text"), ref_text_cn: form.get("ref_text_cn"),
    speed_factor: Number(form.get("speed_factor")), pause_after_ms: Number(form.get("pause_after_ms")),
    seed: Number(form.get("seed")), current_emotion_vector: [...document.querySelectorAll("[data-vector]")].map((item) => Number(item.value)),
  };
  try {
    const updated = await request(`/api/v1/segments/${segment.segment_id}/inputs`, { method: "PATCH", body: JSON.stringify(body) });
    state.segments = state.segments.map((item) => item.segment_id === updated.segment_id ? updated : item);
    state.selected = updated;
    await loadHistory();
    renderRows(); renderEditor(); setStatus("草稿已保存；未触发推理");
  } catch (error) { setStatus(String(error), true); }
}

async function regenerate(segment, kind) {
  const profile = $("#model-profile").value || null;
  const baseVoice = $("#regeneration-base-voice").value.trim();
  const path = `/api/v1/segments/${segment.segment_id}${regenerationPaths[kind]}`;
  const body = { request_id: crypto.randomUUID(), model_profile_id: profile };
  if (kind !== "gsv") body.base_voice_path = baseVoice;
  if (kind !== "gsv" && !baseVoice) { setStatus("请先填写重新生成参考所用音色路径", true); return; }
  try {
    const accepted = await request(path, { method: "POST", body: JSON.stringify(body) });
    setStatus(`${kind} 已排队：${accepted.job_id.slice(0, 8)}`);
  } catch (error) { setStatus(String(error), true); }
}

function renderHistory() {
  const root = $("#version-history");
  if (!state.history) { root.textContent = "版本历史加载中"; return; }
  root.innerHTML = `<h3>版本历史</h3><div class="history-list">${historySection("参考", state.history.reference)}${historySection("GSV", state.history.gsv)}</div>`;
  root.querySelectorAll("[data-activate]").forEach((button) => { button.onclick = () => activateVersion(button.dataset.activate); });
  root.querySelectorAll("[data-restore]").forEach((button) => { button.onclick = () => restoreVersion(button.dataset.restore); });
}

function historySection(label, versions) {
  const rows = versions.map((version) => `<article class="history-item"><strong>${label} ${version.version_id.slice(0, 8)}${version.active ? "（当前）" : ""}</strong>
    <span class="status">${version.ref_version_id ? `使用参考 ${version.ref_version_id.slice(0, 8)}` : ""}</span>
    <audio controls preload="none" src="${version.audio_url}"></audio>
    <button type="button" data-activate="${version.version_id}">设为当前</button>
    <button type="button" data-restore="${version.version_id}">恢复此版本参数</button></article>`).join("");
  return `<section><h4>${label}</h4>${rows || "暂无版本"}</section>`;
}

async function activateVersion(versionId) {
  try {
    await request(`/api/v1/segments/${state.selected.segment_id}/versions/${versionId}/activate`, {
      method: "POST", body: JSON.stringify({ expected_selection_revision: state.history.selection_revision }),
    });
    await refreshRun(); setStatus("历史版本已设为当前；需要时请显式重新拼接整篇");
  } catch (error) { setStatus(String(error), true); }
}

async function restoreVersion(versionId) {
  const segment = state.selected;
  try {
    await request(`/api/v1/segments/${segment.segment_id}/versions/${versionId}/restore-inputs`, {
      method: "POST", body: JSON.stringify({ expected_ref_draft_revision: segment.ref_draft_revision, expected_gsv_draft_revision: segment.gsv_draft_revision }),
    });
    await refreshRun(); setStatus("已恢复历史版本参数；未触发推理");
  } catch (error) { setStatus(String(error), true); }
}

async function composeChapter() {
  if (!state.run) return;
  try {
    await request(`/api/v1/chapters/${state.run.run_id}/compose`, { method: "POST", body: "{}" });
    await refreshRun(); setStatus("整篇已按当前 GSV 选择重新拼接");
  } catch (error) { setStatus(String(error), true); }
}

function connectEvents(runId) {
  const events = new EventSource(`/api/v1/chapters/${runId}/events`);
  state.events = events;
  events.addEventListener("chapter_progress", async (event) => {
    if (state.run?.run_id !== runId) return;
    state.progress = JSON.parse(event.data);
    await refreshRun();
    setStatus(`任务 ${state.progress.status}`);
  });
  events.onerror = () => setTimeout(() => state.run?.run_id === runId && refreshRun(), 2000);
}

function audioControl(label, versionId) {
  return versionId ? `<label>${label}<audio controls preload="none" src="/api/v1/versions/${versionId}/audio"></audio></label>` : "";
}
function sumVector(values) { return values.reduce((sum, value) => sum + value, 0); }
function escapeHtml(value) { const node = document.createElement("span"); node.textContent = value; return node.innerHTML; }

$("#chapter-form").onsubmit = async (event) => {
  event.preventDefault();
  const data = new FormData(event.currentTarget);
  try {
    const result = await request("/api/v1/chapters", { method: "POST", body: JSON.stringify({ ...Object.fromEntries(data), request_id: crypto.randomUUID() }) });
    await loadChapters(); await selectRun(result.run_id);
  } catch (error) { setStatus(String(error), true); }
};
$("#compose-chapter").onclick = composeChapter;
$("#segment-search").oninput = renderRows;
Promise.all([loadProfiles(), loadChapters()]).catch((error) => setStatus(String(error), true));
