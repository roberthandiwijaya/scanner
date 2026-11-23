const settingsForm = document.getElementById('settingsForm');
const labelInput = document.getElementById('destinationLabel');
const folderInput = document.getElementById('destinationFolder');
const saveButton = document.getElementById('saveDestination');
const testButton = document.getElementById('testDestination');
const result = document.getElementById('settingsResult');
let settingsEditable = false;
let settingsBusy = false;
function controls() { saveButton.disabled = testButton.disabled = !settingsEditable || settingsBusy; }
async function requestSettings(path, data) {
  const response = await fetch('/api/archive/settings' + path, data ? { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data) } : {});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || 'Could not update archive settings.');
  return payload;
}
async function loadSettings() {
  try {
    const settings = await requestSettings('');
    settingsEditable = settings.editable;
    labelInput.value = settings.label || 'Backup hard drive';
    folderInput.value = settings.folder || 'ScannerArchive';
    document.getElementById('settingsState').textContent = settingsEditable ? (settings.available ? `Current destination: ${settings.label}. Ready.` : 'Backup drive is mounted. Save a folder below to configure backups.') : 'The backup drive is not mounted for website settings. Complete the one-time Docker setup below first.';
  } catch(error) { document.getElementById('settingsState').textContent = error.message; }
  controls();
}
async function changeSettings(save) {
  if (settingsBusy || !settingsEditable || !settingsForm.reportValidity()) return;
  if (save && !window.confirm('Save this backup folder? This may create a folder and backup identity on the mounted drive. No recordings will be moved or deleted.')) return;
  settingsBusy = true; controls(); result.classList.remove('error'); result.textContent = 'Testing backup drive…';
  try {
    const payload = await requestSettings(save ? '' : '/test', {folder:folderInput.value.trim(),label:labelInput.value.trim()});
    result.textContent = `${save ? 'Saved and verified' : 'Access verified'}: ${payload.label} / ${payload.folder}. ${(payload.free_bytes / 1024 ** 3).toFixed(1)} GB free.`;
    if(save) await loadSettings();
  } catch(error) { result.textContent = error.message; result.classList.add('error'); }
  finally { settingsBusy = false; controls(); }
}
settingsForm.addEventListener('submit',event=>{event.preventDefault();changeSettings(true);});
testButton.addEventListener('click',()=>changeSettings(false));
function setTheme(theme) { document.documentElement.dataset.theme=theme;localStorage.setItem('scanner-theme',theme);document.getElementById('themeToggle').textContent=theme==='dark'?'Light mode':'Dark mode'; }
document.getElementById('themeToggle').addEventListener('click',()=>setTheme(document.documentElement.dataset.theme==='dark'?'light':'dark'));
setTheme(localStorage.getItem('scanner-theme')==='dark'?'dark':'light');
loadSettings();
