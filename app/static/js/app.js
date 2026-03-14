/**
 * S3 Backup Flow — Dashboard JS
 * JHBridge Translation Services
 *
 * Features:
 *  - WebSocket live progress (SVG circle)
 *  - Manual backup trigger with confirmation dialog
 *  - Backup history table
 *  - Schedule settings (load + save)
 *  - System health indicators
 */

// ── Auth token (from Cognito login) ──────────────────────────────────────
const TOKEN = localStorage.getItem('id_token') || '';
const HEADERS = TOKEN
  ? { 'Content-Type': 'application/json', Authorization: `Bearer ${TOKEN}` }
  : { 'Content-Type': 'application/json' };

// ── WebSocket handle ──────────────────────────────────────────────────────
let _ws = null;
let _activeTaskId = null;

// ══════════════════════════════════════════════════════════════════════════
// Boot
// ══════════════════════════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', () => {
  loadHealth();
  loadHistory();
  loadSchedule();

  // Check if there is an active task stored in session
  const savedTask = sessionStorage.getItem('active_task_id');
  if (savedTask) connectWebSocket(savedTask);
});

// ══════════════════════════════════════════════════════════════════════════
// Health check
// ══════════════════════════════════════════════════════════════════════════
async function loadHealth() {
  try {
    const data = await apiFetch('/api/v1/health');

    setHealth('health-redis',  data.redis);
    setHealth('health-dynamo', data.dynamodb);
    setHealth('health-s3',     data.s3);

    if (data.last_backup) {
      document.getElementById('stat-last').textContent =
        formatDate(data.last_backup);
    }

    document.getElementById('stat-storage').textContent =
      fmtBytes(data.total_storage_bytes);

    document.getElementById('stat-count').textContent = data.total_files;
    document.getElementById('nav-status').classList.remove('hidden');
  } catch (_) { /* silent */ }
}

function setHealth(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  if (value === 'ok') {
    el.textContent = 'OK';
    el.className = 'font-medium text-green-600';
  } else {
    el.textContent = 'Erreur';
    el.className = 'font-medium text-red-500';
  }
}

// ══════════════════════════════════════════════════════════════════════════
// Backup trigger — with confirmation dialog
// ══════════════════════════════════════════════════════════════════════════
function triggerBackup() {
  document.getElementById('btn-trigger').classList.add('hidden');
  document.getElementById('confirm-dialog').classList.remove('hidden');
}

function cancelBackup() {
  document.getElementById('btn-trigger').classList.remove('hidden');
  document.getElementById('confirm-dialog').classList.add('hidden');
}

async function confirmBackup() {
  cancelBackup();
  try {
    const data = await apiFetch('/api/v1/backups/run', {
      method: 'POST',
      body: JSON.stringify({}),
    });
    showToast(`Backup lancé — ID: ${data.task_id.slice(0, 8)}…`, 'success');
    sessionStorage.setItem('active_task_id', data.task_id);
    connectWebSocket(data.task_id);
  } catch (err) {
    showToast(`Erreur: ${err.message}`, 'error');
  }
}

// ══════════════════════════════════════════════════════════════════════════
// WebSocket — live progress
// ══════════════════════════════════════════════════════════════════════════
function connectWebSocket(taskId) {
  if (_ws) _ws.close();
  _activeTaskId = taskId;

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${location.host}/api/v1/ws/backup/${taskId}`;

  _ws = new WebSocket(url);

  _ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    updateProgress(msg.progress, msg.phase, msg.done, msg.error);

    if (msg.done || msg.error) {
      sessionStorage.removeItem('active_task_id');
      loadHistory();
      loadHealth();

      setTimeout(() => resetProgress(), 3000);
    }
  };

  _ws.onerror = () => {
    showToast('Connexion WebSocket perdue.', 'error');
    resetProgress();
  };
}

function updateProgress(pct, phase, done, error) {
  pct = Math.max(0, Math.min(100, pct));
  const circ = document.getElementById('progress-circle');
  const circumference = 314;
  circ.style.strokeDashoffset = circumference - (circumference * pct) / 100;
  circ.style.stroke = error ? '#FC8181' : '#68D391';

  document.getElementById('progress-pct').textContent = `${pct}%`;
  document.getElementById('progress-phase').textContent = phase || 'En cours...';

  const badge = document.getElementById('progress-badge');
  if (done) {
    badge.textContent = 'COMPLETED';
    badge.className = 'text-xs font-semibold px-3 py-1 rounded-full badge-COMPLETED';
    showToast('Backup terminé avec succès !', 'success');
  } else if (error) {
    badge.textContent = 'FAILED';
    badge.className = 'text-xs font-semibold px-3 py-1 rounded-full badge-FAILED';
    showToast('Backup échoué. Vérifiez les logs.', 'error');
  } else {
    badge.textContent = 'RUNNING';
    badge.className = 'text-xs font-semibold px-3 py-1 rounded-full badge-RUNNING';
  }
}

function resetProgress() {
  document.getElementById('progress-circle').style.strokeDashoffset = '314';
  document.getElementById('progress-pct').textContent = '0%';
  document.getElementById('progress-phase').textContent = 'En attente...';
  document.getElementById('progress-badge').textContent = 'IDLE';
  document.getElementById('progress-badge').className =
    'text-xs font-semibold px-3 py-1 rounded-full bg-gray-100 text-gray-500';
  document.getElementById('btn-trigger').classList.remove('hidden');
}

// ══════════════════════════════════════════════════════════════════════════
// History table
// ══════════════════════════════════════════════════════════════════════════
async function loadHistory() {
  try {
    const data = await apiFetch('/api/v1/backups/history?limit=50');
    renderHistory(data.tasks || []);

    // Update next schedule stat
    const sched = await apiFetch('/api/v1/settings/schedule');
    const now = new Date();
    const candidates = [
      new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(),
        sched.morning_hour, sched.morning_minute)),
      new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(),
        sched.evening_hour, sched.evening_minute)),
    ].filter(d => d > now).sort((a, b) => a - b);

    document.getElementById('stat-next').textContent = candidates.length
      ? candidates[0].toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) + ' UTC'
      : 'Demain';
  } catch (_) { /* silent */ }
}

function renderHistory(tasks) {
  const tbody = document.getElementById('history-body');
  if (!tasks.length) {
    tbody.innerHTML = `<tr>
      <td colspan="6" class="text-center py-10 text-gray-400 text-sm">
        Aucun backup enregistré.
      </td>
    </tr>`;
    return;
  }

  tbody.innerHTML = tasks.map(t => `
    <tr class="border-b border-gray-50 hover:bg-gray-50/60 transition">
      <td class="py-3 px-2 text-gray-600 font-mono text-xs">${t.task_id.slice(0, 8)}…</td>
      <td class="py-3 px-2">
        <span class="text-xs font-semibold px-2.5 py-1 rounded-full badge-${t.status}">
          ${t.status}
        </span>
      </td>
      <td class="py-3 px-2 text-gray-500 text-xs">${t.triggered_by || '—'}</td>
      <td class="py-3 px-2 text-gray-500 text-xs whitespace-nowrap">${formatDate(t.timestamp)}</td>
      <td class="py-3 px-2 text-gray-500 text-xs">${t.duration_seconds ? t.duration_seconds + 's' : '—'}</td>
      <td class="py-3 px-2 text-right">
        ${t.status === 'COMPLETED' && t.s3_url
          ? `<button onclick="downloadBackup('${t.task_id}')"
               class="text-xs text-green-dark hover:text-green font-medium">
               Télécharger
             </button>`
          : ''}
      </td>
    </tr>
  `).join('');
}

async function downloadBackup(taskId) {
  try {
    const data = await apiFetch(`/api/v1/backups/${taskId}/download`);
    window.open(data.download_url, '_blank');
  } catch (err) {
    showToast(`Erreur: ${err.message}`, 'error');
  }
}

// ══════════════════════════════════════════════════════════════════════════
// Schedule settings
// ══════════════════════════════════════════════════════════════════════════
async function loadSchedule() {
  try {
    const s = await apiFetch('/api/v1/settings/schedule');
    document.getElementById('morning-hour').value = s.morning_hour ?? 9;
    document.getElementById('morning-min').value  = s.morning_minute ?? 0;
    document.getElementById('evening-hour').value = s.evening_hour ?? 21;
    document.getElementById('evening-min').value  = s.evening_minute ?? 0;
  } catch (_) { /* use defaults */ }
}

async function saveSchedule() {
  const body = {
    morning_hour:   parseInt(document.getElementById('morning-hour').value, 10),
    morning_minute: parseInt(document.getElementById('morning-min').value, 10),
    evening_hour:   parseInt(document.getElementById('evening-hour').value, 10),
    evening_minute: parseInt(document.getElementById('evening-min').value, 10),
  };

  try {
    await apiFetch('/api/v1/settings/schedule', {
      method: 'PUT',
      body: JSON.stringify(body),
    });
    const msg = document.getElementById('schedule-msg');
    msg.classList.remove('hidden');
    setTimeout(() => msg.classList.add('hidden'), 3000);
  } catch (err) {
    showToast(`Erreur: ${err.message}`, 'error');
  }
}

// ══════════════════════════════════════════════════════════════════════════
// Auth
// ══════════════════════════════════════════════════════════════════════════
function logout() {
  localStorage.removeItem('id_token');
  window.location.href = '/login';
}

// ══════════════════════════════════════════════════════════════════════════
// Utilities
// ══════════════════════════════════════════════════════════════════════════
async function apiFetch(url, options = {}) {
  const resp = await fetch(url, {
    headers: HEADERS,
    ...options,
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

function fmtBytes(bytes) {
  if (!bytes) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

function formatDate(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString('fr-FR', {
      day: '2-digit', month: '2-digit', year: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  } catch (_) {
    return iso;
  }
}

let _toastTimer = null;
function showToast(msg, type = 'info') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = [
    'fixed bottom-6 right-6 z-50 flex items-center gap-3 px-5 py-3.5',
    'rounded-xl shadow-xl text-sm font-medium',
    type === 'success' ? 'bg-green-100 text-green-800' : '',
    type === 'error'   ? 'bg-red-100 text-red-700'     : '',
    type === 'info'    ? 'bg-navy text-white'           : '',
  ].join(' ');
  el.classList.remove('hidden');

  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.add('hidden'), 4000);
}
