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
const scanCameraBtn = document.querySelector("#scanCameraBtn");
const cancelScanBtn = document.querySelector("#cancelScanBtn");
const cameraFacing = document.querySelector("#cameraFacing");
const scanStatus = document.querySelector("#scanStatus");
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

const PER_PAGE = 10;
const COUNTDOWN_SECONDS = 3;
const ORDER_LOOKUP_DELAY_MS = 600;
const MIN_TRACKING_NUMBER_LENGTH = 6;

let stream = null;
let recorder = null;
let chunks = [];
let recordedBlob = null;
let recordedDurationMs = 0;
let startedAt = 0;
let timerHandle = null;
let searchDebounce = null;
let duplicateDebounce = null;
let orderLookupDebounce = null;
let currentPage = 1;
let currentTotalPages = 1;
let startAfterOrderMatch = false;
let activeScanContext = null;
let recordingContext = null;
let uploadContext = null;
let startingRecording = false;
let uploadingVideo = false;
let inputRevision = 0;
let debouncedSubmission = null;
let lookupSequence = 0;
let displayedScanContext = null;
let scanning = false;
let scanSession = 0;
let scanTimer = null;
let cameraGeneration = 0;
let openingCamera = null;
let savingRecording = false;

function captureBusy() {
  return startingRecording || uploadingVideo || savingRecording || recorder?.state === "recording";
}

function updateCameraControls() {
  scanCameraBtn.disabled = captureBusy() || scanning;
  cancelScanBtn.hidden = !scanning;
  cameraFacing.disabled = captureBusy() || Boolean(openingCamera);
  startBtn.disabled = scanning || captureBusy();
  uploadBtn.disabled = scanning || captureBusy();
  saveBtn.disabled = scanning || captureBusy() || !recordedBlob;
  retryBtn.disabled = scanning || captureBusy() || !recordedBlob;
}

function focusInvoice() {
  if (!window.matchMedia?.("(pointer: coarse)").matches) invoiceInput.focus();
}

function releaseCamera() {
  cameraGeneration++;
  const previous = stream;
  stream = null;
  preview.srcObject = null;
  previous?.getTracks().forEach((track) => track.stop());
  cameraOffBtn.disabled = true;
}

function stopCameraScan(text = "Scan cancelled.", release = true) {
  scanning = false;
  scanSession++;
  window.clearTimeout(scanTimer);
  scanTimer = null;
  if (release) releaseCamera();
  scanStatus.textContent = text;
  updateCameraControls();
}

function trackingTextFromBarcode(value) {
  // QR contents are identifiers, not navigation or structured commands.
  const raw = String(value);
  if (/[\u0000-\u001f\u007f]/.test(raw)) return null;
  const text = raw.trim().toUpperCase();
  if (!/^[A-Z0-9][A-Z0-9_-]{5,127}$/.test(text)) return null;
  return text;
}

function acceptCameraResult(value, session) {
  if (!scanning || session !== scanSession || captureBusy()) return false;
  const tracking = trackingTextFromBarcode(value);
  if (!tracking) {
    scanStatus.textContent = "This code is not a plain tracking number. Scan the shipping barcode or type it manually.";
    return false;
  }
  stopCameraScan(`Scanned ${tracking}. Review the order details.`, false);
  invoiceInput.value = tracking;
  handleInvoiceInput(false);
  beginShippingOrderLookup(tracking, "camera");
  return true;
}

async function startCameraScan() {
  if (captureBusy() || scanning) return;
  if (!window.ZXingBrowser?.BrowserMultiFormatReader) {
    setMessage("Barcode decoder unavailable. Reload the page or enter the tracking number manually.", true);
    return;
  }
  window.clearTimeout(orderLookupDebounce);
  // Invalidate pending order callbacks before a new camera submission.
  lookupSequence++;
  displayedScanContext = null;
  orderMatchBtn.disabled = true;
  startAfterOrderMatch = false;
  if (orderDialog.open) orderDialog.close();
  scanning = true;
  const session = ++scanSession;
  scanStatus.textContent = "Opening camera… Allow camera access, then aim at the barcode.";
  updateCameraControls();
  try {
    await ensureCamera(false);
    if (!scanning || session !== scanSession) return;
    const { BrowserMultiFormatReader, BarcodeFormat } = window.ZXingBrowser;
    const reader = new BrowserMultiFormatReader();
    reader.possibleFormats = [BarcodeFormat.CODE_39, BarcodeFormat.CODE_128, BarcodeFormat.ITF, BarcodeFormat.QR_CODE];
    scanStatus.textContent = "Aim at one shipping barcode or tracking-number QR. Hold steady in good light.";
    const decodeFrame = () => {
      if (!scanning || session !== scanSession) return;
      try {
        if (preview.readyState >= 2) {
          const result = reader.decode(preview);
          if (acceptCameraResult(result.getText(), session)) return;
        }
      } catch (error) {
        // Missing/partial barcodes are normal while aiming the camera.
        if (!["NotFoundException", "ChecksumException", "FormatException"].includes(error.getKind?.() || error.name)) {
          stopCameraScan("Scanner stopped. Try scanning again.");
          setMessage(`Barcode scan failed: ${error.message}`, true);
          return;
        }
      }
      if (scanning && session === scanSession) scanTimer = window.setTimeout(decodeFrame, 250);
    };
    decodeFrame();
  } catch (error) {
    if (session !== scanSession) return;
    stopCameraScan("Camera unavailable. Check camera permission and trusted HTTPS.");
    setMessage(`Could not scan: ${error.message}`, true);
  }
}

function newSubmissionId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  // getRandomValues also works on LAN HTTP pages where randomUUID is unavailable.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function createScanContext(label, mode, source = "scan") {
  return { label, mode, source, submissionId: newSubmissionId(), event: null, pending: null, consumed: false };
}

async function persistScan(context) {
  if (context.event) return context.event;
  if (!context.pending) {
    context.pending = (async () => {
      const response = await fetch("/api/scan-events", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ submission_id: context.submissionId, tracking_number: context.label, mode: context.mode, source: context.source })
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Scan history could not be saved. Submit the label again to retry.");
      context.event = payload;
      return payload;
    })();
  }
  try {
    const event = await context.pending;
    context.persistenceFailed = false;
    return event;
  } catch (error) {
    context.persistenceFailed = true;
    throw error;
  } finally {
    context.pending = null;
  }
}

function matchingScanContext(label, mode) {
  return activeScanContext && !activeScanContext.consumed && activeScanContext.label === label && activeScanContext.mode === mode ? activeScanContext : null;
}

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

function uppercaseInvoiceInput() {
  const originalValue = invoiceInput.value;
  const upperValue = originalValue.toUpperCase();

  if (originalValue === upperValue) {
    return;
  }

  const selectionStart = invoiceInput.selectionStart;
  const selectionEnd = invoiceInput.selectionEnd;
  invoiceInput.value = upperValue;

  if (selectionStart !== null && selectionEnd !== null) {
    invoiceInput.setSelectionRange(selectionStart, selectionEnd);
  }
}

function setRecordingUi(isRecording) {
  uploadBtn.disabled = isRecording;
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
  updateCameraControls();
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

async function lookupShippingOrder(context) {
  const sequence = ++lookupSequence;
  displayedScanContext = null;
  orderMatchBtn.disabled = true;
  orderDetails.hidden = true;
  setOrderLookupStatus(`Saving scan and looking up ${context.label}...`);
  openOrderDialog();

  try {
    const event = await persistScan(context);
    // Do not abort persistence when the dialog closes or another scan starts.
    const response = await fetch(`/api/scan-events/${event.id}/lookup`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mutation_token: event.mutation_token })
    });
    const payload = await response.json();
    if (sequence !== lookupSequence) return;
    if (!response.ok) throw new Error(payload.error || "Could not find shipping information.");
    displayedScanContext = context;
    orderMatchBtn.disabled = false;
    renderOrderDetails(payload);
  } catch (error) {
    if (sequence !== lookupSequence) return;
    startAfterOrderMatch = false;
    orderDetails.hidden = true;
    setOrderLookupStatus(context.event ? error.message : `Scan history could not be saved: ${error.message}`, true);
  }
}

function confirmOrderMatch() {
  const context = displayedScanContext;
  if (!context || context !== activeScanContext) return;
  const trackingNumber = context.label;
  const shouldStartRecording = startAfterOrderMatch;
  startAfterOrderMatch = false;
  setMessage(`Product details confirmed for ${trackingNumber}.`);
  orderDialog.close();

  if (shouldStartRecording) {
    startRecording(context);
  }
}

function beginShippingOrderLookup(trackingNumber, trigger = "enter") {
  if (!trackingNumber || scanning || captureBusy()) return;
  if (trigger === "enter" && debouncedSubmission?.revision === inputRevision && !debouncedSubmission.enterSeen) {
    debouncedSubmission.enterSeen = true;
    return;
  }
  const mode = selectedMode();
  // A failed persistence attempt retries its original submission identifier.
  const retry = matchingScanContext(trackingNumber, mode);
  const context = retry?.persistenceFailed ? retry : createScanContext(trackingNumber, mode);
  activeScanContext = context;
  if (trigger === "debounce") debouncedSubmission = { revision: inputRevision, enterSeen: false };
  startAfterOrderMatch = autoStartEnabled();
  lookupShippingOrder(context);
}

function queueShippingOrderLookup() {
  window.clearTimeout(orderLookupDebounce);
  const trackingNumber = invoiceInput.value.trim();

  if (trackingNumber.length < MIN_TRACKING_NUMBER_LENGTH) {
    return;
  }

  orderLookupDebounce = window.setTimeout(() => {
    if (invoiceInput.value.trim() === trackingNumber) {
      beginShippingOrderLookup(trackingNumber, "debounce");
    }
  }, ORDER_LOOKUP_DELAY_MS);
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

async function ensureCamera(withAudio = audioEnabled()) {
  if (!navigator.mediaDevices?.getUserMedia || window.isSecureContext === false) {
    throw new Error("Use trusted HTTPS and allow camera access on this device.");
  }
  if (openingCamera) {
    try { await openingCamera; } catch (_) { /* A cancelled request may be retried. */ }
    return ensureCamera(withAudio);
  }
  const facing = cameraFacing.value || "environment";
  const videoTrack = stream?.getVideoTracks().find((track) => track.readyState === "live");
  const hasAudio = Boolean(stream?.getAudioTracks().some((track) => track.readyState === "live"));
  if (videoTrack && hasAudio === withAudio && stream.requestedFacing === facing) return stream;
  releaseCamera();
  const generation = cameraGeneration;
  const operation = (async () => {
    const camera = await navigator.mediaDevices.getUserMedia({
      audio: withAudio ? { echoCancellation: true, noiseSuppression: true } : false,
      video: {
        facingMode: { ideal: facing },
        width: { ideal: 1280 },
        height: { ideal: 720 },
        frameRate: { ideal: 24, max: 30 }
      }
    });
    if (generation !== cameraGeneration) {
      camera.getTracks().forEach((track) => track.stop());
      throw new Error("Camera request cancelled. Try again.");
    }
    camera.requestedFacing = facing;
    stream = camera;
    preview.srcObject = camera;
    try {
      await preview.play();
    } catch (error) {
      if (stream === camera) releaseCamera();
      throw error;
    }
    if (generation !== cameraGeneration) throw new Error("Camera request cancelled. Try again.");
    camera.getVideoTracks().forEach((track) => track.addEventListener("ended", () => {
      if (stream !== camera) return;
      if (scanning) stopCameraScan("Camera interrupted. Tap Scan with camera to retry.");
      else releaseCamera();
      if (recorder?.state === "recording") stopRecording();
      cameraStatus.textContent = "Camera interrupted";
    }));
    cameraStatus.textContent = "Camera ready";
    cameraOffBtn.disabled = false;
    return camera;
  })();
  openingCamera = operation;
  updateCameraControls();
  try { return await operation; }
  finally {
    if (openingCamera === operation) openingCamera = null;
    updateCameraControls();
  }
}

function getMimeType() {
  if (typeof MediaRecorder === "undefined" || !MediaRecorder.isTypeSupported) return "";
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

  options.push("video/mp4");
  return options.find((type) => MediaRecorder.isTypeSupported(type)) || "";
}

async function startRecording(requestedContext = null) {
  if (scanning || captureBusy()) return;
  const context = requestedContext?.submissionId ? requestedContext : null;
  const invoice = context?.label || invoiceInput.value.trim();
  const mode = context?.mode || selectedMode();

  if (!invoice) {
    setMessage("Scan or type the invoice / shipping receipt first.", true);
    focusInvoice();
    return;
  }

  const mimeType = getMimeType();
  if (!mimeType) {
    setMessage("This browser cannot record supported video. Use an up-to-date Android Chrome or iPhone Safari, or upload an existing video.", true);
    cameraStatus.textContent = "Recording unavailable";
    return;
  }

  if (recordedBlob && !window.confirm("Discard the unsaved recording and start a new one?")) return;
  recordedBlob = null;
  chunks = [];
  recordedDurationMs = 0;
  startingRecording = true;
  setRecordingUi(true);
  stopBtn.disabled = true;
  uploadBtn.disabled = true;
  window.clearTimeout(orderLookupDebounce);
  recordingContext = context || matchingScanContext(invoice, mode) || createScanContext(invoice, mode, "manual_recording");
  activeScanContext = recordingContext;
  try {
    await persistScan(recordingContext);
    const camera = await ensureCamera();
    chunks = [];
    recordedBlob = null;
    recordedDurationMs = 0;
    timer.textContent = "00:00";
    setMessage("Get ready...");

    await runCountdown();
    if (stream !== camera || !camera.getVideoTracks().some((track) => track.readyState === "live")) {
      throw new Error("Camera was interrupted during countdown. Please try again.");
    }

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
    setMessage(`Could not start recording: ${error.message}`, true);
    cameraStatus.textContent = "Recording unavailable";
  } finally {
    startingRecording = false;
    uploadBtn.disabled = recorder?.state === "recording";
    updateCameraControls();
  }
}

function stopRecording() {
  if (recorder && recorder.state === "recording") {
    recorder.stop();
  }
}

function stopWebcamPreview() {
  if (startingRecording || (recorder && recorder.state === "recording")) {
    setMessage("Stop the recording before turning off the webcam preview.", true);
    return;
  }

  if (scanning) stopCameraScan();
  if (openingCamera) releaseCamera();
  if (!stream) {
    setMessage("Webcam preview is already stopped.");
    cameraOffBtn.disabled = true;
    return;
  }

  releaseCamera();
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
  recordingContext = null;
  chunks = [];
  recordedBlob = null;
  recordedDurationMs = 0;
  timer.textContent = "00:00";
  setRecordingUi(false);
  setMessage("Ready to record again.");
  cameraStatus.textContent = stream ? "Camera ready" : "Camera idle";
}

async function uploadVideoBlob(videoBlob, fileName = "recording.webm", expectedDurationMs = null, context) {
  const event = await persistScan(context);
  const form = new FormData();
  form.append("invoice", context.label);
  form.append("mode", context.mode);
  form.append("scan_event_id", String(event.id));
  form.append("scan_event_token", event.mutation_token);
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

  context.consumed = true;
  searchInput.value = payload.invoice;
  currentPage = 1;
  // A refresh failure must not turn a successful save into a second upload.
  await Promise.allSettled([loadResults(payload.invoice), loadSummary(), checkDuplicateInvoice()]);
  return payload;
}

async function hasWebmHeader(videoBlob) {
  const header = new Uint8Array(await videoBlob.slice(0, 4).arrayBuffer());
  return header[0] === 0x1a && header[1] === 0x45 && header[2] === 0xdf && header[3] === 0xa3;
}

async function recordingExtension(videoBlob) {
  const mime = videoBlob.type.split(";")[0].toLowerCase();
  if (mime === "video/webm") {
    if (!(await hasWebmHeader(videoBlob))) throw new Error("Recording file is incomplete. Please retry the recording.");
    return "webm";
  }
  if (mime === "video/mp4") {
    const header = new Uint8Array(await videoBlob.slice(0, 12).arrayBuffer());
    if (header.length < 12 || String.fromCharCode(...header.slice(4, 8)) !== "ftyp") {
      throw new Error("MP4 recording file is incomplete. Please retry the recording.");
    }
    return "mp4";
  }
  throw new Error("Unsupported recording format. Please retry in a supported browser.");
}

async function saveRecording() {
  if (scanning || captureBusy()) return;
  const invoice = recordingContext?.label;

  if (!recordedBlob || !invoice) {
    setMessage("Nothing to save yet.", true);
    return;
  }

  savingRecording = true;
  setRecordingUi(true);
  stopBtn.disabled = true;
  setMessage("Saving recording...");

  try {
    const extension = await recordingExtension(recordedBlob);
    await uploadVideoBlob(recordedBlob, `${invoice}.${extension}`, recordedDurationMs, recordingContext);
    setMessage("Saved locally.");
    resetRecordingDraft();
    if (invoiceInput.value.trim() === invoice) invoiceInput.value = "";
    duplicateWarning.textContent = "";
    duplicateWarning.classList.remove("active");
    focusInvoice();
  } catch (error) {
    setMessage(`Save error: ${error.message}`, true);
  } finally {
    savingRecording = false;
    setRecordingUi(false);
  }
}

async function uploadExistingVideo() {
  if (scanning || captureBusy()) return;
  const invoice = invoiceInput.value.trim();
  const mode = selectedMode();
  const file = videoUpload.files[0];

  if (!invoice) {
    setMessage("Scan or type the invoice / shipping receipt first.", true);
    focusInvoice();
    return;
  }

  if (!file) {
    setMessage("Choose a video file to upload.", true);
    videoUpload.focus();
    return;
  }

  if (!uploadContext || uploadContext.file !== file || uploadContext.context.label !== invoice || uploadContext.context.mode !== mode || uploadContext.context.consumed) {
    uploadContext = { file, context: matchingScanContext(invoice, mode) || createScanContext(invoice, mode, "manual_upload") };
  }
  const context = uploadContext.context;
  uploadingVideo = true;
  updateCameraControls();
  uploadBtn.disabled = true;
  startBtn.disabled = true;
  window.clearTimeout(orderLookupDebounce);
  setMessage("Uploading video...");

  try {
    await uploadVideoBlob(file, file.name, null, context);
    setMessage("Uploaded and saved locally.");
    uploadContext = null;
    videoUpload.value = "";
    if (invoiceInput.value.trim() === invoice) invoiceInput.value = "";
    duplicateWarning.textContent = "";
    duplicateWarning.classList.remove("active");
    focusInvoice();
  } catch (error) {
    setMessage(`Upload error: ${error.message}`, true);
  } finally {
    uploadingVideo = false;
    updateCameraControls();
    uploadBtn.disabled = false;
    startBtn.disabled = false;
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
function handleInvoiceInput(scheduleLookup = true) {
  if (scanning) stopCameraScan("Scan cancelled because the tracking input changed.");
  window.clearTimeout(orderLookupDebounce);
  inputRevision++;
  debouncedSubmission = null;
  displayedScanContext = null;
  orderMatchBtn.disabled = true;
  lookupSequence++;
  startAfterOrderMatch = false;
  uppercaseInvoiceInput();
  queueDuplicateCheck();
  if (scheduleLookup) queueShippingOrderLookup();
}
invoiceInput.addEventListener("input", () => handleInvoiceInput());
invoiceInput.addEventListener("blur", () => checkDuplicateInvoice());
orderDialogClose.addEventListener("click", () => orderDialog.close());
orderMatchBtn.addEventListener("click", confirmOrderMatch);
orderDialog.addEventListener("click", (event) => {
  if (event.target === orderDialog) {
    orderDialog.close();
  }
});
orderDialog.addEventListener("close", () => {
  lookupSequence++;
  startAfterOrderMatch = false;
  if (!invoiceInput.disabled) {
    focusInvoice();
  }
});

invoiceInput.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.repeat) {
    return;
  }

  event.preventDefault();
  window.clearTimeout(orderLookupDebounce);
  const trackingNumber = invoiceInput.value.trim();

  if (trackingNumber) {
    beginShippingOrderLookup(trackingNumber);
  } else {
    setMessage("Scan or type the invoice / shipping receipt first.", true);
  }
});

scanCameraBtn.addEventListener("click", startCameraScan);
cancelScanBtn.addEventListener("click", () => stopCameraScan());
cameraFacing.addEventListener("change", () => {
  if (captureBusy()) return;
  const restart = scanning;
  stopCameraScan(restart ? "Switching camera…" : "Camera changed.");
  if (restart) startCameraScan();
});
window.addEventListener("pagehide", () => {
  stopCameraScan("Camera stopped.");
  if (recorder?.state === "recording") stopRecording();
});

initTheme();
initAudioPreference();
initAutoStartPreference();
setRecordingUi(false);
Promise.all([loadResults(), loadSummary()]);
