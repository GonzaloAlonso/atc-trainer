import { REVISION } from 'three';
import { api } from './net.js';
import { F } from './traffic.js';
import { holderColor, holderName } from './holders.js';

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
    this.bindUser();
  }

  // ------------------------------------------------------------------ account
  async bindUser() {
    const pop = $('user-pop');
    $('user-btn').onclick = (e) => { e.stopPropagation(); pop.classList.toggle('hidden'); };
    document.addEventListener('click', (e) => { if (!pop.contains(e.target)) pop.classList.add('hidden'); });
    $('menu-logout').onclick = async () => {
      try { await api('/api/auth/logout', {}); } finally { location.href = '/login'; }
    };
    const dlg = $('pw-dialog');
    $('menu-password').onclick = () => {
      pop.classList.add('hidden');
      $('pw-form').reset();
      $('pw-error').textContent = '';
      dlg.showModal();
    };
    $('pw-cancel').onclick = () => dlg.close();
    $('menu-about').onclick = () => { pop.classList.add('hidden'); this.showAbout(); };
    $('version-link').onclick = () => this.showAbout();
    $('about-close').onclick = () => $('about-dialog').close();
    $('pw-form').onsubmit = async (e) => {
      e.preventDefault();
      if ($('pw-new').value !== $('pw-new2').value) { $('pw-error').textContent = 'The new passwords do not match.'; return; }
      try {
        await api('/api/auth/password', { current_password: $('pw-current').value, new_password: $('pw-new').value });
        dlg.close();
        this.toast('Password changed');
      } catch (err) {
        $('pw-error').textContent = err.message;
      }
    };
    try {
      this.me = await api('/api/auth/me');
      $('user-name').textContent = this.me.username;
      $('user-role').textContent = `Signed in as ${this.me.username} · ${this.me.role}`;
      $('menu-admin').classList.toggle('hidden', this.me.role !== 'admin');
    } catch { /* redirected to login */ }
  }

  async showAbout() {
    const a = await this.call('/api/about');
    if (!a) return;
    const kv = (rows) => rows.filter(([, v]) => v != null && v !== '')
      .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join('');
    const b = a.build;
    const commit = b.commit
      ? (b.commit_url ? `<a href="${esc(b.commit_url)}" target="_blank" rel="noopener">${esc(b.commit_short)}</a>` : esc(b.commit_short))
        + (b.modified ? ' <span class="pill warn">uncommitted changes</span>' : '')
      : 'unknown';
    $('about-desc').textContent = a.description;
    $('about-build').innerHTML = kv([
      ['Version', `<b>${esc(a.version)}</b> <span class="pill ${b.type === 'release' ? '' : 'warn'}">${esc(b.type)}</span>`],
      ['Commit', commit],
      ['Built', b.date ? esc(new Date(b.date).toUTCString()) : 'not a packaged build'],
      ['Source', b.source ? `<a href="${esc(b.source)}" target="_blank" rel="noopener">${esc(b.source.replace(/^https?:\/\//, ''))}</a>` : null],
    ]);
    const deps = Object.entries(a.runtime.dependencies).filter(([, v]) => v).map(([k, v]) => `${k} ${v}`).join(' · ');
    $('about-runtime').innerHTML = kv([
      ['Server', `${esc(a.runtime.implementation)} ${esc(a.runtime.python)} · ${esc(a.runtime.platform)}`],
      ['Libraries', esc(deps)],
      ['Client', `three.js r${esc(REVISION)}`],
      ['Up since', esc(new Date(a.runtime.started_at * 1000).toUTCString())],
    ]);
    $('about-copyright').textContent = a.copyright;
    $('about-credits').textContent = a.credits;
    $('about-third').innerHTML = a.third_party
      .map((t) => `<li><a href="${esc(t.url)}" target="_blank" rel="noopener">${esc(t.name)}</a> — ${esc(t.use)}</li>`).join('');
    $('about-dialog').showModal();
  }

  /** "human:alice" -> "alice" (or "YOU"), "ai:rules" -> "AI" */
  who(issuer, mine = 'YOU') {
    if (!issuer) return '';
    if (issuer.startsWith('ai:')) return 'AI';
    const name = issuer.startsWith('human:') ? issuer.slice(6) : issuer;
    return this.me && name === this.me.username ? mine : name;
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
    // compact header: secondary controls live in a panel behind the ⋯ button
    const extra = $('top-extra');
    const setMore = (open) => {
      extra.classList.toggle('open', open);
      $('btn-more').setAttribute('aria-expanded', String(open));
    };
    $('btn-more').onclick = (e) => { e.stopPropagation(); setMore(!extra.classList.contains('open')); };
    document.addEventListener('click', (e) => {
      // clicks in the tutorial card don't count as "outside": its lessons open this panel on purpose
      if (extra.classList.contains('open') && !extra.contains(e.target) && !$('btn-more').contains(e.target)
          && !e.target.closest('#tutorial')) setMore(false);
    });
    $('btn-lists').onclick = () => this.setLists(!document.body.classList.contains('lists-open'));
    this.setMore = setMore;
    $('speed-seg').onclick = (e) => {
      const s = e.target.dataset.speed;
      if (s) this.call('/api/sim', { action: 'speed', speed: Number(s) });
    };
    $('scenario').onchange = (e) => {
      const v = e.target.value;
      const body = v === 'live' ? { action: 'reset', mode: 'live' } : { action: 'reset', mode: 'replay', start: Number(v) };
      this.call('/api/sim', body).then((r) => r && this.toast(v === 'live' ? 'Live traffic' : `Replay from ${hhmmss(Number(v))} UTC`));
    };
    $('my-sectors').onclick = (e) => { e.stopPropagation(); this.setMore(false); this.openTab('sectors'); };
    $('ai-mode').onchange = () => this.setAi();
    $('ai-agent').onchange = () => this.setAi();
    $('btn-settings').onclick = (e) => {
      e.stopPropagation();
      this.setMore(false);
      $('settings').classList.toggle('hidden');
    };
    $('style-seg').onclick = (e) => { const s = e.target.dataset.style; if (s) this.app.setStyle(s); };
  }

  /** Switching between live traffic and the training sandbox: forget the other feed's state. */
  resetForContext(message) {
    this.eventSeq = 0;
    this.sigs = {};
    this.detail = null;
    this.frame = null;
    $('radio').innerHTML = '';
    if (message) {
      const div = document.createElement('div');
      div.className = 'msg SYSTEM';
      div.innerHTML = `<span class="who">SYSTEM</span><span class="txt">${esc(message)}</span>`;
      $('radio').appendChild(div);
    }
    this.setMore?.(false);
    this.setLists(false);
  }

  /** Show a left-panel tab (on phones the lists live in a sheet behind ☰). */
  openTab(name) {
    document.querySelector(`.tabs [data-tab=${name}]`)?.click();
    if (window.matchMedia('(max-width: 900px)').matches) this.setLists(true);
  }

  setLists(open) {
    document.body.classList.toggle('lists-open', open);
    $('btn-lists').setAttribute('aria-expanded', String(open));
  }

  async setAi() {
    const r = await this.call('/api/ai', { mode: $('ai-mode').value, agent: $('ai-agent').value });
    if (r && !r.agent.available) this.toast(`Agent "${r.agent.name}" is not configured (see README)`, true);
  }

  togglePause() {
    if (!this.frame) return;
    this.call('/api/sim', { action: this.frame.paused ? 'resume' : 'pause' });
  }

  /** Static sectorization catalogue from /api/sectors: regions × vertical layers. */
  setSectors(catalogue) {
    this.catalogue = catalogue;
    this.sectorById = new Map(catalogue.sectors.map((s) => [s.id, s]));
  }

  sectorName(sid) { return this.sectorById?.get(sid)?.name ?? sid; }

  async sectorAction(action, sid) {
    const path = { take: 'take', release: 'release', ai: 'assign-ai', force: 'take' }[action];
    const body = action === 'force' ? { force: true } : action === 'ai' ? { agent: $('ai-agent').value || 'rules' } : {};
    const r = await this.call(`/api/sectors/${sid}/${path}`, body);
    if (r) {
      this.toast({ take: 'You now control', force: 'You took over', release: 'Released', ai: 'AI now controls' }[action]
        + ` ${this.sectorName(sid)}`);
      this.sigs.sectors = null;
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
    $('version-link').textContent = `ATC Trainer ${st.version}`;
    $('rec-status').textContent = `· ${st.recording ? '●' : '○ not'} recording OpenSky (${rec.authenticated ? 'authenticated' : 'anonymous'}, every ${Math.round(rec.interval_s)} s) · ${span} · next poll in ${Math.floor(next / 60)}:${String(Math.floor(next % 60)).padStart(2, '0')}${rec.credits_left != null ? ` · ${rec.credits_left} credits left` : ''}${rec.last_error ? ' · ⚠ ' + rec.last_error : ''}`;
  }

  // ------------------------------------------------------------------ frames
  onFrame(frame) {
    this.frame = frame;
    const badge = $('mode-badge');
    const training = document.body.classList.contains('training');
    badge.textContent = training ? 'TRAINING' : frame.lockstep ? 'LOCKSTEP' : frame.mode.toUpperCase();
    badge.className = 'badge' + (training ? ' training' : frame.mode === 'replay' ? ' replay' : '');
    // the shared simulation's clock and scenario are admin-only; a trainee's sandbox is theirs
    const simLocked = !training && this.me?.role !== 'admin';
    const lockTip = simLocked ? 'Only admins can change the shared simulation' : '';
    $('scenario').disabled = training || simLocked;
    $('scenario').title = training ? 'The training sector runs its own scripted scenario' : lockTip;
    $('btn-pause').textContent = frame.paused ? '▶' : '❚❚';
    $('btn-pause').disabled = simLocked;
    $('btn-pause').title = simLocked ? lockTip : 'Pause / resume (Space)';
    for (const b of $('speed-seg').children) {
      b.classList.toggle('on', Number(b.dataset.speed) === frame.speed);
      b.disabled = simLocked || (frame.mode === 'live' && b.dataset.speed !== '1');
      b.title = lockTip;
    }
    if (document.activeElement !== $('ai-mode')) $('ai-mode').value = frame.ai.mode;

    const mySectors = frame.me?.sectors ?? [];
    $('my-sectors-text').textContent = mySectors.length ? mySectors.join(' + ') : 'No sector';
    $('my-sectors').classList.toggle('none', !mySectors.length);

    const s = frame.score;
    $('score').innerHTML = `<span class="pts" title="your score">${s.points ?? 0}</span>` +
      `<span>LoS <b>${s.los ?? 0}</b></span><span>STCA <b>${s.stca ?? 0}</b></span>` +
      `<span>REQ <b>${s.requests_granted ?? 0}</b>/<b>${s.requests_expired ?? 0}</b></span>` +
      `<span>HND <b>${s.handled ?? 0}</b></span>` +
      `<span title="clearances you issued">CLR <b style="color:var(--human)">${s.clearances ?? 0}</b></span>`;

    if (frame.event_seq > this.eventSeq) this.pullEvents();
    const now = performance.now();
    if (now - this.lastListRender > 900) {
      this.lastListRender = now;
      this.renderAlerts();
      this.renderDecisions();
      this.renderTraffic();
      this.renderSectors();
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
    const conf = [...f.conflicts].sort((a, b) => (a[2] === 'LOS' ? -1 : 0) - (b[2] === 'LOS' ? -1 : 0) || a[3] - b[3]);
    const reqs = f.decisions.filter((d) => d.status === 'open' && d.kind !== 'conflict');
    const sig = JSON.stringify([conf.map((c) => [c[0], c[1], c[2], Math.round(c[3] / 10)]), reqs.map((r) => r.id)]);
    const n = $('n-alerts');
    n.textContent = conf.length + reqs.length;
    n.classList.toggle('hot', conf.some((c) => c[2] === 'LOS'));
    const lc = $('lists-count');
    lc.textContent = conf.length + reqs.length || '';
    lc.classList.toggle('hot', conf.some((c) => c[2] === 'LOS'));
    if (sig === this.sigs.alerts) return;
    this.sigs.alerts = sig;
    const el = $('tab-alerts');
    if (!conf.length && !reqs.length) {
      el.innerHTML = f.me?.sectors?.length
        ? `<div class="empty">No alerts in your sectors.<br>STCA warns ${'≤'}2 min ahead; separation minima 5 NM / 1000 ft (3 NM below FL100).</div>`
        : '<div class="empty">You are not controlling any sector.<br>Take one in the <b>Sectors</b> tab: its alerts and pilot requests will appear here.</div>';
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
      el.innerHTML = `<div class="empty">Decision points for your sectors appear here: conflicts to resolve and pilot requests.<br>Each option shows the outcome of a fast-time prediction. Turn on <b>AI</b> to get advice or let it act.${f.me?.sectors?.length ? '' : '<br><br>Take a sector in the <b>Sectors</b> tab first.'}</div>`;
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
      const done = d.status !== 'open' ? `<div class="meta">${d.status}${d.answered_by ? ' by ' + esc(this.who(d.answered_by, 'you')) : ''}${d.answer?.action ? ' → ' + esc(q.options.find((o) => o.id === d.answer.action)?.label ?? '') : ''}</div>` : '';
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
    const recs = this.app.store.list.filter((r) => r.flags & F.SECTOR);
    recs.sort((a, b) => b.alt - a.alt);
    $('n-traffic').textContent = recs.length;
    const sig = JSON.stringify(recs.map((r) => [r.id, Math.round(r.alt / 100), r.cfl, r.lateral, r.flags]));
    if (sig === this.sigs.traffic) return;
    this.sigs.traffic = sig;
    const el = $('tab-traffic');
    if (!recs.length) {
      el.innerHTML = `<div class="empty">${f.me?.sectors?.length ? 'No traffic in your sectors right now.' : 'Take a sector in the <b>Sectors</b> tab to see the traffic you are responsible for.'}</div>`;
      return;
    }
    el.innerHTML = `<table class="traffic"><thead><tr><th>CS</th><th>FL</th><th>CFL</th><th>GS</th><th>NAV</th></tr></thead><tbody>${recs.map((r) => {
      const col = r.flags & F.LOS ? 'var(--alert)' : r.flags & F.STCA ? 'var(--warn)' : r.flags & F.HUMAN ? 'var(--human)' : r.flags & F.AI ? 'var(--ai)' : 'var(--text)';
      return `<tr data-sel="${r.id}"><td style="color:${col}">${esc(r.cs)}</td><td>${pad3(r.alt / 100)}${r.vs > 250 ? '↑' : r.vs < -250 ? '↓' : ''}</td><td>${r.cfl != null ? pad3(r.cfl) : ''}</td><td>${r.gs}</td><td>${esc(r.lateral)}</td></tr>`;
    }).join('')}</tbody></table>`;
    el.querySelectorAll('[data-sel]').forEach((x) => { x.onclick = () => this.app.select(x.dataset.sel, true); });
  }

  /** Every sector, grouped by region with its vertical layers stacked (High on top). */
  renderSectors() {
    const f = this.frame;
    if (!this.catalogue || !f.sectorization) return;
    const me = f.me?.holder;
    const rows = new Map(f.sectorization.map(([sid, h, n]) => [sid, { h, n }]));
    const mine = f.me?.sectors ?? [];
    $('n-sectors').textContent = mine.length;
    const sig = JSON.stringify([f.sectorization, me]);
    if (sig === this.sigs.sectors) return;
    this.sigs.sectors = sig;
    const admin = this.me?.role === 'admin';
    const layers = [...this.catalogue.layers].reverse();
    const fl = (x) => (x >= 600 ? 'UNL' : pad3(x));
    const html = this.catalogue.regions.map((reg) => `
      <div class="region">
        <div class="region-head" data-region="${reg.id}"><b>${esc(reg.name)}</b><span class="dim">${reg.id}</span></div>
        ${layers.map((l) => {
    const sid = `${reg.id}-${l.id}`;
    const { h, n } = rows.get(sid) ?? { h: null, n: 0 };
    const isMine = h && h === me;
    const isAi = h?.startsWith('ai:');
    const other = h && !isMine;
    const btns = [];
    if (!h) btns.push(`<button class="mini" data-sact="take" data-sid="${sid}">Take</button>`);
    if (isMine) btns.push(`<button class="mini ghost" data-sact="release" data-sid="${sid}">Release</button>`);
    if (!h || isMine) btns.push(`<button class="mini ai" data-sact="ai" data-sid="${sid}" title="Let the AI control this sector">AI</button>`);
    if (isAi) btns.push(`<button class="mini" data-sact="take" data-sid="${sid}" title="Take over from the AI">Take</button>`);
    if (other && admin && !isAi) btns.push(`<button class="mini ghost" data-sact="force" data-sid="${sid}" title="Admin: take this sector over">Take</button>`);
    if (other && admin) btns.push(`<button class="mini ghost" data-sact="release" data-sid="${sid}" title="Admin: release this sector">Free</button>`);
    return `<div class="layer ${isMine ? 'mine' : ''}" style="--hc:${holderColor(h, me)}">
            <span class="lay">${l.id}</span>
            <span class="band">FL${fl(l.fl_min)}–${fl(l.fl_max)}</span>
            <span class="holder">${h ? esc(holderName(h, me)) : '<span class="dim">free</span>'}</span>
            <span class="count" title="aircraft in this sector">${n}</span>
            <span class="acts">${btns.join('')}</span>
          </div>`;
  }).join('')}
      </div>`).join('');
    const el = $('tab-sectors');
    el.innerHTML = `<p class="hint sector-hint">Take the sectors you want to control; you can combine several.
      Aircraft inside them are yours: only you can clear them and only you see their alerts and requests.</p>${html}`;
    el.querySelectorAll('[data-sact]').forEach((b) => { b.onclick = () => this.sectorAction(b.dataset.sact, b.dataset.sid); });
    el.querySelectorAll('[data-region]').forEach((x) => {
      x.onclick = () => {
        const reg = this.catalogue.regions.find((r) => r.id === x.dataset.region);
        const lat = reg.poly.reduce((a, p) => a + p[0], 0) / reg.poly.length;
        const lon = reg.poly.reduce((a, p) => a + p[1], 0) / reg.poly.length;
        this.app.flyTo(lat, lon, 1100);
      };
    });
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
        const who = e.speaker === 'ATC' ? `ATC·${this.who(e.issuer, 'you')}` : e.speaker === 'PILOT' ? (e.callsign ?? 'PILOT') : e.speaker;
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
    const ctlName = d && d.icao24 === rec.id && d.controller ? this.who(d.controller).toUpperCase() : null;
    const ctl = ctlName ?? (rec.flags & F.HUMAN ? 'CONTROLLED' : rec.flags & F.AI ? 'AI' : rec.flags & F.SECTOR ? 'IN SECTOR' : 'UNCONTROLLED');
    $('s-ctl').textContent = ctl;
    $('s-ctl').style.color = rec.flags & F.HUMAN ? 'var(--human)' : rec.flags & F.AI ? 'var(--ai)' : 'var(--muted)';
    // responsibility: only the holder of the aircraft's sector may clear it
    const auth = $('s-authority');
    const me = this.frame?.me?.holder;
    const holder = rec.sec ? this.frame?.sectorization?.find((r) => r[0] === rec.sec)?.[1] : null;
    const locked = !!holder && holder !== me;
    auth.className = 'authority ' + (locked ? 'locked' : holder ? 'mine' : 'free');
    auth.style.setProperty('--hc', holderColor(holder, me));
    auth.innerHTML = !rec.sec ? 'Outside the sectorization · anyone may clear it'
      : locked ? `In <b>${esc(this.sectorName(rec.sec))}</b> · controlled by <b>${esc(holderName(holder, me))}</b> · read only`
        : holder ? `In your sector <b>${esc(this.sectorName(rec.sec))}</b>`
          : `In <b>${esc(this.sectorName(rec.sec))}</b> · unmanned · anyone may clear it`;
    document.querySelector('#right .ctl').classList.toggle('locked', locked);
    document.querySelectorAll('#right .ctl button, #right .ctl input').forEach((x) => { x.disabled = locked; });
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
