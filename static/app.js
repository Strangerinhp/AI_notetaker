(() => {
  "use strict";

  const SETTINGS_STORAGE_KEY = "meetnote.settings.v1";
  const DEFAULT_TRANSCRIPT_ENGINE = "zipformer";

  const state = {
    meetings: [],
    current: null,
    files: [],
    pendingWordFile: null,
    activeWorkspace: "transcript",
    poll: null,
    saveTimer: null,
    saving: false,
    saveAgain: false,
    settings: {
      engine: DEFAULT_TRANSCRIPT_ENGINE,
      summaryPrompt: "",
    },
    settingsReturnFocus: null,
  };

  const $ = (id) => document.getElementById(id);
  const ui = {
    sidebar: $("sidebar"),
    closeSidebar: $("close-sidebar"),
    backdrop: $("backdrop"),
    menu: $("menu-button"),
    newMeeting: $("new-meeting"),
    history: $("history-list"),
    historyCount: $("history-count"),
    historySearch: $("history-search"),
    pageKicker: $("page-kicker"),
    pageTitle: $("page-title"),
    saveStatus: $("save-status"),
    settingsButton: $("settings-button"),
    settingsCurrentModel: $("settings-current-model"),
    settingsModal: $("settings-modal"),
    settingsBackdrop: $("settings-backdrop"),
    settingsClose: $("settings-close"),
    settingsCancel: $("settings-cancel"),
    settingsReset: $("settings-reset"),
    settingsSave: $("settings-save"),
    summaryPromptInput: $("summary-prompt-input"),
    uploadView: $("upload-view"),
    meetingView: $("meeting-view"),
    uploadCard: $("upload-card"),
    processingCard: $("processing-card"),
    dropZone: $("drop-zone"),
    meetingTitleInput: $("meeting-title-input"),
    fileInput: $("file-input"),
    fileName: $("file-name"),
    fileDetail: $("file-detail"),
    engine: $("engine-select"),
    diarization: $("diarization-enabled"),
    diarizationNote: $("diarization-note"),
    diarizationOptions: $("diarization-options"),
    speakerCount: $("speaker-count"),
    minSpeakerTurn: $("min-speaker-turn"),
    uploadButton: $("upload-button"),
    processTitle: $("process-title"),
    processMessage: $("process-message"),
    progress: $("progress-fill"),
    meetingTitle: $("meeting-title"),
    meetingMeta: $("meeting-meta"),
    transcriptWorkspace: $("transcript-workspace"),
    wordWorkspace: $("word-workspace"),
    markdownInput: $("markdown-input"),
    markdownPreview: $("markdown-preview"),
    documentStats: $("document-stats"),
    editorBody: $("editor-body"),
    saveButton: $("save-button"),
    downloadButton: $("download-button"),
    wordDownloadButton: $("word-download-button"),
    wordUpdateActions: $("word-update-actions"),
    wordUploadInput: $("word-upload-input"),
    wordSaveButton: $("word-save-button"),
    wordFileName: $("word-file-name"),
    wordFileMeta: $("word-file-meta"),
    wordPlaceholder: $("word-placeholder"),
    wordFrame: $("word-frame"),
    summarizeButton: $("summarize-button"),
    transcriptInspector: $("transcript-inspector"),
    speakerTimeline: $("speaker-timeline"),
    timelineStats: $("timeline-stats"),
    speakerRenames: $("speaker-renames"),
    applySpeakerNames: $("apply-speaker-names"),
    toasts: $("toasts"),
  };

  const speakerColors = [
    "#3856e8", "#2b8a6e", "#d47b28", "#8d54c7", "#cf4f63",
    "#1684a5", "#7e8c2d", "#8d6655", "#5369a8", "#aa4d98",
  ];

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function sanitizeHtml(html) {
    const template = document.createElement("template");
    template.innerHTML = html;
    template.content
      .querySelectorAll("script,style,iframe,object,embed,form,link,meta")
      .forEach((node) => node.remove());
    template.content.querySelectorAll("*").forEach((node) => {
      [...node.attributes].forEach((attribute) => {
        const name = attribute.name.toLowerCase();
        const value = attribute.value.trim().toLowerCase();
        if (name.startsWith("on") || name === "style") {
          node.removeAttribute(attribute.name);
        } else if (
          (name === "href" || name === "src") &&
          (value.startsWith("javascript:") || value.startsWith("data:text/html"))
        ) {
          node.removeAttribute(attribute.name);
        }
      });
      if (node.tagName === "A") {
        node.setAttribute("target", "_blank");
        node.setAttribute("rel", "noopener noreferrer");
      }
    });
    return template.innerHTML;
  }

  function renderMarkdown(markdown) {
    const content = markdown || "";
    if (window.marked?.parse) {
      return sanitizeHtml(
        window.marked.parse(content, {
          breaks: true,
          gfm: true,
        }),
      );
    }
    return `<p>${escapeHtml(content).replaceAll("\n", "<br>")}</p>`;
  }

  function toast(message, error = false) {
    const element = document.createElement("div");
    element.className = `toast${error ? " error" : ""}`;
    element.textContent = message;
    ui.toasts.appendChild(element);
    window.setTimeout(() => element.remove(), 3200);
  }

  function setSaveStatus(type, text) {
    ui.saveStatus.className = `save-status ${type}`;
    ui.saveStatus.querySelector("b").textContent = text;
  }

  function formatDate(value, detailed = false) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "Không rõ thời gian";
    return new Intl.DateTimeFormat("vi-VN", {
      day: "2-digit",
      month: detailed ? "long" : "2-digit",
      year: "numeric",
      ...(detailed ? { hour: "2-digit", minute: "2-digit" } : {}),
    }).format(date);
  }

  async function api(url, options = {}) {
    const response = await fetch(url, options);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.error || `Lỗi máy chủ (${response.status})`);
    }
    return payload;
  }

  function closeSidebar() {
    document.body.classList.remove("sidebar-open");
  }

  function defaultSummaryPrompt() {
    return ui.summaryPromptInput.defaultValue.trim();
  }

  function validEngine(engine) {
    return [...ui.engine.options].some((option) => option.value === engine);
  }

  function updateSettingsSummary() {
    const selected = [...ui.engine.options].find(
      (option) => option.value === state.settings.engine,
    );
    ui.settingsCurrentModel.textContent = selected
      ? selected.textContent.split("—")[0].trim()
      : "Zipformer 30M";
  }

  function loadSettings() {
    const fallback = {
      engine: DEFAULT_TRANSCRIPT_ENGINE,
      summaryPrompt: defaultSummaryPrompt(),
    };
    let stored = null;
    try {
      stored = JSON.parse(localStorage.getItem(SETTINGS_STORAGE_KEY) || "null");
    } catch (_error) {
      stored = null;
    }
    state.settings = {
      engine: validEngine(stored?.engine) ? stored.engine : fallback.engine,
      summaryPrompt: typeof stored?.summaryPrompt === "string" && stored.summaryPrompt.trim()
        ? stored.summaryPrompt.trim()
        : fallback.summaryPrompt,
    };
    ui.engine.value = state.settings.engine;
    ui.summaryPromptInput.value = state.settings.summaryPrompt;
    updateSettingsSummary();
  }

  function openSettings() {
    state.settingsReturnFocus = document.activeElement;
    ui.engine.value = state.settings.engine;
    ui.summaryPromptInput.value = state.settings.summaryPrompt;
    ui.settingsModal.classList.remove("hidden");
    ui.settingsModal.setAttribute("aria-hidden", "false");
    document.body.classList.add("settings-open");
    window.setTimeout(() => ui.engine.focus(), 0);
  }

  function closeSettings() {
    ui.engine.value = state.settings.engine;
    ui.summaryPromptInput.value = state.settings.summaryPrompt;
    ui.settingsModal.classList.add("hidden");
    ui.settingsModal.setAttribute("aria-hidden", "true");
    document.body.classList.remove("settings-open");
    updateDiarizationControls();
    state.settingsReturnFocus?.focus?.();
    state.settingsReturnFocus = null;
  }

  function resetSettingsForm() {
    ui.engine.value = DEFAULT_TRANSCRIPT_ENGINE;
    ui.summaryPromptInput.value = defaultSummaryPrompt();
    updateDiarizationControls();
  }

  function saveSettings() {
    const summaryPrompt = ui.summaryPromptInput.value.trim();
    if (!summaryPrompt) {
      toast("Prompt tóm tắt không được để trống.", true);
      ui.summaryPromptInput.focus();
      return;
    }
    state.settings = {
      engine: validEngine(ui.engine.value)
        ? ui.engine.value
        : DEFAULT_TRANSCRIPT_ENGINE,
      summaryPrompt,
    };
    let persisted = true;
    try {
      localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(state.settings));
    } catch (_error) {
      persisted = false;
    }
    updateSettingsSummary();
    closeSettings();
    toast(
      persisted
        ? "Đã lưu cài đặt."
        : "Đã áp dụng cho phiên này; trình duyệt không cho phép lưu lâu dài.",
      !persisted,
    );
  }

  function switchWorkspace(target, refresh = true) {
    const workspace = target === "word" ? "word" : "transcript";
    state.activeWorkspace = workspace;
    ui.transcriptWorkspace.classList.toggle("hidden", workspace !== "transcript");
    ui.wordWorkspace.classList.toggle("hidden", workspace !== "word");
    document.querySelectorAll("[data-workspace-target]").forEach((button) => {
      const active = button.dataset.workspaceTarget === workspace;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    if (workspace === "word" && refresh) refreshWordViewer();
  }

  async function loadHistory() {
    try {
      const payload = await api("/api/meetings");
      state.meetings = payload.meetings || [];
      renderHistory();
    } catch (error) {
      toast(`Không tải được lịch sử: ${error.message}`, true);
    }
  }

  function renderHistory() {
    const query = ui.historySearch.value.trim().toLocaleLowerCase("vi");
    const meetings = state.meetings.filter((meeting) =>
      meeting.title.toLocaleLowerCase("vi").includes(query),
    );
    ui.historyCount.textContent = state.meetings.length;
    ui.history.replaceChildren();

    if (!meetings.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = query
        ? "Không tìm thấy cuộc họp phù hợp."
        : "Chưa có cuộc họp nào.";
      ui.history.appendChild(empty);
      return;
    }

    meetings.forEach((meeting) => {
      const item = document.createElement("div");
      item.className = `history-item${
        state.current?.id === meeting.id ? " active" : ""
      }`;

      const openButton = document.createElement("button");
      openButton.type = "button";
      openButton.className = "history-open";
      openButton.title = meeting.title;

      const icon = document.createElement("span");
      icon.className = "history-icon";
      icon.innerHTML =
        '<svg viewBox="0 0 24 24"><path d="M6 3h9l3 3v15H6z"/><path d="M14 3v4h4M9 12h6M9 16h6"/></svg>';

      const copy = document.createElement("span");
      copy.className = "history-copy";
      const title = document.createElement("strong");
      title.textContent = meeting.title;
      const date = document.createElement("small");
      date.textContent = formatDate(meeting.updated_at);
      copy.append(title, date);
      openButton.append(icon, copy);
      openButton.addEventListener("click", () => openMeeting(meeting.id));

      const deleteButton = document.createElement("button");
      deleteButton.type = "button";
      deleteButton.className = "history-delete";
      deleteButton.title = "Xoá cuộc họp";
      deleteButton.setAttribute("aria-label", `Xoá ${meeting.title}`);
      deleteButton.innerHTML =
        '<svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 11v5M14 11v5"/></svg>';
      deleteButton.addEventListener("click", () => deleteMeeting(meeting, deleteButton));

      item.append(openButton, deleteButton);
      ui.history.appendChild(item);
    });
  }

  async function deleteMeeting(meeting, deleteButton) {
    const confirmed = window.confirm(
      `Xoá “${meeting.title}”?\n\nTranscript, báo cáo và file Word đã lưu sẽ bị xoá vĩnh viễn.`,
    );
    if (!confirmed) return;

    deleteButton.disabled = true;
    const deletingCurrent = state.current?.id === meeting.id;
    if (deletingCurrent && state.saveTimer) {
      window.clearTimeout(state.saveTimer);
      state.saveTimer = null;
      state.saveAgain = false;
    }
    while (deletingCurrent && state.saving) {
      await new Promise((resolve) => window.setTimeout(resolve, 50));
    }

    try {
      await api(`/api/meetings/${encodeURIComponent(meeting.id)}`, {
        method: "DELETE",
      });
      state.meetings = state.meetings.filter((item) => item.id !== meeting.id);
      if (deletingCurrent) {
        state.current = null;
        await showUpload();
      } else {
        renderHistory();
      }
      toast("Đã xoá cuộc họp.");
    } catch (error) {
      deleteButton.disabled = false;
      toast(`Không thể xoá cuộc họp: ${error.message}`, true);
    }
  }

  function storeEditorValue() {
    if (state.current) {
      state.current.transcript = ui.markdownInput.value;
    }
  }

  function updateEditor() {
    const content = ui.markdownInput.value;
    ui.markdownPreview.innerHTML = renderMarkdown(content);
    const words = content.trim() ? content.trim().split(/\s+/).length : 0;
    ui.documentStats.textContent =
      `${words.toLocaleString("vi-VN")} từ · ${content.length.toLocaleString("vi-VN")} ký tự`;
  }

  function formatClock(seconds) {
    const value = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = Math.floor(value / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    const remainder = value % 60;
    return hours
      ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
      : `${minutes}:${String(remainder).padStart(2, "0")}`;
  }

  function timelineSegments() {
    const segments = state.current?.diarization_segments;
    return Array.isArray(segments) ? segments : [];
  }

  function timelineSpeakers() {
    const speakers = [];
    const seen = new Set();
    timelineSegments().forEach((segment) => {
      const speaker = String(segment.speaker || "").trim();
      const key = speaker.toLocaleLowerCase("vi");
      if (speaker && !seen.has(key)) {
        seen.add(key);
        speakers.push(speaker);
      }
    });
    return speakers;
  }

  function renderSpeakerTimeline() {
    const segments = timelineSegments();
    const speakers = timelineSpeakers();
    ui.speakerTimeline.replaceChildren();
    ui.speakerRenames.replaceChildren();

    if (!segments.length || !speakers.length) return;
    const duration = Math.max(...segments.map((segment) => Number(segment.end) || 0), 1);
    ui.timelineStats.textContent =
      `${speakers.length} người nói · ${segments.length} lượt · ${formatClock(duration)}`;

    speakers.forEach((speaker, speakerIndex) => {
      const row = document.createElement("div");
      row.className = "timeline-row";
      const label = document.createElement("span");
      label.textContent = speaker;
      label.title = speaker;
      const lane = document.createElement("div");
      lane.className = "timeline-lane";

      segments
        .filter((segment) => segment.speaker === speaker)
        .forEach((segment) => {
          const block = document.createElement("i");
          const start = Math.max(0, Number(segment.start) || 0);
          const end = Math.max(start, Number(segment.end) || start);
          block.style.left = `${(start / duration) * 100}%`;
          block.style.width = `${Math.max(((end - start) / duration) * 100, 0.12)}%`;
          block.style.backgroundColor = speakerColors[speakerIndex % speakerColors.length];
          block.title = `${speaker}: ${formatClock(start)} – ${formatClock(end)}`;
          lane.appendChild(block);
        });
      row.append(label, lane);
      ui.speakerTimeline.appendChild(row);

      const rename = document.createElement("label");
      rename.className = "speaker-rename";
      const original = document.createElement("span");
      original.textContent = speaker;
      const input = document.createElement("input");
      input.type = "text";
      input.maxLength = 200;
      input.value = speaker;
      input.dataset.originalSpeaker = speaker;
      input.setAttribute("aria-label", `Tên mới cho ${speaker}`);
      rename.append(original, input);
      ui.speakerRenames.appendChild(rename);
    });

    const axis = document.createElement("div");
    axis.className = "timeline-axis";
    const spacer = document.createElement("span");
    const ticks = document.createElement("div");
    for (let index = 0; index <= 4; index += 1) {
      const tick = document.createElement("span");
      tick.style.left = `${index * 25}%`;
      tick.textContent = formatClock((duration * index) / 4);
      ticks.appendChild(tick);
    }
    axis.append(spacer, ticks);
    ui.speakerTimeline.appendChild(axis);
  }

  function updateMeetingControls() {
    const hasTimeline = timelineSegments().length > 0;
    const summarizing = state.current?.status === "summarizing";
    const hasSummary = Boolean(state.current?.summary?.trim());
    const hasWord = hasSummary || Boolean(state.current?.has_word_document);

    ui.transcriptInspector.classList.toggle("hidden", !hasTimeline);
    ui.wordUpdateActions.classList.remove("hidden");
    ui.summarizeButton.disabled = summarizing || !state.current?.transcript?.trim();
    ui.summarizeButton.querySelector("span").textContent = summarizing
      ? "Đang tóm tắt..."
      : hasSummary
        ? "Tạo lại tóm tắt"
        : "Tóm tắt transcript";
    ui.markdownInput.disabled = summarizing;
    ui.saveButton.disabled = summarizing;
    ui.wordDownloadButton.disabled = !hasWord || summarizing;
    const wordUploadDisabled = !hasWord || summarizing;
    ui.wordUploadInput.disabled = wordUploadDisabled;
    ui.wordUploadInput.closest("label").classList.toggle("disabled", wordUploadDisabled);
    ui.wordSaveButton.disabled = !state.pendingWordFile || !hasWord || summarizing;
    if (hasTimeline) renderSpeakerTimeline();
    updateWordFileInfo();
  }

  function updateWordFileInfo() {
    if (state.pendingWordFile) {
      ui.wordFileName.textContent = state.pendingWordFile.name;
      ui.wordFileMeta.textContent =
        `${(state.pendingWordFile.size / 1024 / 1024).toFixed(2)} MB · Chưa lưu`;
      return;
    }
    if (state.current?.has_word_document || state.current?.summary?.trim()) {
      ui.wordFileName.textContent = state.current.word_filename || "Báo cáo cuộc họp.docx";
      const updated = state.current.word_updated_at || state.current.updated_at;
      ui.wordFileMeta.textContent = `Bản đang lưu · ${formatDate(updated, true)}`;
      return;
    }
    ui.wordFileName.textContent = "Chưa có báo cáo Word";
    ui.wordFileMeta.textContent = state.current?.status === "summarizing"
      ? "Đang tạo báo cáo từ transcript..."
      : "Tóm tắt transcript để tạo file đầu tiên.";
  }

  function refreshWordViewer(force = false) {
    const hasWord = Boolean(
      state.current?.has_word_document || state.current?.summary?.trim(),
    );
    if (!hasWord || state.current?.status === "summarizing") {
      ui.wordFrame.classList.add("hidden");
      ui.wordFrame.removeAttribute("src");
      ui.wordFrame.removeAttribute("srcdoc");
      ui.wordPlaceholder.classList.remove("hidden");
      const heading = ui.wordPlaceholder.querySelector("h3");
      const copy = ui.wordPlaceholder.querySelector("p");
      if (state.current?.status === "summarizing") {
        heading.textContent = "Đang tạo báo cáo Word";
        copy.textContent = "LLM đang tóm tắt transcript và hệ thống sẽ dựng file DOCX ngay sau đó.";
      } else {
        heading.textContent = "Chưa có báo cáo để hiển thị";
        copy.textContent = "Hoàn tất transcript rồi bấm “Tóm tắt transcript”. File Word sẽ xuất hiện tại đây.";
      }
      return;
    }

    ui.wordPlaceholder.classList.add("hidden");
    ui.wordFrame.classList.remove("hidden");
    ui.wordFrame.removeAttribute("srcdoc");
    const revision = force
      ? Date.now()
      : state.current.word_updated_at || state.current.updated_at || Date.now();
    ui.wordFrame.src =
      `/api/meetings/${encodeURIComponent(state.current.id)}/word/view?v=${encodeURIComponent(revision)}`;
  }

  function escapeRegExp(value) {
    return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function applySpeakerRenames() {
    if (!state.current) return;
    storeEditorValue();
    const mapping = new Map();
    ui.speakerRenames.querySelectorAll("input[data-original-speaker]").forEach((input) => {
      const original = input.dataset.originalSpeaker.trim();
      const replacement = input.value.trim();
      if (original && replacement && replacement !== original) {
        mapping.set(original.toLocaleLowerCase("vi"), replacement);
      }
    });
    if (!mapping.size) {
      toast("Chưa có tên người nói nào thay đổi.");
      return;
    }

    const aliases = timelineSpeakers().sort((a, b) => b.length - a.length);
    const pattern = new RegExp(
      `(^\\s*(?:\\[[^\\]\\n]+\\]\\s*)?)(${aliases.map(escapeRegExp).join("|")})(?=\\s*:)`,
      "gimu",
    );
    state.current.transcript = state.current.transcript.replace(
      pattern,
      (match, prefix, speaker) =>
        `${prefix}${mapping.get(speaker.toLocaleLowerCase("vi")) || speaker}`,
    );
    state.current.diarization_segments = timelineSegments().map((segment) => ({
      ...segment,
      speaker: mapping.get(String(segment.speaker).toLocaleLowerCase("vi")) || segment.speaker,
    }));
    ui.markdownInput.value = state.current.transcript;
    updateEditor();
    updateMeetingControls();
    queueSave();
    toast("Đã thay tên trong toàn bộ transcript.");
  }

  async function showUpload() {
    if (state.saveTimer) {
      window.clearTimeout(state.saveTimer);
      state.saveTimer = null;
      await saveMeeting(true);
    }
    state.current = null;
    state.pendingWordFile = null;
    ui.wordUploadInput.value = "";
    ui.wordFrame.removeAttribute("src");
    ui.wordFrame.removeAttribute("srcdoc");
    switchWorkspace("transcript", false);
    ui.meetingView.classList.add("hidden");
    ui.uploadView.classList.remove("hidden");
    ui.pageKicker.textContent = "Không gian làm việc";
    ui.pageTitle.textContent = "Cuộc họp mới";
    setSaveStatus("saved", "Đã lưu");
    renderHistory();
    closeSidebar();
    history.replaceState(null, "", location.pathname);
  }

  async function openMeeting(id, force = false, workspace = "transcript") {
    if (state.current?.id === id && !force) {
      closeSidebar();
      return;
    }
    if (state.saveTimer) {
      window.clearTimeout(state.saveTimer);
      state.saveTimer = null;
      await saveMeeting(true);
    }

    try {
      setSaveStatus("saving", "Đang mở...");
      state.current = await api(`/api/meetings/${encodeURIComponent(id)}`);
      state.pendingWordFile = null;
      ui.wordUploadInput.value = "";
      ui.meetingTitle.textContent = state.current.title;
      const statusText = state.current.status === "transcript_ready"
        ? "Transcript đã sẵn sàng"
        : state.current.status === "summary_error"
          ? "Có lỗi khi tóm tắt — transcript vẫn an toàn"
          : state.current.status === "summarizing"
            ? "Đang tạo bản tóm tắt"
            : "Đã hoàn tất";
      ui.meetingMeta.textContent = `${statusText} · ${formatDate(state.current.updated_at, true)}`;
      ui.pageKicker.textContent = "Ghi chú cuộc họp";
      ui.pageTitle.textContent = state.current.title;
      ui.uploadView.classList.add("hidden");
      ui.meetingView.classList.remove("hidden");
      loadMeetingContent(workspace);
      renderHistory();
      setSaveStatus("saved", "Đã lưu");
      closeSidebar();
      history.replaceState(null, "", `#meeting=${encodeURIComponent(id)}`);
    } catch (error) {
      setSaveStatus("error", "Không mở được");
      toast(error.message, true);
    }
  }

  function loadMeetingContent(workspace = "transcript") {
    ui.markdownInput.value = state.current?.transcript || "";
    updateEditor();
    updateMeetingControls();
    switchWorkspace(workspace);
  }

  function queueSave() {
    setSaveStatus("saving", "Chưa lưu");
    if (state.saveTimer) window.clearTimeout(state.saveTimer);
    state.saveTimer = window.setTimeout(() => {
      state.saveTimer = null;
      saveMeeting(true);
    }, 1000);
  }

  async function saveMeeting(silent = false) {
    if (!state.current) return false;
    storeEditorValue();
    if (state.saving) {
      state.saveAgain = true;
      return false;
    }

    state.saving = true;
    setSaveStatus("saving", "Đang lưu...");
    try {
      state.current = await api(
        `/api/meetings/${encodeURIComponent(state.current.id)}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            transcript: state.current.transcript,
            diarization_segments: state.current.diarization_segments || [],
          }),
        },
      );
      ui.meetingTitle.textContent = state.current.title;
      ui.pageTitle.textContent = state.current.title;
      updateMeetingControls();
      setSaveStatus("saved", "Đã lưu");
      await loadHistory();
      if (!silent) toast("Đã lưu thay đổi.");
      return true;
    } catch (error) {
      setSaveStatus("error", "Lưu thất bại");
      toast(`Không thể lưu: ${error.message}`, true);
      return false;
    } finally {
      state.saving = false;
      if (state.saveAgain) {
        state.saveAgain = false;
        saveMeeting(true);
      }
    }
  }

  function selectFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    state.files = files;
    ui.dropZone.classList.add("selected");
    ui.dropZone.querySelector(".drop-icon").innerHTML =
      '<svg viewBox="0 0 24 24"><path d="m5 12 4 4L19 6"/></svg>';
    const totalSize = files.reduce((sum, file) => sum + file.size, 0);
    ui.fileName.textContent = files.length === 1
      ? files[0].name
      : `${files.length} đoạn audio · ${(totalSize / 1024 / 1024).toFixed(1)} MB`;
    ui.fileDetail.textContent =
      `Thứ tự ghép: ${files.map((file, index) => `${index + 1}. ${file.name}`).join(" → ")}`;
    updateUploadButtonState();
  }

  function updateUploadButtonState() {
    ui.uploadButton.disabled = !state.files.length || !ui.meetingTitleInput.value.trim();
  }

  function updateDiarizationControls() {
    const selectedEngine = ui.engine.selectedOptions[0];
    const supported = selectedEngine?.dataset.diarization === "true";
    if (!supported) ui.diarization.checked = false;
    ui.diarization.disabled = !supported;
    const enabled = supported && ui.diarization.checked;
    ui.diarizationOptions.classList.toggle("hidden", !enabled);
    ui.speakerCount.disabled = !enabled;
    ui.minSpeakerTurn.disabled = !enabled;
    ui.diarizationNote.textContent = supported
      ? "Dùng pyannote trước khi chạy model ASR đã chọn."
      : "Model transcript đã chọn chưa hỗ trợ tách người nói.";
  }

  function setProgress(status, message) {
    const stages = {
      uploading: ["Đang tải file lên...", 8],
      queued: ["Đang chờ xử lý...", 14],
      splitting: ["Đang chuẩn bị audio...", 28],
      diarizing: ["Đang tách người nói...", 46],
      transcribing: ["Đang tạo transcript...", 70],
      transcript_ready: ["Transcript đã sẵn sàng", 100],
      summarizing: ["Đang tạo bản tóm tắt...", 88],
      completed: ["Hoàn tất", 100],
    };
    const [title, percent] = stages[status] || ["Đang xử lý...", 20];
    ui.processTitle.textContent = title;
    ui.processMessage.textContent = message || "Vui lòng giữ trang này mở.";
    ui.progress.style.width = `${percent}%`;
  }

  async function upload() {
    const meetingTitle = ui.meetingTitleInput.value.trim();
    if (!state.files.length || !meetingTitle) {
      toast("Vui lòng nhập tên báo cáo và chọn bản ghi âm.", true);
      return;
    }
    ui.uploadCard.classList.add("hidden");
    ui.processingCard.classList.remove("hidden");
    setProgress("uploading");
    const form = new FormData();
    state.files.forEach((file) => form.append("files", file));
    form.append("engine", state.settings.engine);
    form.append("title", meetingTitle);
    form.append("diarization", String(ui.diarization.checked));
    if (ui.diarization.checked) {
      form.append("speaker_count", ui.speakerCount.value.trim());
      form.append("min_speaker_turn", ui.minSpeakerTurn.value.trim());
    }

    try {
      const payload = await api("/upload", { method: "POST", body: form });
      pollStatus(payload.job_id);
    } catch (error) {
      ui.uploadCard.classList.remove("hidden");
      ui.processingCard.classList.add("hidden");
      toast(error.message, true);
    }
  }

  function pollStatus(jobId) {
    if (state.poll) window.clearInterval(state.poll);
    const poll = async () => {
      try {
        const payload = await api(`/status/${encodeURIComponent(jobId)}`);
        setProgress(payload.status, payload.message);
        if (payload.status === "transcript_ready") {
          window.clearInterval(state.poll);
          state.poll = null;
          await loadHistory();
          await openMeeting(jobId);
          resetUpload();
          toast("Transcript đã sẵn sàng. Hãy kiểm tra trước khi tóm tắt.");
        } else if (payload.status === "error") {
          throw new Error(payload.message);
        }
      } catch (error) {
        if (state.poll) window.clearInterval(state.poll);
        state.poll = null;
        ui.uploadCard.classList.remove("hidden");
        ui.processingCard.classList.add("hidden");
        toast(error.message, true);
      }
    };
    poll();
    state.poll = window.setInterval(poll, 3000);
  }

  function resetUpload() {
    state.files = [];
    ui.meetingTitleInput.value = "";
    ui.fileInput.value = "";
    ui.dropZone.classList.remove("selected");
    ui.dropZone.querySelector(".drop-icon").innerHTML =
      '<svg viewBox="0 0 24 24"><path d="M12 16V4M7.5 8.5 12 4l4.5 4.5M5 13v5a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-5"/></svg>';
    ui.fileName.textContent = "Thả các file vào đây hoặc bấm để chọn";
    ui.fileDetail.textContent = "MP3, WAV, M4A, MP4, OGG, FLAC hoặc WEBM";
    ui.diarization.checked = false;
    ui.speakerCount.value = "";
    ui.minSpeakerTurn.value = "2.0";
    updateDiarizationControls();
    ui.uploadButton.disabled = true;
    ui.uploadCard.classList.remove("hidden");
    ui.processingCard.classList.add("hidden");
  }

  async function summarizeTranscript() {
    if (!state.current) return;
    storeEditorValue();
    if (!state.current.transcript.trim()) {
      toast("Transcript đang trống.", true);
      return;
    }
    if (state.saveTimer) {
      window.clearTimeout(state.saveTimer);
      state.saveTimer = null;
    }

    try {
      while (state.saving) {
        await new Promise((resolve) => window.setTimeout(resolve, 50));
      }
      const saved = await saveMeeting(true);
      if (!saved) throw new Error("Chưa lưu được transcript mới nhất.");

      await api(
        `/api/meetings/${encodeURIComponent(state.current.id)}/summarize`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            transcript: state.current.transcript,
            diarization_segments: state.current.diarization_segments || [],
            summary_prompt: state.settings.summaryPrompt,
          }),
        },
      );
      state.current.status = "summarizing";
      updateMeetingControls();
      refreshWordViewer();
      setSaveStatus("saving", "Đang tóm tắt...");
      toast("Đã gửi transcript đã chỉnh sửa tới LLM.");
      pollSummary(state.current.id);
    } catch (error) {
      updateMeetingControls();
      setSaveStatus("error", "Không thể tóm tắt");
      toast(error.message, true);
    }
  }

  function pollSummary(jobId) {
    if (state.poll) window.clearInterval(state.poll);
    const poll = async () => {
      try {
        const payload = await api(`/status/${encodeURIComponent(jobId)}`);
        if (payload.status === "completed") {
          window.clearInterval(state.poll);
          state.poll = null;
          await loadHistory();
          await openMeeting(jobId, true, "word");
          toast("Bản tóm tắt đã hoàn tất.");
        } else if (payload.status === "summary_error") {
          window.clearInterval(state.poll);
          state.poll = null;
          await openMeeting(jobId, true);
          setSaveStatus("error", "Tóm tắt thất bại");
          toast(payload.message || "Không thể tạo bản tóm tắt.", true);
        } else if (payload.status === "error") {
          throw new Error(payload.message);
        }
      } catch (error) {
        if (state.poll) window.clearInterval(state.poll);
        state.poll = null;
        if (state.current) state.current.status = "summary_error";
        updateMeetingControls();
        setSaveStatus("error", "Tóm tắt thất bại");
        toast(error.message, true);
      }
    };
    poll();
    state.poll = window.setInterval(poll, 3000);
  }

  function applyFormat(format) {
    const input = ui.markdownInput;
    input.focus();
    const start = input.selectionStart;
    const end = input.selectionEnd;
    const selected = input.value.slice(start, end);
    let replacement;
    let selectionStart = start;
    let selectionEnd = end;

    if (["bold", "italic", "code", "link"].includes(format)) {
      const marker = format === "bold" ? "**" : format === "italic" ? "*" : "`";
      const placeholder = selected || (format === "code" ? "code" : "văn bản");
      if (format === "link") {
        replacement = `[${selected || "liên kết"}](https://)`;
        selectionStart = start + (selected || "liên kết").length + 3;
        selectionEnd = selectionStart + 8;
      } else {
        replacement = `${marker}${placeholder}${marker}`;
        selectionStart = start + marker.length;
        selectionEnd = selectionStart + placeholder.length;
      }
      input.setRangeText(replacement, start, end, "end");
    } else {
      const lineStart = input.value.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
      const nextBreak = input.value.indexOf("\n", end);
      const lineEnd = nextBreak < 0 ? input.value.length : nextBreak;
      const lines = input.value.slice(lineStart, lineEnd).split("\n");
      const prefixes = { heading: "## ", bullet: "- ", quote: "> " };
      replacement = lines
        .map((line, index) =>
          format === "number" ? `${index + 1}. ${line}` : `${prefixes[format]}${line}`,
        )
        .join("\n");
      input.setRangeText(replacement, lineStart, lineEnd, "end");
      selectionStart = lineStart;
      selectionEnd = lineStart + replacement.length;
    }

    input.setSelectionRange(selectionStart, selectionEnd);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function setEditorMode(mode) {
    ui.editorBody.className = `editor-body ${mode}`;
    document.querySelectorAll("[data-mode]").forEach((button) => {
      button.classList.toggle("active", button.dataset.mode === mode);
    });
    if (mode !== "preview") ui.markdownInput.focus();
  }

  function downloadDocument() {
    if (!state.current) return;
    storeEditorValue();
    const content = state.current.transcript || "";
    const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const title = state.current.title
      .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_")
      .slice(0, 80);
    link.href = url;
    link.download = `${title}.transcript.md`;
    link.click();
    URL.revokeObjectURL(url);
  }

  async function downloadWordDocument() {
    if (!state.current) return;

    try {
      const response = await fetch(
        `/api/meetings/${encodeURIComponent(state.current.id)}/word?document=summary`,
      );
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error || `Lỗi máy chủ (${response.status})`);
      }

      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = state.current.word_filename || "bao-cao-cuoc-hop.docx";
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("Đã tải báo cáo Word.");
    } catch (error) {
      toast(`Không thể tải Word: ${error.message}`, true);
    }
  }

  function selectWordFile(fileList) {
    const file = Array.from(fileList || [])[0];
    if (!file) return;
    if (!file.name.toLocaleLowerCase("vi").endsWith(".docx")) {
      ui.wordUploadInput.value = "";
      toast("Chỉ hỗ trợ file Word có đuôi .docx.", true);
      return;
    }
    if (file.size > 25 * 1024 * 1024) {
      ui.wordUploadInput.value = "";
      toast("File Word vượt quá giới hạn 25 MB.", true);
      return;
    }
    state.pendingWordFile = file;
    updateMeetingControls();
    setSaveStatus("saving", "File Word chưa lưu");
  }

  async function saveWordFile() {
    if (!state.current || !state.pendingWordFile) return;
    const form = new FormData();
    form.append("word_file", state.pendingWordFile);
    ui.wordSaveButton.disabled = true;
    setSaveStatus("saving", "Đang cập nhật Word...");
    try {
      const result = await api(
        `/api/meetings/${encodeURIComponent(state.current.id)}/word`,
        { method: "POST", body: form },
      );
      state.current = result;
      state.pendingWordFile = null;
      ui.wordUploadInput.value = "";
      updateMeetingControls();
      refreshWordViewer(true);
      setSaveStatus("saved", "Đã lưu file Word");
      await loadHistory();
      toast("Đã cập nhật báo cáo Word trong hệ thống.");
    } catch (error) {
      updateMeetingControls();
      setSaveStatus("error", "Lưu Word thất bại");
      toast(`Không thể lưu file Word: ${error.message}`, true);
    }
  }

  function bindEvents() {
    ui.menu.addEventListener("click", () => document.body.classList.add("sidebar-open"));
    ui.closeSidebar.addEventListener("click", closeSidebar);
    ui.backdrop.addEventListener("click", closeSidebar);
    ui.newMeeting.addEventListener("click", showUpload);
    ui.historySearch.addEventListener("input", renderHistory);
    ui.settingsButton.addEventListener("click", openSettings);
    ui.settingsBackdrop.addEventListener("click", closeSettings);
    ui.settingsClose.addEventListener("click", closeSettings);
    ui.settingsCancel.addEventListener("click", closeSettings);
    ui.settingsReset.addEventListener("click", resetSettingsForm);
    ui.settingsSave.addEventListener("click", saveSettings);
    document.querySelectorAll("[data-workspace-target]").forEach((button) => {
      button.addEventListener("click", () => switchWorkspace(button.dataset.workspaceTarget));
    });

    ui.dropZone.addEventListener("click", () => ui.fileInput.click());
    ui.fileInput.addEventListener("change", () => selectFiles(ui.fileInput.files));
    ui.meetingTitleInput.addEventListener("input", updateUploadButtonState);
    ui.engine.addEventListener("change", updateDiarizationControls);
    ui.diarization.addEventListener("change", updateDiarizationControls);
    ui.dropZone.addEventListener("dragover", (event) => {
      event.preventDefault();
      ui.dropZone.classList.add("drag");
    });
    ui.dropZone.addEventListener("dragleave", () => ui.dropZone.classList.remove("drag"));
    ui.dropZone.addEventListener("drop", (event) => {
      event.preventDefault();
      ui.dropZone.classList.remove("drag");
      selectFiles(event.dataTransfer.files);
    });
    ui.uploadButton.addEventListener("click", upload);

    document.querySelectorAll("[data-format]").forEach((button) => {
      button.addEventListener("click", () => applyFormat(button.dataset.format));
    });
    document.querySelectorAll("[data-mode]").forEach((button) => {
      button.addEventListener("click", () => setEditorMode(button.dataset.mode));
    });
    ui.markdownInput.addEventListener("input", () => {
      storeEditorValue();
      updateEditor();
      updateMeetingControls();
      queueSave();
    });
    ui.saveButton.addEventListener("click", () => saveMeeting());
    ui.summarizeButton.addEventListener("click", summarizeTranscript);
    ui.applySpeakerNames.addEventListener("click", applySpeakerRenames);
    ui.downloadButton.addEventListener("click", downloadDocument);
    ui.wordDownloadButton.addEventListener("click", downloadWordDocument);
    ui.wordUploadInput.addEventListener("change", () => {
      selectWordFile(ui.wordUploadInput.files);
    });
    ui.wordSaveButton.addEventListener("click", saveWordFile);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !ui.settingsModal.classList.contains("hidden")) {
        event.preventDefault();
        closeSettings();
        return;
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        if (!ui.settingsModal.classList.contains("hidden")) {
          saveSettings();
          return;
        }
        if (state.pendingWordFile) saveWordFile();
        else saveMeeting();
      }
    });
  }

  async function init() {
    loadSettings();
    bindEvents();
    updateDiarizationControls();
    await loadHistory();
    const match = location.hash.match(/^#meeting=([^&]+)$/);
    if (match) {
      await openMeeting(decodeURIComponent(match[1]));
    } else {
      await showUpload();
    }
  }

  init();
})();
