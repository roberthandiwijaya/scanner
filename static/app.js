const invoiceInput = document.querySelector("#invoice");
const duplicateWarning = document.querySelector("#duplicateWarning");
const modeInputs = [...document.querySelectorAll("input[name='mode']")];
const audioEnabledInput = document.querySelector("#audioEnabled");
const autoStartEnabledInput = document.querySelector("#autoStartEnabled");
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
const orderDialog = document.querySelector("#orderDialog");
const orderDialogClose = document.querySelector("#orderDialogClose");
const orderLookupStatus = document.querySelector("#orderLookupStatus");
const orderDetails = document.querySelector("#orderDetails");
const orderTrackingNumber = document.querySelector("#orderTrackingNumber");
const orderNumber = document.querySelector("#orderNumber");
const orderPackageNumber = document.querySelector("#orderPackageNumber");
const orderShippingCarrier = document.querySelector("#orderShippingCarrier");
const orderItemCount = document.querySelector("#orderItemCount");
const orderItems = document.querySelector("#orderItems");
const orderItemTemplate = document.querySelector("#orderItemTemplate");
const orderMatchBtn = document.querySelector("#orderMatchBtn");
const orderMismatchBtn = document.querySelector("#orderMismatchBtn");
const orderMatchStatus = document.querySelector("#orderMatchStatus");

const PER_PAGE = 10;
const COUNTDOWN_SECONDS = 3;

let stream = null;
let recorder = null;
let chunks = [];
let recordedBlob = null;
let recordedDurationMs = 0;
let startedAt = 0;
let timerHandle = null;
let searchDebounce = null;
let duplicateDebounce = null;
let currentPage = 1;
let currentTotalPages = 1;
let orderLookupController = null;
let startAfterOrderMatch = false;

function selectedMode() {
  return modeInputs.find((input) => input.checked).value;
}

function audioEnabled() {
  return audioEnabledInput.checked;
}

function autoStartEnabled() {
  return autoStartEnabledInput.checked;
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
  autoStartEnabledInput.disabled = isRecording;
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
  autoStartEnabledInput.disabled = isCountingDown;
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

function displayValue(value) {
  return value === null || value === undefined || value === "" ? "Not available" : String(value);
}

function openOrderDialog() {
  if (!orderDialog.open) {
    orderDialog.showModal();
  }
}

function setOrderLookupStatus(text, isError = false) {
  orderLookupStatus.textContent = text;
  orderLookupStatus.classList.toggle("error", isError);
  orderLookupStatus.hidden = !text;
}

function renderOrderItem(item) {
  const node = orderItemTemplate.content.cloneNode(true);
  const image = node.querySelector(".order-item-image");

  node.querySelector(".order-item-name").textContent = displayValue(item.item_name);
  node.querySelector(".order-item-model").textContent = displayValue(item.model_name);
  node.querySelector(".order-item-sku").textContent = displayValue(item.model_sku);
  node.querySelector(".order-item-quantity").textContent = displayValue(item.model_quantity_purchased);

  if (item.image_url) {
    image.src = item.image_url;
    image.alt = item.item_name ? `Product image for ${item.item_name}` : "Product image";
    image.addEventListener("error", () => {
      image.hidden = true;
    });
  } else {
    image.hidden = true;
  }

  orderItems.appendChild(node);
}

function renderOrderDetails(order) {
  orderTrackingNumber.textContent = displayValue(order.tracking_number);
  orderNumber.textContent = displayValue(order.order_sn);
  orderPackageNumber.textContent = displayValue(order.package_number);
  orderShippingCarrier.textContent = displayValue(order.shipping_carrier);
  orderItems.textContent = "";
  orderMatchStatus.textContent = "";
  orderMatchStatus.classList.remove("error", "success");

  const items = Array.isArray(order.items) ? order.items : [];
  orderItemCount.textContent = `${items.length} ${items.length === 1 ? "product" : "products"}`;

  if (items.length) {
    items.forEach(renderOrderItem);
  } else {
    const empty = document.createElement("p");
    empty.className = "order-items-empty";
    empty.textContent = "No product details were returned for this shipment.";
    orderItems.appendChild(empty);
  }

  setOrderLookupStatus("");
  orderDetails.hidden = false;
}

async function lookupShippingOrder(trackingNumber) {
  orderLookupController?.abort();
  orderLookupController = new AbortController();
  orderDetails.hidden = true;
  setOrderLookupStatus(`Looking up ${trackingNumber}...`);
  openOrderDialog();

  try {
    const response = await fetch(
      `/api/orders/by-tracking-number?tracking_number=${encodeURIComponent(trackingNumber)}`,
      { signal: orderLookupController.signal }
    );
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "Could not find shipping information.");
    }

    renderOrderDetails(payload);
  } catch (error) {
    if (error.name === "AbortError") {
      return;
    }
    startAfterOrderMatch = false;
    orderDetails.hidden = true;
    setOrderLookupStatus(error.message || "Could not find shipping information.", true);
  }
}

function confirmOrderMatch() {
  const trackingNumber = orderTrackingNumber.textContent;
  const shouldStartRecording = startAfterOrderMatch;
  startAfterOrderMatch = false;
  setMessage(`Product details confirmed for ${trackingNumber}.`);
  orderDialog.close();

  if (shouldStartRecording) {
    startRecording();
  }
}

function reportOrderMismatch() {
  startAfterOrderMatch = false;
  orderMatchStatus.textContent = "Mismatch selected. Recording was not started. Check the shelf products.";
  orderMatchStatus.classList.remove("success");
  orderMatchStatus.classList.add("error");
  setMessage("Product details do not match. Check the shelf products before recording.", true);
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
    recordedDurationMs = 0;
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
      recordedDurationMs = Date.now() - startedAt;
      stopTimer();
      setRecordingUi(false);
      setMessage("Recording stopped. Save it, or retry if you want to record again.");
      cameraStatus.textContent = "Recording ready to save";
    });

    recorder.start();
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

  resetRecordingDraft();
}

function resetRecordingDraft() {
  chunks = [];
  recordedBlob = null;
  recordedDurationMs = 0;
  timer.textContent = "00:00";
  setRecordingUi(false);
  setMessage("Ready to record again.");
  cameraStatus.textContent = stream ? "Camera ready" : "Camera idle";
}

async function uploadVideoBlob(videoBlob, fileName = "recording.webm", expectedDurationMs = null) {
  const invoice = invoiceInput.value.trim();

  const form = new FormData();
  form.append("invoice", invoice);
  form.append("mode", selectedMode());
  form.append("video", videoBlob, fileName);
  if (expectedDurationMs !== null) {
    form.append("duration_ms", String(Math.max(Math.round(expectedDurationMs), 0)));
  }

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

async function hasWebmHeader(videoBlob) {
  const header = new Uint8Array(await videoBlob.slice(0, 4).arrayBuffer());
  return header[0] === 0x1a && header[1] === 0x45 && header[2] === 0xdf && header[3] === 0xa3;
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
    if (!(await hasWebmHeader(recordedBlob))) {
      throw new Error("Recording file is incomplete. Please retry the recording.");
    }

    await uploadVideoBlob(recordedBlob, `${invoice}.webm`, recordedDurationMs);
    setMessage("Saved locally.");
    resetRecordingDraft();
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

function initAutoStartPreference() {
  autoStartEnabledInput.checked = localStorage.getItem("scanner-start-on-enter") === "1";
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
autoStartEnabledInput.addEventListener("change", () => {
  localStorage.setItem("scanner-start-on-enter", autoStartEnabled() ? "1" : "0");
});
invoiceInput.addEventListener("input", queueDuplicateCheck);
invoiceInput.addEventListener("blur", () => checkDuplicateInvoice());
orderDialogClose.addEventListener("click", () => orderDialog.close());
orderMatchBtn.addEventListener("click", confirmOrderMatch);
orderMismatchBtn.addEventListener("click", reportOrderMismatch);
orderDialog.addEventListener("click", (event) => {
  if (event.target === orderDialog) {
    orderDialog.close();
  }
});
orderDialog.addEventListener("close", () => {
  orderLookupController?.abort();
  orderLookupController = null;
  startAfterOrderMatch = false;
  if (!invoiceInput.disabled) {
    invoiceInput.focus();
  }
});

invoiceInput.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") {
    return;
  }

  event.preventDefault();
  const trackingNumber = invoiceInput.value.trim();

  if (trackingNumber) {
    startAfterOrderMatch = autoStartEnabled();
    lookupShippingOrder(trackingNumber);
  } else {
    setMessage("Scan or type the invoice / shipping receipt first.", true);
  }
});

initTheme();
initAudioPreference();
initAutoStartPreference();
setRecordingUi(false);
Promise.all([loadResults(), loadSummary()]);
