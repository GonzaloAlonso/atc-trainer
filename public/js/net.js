import { getLang } from './i18n.js';

// "tutorial" / "exercise" route API calls and the live feed to the caller's private sandbox.
let context = null;
export function setContext(ctx) { context = ctx; }
export function getContext() { return context; }

function toLogin(extra = '') {
  const next = location.pathname + location.search;
  location.href = `/login?next=${encodeURIComponent(next)}${extra}`;
}

/** JSON API call. GET without a body; POST by default with a body; any method via `method`. */
export async function api(path, body, method) {
  const m = method || (body === undefined ? 'GET' : 'POST');
  // the server answers errors in the interface's language
  const opts = { method: m, headers: { 'Accept-Language': getLang() } };
  if (context) opts.headers['X-ATC-Context'] = context;
  if (body !== undefined && m !== 'GET') {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) { toLogin(); throw new Error('Signed out'); }
  if (res.status === 403 && data.code === 'password_change_required') { location.href = '/login?change=1'; throw new Error(data.detail); }
  if (!res.ok) {
    const d = data.detail;
    throw new Error(typeof d === 'string' ? d : data.message || (Array.isArray(d) ? d.map((x) => x.msg).join('; ') : `HTTP ${res.status}`));
  }
  return data;
}

/**
 * WebSocket with automatic reconnect. onFrame receives parsed frames.
 * Returns { reconnect() } to switch feeds after setContext(); onSandboxGone fires when the
 * training sandbox no longer exists (stopped elsewhere or reaped while idle).
 */
export function connect(onFrame, onState, onSandboxGone) {
  let ws;
  let retry = 500;
  let generation = 0;
  const open = () => {
    const gen = ++generation;
    const ctx = context ? `?ctx=${encodeURIComponent(context)}` : '';
    ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws${ctx}`);
    ws.onopen = () => { retry = 500; onState?.(true); };
    ws.onmessage = (e) => { if (gen === generation) onFrame(JSON.parse(e.data)); };
    ws.onclose = (e) => {
      if (gen !== generation) return;                 // replaced on purpose by reconnect()
      if (e.code === 4401) { toLogin(); return; }     // session expired or revoked
      if (e.code === 4404) { onSandboxGone?.(); return; }
      onState?.(false);
      setTimeout(() => { if (gen === generation) open(); }, retry);
      retry = Math.min(retry * 2, 8000);
    };
  };
  open();
  return {
    reconnect() {
      const old = ws;
      open();
      try { old.close(); } catch { /* ignore */ }
    },
  };
}
