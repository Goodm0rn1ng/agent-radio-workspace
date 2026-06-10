const state = {
  jobs: [],
  metrics: [],
  scheduledPrograms: [],
  profiles: [],
  collections: [],
  pendingSegments: [],
  librarySegments: [],
  artifactsByJob: {},
  artifactFilesByPath: {},
  artifactPreviewByPath: {},
  jobStatusFilter: "all",
  jobSearch: "",
  librarySearch: "",
  selectedJobId: null,
  reconnectTimer: null,
  reconnectDelayMs: 1800,
};

const apiBasePath = (() => {
  const marker = "/radio";
  const path = window.location.pathname;
  const index = path.indexOf(marker);
  return index >= 0 ? path.slice(0, index + marker.length) : "";
})();

function apiPath(path) {
  if (!path || /^https?:\/\//.test(path)) {
    return path;
  }
  return `${apiBasePath}${path}`;
}

const stageLabels = {
  queued: "排队中",
  waiting: "等待开始",
  download: "获取音频",
  recording: "录制中",
  pipeline_waiting: "等待处理",
  pipeline: "转写总结",
  distributed: "已推送",
  failed: "失败",
  canceled: "已取消",
};

const stageProgress = {
  queued: 4,
  waiting: 10,
  download: 28,
  recording: 38,
  pipeline_waiting: 44,
  pipeline: 78,
  distributed: 100,
  failed: 100,
  canceled: 100,
};

const credentialLabels = {
  gemini_api_key: "GEMINI_API_KEY",
  groq_api_key: "GROQ_API_KEY",
  deepseek_api_key: "DEEPSEEK_API_KEY",
  anthropic_api_key: "ANTHROPIC_API_KEY",
  telegram_bot_token: "TELEGRAM_BOT_TOKEN",
  telegram_chat_id: "TELEGRAM_CHAT_ID",
};

const defaultTranslationPrompt = `角色：
你是一位熟悉日本广播内容的日语翻译官。请将输入段落翻译为自然流畅的简体中文，保留广播口语感和节目氛围。

术语库（必须优先遵循）：
{terminology}

输出格式要求：
- 只输出 JSON，不要额外说明。
- 输入段数必须等于输出段数，每个 i 必须一一对应。
{
  "segments": [
    {"i": 0, "zh": "中文翻译..."}
  ]
}

--- 输入 ---
{input_json}`;

const defaultSummaryPrompt = `你是一位日本广播节目编辑，正在整理一期结构化复盘笔记。

下面是一期节目的中日双语逐字稿，每行格式为：
[HH:MM:SS] [日文原文] / [中文翻译]

请输出严格 JSON，包含 summary、sections、key_topics、highlights。
summary 控制在 {max_summary_chars} 字以内。sections 按节目流程整理，覆盖开场、主要话题/来信、告知和结尾。

术语库：
{terminology}

常驻环节库：
{segments_library}

往期回忆：
{recent_history}

输出格式：
{
  "summary": "...",
  "sections": [
    {
      "title": "",
      "title_ja": "オープニング",
      "intro": "节目开场。",
      "is_recurring": false,
      "time_range": "00:00:00-00:03:20",
      "content": "...",
      "listener_mail_from": "",
      "listener_mail_ja": "",
      "listener_mail": "",
      "member_reactions": ["..."],
      "music": [],
      "notes": []
    }
  ],
  "key_topics": ["..."],
  "highlights": []
}

--- 双语逐字稿 ---
{transcript}`;

document.addEventListener("DOMContentLoaded", () => {
  bindTabs();
  bindProfiles();
  bindKnowledge();
  bindVideoForm();
  bindYouTubeForm();
  bindRadikoForm();
  bindScheduleForm();
  bindJobFilters();
  bindDrawer();
  syncDuration("youtube");
  syncDuration("radiko", 30);
  setDefaultStartTime();
  loadCredentials();
  loadProfiles();
  loadKnowledge();
  loadScheduledPrograms();
  loadRecentMetrics();
  hydrateProfileTemplate();
  connectJobsSocket();
});

function bindTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((item) => item.classList.remove("active"));
      tab.classList.add("active");
      document.getElementById(`${tab.dataset.tab}Form`).classList.add("active");
    });
  });
}

function bindProfiles() {
  document.getElementById("profileList").addEventListener("click", (event) => {
    const button = event.target.closest("[data-profile-target]");
    if (!button) {
      return;
    }
    const selectId = `${button.dataset.profileTarget}Profile`;
    document.getElementById(selectId).value = button.dataset.profileId;
    resetCollectionToAuto(button.dataset.profileTarget);
    showToast(`已为${sourceTargetLabel(button.dataset.profileTarget)}选择「${button.dataset.profileName}」`);
  });

  document.getElementById("profileForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = event.currentTarget.querySelector('button[type="submit"]');
    setButtonPending(submitButton, true);
    try {
      const payload = {
        id: document.getElementById("profileId").value.trim(),
        name: document.getElementById("profileName").value.trim(),
        description: document.getElementById("profileDescription").value.trim(),
        terminology_path: document.getElementById("profileTerminologyPath").value.trim() || null,
        translation_prompt: document.getElementById("profileTranslationPrompt").value.trim(),
        summary_prompt: document.getElementById("profileSummaryPrompt").value.trim(),
      };
      if (!payload.id || !payload.name || !payload.translation_prompt || !payload.summary_prompt) {
        showToast("请填写方案 ID、名称、翻译提示词和总结提示词");
        return;
      }
      await apiFetch("/api/profiles", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      event.currentTarget.reset();
      hydrateProfileTemplate();
      await loadProfiles(payload.id);
      showToast("节目处理方案已保存");
    } finally {
      setButtonPending(submitButton, false);
    }
  });
}

function bindKnowledge() {
  document.getElementById("refreshKnowledge").addEventListener("click", () => loadKnowledge());
  document.getElementById("librarySearch").addEventListener("input", (event) => {
    state.librarySearch = event.currentTarget.value.trim().toLowerCase();
    renderKnowledge();
  });
  document.getElementById("pendingSegments").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-pending-action]");
    if (!button) {
      return;
    }
    const action = button.dataset.pendingAction;
    const segmentId = button.dataset.segmentId;
    if (!action || !segmentId) {
      return;
    }
    setButtonPending(button, true);
    try {
      await apiFetch(`/api/knowledge/pending/${encodeURIComponent(segmentId)}/${action}`, {
        method: "POST",
      });
      await loadKnowledge();
      showToast(action === "approve" ? "已收录这个环节" : "已跳过这个环节");
    } finally {
      setButtonPending(button, false);
    }
  });
}

function hydrateProfileTemplate() {
  const terminologyPath = document.getElementById("profileTerminologyPath");
  const translationPrompt = document.getElementById("profileTranslationPrompt");
  const summaryPrompt = document.getElementById("profileSummaryPrompt");
  if (!terminologyPath.value) {
    terminologyPath.value = "config/terminology.yaml";
  }
  if (!translationPrompt.value) {
    translationPrompt.value = defaultTranslationPrompt;
  }
  if (!summaryPrompt.value) {
    summaryPrompt.value = defaultSummaryPrompt;
  }
}

function bindVideoForm() {
  const playlistToggle = document.getElementById("playlistToggle");
  bindCollectionPicker("videoCollection", "videoCollectionCustom");
  playlistToggle.addEventListener("change", () => {
    document.getElementById("playlistFields").hidden = !playlistToggle.checked;
    document.getElementById("playlistPreview").hidden = true;
  });

  document.getElementById("previewPlaylist").addEventListener("click", async () => {
    if (!playlistToggle.checked) {
      showToast("请先打开播放列表范围");
      return;
    }
    const firstUrl = getUrlLines()[0];
    if (!firstUrl) {
      showToast("请先填写视频链接");
      return;
    }
    const items = await apiFetch("/api/playlists/expand", {
      method: "POST",
      body: JSON.stringify({
        playlist_url: firstUrl,
        start_index: Number(document.getElementById("playlistStart").value),
        end_index: Number(document.getElementById("playlistEnd").value),
      }),
    });
    renderPlaylistPreview(items);
    showToast(`已找到 ${items.length} 个播放列表项目`);
  });

  document.getElementById("videoForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = event.currentTarget.querySelector('button[type="submit"]');
    setButtonPending(submitButton, true);
    const urls = getUrlLines();
    if (!urls.length) {
      setButtonPending(submitButton, false);
      showToast("请至少填写一个视频链接");
      return;
    }
    try {
      const payload = {
        fine_translation: document.getElementById("videoFineTranslation").checked,
        keep_audio: document.getElementById("videoKeepAudio").checked,
        profile_id: selectedProfileId("videoProfile"),
        collection_id: selectedCollectionId("videoCollection", "videoCollectionCustom"),
      };
      if (playlistToggle.checked) {
        payload.playlist_url = urls[0];
        payload.playlist_start_index = Number(document.getElementById("playlistStart").value);
        payload.playlist_end_index = Number(document.getElementById("playlistEnd").value);
        payload.title_template = document.getElementById("titleTemplate").value || "{title}";
      } else {
        payload.urls = urls;
      }
      const job = await apiFetch("/api/video-jobs", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      showToast(`任务 ${shortId(job.job_id)} 已加入队列`);
    } finally {
      setButtonPending(submitButton, false);
    }
  });
}

function bindYouTubeForm() {
  const range = document.getElementById("youtubeDurationRange");
  const input = document.getElementById("youtubeDurationInput");
  bindCollectionPicker("youtubeCollection", "youtubeCollectionCustom");
  range.addEventListener("input", () => syncDuration("youtube", range.value));
  input.addEventListener("input", () => syncDuration("youtube", input.value));

  document.getElementById("youtubeForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = event.currentTarget.querySelector('button[type="submit"]');
    setButtonPending(submitButton, true);
    const liveUrl = document.getElementById("youtubeUrl").value.trim();
    if (!liveUrl) {
      setButtonPending(submitButton, false);
      showToast("请填写 YouTube 直播链接");
      return;
    }
    try {
      const startAt = document.getElementById("youtubeStart").value || toDatetimeLocal(new Date());
      const payload = {
        url: liveUrl,
        start_at: toLocalIso(startAt),
        duration_minutes: Number(document.getElementById("youtubeDurationInput").value),
        title: document.getElementById("youtubeTitle").value.trim() || null,
        fine_translation: document.getElementById("youtubeFineTranslation").checked,
        keep_audio: document.getElementById("youtubeKeepAudio").checked,
        detection_timeout_minutes: Number(document.getElementById("youtubeDetectionTimeout").value),
        detection_interval_seconds: Number(document.getElementById("youtubeDetectionInterval").value),
        profile_id: selectedProfileId("youtubeProfile"),
        collection_id: selectedCollectionId("youtubeCollection", "youtubeCollectionCustom"),
      };
      const job = await apiFetch("/api/live-jobs", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      showToast(`YouTube 录制任务 ${shortId(job.job_id)} 已预约`);
    } finally {
      setButtonPending(submitButton, false);
    }
  });
}

function bindRadikoForm() {
  const range = document.getElementById("radikoDurationRange");
  const input = document.getElementById("radikoDurationInput");
  const mode = document.getElementById("radikoMode");
  bindCollectionPicker("radikoCollection", "radikoCollectionCustom");
  range.addEventListener("input", () => syncDuration("radiko", range.value));
  input.addEventListener("input", () => syncDuration("radiko", input.value));
  mode.addEventListener("change", updateRadikoMode);
  updateRadikoMode();

  document.getElementById("radikoForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = event.currentTarget.querySelector('button[type="submit"]');
    setButtonPending(submitButton, true);
    const radikoUrl = document.getElementById("radikoUrl").value.trim();
    const isLiveMode = document.getElementById("radikoMode").value === "live";
    if (!radikoUrl) {
      setButtonPending(submitButton, false);
      showToast("请填写 Radiko 实时或回听链接");
      return;
    }
    if (isLiveMode && !radikoUrl.includes("/live/")) {
      setButtonPending(submitButton, false);
      showToast("实时录制请选择 Radiko /live/ 链接");
      return;
    }
    if (!isLiveMode && !radikoUrl.includes("/ts/")) {
      setButtonPending(submitButton, false);
      showToast("回听处理请选择 Radiko /ts/ time-free 链接");
      return;
    }
    try {
      const startAt = document.getElementById("radikoStart").value;
      const payload = {
        url: radikoUrl,
        start_at: isLiveMode && startAt ? toLocalIso(startAt) : null,
        duration_minutes: Number(document.getElementById("radikoDurationInput").value),
        title: document.getElementById("radikoTitle").value.trim() || null,
        fine_translation: document.getElementById("radikoFineTranslation").checked,
        keep_audio: document.getElementById("radikoKeepAudio").checked,
        profile_id: selectedProfileId("radikoProfile"),
        collection_id: selectedCollectionId("radikoCollection", "radikoCollectionCustom"),
      };
      const job = await apiFetch("/api/radiko-jobs", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      showToast(
        isLiveMode
          ? `Radiko 录制任务 ${shortId(job.job_id)} 已预约`
          : `Radiko 回听任务 ${shortId(job.job_id)} 已创建`,
      );
    } finally {
      setButtonPending(submitButton, false);
    }
  });
}

function updateRadikoMode() {
  const isLiveMode = document.getElementById("radikoMode").value === "live";
  document.getElementById("radikoStartRow").hidden = !isLiveMode;
  document.getElementById("radikoUrl").placeholder = isLiveMode
    ? "https://radiko.jp/#!/live/QRR"
    : "https://radiko.jp/#!/ts/QRR/20260511003000";
  document.getElementById("radikoModeNote").textContent = isLiveMode
    ? "实时录制只接受 /live/ 链接，会先等待指定时间，再开始录制和后续处理。"
    : "回听处理只接受 /ts/ time-free 链接，会立即下载指定时长并进入转写、翻译、总结。";
}

function bindScheduleForm() {
  document.getElementById("scheduleList").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-schedule-profile-save]");
    if (!button) {
      return;
    }
    const item = button.closest(".schedule-item");
    const select = item.querySelector("[data-schedule-profile]");
    setButtonPending(button, true);
    try {
      await apiFetch(`/api/scheduler/programs/${button.dataset.scheduleProfileSave}`, {
        method: "PATCH",
        body: JSON.stringify({ profile_id: select.value || null }),
      });
      await loadScheduledPrograms();
      showToast("定时计划的提示词方案已写入配置；守护进程重启后生效");
    } finally {
      setButtonPending(button, false);
    }
  });

  document.getElementById("scheduleForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = event.currentTarget.querySelector('button[type="submit"]');
    setButtonPending(submitButton, true);
    const name = document.getElementById("scheduleName").value.trim();
    const sourceUrl = document.getElementById("scheduleSourceUrl").value.trim();
    const [hour = "0", minute = "0"] = document.getElementById("scheduleTime").value.split(":");
    if (!name || !sourceUrl) {
      setButtonPending(submitButton, false);
      showToast("请填写节目名称和来源链接");
      return;
    }
    try {
      await apiFetch("/api/scheduler/programs", {
        method: "POST",
        body: JSON.stringify({
          name,
          source_type: document.getElementById("scheduleSourceType").value,
          source_url: sourceUrl,
          schedule: {
            timezone: "Asia/Tokyo",
            day_of_week: document.getElementById("scheduleDay").value,
            hour: Number(hour),
            minute: Number(minute),
          },
          duration_minutes: Number(document.getElementById("scheduleDuration").value),
          profile_id: selectedProfileId("scheduleProfile"),
          enabled: document.getElementById("scheduleEnabled").checked,
          fine_translation: document.getElementById("scheduleFineTranslation").checked,
        }),
      });
      event.currentTarget.reset();
      document.getElementById("scheduleEnabled").checked = true;
      document.getElementById("scheduleTime").value = "00:30";
      document.getElementById("scheduleDuration").value = "30";
      populateProfileSelect("scheduleProfile");
      await loadScheduledPrograms();
      showToast("定时计划已写入配置；守护进程重启后会按新计划运行");
    } finally {
      setButtonPending(submitButton, false);
    }
  });
}

function bindDrawer() {
  document.getElementById("closeDrawer").addEventListener("click", closeDrawer);
  document.getElementById("drawerBody").addEventListener("click", async (event) => {
    const cancelButton = event.target.closest("[data-cancel-job]");
    if (cancelButton) {
      const jobId = cancelButton.dataset.cancelJob;
      if (!jobId || !window.confirm("确定要取消这个任务吗？正在录制或处理的任务会被中断。")) {
        return;
      }
      setButtonPending(cancelButton, true);
      try {
        await apiFetch(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {
          method: "POST",
        });
        showToast("任务已取消");
      } finally {
        setButtonPending(cancelButton, false);
      }
      return;
    }

    const button = event.target.closest("[data-preview-path]");
    if (!button) {
      return;
    }
    const path = button.dataset.previewPath;
    setButtonPending(button, true);
    try {
      await loadArtifactPreview(path);
      refreshDrawer();
    } finally {
      setButtonPending(button, false);
    }
  });
}

function bindJobFilters() {
  document.getElementById("jobStatusFilter").addEventListener("change", (event) => {
    state.jobStatusFilter = event.currentTarget.value;
    renderJobs();
  });
  document.getElementById("jobSearch").addEventListener("input", (event) => {
    state.jobSearch = event.currentTarget.value.trim().toLowerCase();
    renderJobs();
  });
}

async function loadCredentials() {
  const res = await apiFetch("/api/credentials");
  updateCredentialSummary(res.configured);
}

async function loadProfiles(selectedId = null) {
  const profiles = await apiFetch("/api/profiles");
  state.profiles = profiles || [];
  renderProfiles();
  populateProfileSelect("videoProfile", selectedId);
  populateProfileSelect("youtubeProfile", selectedId);
  populateProfileSelect("radikoProfile", selectedId);
  populateProfileSelect("scheduleProfile", selectedId);
  renderScheduler();
  await loadCollections(selectedId);
}

async function loadCollections(selectedId = null) {
  const collections = await apiFetch("/api/collections");
  state.collections = collections || [];
  populateCollectionSelect("videoCollection", selectedId);
  populateCollectionSelect("youtubeCollection", selectedId);
  populateCollectionSelect("radikoCollection", selectedId);
}

async function loadKnowledge() {
  const [pending, library] = await Promise.all([
    apiFetch("/api/knowledge/pending"),
    apiFetch("/api/knowledge/library"),
  ]);
  state.pendingSegments = pending || [];
  state.librarySegments = library || [];
  renderKnowledge();
}

async function loadScheduledPrograms() {
  const programs = await apiFetch("/api/scheduler/programs");
  state.scheduledPrograms = programs || [];
  renderScheduler();
}

async function loadRecentMetrics() {
  const metrics = await apiFetch("/api/metrics/recent?limit=20");
  state.metrics = metrics || [];
  renderMetricsOverview();
}

function connectJobsSocket() {
  const dot = document.getElementById("socketDot");
  const label = document.getElementById("socketState");
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${window.location.host}${apiPath("/api/jobs/ws")}`);

  socket.addEventListener("open", () => {
    dot.classList.add("online");
    label.textContent = "已连接";
    state.reconnectDelayMs = 1800;
  });
  socket.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    state.jobs = payload.jobs || [];
    state.metrics = payload.metrics || [];
    if (payload.credentials) {
      updateCredentialSummary(payload.credentials);
    }
    renderJobs();
    renderMetricsOverview();
    refreshDrawer();
  });
  socket.addEventListener("close", () => {
    dot.classList.remove("online");
    label.textContent = "未连接";
    window.clearTimeout(state.reconnectTimer);
    state.reconnectTimer = window.setTimeout(connectJobsSocket, state.reconnectDelayMs);
    state.reconnectDelayMs = Math.min(Math.round(state.reconnectDelayMs * 1.8), 30000);
  });
}

function renderJobs() {
  const list = document.getElementById("jobsList");
  const jobs = filteredJobs();
  document.getElementById("jobCount").textContent = `${jobs.length}/${state.jobs.length} 个任务`;
  if (!jobs.length) {
    list.innerHTML = `<div class="empty">还没有符合条件的任务。可以从左侧新建第一个处理任务。</div>`;
    return;
  }
  list.innerHTML = "";
  jobs.forEach((job) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "job-card";
    card.addEventListener("click", () => openDrawer(job.job_id));
    const title = jobTitle(job);
    const progress = progressFor(job);
    card.innerHTML = `
      <span class="job-dot ${job.stage}"></span>
      <div>
        <p class="job-title">${escapeHtml(title)}</p>
        <p class="job-meta">${escapeHtml(jobSummary(job))}</p>
      </div>
      <div class="job-stage">${escapeHtml(stageLabels[job.stage] || job.stage)}</div>
      ${renderJobQueue(job, true)}
      <div class="progress"><span style="width:${progress}%"></span></div>
    `;
    list.appendChild(card);
  });
}

function filteredJobs() {
  return state.jobs.filter((job) => {
    if (state.jobStatusFilter === "active" && !["queued", "waiting", "running"].includes(job.status)) {
      return false;
    }
    if (state.jobStatusFilter === "succeeded" && job.status !== "succeeded") {
      return false;
    }
    if (state.jobStatusFilter === "failed" && job.status !== "failed") {
      return false;
    }
    if (state.jobStatusFilter === "canceled" && job.status !== "canceled") {
      return false;
    }
    if (!state.jobSearch) {
      return true;
    }
    return jobSearchText(job).includes(state.jobSearch);
  });
}

function jobSearchText(job) {
  return [
    job.job_id,
    job.kind,
    job.status,
    job.stage,
    job.profile_id,
    job.collection_id,
    job.current,
    ...(job.items || []).flatMap((item) => [item.url, item.title, item.stage, item.status]),
    ...(job.results || []).flatMap((item) => [item.url, item.title, item.work_dir, item.error]),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function renderProfiles() {
  const list = document.getElementById("profileList");
  document.getElementById("profileSummary").textContent = `${state.profiles.length} 个方案`;
  if (!state.profiles.length) {
    list.innerHTML = `<div class="empty mini">还没有节目处理方案</div>`;
    return;
  }
  list.innerHTML = state.profiles
    .map(
      (profile) => `
        <div class="profile-item">
          <strong>${escapeHtml(profile.name)}</strong>
          <span>${escapeHtml(profile.id)}</span>
          <small>${escapeHtml(profile.description || "")}</small>
          <div class="profile-actions">
            <button
              class="button compact"
              type="button"
              data-profile-target="video"
              data-profile-id="${escapeHtml(profile.id)}"
              data-profile-name="${escapeHtml(profile.name)}"
            >视频</button>
            <button
              class="button compact"
              type="button"
              data-profile-target="youtube"
              data-profile-id="${escapeHtml(profile.id)}"
              data-profile-name="${escapeHtml(profile.name)}"
            >YouTube</button>
            <button
              class="button compact"
              type="button"
              data-profile-target="radiko"
              data-profile-id="${escapeHtml(profile.id)}"
              data-profile-name="${escapeHtml(profile.name)}"
            >Radiko</button>
          </div>
        </div>
      `,
    )
    .join("");
}

function renderKnowledge() {
  document.getElementById("knowledgeSummary").textContent =
    `${state.pendingSegments.length} 条待确认 · ${state.librarySegments.length} 条已收录`;
  renderPendingSegments();
  renderLibrarySegments();
}

function renderScheduler() {
  const list = document.getElementById("scheduleList");
  const programs = state.scheduledPrograms || [];
  const enabledCount = programs.filter((program) => program.enabled).length;
  document.getElementById("schedulerSummary").textContent =
    `${enabledCount}/${programs.length} 个启用`;
  if (!programs.length) {
    list.innerHTML = `<div class="empty mini">还没有配置长期定时录制计划</div>`;
    return;
  }
  list.innerHTML = programs
    .map(
      (program) => `
        <div class="schedule-item ${program.enabled ? "enabled" : "disabled"}">
          <div>
            <strong>${escapeHtml(program.name)}</strong>
            <span>${escapeHtml(program.source_label)} · ${escapeHtml(program.schedule_label)}</span>
            <small>${escapeHtml(program.url || "未填写来源链接")} · ${escapeHtml(program.duration_minutes)} 分钟 · ${escapeHtml(program.profile_name || "通用默认方案")}</small>
            <div class="schedule-profile-row">
              <select data-schedule-profile="${escapeHtml(program.id)}">
                ${profileOptions(program.profile_id)}
              </select>
              <button type="button" class="button ghost" data-schedule-profile-save="${escapeHtml(program.id)}">应用方案</button>
            </div>
          </div>
          <em>${program.enabled ? "运行中" : "已停用"}</em>
        </div>
      `,
    )
    .join("");
}

function renderMetricsOverview() {
  const list = document.getElementById("metricsCards");
  const metrics = state.metrics || [];
  const successCount = metrics.filter((item) => item.success).length;
  document.getElementById("metricsSummary").textContent = metrics.length
    ? `最近 ${metrics.length} 次 · ${successCount} 次成功`
    : "暂无记录";
  if (!metrics.length) {
    list.innerHTML = `<div class="empty mini">完成一次处理后，这里会显示耗时、段落数、知识库命中和推送记录。</div>`;
    return;
  }
  list.innerHTML = metrics
    .slice(0, 4)
    .map((item) => {
      const usage = tokenUsage(item);
      return `
        <div class="metric-card ${item.success ? "success" : "failed"}" title="${escapeHtml(tokenTooltip(item))}">
          <strong>${escapeHtml(item.program_name || item.run_id || "未命名节目")}</strong>
          <span>${escapeHtml(sourceLabel(item.source))} · ${escapeHtml(item.air_date || "日期未知")}</span>
          <div class="metric-row">
            <b>${formatDuration(item.duration_s)}</b>
            <small>总耗时</small>
          </div>
          <div class="metric-grid-mini">
            <span>${escapeHtml(item.segments_count || 0)} 段</span>
            <span>${escapeHtml(item.sections_count || 0)} 节</span>
            <span>${escapeHtml(item.library_hits || 0)} 次命中</span>
            <span>${escapeHtml(formatTokenCount(usage?.total))}</span>
          </div>
        </div>
      `;
    })
    .join("");
}

function renderPendingSegments() {
  const list = document.getElementById("pendingSegments");
  const items = state.pendingSegments || [];
  if (!items.length) {
    list.innerHTML = `<div class="empty mini">暂时没有待确认环节</div>`;
    return;
  }
  list.innerHTML = items
    .map(
      (item) => `
        <div class="knowledge-item">
          <strong>${escapeHtml(item.title_ja)}</strong>
          <span>${escapeHtml(item.program_series)} · ${escapeHtml(item.air_date)}</span>
          <p>${escapeHtml(item.intro)}</p>
          <small>${escapeHtml(item.id)}</small>
          <div class="knowledge-actions">
            <button
              type="button"
              class="button compact"
              data-pending-action="skip"
              data-segment-id="${escapeHtml(item.id)}"
            >跳过</button>
            <button
              type="button"
              class="button compact"
              data-pending-action="approve"
              data-segment-id="${escapeHtml(item.id)}"
            >收录</button>
          </div>
        </div>
      `,
    )
    .join("");
}

function renderLibrarySegments() {
  const list = document.getElementById("librarySegments");
  const items = filteredLibrarySegments();
  if (!items.length) {
    list.innerHTML = `<div class="empty mini">没有找到匹配的知识库条目</div>`;
    return;
  }
  list.innerHTML = items
    .map(
      (item) => `
        <div class="knowledge-item">
          <strong>${escapeHtml(item.title_ja)}</strong>
          <span>${escapeHtml(item.program_ja)}</span>
          <p>${escapeHtml(item.intro)}</p>
          <small>${escapeHtml((item.aliases || []).join(" · "))}</small>
        </div>
      `,
    )
    .join("");
}

function filteredLibrarySegments() {
  if (!state.librarySearch) {
    return state.librarySegments || [];
  }
  return (state.librarySegments || []).filter((item) =>
    [item.id, item.program_ja, item.title_ja, item.intro, ...(item.aliases || [])]
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
      .includes(state.librarySearch),
  );
}

function populateProfileSelect(selectId, selectedId = null) {
  const select = document.getElementById(selectId);
  const current = selectedId || select.value;
  select.innerHTML = profileOptions(current);
  if (current && state.profiles.some((profile) => profile.id === current)) {
    select.value = current;
  }
}

function profileOptions(selectedId = "") {
  return `
    <option value="">使用通用默认方案</option>
    ${state.profiles
      .map((profile) => {
        const selected = profile.id === selectedId ? " selected" : "";
        return `<option value="${escapeHtml(profile.id)}"${selected}>${escapeHtml(profile.name)}</option>`;
      })
      .join("")}
  `;
}

function populateCollectionSelect(selectId, selectedId = null) {
  const select = document.getElementById(selectId);
  const current = selectedId || select.value;
  select.innerHTML = `
    <option value="">自动按方案归档</option>
    ${state.collections
      .map(
        (collection) =>
          `<option value="${escapeHtml(collection.id)}">${escapeHtml(collection.name)}</option>`,
      )
      .join("")}
    <option value="__custom">新建合集...</option>
  `;
  if (current && state.collections.some((collection) => collection.id === current)) {
    select.value = current;
  } else if (current === "__custom") {
    select.value = "__custom";
  }
  toggleCollectionCustom(selectId);
}

function selectedProfileId(selectId) {
  return document.getElementById(selectId).value || null;
}

function bindCollectionPicker(selectId, customId) {
  document.getElementById(selectId).addEventListener("change", () => toggleCollectionCustom(selectId));
  document.getElementById(customId).addEventListener("input", (event) => {
    event.currentTarget.value = slugifyCollectionId(event.currentTarget.value);
  });
}

function toggleCollectionCustom(selectId) {
  const row = document.getElementById(`${selectId}CustomRow`);
  if (!row) {
    return;
  }
  row.hidden = document.getElementById(selectId).value !== "__custom";
}

function resetCollectionToAuto(target) {
  const selectId = `${target}Collection`;
  const select = document.getElementById(selectId);
  if (select) {
    select.value = "";
    toggleCollectionCustom(selectId);
  }
}

function selectedCollectionId(selectId, customId) {
  const value = document.getElementById(selectId).value;
  if (value === "__custom") {
    return document.getElementById(customId).value.trim() || null;
  }
  return value || null;
}

async function openDrawer(jobId) {
  state.selectedJobId = jobId;
  document.getElementById("jobDrawer").classList.add("open");
  document.getElementById("jobDrawer").setAttribute("aria-hidden", "false");
  refreshDrawer();
  await loadArtifacts(jobId);
}

function closeDrawer() {
  state.selectedJobId = null;
  document.getElementById("jobDrawer").classList.remove("open");
  document.getElementById("jobDrawer").setAttribute("aria-hidden", "true");
}

function refreshDrawer() {
  if (!state.selectedJobId) {
    return;
  }
  const job = state.jobs.find((item) => item.job_id === state.selectedJobId);
  if (!job) {
    return;
  }
  document.getElementById("drawerTitle").textContent = shortId(job.job_id);
  const metrics = metricsFor(job);
  const artifacts = state.artifactsByJob[job.job_id] || [];
  document.getElementById("drawerBody").innerHTML = `
    <section class="detail-block">
      <h3>状态</h3>
      ${renderJobActions(job)}
      <pre>${escapeHtml(JSON.stringify({
        kind: job.kind,
        status: job.status,
        stage: job.stage,
        completed: job.completed,
        total: job.total,
        message: job.message,
        error: job.error,
        profile_id: job.profile_id,
        collection_id: job.collection_id,
      }, null, 2))}</pre>
    </section>
    <section class="detail-block">
      <h3>队列</h3>
      ${renderJobQueue(job, false)}
    </section>
    <section class="detail-block">
      <h3>结果</h3>
      ${renderJobResults(job)}
    </section>
    <section class="detail-block">
      <h3>产物文件</h3>
      ${renderArtifacts(artifacts)}
    </section>
    <section class="detail-block">
      <h3>日志</h3>
      <pre>${escapeHtml((job.logs || []).join("\n") || "暂无日志")}</pre>
    </section>
    <section class="detail-block">
      <h3>处理指标</h3>
      <pre>${escapeHtml(metrics.length ? JSON.stringify(metrics, null, 2) : "暂无指标记录")}</pre>
    </section>
  `;
}

function renderJobActions(job) {
  if (!canCancelJob(job)) {
    return "";
  }
  return `
    <div class="detail-actions">
      <button class="button danger compact" type="button" data-cancel-job="${escapeHtml(job.job_id)}">
        取消任务
      </button>
    </div>
  `;
}

function canCancelJob(job) {
  return !["succeeded", "failed", "canceled"].includes(job.status);
}

async function loadArtifacts(jobId) {
  try {
    const artifacts = await apiFetch(`/api/artifacts?job_id=${encodeURIComponent(jobId)}`);
    state.artifactsByJob[jobId] = artifacts;
    await Promise.all(
      artifacts
        .filter((artifact) => artifact.kind === "work_dir")
        .map((artifact) => loadArtifactFiles(artifact.path)),
    );
    refreshDrawer();
  } catch (error) {
    console.warn("Failed to load artifacts", error);
  }
}

async function loadArtifactFiles(path) {
  if (state.artifactFilesByPath[path]) {
    return;
  }
  state.artifactFilesByPath[path] = await apiFetch(
    `/api/artifacts/files?path=${encodeURIComponent(path)}`,
  );
}

async function loadArtifactPreview(path) {
  if (state.artifactPreviewByPath[path]) {
    return;
  }
  const res = await fetch(apiPath(`/api/artifacts/file?path=${encodeURIComponent(path)}`));
  if (!res.ok) {
    showToast("预览失败");
    throw new Error("预览失败");
  }
  const text = await res.text();
  state.artifactPreviewByPath[path] = text.slice(0, 12000);
}

function metricsFor(job) {
  const runIds = new Set([
    ...(job.items || []).map((item) => item.run_id).filter(Boolean),
    ...(job.results || []).map((item) => item.run_id).filter(Boolean),
  ]);
  const titles = new Set((job.results || []).map((item) => item.title).filter(Boolean));
  return state.metrics.filter(
    (metric) => runIds.has(metric.run_id) || titles.has(metric.program_name),
  );
}

function renderJobResults(job) {
  const results = job.results || [];
  if (!results.length) {
    return `<pre>还没有完成的条目</pre>`;
  }
  return `
    <div class="result-list">
      ${results
        .map((item) => {
          const index = item.playlist_index ? `#${item.playlist_index}` : "手动任务";
          const status = item.error ? "失败" : "已推送";
          return `
            <div class="result-item ${item.error ? "failed" : ""}">
              <div>
                <strong>${escapeHtml(index)} · ${escapeHtml(status)}</strong>
                <span>${escapeHtml(item.title || item.url)}</span>
              </div>
              <small>${escapeHtml(item.error || item.work_dir || "")}</small>
            </div>
          `;
        })
        .join("")}
    </div>
  `;
}

function renderArtifacts(artifacts) {
  if (!artifacts.length) {
    return `<pre>还没有索引到产物文件</pre>`;
  }
  return `
    <div class="artifact-list">
      ${artifacts
        .map(
          (artifact) => `
            <div class="artifact-item">
              <strong>${escapeHtml(artifact.kind)}</strong>
              <span>${escapeHtml(artifact.label || artifact.path)}</span>
              <small>${escapeHtml(artifact.path)}</small>
              ${renderArtifactFiles(artifact)}
            </div>
          `,
        )
        .join("")}
    </div>
  `;
}

function renderArtifactFiles(artifact) {
  const files = state.artifactFilesByPath[artifact.path] || [];
  if (!files.length) {
    return "";
  }
  return `
    <div class="artifact-files">
      ${files
        .map((file) => {
          const size = file.kind === "file" ? formatBytes(file.size || 0) : "文件夹";
          return `
            <div class="artifact-file ${escapeHtml(file.kind)}">
              <div>
                <strong>${escapeHtml(file.name)}</strong>
                <span>${escapeHtml(size)}</span>
              </div>
              ${renderArtifactFileActions(file)}
              ${renderArtifactPreview(file)}
            </div>
          `;
        })
        .join("")}
    </div>
  `;
}

function renderArtifactFileActions(file) {
  if (file.kind !== "file") {
    return "";
  }
  return `
    <div class="artifact-actions">
      ${file.previewable ? `<button type="button" data-preview-path="${escapeHtml(file.path)}">预览</button>` : ""}
      <a href="${escapeHtml(apiPath(file.view_url))}" target="_blank" rel="noreferrer">打开</a>
      <a href="${escapeHtml(apiPath(file.download_url))}">下载</a>
    </div>
  `;
}

function renderArtifactPreview(file) {
  const preview = state.artifactPreviewByPath[file.path];
  if (!preview) {
    return "";
  }
  return `<pre class="artifact-preview">${escapeHtml(preview)}</pre>`;
}

function renderJobQueue(job, compact) {
  const items = job.items || [];
  if (!items.length) {
    return compact ? "" : `<pre>没有队列条目</pre>`;
  }
  const visibleItems = compact ? items.slice(0, 6) : items;
  const hiddenCount = items.length - visibleItems.length;
  return `
    <div class="queue-list ${compact ? "compact" : ""}">
      ${visibleItems
        .map((item) => {
          const index = item.playlist_index ? `#${item.playlist_index}` : String(item.queue_index);
          return `
            <div class="queue-item ${escapeHtml(item.stage)}">
              <span class="queue-state">${escapeHtml(itemStatus(item))}</span>
              <strong>${escapeHtml(index)}</strong>
              <p>${escapeHtml(item.title || item.url)}</p>
            </div>
          `;
        })
        .join("")}
      ${hiddenCount > 0 ? `<div class="queue-more">还有 ${hiddenCount} 个等待中</div>` : ""}
    </div>
  `;
}

function itemStatus(item) {
  if (item.stage === "download") {
    return "获取";
  }
  if (item.stage === "pipeline") {
    return "处理";
  }
  if (item.stage === "pipeline_waiting") {
    return "等待";
  }
  if (item.stage === "distributed") {
    return "完成";
  }
  if (item.stage === "failed") {
    return "失败";
  }
  if (item.stage === "canceled") {
    return "取消";
  }
  return "排队";
}

function renderPlaylistPreview(items) {
  const preview = document.getElementById("playlistPreview");
  preview.hidden = false;
  preview.innerHTML = `
    <div class="preview-head">
      <span>${items.length} 个条目</span>
      <span>${escapeHtml(items[0]?.index || "")} → ${escapeHtml(items.at(-1)?.index || "")}</span>
    </div>
    <div class="preview-list">
      ${items
        .map(
          (item) => `
            <div class="preview-item">
              <strong>#${escapeHtml(item.index)}</strong>
              <span>${escapeHtml(item.title)}</span>
              <small>${escapeHtml(item.url)}</small>
            </div>
          `,
        )
        .join("")}
    </div>
  `;
}

function updateCredentialSummary(configured = {}) {
  const count = Object.values(configured).filter(Boolean).length;
  const total = Object.keys(configured).length || 6;
  document.getElementById("credentialSummary").textContent = `${count}/${total} 项已配置`;
  renderCredentialList(configured);
}

function renderCredentialList(configured = {}) {
  const list = document.getElementById("credentialList");
  if (!list) {
    return;
  }
  const entries = Object.entries(credentialLabels);
  list.innerHTML = entries
    .map(([field, label]) => {
      const ok = Boolean(configured[field]);
      return `
        <div class="credential-status ${ok ? "configured" : "missing"}">
          <span>${escapeHtml(label)}</span>
          <strong>${ok ? "已配置" : "未配置"}</strong>
        </div>
      `;
    })
    .join("");
}

function syncDuration(prefix, value) {
  const normalized = Math.max(1, Number(value || 60));
  document.getElementById(`${prefix}DurationRange`).value = Math.min(240, normalized);
  document.getElementById(`${prefix}DurationInput`).value = normalized;
  document.getElementById(`${prefix}DurationOutput`).value = normalized;
}

function setDefaultStartTime() {
  const date = new Date();
  date.setMinutes(date.getMinutes() + 5);
  document.getElementById("youtubeStart").value = toDatetimeLocal(date);
  document.getElementById("radikoStart").value = toDatetimeLocal(date);
}

function getUrlLines() {
  return document
    .getElementById("videoUrls")
    .value.split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean);
}

async function apiFetch(path, options = {}) {
  const res = await fetch(apiPath(path), {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const payload = await res.json();
      detail = payload.detail || detail;
    } catch {
      detail = await res.text();
    }
    showToast(String(detail));
    throw new Error(detail);
  }
  return res.json();
}

function jobTitle(job) {
  if ((job.items || []).length > 1) {
    return `批量任务 ${job.completed}/${job.total}`;
  }
  const firstResult = (job.results || [])[0];
  if (firstResult && firstResult.title) {
    return firstResult.title;
  }
  const firstItem = (job.items || [])[0];
  if (firstItem?.title) {
    return firstItem.title;
  }
  if (job.current) {
    try {
      const url = new URL(job.current);
      return url.hostname.replace(/^www\./, "");
    } catch {
      return job.current;
    }
  }
  return `${sourceLabel(job.kind)} ${shortId(job.job_id)}`;
}

function jobSummary(job) {
  const items = job.items || [];
  if (!items.length) {
    return job.current || job.message || job.kind;
  }
  const counts = items.reduce((acc, item) => {
    acc[item.stage] = (acc[item.stage] || 0) + 1;
    return acc;
  }, {});
  const running = items.find((item) => item.stage === "download" || item.stage === "pipeline");
  const waiting = (counts.pipeline_waiting || 0) + (counts.queued || 0);
  const parts = [
    `${counts.distributed || 0} 个完成`,
    `${waiting} 个等待`,
  ];
  if (running) {
    parts.unshift(`${itemStatus(running)} ${running.playlist_index ? `#${running.playlist_index}` : running.queue_index}`);
  }
  if (job.collection_id) {
    parts.push(job.collection_id);
  }
  return parts.join(" · ");
}

function progressFor(job) {
  if (job.total > 1) {
    const base = Math.round((job.completed / job.total) * 100);
    if (job.status === "succeeded" || job.status === "failed" || job.status === "canceled") {
      return 100;
    }
    return Math.min(96, Math.max(base, stageProgress[job.stage] || 8));
  }
  return stageProgress[job.stage] || 8;
}

function toDatetimeLocal(date) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

function toLocalIso(datetimeLocal) {
  const date = new Date(datetimeLocal);
  const pad = (value) => String(Math.abs(value)).padStart(2, "0");
  const offsetMinutes = -date.getTimezoneOffset();
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const hours = pad(Math.trunc(offsetMinutes / 60));
  const minutes = pad(offsetMinutes % 60);
  return `${datetimeLocal}:00${sign}${hours}:${minutes}`;
}

function slugifyCollectionId(value) {
  return String(value)
    .toLowerCase()
    .replace(/[^\p{L}\p{N}_-]+/gu, "_")
    .replace(/_+/g, "_")
    .replace(/^[_.-]+|[_.-]+$/g, "")
    .slice(0, 64);
}

function shortId(id) {
  return String(id).slice(0, 8);
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return "0 B";
  }
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatDuration(seconds) {
  const value = Number(seconds || 0);
  if (value < 60) {
    return `${Math.round(value)} 秒`;
  }
  return `${(value / 60).toFixed(1)} 分钟`;
}

function tokenTooltip(item) {
  const usage = tokenUsage(item);
  if (!usage) {
    return "Token 记录：当前指标文件暂未记录 token 消耗";
  }
  const parts = [];
  if (usage.input !== null) {
    parts.push(`输入 ${usage.input}`);
  }
  if (usage.output !== null) {
    parts.push(`输出 ${usage.output}`);
  }
  if (usage.total !== null) {
    parts.push(`合计 ${usage.total}`);
  }
  if (usage.providers.length) {
    parts.push(usage.providers.join(" / "));
  }
  return `Token 记录：${parts.join(" · ")}`;
}

function tokenUsage(item) {
  const usage = item.token_usage || item.usage || {};
  const nested = Object.entries(usage)
    .filter(([, value]) => value && typeof value === "object" && !Array.isArray(value))
    .map(([label, value]) => ({
      label,
      input: firstNumber(value.input_tokens, value.prompt_tokens),
      output: firstNumber(value.output_tokens, value.completion_tokens),
      total: firstNumber(value.total_tokens, value.tokens_total),
    }));
  const nestedInput = sumNumbers(nested.map((item) => item.input));
  const nestedOutput = sumNumbers(nested.map((item) => item.output));
  const nestedTotal = sumNumbers(nested.map((item) => item.total));
  const input = firstNumber(
    item.input_tokens,
    item.prompt_tokens,
    usage.input_tokens,
    usage.prompt_tokens,
    nestedInput,
  );
  const output = firstNumber(
    item.output_tokens,
    item.completion_tokens,
    usage.output_tokens,
    usage.completion_tokens,
    nestedOutput,
  );
  const total = firstNumber(
    item.total_tokens,
    item.tokens_total,
    usage.total_tokens,
    nestedTotal,
  ) ??
    (input !== null || output !== null ? (input || 0) + (output || 0) : null);
  if (input === null && output === null && total === null) {
    return null;
  }
  const providers = nested
    .filter((item) => item.input !== null || item.output !== null || item.total !== null)
    .map((item) => `${item.label} ${item.total ?? (item.input || 0) + (item.output || 0)}`);
  return { input, output, total, providers };
}

function firstNumber(...values) {
  for (const value of values) {
    if (value === null || value === undefined || value === "") {
      continue;
    }
    const number = Number(value);
    if (Number.isFinite(number) && number >= 0) {
      return number;
    }
  }
  return null;
}

function sumNumbers(values) {
  const valid = values.filter((value) => value !== null && value !== undefined);
  if (!valid.length) {
    return null;
  }
  return valid.reduce((sum, value) => sum + value, 0);
}

function formatTokenCount(value) {
  if (value === null || value === undefined) {
    return "未记录";
  }
  if (value >= 1000) {
    return `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}K tokens`;
  }
  return `${value} tokens`;
}

function sourceLabel(source) {
  return {
    video: "视频",
    video_batch: "批量视频",
    youtube_live: "YouTube 直播",
    live_recording: "直播",
    radiko_recording: "Radiko",
    radiko_live: "Radiko 实时",
    radiko_timefree: "Radiko 回听",
    oneshot: "单次处理",
    resummarize: "重新总结",
  }[source] || source || "未知来源";
}

function sourceTargetLabel(target) {
  return {
    video: "视频入口",
    youtube: "YouTube 直播入口",
    radiko: "Radiko 入口",
  }[target] || "当前入口";
}

function showToast(message) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 2800);
}

function setButtonPending(button, pending) {
  if (!button) {
    return;
  }
  if (!button.dataset.label) {
    button.dataset.label = button.textContent;
  }
  button.disabled = pending;
  button.textContent = pending ? "处理中" : button.dataset.label;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
