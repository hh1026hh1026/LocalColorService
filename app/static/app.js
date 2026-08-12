const API_BASE = "";
const ACTIVE_JOB_STORAGE_KEY = "local-color-service.active-job.v1";
const JOB_POLL_INTERVAL_MS = 800;
const JOB_RETRY_MAX_DELAY_MS = 10_000;

const state = {
  sceneJobId: null,
  sceneResult: null,
  projectJobId: null,
  projectResult: null,
  candidateJobId: null,
  candidateScope: "__all__",
  adaintLutPath: null,
  comparisonItems: [],
  comparisonMode: "wipe",
  comparisonVideos: [],
  comparisonDuration: 0,
  logsTimer: null,
  taskBoardTimer: null,
  looks: [],
  selectedShotId: null,
};

const byId = id => document.getElementById(id);
const sourcePath = () => byId("filePathInput").value.trim();
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function rememberActiveJob(jobId, label) {
  try {
    localStorage.setItem(ACTIVE_JOB_STORAGE_KEY, JSON.stringify({
      jobId, label, startedAt: new Date().toISOString(),
    }));
  } catch (error) {
    console.warn("无法保存进行中的任务", error);
  }
}

function activeJob() {
  try {
    const value = JSON.parse(localStorage.getItem(ACTIVE_JOB_STORAGE_KEY) || "null");
    return value?.jobId && value?.label ? value : null;
  } catch {
    localStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
    return null;
  }
}

function forgetActiveJob(jobId) {
  const saved = activeJob();
  if (!jobId || saved?.jobId === jobId) localStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
}

function showLoader(title, detail = "后台任务正在运行，请勿关闭页面") {
  byId("loaderText").textContent = title;
  byId("loaderDetail").textContent = detail;
  byId("loaderOverlay").hidden = false;
}

function hideLoader() { byId("loaderOverlay").hidden = true; }

function toast(message, error = false) {
  const box = byId("toast");
  box.textContent = message;
  box.className = `toast${error ? " error" : ""}`;
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, 3500);
}

function setBanner(message, kind = "") {
  byId("qcText").textContent = message;
  byId("qcBanner").className = `qc-banner ${kind}`.trim();
}

function showResult(title, data) {
  const summary = { ...data };
  if (data?.scene_analysis) {
    const analysis = data.scene_analysis;
    summary.scene_analysis = {
      detector: analysis.detector, scene_count: analysis.scenes?.length || 0,
      duration: analysis.media_info?.duration, resolution: `${analysis.media_info?.width || 0}x${analysis.media_info?.height || 0}`,
      analysis_source_path: analysis.analysis_source_path, acceleration: analysis.acceleration,
      thumbnails: (analysis.scenes || []).filter(item => item.thumbnail_path).length,
    };
  }
  if (data?.project_recipe) {
    const project = data.project_recipe;
    summary.project_recipe = {
      revision: project.revision, status: project.status, workflow: project.workflow,
      project_look: project.project_look, shot_count: project.shots?.length || 0,
      scene_group_count: project.scene_groups?.length || 0,
    };
  }
  if (summary.shot_luts?.length > 12) summary.shot_luts = `${summary.shot_luts.length} 个镜头 LUT（完整路径已写入任务结果）`;
  byId("resultConsole").textContent = `${title}\n\n${JSON.stringify(summary, null, 2)}`;
}

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, options);
  let data;
  try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) {
    const error = new Error(data.detail || data.error_message || `请求失败（${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return data;
}

async function cancelJob(jobId) {
  try {
    await api(`/v1/jobs/${jobId}`, { method: "DELETE" });
    toast(`已请求取消任务 ${jobId}`);
    return true;
  } catch (error) {
    toast(`取消失败：${error.message}`, true);
    return false;
  }
}

function taskSummary(jobs) {
  const count = status => jobs.filter(job => job.status === status).length;
  return `执行中 ${count("processing")} · 正在取消 ${count("cancelling")} · 排队 ${count("pending")} · 已完成 ${count("completed")} · 失败 ${count("failed")} · 已取消 ${count("cancelled")}`;
}

function taskDetail(job) {
  if (job.status === "pending") return `排队中 · ${job.lane || "-"} 通道 · 第 ${job.queue_position || "?"} 位`;
  if (job.status === "processing") return job.worker_id ? `执行中 · ${job.worker_id}` : "执行中";
  if (job.error_message) return String(job.error_message).split("\n")[0];
  return `最近更新 ${new Date(job.updated_at).toLocaleString()}`;
}

async function refreshTaskBoard() {
  const list = byId("taskBoardList"), summary = byId("taskBoardSummary");
  if (!list || !summary) return;
  try {
    const data = await api("/v1/jobs?limit=100", { cache: "no-store" });
    const jobs = data.jobs || [];
    summary.textContent = taskSummary(jobs);
    list.innerHTML = jobs.map(job => {
      const active = job.status === "pending" || job.status === "processing";
      const progress = Math.max(0, Math.min(100, Number(job.progress) || 0));
      return `<article class="task-card">
        <div class="task-card-main"><strong>${escapeHtml(job.job_id)}</strong><span>${escapeHtml(job.job_type)}</span></div>
        <div class="task-card-detail"><span class="task-status ${escapeHtml(job.status)}">${escapeHtml(job.status)}</span> ${escapeHtml(taskDetail(job))}${active ? `<progress value="${progress}" max="100"></progress>` : ""}</div>
        <div class="task-card-actions">${active ? `<button class="task-cancel-btn" type="button" onclick="cancelTaskFromBoard('${escapeHtml(job.job_id)}')">取消任务</button>` : ""}</div>
      </article>`;
    }).join("") || '<p class="empty-note">暂无任务记录</p>';
  } catch (error) {
    summary.textContent = `任务看板读取失败：${error.message}`;
  }
}

async function cancelTaskFromBoard(jobId) {
  if (!window.confirm(`确认取消任务 ${jobId}？`)) return;
  if (await cancelJob(jobId)) {
    await refreshTaskBoard();
  }
}

async function pollJob(jobId, label) {
  const button = byId("loaderCancel");
  if (button) {
    button.hidden = false;
    button.disabled = false;
    button.onclick = async () => {
      button.disabled = true;
      button.textContent = "正在取消…";
      if (!await cancelJob(jobId)) {
        button.disabled = false;
        button.textContent = "取消任务";
      }
    };
  }
  try {
    let retryCount = 0;
    while (true) {
      let job;
      try {
        job = await api(`/v1/jobs/${jobId}`, { cache: "no-store" });
        retryCount = 0;
      } catch (error) {
        // A browser refresh, a brief server restart, or a sleeping machine must
        // not turn a running render into a false failure or a duplicate submit.
        if (error.status === 404) {
          forgetActiveJob(jobId);
          throw error;
        }
        retryCount += 1;
        const delay = Math.min(JOB_RETRY_MAX_DELAY_MS, 1000 * (2 ** Math.min(retryCount - 1, 4)));
        byId("loaderDetail").textContent = `${label} · 与服务暂时断开，将在 ${Math.ceil(delay / 1000)} 秒后继续跟踪（第 ${retryCount} 次重试）`;
        await sleep(delay);
        continue;
      }
      // A queued job is waiting, not broken. Saying so stops it looking like a
      // silent failure, which is exactly how a blocked preflight used to read.
      const percent = job.progress ?? 0;
      const bar = "█".repeat(Math.round(percent / 5)) + "░".repeat(20 - Math.round(percent / 5));
      let detail = `${label} · 任务 ${jobId}\n${bar} ${percent}%`;
      if (job.status === "pending") {
        detail = `${label} · 任务 ${jobId} · 排队中（${job.lane || "-"} 车道第 ${job.queue_position} 位）`;
        if (job.blocked_by) detail += ` · 前面是 ${job.blocked_by.job_type}`;
      }
      byId("loaderDetail").textContent = detail;
      if (job.status === "completed") {
        forgetActiveJob(jobId);
        return job.result_data || {};
      }
      if (job.status === "cancelled") {
        forgetActiveJob(jobId);
        throw new Error("任务已取消");
      }
      if (job.status === "failed") {
        forgetActiveJob(jobId);
        const message = String(job.error_message || "后台任务执行失败").split("\n")[0];
        throw new Error(message);
      }
      await sleep(JOB_POLL_INTERVAL_MS);
    }
  } finally {
    if (button) {
      button.hidden = true;
      button.onclick = null;
      button.textContent = "取消任务";
    }
  }
}

async function runJob(endpoint, body, label) {
  showLoader(label);
  try {
    const created = await api(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    rememberActiveJob(created.job_id, label);
    const result = await pollJob(created.job_id, label);
    showResult(`${label}完成 · 任务编号 ${created.job_id}`, result);
    setBanner(`${label}完成，任务编号：${created.job_id}`, "success");
    return { jobId: created.job_id, result };
  } catch (error) {
    setBanner(`${label}失败：${error.message}`, "error");
    toast(error.message, true);
    throw error;
  } finally {
    hideLoader();
  }
}

async function resumeActiveJob() {
  const saved = activeJob();
  if (!saved) return;
  showLoader(`继续跟踪：${saved.label}`, `任务 ${saved.jobId} 正在后台执行`);
  try {
    const result = await pollJob(saved.jobId, saved.label);
    showResult(`${saved.label}完成 · 任务编号 ${saved.jobId}`, result);
    setBanner(`${saved.label}完成，任务编号：${saved.jobId}`, "success");
    if (result.output_path || result.preview_path) {
      await renderMediaInViewer(result.output_path || result.preview_path, saved.jobId);
    }
  } catch (error) {
    setBanner(`${saved.label}无法继续跟踪：${error.message}`, "error");
    toast(error.message, true);
  } finally {
    hideLoader();
  }
}

function requirePath(value, label) {
  if (!value) { toast(`请先填写${label}`, true); throw new Error(`缺少${label}`); }
  return value;
}

function triggerFilePicker(id = "filePickerInput") { byId(id).click(); }

async function handleFileSelected(input, targetId = "filePathInput") {
  const file = input.files?.[0];
  if (!file) return;
  showLoader("正在上传本机文件", file.name);
  try {
    const form = new FormData();
    form.append("file", file);
    const data = await api("/v1/upload", { method: "POST", body: form });
    byId(targetId).value = data.file_path;
    toast(`已选择：${file.name}`);
  } catch (error) {
    toast(`上传失败：${error.message}`, true);
  } finally { hideLoader(); input.value = ""; }
}

function selectSample(filename) {
  byId("filePathInput").value = `F:\\LocalColorService\\test_assets\\${filename}`;
}

async function loadSystemInfo() {
  try {
    const [health, models, looks, performance] = await Promise.all([
      api("/health", { cache: "no-store" }),
      api("/v1/models/status", { cache: "no-store" }),
      api("/v1/looks", { cache: "no-store" }),
      api("/v1/performance/status", { cache: "no-store" }),
    ]);
    byId("serviceStatus").textContent = `服务正常 · ${health.version}`;
    byId("serviceStatus").classList.add("ok");
    byId("gpuStatus").textContent = health.gpu_available ? "已检测到 NVIDIA 显卡" : "未检测到 NVIDIA 显卡";
    byId("gpuStatus").classList.add(health.gpu_available ? "ok" : "warn");
    const acceleration = performance.acceleration || {};
    byId("accelerationStatus").textContent = acceleration.nvenc_preview
      ? `NVDEC/NVENC · LUT ${acceleration.lut_backend || "CPU"}` : "CPU 渲染回退";
    byId("accelerationStatus").classList.add(acceleration.nvenc_preview ? "ok" : "warn");
    const model = models.models?.adaint || {};
    const canon = models.models?.canoncgt || {};
    byId("adaintModelStatus").textContent = model.available ? "模型可用" : "模型不可用";
    byId("torchDeviceStatus").textContent = `${model.device || "未知"} · PyTorch ${model.torch_version || "未知"}`;
    byId("canoncgtModelStatus").textContent = canon.available ? "官方模型可用" : `未安装 · ${canon.detail || "将使用统计回退"}`;
    state.looks = looks.looks || [];
    byId("lookSelect").innerHTML = state.looks.map(look =>
      `<option value="${escapeHtml(look.id)}">${escapeHtml(look.name)} / ${escapeHtml(look.display_name_en)}</option>`
    ).join("");
    renderLookProfile();
    setBanner("本机服务已就绪，可以开始测试", "success");
  } catch (error) {
    byId("serviceStatus").textContent = "服务连接失败";
    byId("serviceStatus").classList.add("warn");
    setBanner(`初始化失败：${error.message}`, "error");
  }
}

function renderLookProfile() {
  const look = state.looks.find(item => item.id === byId("lookSelect").value);
  if (!look) return;
  byId("lookSuitable").textContent = (look.suitable_for || []).join("、") || "—";
  byId("lookCharacteristics").textContent = (look.core_characteristics || []).join("、") || look.description || "—";
  byId("lookProtection").textContent = (look.must_protect || []).join("、") || "—";
  byId("lookTechnicalNote").textContent = `${look.technical_notes || ""} ${look.classification || ""}`.trim();
}

async function runSceneAnalysis() {
  try {
    byId("sceneJobState").textContent = "正在分析";
    const task = await runJob("/v1/color/scenes", {
      file_path: requirePath(sourcePath(), "待调色素材路径"),
      detector: byId("sceneDetectorSelect").value,
      threshold: Number(byId("sceneThresholdInput").value),
      min_scene_len: Number(byId("minSceneLenInput").value),
      analyze: true,
    }, "镜头检测与分析");
    state.sceneJobId = task.jobId;
    state.sceneResult = task.result.scene_analysis;
    state.projectJobId = null;
    state.projectResult = null;
    byId("sceneJobState").textContent = `${task.result.scene_count} 个镜头 · ${task.jobId}`;
    byId("projectJobState").textContent = "等待生成";
    renderSceneTable(state.sceneResult);
    byId("timelineAcceleration").textContent = state.sceneResult.acceleration?.mode || "原始素材分析";
  } catch { byId("sceneJobState").textContent = "分析失败"; }
}

async function createProjectRecipe() {
  if (!state.sceneJobId) return toast("请先执行镜头检测与分析", true);
  try {
    byId("projectJobState").textContent = "正在生成";
    const task = await runJob("/v1/color/project-recipe", {
      scene_job_id: state.sceneJobId,
      look_id: byId("lookSelect").value,
      workflow: byId("workflowSelect").value,
      quality_profile: byId("qualityProfileSelect").value,
    }, "生成项目级调色配方");
    state.projectJobId = task.jobId;
    state.projectResult = task.result.project_recipe;
    byId("projectJobState").textContent = `${task.result.shot_count} 个镜头 / ${task.result.scene_group_count} 个场景组 · r${task.result.revision}`;
    renderGradePlan(state.projectResult);
  } catch { byId("projectJobState").textContent = "生成失败"; }
}

async function runProjectRender(preview) {
  if (!state.projectJobId) return toast("请先生成项目配方", true);
  if (!preview && state.projectResult?.status !== "approved") return toast("请先批准当前 GradePlan", true);
  try {
    const label = preview ? "生成镜头项目预览" : "镜头项目正式渲染";
    const task = await runJob("/v1/color/project-render", {
      project_job_id: state.projectJobId,
      input_path: requirePath(sourcePath(), "待调色素材路径"),
      target_height: preview ? 720 : 0,
      preview,
      quality_profile: state.projectResult?.quality_profile,
    }, label);
    await renderMediaInViewer(task.result.output_path, task.jobId);
    showProjectQuality(task.result.project_quality_report, task.result.quality_report, label);
  } catch { /* runJob already reports the error */ }
}

function renderGradePlan(project) {
  if (!project) return;
  byId("approvalState").textContent = `${project.status === "approved" ? "已批准" : "草稿"} · r${project.revision}`;
  const profile = byId("qualityProfileSelect");
  if (profile && project.quality_profile) profile.value = project.quality_profile;
  const groups = project.scene_groups || [];
  const groupOptions = groups.map(group =>
    `<option value="${escapeHtml(group.scene_group_id)}">${escapeHtml(group.label || group.scene_group_id)} · ${group.shot_ids.length} 镜头</option>`
  ).join("");
  // Full-project reference matching is the safe default.  Previously the
  // first scene-group option became the browser's implicit selection, so a
  // user who clicked "generate candidates" without touching the dropdown
  // unknowingly processed only group_0001.
  byId("referenceGroupSelect").innerHTML = '<option value="__all__">全视频 / 全部场景组（逐组生成 LUT）</option>' + groupOptions;
  byId("referenceScopeHelp").textContent = "默认处理整部视频：CanonCGT 会为每个 SceneGroup 生成候选并在最终预览中应用。若只想调整一个场景组，再从下拉框选择具体组。";
  byId("adaintGroupSelect").innerHTML = '<option value="__all__">全部场景组（默认）</option>' + groupOptions;
  renderSceneTable(state.sceneResult, project);
}

async function saveGradePlan(previewAfter = false) {
  if (!state.projectJobId || !state.projectResult) return toast("请先生成项目配方", true);
  const rows = [...byId("sceneTableBody").querySelectorAll("tr[data-shot-id]")];
  const grouped = new Map();
  const updates = rows.map(row => {
    const shotId = row.dataset.shotId;
    const groupId = row.querySelector(".group-input").value.trim() || "group_0001";
    if (!grouped.has(groupId)) grouped.set(groupId, []);
    grouped.get(groupId).push(shotId);
    return {
      shot_id: shotId,
      exposure: Number(row.querySelector(".exposure-input").value),
      temperature: Number(row.querySelector(".temperature-input").value),
      look_strength: Number(row.querySelector(".strength-input").value),
      enabled: row.querySelector(".enabled-input").checked,
    };
  });
  const groups = [...grouped.entries()].map(([id, shotIds], index) => {
    const existing = (state.projectResult.scene_groups || []).find(group => group.scene_group_id === id);
    return { scene_group_id: id, shot_ids: shotIds, hero_shot_id: existing && shotIds.includes(existing.hero_shot_id) ? existing.hero_shot_id : shotIds[0], label: existing?.label || `场景组 ${index + 1}` };
  });
  try {
    const task = await runJob("/v1/color/grade-plan/revise", { project_job_id: state.projectJobId, expected_revision: state.projectResult.revision, scene_groups: groups, shot_updates: updates }, "保存 GradePlan 人工调整");
    state.projectJobId = task.jobId; state.projectResult = task.result.project_recipe; renderGradePlan(state.projectResult);
    if (previewAfter) await runProjectRender(true);
  } catch { /* handled */ }
}

async function approveGradePlan() {
  if (!state.projectJobId || !state.projectResult) return toast("请先生成项目配方", true);
  try {
    const task = await runJob("/v1/color/grade-plan/approve", { project_job_id: state.projectJobId, expected_revision: state.projectResult.revision, actor: "local-user" }, "批准 GradePlan");
    state.projectJobId = task.jobId; state.projectResult = task.result.project_recipe; renderGradePlan(state.projectResult); return true;
  } catch { return false; }
}

async function approveAndRender() {
  if (!state.projectJobId || !state.projectResult) return toast("请先生成项目配方", true);
  if (state.projectResult.status !== "approved" && !(await approveGradePlan())) return;
  await runProjectRender(false);
}

async function generateSceneGroupAdaInt(previewWhole = true) {
  if (!state.projectJobId || !state.projectResult) return toast("请先分析镜头并生成项目配方", true);
  const scope = byId("adaintGroupSelect").value;
  try {
    const task = await runJob("/v1/color/scene-group-adaint", {
      project_job_id: state.projectJobId,
      expected_revision: state.projectResult.revision,
      ...(scope === "__all__" ? {} : { scene_group_ids: [scope] }),
      lut_size: Number(byId("aiLutSizeSelect").value),
      strength: Number(byId("adaintStrengthInput").value),
    }, scope === "__all__" ? "为全部场景组生成 AdaInt LUT" : `为 ${scope} 生成 AdaInt LUT`);
    state.projectJobId = task.jobId; state.projectResult = task.result.project_recipe; renderGradePlan(state.projectResult);
    toast(`已生成 ${task.result.scene_group_count} 个场景 LUT，正在生成预览`);
    if (previewWhole) await runProjectRender(true); else await previewTimeline("scene_group");
  } catch { /* handled */ }
}

async function exportProject() {
  if (!state.projectJobId) return toast("请先生成项目配方", true);
  const format = byId("exportFormatSelect").value;
  const sceneId = byId("exportSceneIdInput").value.trim();
  try {
    const task = await runJob("/v1/color/export", {
      source_job_id: state.projectJobId,
      format,
      ...(sceneId ? { scene_id: sceneId } : {}),
    }, `导出 ${format.toUpperCase()} 文件`);
    toast(`文件已生成：${task.result.export_path}`);
  } catch { /* handled */ }
}

async function runAdaInt() {
  try {
    const sample = byId("sampleTimeInput").value.trim();
    const task = await runJob("/v1/color/adaint-lut", {
      input_path: requirePath(sourcePath(), "待调色素材路径"),
      lut_size: Number(byId("aiLutSizeSelect").value),
      ...(sample ? { sample_time: Number(sample) } : {}),
    }, "生成 AdaInt 智能 LUT");
    state.adaintLutPath = task.result.lut_path;
    byId("previewAdaIntBtn").disabled = false;
    toast("AdaInt LUT 已生成，可点击预览效果");
  } catch { /* handled */ }
}

async function previewAdaInt() {
  if (!state.adaintLutPath) return toast("请先生成 AdaInt LUT", true);
  await renderLutPreview(state.adaintLutPath, "AdaInt LUT 预览");
}

async function runReferenceCandidates() {
  if (!state.projectJobId || !state.projectResult) return toast("请先完成镜头分析并生成项目配方", true);
  try {
    const sample = byId("sampleTimeInput").value.trim();
    const scope = byId("referenceGroupSelect").value || "__all__";
    const task = await runJob("/v1/color/reference-candidates", {
      source_path: requirePath(sourcePath(), "待调色素材路径"),
      reference_path: requirePath(byId("referencePathInput").value.trim(), "参考素材路径"),
      engine: byId("candidateEngineSelect").value,
      project_job_id: state.projectJobId,
      scene_group_id: scope,
      ...(sample ? { sample_time: Number(sample) } : {}),
    }, "生成参考图调色候选");
    state.candidateJobId = task.jobId;
    state.candidateScope = scope;
    renderCandidates(task.result.candidates || []);
    const affectedCount = task.result.affected_scene_group_ids?.length || (scope === "__all__" ? state.projectResult.scene_groups?.length || 0 : 1);
    toast(`${scope === "__all__" ? "候选已按场景组分别生成" : `候选已生成（${scope}）`} · 实际处理 ${affectedCount} 个场景组，请检查分数后选择 A/B/C`);
  } catch { /* handled */ }
}

async function runReferencePreflight() {
  try {
    const scope = byId("referenceGroupSelect").value || "__all__";
    const task = await runJob("/v1/color/reference-preflight", {
      reference_path: requirePath(byId("referencePathInput").value.trim(), "参考素材路径"),
      ...(state.projectJobId ? { project_job_id: state.projectJobId, scene_group_id: scope } : {}),
    }, "参考图预检");
    const report = task.result.reference_report || {};
    const warnings = [...(report.warnings || []), ...(task.result.compatibility_warnings || [])];
    const panel = byId("referencePreflight");
    panel.hidden = false;
    panel.innerHTML = `<div class="reference-preflight-grid">
      <img src="${escapeHtml(task.result.preview_url)}" alt="参考图预览">
      <div><strong>适配度 ${Math.round(Number(task.result.suitability_score || 0) * 100)}%</strong><br>
      ${report.width || 0} × ${report.height || 0} · 高光裁切 ${formatPercent(report.highlight_clipping_ratio)} · 暗部裁切 ${formatPercent(report.black_clipping_ratio)}<br>
      ${warnings.length ? `⚠ ${escapeHtml(warnings.join("；"))}` : "画面技术检查通过"}<br>
      ${escapeHtml((task.result.recommendations || []).join("；"))}</div></div>`;
  } catch { /* handled */ }
}

function renderCandidates(candidates) {
  const labels = { restrained: "克制自然", balanced: "均衡明亮", cinematic: "柔和电影感" };
  byId("candidatePanel").innerHTML = candidates.map(item => `
    <article class="candidate-card">
      <h3>候选 ${escapeHtml(item.id)} · ${labels[item.label] || escapeHtml(item.label || "模型结果")}</h3>
      <p>来源：${escapeHtml(item.provider || item.engine || item.look_id || "自定义")}<br>强度：${item.strength == null ? "模型决定" : Math.round(item.strength * 100) + "%"}<br>总分：${Math.round(Number(item.score?.total || 0) * 100)} · 安全：${Math.round(Number(item.score?.technical_safety || 0) * 100)} · 连续：${Math.round(Number(item.score?.continuity || 0) * 100)} · 匹配：${Math.round(Number(item.score?.reference_match || 0) * 100)}${item.fallback_used ? "<br>⚠ 已使用统计回退" : ""}${item.warnings?.length ? `<br>⚠ ${escapeHtml(item.warnings.join("；"))}` : ""}</p>
       <button class="secondary-btn" onclick="selectAndPreviewCandidate('${escapeHtml(item.id)}')">套用并预览整个视频</button>
    </article>`).join("") || '<p class="empty-note">未返回候选结果</p>';
}

async function selectAndPreviewCandidate(candidateId, automatic = false) {
  if (!state.candidateJobId) return toast("请先生成候选", true);
  try {
    const groupId = state.candidateScope || "__all__";
    const selected = await runJob("/v1/color/reference-select", {
      candidate_job_id: state.candidateJobId,
      candidate_id: candidateId,
      project_job_id: state.projectJobId, scene_group_id: groupId, expected_revision: state.projectResult.revision,
    }, `选择候选 ${candidateId}`);
    if (selected.result.project_recipe) {
      state.projectJobId = selected.jobId; state.projectResult = selected.result.project_recipe; renderGradePlan(state.projectResult);
      await runProjectRender(true);
    } else {
      await renderLutPreview(selected.result.selected_lut_path, `候选 ${candidateId} 预览`);
    }
  } catch { /* handled */ }
}

async function renderLutPreview(lutPath, label) {
  try {
    const task = await runJob("/v1/color/lut-render", {
      input_path: requirePath(sourcePath(), "待调色素材路径"),
      lut_path: lutPath,
      target_height: 720,
      preview: true,
      split_screen: false,
    }, label);
    await renderMediaInViewer(task.result.preview_path, task.jobId);
    showQuality(task.result.quality_report, label);
  } catch { /* handled */ }
}

function renderSceneTable(analysis, project = state.projectResult) {
  const scenes = analysis?.scenes || [];
  byId("sceneSummary").textContent = scenes.length ? `共 ${scenes.length} 个镜头 · ${formatTime(analysis.media_info?.duration || 0)}` : "没有检测到镜头";
  byId("sceneTableBody").innerHTML = scenes.map(scene => {
    const report = scene.analysis || {};
    const shot = project?.shots?.find(item => (item.shot_id || item.scene_id) === scene.scene_id);
    const exposure = shot?.base_correction?.exposure ?? scene.suggested_recipe?.exposure ?? 0;
    const group = project?.scene_groups?.find(item => item.shot_ids.includes(scene.scene_id));
    const decision = scene.grade_decision || {};
    const face = scene.face_analysis || {};
    const warnings = [
      ...(report.anomalies || []),
      `智能建议 ${decision.action || "待分析"}（技术需求 ${Math.round(Number(decision.technical_need || 0) * 100)} / 风险 ${Math.round(Number(decision.creative_risk || 0) * 100)}）`,
      ...(Number(face.face_count || 0) ? [`检测到 ${face.face_count} 张人脸，启用肤色保护`] : []),
    ].join("；");
    return `<tr data-shot-id="${escapeHtml(scene.scene_id)}"><td>${escapeHtml(scene.scene_id)}</td><td><input class="group-input table-input" value="${escapeHtml(group?.scene_group_id || scene.scene_id)}"></td><td>${formatTime(scene.start_time)} – ${formatTime(scene.end_time)}</td><td><input class="exposure-input table-number" type="number" min="-2" max="2" step="0.05" value="${Number(exposure).toFixed(2)}"></td><td><input class="temperature-input table-number" type="number" min="-50" max="50" step="1" value="${Number(shot?.base_correction?.temperature || 0).toFixed(0)}"></td><td><input class="strength-input table-number" type="number" min="0" max="1" step="0.05" value="${Number(shot?.look_strength ?? 1).toFixed(2)}"></td><td><input class="enabled-input" type="checkbox" ${shot?.enabled === false ? "" : "checked"}></td><td title="${escapeHtml(warnings)}">${escapeHtml(warnings)}</td></tr>`;
  }).join("") || '<tr><td colspan="9" class="empty-cell">没有镜头数据</td></tr>';
  byId("sceneTableBody").querySelectorAll("tr[data-shot-id]").forEach(row => {
    const shot = project?.shots?.find(item => (item.shot_id || item.scene_id) === row.dataset.shotId);
    const correction = shot?.base_correction || {};
    const cell = document.createElement("td");
    cell.className = "grade-summary-cell";
    cell.title = "色调偏移 / 对比度 / 对比中心 / 饱和度 / 高光滚降 / 高光柔化 / 肤色保护 / 色域保护";
    cell.textContent = `色调 ${Number(correction.tint || 0).toFixed(2)} · 对比 ${Number(correction.contrast ?? 1).toFixed(2)} · Pivot ${Number(correction.pivot ?? 0.18).toFixed(2)} · 饱和 ${Number(correction.saturation ?? 1).toFixed(2)} · 滚降 ${Number(correction.highlight_rolloff || 0).toFixed(2)} · 柔化 ${Number(correction.highlight_softness || 0).toFixed(2)} · 肤保 ${Number(correction.skin_protection || 0).toFixed(2)} · 色域保 ${Number(correction.gamut_protection || 0).toFixed(2)}`;
    const enabledCell = row.querySelector(".enabled-input")?.closest("td");
    if (enabledCell) row.insertBefore(cell, enabledCell);
  });
  const reports = scenes.map(item => item.analysis).filter(Boolean);
  if (reports.length) {
    const average = getter => reports.reduce((sum, report) => sum + Number(getter(report) || 0), 0) / reports.length;
    byId("statLumMean").textContent = average(x => x.luminance?.mean).toFixed(3);
    byId("statLumMed").textContent = average(x => x.luminance?.median).toFixed(3);
    byId("statBlackClip").textContent = `${(average(x => x.clipping?.black_clipping_ratio) * 100).toFixed(2)}%`;
    byId("statHighClip").textContent = `${(average(x => x.clipping?.highlight_clipping_ratio) * 100).toFixed(2)}%`;
  }
  renderTimeline(analysis, project);
}

function renderTimeline(analysis, project = state.projectResult) {
  const scenes = analysis?.scenes || [];
  const track = byId("timelineTrack");
  if (!scenes.length) {
    track.innerHTML = '<p class="empty-note">完成镜头分析后显示缩略图时间线。</p>';
    byId("timelineInspector").hidden = true;
    return;
  }
  const maxDuration = Math.max(...scenes.map(item => Number(item.duration || 0)), 1);
  track.innerHTML = scenes.map(scene => {
    const shot = project?.shots?.find(item => (item.shot_id || item.scene_id) === scene.scene_id);
    const group = project?.scene_groups?.find(item => item.shot_ids.includes(scene.scene_id));
    const provider = group?.creative_transform?.provider || (project ? "预设" : "分析");
    const width = Math.max(112, Math.min(280, 112 + Number(scene.duration || 0) / maxDuration * 150));
    const thumb = scene.thumbnail_path && state.sceneJobId ? `/v1/jobs/${state.sceneJobId}/artifacts/${scene.thumbnail_path}` : "";
    return `<button type="button" class="timeline-clip ${state.selectedShotId === scene.scene_id ? "selected" : ""}" style="--clip-width:${width}px" data-shot-id="${escapeHtml(scene.scene_id)}" onclick="selectTimelineShot('${escapeHtml(scene.scene_id)}')">
      ${thumb ? `<img src="${escapeHtml(thumb)}" alt="${escapeHtml(scene.scene_id)} 缩略图" loading="lazy">` : '<span class="timeline-thumb-placeholder"></span>'}
      <span class="timeline-badge ${provider === "adaint" || provider === "canoncgt" ? "ai" : ""}">${escapeHtml(provider)}</span>
      <span class="timeline-clip-info"><strong>${escapeHtml(scene.scene_id)} · ${escapeHtml(group?.label || group?.scene_group_id || "未分组")}</strong><span>${formatTime(scene.start_time)}–${formatTime(scene.end_time)} · ${Number(scene.duration || 0).toFixed(1)}s</span></span>
    </button>`;
  }).join("");
  if (state.selectedShotId && scenes.some(item => item.scene_id === state.selectedShotId)) selectTimelineShot(state.selectedShotId, false);
}

function selectedTimelineContext() {
  const scene = state.sceneResult?.scenes?.find(item => item.scene_id === state.selectedShotId);
  const shot = state.projectResult?.shots?.find(item => (item.shot_id || item.scene_id) === state.selectedShotId);
  const group = state.projectResult?.scene_groups?.find(item => item.shot_ids.includes(state.selectedShotId));
  return { scene, shot, group };
}

function setTimelineValue(id, value, digits = 2) {
  const input = byId(id); if (!input) return;
  const number = Number(value); input.value = Number.isFinite(number) ? number.toFixed(digits) : "";
}

function setTimelineVector(prefix, values, digits = 2, fallback = [0, 0, 0]) {
  const vector = Array.isArray(values) && values.length === 3 ? values : fallback;
  ["R", "G", "B"].forEach((channel, index) => setTimelineValue(`timeline${prefix}${channel}Input`, vector[index], digits));
}

function timelineNumber(id, fallback = 0) {
  const value = Number(byId(id)?.value); return Number.isFinite(value) ? value : fallback;
}

function timelineVector(prefix, fallback) {
  return ["R", "G", "B"].map((channel, index) => timelineNumber(`timeline${prefix}${channel}Input`, fallback[index]));
}

function timelineGradeUpdate(shotId) {
  return {
    shot_id: shotId,
    exposure: Number(byId("timelineExposureInput").value), temperature: Number(byId("timelineTemperatureInput").value),
    tint: timelineNumber("timelineTintInput"), contrast: timelineNumber("timelineContrastInput", 1),
    pivot: timelineNumber("timelinePivotInput", 0.18), saturation: timelineNumber("timelineSaturationInput", 1),
    highlight_rolloff: timelineNumber("timelineHighlightRolloffInput"), highlight_softness: timelineNumber("timelineHighlightSoftnessInput"),
    color_density: timelineNumber("timelineColorDensityInput"), skin_protection: timelineNumber("timelineSkinProtectionInput"),
    gamut_protection: timelineNumber("timelineGamutProtectionInput"), rgb_gains: timelineVector("Rgb", [1, 1, 1]),
    lift: timelineVector("Lift", [0, 0, 0]), gamma: timelineVector("Gamma", [1, 1, 1]), gain: timelineVector("Gain", [1, 1, 1]),
    look_strength: Number(byId("timelineStrengthInput").value),
  };
}

function selectTimelineShot(shotId, seek = true) {
  state.selectedShotId = shotId;
  byId("timelineTrack").querySelectorAll(".timeline-clip").forEach(item => item.classList.toggle("selected", item.dataset.shotId === shotId));
  const { scene, shot, group } = selectedTimelineContext();
  if (!scene) return;
  byId("timelineInspector").hidden = false;
  byId("timelineShotTitle").textContent = `${shotId} · ${group?.label || group?.scene_group_id || "等待项目配方"}`;
  byId("timelineShotMeta").textContent = `${formatTime(scene.start_time)}–${formatTime(scene.end_time)} · ${Number(scene.duration || 0).toFixed(2)} 秒`;
  byId("timelineExposureInput").value = Number(shot?.base_correction?.exposure ?? scene.suggested_recipe?.exposure ?? 0).toFixed(2);
  byId("timelineTemperatureInput").value = Number(shot?.base_correction?.temperature ?? scene.suggested_recipe?.temperature ?? 0).toFixed(0);
  byId("timelineStrengthInput").value = Number(shot?.look_strength ?? 1).toFixed(2);
  const correction = shot?.base_correction || scene.suggested_recipe || {};
  setTimelineValue("timelineTintInput", correction.tint, 2); setTimelineValue("timelineContrastInput", correction.contrast ?? 1, 2);
  setTimelineValue("timelinePivotInput", correction.pivot ?? 0.18, 2); setTimelineValue("timelineSaturationInput", correction.saturation ?? 1, 2);
  setTimelineValue("timelineHighlightRolloffInput", correction.highlight_rolloff, 2); setTimelineValue("timelineHighlightSoftnessInput", correction.highlight_softness, 2);
  setTimelineValue("timelineSkinProtectionInput", correction.skin_protection, 2); setTimelineValue("timelineGamutProtectionInput", correction.gamut_protection, 2);
  setTimelineValue("timelineColorDensityInput", correction.color_density, 2);
  setTimelineVector("Rgb", correction.rgb_gains, 3, [1, 1, 1]); setTimelineVector("Lift", correction.lift, 3, [0, 0, 0]);
  setTimelineVector("Gamma", correction.gamma, 3, [1, 1, 1]); setTimelineVector("Gain", correction.gain, 3, [1, 1, 1]);
  if (seek) seekComparisonTo(scene.start_time);
}

function seekComparisonTo(seconds) {
  state.comparisonVideos.forEach(video => { if (Number.isFinite(video.duration)) video.currentTime = Math.min(Number(seconds), video.duration); });
  byId("compareCurrentTime").textContent = formatTime(seconds);
}

async function applyTimelineManual() {
  if (!state.projectJobId || !state.projectResult || !state.selectedShotId) return toast("请先生成项目配方并选择镜头", true);
  const { group } = selectedTimelineContext();
  const scope = byId("timelineScopeSelect").value;
  const ids = scope === "scene_group" ? (group?.shot_ids || []) : [state.selectedShotId];
  const update = timelineGradeUpdate(state.selectedShotId);
  try {
    const task = await runJob("/v1/color/grade-plan/revise", {
      project_job_id: state.projectJobId, expected_revision: state.projectResult.revision,
      shot_updates: ids.map(shot_id => ({shot_id, ...update})), actor: "timeline-user",
    }, scope === "scene_group" ? "应用 SceneGroup 人工调整" : "应用镜头人工调整");
    state.projectJobId = task.jobId; state.projectResult = task.result.project_recipe; renderGradePlan(state.projectResult);
  } catch { /* handled */ }
}

async function runTimelineAuto(operation) {
  if (!state.projectJobId || !state.projectResult || !state.selectedShotId) return toast("请先生成项目配方并选择镜头", true);
  const { group } = selectedTimelineContext();
  const scope = byId("timelineScopeSelect").value;
  try {
    const task = await runJob("/v1/color/timeline-adjust", {
      project_job_id: state.projectJobId, expected_revision: state.projectResult.revision, scope, operation,
      ...(scope === "shot" ? {shot_id: state.selectedShotId} : {scene_group_id: group.scene_group_id}),
    }, operation === "restore_auto" ? "恢复自动校正" : "匹配 Hero Shot");
    state.projectJobId = task.jobId; state.projectResult = task.result.project_recipe; renderGradePlan(state.projectResult);
  } catch { /* handled */ }
}

async function generateSelectedTimelineAdaInt() {
  const { group } = selectedTimelineContext();
  if (!group) return toast("请先生成项目配方并选择镜头", true);
  byId("adaintGroupSelect").value = group.scene_group_id;
  await generateSceneGroupAdaInt(false);
}

async function previewTimeline(scope) {
  if (!state.projectJobId || !state.selectedShotId) return toast("请先生成项目配方并选择镜头", true);
  const { group } = selectedTimelineContext();
  try {
    const task = await runJob("/v1/color/timeline-preview", {
      project_job_id: state.projectJobId, scope, target_height: 540,
      ...(scope === "shot" ? {shot_id: state.selectedShotId, context_seconds: 1} : {scene_group_id: group.scene_group_id}),
    }, scope === "shot" ? "生成镜头上下文预览" : "生成 SceneGroup 预览");
    await renderMediaInViewer(task.result.output_path, task.jobId);
  } catch { /* handled */ }
}

function clearResults() {
  state.sceneJobId = state.sceneResult = state.projectJobId = state.projectResult = null;
  state.selectedShotId = null;
  byId("sceneTableBody").innerHTML = '<tr><td colspan="9" class="empty-cell">还没有镜头数据</td></tr>';
  byId("sceneSummary").textContent = "等待镜头分析";
  byId("sceneJobState").textContent = "尚未开始";
  byId("projectJobState").textContent = "尚未开始";
  byId("approvalState").textContent = "未生成";
  byId("timelineTrack").innerHTML = '<p class="empty-note">完成镜头分析后显示缩略图时间线。</p>';
  byId("timelineInspector").hidden = true;
}

function manualRecipe() {
  return {
    exposure: Number(byId("expSlider").value), contrast: Number(byId("contrastSlider").value),
    saturation: Number(byId("satSlider").value), temperature: Number(byId("tempSlider").value),
    tint: Number(byId("tintSlider").value), pivot: Number(byId("pivotSlider").value),
    highlight_rolloff: Number(byId("rolloffSlider").value), highlight_softness: Number(byId("softnessSlider").value),
    color_density: Number(byId("colorDensitySlider").value),
    skin_protection: Number(byId("skinProtectionSlider").value), gamut_protection: Number(byId("gamutProtectionSlider").value),
    lift: ["R", "G", "B"].map(channel => Number(byId(`lift${channel}Slider`).value)),
    gamma: ["R", "G", "B"].map(channel => Number(byId(`gamma${channel}Slider`).value)),
    gain: ["R", "G", "B"].map(channel => Number(byId(`gain${channel}Slider`).value)),
    rgb_gains: ["R", "G", "B"].map(channel => Number(byId(`rgb${channel}Slider`).value)),
    lut_size: Number(byId("lutSizeSelect").value),
  };
}

async function runAnalyze() {
  try {
    const body = { file_path: requirePath(sourcePath(), "待调色素材路径"), provider_type: byId("providerSelect").value, sample_count: 7 };
    if (body.provider_type === "reinhard_transfer") body.reference_path = requirePath(byId("referencePathInput").value.trim(), "参考素材路径");
    const task = await runJob("/v1/color/analyze", body, "整片画面分析");
    updateAnalysisMetrics(task.result.analysis_report || task.result.analysis);
    const recipe = task.result.recipe || {};
    setSliders(recipe);
  } catch { /* handled */ }
}

async function runPreview() {
  try {
    const task = await runJob("/v1/color/preview", {
      input_path: requirePath(sourcePath(), "待调色素材路径"), recipe: manualRecipe(), target_height: 720,
      split_screen: byId("splitScreenCheck").checked,
    }, "快速整片预览");
    await renderMediaInViewer(task.result.preview_path, task.jobId);
    showQuality(task.result.quality_report, "快速整片预览");
  } catch { /* handled */ }
}

async function runFullRender() {
  try {
    const task = await runJob("/v1/color/render", { input_path: requirePath(sourcePath(), "待调色素材路径"), recipe: manualRecipe() }, "整片正式渲染");
    await renderMediaInViewer(task.result.output_path, task.jobId);
    showQuality(task.result.quality_report, "整片正式渲染");
  } catch { /* handled */ }
}

async function runBakeLut() {
  try { await runJob("/v1/color/lut", { recipe: manualRecipe() }, "生成三维查找表"); } catch { /* handled */ }
}

function setSliders(recipe) {
  [["expSlider", "exposure"], ["contrastSlider", "contrast"], ["satSlider", "saturation"], ["tempSlider", "temperature"], ["tintSlider", "tint"], ["pivotSlider", "pivot"], ["rolloffSlider", "highlight_rolloff"], ["softnessSlider", "highlight_softness"], ["colorDensitySlider", "color_density"], ["skinProtectionSlider", "skin_protection"], ["gamutProtectionSlider", "gamut_protection"]].forEach(([id, key]) => {
    if (recipe[key] != null) { byId(id).value = recipe[key]; byId(id).dispatchEvent(new Event("input")); }
  });
  [["lift", "lift"], ["gamma", "gamma"], ["gain", "gain"], ["rgb_gains", "rgb"]].forEach(([key, prefix]) => {
    if (!Array.isArray(recipe[key])) return;
    ["R", "G", "B"].forEach((channel, index) => { const input = byId(`${prefix}${channel}Slider`); if (input) { input.value = recipe[key][index]; input.dispatchEvent(new Event("input")); } });
  });
}

function updateAnalysisMetrics(report) {
  if (!report) return;
  byId("statLumMean").textContent = formatNumber(report.luminance?.mean);
  byId("statLumMed").textContent = formatNumber(report.luminance?.median);
  byId("statBlackClip").textContent = formatPercent(report.clipping?.black_clipping_ratio);
  byId("statHighClip").textContent = formatPercent(report.clipping?.highlight_clipping_ratio);
}

function showQuality(report, label) {
  if (!report) return;
  const passed = report.passed !== false;
  const errors = report.errors?.length ? `；${report.errors.join("；")}` : "";
  setBanner(`${label}质量检查${passed ? "通过" : "未通过"}${errors}`, passed ? "success" : "error");
}

function showProjectQuality(projectReport, renderReport, label) {
  if (!projectReport) return showQuality(renderReport, label);
  const decision = projectReport.final_decision || (projectReport.passed === false ? "FAIL" : "PASS");
  const labels = { PASS: "通过", PASS_WITH_WARNINGS: "带警告通过", NEEDS_REVIEW: "需要人工复核", FAIL: "失败" };
  const level = decision === "FAIL" ? "error" : decision === "NEEDS_REVIEW" ? "warning" : "success";
  const reviewCount = projectReport.review_items?.length || 0;
  const detail = `文件 ${projectReport.render_integrity || "-"} / 技术色彩 ${projectReport.technical_color || "-"} / 连续性 ${projectReport.scene_continuity || "-"} / 肤色 ${projectReport.skin_safety || "-"}`;
  setBanner(`${label}：${labels[decision] || decision}；${detail}${reviewCount ? `；${reviewCount} 项待复核` : ""}`, level);
}

function fillComparisonSelect(select, selectedId) {
  const names = { source: "原片", preview: "预览结果", render: "正式结果" };
  select.innerHTML = "";
  for (const kind of ["source", "preview", "render"]) {
    const items = state.comparisonItems.filter(item => item.kind === kind);
    if (!items.length) continue;
    const group = document.createElement("optgroup");
    group.label = names[kind];
    items.forEach(item => { const option = document.createElement("option"); option.value = item.id; option.textContent = item.label; group.appendChild(option); });
    select.appendChild(group);
  }
  if (selectedId && state.comparisonItems.some(item => item.id === selectedId)) select.value = selectedId;
}

function comparisonItem(id) { return state.comparisonItems.find(item => item.id === id); }

async function refreshComparisonAssets(options = {}) {
  try {
    const previousA = byId("compareASelect").value;
    const previousB = byId("compareBSelect").value;
    const data = await api("/v1/comparison/media", { cache: "no-store" });
    state.comparisonItems = data.items || [];
    let selectedB = state.comparisonItems.some(x => x.id === previousB) ? previousB : null;
    if (options.preferredJobId) selectedB = state.comparisonItems.find(x => x.job_id === options.preferredJobId && x.kind !== "source")?.id || selectedB;
    selectedB ||= state.comparisonItems.find(x => x.kind !== "source")?.id || state.comparisonItems[0]?.id;
    const b = comparisonItem(selectedB);
    let selectedA = state.comparisonItems.some(x => x.id === previousA) ? previousA : null;
    if (b) selectedA = state.comparisonItems.find(x => x.kind === "source" && x.group_key === b.group_key)?.id || selectedA;
    selectedA ||= state.comparisonItems.find(x => x.kind === "source")?.id || state.comparisonItems[0]?.id;
    fillComparisonSelect(byId("compareASelect"), selectedA);
    fillComparisonSelect(byId("compareBSelect"), selectedB);
    renderComparisonStage();
  } catch (error) { console.warn("无法刷新对比素材", error); }
}

function createVideo(item, muted) {
  const video = document.createElement("video");
  video.src = item.url;
  video.preload = "metadata";
  video.playsInline = true;
  video.muted = muted;
  return video;
}

function addLabel(container, prefix, item) {
  const label = document.createElement("span"); label.className = "media-label"; label.textContent = `${prefix} · ${item.label}`; container.appendChild(label);
}

function playbackState() {
  const video = state.comparisonVideos[0];
  return { time: video?.currentTime || 0, paused: video?.paused !== false, muted: video?.muted ?? false, rate: video?.playbackRate || 1 };
}

function renderComparisonStage(saved = null) {
  const a = comparisonItem(byId("compareASelect").value);
  const b = comparisonItem(byId("compareBSelect").value);
  const container = byId("mediaContainer");
  const play = saved || { time: 0, paused: true, muted: false, rate: 1 };
  container.innerHTML = "";
  state.comparisonVideos = [];
  if (!a || !b) {
    container.innerHTML = '<div class="media-placeholder"><div>▶</div><p>暂无可比较的视频结果</p></div>';
    byId("comparisonControls").hidden = true;
    return;
  }
  const stage = document.createElement("div");
  stage.className = `comparison-stage ${state.comparisonMode}`;
  const va = createVideo(a, play.muted), vb = createVideo(b, true);
  state.comparisonVideos = [va, vb];
  if (state.comparisonMode === "side") {
    const pa = document.createElement("div"), pb = document.createElement("div");
    pa.className = pb.className = "comparison-pane"; pa.appendChild(va); pb.appendChild(vb); addLabel(pa, "A", a); addLabel(pb, "B", b); stage.append(pa, pb);
  } else {
    const la = document.createElement("div"), lb = document.createElement("div"), divider = document.createElement("div"), slider = document.createElement("input");
    la.className = "comparison-layer a"; lb.className = "comparison-layer b"; divider.className = "wipe-divider"; slider.className = "wipe-slider"; slider.type = "range"; slider.min = 0; slider.max = 100; slider.value = 50;
    la.appendChild(va); lb.appendChild(vb); addLabel(la, "A", a); addLabel(lb, "B", b);
    slider.addEventListener("input", () => { lb.style.clipPath = `inset(0 0 0 ${slider.value}%)`; divider.style.left = `${slider.value}%`; });
    stage.append(la, lb, divider, slider);
  }
  container.appendChild(stage);
  byId("comparisonControls").hidden = false;
  bindPlayback(play);
}

function bindPlayback(play) {
  let ready = 0;
  state.comparisonVideos.forEach((video, index) => {
    video.playbackRate = play.rate;
    video.addEventListener("loadedmetadata", () => {
      ready += 1;
      const durations = state.comparisonVideos.map(v => v.duration).filter(Number.isFinite);
      state.comparisonDuration = durations.length ? Math.min(...durations) : 0;
      video.currentTime = Math.min(play.time, state.comparisonDuration || 0);
      byId("compareDuration").textContent = formatTime(state.comparisonDuration);
      if (ready === state.comparisonVideos.length && !play.paused) playVideos();
    });
    if (index === 0) {
      video.addEventListener("timeupdate", () => {
        const pct = state.comparisonDuration ? video.currentTime / state.comparisonDuration * 1000 : 0;
        byId("compareSeekSlider").value = Math.min(1000, pct);
        byId("compareCurrentTime").textContent = formatTime(video.currentTime);
        if (Math.abs((state.comparisonVideos[1]?.currentTime || 0) - video.currentTime) > .12) state.comparisonVideos[1].currentTime = video.currentTime;
      });
      video.addEventListener("play", updatePlayButton); video.addEventListener("pause", updatePlayButton);
    }
  });
  updatePlayButton(); updateAudioButton();
}

function playVideos() { state.comparisonVideos.forEach(video => video.play().catch(() => {})); }
function toggleComparisonPlayback() { const primary = state.comparisonVideos[0]; if (!primary) return; primary.paused ? playVideos() : state.comparisonVideos.forEach(video => video.pause()); }
function updatePlayButton() { byId("comparePlayBtn").textContent = state.comparisonVideos[0]?.paused === false ? "暂停" : "播放"; }
function toggleComparisonAudio() { const primary = state.comparisonVideos[0]; if (!primary) return; primary.muted = !primary.muted; updateAudioButton(); }
function updateAudioButton() { byId("compareAudioBtn").textContent = state.comparisonVideos[0]?.muted ? "A 路静音" : "A 路声音"; }
function setComparisonMode(mode) { const saved = playbackState(); state.comparisonMode = mode; byId("sideBySideModeBtn").classList.toggle("active", mode === "side"); byId("wipeModeBtn").classList.toggle("active", mode === "wipe"); renderComparisonStage(saved); }
function swapComparisonMedia() { const saved = playbackState(), a = byId("compareASelect").value; byId("compareASelect").value = byId("compareBSelect").value; byId("compareBSelect").value = a; renderComparisonStage(saved); }
async function renderMediaInViewer(path, jobId) { if (path) await refreshComparisonAssets({ preferredJobId: jobId }); }

function toggleLogConsole(forceOpen) {
  const open = forceOpen ?? !byId("logDrawer").classList.contains("open");
  byId("logDrawer").classList.toggle("open", open); byId("logDrawerOverlay").classList.toggle("open", open);
  clearInterval(state.logsTimer); state.logsTimer = null;
  if (open) { fetchSystemLogs(); state.logsTimer = setInterval(fetchSystemLogs, 2500); }
}

async function fetchSystemLogs() {
  try {
    const data = await api("/v1/system/logs?limit=150", { cache: "no-store" });
    const logs = data.logs || [];
    byId("logConsoleBody").innerHTML = logs.map(item => `<div class="log-line">${escapeHtml(typeof item === "string" ? item : `${item.timestamp || ""} ${item.level || ""} ${item.message || JSON.stringify(item)}`)}</div>`).join("") || '<div class="log-line">暂无日志</div>';
    byId("logConsoleBody").scrollTop = byId("logConsoleBody").scrollHeight;
  } catch (error) { byId("logConsoleBody").textContent = `读取失败：${error.message}`; }
}

function formatTime(seconds) { const value = Math.max(0, Number(seconds) || 0); const m = Math.floor(value / 60), s = Math.floor(value % 60); return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`; }
function formatNumber(value) { return value == null ? "—" : Number(value).toFixed(3); }
function formatPercent(value) { return value == null ? "—" : `${(Number(value) * 100).toFixed(2)}%`; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char])); }

[["expSlider", "expVal", v => `${Number(v).toFixed(2)} EV`], ["contrastSlider", "contrastVal", v => Number(v).toFixed(2)], ["satSlider", "satVal", v => Number(v).toFixed(2)], ["tempSlider", "tempVal", v => `${Number(v).toFixed(0)}°`]].forEach(([inputId, outputId, format]) => byId(inputId).addEventListener("input", event => { byId(outputId).textContent = format(event.target.value); }));
[["tintSlider", "tintVal"], ["pivotSlider", "pivotVal"], ["rolloffSlider", "rolloffVal"], ["softnessSlider", "softnessVal"], ["colorDensitySlider", "colorDensityVal"], ["skinProtectionSlider", "skinProtectionVal"], ["gamutProtectionSlider", "gamutProtectionVal"]].forEach(([inputId, outputId]) => byId(inputId).addEventListener("input", event => { byId(outputId).textContent = Number(event.target.value).toFixed(2); }));
byId("compareASelect").addEventListener("change", () => renderComparisonStage());
byId("compareBSelect").addEventListener("change", () => renderComparisonStage());
byId("compareSeekSlider").addEventListener("input", event => { const time = Number(event.target.value) / 1000 * state.comparisonDuration; state.comparisonVideos.forEach(video => { video.currentTime = time; }); byId("compareCurrentTime").textContent = formatTime(time); });
byId("compareRateSelect").addEventListener("change", event => state.comparisonVideos.forEach(video => { video.playbackRate = Number(event.target.value); }));
byId("lookSelect").addEventListener("change", renderLookProfile);

const legacyAdvancedControls = byId("legacyAdvancedControls");
const legacyPanel = document.querySelector(".legacy-panel");
if (legacyAdvancedControls && legacyPanel) legacyPanel.insertBefore(legacyAdvancedControls, legacyPanel.querySelector(".form-grid.two"));

loadSystemInfo();
refreshComparisonAssets();
resumeActiveJob();
refreshTaskBoard();
state.taskBoardTimer = setInterval(refreshTaskBoard, 3000);
