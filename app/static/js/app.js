/**
 * S3 Backup Flow — Dashboard JS
 * JHBridge Translation Services
 */

// ── Auth token (from Cognito login) ──────────────────────────────────────────
const TOKEN = localStorage.getItem('id_token') || '';
const HEADERS = TOKEN
  ? { 'Content-Type': 'application/json', Authorization: `Bearer ${TOKEN}` }
  : { 'Content-Type': 'application/json' };

// ── WebSocket state ───────────────────────────────────────────────────────────
let _ws              = null;
let _activeTaskId    = null;
let _wsRetries       = 0;
const WS_MAX_RETRIES = 4;

// ── Auto-refresh intervals ────────────────────────────────────────────────────
const HEALTH_INTERVAL  = 30_000;   // 30s
const HISTORY_INTERVAL = 30_000;   // 30s

// ── Clock state ───────────────────────────────────────────────────────────────
let _clockInterval = null;
let _clockSched    = null;
const CLOCK_CIRC   = 2 * Math.PI * 42;   // r=42 → ≈ 263.9

// ══════════════════════════════════════════════════════════════════════════════
// Boot
// ══════════════════════════════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', () => {
  loadHealth();
  loadHistory();
  loadSchedule();

  // Auto-refresh in background so scheduled backups appear without F5
  setInterval(loadHealth,  HEALTH_INTERVAL);
  setInterval(loadHistory, HISTORY_INTERVAL);

  // Resume progress tracking if a backup was running before page reload
  const savedTask = sessionStorage.getItem('active_task_id');
  if (savedTask) connectWebSocket(savedTask);
});

// ══════════════════════════════════════════════════════════════════════════════
// Clock — live countdown to next scheduled backup
// ══════════════════════════════════════════════════════════════════════════════
function startClock(sched) {
  _clockSched = sched;
  if (_clockInterval) clearInterval(_clockInterval);
  _clockInterval = setInterval(tickClock, 1000);
  tickClock();
}

function tickClock() {
  if (!_clockSched) return;

  const now = new Date();
  const mh = _clockSched.morning_hour,   mm = _clockSched.morning_minute;
  const eh = _clockSched.evening_hour,   em = _clockSched.evening_minute;

  // Build candidates (today + tomorrow morning) to always find a future slot
  const d = now;
  const slots = [
    { t: new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), mh, mm)), label: 'Matin',  key:'morning' },
    { t: new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), eh, em)), label: 'Soir',   key:'evening' },
    { t: new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()+1, mh, mm)), label: 'Matin', key:'morning' },
  ];

  // Dot labels for the two daily slots
  document.getElementById('lbl-morning').textContent =
    `Matin ${String(mh).padStart(2,'0')}:${String(mm).padStart(2,'0')}`;
  document.getElementById('lbl-evening').textContent =
    `Soir ${String(eh).padStart(2,'0')}:${String(em).padStart(2,'0')}`;

  const future = slots.filter(s => s.t > now).sort((a,b) => a.t - b.t);
  if (!future.length) return;

  const next   = future[0];
  const diff   = next.t - now;           // ms until next backup
  const isOver = diff < 0;

  // Previous slot = last one that is NOT future
  const past   = slots.filter(s => s.t <= now).sort((a,b) => b.t - a.t);
  const prev   = past[0];
  const window = prev ? (next.t - prev.t) : (12 * 3600 * 1000);
  const elapsed = window - diff;
  const pct    = Math.max(0, Math.min(1, elapsed / window));
  const offset = CLOCK_CIRC * (1 - pct);

  // Update arc
  const arc = document.getElementById('clock-arc');
  arc.style.strokeDashoffset = offset;
  arc.classList.remove('clock-arc-warn', 'clock-arc-over');
  if (isOver)      arc.classList.add('clock-arc-over');
  else if (pct > 0.85) arc.classList.add('clock-arc-warn');

  // Countdown display
  const absDiff = Math.abs(diff);
  const h = Math.floor(absDiff / 3600000);
  const m = Math.floor((absDiff % 3600000) / 60000);
  const s = Math.floor((absDiff % 60000) / 1000);
  const hm = `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}`;
  const ss = String(s).padStart(2,'0');

  document.getElementById('clock-hm').textContent    = hm;
  document.getElementById('clock-s').textContent     = ss + 's';
  document.getElementById('clock-label').textContent = next.label;
  document.getElementById('clock-state').textContent = isOver ? 'En attente...' : 'avant backup';

  // Highlight active slot dot
  ['morning','evening'].forEach(k => {
    const dot = document.getElementById(`dot-${k}`);
    dot.className = `w-2 h-2 rounded-full inline-block ${next.key === k ? 'bg-green-dark' : 'bg-gray-200'}`;
  });

  // Update stat-next card too
  document.getElementById('stat-next').textContent =
    `${next.label} · ${hm}:${ss}`;
}

// ══════════════════════════════════════════════════════════════════════════════
// Health check
// ══════════════════════════════════════════════════════════════════════════════
async function loadHealth() {
  try {
    const data = await apiFetch('/api/v1/health');

    setHealth('health-redis',  data.redis);
    setHealth('health-dynamo', data.dynamodb);
    setHealth('health-s3',     data.s3);

    if (data.last_backup) {
      document.getElementById('stat-last').textContent = formatDate(data.last_backup);
    }
    document.getElementById('stat-storage').textContent = fmtBytes(data.total_storage_bytes);
    document.getElementById('stat-count').textContent   = data.total_files;
    document.getElementById('nav-status').classList.remove('hidden');
  } catch (_) { /* silent — keep last known values */ }
}

function setHealth(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  if (value === 'ok') {
    el.textContent = 'OK';
    el.className   = 'font-medium text-green-600';
  } else {
    el.textContent = 'Erreur';
    el.className   = 'font-medium text-red-500';
  }
}

// ══════════════════════════════════════════════════════════════════════════════
// Backup trigger — confirmation dialog + loading guard
// ══════════════════════════════════════════════════════════════════════════════
function triggerBackup() {
  document.getElementById('btn-trigger').classList.add('hidden');
  document.getElementById('confirm-dialog').classList.remove('hidden');
}

function cancelBackup() {
  document.getElementById('btn-trigger').classList.remove('hidden');
  document.getElementById('confirm-dialog').classList.add('hidden');
}

async function confirmBackup() {
  // Hide dialog immediately, show spinner while we wait for the API
  document.getElementById('confirm-dialog').classList.add('hidden');
  setTriggerLoading(true);

  try {
    const data = await apiFetch('/api/v1/backups/run', {
      method: 'POST',
      body: JSON.stringify({}),
    });
    showToast(`Backup lancé — ID: ${data.task_id.slice(0, 8)}…`, 'success');
    sessionStorage.setItem('active_task_id', data.task_id);
    _wsRetries = 0;
    connectWebSocket(data.task_id);
  } catch (err) {
    showToast(`Erreur: ${err.message}`, 'error');
    setTriggerLoading(false);
    document.getElementById('btn-trigger').classList.remove('hidden');
  }
}

function setTriggerLoading(loading) {
  const btn = document.getElementById('btn-trigger-loading');
  if (!btn) return;
  btn.classList.toggle('hidden', !loading);
}

// ══════════════════════════════════════════════════════════════════════════════
// WebSocket — live progress with auto-reconnect
// ══════════════════════════════════════════════════════════════════════════════
function connectWebSocket(taskId) {
  if (_ws) { try { _ws.close(); } catch (_) {} }
  _activeTaskId = taskId;

  // Show loading state while connecting
  setTriggerLoading(true);

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url   = `${proto}://${location.host}/api/v1/ws/backup/${taskId}`;
  _ws = new WebSocket(url);

  _ws.onopen = () => {
    _wsRetries = 0;
    setTriggerLoading(false);
    // Apply pulse animation to progress ring container
    document.getElementById('progress-ring').classList.add('pulse');
  };

  _ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    updateProgress(msg.progress, msg.phase, msg.done, msg.error);

    if (msg.done || msg.error) {
      sessionStorage.removeItem('active_task_id');
      document.getElementById('progress-ring').classList.remove('pulse');
      loadHistory();
      loadHealth();
      setTimeout(() => resetProgress(), 4000);
    }
  };

  _ws.onerror = () => {
    document.getElementById('progress-ring').classList.remove('pulse');
  };

  _ws.onclose = () => {
    // Retry with exponential backoff if backup may still be running
    if (_activeTaskId && _wsRetries < WS_MAX_RETRIES) {
      _wsRetries++;
      const delay = Math.min(1000 * Math.pow(2, _wsRetries), 16000); // 2s, 4s, 8s, 16s
      showToast(`Reconnexion WebSocket dans ${delay / 1000}s… (${_wsRetries}/${WS_MAX_RETRIES})`, 'info');
      setTimeout(() => {
        if (_activeTaskId) connectWebSocket(_activeTaskId);
      }, delay);
    } else if (_wsRetries >= WS_MAX_RETRIES) {
      showToast('Connexion WebSocket perdue. Rafraîchissez la page.', 'error');
      setTriggerLoading(false);
      resetProgress();
    }
  };
}

function updateProgress(pct, phase, done, error) {
  pct = Math.max(0, Math.min(100, Number(pct)));
  const circumference = 314;

  const circle = document.getElementById('progress-circle');
  circle.style.strokeDashoffset = circumference - (circumference * pct) / 100;
  circle.style.stroke = error ? '#FC8181' : '#68D391';

  document.getElementById('progress-pct').textContent   = `${pct}%`;
  document.getElementById('progress-phase').textContent = phase || 'En cours...';

  // Clear error message
  const errEl = document.getElementById('progress-error');
  errEl.classList.add('hidden');
  errEl.textContent = '';

  const badge = document.getElementById('progress-badge');
  if (done) {
    badge.textContent = 'COMPLETED';
    badge.className   = 'text-xs font-semibold px-3 py-1 rounded-full badge-COMPLETED';
    showToast('Backup terminé avec succès !', 'success');
  } else if (error) {
    badge.textContent = 'FAILED';
    badge.className   = 'text-xs font-semibold px-3 py-1 rounded-full badge-FAILED';
    showToast('Backup échoué. Vérifiez les logs.', 'error');
    // Show error detail if we can fetch it
    if (_activeTaskId) {
      apiFetch(`/api/v1/backups/status/${_activeTaskId}`)
        .then(t => {
          if (t.error_message) {
            errEl.textContent = `Erreur : ${t.error_message}`;
            errEl.classList.remove('hidden');
          }
        })
        .catch(() => {});
    }
  } else {
    badge.textContent = 'RUNNING';
    badge.className   = 'text-xs font-semibold px-3 py-1 rounded-full badge-RUNNING';
  }
}

function resetProgress() {
  _activeTaskId = null;
  document.getElementById('progress-circle').style.strokeDashoffset = '314';
  document.getElementById('progress-circle').style.stroke = '#68D391';
  document.getElementById('progress-pct').textContent     = '0%';
  document.getElementById('progress-phase').textContent   = 'En attente...';
  document.getElementById('progress-error').classList.add('hidden');
  document.getElementById('progress-ring').classList.remove('pulse');

  const badge = document.getElementById('progress-badge');
  badge.textContent = 'IDLE';
  badge.className   = 'text-xs font-semibold px-3 py-1 rounded-full bg-gray-100 text-gray-500';

  setTriggerLoading(false);
  document.getElementById('btn-trigger').classList.remove('hidden');
}

// ══════════════════════════════════════════════════════════════════════════════
// History table
// ══════════════════════════════════════════════════════════════════════════════
async function loadHistory() {
  try {
    const data  = await apiFetch('/api/v1/backups/history?limit=50');
    renderHistory(data.tasks || []);

    const sched = await apiFetch('/api/v1/settings/schedule');
    startClock(sched);
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

  tbody.innerHTML = tasks.map(t => {
    const dur = parseFloat(t.duration_seconds || 0);
    return `
    <tr class="border-b border-gray-50 hover:bg-gray-50/60 transition">
      <td class="py-3 px-2 text-gray-600 font-mono text-xs">${t.task_id.slice(0, 8)}…</td>
      <td class="py-3 px-2">
        <span class="text-xs font-semibold px-2.5 py-1 rounded-full badge-${t.status}">
          ${t.status}
        </span>
      </td>
      <td class="py-3 px-2 text-gray-500 text-xs">${t.triggered_by || '—'}</td>
      <td class="py-3 px-2 text-gray-500 text-xs whitespace-nowrap">${formatDate(t.timestamp)}</td>
      <td class="py-3 px-2 text-gray-500 text-xs">${dur > 0 ? dur.toFixed(1) + 's' : '—'}</td>
      <td class="py-3 px-2 text-right">
        ${t.status === 'COMPLETED' && t.s3_url
          ? `<button onclick="downloadBackup('${t.task_id}')"
               class="text-xs text-green-dark hover:text-green font-medium">
               Télécharger
             </button>`
          : (t.status === 'FAILED' && t.error_message
            ? `<span class="text-xs text-red-400 italic" title="${t.error_message}">Voir erreur</span>`
            : '')}
      </td>
    </tr>`;
  }).join('');
}

async function downloadBackup(taskId) {
  try {
    const data = await apiFetch(`/api/v1/backups/${taskId}/download`);
    window.open(data.download_url, '_blank');
  } catch (err) {
    showToast(`Erreur: ${err.message}`, 'error');
  }
}

// ══════════════════════════════════════════════════════════════════════════════
// Schedule settings
// ══════════════════════════════════════════════════════════════════════════════
async function loadSchedule() {
  try {
    const s = await apiFetch('/api/v1/settings/schedule');
    document.getElementById('morning-hour').value = s.morning_hour  ?? 9;
    document.getElementById('morning-min').value  = s.morning_minute ?? 0;
    document.getElementById('evening-hour').value = s.evening_hour  ?? 21;
    document.getElementById('evening-min').value  = s.evening_minute ?? 0;
  } catch (_) { /* use defaults */ }
}

async function saveSchedule() {
  const body = {
    morning_hour:   parseInt(document.getElementById('morning-hour').value, 10),
    morning_minute: parseInt(document.getElementById('morning-min').value,  10),
    evening_hour:   parseInt(document.getElementById('evening-hour').value, 10),
    evening_minute: parseInt(document.getElementById('evening-min').value,  10),
  };

  try {
    const saved = await apiFetch('/api/v1/settings/schedule', {
      method: 'PUT',
      body: JSON.stringify(body),
    });
    startClock(saved);   // restart clock with new times
    const msg = document.getElementById('schedule-msg');
    msg.classList.remove('hidden');
    setTimeout(() => msg.classList.add('hidden'), 3000);
  } catch (err) {
    showToast(`Erreur: ${err.message}`, 'error');
  }
}

// ══════════════════════════════════════════════════════════════════════════════
// Auth
// ══════════════════════════════════════════════════════════════════════════════
async function logout() {
  localStorage.removeItem('id_token');
  try { await fetch('/api/v1/auth/logout', { method: 'POST' }); } catch (_) {}
  window.location.href = '/login';
}

// ══════════════════════════════════════════════════════════════════════════════
// Utilities
// ══════════════════════════════════════════════════════════════════════════════
async function apiFetch(url, options = {}) {
  const resp = await fetch(url, { headers: HEADERS, ...options });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

function fmtBytes(bytes) {
  if (!bytes) return '0 B';
  const k     = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i     = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

function formatDate(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString('fr-FR', {
      day: '2-digit', month: '2-digit', year: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  } catch (_) { return iso; }
}

let _toastTimer = null;
function showToast(msg, type = 'info') {
  const icons = { success: '✓', error: '✕', info: 'ℹ' };
  const el = document.getElementById('toast');
  el.innerHTML = `<span class="font-bold">${icons[type] || ''}</span> ${msg}`;
  el.className = [
    'fixed bottom-6 right-6 z-50 flex items-center gap-3 px-5 py-3.5',
    'rounded-xl shadow-xl text-sm font-medium transition-all',
    type === 'success' ? 'bg-green-100 text-green-800'  : '',
    type === 'error'   ? 'bg-red-100 text-red-700'      : '',
    type === 'info'    ? 'bg-navy text-white'            : '',
  ].join(' ');
  el.style.minWidth = '260px';
  el.classList.remove('hidden');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.add('hidden'), 5000);
}
