const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
function harness() {
  const nodes = new Map();
  function node() { return { children: [], value: '', textContent: '', disabled: false, classList: {toggle() {}}, appendChild(n) { this.children.push(n); }, replaceChildren(...ns) { this.children = ns; }, addEventListener() {}, setAttribute() {}, showModal() {this.open = true;}, close() {this.open = false;} }; }
  const get = id => { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); };
  const calls = [];
  const sandbox = { URLSearchParams, console, clearTimeout() {}, setTimeout() {}, window: {confirm: () => true}, document: {getElementById: get, createElement: node, documentElement: {dataset: {}}}, localStorage: {getItem() {}, setItem() {}}, fetch: async (url, opts = {}) => {
    calls.push({url, opts});
    let payload = {};
    if (url === '/api/archive/automatic') payload = {items:[],total:0,page:1,per_page:20};
    else if (url === '/api/archive/automatic/preview') payload = {cutoff:'2025-01-01T00:00:00Z',eligible_count:1,original_bytes:20};
    else if (url.endsWith('removal-preview')) payload = {job_id:'job-1',token:'secret',digest:'digest',recording_count:1,file_count:2,total_bytes:20,label:'NAS'};
    else if (url.includes('/jobs?')) payload = {items:[],total:0,per_page:10};
    else if (url.includes('/preview?')) payload = {items:[],total:0,per_page:25};
    return {ok:true,json:async()=>payload};
  }};
  vm.createContext(sandbox);
  let source = fs.readFileSync(path.join(__dirname, '../static/archive.js'), 'utf8');
  source = source.slice(0, source.lastIndexOf('\narchiveTheme(localStorage'));
  vm.runInContext(source, sandbox);
  return {calls,get,run:code=>vm.runInContext(code,sandbox)};
}
test('backup submits selection but never authorizes local removal', async () => {
  const h = harness(); h.get('archiveCutoff').value = '2025-01-01';
  await h.run("archiveConfig={available:true}; selectedRecordings.add(12); startBackup()");
  const post = h.calls.find(c=>c.opts.method==='POST');
  assert.equal(post.url, '/api/archive/jobs');
  assert.deepEqual(JSON.parse(post.opts.body).recording_ids,[12]);
  assert.match(JSON.parse(post.opts.body).cutoff,/T23:59:59/);
  assert.equal(h.calls.some(c=>c.url.includes('confirm-removal')),false);
});
test('removal preview opens review without calling destructive endpoint', async () => {
  const h = harness(); await h.run("prepareRemoval('job-1')");
  assert.equal(h.get('archiveRemovalDialog').open,true);
  assert.equal(h.calls.length,1);
  assert.match(h.calls[0].url,/removal-preview$/);
});
test('explicit removal uses exact reviewed token and digest', async () => {
  const h = harness(); await h.run("prepareRemoval('job-1')");
  await h.run('confirmRemoval()');
  const call = h.calls.find(c=>c.url.endsWith('confirm-removal'));
  assert.deepEqual(JSON.parse(call.opts.body),{token:'secret',digest:'digest'});
});
test('automatic cleanup submits explicit removal authority without a client cutoff', async () => {
  const h = harness();
  h.get('automaticArchiveRate').value = '5';
  h.get('startAutomaticArchive').disabled = false;
  await h.run("startAutomaticArchive()");
  const request = h.calls.find(c => c.url === '/api/archive/automatic' && c.opts.method === 'POST');
  assert.deepEqual(JSON.parse(request.opts.body), {authorize_removal:true,rate_mbps:5});
  assert.equal(h.calls.some(c => c.url.includes('confirm-removal')), false);
});
test('automatic child jobs do not offer manual confirmation controls', () => {
  const h = harness();
  const card = h.run("renderJob({id:'job-1',automatic_run_id:'auto-1',state:'verified',label:'Drive',file_count:1,total_bytes:20,verified_bytes:20,removed_bytes:0,created_at:'2025-01-01',items:[]})");
  const actions = card.children[card.children.length - 1];
  assert.equal(actions.children.length, 0);
});
test('verified job rendering does not send mutation requests', () => {
  const h = harness();
  h.run("renderJob({id:'job-1',state:'verified',label:'NAS',file_count:2,total_bytes:20,verified_bytes:20,removed_bytes:0,created_at:'2025-01-01',items:[]})");
  assert.equal(h.calls.length,0);
});
