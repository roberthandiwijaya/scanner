const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

function harness() {
  const elements = new Map();
  function node(key) {
    if (!elements.has(key)) elements.set(key, { value: '', checked: false, disabled: false, textContent: '', hidden: false,
      classList: { toggle() {}, add() {}, remove() {} }, addEventListener() {}, focus() {}, appendChild() {}, showModal() {}, close() {}, files: [] });
    return elements.get(key);
  }
  const modes = [{ value: 'packing', checked: true }, { value: 'return', checked: false }];
  const requests = [];
  let nextId = 0;
  const sandbox = { console, crypto: webcrypto, URLSearchParams, Blob, FormData,
    document: { querySelector: node, querySelectorAll: () => modes, documentElement: { dataset: {} } },
    navigator: {},
    window: { clearTimeout() {}, setTimeout() { return 1; }, clearInterval() {}, setInterval() {}, confirm: () => true, addEventListener() {} },
    localStorage: { getItem() {}, setItem() {} },
    fetch: async (url, options = {}) => {
      requests.push({ url, options });
      if (url === '/api/scan-events') return { ok: true, json: async () => ({ id: ++nextId, mutation_token: `token-${nextId}` }) };
      if (url.endsWith('/lookup')) return { ok: true, json: async () => ({ tracking_number: 'LABEL123', items: [] }) };
      if (url === '/api/recordings') return { ok: true, json: async () => ({ invoice: options.body.get('invoice') }) };
      return { ok: true, json: async () => ({}) };
    }
  };
  vm.createContext(sandbox);
  const source = fs.readFileSync(require('node:path').join(__dirname, '../static/app.js'), 'utf8');
  vm.runInContext(source.slice(0, source.lastIndexOf('\ninitTheme();')), sandbox);
  vm.runInContext('renderOrderDetails = () => {}; openOrderDialog = () => {}; loadResults = async () => {}; loadSummary = async () => {}; checkDuplicateInvoice = async () => {};', sandbox);
  return { sandbox, requests, node, run: (code) => vm.runInContext(code, sandbox) };
}
const flush = () => new Promise(resolve => setImmediate(resolve));

function cameraHarness() {
  const h = harness();
  const calls = [];
  const tracks = [];
  h.node('#preview').play = async () => {};
  h.sandbox.navigator.mediaDevices = { getUserMedia: async constraints => {
    calls.push(constraints);
    const video = { readyState: 'live', stop() { this.readyState = 'ended'; }, addEventListener() {} };
    const audio = { readyState: 'live', stop() { this.readyState = 'ended'; } };
    tracks.push(video);
    return { getTracks: () => constraints.audio ? [video, audio] : [video],
      getVideoTracks: () => [video], getAudioTracks: () => constraints.audio ? [audio] : [] };
  } };
  return { ...h, calls, tracks };
}

test('camera scan accepts once, normalizes, and uses existing lookup identity', async () => {
  const h = harness();
  h.run('scanning = true; scanSession = 7; acceptCameraResult(" label123 ", 7); acceptCameraResult("LABEL123", 7);');
  await flush();
  assert.equal(h.node('#invoice').value, 'LABEL123');
  assert.equal(h.requests.filter(r => r.url === '/api/scan-events').length, 1);
  assert.equal(h.requests.filter(r => r.url.endsWith('/lookup')).length, 1);
  assert.equal(h.run('scanning'), false);
  assert.equal(h.run('activeScanContext.label'), 'LABEL123');
});

test('QR URLs, structured data, controls and overlong identifiers are rejected', () => {
  const h = harness();
  for (const value of ['https://example.com/LABEL123', '{"tracking":"LABEL123"}', 'ABC\u0000123', 'X'.repeat(129)]) {
    h.sandbox.value = value;
    assert.equal(h.run('trackingTextFromBarcode(value)'), null);
  }
  assert.equal(h.run('trackingTextFromBarcode(" SPXID123456 ")'), 'SPXID123456');
});

test('cancelled or superseded scans and scans during capture cannot submit', () => {
  const h = harness();
  h.run('scanning = true; scanSession = 1; stopCameraScan(); acceptCameraResult("LABEL123", 1);');
  h.run('scanning = true; scanSession = 3; acceptCameraResult("LABEL123", 2);');
  h.run('startingRecording = true; acceptCameraResult("LABEL123", 3);');
  assert.equal(h.requests.length, 0);
});

test('editing input cancels decoding and rejects its late result', () => {
  const h = harness();
  h.node('#invoice').value = 'MANUAL123';
  h.run('scanning = true; scanSession = 1; handleInvoiceInput(false); acceptCameraResult("LABEL123", 1);');
  assert.equal(h.node('#invoice').value, 'MANUAL123');
  assert.equal(h.run('scanning'), false);
  assert.equal(h.requests.length, 0);
});

test('scanning blocks recording, upload and saving', async () => {
  const h = harness();
  h.run('scanning = true;');
  await h.run('startRecording()');
  await h.run('uploadExistingVideo()');
  await h.run('saveRecording()');
  assert.equal(h.requests.length, 0);
});

test('camera scanner never requests microphone; recording reacquires only when needed', async () => {
  const h = cameraHarness();
  const first = await h.run('ensureCamera(false)');
  assert.equal(h.calls[0].audio, false);
  assert.equal(h.calls[0].video.facingMode.ideal, 'environment');
  assert.equal(await h.run('ensureCamera(false)'), first);
  h.node('#audioEnabled').checked = true;
  await h.run('ensureCamera()');
  assert.equal(h.calls.length, 2);
  assert.equal(h.tracks[0].readyState, 'ended');
  assert.equal(h.calls[1].audio.echoCancellation, true);
});

test('late camera permission after cancellation releases newly acquired tracks', async () => {
  const h = cameraHarness();
  let resolve;
  const video = { stop() { this.stopped = true; } };
  h.sandbox.navigator.mediaDevices.getUserMedia = () => new Promise(r => { resolve = r; });
  const pending = h.run('ensureCamera(false)');
  h.run('releaseCamera()');
  resolve({ getTracks: () => [video] });
  await assert.rejects(pending, /cancelled/);
  assert.equal(video.stopped, true);
  assert.equal(h.run('stream'), null);
});

test('camera switch releases the old stream before acquiring a new one', async () => {
  const h = cameraHarness();
  await h.run('ensureCamera(false)');
  h.node('#cameraFacing').value = 'user';
  await h.run('ensureCamera(false)');
  assert.equal(h.tracks[0].readyState, 'ended');
  assert.equal(h.calls[1].video.facingMode.ideal, 'user');
});

test('MIME negotiation supports Safari MP4 and missing MediaRecorder', () => {
  const h = harness();
  assert.equal(h.run('getMimeType()'), '');
  h.sandbox.MediaRecorder = { isTypeSupported: type => type === 'video/mp4' };
  assert.equal(h.run('getMimeType()'), 'video/mp4');
  h.sandbox.MediaRecorder = { isTypeSupported: type => type === 'video/webm' };
  assert.equal(h.run('getMimeType()'), 'video/webm');
});

test('recording validation preserves WebM checks and validates MP4 headers', async () => {
  const h = harness();
  h.sandbox.blob = new Blob([new Uint8Array([0x1a, 0x45, 0xdf, 0xa3])], { type: 'video/webm' });
  assert.equal(await h.run('recordingExtension(blob)'), 'webm');
  h.sandbox.blob = new Blob(['invalid'], { type: 'video/webm' });
  await assert.rejects(h.run('recordingExtension(blob)'), /incomplete/);
  h.sandbox.blob = new Blob([new Uint8Array([0, 0, 0, 24]), 'ftypisom'], { type: 'video/mp4;codecs=avc1' });
  assert.equal(await h.run('recordingExtension(blob)'), 'mp4');
  h.sandbox.blob = new Blob(['invalid-mp4'], { type: 'video/mp4' });
  await assert.rejects(h.run('recordingExtension(blob)'), /incomplete/);
});

test('MP4 saving uses actual MIME, extension and original scan context', async () => {
  const h = harness();
  h.sandbox.blob = new Blob([new Uint8Array([0, 0, 0, 24]), 'ftypisom'], { type: 'video/mp4' });
  h.run('recordedBlob = blob; recordingContext = createScanContext("PHONE123", "packing"); recordedDurationMs = 1234;');
  await h.run('saveRecording()');
  const form = h.requests.find(r => r.url === '/api/recordings').options.body;
  assert.equal(form.get('video').name, 'PHONE123.mp4');
  assert.equal(form.get('video').type, 'video/mp4');
  assert.equal(form.get('duration_ms'), '1234');
  assert.equal(form.get('invoice'), 'PHONE123');
  assert.equal(h.run('savingRecording'), false);
});

test('camera permission denial leaves scanner idle and reports HTTPS guidance', async () => {
  const h = cameraHarness();
  h.sandbox.window.ZXingBrowser = { BrowserMultiFormatReader: function () {} };
  h.sandbox.navigator.mediaDevices.getUserMedia = async () => { throw new Error('Permission denied'); };
  await h.run('startCameraScan()');
  assert.equal(h.run('scanning'), false);
  assert.match(h.node('#scanStatus').textContent, /trusted HTTPS/);
  assert.match(h.node('#message').textContent, /Permission denied/);
});


test('decoder uses the existing preview, retries recoverable minified errors and stops on result', async () => {
  const h = cameraHarness();
  const pending = [];
  h.sandbox.window.setTimeout = fn => { pending.push(fn); return pending.length; };
  h.node('#preview').readyState = 2;
  let frames = 0;
  let formats;
  h.sandbox.window.ZXingBrowser = {
    BarcodeFormat: { CODE_39: 2, CODE_128: 4, ITF: 8, QR_CODE: 11 },
    BrowserMultiFormatReader: class {
      set possibleFormats(value) { formats = [...value]; }
      decode(video) {
        assert.equal(video, h.node('#preview'));
        if (++frames === 1) throw { name: 'e', getKind: () => 'NotFoundException' };
        return { getText: () => 'PHONE123' };
      }
    }
  };
  await h.run('startCameraScan()');
  assert.equal(h.run('scanning'), true);
  assert.deepEqual(formats, [2, 4, 8, 11]);
  pending.shift()();
  await flush();
  assert.equal(h.run('scanning'), false);
  assert.equal(h.calls.length, 1);
  assert.equal(h.requests.filter(r => r.url === '/api/scan-events').length, 1);
  for (const callback of pending) callback();
  assert.equal(frames, 2);
});

test('save failure retains MP4 draft and its event identity for retry', async () => {
  const h = harness();
  h.sandbox.blob = new Blob([new Uint8Array([0, 0, 0, 24]), 'ftypisom'], { type: 'video/mp4' });
  h.run('recordedBlob = blob; recordingContext = createScanContext("PHONE123", "packing");');
  const original = h.sandbox.fetch;
  let attempts = 0;
  h.sandbox.fetch = async (url, options) => {
    if (url === '/api/recordings' && ++attempts === 1) throw new Error('offline');
    return original(url, options);
  };
  await h.run('saveRecording()');
  assert.equal(h.run('recordedBlob'), h.sandbox.blob);
  assert.equal(h.run('savingRecording'), false);
  assert.equal(h.node('#saveBtn').disabled, false);
  await h.run('saveRecording()');
  assert.equal(h.requests.filter(r => r.url === '/api/scan-events').length, 1);
  assert.equal(h.run('recordedBlob'), null);
});

test('debounce plus Enter logs once; another Enter logs a genuine repeat', async () => {
  const h = harness();
  h.run('beginShippingOrderLookup("LABEL123", "debounce"); beginShippingOrderLookup("LABEL123", "enter");');
  await flush();
  assert.equal(h.requests.filter(r => r.url === '/api/scan-events').length, 1);
  h.run('beginShippingOrderLookup("LABEL123", "enter");');
  await flush();
  assert.equal(h.requests.filter(r => r.url === '/api/scan-events').length, 2);
});

test('rapid deliberate repeated submissions get distinct events even before creation returns', async () => {
  const h = harness();
  h.run('beginShippingOrderLookup("LABEL123"); beginShippingOrderLookup("LABEL123");');
  await flush();
  assert.equal(h.requests.filter(r => r.url === '/api/scan-events').length, 2);
});

test('persist retries reuse submission identity', async () => {
  const h = harness();
  let attempts = 0;
  const ids = [];
  h.sandbox.fetch = async (_url, options) => {
    ids.push(JSON.parse(options.body).submission_id);
    if (++attempts === 1) throw new Error('offline');
    return { ok: true, json: async () => ({ id: 12, mutation_token: 'token' }) };
  };
  h.run('activeScanContext = createScanContext("LABEL123", "packing");');
  await assert.rejects(h.run('persistScan(activeScanContext)'), /offline/);
  await h.run('persistScan(activeScanContext)');
  assert.equal(ids[0], ids[1]);
});

test('closing or superseding UI does not cancel persisted lookup work', async () => {
  const h = harness();
  h.run('beginShippingOrderLookup("LABEL123"); lookupSequence++;');
  await flush();
  assert.equal(h.requests.filter(r => r.url.endsWith('/lookup')).length, 1);
  assert.equal(h.requests[0].options.signal, undefined);
});

test('upload sends captured label, mode, and event rather than mutable input', async () => {
  const h = harness();
  h.node('#invoice').value = 'NEW-LABEL';
  h.sandbox.blob = new Blob(['test'], { type: 'video/webm' });
  await h.run('uploadVideoBlob(blob, "test.webm", null, createScanContext("OLD-LABEL", "return"))');
  const form = h.requests.find(r => r.url === '/api/recordings').options.body;
  assert.equal(form.get('invoice'), 'OLD-LABEL');
  assert.equal(form.get('mode'), 'return');
  assert.equal(form.get('scan_event_id'), '1');
  assert.equal(form.get('scan_event_token'), 'token-1');
});

test('history save failure prevents lookup and displays an explicit error', async () => {
  const h = harness();
  h.sandbox.fetch = async () => { throw new Error('offline'); };
  await h.run('lookupShippingOrder(createScanContext("LABEL123", "packing"))');
  assert.match(h.node('#orderLookupStatus').textContent, /Scan history could not be saved/);
});
