(() => {
  "use strict";

  const state = {
    meetings: [],
    current: null,
    document: "summary",
    files: [],
    poll: null,
    saveTimer: null,
    saving: false,
    saveAgain: false,
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
    uploadButton: $("upload-button"),
    processTitle: $("process-title"),
    processMessage: $("process-message"),
    progress: $("progress-fill"),
    meetingTitle: $("meeting-title"),
    meetingMeta: $("meeting-meta"),
    markdownInput: $("markdown-input"),
    markdownPreview: $("markdown-preview"),
    documentStats: $("document-stats"),
    editorBody: $("editor-body"),
    saveButton: $("save-button"),
    downloadButton: $("download-button"),
    wordDownloadButton: $("word-download-button"),
    toasts: $("toasts"),
  };

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
          breaks: state.document === "transcript",
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
      const button = document.createElement("button");
      button.type = "button";
      button.className = `history-item${
        state.current?.id === meeting.id ? " active" : ""
      }`;

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
      button.append(icon, copy);
      button.addEventListener("click", () => openMeeting(meeting.id));
      ui.history.appendChild(button);
    });
  }

  function storeEditorValue() {
    if (state.current) {
      state.current[state.document] = ui.markdownInput.value;
    }
  }

  function updateEditor() {
    const content = ui.markdownInput.value;
    ui.markdownPreview.innerHTML = renderMarkdown(content);
    const words = content.trim() ? content.trim().split(/\s+/).length : 0;
    ui.documentStats.textContent =
      `${words.toLocaleString("vi-VN")} từ · ${content.length.toLocaleString("vi-VN")} ký tự`;
  }

  async function showUpload() {
    if (state.saveTimer) {
      window.clearTimeout(state.saveTimer);
      state.saveTimer = null;
      await saveMeeting(true);
    }
    state.current = null;
    ui.meetingView.classList.add("hidden");
    ui.uploadView.classList.remove("hidden");
    ui.pageKicker.textContent = "Không gian làm việc";
    ui.pageTitle.textContent = "Cuộc họp mới";
    setSaveStatus("saved", "Đã lưu");
    renderHistory();
    closeSidebar();
    history.replaceState(null, "", location.pathname);
  }

  async function openMeeting(id) {
    if (state.current?.id === id) {
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
      state.document = "summary";
      ui.meetingTitle.textContent = state.current.title;
      ui.meetingMeta.textContent = formatDate(state.current.updated_at, true);
      ui.pageKicker.textContent = "Ghi chú cuộc họp";
      ui.pageTitle.textContent = state.current.title;
      ui.uploadView.classList.add("hidden");
      ui.meetingView.classList.remove("hidden");
      loadDocument();
      renderHistory();
      setSaveStatus("saved", "Đã lưu");
      closeSidebar();
      history.replaceState(null, "", `#meeting=${encodeURIComponent(id)}`);
    } catch (error) {
      setSaveStatus("error", "Không mở được");
      toast(error.message, true);
    }
  }

  function loadDocument() {
    ui.markdownInput.value = state.current?.[state.document] || "";
    document.querySelectorAll(".tab").forEach((tab) => {
      tab.classList.toggle("active", tab.dataset.document === state.document);
    });
    updateEditor();
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
            summary: state.current.summary,
            transcript: state.current.transcript,
          }),
        },
      );
      ui.meetingTitle.textContent = state.current.title;
      ui.pageTitle.textContent = state.current.title;
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

  function setProgress(status, message) {
    const stages = {
      uploading: ["Đang tải file lên...", 8],
      queued: ["Đang chờ xử lý...", 14],
      splitting: ["Đang chuẩn bị audio...", 28],
      transcribing: ["Đang tạo transcript...", 62],
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
    form.append("engine", ui.engine.value);
    form.append("title", meetingTitle);

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
        if (payload.status === "completed") {
          window.clearInterval(state.poll);
          state.poll = null;
          await loadHistory();
          await openMeeting(jobId);
          resetUpload();
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
    ui.uploadButton.disabled = true;
    ui.uploadCard.classList.remove("hidden");
    ui.processingCard.classList.add("hidden");
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
    const content = state.current[state.document] || "";
    const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const title = state.current.title
      .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_")
      .slice(0, 80);
    link.href = url;
    link.download = `${title}.${state.document}.md`;
    link.click();
    URL.revokeObjectURL(url);
  }

  async function downloadWordDocument() {
    if (!state.current) return;
    storeEditorValue();
    if (state.saveTimer) {
      window.clearTimeout(state.saveTimer);
      state.saveTimer = null;
    }

    try {
      while (state.saving) {
        await new Promise((resolve) => window.setTimeout(resolve, 50));
      }
      const saved = await saveMeeting(true);
      if (!saved) {
        throw new Error("Chưa lưu được nội dung mới nhất.");
      }
      while (state.saving) {
        await new Promise((resolve) => window.setTimeout(resolve, 50));
      }

      const response = await fetch(
        `/api/meetings/${encodeURIComponent(state.current.id)}/word?document=${encodeURIComponent(state.document)}`,
      );
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error || `Lỗi máy chủ (${response.status})`);
      }

      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      const title = state.current.title
        .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_")
        .slice(0, 80);
      link.href = url;
      link.download = `${title}.${state.document}.docx`;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("Đã tạo file Word.");
    } catch (error) {
      toast(`Không thể tải Word: ${error.message}`, true);
    }
  }

  function bindEvents() {
    ui.menu.addEventListener("click", () => document.body.classList.add("sidebar-open"));
    ui.closeSidebar.addEventListener("click", closeSidebar);
    ui.backdrop.addEventListener("click", closeSidebar);
    ui.newMeeting.addEventListener("click", showUpload);
    ui.historySearch.addEventListener("input", renderHistory);

    ui.dropZone.addEventListener("click", () => ui.fileInput.click());
    ui.fileInput.addEventListener("change", () => selectFiles(ui.fileInput.files));
    ui.meetingTitleInput.addEventListener("input", updateUploadButtonState);
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

    document.querySelectorAll(".tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        if (!state.current || state.document === tab.dataset.document) return;
        storeEditorValue();
        state.document = tab.dataset.document;
        loadDocument();
      });
    });
    document.querySelectorAll("[data-format]").forEach((button) => {
      button.addEventListener("click", () => applyFormat(button.dataset.format));
    });
    document.querySelectorAll("[data-mode]").forEach((button) => {
      button.addEventListener("click", () => setEditorMode(button.dataset.mode));
    });
    ui.markdownInput.addEventListener("input", () => {
      storeEditorValue();
      updateEditor();
      queueSave();
    });
    ui.saveButton.addEventListener("click", () => saveMeeting());
    ui.downloadButton.addEventListener("click", downloadDocument);
    ui.wordDownloadButton.addEventListener("click", downloadWordDocument);
    document.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        saveMeeting();
      }
    });
  }

  async function init() {
    bindEvents();
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
