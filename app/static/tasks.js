const managerState = { statuses: "", timer: null, detail: null, selectedShotId: null, projectJobId: null };
const rowTarget = () => document.getElementById("taskManagerRows");
const esc = value => String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));

async function taskApi(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败（${response.status}）`);
  return data;
}

function summary(jobs) {
  const total = state => jobs.filter(job => job.status === state).length;
  return `执行中 ${total("processing")} · 正在取消 ${total("cancelling")} · 排队 ${total("pending")} · 已完成 ${total("completed")} · 失败 ${total("failed")} · 已取消 ${total("cancelled")}`;
}

function statusText(job) {
  if (job.status === "pending") return `排队第 ${job.queue_position || "?"} 位`;
  if (job.status === "cancelling") return "已请求取消，等待当前步骤退出";
  if (job.error_message) return String(job.error_message).split("\n")[0];
  return job.worker_id || "—";
}

function qualityProfileLabel(profile) {
  return ({ broadcast_safe: "广播安全", balanced: "平衡模式", creative: "创意模式" }[profile] || profile || "—");
}

function render(jobs) {
  document.getElementById("taskManagerSummary").textContent = summary(jobs);
  document.getElementById("taskManagerUpdated").textContent = `更新于 ${new Date().toLocaleTimeString()}`;
  rowTarget().innerHTML = jobs.map(job => {
    const active = job.status === "processing" || job.status === "pending";
    const detail = job.execution || {};
    const progress = Math.max(0, Math.min(100, Number(job.progress) || 0));
    return `<tr><td><a class="task-id-link" href="/tasks?job=${encodeURIComponent(job.job_id)}"><strong>${esc(job.job_id)}</strong></a><small>${esc(job.job_type)}</small></td>
      <td><span class="task-status ${esc(job.status)}">${esc(job.status)}</span><progress value="${progress}" max="100"></progress><small>${progress}% · ${esc(statusText(job))}</small></td>
      <td>${esc(detail.mode || "—")}<small>${esc(qualityProfileLabel(detail.quality_profile))}</small></td><td>${esc(detail.model || "—")}</td><td title="${esc(detail.source_name || "")}">${esc(detail.source_name || "—")}</td>
      <td>${esc(new Date(job.updated_at).toLocaleString())}</td><td>${active ? `<button class="task-cancel-btn" data-cancel="${esc(job.job_id)}">取消</button>` : "—"}</td></tr>`;
  }).join("") || '<tr><td colspan="7" class="empty-cell">没有符合筛选条件的任务</td></tr>';
}

function badge(label, value, kind = "") {
  return `<div class="task-qc-card ${kind}"><span>${esc(label)}</span><strong>${esc(value ?? "—")}</strong></div>`;
}

function formatNumber(value, digits = 2) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : "-";
}

function reviewCategoryLabel(category) {
  return ({
    large_exposure_correction: "曝光修正",
    highlight_clipping: "高光裁切",
    skin_safety: "肤色安全",
    continuity_luminance: "亮度连续性",
    continuity_white_balance: "白平衡连续性",
  }[category] || category || "复核");
}

function canAutoRepair(category) {
  return ["large_exposure_correction", "highlight_clipping", "skin_safety", "continuity_luminance", "continuity_white_balance"].includes(category);
}

function reviewParameters(item) {
  const category = item.category;
  if (category === "large_exposure_correction") {
    const applied = formatNumber(item.applied_ev);
    const requested = formatNumber(item.requested_ev);
    const anchor = item.anchor ? `anchor=${item.anchor}` : "";
    const limited = item.highlight_limited == null ? "" : (item.highlight_limited ? "高光受限" : "未触发高光限制");
    return `已应用 ${applied} EV · 测量请求 ${requested} EV${anchor ? ` · ${anchor}` : ""}${limited ? ` · ${limited}` : ""}`;
  }
  if (category === "skin_safety") {
    const parts = [];
    if (item.skin_delta_e2000 != null || item.delta_e2000 != null) parts.push(`ΔE2000 ${formatNumber(item.skin_delta_e2000 ?? item.delta_e2000)}`);
    if (item.skin_hue_rotation_deg != null || item.hue_rotation_deg != null) parts.push(`肤色旋转 ${formatNumber(item.skin_hue_rotation_deg ?? item.hue_rotation_deg, 1)}°`);
    if (item.skin_delta_lightness != null || item.delta_lightness != null) parts.push(`ΔL* ${formatNumber(item.skin_delta_lightness ?? item.delta_lightness, 1)}`);
    if (item.skin_delta_chroma != null || item.delta_chroma != null) parts.push(`ΔC* ${formatNumber(item.skin_delta_chroma ?? item.delta_chroma, 1)}`);
    if (item.skin_delta_hue != null || item.delta_hue != null) parts.push(`Δh* ${formatNumber(item.skin_delta_hue ?? item.delta_hue, 1)}`);
    if (item.category_detail) parts.push(item.category_detail);
    return parts.join(" · ") || (item.suggested_action ? `建议 ${item.suggested_action}` : "需人工确认肤色");
  }
  if (category === "continuity_white_balance") {
    const parts = [];
    if (item.delta != null) parts.push(`跳变 ${formatNumber(item.delta, 1)} ${item.unit || ""}`.trim());
    if (item.source_delta != null) parts.push(`源素材 ${formatNumber(item.source_delta, 1)} ${item.unit || ""}`.trim());
    if (item.worsened_by != null) parts.push(`调色增加 ${formatNumber(item.worsened_by, 1)} ${item.unit || ""}`.trim());
    if (item.within_scene_group != null) parts.push(item.within_scene_group ? "组内" : "组间边界");
    return parts.join(" · ") || (item.suggested_action ? `建议 ${item.suggested_action}` : "需检查边界匹配");
  }
  return item.suggested_action ? `建议 ${item.suggested_action}` : "";
}

function renderTaskDetail(detail) {
  const panel = document.getElementById("taskDetailPanel");
  const execution = detail.execution || {}, result = detail.result_summary || {};
  const renderQc = detail.quality_report || {}, projectQc = detail.project_quality_report || {};
  const decision = projectQc.final_decision || (renderQc.passed === false ? "FAIL" : detail.status === "completed" ? "PASS" : detail.status);
  const counts = detail.review_category_counts || {};
  document.getElementById("taskDetailTitle").textContent = `${detail.job_id} · ${detail.job_type}`;
  document.getElementById("taskDetailSubtitle").textContent = `${detail.status} · 最近更新 ${new Date(detail.updated_at).toLocaleString()}`;
  document.getElementById("taskDetailMeta").innerHTML = [
    ["处理模式", execution.mode], ["模型 / 引擎", execution.model], ["输入素材", execution.source_name],
    ["自动质量档位", qualityProfileLabel(execution.quality_profile || result.quality_profile)],
    ["项目 revision", result.revision], ["镜头 / 场景组", `${result.shot_count ?? "—"} / ${result.scene_group_count ?? "—"}`],
    ["人脸选择性渲染", result.face_selective_render == null ? "—" : (result.face_selective_render ? "启用" : "未启用")],
    ["QC 批量修复", result.repair_count == null ? "—" : `${result.repair_count} 项 / ${result.updated_shot_count || 0} 镜头`],
  ].map(item => `<div><span>${esc(item[0])}</span><strong>${esc(item[1])}</strong></div>`).join("");
  document.getElementById("taskDetailOutput").innerHTML = detail.output_url
    ? `<a class="secondary-btn" href="${esc(detail.output_url)}" target="_blank">打开结果视频</a><button class="secondary-btn" type="button" onclick="compareWithRecentProject('${esc(detail.job_id)}')">与最近项目对比</button><span>${esc(detail.output_path)}</span>`
    : `<span class="empty-note">该任务没有可播放的视频输出</span>`;
  managerState.detail = detail;
  // A project_recipe task is itself the current GradePlan revision. When a
  // render/revision task is opened, keep following its source project so the
  // next edit chains from the newest revision instead of branching again.
  managerState.projectJobId = detail.project_recipe ? detail.job_id : (detail.project_job_id || null);
  setupTaskEditor(detail);
  document.getElementById("taskDetailQc").innerHTML = [
    badge("任务状态", detail.status, detail.status), badge("渲染 QC", renderQc.passed === true ? "PASS" : renderQc.passed === false ? "FAIL" : "—", renderQc.passed ? "pass" : ""),
    badge("项目判定", decision, decision === "NEEDS_REVIEW" ? "review" : decision === "FAIL" ? "fail" : "pass"), badge("输出可读", renderQc.output_readable == null ? "—" : (renderQc.output_readable ? "是" : "否")),
    badge("时长 / 帧率", `${renderQc.duration_within_one_frame ? "正常" : "需检查"} / ${renderQc.fps_matches ? "匹配" : "需检查"}`), badge("音频 / 元数据", `${renderQc.audio_preserved ? "保留" : "检查"} / ${renderQc.metadata_ok ? "正常" : "检查"}`),
    badge("复核项", Object.values(counts).reduce((sum, value) => sum + Number(value || 0), 0), "review"), badge("安全 fallback", result.safety_fallback_count || 0),
  ].join("");
  const review = projectQc.review_items || [];
  document.getElementById("taskReviewDetails").hidden = !review.length;
  const batchButton = document.getElementById("taskBatchRepairBtn");
  const batchCount = review.filter(item => canAutoRepair(item.category) && (item.shot_id || item.to_shot_id || item.scene_id)).length;
  if (batchButton) {
    batchButton.hidden = batchCount === 0;
    batchButton.textContent = `批量自动修复并预览（${batchCount}项）`;
    batchButton.disabled = false;
  }
  document.getElementById("taskReviewItems").innerHTML = review.map((item, index) => {
    const locator = item.timecode || item.shot_id || item.scene_id || "";
    const repair = canAutoRepair(item.category) ? `<button class="ghost-btn task-auto-repair" type="button" data-repair-category="${esc(item.category)}" data-repair-shot="${esc(item.shot_id || item.to_shot_id || item.scene_id || "")}">自动修复</button>` : "";
    return `<div class="task-review-item"><strong>${esc(item.category || "review")}</strong><span>${esc(locator)}</span><p>${esc(item.reason || item.suggested_action || "")}</p><span class="task-review-actions"><button class="ghost-btn task-jump-review" type="button" data-review-shot="${esc(item.shot_id || item.to_shot_id || item.scene_id || "")}" data-review-time="${Number(item.start_time || 0)}">定位</button><button class="ghost-btn task-copy-review" type="button" data-review-index="${index}" data-review-locator="${esc(locator)}">复制定位</button>${repair}</span></div>`;
  }).join("");
  document.querySelectorAll("#taskReviewItems .task-review-item").forEach((node, index) => {
    const item = review[index];
    if (!item) return;
    const label = node.querySelector("strong");
    if (label) label.textContent = reviewCategoryLabel(item.category);
    const params = document.createElement("div");
    params.className = "task-review-params";
    params.textContent = reviewParameters(item);
    node.insertBefore(params, node.querySelector(".task-review-actions"));
    const jump = node.querySelector(".task-jump-review");
    if (jump && !jump.dataset.reviewShot) jump.dataset.reviewShot = item.from_shot_id || item.to_shot_id || "";
    const actions = node.querySelectorAll(".task-jump-review, .task-copy-review");
    if (actions[0]) actions[0].textContent = "定位";
    if (actions[1]) actions[1].textContent = "复制定位";
  });
  panel.hidden = false;
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

function clock(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  return `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(Math.floor(value % 60)).padStart(2, "0")}`;
}

function editorShot(shotId) {
  return (managerState.detail?.project_recipe?.shots || []).find(shot => shot.shot_id === shotId || shot.scene_id === shotId);
}

function setEditorValue(id, value, digits = 2) {
  const input = document.getElementById(id);
  if (!input) return;
  const number = Number(value);
  input.value = Number.isFinite(number) ? number.toFixed(digits) : "";
}

function setEditorVector(prefix, values, digits = 2, fallback = [0, 0, 0]) {
  const vector = Array.isArray(values) && values.length === 3 ? values : fallback;
  ["R", "G", "B"].forEach((channel, index) => setEditorValue(`task${prefix}${channel}Input`, vector[index], digits));
}

function editorNumber(id, fallback = 0) {
  const value = Number(document.getElementById(id)?.value);
  return Number.isFinite(value) ? value : fallback;
}

function editorVector(prefix, fallback) {
  return ["R", "G", "B"].map((channel, index) => editorNumber(`task${prefix}${channel}Input`, fallback[index]));
}

function editorGradeUpdate(shotId) {
  return {
    shot_id: shotId,
    exposure: editorNumber("taskExposureInput"), temperature: editorNumber("taskTemperatureInput"),
    tint: editorNumber("taskTintInput"), contrast: editorNumber("taskContrastInput", 1),
    pivot: editorNumber("taskPivotInput", 0.18), saturation: editorNumber("taskSaturationInput", 1),
    highlight_rolloff: editorNumber("taskHighlightRolloffInput"), highlight_softness: editorNumber("taskHighlightSoftnessInput"),
    color_density: editorNumber("taskColorDensityInput"), skin_protection: editorNumber("taskSkinProtectionInput"),
    gamut_protection: editorNumber("taskGamutProtectionInput"), rgb_gains: editorVector("Rgb", [1, 1, 1]),
    lift: editorVector("Lift", [0, 0, 0]), gamma: editorVector("Gamma", [1, 1, 1]), gain: editorVector("Gain", [1, 1, 1]),
    look_strength: editorNumber("taskLookStrengthInput"),
  };
}

function selectEditorShot(shotId, atTime = null) {
  const shot = editorShot(shotId);
  if (!shot) return;
  managerState.selectedShotId = shot.shot_id || shot.scene_id;
  const video = document.getElementById("taskDetailVideo");
  const targetTime = atTime == null ? Number(shot.start_time || 0) : Number(atTime);
  if (video) { video.currentTime = targetTime; video.play().catch(() => {}); }
  document.getElementById("taskSelectedShot").textContent = `${shot.shot_id || shot.scene_id} · ${clock(shot.start_time)}–${clock(shot.end_time)}`;
  document.getElementById("taskExposureInput").value = Number(shot.exposure || 0).toFixed(2);
  document.getElementById("taskTemperatureInput").value = Number(shot.temperature || 0).toFixed(0);
  document.getElementById("taskLookStrengthInput").value = Number(shot.look_strength || 0).toFixed(2);
  setEditorValue("taskTintInput", shot.tint, 2); setEditorValue("taskContrastInput", shot.contrast ?? 1, 2);
  setEditorValue("taskPivotInput", shot.pivot ?? 0.18, 2); setEditorValue("taskSaturationInput", shot.saturation ?? 1, 2);
  setEditorValue("taskHighlightRolloffInput", shot.highlight_rolloff, 2); setEditorValue("taskHighlightSoftnessInput", shot.highlight_softness, 2);
  setEditorValue("taskColorDensityInput", shot.color_density, 2); setEditorValue("taskSkinProtectionInput", shot.skin_protection, 2);
  setEditorValue("taskGamutProtectionInput", shot.gamut_protection, 2);
  setEditorVector("Rgb", shot.rgb_gains, 3, [1, 1, 1]); setEditorVector("Lift", shot.lift, 3, [0, 0, 0]);
  setEditorVector("Gamma", shot.gamma, 3, [1, 1, 1]); setEditorVector("Gain", shot.gain, 3, [1, 1, 1]);
  const effective = shot.effective || {};
  const effectiveSummary = document.getElementById("taskEffectiveSummary");
  if (effectiveSummary) {
    const sources = effective.parameter_sources || {};
    const confidence = effective.parameter_confidence || {};
    const sourceLabel = sources.contrast === "quality_profile" ? "质量策略" : sources.contrast === "look_package" ? "Look" : "自动分析";
    const confidenceValue = Number(confidence.contrast);
    const confidenceLabel = Number.isFinite(confidenceValue) ? ` · 置信度 ${Math.round(confidenceValue * 100)}%` : "";
    effectiveSummary.textContent = `实际渲染：Look「${effective.look_id || "自然广播"}」 · 对比度 ${Number(effective.contrast ?? shot.contrast ?? 1).toFixed(2)} · 饱和度 ${Number(effective.saturation ?? shot.saturation ?? 1).toFixed(2)} · 色调偏移 ${Number(effective.tint ?? shot.tint ?? 0).toFixed(2)} · 高光滚降 ${Number(effective.highlight_rolloff ?? shot.highlight_rolloff ?? 0).toFixed(2)} · 肤色保护 ${Number(effective.skin_protection ?? shot.skin_protection ?? 0).toFixed(2)} · 色域保护 ${Number(effective.gamut_protection ?? shot.gamut_protection ?? 0).toFixed(2)} · 来源 ${sourceLabel}${confidenceLabel}`;
  }
  const provenance = document.getElementById("taskParameterProvenance");
  if (provenance) {
    const sourceNames = {
      skin_or_frame_analysis: "肤色/画面分析", white_balance_analysis: "白平衡分析",
      dynamic_range_analysis: "动态范围分析", chroma_analysis: "色彩分析",
      highlight_headroom_analysis: "高光余量分析", derived_from_headroom: "由高光余量推导",
      look_package: "Look 预设", quality_profile: "质量策略", qc_auto_repair: "QC 自动修复",
      safe_default: "安全默认值", neutral_default: "中性默认值",
    };
    const labels = {
      exposure: "曝光", temperature: "色温", tint: "Tint", contrast: "对比度", pivot: "Pivot",
      saturation: "饱和度", highlight_rolloff: "高光滚降", highlight_softness: "高光柔化",
      skin_protection: "肤色保护", gamut_protection: "色域保护", color_density: "色彩密度",
      rgb_gains: "RGB 增益", lift: "Lift", gamma: "Gamma", gain: "Gain",
    };
    provenance.innerHTML = Object.keys(labels).map(key => {
      const source = sources[key] || shot.parameter_sources?.[key] || "—";
      const value = Number(confidence[key] ?? shot.parameter_confidence?.[key]);
      const confidenceText = Number.isFinite(value) ? `${Math.round(value * 100)}%` : "—";
      return `<div><span>${labels[key]}</span><strong>${sourceNames[source] || source}</strong><em>${confidenceText}</em></div>`;
    }).join("");
  }
  document.getElementById("applyTaskGradeBtn").disabled = false;
  document.getElementById("renderTaskPreviewBtn").disabled = false;
  document.querySelectorAll(".task-review-marker").forEach(item => item.classList.toggle("active", item.dataset.shotId === managerState.selectedShotId));
}

function setupTaskEditor(detail) {
  const recipe = detail.project_recipe;
  const workspace = document.getElementById("taskEditWorkspace");
  if (!recipe?.shots?.length || !detail.output_url || !detail.project_job_id) { workspace.hidden = true; return; }
  workspace.hidden = false;
  const video = document.getElementById("taskDetailVideo");
  if (video.src !== new URL(detail.output_url, window.location.href).href) video.src = detail.output_url;
  video.onloadedmetadata = () => {
    document.getElementById("taskTimelineDuration").textContent = clock(video.duration);
    document.getElementById("taskViewerEmpty").hidden = true;
  };
  video.ontimeupdate = () => {
    document.getElementById("taskTimelineCurrent").textContent = clock(video.currentTime);
    document.getElementById("taskTimelineSeek").value = video.duration ? Math.round(video.currentTime / video.duration * 1000) : 0;
  };
  const review = detail.project_quality_report?.review_items || [];
  const markers = [];
  const seen = new Set();
  for (const item of review) {
    const shotId = item.shot_id || item.scene_id;
    if (!shotId || seen.has(shotId)) continue;
    seen.add(shotId); markers.push({ shotId, time: item.start_time ?? editorShot(shotId)?.start_time ?? 0, label: item.timecode || shotId });
  }
  const visibleMarkers = markers.length ? markers : recipe.shots.slice(0, 80).map(shot => ({ shotId: shot.shot_id, time: shot.start_time, label: shot.shot_id }));
  document.getElementById("taskReviewMarkers").innerHTML = visibleMarkers.map(marker => `<button class="task-review-marker" type="button" data-shot-id="${esc(marker.shotId)}" data-time="${Number(marker.time) || 0}">${esc(marker.label)}</button>`).join("");
  if (!managerState.selectedShotId || !editorShot(managerState.selectedShotId)) selectEditorShot(visibleMarkers[0]?.shotId, visibleMarkers[0]?.time);
}

async function waitForTask(jobId, statusNode) {
  while (true) {
    const job = await taskApi(`/v1/jobs/${encodeURIComponent(jobId)}`, { cache: "no-store" });
    if (statusNode) statusNode.textContent = `${job.status} · ${job.progress ?? 0}%`;
    if (job.status === "completed") return job;
    if (["failed", "cancelled"].includes(job.status)) throw new Error(job.error_message || `任务${job.status}`);
    await new Promise(resolve => setTimeout(resolve, 800));
  }
}

async function applyTaskGrade() {
  const detail = managerState.detail, shot = editorShot(managerState.selectedShotId);
  if (!detail?.project_job_id || !shot) return;
  const status = document.getElementById("taskEditStatus");
  const button = document.getElementById("applyTaskGradeBtn");
  button.disabled = true; status.textContent = "正在保存 GradePlan…";
  try {
    const previousOutputUrl = detail.output_url, previousOutputPath = detail.output_path;
    const created = await taskApi("/v1/color/grade-plan/revise", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      project_job_id: managerState.projectJobId || detail.project_job_id,
      expected_revision: detail.project_recipe.revision,
      shot_updates: [editorGradeUpdate(shot.shot_id)],
      actor: "task-detail-ui",
    })});
    await waitForTask(created.job_id, status);
    await openTaskDetail(created.job_id);
    managerState.projectJobId = created.job_id;
    // A GradePlan revision has no new video yet. Keep the prior preview in the
    // viewer so the user can make several adjustments before re-rendering.
    if (!managerState.detail.output_url && previousOutputUrl) {
      managerState.detail.output_url = previousOutputUrl;
      managerState.detail.output_path = previousOutputPath;
      setupTaskEditor(managerState.detail);
    }
    document.getElementById("taskEditStatus").textContent = `已保存 revision ${managerState.detail.project_recipe?.revision || ""}，可重新生成预览`;
  } catch (error) { status.textContent = `保存失败：${error.message}`; }
  finally { button.disabled = !editorShot(managerState.selectedShotId); }
}

async function renderTaskPreview() {
  const detail = managerState.detail, recipe = detail?.project_recipe;
  const projectJobId = managerState.projectJobId || detail?.project_job_id;
  if (!recipe?.source_path || !projectJobId) return;
  const status = document.getElementById("taskEditStatus"), button = document.getElementById("renderTaskPreviewBtn");
  button.disabled = true; status.textContent = "正在创建项目预览任务…";
  try {
    const created = await taskApi("/v1/color/project-render", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project_job_id: projectJobId, input_path: recipe.source_path, target_height: 720, preview: true, allow_unapproved: false, quality_profile: recipe.quality_profile }) });
    await waitForTask(created.job_id, status);
    await openTaskDetail(created.job_id);
    status.textContent = "新预览已完成";
  } catch (error) { status.textContent = `预览失败：${error.message}`; }
  finally { button.disabled = false; }
}

async function autoRepairReview(button) {
  const detail = managerState.detail;
  const recipe = detail?.project_recipe;
  const projectJobId = managerState.projectJobId || detail?.project_job_id;
  if (!recipe || !projectJobId) return;
  const category = button.dataset.repairCategory;
  const shotId = button.dataset.repairShot;
  const status = document.getElementById("taskEditStatus");
  const previousOutputUrl = detail.output_url, previousOutputPath = detail.output_path;
  button.disabled = true;
  status.textContent = "正在应用 QC 自动修复…";
  try {
    const created = await taskApi("/v1/color/timeline-adjust", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      project_job_id: projectJobId, expected_revision: recipe.revision, scope: "shot", shot_id: shotId,
      operation: "auto_repair", repair_category: category, actor: "task-qc-auto-repair",
    })});
    await waitForTask(created.job_id, status);
    await openTaskDetail(created.job_id);
    managerState.projectJobId = created.job_id;
    if (!managerState.detail.output_url && previousOutputUrl) {
      managerState.detail.output_url = previousOutputUrl;
      managerState.detail.output_path = previousOutputPath;
      setupTaskEditor(managerState.detail);
    }
    status.textContent = `已应用 ${reviewCategoryLabel(category)} 自动修复，请重新生成预览`;
  } catch (error) {
    status.textContent = `自动修复失败：${error.message}`;
  } finally {
    button.disabled = false;
  }
}

async function batchAutoRepairReview() {
  const detail = managerState.detail;
  const recipe = detail?.project_recipe;
  const projectJobId = managerState.projectJobId || detail?.project_job_id;
  if (!recipe || !projectJobId) return;
  const status = document.getElementById("taskEditStatus");
  const button = document.getElementById("taskBatchRepairBtn");
  const items = (detail.project_quality_report?.review_items || [])
    .filter(item => canAutoRepair(item.category))
    .map(item => ({ shot_id: item.shot_id || item.to_shot_id || item.scene_id, category: item.category }))
    .filter(item => item.shot_id);
  const repairs = [...new Map(items.map(item => [`${item.shot_id}:${item.category}`, item])).values()];
  if (!repairs.length) return;
  button.disabled = true;
  status.textContent = `正在批量修复 ${repairs.length} 项 QC…`;
  try {
    const created = await taskApi("/v1/color/qc-auto-repair", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_job_id: projectJobId,
        expected_revision: recipe.revision,
        repairs,
        actor: "task-qc-batch-ui",
      }),
    });
    await waitForTask(created.job_id, status);
    await openTaskDetail(created.job_id);
    status.textContent = `已批量修复 ${repairs.length} 项，正在生成预览…`;
    await renderTaskPreview();
    status.textContent = `批量修复完成：已生成新预览（${repairs.length} 项）`;
  } catch (error) {
    status.textContent = `批量修复失败：${error.message}`;
  } finally {
    button.disabled = false;
  }
}

function compactQc(detail) {
  const qc = detail.project_quality_report || detail.quality_report || {};
  const decision = qc.final_decision || (qc.passed === false ? "FAIL" : qc.passed === true ? "PASS" : detail.status);
  return [badge("判定", decision, decision === "NEEDS_REVIEW" ? "review" : decision === "FAIL" ? "fail" : "pass"), badge("输出", qc.output_readable === false ? "不可读" : "可读"), badge("复核项", (qc.review_items || []).length, "review")].join("");
}

async function renderTaskComparison(jobA, jobB) {
  const [a, b] = await Promise.all([taskApi(`/v1/jobs/${encodeURIComponent(jobA)}/detail`, { cache: "no-store" }), taskApi(`/v1/jobs/${encodeURIComponent(jobB)}/detail`, { cache: "no-store" })]);
  if (!a.output_url || !b.output_url) throw new Error("两个任务都必须有视频输出");
  document.getElementById("compareTitleA").textContent = `${a.job_id} · ${a.execution?.mode || "A"}`;
  document.getElementById("compareTitleB").textContent = `${b.job_id} · ${b.execution?.mode || "B"}`;
  const videoA = document.getElementById("compareVideoA"), videoB = document.getElementById("compareVideoB");
  videoA.src = a.output_url; videoB.src = b.output_url;
  videoA.onplay = () => videoB.play().catch(() => {}); videoB.onplay = () => videoA.play().catch(() => {});
  videoA.ontimeupdate = () => { if (Math.abs(videoB.currentTime - videoA.currentTime) > 0.15) videoB.currentTime = videoA.currentTime; };
  document.getElementById("compareQcA").innerHTML = compactQc(a); document.getElementById("compareQcB").innerHTML = compactQc(b);
  document.getElementById("taskComparePanel").hidden = false;
  document.getElementById("taskComparePanel").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function compareWithRecentProject(jobId) {
  try {
    const jobs = (await taskApi("/v1/jobs?limit=200", { cache: "no-store" })).jobs || [];
    const other = jobs.find(job => job.job_type === "project_render" && job.status === "completed" && job.job_id !== jobId && job.execution);
    if (!other) throw new Error("没有找到另一条已完成的项目输出");
    await renderTaskComparison(jobId, other.job_id);
  } catch (error) { alert(`无法建立对比：${error.message}`); }
}

async function openTaskDetail(jobId) {
  try { renderTaskDetail(await taskApi(`/v1/jobs/${encodeURIComponent(jobId)}/detail`, { cache: "no-store" })); }
  catch (error) { alert(`读取任务详情失败：${error.message}`); }
}

async function refreshTasks() {
  try {
    const query = managerState.statuses ? `?statuses=${encodeURIComponent(managerState.statuses)}&limit=200` : "?limit=200";
    render((await taskApi(`/v1/jobs${query}`, { cache: "no-store" })).jobs || []);
  } catch (error) {
    rowTarget().innerHTML = `<tr><td colspan="7" class="empty-cell">读取任务失败：${esc(error.message)}</td></tr>`;
  }
}

document.getElementById("refreshTasksBtn").addEventListener("click", refreshTasks);
document.getElementById("closeTaskDetail").addEventListener("click", () => { document.getElementById("taskDetailPanel").hidden = true; });
document.getElementById("closeTaskCompare").addEventListener("click", () => { document.getElementById("taskComparePanel").hidden = true; });
document.getElementById("taskReviewMarkers").addEventListener("click", event => {
  const marker = event.target.closest("button.task-review-marker");
  if (marker) selectEditorShot(marker.dataset.shotId, Number(marker.dataset.time));
});
document.getElementById("taskTimelineSeek").addEventListener("input", event => {
  const video = document.getElementById("taskDetailVideo");
  if (video.duration) video.currentTime = Number(event.target.value) / 1000 * video.duration;
});
document.getElementById("applyTaskGradeBtn").addEventListener("click", applyTaskGrade);
document.getElementById("renderTaskPreviewBtn").addEventListener("click", renderTaskPreview);
document.getElementById("taskBatchRepairBtn").addEventListener("click", batchAutoRepairReview);
rowTarget().addEventListener("click", event => {
  const link = event.target.closest("a.task-id-link");
  if (!link) return;
  event.preventDefault();
  const jobId = new URL(link.href, window.location.href).searchParams.get("job");
  if (!jobId) return;
  history.pushState({ jobId }, "", `/tasks?job=${encodeURIComponent(jobId)}`);
  openTaskDetail(jobId);
});
document.getElementById("taskReviewItems").addEventListener("click", async event => {
  const jump = event.target.closest("button.task-jump-review");
  if (jump) { selectEditorShot(jump.dataset.reviewShot, Number(jump.dataset.reviewTime)); return; }
  const repair = event.target.closest("button.task-auto-repair");
  if (repair) { await autoRepairReview(repair); return; }
  const button = event.target.closest("button.task-copy-review");
  if (!button) return;
  try { await navigator.clipboard.writeText(button.dataset.reviewLocator || ""); button.textContent = "已复制"; setTimeout(() => { button.textContent = "复制定位"; }, 1200); }
  catch { window.prompt("复制下面的定位信息", button.dataset.reviewLocator || ""); }
});
document.getElementById("taskFilters").addEventListener("click", event => {
  const button = event.target.closest("button[data-status]"); if (!button) return;
  managerState.statuses = button.dataset.status;
  document.querySelectorAll("#taskFilters button").forEach(item => item.classList.toggle("active", item === button));
  refreshTasks();
});
rowTarget().addEventListener("click", async event => {
  const button = event.target.closest("button[data-cancel]"); if (!button) return;
  const jobId = button.dataset.cancel;
  if (!window.confirm(`确认取消任务 ${jobId}？正在编码或分析的任务会尽快安全停止。`)) return;
  button.disabled = true; button.textContent = "取消中…";
  try { await taskApi(`/v1/jobs/${jobId}`, { method: "DELETE" }); } catch (error) { alert(`取消失败：${error.message}`); }
  await refreshTasks();
});
refreshTasks();
managerState.timer = setInterval(refreshTasks, 2500);
const initialJob = new URLSearchParams(window.location.search).get("job");
if (initialJob) openTaskDetail(initialJob);
const compareIds = (new URLSearchParams(window.location.search).get("compare") || "").split(",").filter(Boolean);
if (compareIds.length === 2) renderTaskComparison(compareIds[0], compareIds[1]).catch(error => alert(`无法建立对比：${error.message}`));
window.addEventListener("popstate", () => {
  const jobId = new URLSearchParams(window.location.search).get("job");
  if (jobId) openTaskDetail(jobId); else document.getElementById("taskDetailPanel").hidden = true;
});
