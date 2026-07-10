const recordingSearch = document.querySelector("#recordingSearch");
const recordingSelect = document.querySelector("#recordingSelect");
const resolutionSelect = document.querySelector("#resolutionSelect");
const qualitySelect = document.querySelector("#qualitySelect");
const refreshBtn = document.querySelector("#refreshBtn");
const convertBtn = document.querySelector("#convertBtn");
const convertMessage = document.querySelector("#convertMessage");
const durationValue = document.querySelector("#durationValue");
const sourceValue = document.querySelector("#sourceValue");
const estimateValue = document.querySelector("#estimateValue");
const themeToggle = document.querySelector("#themeToggle");
const convertedEmpty = document.querySelector("#convertedEmpty");
const convertedResult = document.querySelector("#convertedResult");
const convertedVideo = document.querySelector("#convertedVideo");
const downloadConverted = document.querySelector("#downloadConverted");
const actualSizeValue = document.querySelector("#actualSizeValue");
const actualEstimateValue = document.querySelector("#actualEstimateValue");
const conversionProgress = document.querySelector("#conversionProgress");
const progressPercent = document.querySelector("#progressPercent");
const progressBar = document.querySelector("#progressBar");

let searchDebounce = null;
let progressPoll = null;

function setMessage(text, isError = false) {
  convertMessage.textContent = text;
  convertMessage.classList.toggle("error", isError);
}

function setProgress(value) {
  const progress = Math.max(0, Math.min(100, Math.round(value || 0)));
  conversionProgress.hidden = false;
  progressPercent.textContent = `${progress}%`;
  progressBar.style.setProperty("--progress", `${progress}%`);
}

function stopProgressPolling() {
  if (progressPoll) {
    window.clearInterval(progressPoll);
    progressPoll = null;
  }
}

function formatFileSize(bytes) {
  if (!bytes) {
    return "-";
  }

  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }

  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatRecording(item) {
  const date = new Date(item.created_at).toLocaleString();
  return `${item.invoice} - ${date} - ${formatFileSize(item.size_bytes)}`;
}

function selectedRecordingId() {
  return recordingSelect.value;
}

async function loadRecordings() {
  const params = new URLSearchParams();
  const query = recordingSearch.value.trim();
  if (query) {
    params.set("q", query);
  }

  recordingSelect.disabled = true;
  recordingSelect.innerHTML = "<option>Loading...</option>";
  setMessage("");

  try {
    const response = await fetch(`/api/convert/recordings?${params.toString()}`);
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "Could not load recordings.");
    }

    if (!payload.items.length) {
      recordingSelect.innerHTML = "<option value=\"\">No WebM recordings found</option>";
      resetEstimate();
      return;
    }

    recordingSelect.innerHTML = "";
    payload.items.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = formatRecording(item);
      recordingSelect.appendChild(option);
    });
    recordingSelect.disabled = false;
    await updateEstimate();
  } catch (error) {
    recordingSelect.innerHTML = "<option value=\"\">Could not load recordings</option>";
    resetEstimate();
    setMessage(error.message, true);
  }
}

function resetEstimate() {
  durationValue.textContent = "-";
  sourceValue.textContent = "-";
  estimateValue.textContent = "-";
}

async function updateEstimate() {
  const recordingId = selectedRecordingId();
  if (!recordingId) {
    resetEstimate();
    return;
  }

  const params = new URLSearchParams({
    recording_id: recordingId,
    resolution: resolutionSelect.value,
    quality: qualitySelect.value
  });

  try {
    const response = await fetch(`/api/convert/estimate?${params.toString()}`);
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "Could not estimate output size.");
    }

    durationValue.textContent = payload.duration_label;
    sourceValue.textContent = `${payload.source_width}x${payload.source_height} - ${formatFileSize(
      payload.source_size_bytes
    )}`;
    estimateValue.textContent = formatFileSize(payload.estimated_size_bytes);
    setMessage("");
  } catch (error) {
    resetEstimate();
    setMessage(error.message, true);
  }
}

async function convertRecording() {
  const recordingId = selectedRecordingId();
  if (!recordingId) {
    setMessage("Choose a WebM recording first.", true);
    return;
  }

  convertBtn.disabled = true;
  recordingSelect.disabled = true;
  resolutionSelect.disabled = true;
  qualitySelect.disabled = true;
  convertedResult.hidden = true;
  convertedEmpty.hidden = false;
  setProgress(0);
  setMessage("Converting to MP4...");

  try {
    const response = await fetch("/api/convert", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        recording_id: recordingId,
        resolution: resolutionSelect.value,
        quality: qualitySelect.value
      })
    });
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "Conversion failed.");
    }

    pollConversionJob(payload.job_id);
  } catch (error) {
    setMessage(error.message, true);
    convertBtn.disabled = false;
    recordingSelect.disabled = false;
    resolutionSelect.disabled = false;
    qualitySelect.disabled = false;
  }
}

function finishConversion(payload) {
  const result = payload.result;
  convertedEmpty.hidden = true;
  convertedResult.hidden = false;
  convertedVideo.src = result.video_url;
  downloadConverted.href = result.download_url;
  downloadConverted.download = result.filename.split("/").pop();
  actualSizeValue.textContent = formatFileSize(result.size_bytes);
  actualEstimateValue.textContent = formatFileSize(result.estimated_size_bytes);
  setProgress(100);
  setMessage("MP4 conversion finished.");
  convertBtn.disabled = false;
  recordingSelect.disabled = false;
  resolutionSelect.disabled = false;
  qualitySelect.disabled = false;
}

async function loadConversionJob(jobId) {
  const response = await fetch(`/api/convert/jobs/${jobId}`);
  const payload = await response.json();

  if (!response.ok) {
    throw new Error(payload.error || "Could not read conversion progress.");
  }

  setProgress(payload.progress || 0);
  setMessage(payload.message || "Converting to MP4...");

  if (payload.status === "complete") {
    stopProgressPolling();
    finishConversion(payload);
    return;
  }

  if (payload.status === "failed") {
    stopProgressPolling();
    setMessage(payload.message || "Conversion failed.", true);
    convertBtn.disabled = false;
    recordingSelect.disabled = false;
    resolutionSelect.disabled = false;
    qualitySelect.disabled = false;
  }
}

function pollConversionJob(jobId) {
  stopProgressPolling();
  loadConversionJob(jobId).catch((error) => {
    stopProgressPolling();
    setMessage(error.message, true);
    convertBtn.disabled = false;
    recordingSelect.disabled = false;
    resolutionSelect.disabled = false;
    qualitySelect.disabled = false;
  });
  progressPoll = window.setInterval(() => {
    loadConversionJob(jobId).catch((error) => {
      stopProgressPolling();
      setMessage(error.message, true);
      convertBtn.disabled = false;
      recordingSelect.disabled = false;
      resolutionSelect.disabled = false;
      qualitySelect.disabled = false;
    });
  }, 1000);
}

function queueSearch() {
  window.clearTimeout(searchDebounce);
  searchDebounce = window.setTimeout(loadRecordings, 250);
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

refreshBtn.addEventListener("click", loadRecordings);
recordingSearch.addEventListener("input", queueSearch);
recordingSelect.addEventListener("change", updateEstimate);
resolutionSelect.addEventListener("change", updateEstimate);
qualitySelect.addEventListener("change", updateEstimate);
convertBtn.addEventListener("click", convertRecording);
themeToggle.addEventListener("click", toggleTheme);

initTheme();
loadRecordings();
