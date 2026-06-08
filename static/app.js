const invoiceInput = document.querySelector("#invoice");
const modeInputs = [...document.querySelectorAll("input[name='mode']")];
const preview = document.querySelector("#preview");
const startBtn = document.querySelector("#startBtn");
const stopBtn = document.querySelector("#stopBtn");
const retryBtn = document.querySelector("#retryBtn");
const saveBtn = document.querySelector("#saveBtn");
const videoUpload = document.querySelector("#videoUpload");
const uploadBtn = document.querySelector("#uploadBtn");
const refreshBtn = document.querySelector("#refreshBtn");
const searchInput = document.querySelector("#search");
const resultsSummary = document.querySelector("#resultsSummary");
const results = document.querySelector("#results");
const resultTemplate = document.querySelector("#resultTemplate");
const message = document.querySelector("#message");
const timer = document.querySelector("#timer");
const cameraStatus = document.querySelector("#cameraStatus");

let stream = null;
let recorder = null;
let chunks = [];
let recordedBlob = null;
let startedAt = 0;
let timerHandle = null;

function selectedMode() {
  return modeInputs.find((input) => input.checked).value;
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
  invoiceInput.disabled = isRecording;
  modeInputs.forEach((input) => {
    input.disabled = isRecording;
  });
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

async function ensureCamera() {
  if (stream) {
    return stream;
  }

  stream = await navigator.mediaDevices.getUserMedia({
    audio: false,
    video: {
      width: { ideal: 1280 },
      height: { ideal: 720 },
      frameRate: { ideal: 24, max: 30 }
    }
  });
  preview.srcObject = stream;
  cameraStatus.textContent = "Camera ready";
  return stream;
}

function getMimeType() {
  const options = [
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

  try {
    const mimeType = getMimeType();
    if (!mimeType) {
      setMessage("This browser cannot record WebM video. Try Chrome, Edge, or Firefox.", true);
      cameraStatus.textContent = "WebM unavailable";
      return;
    }

    const camera = await ensureCamera();
    chunks = [];
    recordedBlob = null;
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
    setMessage(`Camera error: ${error.message}`, true);
    cameraStatus.textContent = "Camera unavailable";
  }
}

function stopRecording() {
  if (recorder && recorder.state === "recording") {
    recorder.stop();
  }
}

function retryRecording() {
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
  await loadResults(payload.invoice);
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
    invoiceInput.focus();
  } catch (error) {
    setMessage(`Upload error: ${error.message}`, true);
  } finally {
    uploadBtn.disabled = false;
  }
}

function describeRecording(item) {
  const date = new Date(item.created_at);
  return `${formatMode(item.mode)} - ${date.toLocaleString()} - ${formatFileSize(item.size_bytes)}`;
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

    await loadResults();
    setMessage("Recording deleted.");
  } catch (error) {
    deleteButton.disabled = false;
    deleteButton.textContent = "Delete";
    setMessage(`Delete error: ${error.message}`, true);
  }
}

async function loadResults(query = searchInput.value.trim()) {
  const response = await fetch(`/api/recordings?q=${encodeURIComponent(query)}`);
  const items = await response.json();
  results.textContent = "";
  resultsSummary.textContent = "";

  if (!items.length) {
    results.textContent = query ? "No recordings found." : "No recordings saved yet.";
    return;
  }

  resultsSummary.textContent = `Showing ${items.length} scanned label ${items.length === 1 ? "log" : "logs"}.`;

  for (const item of items) {
    const node = resultTemplate.content.cloneNode(true);
    const card = node.querySelector(".result-card");
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
    deleteButton.addEventListener("click", () => deleteRecording(item, card));
    results.appendChild(node);
  }
}

let searchDebounce = null;
function queueSearch() {
  window.clearTimeout(searchDebounce);
  searchDebounce = window.setTimeout(() => loadResults(), 250);
}

startBtn.addEventListener("click", startRecording);
stopBtn.addEventListener("click", stopRecording);
retryBtn.addEventListener("click", retryRecording);
saveBtn.addEventListener("click", saveRecording);
uploadBtn.addEventListener("click", uploadExistingVideo);
refreshBtn.addEventListener("click", () => loadResults());
searchInput.addEventListener("input", queueSearch);

invoiceInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    startRecording();
  }
});

setRecordingUi(false);
loadResults();
