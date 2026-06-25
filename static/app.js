const invoiceInput = document.querySelector("#invoice");
const duplicateWarning = document.querySelector("#duplicateWarning");
const modeInputs = [...document.querySelectorAll("input[name='mode']")];
const audioEnabledInput = document.querySelector("#audioEnabled");
const preview = document.querySelector("#preview");
const countdown = document.querySelector("#countdown");
const startBtn = document.querySelector("#startBtn");
const stopBtn = document.querySelector("#stopBtn");
const retryBtn = document.querySelector("#retryBtn");
const saveBtn = document.querySelector("#saveBtn");
const cameraOffBtn = document.querySelector("#cameraOffBtn");
const videoUpload = document.querySelector("#videoUpload");
const uploadBtn = document.querySelector("#uploadBtn");
const refreshBtn = document.querySelector("#refreshBtn");
const searchInput = document.querySelector("#search");
const dateFromInput = document.querySelector("#dateFrom");
const dateToInput = document.querySelector("#dateTo");
const clearFiltersBtn = document.querySelector("#clearFiltersBtn");
const resultsSummary = document.querySelector("#resultsSummary");
const results = document.querySelector("#results");
const resultTemplate = document.querySelector("#resultTemplate");
const pagination = document.querySelector("#pagination");
const prevPageBtn = document.querySelector("#prevPageBtn");
const nextPageBtn = document.querySelector("#nextPageBtn");
const pageInfo = document.querySelector("#pageInfo");
const message = document.querySelector("#message");
const timer = document.querySelector("#timer");
const cameraStatus = document.querySelector("#cameraStatus");
const themeToggle = document.querySelector("#themeToggle");
const totalRecordings = document.querySelector("#totalRecordings");
const totalStorage = document.querySelector("#totalStorage");
const todayScans = document.querySelector("#todayScans");
const dailyCounts = document.querySelector("#dailyCounts");

const PER_PAGE = 10;
const COUNTDOWN_SECONDS = 3;

let stream = null;
let recorder = null;
let chunks = [];
let recordedBlob = null;
let startedAt = 0;
let timerHandle = null;
let searchDebounce = null;
let duplicateDebounce = null;
let currentPage = 1;
let currentTotalPages = 1;

function selectedMode() {
  return modeInputs.find((input) => input.checked).value;
}

function audioEnabled() {
  return audioEnabledInput.checked;
}

function setMessage(text, isError = false) {
  message.textContent = text;
  message.classList.toggle("error", isError);
}

function setRecordingUi(isRecording) {
  startBtn.disabled = isRecording;
  stopBtn.disabled = !isRecording;
  retryBtn.disabled = isRecording || !recordedBlob;
  saveBtn.disabled = isRecording || !recordedBlob;
  cameraOffBtn.disabled = isRecording || !stream;
  invoiceInput.disabled = isRecording;
  modeInputs.forEach((input) => {
    input.disabled = isRecording;
  });
  audioEnabledInput.disabled = isRecording;
}

function setCountdownUi(isCountingDown) {
  startBtn.disabled = isCountingDown;
  stopBtn.disabled = true;
  retryBtn.disabled = true;
  saveBtn.disabled = true;
  cameraOffBtn.disabled = true;
  invoiceInput.disabled = isCountingDown;
  modeInputs.forEach((input) => {
    input.disabled = isCountingDown;
  });
  audioEnabledInput.disabled = isCountingDown;
}

function formatTimer(seconds) {
  const minutes = Math.floor(seconds / 60).toString().padStart(2, "0");
  const rest = Math.floor(seconds % 60).toString().padStart(2, "0");
  return `${minutes}:${rest}`;
}

function startTimer() {
  startedAt = Date.now();
  timer.textContent = "00:00";
  timerHandle = window.setInterval(() => {
    timer.textContent = formatTimer((Date.now() - startedAt) / 1000);
  }, 500);
}

function stopTimer() {
  window.clearInterval(timerHandle);
  timerHandle = null;
}

function wait(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function runCountdown() {
  setCountdownUi(true);
  countdown.hidden = false;

  for (let value = COUNTDOWN_SECONDS; value > 0; value -= 1) {
    countdown.textContent = value;
    cameraStatus.textContent = `Recording starts in ${value}`;
    await wait(1000);
  }

  countdown.textContent = "GO";
  cameraStatus.textContent = "Recording";
  await wait(250);
  countdown.hidden = true;
  countdown.textContent = "";
}

async function ensureCamera() {
  if (stream) {
    return stream;
  }

  stream = await navigator.mediaDevices.getUserMedia({
    audio: audioEnabled()
      ? {
          echoCancellation: true,
          noiseSuppression: true
        }
      : false,
    video: {
      width: { ideal: 1280 },
      height: { ideal: 720 },
      frameRate: { ideal: 24, max: 30 }
    }
  });
  preview.srcObject = stream;
  cameraStatus.textContent = "Camera ready";
  cameraOffBtn.disabled = false;
  return stream;
}

function getMimeType() {
  const options = audioEnabled()
    ? [
        "video/webm;codecs=vp9,opus",
        "video/webm;codecs=vp8,opus",
        "video/webm;codecs=opus",
        "video/webm"
      ]
    : [
        "video/webm;codecs=vp9",
        "video/webm;codecs=vp8",
        "video/webm"
      ];

  return options.find((type) => MediaRecorder.isTypeSupported(type)) || "";
}

async function startRecording() {
  const invoice = invoiceInput.value.trim();

  if (!invoice) {
    setMessage("Scan or type the invoice / shipping receipt first.", true);
    invoiceInput.focus();
    return;
  }

  const mimeType = getMimeType();
  if (!mimeType) {
    setMessage("This browser cannot record WebM video. Try Chrome, Edge, or Firefox.", true);
    cameraStatus.textContent = "WebM unavailable";
    return;
  }

  try {
    const camera = await ensureCamera();
    chunks = [];
    recordedBlob = null;
    timer.textContent = "00:00";
    setMessage("Get ready...");

    await runCountdown();

    recorder = new MediaRecorder(camera, { mimeType });

    recorder.addEventListener("dataavailable", (event) => {
      if (event.data.size > 0) {
        chunks.push(event.data);
      }
    });

    recorder.addEventListener("stop", () => {
      recordedBlob = new Blob(chunks, { type: recorder.mimeType || mimeType });
      stopTimer();
      setRecordingUi(false);
      setMessage("Recording stopped. Save it, or retry if you want to record again.");
      cameraStatus.textContent = "Recording ready to save";
    });

    recorder.start(1000);
    startTimer();
    setRecordingUi(true);
    setMessage("Recording...");
    cameraStatus.textContent = "Recording";
  } catch (error) {
    countdown.hidden = true;
    countdown.textContent = "";
    setRecordingUi(false);
    setMessage(`Camera error: ${error.message}`, true);
    cameraStatus.textContent = "Camera unavailable";
  }
}

function stopRecording() {
  if (recorder && recorder.state === "recording") {
    recorder.stop();
  }
}

function stopWebcamPreview() {
  if (recorder && recorder.state === "recording") {
    setMessage("Stop the recording before turning off the webcam preview.", true);
    return;
  }

  if (!stream) {
    setMessage("Webcam preview is already stopped.");
    cameraOffBtn.disabled = true;
    return;
  }

  stream.getTracks().forEach((track) => track.stop());
  stream = null;
  preview.srcObject = null;
  countdown.hidden = true;
  countdown.textContent = "";
  cameraOffBtn.disabled = true;
  cameraStatus.textContent = "Camera stopped";
  setMessage("Webcam preview stopped. Start recording will turn it back on.");
}

function resetCameraForAudioChange() {
  localStorage.setItem("scanner-record-audio", audioEnabled() ? "1" : "0");

  if (!stream || (recorder && recorder.state === "recording")) {
    return;
  }

  stopWebcamPreview();
  setMessage("Audio setting updated. Start recording will turn the webcam on again.");
}

function retryRecording() {
  if (recordedBlob && !window.confirm("Are you sure you want to retry? The current recording data cannot be restored.")) {
    return;
  }

  chunks = [];
  recordedBlob = null;
  timer.textContent = "00:00";
  setRecordingUi(false);
  setMessage("Ready to record again.");
  cameraStatus.textContent = stream ? "Camera ready" : "Camera idle";
}

async function uploadVideoBlob(videoBlob, fileName = "recording.webm") {
  const invoice = invoiceInput.value.trim();

  const form = new FormData();
  form.append("invoice", invoice);
  form.append("mode", selectedMode());
  form.append("video", videoBlob, fileName);

  const response = await fetch("/api/recordings", {
    method: "POST",
    body: form
  });
  const payload = await response.json();

  if (!response.ok) {
    throw new Error(payload.error || "Save failed.");
  }

  searchInput.value = payload.invoice;
  currentPage = 1;
  await Promise.all([loadResults(payload.invoice), loadSummary(), checkDuplicateInvoice()]);
  return payload;
}

async function saveRecording() {
  const invoice = invoiceInput.value.trim();

  if (!recordedBlob || !invoice) {
    setMessage("Nothing to save yet.", true);
    return;
  }

  saveBtn.disabled = true;
  setMessage("Saving recording...");

  try {
    await uploadVideoBlob(recordedBlob, `${invoice}.webm`);
    setMessage("Saved locally.");
    retryRecording();
    invoiceInput.value = "";
    duplicateWarning.textContent = "";
    duplicateWarning.classList.remove("active");
    invoiceInput.focus();
  } catch (error) {
    setMessage(`Save error: ${error.message}`, true);
    saveBtn.disabled = false;
  }
}

async function uploadExistingVideo() {
  const invoice = invoiceInput.value.trim();
  const file = videoUpload.files[0];

  if (!invoice) {
    setMessage("Scan or type the invoice / shipping receipt first.", true);
    invoiceInput.focus();
    return;
  }

  if (!file) {
    setMessage("Choose a video file to upload.", true);
    videoUpload.focus();
    return;
  }

  uploadBtn.disabled = true;
  setMessage("Uploading video...");

  try {
    await uploadVideoBlob(file, file.name);
    setMessage("Uploaded and saved locally.");
    videoUpload.value = "";
    invoiceInput.value = "";
    duplicateWarning.textContent = "";
    duplicateWarning.classList.remove("active");
    invoiceInput.focus();
  } catch (error) {
    setMessage(`Upload error: ${error.message}`, true);
  } finally {
    uploadBtn.disabled = false;
  }
}

function formatMode(mode) {
  return mode === "return" ? "Return unboxing" : "Packing";
}

function formatFileSize(bytes) {
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }

  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatVideoFormat(item) {
  const extension = item.file_extension || item.filename.split(".").pop() || "video";
  return `${extension.toUpperCase()} (${item.content_type || "video"})`;
}

function describeRecording(item) {
  const date = new Date(item.created_at);
  return `${formatMode(item.mode)} - ${date.toLocaleString()} - ${formatFileSize(item.size_bytes)}`;
}

function buildRecordingParams(query = searchInput.value.trim()) {
  const params = new URLSearchParams({
    page: String(currentPage),
    per_page: String(PER_PAGE)
  });

  if (query) {
    params.set("q", query);
  }

  if (dateFromInput.value) {
    params.set("date_from", dateFromInput.value);
  }

  if (dateToInput.value) {
    params.set("date_to", dateToInput.value);
  }

  return params;
}

function setLoadingState(text) {
  results.innerHTML = `<div class="empty-state loading-state">${text}</div>`;
  resultsSummary.textContent = "";
}

function setEmptyState(text, detail = "") {
  results.innerHTML = `
    <div class="empty-state">
      <strong>${text}</strong>
      ${detail ? `<span>${detail}</span>` : ""}
    </div>
  `;
}

async function deleteRecording(item, card) {
  if (!window.confirm(`Delete recording for ${item.invoice}? This cannot be undone.`)) {
    return;
  }

  const deleteButton = card.querySelector(".delete-recording");
  deleteButton.disabled = true;
  deleteButton.textContent = "Deleting...";
  setMessage("Deleting recording...");

  try {
    const response = await fetch(`/api/recordings/${item.id}`, {
      method: "DELETE"
    });
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "Delete failed.");
    }

    await Promise.all([loadResults(), loadSummary(), checkDuplicateInvoice()]);
    setMessage("Recording deleted.");
  } catch (error) {
    deleteButton.disabled = false;
    deleteButton.textContent = "Delete";
    setMessage(`Delete error: ${error.message}`, true);
  }
}

function renderRecording(item) {
  const node = resultTemplate.content.cloneNode(true);
  const card = node.querySelector(".result-card");
  const downloadLink = node.querySelector(".download-recording");
  const deleteButton = node.querySelector(".delete-recording");
  const date = new Date(item.created_at);

  node.querySelector(".result-invoice").textContent = item.invoice;
  node.querySelector(".result-meta").textContent = describeRecording(item);
  node.querySelector(".result-label").textContent = item.invoice;
  node.querySelector(".result-mode").textContent = formatMode(item.mode);
  node.querySelector(".result-created").textContent = date.toLocaleString();
  node.querySelector(".result-format").textContent = formatVideoFormat(item);
  node.querySelector(".result-size").textContent = formatFileSize(item.size_bytes);
  node.querySelector(".result-video").src = item.video_url;
  downloadLink.href = item.download_url || `${item.video_url}?download=1`;
  downloadLink.download = item.filename.split("/").pop();
  deleteButton.addEventListener("click", () => deleteRecording(item, card));

  results.appendChild(node);
}

function updatePagination(payload) {
  currentPage = payload.page;
  currentTotalPages = payload.total_pages;
  pagination.hidden = payload.total_pages <= 1;
  pageInfo.textContent = `Page ${payload.page} of ${payload.total_pages}`;
  prevPageBtn.disabled = payload.page <= 1;
  nextPageBtn.disabled = payload.page >= payload.total_pages;
}

async function loadResults(query = searchInput.value.trim()) {
  setLoadingState("Loading scanned label logs...");

  try {
    const response = await fetch(`/api/recordings?${buildRecordingParams(query).toString()}`);
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "Could not load recordings.");
    }

    results.textContent = "";
    updatePagination(payload);

    if (!payload.items.length) {
      resultsSummary.textContent = "";
      const hasFilters = query || dateFromInput.value || dateToInput.value;
      setEmptyState(
        hasFilters ? "No matching scanned labels." : "No scanned labels saved yet.",
        hasFilters ? "Try a different label or date range." : "Scan a label, record, and save to build the log."
      );
      return;
    }

    const shownStart = (payload.page - 1) * payload.per_page + 1;
    const shownEnd = shownStart + payload.items.length - 1;
    resultsSummary.textContent = `Showing ${shownStart}-${shownEnd} of ${payload.total} scanned label ${
      payload.total === 1 ? "log" : "logs"
    }.`;

    payload.items.forEach(renderRecording);
  } catch (error) {
    pagination.hidden = true;
    resultsSummary.textContent = "";
    setEmptyState("Could not load scanned labels.", error.message);
  }
}

function renderDailyCounts(days) {
  dailyCounts.textContent = "";
  const maxCount = Math.max(...days.map((day) => day.count), 1);

  days.forEach((day) => {
    const item = document.createElement("div");
    const label = document.createElement("span");
    const bar = document.createElement("i");
    const count = document.createElement("strong");
    const date = new Date(`${day.date}T00:00:00`);

    label.textContent = date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    bar.style.setProperty("--bar-width", `${Math.max((day.count / maxCount) * 100, day.count ? 10 : 0)}%`);
    count.textContent = day.count;

    item.append(label, bar, count);
    dailyCounts.appendChild(item);
  });
}

async function loadSummary() {
  try {
    const response = await fetch("/api/summary");
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "Could not load summary.");
    }

    totalRecordings.textContent = payload.total_recordings;
    totalStorage.textContent = formatFileSize(payload.total_size_bytes);
    todayScans.textContent = payload.today_recordings;
    renderDailyCounts(payload.daily_counts);
  } catch (error) {
    totalRecordings.textContent = "-";
    totalStorage.textContent = "-";
    todayScans.textContent = "-";
    dailyCounts.textContent = "Summary unavailable";
  }
}

async function checkDuplicateInvoice(invoice = invoiceInput.value.trim()) {
  if (!invoice) {
    duplicateWarning.textContent = "";
    duplicateWarning.classList.remove("active");
    return;
  }

  try {
    const response = await fetch(`/api/recordings/duplicate?invoice=${encodeURIComponent(invoice)}`);
    const payload = await response.json();

    if (!response.ok || !payload.count) {
      duplicateWarning.textContent = "";
      duplicateWarning.classList.remove("active");
      return;
    }

    const latest = payload.latest ? new Date(payload.latest.created_at).toLocaleString() : "unknown time";
    duplicateWarning.textContent = `Duplicate label warning: ${payload.count} recording${
      payload.count === 1 ? "" : "s"
    } already saved. Latest: ${latest}.`;
    duplicateWarning.classList.add("active");
  } catch (error) {
    duplicateWarning.textContent = "";
    duplicateWarning.classList.remove("active");
  }
}

function queueSearch() {
  window.clearTimeout(searchDebounce);
  searchDebounce = window.setTimeout(() => {
    currentPage = 1;
    loadResults();
  }, 250);
}

function queueDuplicateCheck() {
  window.clearTimeout(duplicateDebounce);
  duplicateDebounce = window.setTimeout(() => checkDuplicateInvoice(), 300);
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("scanner-theme", theme);
  themeToggle.textContent = theme === "dark" ? "Light mode" : "Dark mode";
}

function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
}

function initTheme() {
  const savedTheme = localStorage.getItem("scanner-theme");
  applyTheme(savedTheme === "dark" ? "dark" : "light");
}

function initAudioPreference() {
  audioEnabledInput.checked = localStorage.getItem("scanner-record-audio") === "1";
}

startBtn.addEventListener("click", startRecording);
stopBtn.addEventListener("click", stopRecording);
retryBtn.addEventListener("click", retryRecording);
saveBtn.addEventListener("click", saveRecording);
cameraOffBtn.addEventListener("click", stopWebcamPreview);
uploadBtn.addEventListener("click", uploadExistingVideo);
refreshBtn.addEventListener("click", () => {
  currentPage = 1;
  Promise.all([loadResults(), loadSummary()]);
});
searchInput.addEventListener("input", queueSearch);
dateFromInput.addEventListener("change", () => {
  currentPage = 1;
  loadResults();
});
dateToInput.addEventListener("change", () => {
  currentPage = 1;
  loadResults();
});
clearFiltersBtn.addEventListener("click", () => {
  searchInput.value = "";
  dateFromInput.value = "";
  dateToInput.value = "";
  currentPage = 1;
  loadResults();
});
prevPageBtn.addEventListener("click", () => {
  if (currentPage > 1) {
    currentPage -= 1;
    loadResults();
  }
});
nextPageBtn.addEventListener("click", () => {
  if (currentPage < currentTotalPages) {
    currentPage += 1;
    loadResults();
  }
});
themeToggle.addEventListener("click", toggleTheme);
audioEnabledInput.addEventListener("change", resetCameraForAudioChange);
invoiceInput.addEventListener("input", queueDuplicateCheck);
invoiceInput.addEventListener("blur", () => checkDuplicateInvoice());

invoiceInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    startRecording();
  }
});

initTheme();
initAudioPreference();
setRecordingUi(false);
Promise.all([loadResults(), loadSummary()]);
