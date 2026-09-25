function toLogin(extra = '') {
  const next = location.pathname + location.search;
  location.href = `/login?next=${encodeURIComponent(next)}${extra}`;
}

/** JSON API call. GET without a body; POST by default with a body; any method via `method`. */
export async function api(path, body, method) {
  const m = method || (body === undefined ? 'GET' : 'POST');
  const opts = { method: m };
  if (body !== undefined && m !== 'GET') {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) { toLogin(); throw new Error('Signed out'); }
  if (res.status === 403 && data.detail === 'password change required') { location.href = '/login?change=1'; throw new Error(data.detail); }
  if (!res.ok) {
    const d = data.detail;
    throw new Error(typeof d === 'string' ? d : Array.isArray(d) ? d.map((x) => x.msg).join('; ') : `HTTP ${res.status}`);
  }
  return data;
}

/** WebSocket with automatic reconnect. onFrame receives parsed frames. */
export function connect(onFrame, onState) {
  let ws;
  let retry = 500;
  const open = () => {
    ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
    ws.onopen = () => { retry = 500; onState?.(true); };
    ws.onmessage = (e) => onFrame(JSON.parse(e.data));
    ws.onclose = (e) => {
      if (e.code === 4401) { toLogin(); return; }     // session expired or revoked
      onState?.(false);
      setTimeout(open, retry);
      retry = Math.min(retry * 2, 8000);
    };
  };
  open();
}
