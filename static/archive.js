const byId = (id) => document.getElementById(id);
const selectedRecordings = new Set();
let archiveConfig = { enabled: false, available: false };
let previewItems = [];
let previewPage = 1;
let previewTotalPages = 1;
let jobsPage = 1;
let jobsTotalPages = 1;
let previewSequence = 0;
let jobsSequence = 0;
let pollTimer;
let removalConfirmation = null;
let archiveActionBusy = false;
let automaticPreview = null;
let automaticRuns = [];
let automaticPollTimer;
let automaticLoading = false;

function node(tag, text, className) {
  const result = document.createElement(tag);
  if (text !== undefined) result.textContent = text;
  if (className) result.className = className;
  return result;
}
function bytes(value) {
  value = Number(value || 0);
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  return value < 1024 ** 3 ? `${(value / 1024 ** 2).toFixed(1)} MB` : `${(value / 1024 ** 3).toFixed(2)} GB`;
}
async function archiveRequest(path, payload) {
  const options = payload === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) };
  const response = await fetch(`/api/archive${path}`, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Archive request failed.');
  return result;
}
function notice(id, text, error = false) {
  byId(id).textContent = text;
  byId(id).classList.toggle('error', error);
}
function updateSelection() {
  byId('archiveSelected').textContent = `${selectedRecordings.size} recording${selectedRecordings.size === 1 ? '' : 's'} selected (maximum 50).`;
  byId('startArchiveBackup').disabled = !archiveConfig.available || !selectedRecordings.size || archiveActionBusy;
  updateAutomaticButton();
}
async function loadArchiveConfig() {
  try {
    archiveConfig = await archiveRequest('/config');
    notice('archiveDestination', archiveConfig.available
      ? `Destination: ${archiveConfig.label} · identity checked. Backups are separate from local cleanup.`
      : `Archiving unavailable: ${archiveConfig.error || 'Configure a NAS mount and destination identity before use.'}`, !archiveConfig.available);
  } catch (error) { archiveConfig = { available: false }; notice('archiveDestination', error.message, true); }
  updateSelection();
}
function appendCell(row, text) { const cell = node('td', text); row.appendChild(cell); return cell; }
function renderPreview(item) {
  const row = node('tr');
  const input = node('input');
  input.type = 'checkbox'; input.checked = selectedRecordings.has(item.id); input.disabled = Boolean(item.error);
  input.setAttribute('aria-label', `Select ${item.invoice}`);
  input.addEventListener('change', () => {
    if (input.checked && selectedRecordings.size >= 50) { input.checked = false; notice('archiveStatus', 'Select at most 50 recordings per job.', true); return; }
    if (input.checked) selectedRecordings.add(item.id); else selectedRecordings.delete(item.id);
    updateSelection();
  });
  appendCell(row).appendChild(input);
  appendCell(row, item.invoice);
  appendCell(row, new Date(item.created_at).toLocaleString());
  appendCell(row, item.mode === 'return' ? 'Return unboxing' : 'Packing');
  appendCell(row, bytes(item.original_bytes));
  appendCell(row, `${item.files.filter(file => file.kind === 'converted').length} · ${bytes(item.converted_bytes)}`);
  appendCell(row, item.error || 'Eligible').className = item.error ? 'error' : '';
  return row;
}
function archiveCutoff() {
  const value = byId('archiveCutoff').value;
  return value ? `${value}T23:59:59.999999+00:00` : '';
}
async function loadArchivePreview() {
  const cutoff = archiveCutoff();
  if (!cutoff) { notice('archiveStatus', 'Choose a save-date cutoff first.', true); return; }
  const sequence = ++previewSequence;
  notice('archiveStatus', 'Inspecting local recording files…');
  try {
    const result = await archiveRequest(`/preview?${new URLSearchParams({ cutoff, page: previewPage, per_page: 25 })}`);
    if (sequence !== previewSequence) return;
    previewItems = result.items;
    previewTotalPages = Math.max(1, Math.ceil(result.total / result.per_page));
    if (previewPage > previewTotalPages) { previewPage = previewTotalPages; return loadArchivePreview(); }
    byId('archivePreviewRows').replaceChildren(...previewItems.map(renderPreview));
    notice('archiveStatus', `${result.total} recordings match the cutoff. Preview is advisory; files are checked again when the job starts.`);
    byId('archivePagination').hidden = !result.total;
    byId('archivePageInfo').textContent = `Page ${previewPage} of ${previewTotalPages}`;
    byId('archivePrevious').disabled = previewPage <= 1;
    byId('archiveNext').disabled = previewPage >= previewTotalPages;
  } catch (error) { if (sequence === previewSequence) { byId('archivePreviewRows').replaceChildren(); notice('archiveStatus', error.message, true); } }
}
function actionButton(text, handler) {
  const button = node('button', text); button.type = 'button';
  button.addEventListener('click', async () => {
    button.disabled = true;
    try { await handler(); }
    catch (error) { notice('archiveJobsStatus', error.message, true); }
    finally { button.disabled = false; }
  });
  return button;
}
const stateLabels = { cancelled: 'Backup cancelled — local files retained', preparing: 'Preparing inventory', queued: 'Queued', backing_up: 'Backing up and verifying', verified: 'Backup verified — local files retained', removal_pending: 'Local removal confirmed', removing: 'Removing verified local files', interrupted: 'Interrupted — review before resuming', completed: 'Archived', error: 'Needs attention' };
function renderJob(job) {
  const card = node('article', undefined, 'archive-job');
  const stateLabel = job.state === 'removal_pending' && !job.confirmed_at ? 'Awaiting removal confirmation — local files retained' : stateLabels[job.state] || job.state;
  card.appendChild(node('h3', `${stateLabel} · ${job.id.slice(0, 8)}`));
  card.appendChild(node('p', `${job.label} · ${job.file_count} files · ${bytes(job.total_bytes)} total · ${bytes(job.verified_bytes)} verified · ${bytes(job.removed_bytes)} removed`, 'hint'));
  card.appendChild(node('small', `Created ${new Date(job.created_at).toLocaleString()}`, 'history-secondary'));
  if (job.error) card.appendChild(node('p', job.error, 'error'));
  const details = node('details'); details.appendChild(node('summary', `${job.items.length} recordings · view file details`));
  const list = node('ul', undefined, 'archive-file-list');
  for (const item of job.items) {
    const entry = node('li');
    entry.appendChild(node('strong', `${item.snapshot.invoice || item.recording_id} · ${item.state}`));
    for (const file of item.files) {
      entry.appendChild(node('span', `${file.source} → ${file.target} · ${bytes(file.size_bytes)} · ${file.removed_at ? 'local copy removed' : file.verified_at ? 'verified' : 'not yet verified'}`, 'history-secondary'));
      if (file.error) entry.appendChild(node('span', file.error, 'error'));
    }
    list.appendChild(entry);
  }
  details.appendChild(list); card.appendChild(details);
  const actions = node('div', undefined, 'archive-controls');
  if (!job.automatic_run_id && ['verified', 'removal_pending'].includes(job.state) && !job.confirmed_at) actions.appendChild(actionButton('Review local removal…', () => prepareRemoval(job.id)));
  if (!job.automatic_run_id && !job.confirmed_at && ['verified', 'removal_pending', 'error', 'interrupted'].includes(job.state)) actions.appendChild(actionButton('Cancel backup job', async () => {
    if (!window.confirm('Cancel this unconfirmed job and release its recordings? Local files and any NAS copies will be retained.')) return;
    await archiveRequest(`/jobs/${job.id}/cancel`, {}); await loadArchiveJobs();
  }));
  if (!job.automatic_run_id && ['interrupted', 'error'].includes(job.state)) actions.appendChild(actionButton(job.confirmed_at ? 'Review and resume local removal…' : 'Retry backup', async () => {
    if (job.confirmed_at && !window.confirm('Resume previously confirmed cleanup? The app will reverify remaining local files and all required backups before continuing.')) return;
    await archiveRequest(`/jobs/${job.id}/resume`, {}); await loadArchiveJobs();
  }));
  card.appendChild(actions);
  return card;
}
async function loadArchiveJobs() {
  clearTimeout(pollTimer);
  const sequence = ++jobsSequence;
  try {
    const result = await archiveRequest(`/jobs?${new URLSearchParams({ page: jobsPage, per_page: 10 })}`);
    if (sequence !== jobsSequence) return;
    jobsTotalPages = Math.max(1, Math.ceil(result.total / result.per_page));
    if (jobsPage > jobsTotalPages) { jobsPage = jobsTotalPages; return loadArchiveJobs(); }
    byId('archiveJobs').replaceChildren(...result.items.map(renderJob));
    notice('archiveJobsStatus', result.total ? `${result.total} backup job${result.total === 1 ? '' : 's'}.` : 'No backup jobs yet.');
    byId('archiveJobPagination').hidden = !result.total;
    byId('archiveJobsPageInfo').textContent = `Page ${jobsPage} of ${jobsTotalPages}`;
    byId('archiveJobsPrevious').disabled = jobsPage <= 1;
    byId('archiveJobsNext').disabled = jobsPage >= jobsTotalPages;
    // All work continues server-side; polling never authorizes deletion.
    if (result.items.some(job => ['queued', 'backing_up', 'removal_pending', 'removing'].includes(job.state))) pollTimer = setTimeout(loadArchiveJobs, 3000);
  } catch (error) { if (sequence === jobsSequence) notice('archiveJobsStatus', `${error.message} Use Refresh jobs to retry.`, true); }
}
async function startBackup() {
  if (archiveActionBusy || !selectedRecordings.size || !archiveConfig.available) return;
  archiveActionBusy = true; updateSelection();
  try {
    await archiveRequest('/jobs', { recording_ids: [...selectedRecordings], cutoff: archiveCutoff() });
    selectedRecordings.clear(); jobsPage = 1;
    await Promise.all([loadArchiveJobs(), loadArchivePreview()]);
    notice('archiveStatus', 'Backup queued. Local files will be retained until you separately confirm removal.');
  } catch (error) { notice('archiveStatus', error.message, true); }
  finally { archiveActionBusy = false; updateSelection(); }
}
async function prepareRemoval(id) {
  removalConfirmation = await archiveRequest(`/jobs/${id}/removal-preview`, {});
  byId('archiveRemovalSummary').textContent = `${removalConfirmation.recording_count} recordings · ${removalConfirmation.file_count} files · ${bytes(removalConfirmation.total_bytes)} · ${removalConfirmation.label}. Confirmation expires in 15 minutes.`;
  byId('archiveRemovalError').textContent = '';
  byId('archiveRemovalDialog').showModal();
}
async function confirmRemoval() {
  if (!removalConfirmation || archiveActionBusy) return;
  archiveActionBusy = true; byId('confirmArchiveRemoval').disabled = true;
  try {
    const confirmation = removalConfirmation;
    await archiveRequest(`/jobs/${confirmation.job_id}/confirm-removal`, { token: confirmation.token, digest: confirmation.digest });
    byId('archiveRemovalDialog').close(); removalConfirmation = null;
    await loadArchiveJobs();
  } catch (error) { byId('archiveRemovalError').textContent = error.message; }
  finally { archiveActionBusy = false; byId('confirmArchiveRemoval').disabled = false; updateSelection(); }
}
byId('archivePreviewForm').addEventListener('submit', (event) => { event.preventDefault(); previewPage = 1; selectedRecordings.clear(); updateSelection(); loadArchivePreview(); });
byId('archiveCutoff').addEventListener('change', () => { previewSequence++; selectedRecordings.clear(); previewItems = []; byId('archivePreviewRows').replaceChildren(); byId('archivePagination').hidden = true; updateSelection(); });
byId('selectArchivePage').addEventListener('click', () => { for (const item of previewItems) if (!item.error && selectedRecordings.size < 50) selectedRecordings.add(item.id); byId('archivePreviewRows').replaceChildren(...previewItems.map(renderPreview)); updateSelection(); });
byId('startArchiveBackup').addEventListener('click', startBackup);
byId('archivePrevious').addEventListener('click', () => { if (previewPage > 1) { previewPage--; loadArchivePreview(); } });
byId('archiveNext').addEventListener('click', () => { if (previewPage < previewTotalPages) { previewPage++; loadArchivePreview(); } });
byId('archiveJobsPrevious').addEventListener('click', () => { if (jobsPage > 1) { jobsPage--; loadArchiveJobs(); } });
byId('archiveJobsNext').addEventListener('click', () => { if (jobsPage < jobsTotalPages) { jobsPage++; loadArchiveJobs(); } });
byId('refreshArchiveJobs').addEventListener('click', () => { loadArchiveConfig(); loadArchiveJobs(); });
byId('confirmArchiveRemoval').addEventListener('click', confirmRemoval);
byId('cancelArchiveRemoval').addEventListener('click', () => byId('archiveRemovalDialog').close());
byId('archiveRemovalDialog').addEventListener('close', () => { removalConfirmation = null; });
function updateAutomaticButton() {
  const unfinished = automaticRuns.some(run => !['completed', 'stopped'].includes(run.state));
  byId('startAutomaticArchive').disabled = !archiveConfig.available || !automaticPreview?.eligible_count || unfinished || archiveActionBusy;
}
async function loadAutomaticArchive() {
  if (automaticLoading) return;
  automaticLoading = true;
  clearTimeout(automaticPollTimer);
  try {
    const [preview, listing] = await Promise.all([archiveRequest('/automatic/preview'), archiveRequest('/automatic')]);
    automaticPreview = preview;
    automaticRuns = Array.isArray(listing) ? listing : listing.items;
    notice('automaticArchivePreview', `${preview.eligible_count} recording entries saved strictly before ${new Date(preview.cutoff).toLocaleString()} meet the age rule; missing/busy files are skipped during processing. Original size: ${bytes(preview.original_bytes)}; converted copies are additional. Destination: ${archiveConfig.label || 'configure a backup drive'}.`);
    byId('automaticArchiveRuns').replaceChildren(...automaticRuns.map(renderAutomaticRun));
    updateAutomaticButton();
    if (automaticRuns.some(run => ['queued', 'running', 'waiting_io'].includes(run.state))) automaticPollTimer = setTimeout(loadAutomaticArchive, 4000);
  } catch (error) { notice('automaticArchiveStatus', error.message, true); }
  finally { automaticLoading = false; }
}
function renderAutomaticRun(run) {
  const card = node('article', undefined, 'archive-job');
  card.appendChild(node('h3', `Automatic cleanup · ${run.state}`));
  card.appendChild(node('p', `Cutoff: ${new Date(run.cutoff).toLocaleString()} · ${run.rate_mbps} MiB/s I/O limit`, 'hint'));
  card.appendChild(node('p', `${run.processed_count || 0} / ${run.total_count || 0} processed · ${run.completed_count || 0} completed · ${run.skipped_count || 0} skipped · ${run.error_count || 0} failed · ${bytes(run.removed_bytes)} removed · ${bytes(run.bytes_processed)} copy/checksum I/O`));
  card.appendChild(node('p', `Stage: ${run.stage || run.state}${run.current_job_id ? ' · ' + run.current_job_id.slice(0, 8) : ''}`));
  if (run.error) card.appendChild(node('p', run.error, 'error'));
  const actions = node('div', undefined, 'archive-controls');
  if (['queued', 'running', 'waiting_io'].includes(run.state)) actions.appendChild(actionButton('Pause', () => automaticControl(run.id, 'pause')));
  if (['paused', 'interrupted'].includes(run.state)) actions.appendChild(actionButton('Resume authorized cleanup', () => automaticControl(run.id, 'resume')));
  if (!['completed', 'stopped'].includes(run.state)) actions.appendChild(actionButton('Stop', () => automaticControl(run.id, 'stop')));
  card.appendChild(actions);
  return card;
}
async function automaticControl(id, action) {
  await archiveRequest(`/automatic/${id}/control`, {action});
  await loadAutomaticArchive();
  await loadArchiveJobs();
}
async function startAutomaticArchive() {
  if (archiveActionBusy || byId('startAutomaticArchive').disabled) return;
  archiveActionBusy = true; updateAutomaticButton();
  try {
    await archiveRequest('/automatic', {authorize_removal:true, rate_mbps:Number(byId('automaticArchiveRate').value)});
    notice('automaticArchiveStatus', 'Automatic run authorized. Old recordings will be removed locally only after successful backup and verification.');
    await loadAutomaticArchive();
  } catch(error) { notice('automaticArchiveStatus', error.message, true); }
  finally { archiveActionBusy = false; updateAutomaticButton(); }
}
byId('startAutomaticArchive').addEventListener('click', startAutomaticArchive);
byId('refreshAutomaticArchive').addEventListener('click', () => { loadArchiveConfig(); loadAutomaticArchive(); });

function archiveTheme(theme) {
  document.documentElement.dataset.theme = theme; localStorage.setItem('scanner-theme', theme);
  byId('themeToggle').textContent = theme === 'dark' ? 'Light mode' : 'Dark mode';
}
byId('themeToggle').addEventListener('click', () => archiveTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));
archiveTheme(localStorage.getItem('scanner-theme') === 'dark' ? 'dark' : 'light');
loadArchiveConfig().then(loadAutomaticArchive);
loadArchiveJobs();
