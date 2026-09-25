import { api } from './net.js';
import { F } from './traffic.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const pad3 = (n) => String(Math.round(n)).padStart(3, '0');
const hhmmss = (t) => new Date(t * 1000).toISOString().slice(11, 19);

/**
 * DOM side of the controller working position: top bar, alert/decision/traffic lists,
 * flight strip with clearance controls, radio log and command line.
 */
export class UI {
  constructor(app) {
    this.app = app;
    this.frame = null;
    this.eventSeq = 0;
    this.sigs = {};
    this.detail = null;
    this.lastListRender = 0;
    this.bindTop();
    this.bindTabs();
    this.bindStrip();
    this.bindCommand();
  }

  toast(msg, err = false) {
    const t = $('toast');
    t.textContent = msg;
    t.className = err ? 'err' : '';
    clearTimeout(this._toastT);
    this._toastT = setTimeout(() => t.classList.add('hidden'), 3500);
  }

  async call(path, body) {
    try { return await api(path, body); } catch (e) { this.toast(e.message, true); return null; }
  }

  // ------------------------------------------------------------------ top bar
  bindTop() {
    $('btn-pause').onclick = () => this.togglePause();
    $('speed-seg').onclick = (e) => {
      const s = e.target.dataset.speed;
      if (s) this.call('/api/sim', { action: 'speed', speed: Number(s) });
    };
    $('scenario').onchange = (e) => {
      const v = e.target.value;
      const body = v === 'live' ? { action: 'reset', mode: 'live' } : { action: 'reset', mode: 'replay', start: Number(v) };
      this.call('/api/sim', body).then((r) => r && this.toast(v === 'live' ? 'Live traffic' : `Replay from ${hhmmss(Number(v))} UTC`));
    };
    $('sector').onchange = (e) => {
      const id = e.target.value || null;
      this.call('/api/sector', { sector: id });
      this.app.onSector(id);
    };
    $('ai-mode').onchange = () => this.setAi();
    $('ai-agent').onchange = () => this.setAi();
    $('btn-settings').onclick = () => $('settings').classList.toggle('hidden');
    $('style-seg').onclick = (e) => { const s = e.target.dataset.style; if (s) this.app.setStyle(s); };
  }

  async setAi() {
    const r = await this.call('/api/ai', { mode: $('ai-mode').value, agent: $('ai-agent').value });
    if (r && !r.agent.available) this.toast(`Agent "${r.agent.name}" is not configured (see README)`, true);
  }

  togglePause() {
    if (!this.frame) return;
    this.call('/api/sim', { action: this.frame.paused ? 'resume' : 'pause' });
  }

  setSectors(sectors) {
    const sel = $('sector');
    for (const s of sectors) {
      const o = document.createElement('option');
      o.value = s.id;
      o.textContent = `${s.name} (FL${pad3(s.fl_min)}–${s.fl_max >= 600 ? 'UNL' : pad3(s.fl_max)})`;
      sel.appendChild(o);
    }
  }

  setStatus(st) {
    const rec = st.recorder;
    const cov = rec.coverage;
    const ai = $('ai-agent');
    if (!ai.options.length) {
      for (const a of st.ai.available_agents) {
        const o = document.createElement('option'); o.value = a; o.textContent = a; ai.appendChild(o);
      }
    }
    ai.value = st.ai.agent.name;
    $('ai-mode').value = st.ai.mode;

    // scenario options depend on how much history has been recorded
    const sel = $('scenario');
    const cur = this.frame?.mode === 'live' ? 'live' : sel.value;
    sel.innerHTML = '';
    const add = (v, label) => { const o = document.createElement('option'); o.value = v; o.textContent = label; sel.appendChild(o); };
    add('live', 'Live');
    if (cov.first) {
      const span = (cov.last - cov.first) / 3600;
      add(String(cov.first), `Replay from start (${hhmmss(cov.first)}, ${span.toFixed(1)} h)`);
      for (const h of [1, 3, 6, 12]) if (span > h + 0.25) add(String(Math.round(cov.last - h * 3600)), `Replay last ${h} h`);
    }
    sel.value = [...sel.options].some((o) => o.value === cur) ? cur : (this.frame?.mode === 'live' ? 'live' : sel.options[1]?.value ?? 'live');

    const next = Math.max(0, rec.next_poll - st.now);
    const span = cov.first ? `${cov.snapshots} snapshots ${hhmmss(cov.first)}–${hhmmss(cov.last)} UTC` : 'no data yet';
    $('rec-status').textContent = `Visor ${st.version} · ${st.recording ? '●' : '○ not'} recording OpenSky (${rec.authenticated ? 'authenticated' : 'anonymous'}, every ${Math.round(rec.interval_s)} s) · ${span} · next poll in ${Math.floor(next / 60)}:${String(Math.floor(next % 60)).padStart(2, '0')}${rec.credits_left != null ? ` · ${rec.credits_left} credits left` : ''}${rec.last_error ? ' · ⚠ ' + rec.last_error : ''}`;
  }

  // ------------------------------------------------------------------ frames
  onFrame(frame) {
    this.frame = frame;
    const badge = $('mode-badge');
    badge.textContent = frame.lockstep ? 'LOCKSTEP' : frame.mode.toUpperCase();
    badge.className = 'badge' + (frame.mode === 'replay' ? ' replay' : '');
    $('btn-pause').textContent = frame.paused ? '▶' : '❚❚';
    for (const b of $('speed-seg').children) {
      b.classList.toggle('on', Number(b.dataset.speed) === frame.speed);
      b.disabled = frame.mode === 'live' && b.dataset.speed !== '1';
    }
    if ($('sector').value !== (frame.sector ?? '')) { $('sector').value = frame.sector ?? ''; this.app.onSector(frame.sector); }
    if (document.activeElement !== $('ai-mode')) $('ai-mode').value = frame.ai.mode;

    const s = frame.score;
    $('score').innerHTML = `<span class="pts">${s.points ?? 0}</span>` +
      `<span>LoS <b>${s.los ?? 0}</b></span><span>STCA <b>${s.stca ?? 0}</b></span>` +
      `<span>REQ <b>${s.requests_granted ?? 0}</b>/<b>${s.requests_expired ?? 0}</b></span>` +
      `<span>HND <b>${s.handled ?? 0}</b></span>` +
      `<span title="clearances: you / AI"><b style="color:var(--human)">${s.clearances_human ?? 0}</b>/<b style="color:var(--ai)">${s.clearances_ai ?? 0}</b></span>`;

    if (frame.event_seq > this.eventSeq) this.pullEvents();
    const now = performance.now();
    if (now - this.lastListRender > 900) {
      this.lastListRender = now;
      this.renderAlerts();
      this.renderDecisions();
      this.renderTraffic();
    }
  }

  tickClock(simT) {
    if (!simT) return;
    const d = new Date(simT * 1000);
    $('clock').textContent = d.toISOString().slice(11, 19);
    $('date').textContent = d.toISOString().slice(0, 10) + ' UTC';
  }

  // ------------------------------------------------------------------ lists
  bindTabs() {
    document.querySelector('.tabs').onclick = (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      for (const x of document.querySelectorAll('.tabs button')) x.classList.toggle('on', x === b);
      for (const x of document.querySelectorAll('.tab')) x.classList.toggle('on', x.id === 'tab-' + b.dataset.tab);
    };
  }

  cs(id) { return this.app.store.map.get(id)?.cs ?? id; }

  renderAlerts() {
    const f = this.frame;
    const conf = f.conflicts.filter((c) => c[6]).sort((a, b) => (a[2] === 'LOS' ? -1 : 0) - (b[2] === 'LOS' ? -1 : 0) || a[3] - b[3]);
    const reqs = f.decisions.filter((d) => d.status === 'open' && d.kind !== 'conflict');
    const sig = JSON.stringify([conf.map((c) => [c[0], c[1], c[2], Math.round(c[3] / 10)]), reqs.map((r) => r.id)]);
    const n = $('n-alerts');
    n.textContent = conf.length + reqs.length;
    n.classList.toggle('hot', conf.some((c) => c[2] === 'LOS'));
    if (sig === this.sigs.alerts) return;
    this.sigs.alerts = sig;
    const el = $('tab-alerts');
    if (!conf.length && !reqs.length) {
      el.innerHTML = `<div class="empty">No alerts${f.sector ? ' in your sector' : ''}.<br>STCA warns ${'≤'}2 min ahead; separation minima 5 NM / 1000 ft (3 NM below FL100).</div>`;
      return;
    }
    el.innerHTML = conf.map(([a, b, kind, tTo, h, v]) => `
      <div class="card ${kind === 'LOS' ? 'los' : 'stca'}" data-sel="${a}">
        <div class="card-head"><span class="k" style="color:${kind === 'LOS' ? 'var(--alert)' : 'var(--warn)'}">${kind === 'LOS' ? 'SEPARATION LOST' : 'STCA'}</span><span>${kind === 'LOS' ? 'now' : Math.round(tTo) + ' s'}</span></div>
        <div class="card-head" style="margin-top:4px"><span data-sel="${a}">${esc(this.cs(a))}</span><span>↔</span><span data-sel="${b}">${esc(this.cs(b))}</span></div>
        <div class="meta">${h.toFixed(1)} NM · ${v} ft at closest</div>
      </div>`).join('') + reqs.map((d) => `
      <div class="card req" data-sel="${d.subjects[0]}" data-tab-to="decisions">
        <div class="card-head"><span class="k" style="color:var(--accent)">PILOT REQUEST</span><span>${d.id}</span></div>
        <div class="prompt">${esc(d.questions[0].prompt)}</div>
      </div>`).join('');
    el.querySelectorAll('[data-sel]').forEach((x) => { x.onclick = (e) => { e.stopPropagation(); this.app.select(x.dataset.sel, true); }; });
  }

  renderDecisions() {
    const f = this.frame;
    const ds = [...f.decisions].sort((a, b) => (a.status === 'open' ? 0 : 1) - (b.status === 'open' ? 0 : 1) || b.created_t - a.created_t);
    const open = ds.filter((d) => d.status === 'open').length;
    $('n-dec').textContent = open;
    const sig = JSON.stringify(ds.map((d) => [d.id, d.status, d.updated_t, !!d.suggestion, d.answered_by]));
    if (sig === this.sigs.dec) return;
    this.sigs.dec = sig;
    const el = $('tab-decisions');
    if (!ds.length) {
      el.innerHTML = `<div class="empty">Decision points appear here: conflicts to resolve and pilot requests.<br>Each option shows the outcome of a fast-time prediction. Turn on <b>AI</b> to get advice or let it act.${f.sector ? '' : '<br><br>Tip: pick a sector — with "All Europe" decisions are only built when AI is on.'}</div>`;
      return;
    }
    el.innerHTML = ds.map((d) => {
      const q = d.questions[0];
      const sug = d.suggestion?.action;
      const kindLabel = { conflict: 'CONFLICT', level_request: 'LEVEL REQUEST', route_request: 'ROUTE REQUEST' }[d.kind];
      const color = d.kind === 'conflict' ? 'var(--warn)' : 'var(--accent)';
      const opts = [...q.options].sort((a, b) => a.predicted.los_duration_s - b.predicted.los_duration_s || a.cost - b.cost).slice(0, 6);
      const optHtml = d.status !== 'open' ? '' : opts.map((o) => {
        const p = o.predicted;
        const ok = p.los_duration_s === 0;
        const pred = ok ? (p.min_h_nm != null ? `min ${p.min_h_nm}NM` : 'clear') : `LoS ${p.los_duration_s}s`;
        return `<div class="opt ${o.id === sug ? 'suggested' : ''}"><span class="${ok ? 'ok' : 'bad'}">${ok ? '✓' : '✗'}</span>
          <span>${esc(o.label)} <span class="pred">${pred}</span></span>
          <button class="mini" data-dec="${d.id}" data-opt="${o.id}">Issue</button></div>`;
      }).join('');
      const aiLine = d.status === 'open' && d.suggestion ? `<div class="ai-line"><span>AI (${esc(d.suggestion.agent)}): ${esc(q.options.find((o) => o.id === sug)?.label ?? sug)} · ${Math.round((d.suggestion.confidence ?? 0) * 100)}%</span>
          <button class="mini ai" data-dec="${d.id}" data-opt="${sug}" data-by="ai:${esc(d.suggestion.agent)}">Accept</button></div>` : '';
      const done = d.status !== 'open' ? `<div class="meta">${d.status}${d.answered_by ? ' by ' + esc(d.answered_by) : ''}${d.answer?.action ? ' → ' + esc(q.options.find((o) => o.id === d.answer.action)?.label ?? '') : ''}</div>` : '';
      return `<div class="card ${d.kind === 'conflict' ? 'stca' : 'req'}" style="${d.status !== 'open' ? 'opacity:.55' : ''}">
        <div class="card-head"><span class="k" style="color:${color}">${kindLabel}</span><span class="status-pill">${d.id} · ${d.status}</span></div>
        <div class="prompt">${esc(q.prompt)}</div>${optHtml}${aiLine}${done}
        ${d.status === 'open' ? `<div style="text-align:right;margin-top:4px"><button class="mini ghost" data-dismiss="${d.id}">Dismiss</button></div>` : ''}
      </div>`;
    }).join('');
    el.querySelectorAll('[data-dec]').forEach((b) => {
      b.onclick = async () => {
        const r = await this.call(`/api/decisions/${b.dataset.dec}`, { answers: { action: b.dataset.opt }, by: b.dataset.by || 'human' });
        if (r) this.toast(`${r.decision}: ${r.option}`);
      };
    });
    el.querySelectorAll('[data-dismiss]').forEach((b) => { b.onclick = () => this.call(`/api/decisions/${b.dataset.dismiss}/dismiss`, {}); });
  }

  renderTraffic() {
    const f = this.frame;
    const recs = this.app.store.list.filter((r) => (f.sector ? r.flags & F.SECTOR : r.flags & (F.HUMAN | F.AI)));
    recs.sort((a, b) => b.alt - a.alt);
    $('n-traffic').textContent = recs.length;
    const sig = JSON.stringify(recs.map((r) => [r.id, Math.round(r.alt / 100), r.cfl, r.lateral, r.flags]));
    if (sig === this.sigs.traffic) return;
    this.sigs.traffic = sig;
    const el = $('tab-traffic');
    if (!recs.length) {
      el.innerHTML = `<div class="empty">${f.sector ? 'No traffic in the sector right now.' : 'Pick a sector to see the traffic you are responsible for.<br>Aircraft you (or the AI) have cleared are listed here.'}</div>`;
      return;
    }
    el.innerHTML = `<table class="traffic"><thead><tr><th>CS</th><th>FL</th><th>CFL</th><th>GS</th><th>NAV</th></tr></thead><tbody>${recs.map((r) => {
      const col = r.flags & F.LOS ? 'var(--alert)' : r.flags & F.STCA ? 'var(--warn)' : r.flags & F.HUMAN ? 'var(--human)' : r.flags & F.AI ? 'var(--ai)' : 'var(--text)';
      return `<tr data-sel="${r.id}"><td style="color:${col}">${esc(r.cs)}</td><td>${pad3(r.alt / 100)}${r.vs > 250 ? '↑' : r.vs < -250 ? '↓' : ''}</td><td>${r.cfl != null ? pad3(r.cfl) : ''}</td><td>${r.gs}</td><td>${esc(r.lateral)}</td></tr>`;
    }).join('')}</tbody></table>`;
    el.querySelectorAll('[data-sel]').forEach((x) => { x.onclick = () => this.app.select(x.dataset.sel, true); });
  }

  // ------------------------------------------------------------------ radio
  async pullEvents() {
    if (this._pulling) return;
    this._pulling = true;
    try {
      const evs = await api(`/api/events?since=${this.eventSeq}`);
      const box = $('radio');
      const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
      for (const e of evs) {
        this.eventSeq = Math.max(this.eventSeq, e.seq);
        const div = document.createElement('div');
        const who = e.speaker === 'ATC' ? (e.issuer?.startsWith('ai:') ? 'ATC·AI' : 'ATC') : e.speaker === 'PILOT' ? (e.callsign ?? 'PILOT') : e.speaker;
        div.className = `msg ${e.speaker} ${e.issuer?.startsWith('ai:') ? 'ai' : ''} ${e.level ?? ''}`;
        div.innerHTML = `<span class="t">${hhmmss(e.t)}</span><span class="who">${esc(who)}</span><span class="txt">${esc(e.text)}</span>`;
        box.appendChild(div);
      }
      while (box.childElementCount > 400) box.firstChild.remove();
      if (stick) box.scrollTop = box.scrollHeight;
    } catch { /* retry on next frame */ } finally { this._pulling = false; }
  }

  // ------------------------------------------------------------------ strip & clearances
  bindStrip() {
    $('s-close').onclick = () => this.app.select(null);
    document.querySelectorAll('[data-fl]').forEach((b) => {
      b.onclick = () => { $('c-fl').value = Math.max(0, Number($('c-fl').value || 0) + Number(b.dataset.fl)); this.updateLevelBtn(); };
    });
    $('c-fl').oninput = () => this.updateLevelBtn();
    const send = (clearances) => this.sendClearance(clearances);
    $('c-fl-go').onclick = () => send([{ kind: 'LEVEL', value: Number($('c-fl').value) }]);
    $('c-hdg-go').onclick = () => send([{ kind: 'HEADING', value: Number($('c-hdg').value) }]);
    $('c-hdg-l').onclick = () => send([{ kind: 'HEADING', value: Number($('c-hdg').value), direction: 'L' }]);
    $('c-hdg-r').onclick = () => send([{ kind: 'HEADING', value: Number($('c-hdg').value), direction: 'R' }]);
    document.querySelectorAll('[data-turn]').forEach((b) => {
      b.onclick = () => send([{ kind: 'TURN', value: Number(b.dataset.turn.slice(1)), direction: b.dataset.turn[0] }]);
    });
    $('c-dct-go').onclick = () => send([{ kind: 'DIRECT', value: $('c-dct').value.trim().split(' ')[0] }]);
    $('c-spd-go').onclick = () => send([{ kind: 'SPEED', value: Number($('c-spd').value) }]);
    $('c-ron').onclick = () => send([{ kind: 'RESUME' }]);
    for (const id of ['c-fl', 'c-hdg', 'c-dct', 'c-spd']) {
      $(id).onkeydown = (e) => { if (e.key === 'Enter') ({ 'c-fl': $('c-fl-go'), 'c-hdg': $('c-hdg-go'), 'c-dct': $('c-dct-go'), 'c-spd': $('c-spd-go') })[id].click(); };
    }
  }

  updateLevelBtn() {
    const r = this.app.store.map.get(this.app.selectedId);
    if (!r) return;
    $('c-fl-go').textContent = Number($('c-fl').value) * 100 > r.alt ? 'Climb' : 'Descend';
  }

  async sendClearance(clearances) {
    const id = this.app.selectedId;
    if (!id) return;
    const r = await this.call('/api/clearance', { aircraft: id, clearances, issuer: 'human' });
    if (r?.rejected?.length) this.toast(r.rejected.join(' · '), true);
    this.app.refreshDetail();
  }

  showStrip(rec, fresh) {
    $('right').classList.toggle('hidden', !rec);
    if (!rec) return;
    $('s-callsign').textContent = rec.cs;
    const d = this.detail;
    $('s-sub').textContent = d && d.icao24 === rec.id ? `${d.perf} · ${rec.id.toUpperCase()} · ${d.country ?? ''} · SQ ${d.squawk ?? '—'}` : rec.id.toUpperCase();
    $('s-fl').textContent = pad3(rec.pAlt / 100) + (rec.vs > 250 ? '↑' : rec.vs < -250 ? '↓' : '');
    $('s-cfl').textContent = rec.cfl != null ? pad3(rec.cfl) : '—';
    $('s-gs').textContent = rec.gs;
    $('s-hdg').textContent = pad3(rec.hdg) ;
    $('s-vs').textContent = (rec.vs > 0 ? '+' : '') + rec.vs;
    $('s-ias').textContent = d && d.icao24 === rec.id ? d.ias_kt : '—';
    $('s-lat').textContent = `NAV ${rec.lateral}${rec.spd ? ' · S' + rec.spd : ''}`;
    const ctl = rec.flags & F.HUMAN ? 'YOU' : rec.flags & F.AI ? 'AI' : rec.flags & F.SECTOR ? 'IN SECTOR' : 'UNCONTROLLED';
    $('s-ctl').textContent = ctl;
    $('s-ctl').style.color = rec.flags & F.HUMAN ? 'var(--human)' : rec.flags & F.AI ? 'var(--ai)' : 'var(--muted)';
    $('s-pending').textContent = d && d.icao24 === rec.id && d.pending.length
      ? 'Pilot executing: ' + d.pending.map((p) => `${p.kind}${p.value != null ? ' ' + p.value : ''}${p.direction ? ' ' + p.direction : ''}`).join(', ') : '';
    if (fresh) {
      $('c-fl').value = rec.cfl != null ? rec.cfl : Math.round(rec.alt / 1000) * 10;
      $('c-hdg').value = Math.round(rec.hdg) || 360;
      $('c-spd').value = rec.spd ?? '';
      $('c-dct').value = '';
      this.updateLevelBtn();
    }
  }

  setDetail(d) {
    this.detail = d;
    if (!d) return;
    const dl = $('c-dct-list');
    dl.innerHTML = d.fixes.map((f) => `<option value="${esc(f.ident)}">${esc(f.ident)} — ${esc(f.name)} (${f.kind.replace('_L', '')}, ${Math.round(f.dist_nm)} NM)</option>`).join('');
  }

  // ------------------------------------------------------------------ command line
  bindCommand() {
    $('cmd-form').onsubmit = async (e) => {
      e.preventDefault();
      const input = $('cmd');
      let text = input.value.trim().toUpperCase();
      if (!text) return;
      const first = text.split(/\s+/)[0];
      const known = this.app.store.list.some((r) => r.cs.toUpperCase() === first || r.id.toUpperCase() === first);
      const sel = this.app.store.map.get(this.app.selectedId);
      if (!known && sel) text = `${sel.cs} ${text}`;
      const r = await this.call('/api/command', { text });
      if (r) {
        input.value = '';
        if (r.rejected.length) this.toast(r.rejected.join(' · '), true);
        this.app.select(r.aircraft);
      }
    };
    window.addEventListener('keydown', (e) => {
      const typing = ['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement?.tagName);
      if (e.key === 'Escape') { document.activeElement?.blur(); this.app.select(null); $('settings').classList.add('hidden'); }
      if (typing) return;
      if (e.key === '/') { e.preventDefault(); $('cmd').focus(); }
      if (e.key === ' ') { e.preventDefault(); this.togglePause(); }
    });
  }
}
