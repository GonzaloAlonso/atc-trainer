import { REVISION } from 'three';
import { api, getContext } from './net.js';
import { F } from './traffic.js';
import { holderColor, holderName } from './holders.js';
import { t, tm, languageSelect } from './i18n.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const pad3 = (n) => String(Math.round(n)).padStart(3, '0');
const hhmmss = (t_) => new Date(t_ * 1000).toISOString().slice(11, 19);

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
    this.whatifMode = false;
    this.hints = new Map();          // decision id -> last hint shown
    this.bindTop();
    this.bindTabs();
    this.bindStrip();
    this.bindCommand();
    this.bindUser();
  }

  get level() { return this.frame?.coach?.level ?? 'off'; }

  // ------------------------------------------------------------------ account
  async bindUser() {
    const pop = $('user-pop');
    $('user-btn').onclick = (e) => { e.stopPropagation(); pop.classList.toggle('hidden'); };
    document.addEventListener('click', (e) => { if (!pop.contains(e.target)) pop.classList.add('hidden'); });
    languageSelect($('menu-lang'));
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
      if ($('pw-new').value !== $('pw-new2').value) { $('pw-error').textContent = t('pw.mismatch'); return; }
      try {
        await api('/api/auth/password', { current_password: $('pw-current').value, new_password: $('pw-new').value });
        dlg.close();
        this.toast(t('pw.changed'));
      } catch (err) {
        $('pw-error').textContent = err.message;
      }
    };
    if (this.me) this.showMe();
  }

  setMe(me) {
    this.me = me;
    if ($('user-name')) this.showMe();
  }

  showMe() {
    $('user-name').textContent = this.me.username;
    $('user-role').textContent = t('menu.signedIn', { user: this.me.username, role: t(`roles.${this.me.role}`) });
    $('menu-admin').classList.toggle('hidden', this.me.role !== 'admin');
  }

  async showAbout() {
    const a = await this.call('/api/about');
    if (!a) return;
    const kv = (rows) => rows.filter(([, v]) => v != null && v !== '')
      .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join('');
    const b = a.build;
    const commit = b.commit
      ? (b.commit_url ? `<a href="${esc(b.commit_url)}" target="_blank" rel="noopener">${esc(b.commit_short)}</a>` : esc(b.commit_short))
        + (b.modified ? ` <span class="pill warn">${esc(t('about.uncommitted'))}</span>` : '')
      : t('about.unknown');
    $('about-desc').textContent = a.description;
    $('about-build').innerHTML = kv([
      [t('about.version'), `<b>${esc(a.version)}</b> <span class="pill ${b.type === 'release' ? '' : 'warn'}">${esc(b.type)}</span>`],
      [t('about.commit'), commit],
      [t('about.built'), b.date ? esc(new Date(b.date).toUTCString()) : esc(t('about.notPackaged'))],
      [t('about.source'), b.source ? `<a href="${esc(b.source)}" target="_blank" rel="noopener">${esc(b.source.replace(/^https?:\/\//, ''))}</a>` : null],
    ]);
    const deps = Object.entries(a.runtime.dependencies).filter(([, v]) => v).map(([k, v]) => `${k} ${v}`).join(' · ');
    $('about-runtime').innerHTML = kv([
      [t('about.server'), `${esc(a.runtime.implementation)} ${esc(a.runtime.python)} · ${esc(a.runtime.platform)}`],
      [t('about.libraries'), esc(deps)],
      [t('about.client'), `three.js r${esc(REVISION)}`],
      [t('about.upSince'), esc(new Date(a.runtime.started_at * 1000).toUTCString())],
    ]);
    $('about-copyright').textContent = a.copyright;
    $('about-credits').textContent = a.credits;
    $('about-third').innerHTML = a.third_party
      .map((x) => `<li><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.name)}</a> — ${esc(x.use)}</li>`).join('');
    $('about-dialog').showModal();
  }

  /** "human:alice" -> "alice" (or "YOU"), "ai:rules" -> "AI" */
  who(issuer, mine = t('common.youCaps')) {
    if (!issuer) return '';
    if (issuer.startsWith('ai:')) return 'AI';
    const name = issuer.startsWith('human:') ? issuer.slice(6) : issuer;
    return this.me && name === this.me.username ? mine : name;
  }

  toast(msg, err = false) {
    const el = $('toast');
    el.textContent = msg;
    el.className = err ? 'err' : '';
    clearTimeout(this._toastT);
    this._toastT = setTimeout(() => el.classList.add('hidden'), 3500);
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
      this.call('/api/sim', body).then((r) => r && this.toast(v === 'live' ? t('top.liveTraffic') : t('top.replayFrom', { time: hhmmss(Number(v)) })));
    };
    $('my-sectors').onclick = (e) => { e.stopPropagation(); this.setMore(false); this.openTab('sectors'); };
    $('coach-level').onchange = () => this.setCoach($('coach-level').value);
    $('ai-agent').onchange = () => this.setAgent();
    $('btn-settings').onclick = (e) => {
      e.stopPropagation();
      this.setMore(false);
      $('settings').classList.toggle('hidden');
    };
    $('style-seg').onclick = (e) => { const s = e.target.dataset.style; if (s) this.app.setStyle(s); };
  }

  async setCoach(level) {
    const r = await this.call('/api/coach', { level });
    if (r) {
      this.toast(t('coachUi.levelSet', { level: t(`levels.${level}`) }));
      this.sigs = {};
    }
  }

  /** Switching between live traffic and a sandbox: forget the other feed's state. */
  resetForContext(message) {
    this.eventSeq = 0;
    this.sigs = {};
    this.detail = null;
    this.frame = null;
    this.hints.clear();
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

  async setAgent() {
    const r = await this.call('/api/ai', { agent: $('ai-agent').value });
    if (r && !r.agent.available) this.toast(t('coachUi.agentMissing', { agent: r.agent.name }), true);
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
      this.toast(t(`sectors.did.${action}`, { sector: this.sectorName(sid) }));
      this.sigs.sectors = null;
    }
  }

  setStatus(st) {
    // The server was upgraded while this page was open: offer a reload instead of running
    // an old UI against a new API.
    this.loadedVersion ??= st.version;
    if (st.version !== this.loadedVersion && !this._upgradeShown) {
      this._upgradeShown = true;
      $('upgrade-text').textContent = t('upgrade.available', { version: st.version, loaded: this.loadedVersion });
      $('upgrade').classList.remove('hidden');
    }
    this.languageCoach = st.coach;
    const rec = st.recorder;
    const cov = rec.coverage;
    const ai = $('ai-agent');
    if (!ai.options.length) {
      for (const a of st.ai.available_agents) {
        const o = document.createElement('option'); o.value = a; o.textContent = a; ai.appendChild(o);
      }
    }
    ai.value = st.ai.agent.name;

    // scenario options depend on how much history has been recorded
    const sel = $('scenario');
    const cur = this.frame?.mode === 'live' ? 'live' : sel.value;
    sel.innerHTML = '';
    const add = (v, label) => { const o = document.createElement('option'); o.value = v; o.textContent = label; sel.appendChild(o); };
    add('live', t('top.live'));
    if (cov.first) {
      const span = (cov.last - cov.first) / 3600;
      add(String(cov.first), t('top.replayStart', { time: hhmmss(cov.first), hours: span.toFixed(1) }));
      for (const h of [1, 3, 6, 12]) if (span > h + 0.25) add(String(Math.round(cov.last - h * 3600)), t('top.replayLast', { hours: h }));
    }
    sel.value = [...sel.options].some((o) => o.value === cur) ? cur : (this.frame?.mode === 'live' ? 'live' : sel.options[1]?.value ?? 'live');

    const next = Math.max(0, rec.next_poll - st.now);
    const span = cov.first ? t('status.span', { n: cov.snapshots, from: hhmmss(cov.first), to: hhmmss(cov.last) }) : t('status.noData');
    $('version-link').textContent = `ATC Trainer ${st.version}`;
    $('rec-status').textContent = '· ' + t(st.recording ? 'status.recording' : 'status.notRecording', {
      mode: t(rec.authenticated ? 'status.authenticated' : 'status.anonymous'), every: Math.round(rec.interval_s),
    }) + ` · ${span} · ` + t('status.nextPoll', { time: `${Math.floor(next / 60)}:${String(Math.floor(next % 60)).padStart(2, '0')}` })
      + (rec.credits_left != null ? ' · ' + t('status.credits', { n: rec.credits_left }) : '')
      + (rec.last_error ? ' · ⚠ ' + rec.last_error : '');
  }

  // ------------------------------------------------------------------ frames
  onFrame(frame) {
    this.frame = frame;
    const badge = $('mode-badge');
    const ctx = getContext();
    const sandbox = !!ctx;
    const key = ctx === 'exercise' ? 'exercise' : ctx === 'tutorial' ? 'training' : frame.lockstep ? 'lockstep' : frame.mode;
    badge.textContent = t(`badge.${key}`);
    badge.className = 'badge' + (sandbox ? ' training' : frame.mode === 'replay' ? ' replay' : '');
    // the shared simulation's clock and scenario are admin-only; a trainee's sandbox is theirs
    const simLocked = !sandbox && this.me?.role !== 'admin';
    const lockTip = simLocked ? t('top.adminOnly') : '';
    $('scenario').disabled = sandbox || simLocked;
    $('scenario').title = sandbox ? t('top.sandboxScenario') : lockTip;
    $('btn-pause').textContent = frame.paused ? '▶' : '❚❚';
    $('btn-pause').disabled = simLocked;
    $('btn-pause').title = simLocked ? lockTip : t('top.pauseTip');
    for (const b of $('speed-seg').children) {
      b.classList.toggle('on', Number(b.dataset.speed) === frame.speed);
      b.disabled = simLocked || (frame.mode === 'live' && b.dataset.speed !== '1');
      b.title = lockTip;
    }
    if (document.activeElement !== $('coach-level') && frame.coach) $('coach-level').value = frame.coach.level;
    document.body.dataset.coach = this.level;

    const mySectors = frame.me?.sectors ?? [];
    $('my-sectors-text').textContent = mySectors.length ? mySectors.join(' + ') : t('top.noSector');
    $('my-sectors').classList.toggle('none', !mySectors.length);

    const s = frame.score;
    $('score').innerHTML = `<span class="pts" title="${esc(t('score.points'))}">${s.points ?? 0}</span>` +
      `<span>LoS <b>${s.los ?? 0}</b></span><span>STCA <b>${s.stca ?? 0}</b></span>` +
      `<span title="${esc(t('score.requests'))}">REQ <b>${s.requests_granted ?? 0}</b>/<b>${s.requests_expired ?? 0}</b></span>` +
      `<span title="${esc(t('score.handled'))}">HND <b>${s.handled ?? 0}</b></span>` +
      `<span title="${esc(t('score.clearances'))}">CLR <b style="color:var(--human)">${s.clearances ?? 0}</b></span>`;

    for (const o of frame.coach?.open ?? []) if (o.hint) this.hints.set(o.dp, o);
    if (frame.event_seq > this.eventSeq) this.pullEvents();
    const now = performance.now();
    if (now - this.lastListRender > 900) {
      this.lastListRender = now;
      this.renderAlerts();
      this.renderDecisions();
      this.renderTraffic();
      this.renderSectors();
    }
    this.app.coach?.onFrame(frame);
  }

  tickClock(simT) {
    if (!simT) return;
    const d = new Date(simT * 1000);
    $('clock').textContent = d.toISOString().slice(11, 19);
    $('date').textContent = d.toISOString().slice(0, 10) + ' UTC';
  }

  // ------------------------------------------------------------------ lists
  bindTabs() {
    document.querySelector('#left .tabs').onclick = (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      for (const x of document.querySelectorAll('#left .tabs button')) x.classList.toggle('on', x === b);
      for (const x of document.querySelectorAll('#left .tab')) x.classList.toggle('on', x.id === 'tab-' + b.dataset.tab);
    };
  }

  cs(id) { return this.app.store.map.get(id)?.cs ?? id; }

  /** Hint line under an alert card, and the Hint button (at the Hints level). */
  hintHtml(dpId) {
    if (!dpId) return '';
    const h = this.hints.get(dpId);
    const text = h?.hint ? `<div class="hint-line tier${h.tier}"><span class="bulb">💡</span>${esc(tm(h.hint))}</div>` : '';
    const can = this.level === 'hints' && (h?.tier ?? 0) < 3;
    const btn = can ? `<button class="mini hint-btn" data-hint="${dpId}" title="${esc(t('coachUi.hintTip'))}">💡 ${esc(t('coachUi.hint'))}${h?.tier ? ` ${h.tier + 1}/3` : ''}</button>` : '';
    return text + (btn ? `<div class="hint-row">${btn}</div>` : '');
  }

  renderAlerts() {
    const f = this.frame;
    const conf = [...f.conflicts].sort((a, b) => (a[2] === 'LOS' ? -1 : 0) - (b[2] === 'LOS' ? -1 : 0) || a[3] - b[3]);
    const reqs = f.decisions.filter((d) => d.status === 'open' && d.kind !== 'conflict');
    const dpOf = new Map(f.decisions.filter((d) => d.status === 'open' && d.kind === 'conflict')
      .map((d) => [d.subjects.slice().sort().join(','), d.id]));
    const sig = JSON.stringify([conf.map((c) => [c[0], c[1], c[2], Math.round(c[3] / 10)]), reqs.map((r) => r.id),
      [...this.hints.values()].map((h) => [h.dp, h.tier]), this.level, [...dpOf.values()]]);
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
        ? `<div class="empty">${t('alerts.none')}</div>`
        : `<div class="empty">${t('alerts.noSector')}</div>`;
      return;
    }
    el.innerHTML = conf.map(([a, b, kind, tTo, h, v]) => {
      const dp = dpOf.get([a, b].slice().sort().join(','));
      return `
      <div class="card ${kind === 'LOS' ? 'los' : 'stca'}" data-sel="${a}">
        <div class="card-head"><span class="k" style="color:${kind === 'LOS' ? 'var(--alert)' : 'var(--warn)'}">${kind === 'LOS' ? t('alerts.los') : 'STCA'}</span><span>${kind === 'LOS' ? t('alerts.now') : Math.round(tTo) + ' s'}</span></div>
        <div class="card-head" style="margin-top:4px"><span data-sel="${a}">${esc(this.cs(a))}</span><span>↔</span><span data-sel="${b}">${esc(this.cs(b))}</span></div>
        <div class="meta">${t('alerts.atClosest', { h: h.toFixed(1), v })}</div>
        ${this.hintHtml(dp)}
      </div>`;
    }).join('') + reqs.map((d) => `
      <div class="card req" data-sel="${d.subjects[0]}" data-tab-to="decisions">
        <div class="card-head"><span class="k" style="color:var(--accent)">${t('alerts.request')}</span><span>${d.id}</span></div>
        <div class="prompt">${esc(this.decisionPrompt(d))}</div>
        ${this.hintHtml(d.id)}
      </div>`).join('');
    el.querySelectorAll('[data-sel]').forEach((x) => { x.onclick = (e) => { e.stopPropagation(); this.app.select(x.dataset.sel, true); }; });
    el.querySelectorAll('[data-hint]').forEach((b) => { b.onclick = (e) => { e.stopPropagation(); this.app.coach?.hint(b.dataset.hint); }; });
  }

  /** The decision's question in the user's language (the server's prompt is English, for agents). */
  decisionPrompt(d) {
    const cs = d.subjects.map((i) => this.cs(i));
    if (d.kind === 'conflict') return t('dec.promptConflict', { a: cs[0], b: cs[1] });
    if (d.kind === 'level_request') {
      const r = d.state.request;
      return t(r.type === 'climb' ? 'dec.promptClimb' : 'dec.promptDescend', { a: cs[0], fl: pad3(r.requested_fl) });
    }
    return t('dec.promptRoute', { a: cs[0] });
  }

  renderDecisions() {
    const f = this.frame;
    const level = this.level;
    const ds = [...f.decisions].sort((a, b) => (a.status === 'open' ? 0 : 1) - (b.status === 'open' ? 0 : 1) || b.created_t - a.created_t);
    const open = ds.filter((d) => d.status === 'open').length;
    $('n-dec').textContent = open;
    const sig = JSON.stringify([ds.map((d) => [d.id, d.status, d.updated_t, !!d.suggestion, d.answered_by, d.questions[0].options.length]), level]);
    if (sig === this.sigs.dec) return;
    this.sigs.dec = sig;
    const el = $('tab-decisions');
    if (!ds.length) {
      el.innerHTML = `<div class="empty">${t('dec.empty')}${f.me?.sectors?.length ? '' : '<br><br>' + t('dec.takeSector')}</div>`;
      return;
    }
    const explain = level === 'advise' || level === 'demonstrate';
    el.innerHTML = ds.map((d) => {
      const q = d.questions[0];
      const sug = d.suggestion?.action;
      const kindLabel = t(`dec.kind.${d.kind}`);
      const color = d.kind === 'conflict' ? 'var(--warn)' : 'var(--accent)';
      const opts = [...q.options].sort((a, b) => a.predicted.los_duration_s - b.predicted.los_duration_s || a.cost - b.cost).slice(0, 6);
      const optHtml = d.status !== 'open' ? '' : opts.map((o) => {
        const p = o.predicted;
        const ok = p.los_duration_s === 0;
        const pred = ok ? (p.min_h_nm != null ? t('dec.minH', { h: p.min_h_nm }) : t('dec.clear')) : t('dec.losFor', { s: p.los_duration_s });
        const why = explain && o.why ? `<div class="why">${esc(tm(o.why))}</div>` : '';
        return `<div class="opt ${o.id === sug ? 'suggested' : ''} ${o.id === d.best && explain ? 'best' : ''}" data-wi-dec="${d.id}" data-wi-opt="${o.id}">
          <span class="${ok ? 'ok' : 'bad'}">${ok ? '✓' : '✗'}</span>
          <span>${esc(o.label)} <span class="pred">${pred}</span>${why}</span>
          <button class="mini" data-dec="${d.id}" data-opt="${o.id}">${t('dec.issue')}</button></div>`;
      }).join('');
      const hidden = d.status === 'open' && d.hidden_options
        ? `<div class="hidden-opts">${t(level === 'evaluate' ? 'dec.hiddenEvaluate' : 'dec.hiddenHints', { n: d.hidden_options })}</div>` : '';
      const aiLine = d.status === 'open' && d.suggestion ? `<div class="ai-line"><span>${t('dec.aiSays', { agent: esc(d.suggestion.agent), option: esc(q.options.find((o) => o.id === sug)?.label ?? sug), pct: Math.round((d.suggestion.confidence ?? 0) * 100) })}</span>
          <button class="mini ai" data-dec="${d.id}" data-opt="${sug}" data-by="ai:${esc(d.suggestion.agent)}">${t('dec.accept')}</button></div>` : '';
      const doneOpt = d.answer?.action ? q.options.find((o) => o.id === d.answer.action)?.label : null;
      const done = d.status !== 'open' ? `<div class="meta">${t(`dec.status.${d.status}`)}${d.answered_by ? ' · ' + esc(this.who(d.answered_by, t('common.you'))) : ''}${doneOpt ? ' → ' + esc(doneOpt) : ''}</div>` : '';
      return `<div class="card ${d.kind === 'conflict' ? 'stca' : 'req'}" style="${d.status !== 'open' ? 'opacity:.55' : ''}">
        <div class="card-head"><span class="k" style="color:${color}">${kindLabel}</span><span class="status-pill">${d.id} · ${t(`dec.status.${d.status}`)}</span></div>
        <div class="prompt">${esc(this.decisionPrompt(d))}</div>${hidden}${optHtml}${aiLine}${done}
        ${d.status === 'open' ? this.hintHtml(d.id) : ''}
        ${d.status === 'open' ? `<div style="text-align:right;margin-top:4px"><button class="mini ghost" data-dismiss="${d.id}">${t('dec.dismiss')}</button></div>` : ''}
      </div>`;
    }).join('');
    el.querySelectorAll('[data-dec]').forEach((b) => {
      b.onclick = async (e) => {
        e.stopPropagation();
        const r = await this.call(`/api/decisions/${b.dataset.dec}`, { answers: { action: b.dataset.opt }, by: b.dataset.by || 'human' });
        if (r) this.toast(`${r.decision}: ${r.option}`);
      };
    });
    el.querySelectorAll('[data-dismiss]').forEach((b) => { b.onclick = () => this.call(`/api/decisions/${b.dataset.dismiss}/dismiss`, {}); });
    el.querySelectorAll('[data-hint]').forEach((b) => { b.onclick = (e) => { e.stopPropagation(); this.app.coach?.hint(b.dataset.hint); }; });
    // hover (or tap) an option: its future in 3D
    el.querySelectorAll('[data-wi-opt]').forEach((row) => {
      row.onmouseenter = () => this.app.whatif?.showOption(row.dataset.wiDec, row.dataset.wiOpt);
      row.onmouseleave = () => this.app.whatif?.hoverEnd();
      row.onclick = () => this.app.whatif?.showOption(row.dataset.wiDec, row.dataset.wiOpt, true);
    });
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
      el.innerHTML = `<div class="empty">${f.me?.sectors?.length ? t('traffic.none') : t('traffic.noSector')}</div>`;
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
    if (!h) btns.push(`<button class="mini" data-sact="take" data-sid="${sid}">${t('sectors.take')}</button>`);
    if (isMine) btns.push(`<button class="mini ghost" data-sact="release" data-sid="${sid}">${t('sectors.release')}</button>`);
    if (!h || isMine) btns.push(`<button class="mini ai" data-sact="ai" data-sid="${sid}" title="${esc(t('sectors.aiTip'))}">AI</button>`);
    if (isAi) btns.push(`<button class="mini" data-sact="take" data-sid="${sid}" title="${esc(t('sectors.takeOverTip'))}">${t('sectors.take')}</button>`);
    if (other && admin && !isAi) btns.push(`<button class="mini ghost" data-sact="force" data-sid="${sid}" title="${esc(t('sectors.forceTip'))}">${t('sectors.take')}</button>`);
    if (other && admin) btns.push(`<button class="mini ghost" data-sact="release" data-sid="${sid}" title="${esc(t('sectors.freeTip'))}">${t('sectors.free')}</button>`);
    return `<div class="layer ${isMine ? 'mine' : ''}" style="--hc:${holderColor(h, me)}">
            <span class="lay">${l.id}</span>
            <span class="band">FL${fl(l.fl_min)}–${fl(l.fl_max)}</span>
            <span class="holder">${h ? esc(holderName(h, me)) : `<span class="dim">${t('sectors.freeState')}</span>`}</span>
            <span class="count" title="${esc(t('sectors.countTip'))}">${n}</span>
            <span class="acts">${btns.join('')}</span>
          </div>`;
  }).join('')}
      </div>`).join('');
    const el = $('tab-sectors');
    el.innerHTML = `<p class="hint sector-hint">${t('sectors.hint')}</p>${html}`;
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
        const who = e.speaker === 'ATC' ? `ATC·${this.who(e.issuer, t('common.you'))}` : e.speaker === 'PILOT' ? (e.callsign ?? 'PILOT') : e.speaker;
        div.className = `msg ${e.speaker} ${e.issuer?.startsWith('ai:') ? 'ai' : ''} ${e.level ?? ''}`;
        const text = e.msg ? tm(e.msg) : e.text;
        const grade = e.grade ? `<span class="grade-chip g${e.grade}">${e.grade}</span>` : '';
        div.innerHTML = `<span class="t">${hhmmss(e.t)}</span><span class="who">${esc(who)}</span><span class="txt">${grade}${esc(text)}</span>`;
        box.appendChild(div);
        if (e.kind === 'coach') this.app.coach?.onCoachEvent(e);
      }
      while (box.childElementCount > 400) box.firstChild.remove();
      if (stick) box.scrollTop = box.scrollHeight;
    } catch { /* retry on next frame */ } finally { this._pulling = false; }
  }

  // ------------------------------------------------------------------ strip & clearances
  bindStrip() {
    $('s-close').onclick = () => this.app.select(null);
    $('s-whatif').onclick = () => this.setWhatifMode(!this.whatifMode);
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

  setWhatifMode(on) {
    this.whatifMode = on;
    $('s-whatif').classList.toggle('on', on);
    $('s-whatif').setAttribute('aria-pressed', String(on));
    $('whatif-note').classList.toggle('hidden', !on);
    document.querySelector('#right .ctl').classList.toggle('whatif', on);
    if (!on) this.app.whatif?.clear();
  }

  updateLevelBtn() {
    const r = this.app.store.map.get(this.app.selectedId);
    if (!r) return;
    $('c-fl-go').textContent = Number($('c-fl').value) * 100 > r.alt ? t('strip.climb') : t('strip.descend');
  }

  async sendClearance(clearances) {
    const id = this.app.selectedId;
    if (!id) return;
    if (this.whatifMode) {
      this.app.whatif?.probe({ aircraft: id, clearances });
      return;
    }
    const r = await this.call('/api/clearance', { aircraft: id, clearances, issuer: 'human' });
    if (r?.rejected?.length) this.toast(r.rejected.join(' · '), true);
    this.app.refreshDetail();
  }

  showStrip(rec, fresh) {
    $('right').classList.toggle('hidden', !rec);
    document.body.classList.toggle('strip-open', !!rec);
    if (!rec) return;
    $('s-callsign').textContent = rec.cs;
    const d = this.detail;
    $('s-sub').textContent = d && d.icao24 === rec.id ? `${d.perf} · ${rec.id.toUpperCase()} · ${d.country ?? ''} · SQ ${d.squawk ?? '—'}` : rec.id.toUpperCase();
    $('s-fl').textContent = pad3(rec.pAlt / 100) + (rec.vs > 250 ? '↑' : rec.vs < -250 ? '↓' : '');
    $('s-cfl').textContent = rec.cfl != null ? pad3(rec.cfl) : '—';
    $('s-gs').textContent = rec.gs;
    $('s-hdg').textContent = pad3(rec.hdg);
    $('s-vs').textContent = (rec.vs > 0 ? '+' : '') + rec.vs;
    $('s-ias').textContent = d && d.icao24 === rec.id ? d.ias_kt : '—';
    $('s-lat').textContent = `NAV ${rec.lateral}${rec.spd ? ' · S' + rec.spd : ''}`;
    const ctlName = d && d.icao24 === rec.id && d.controller ? this.who(d.controller).toUpperCase() : null;
    const ctl = ctlName ?? t(rec.flags & F.HUMAN ? 'strip.controlled' : rec.flags & F.AI ? 'strip.ai' : rec.flags & F.SECTOR ? 'strip.inSector' : 'strip.uncontrolled');
    $('s-ctl').textContent = ctl;
    $('s-ctl').style.color = rec.flags & F.HUMAN ? 'var(--human)' : rec.flags & F.AI ? 'var(--ai)' : 'var(--muted)';
    // responsibility: only the holder of the aircraft's sector may clear it
    const auth = $('s-authority');
    const me = this.frame?.me?.holder;
    const holder = rec.sec ? this.frame?.sectorization?.find((r) => r[0] === rec.sec)?.[1] : null;
    const locked = !!holder && holder !== me;
    auth.className = 'authority ' + (locked ? 'locked' : holder ? 'mine' : 'free');
    auth.style.setProperty('--hc', holderColor(holder, me));
    const sector = `<b>${esc(this.sectorName(rec.sec))}</b>`;
    auth.innerHTML = !rec.sec ? t('strip.outside')
      : locked ? t('strip.locked', { sector, holder: `<b>${esc(holderName(holder, me))}</b>` })
        : holder ? t('strip.mine', { sector })
          : t('strip.unmanned', { sector });
    // a locked strip can still predict (what-if); it can't send
    const ctlBox = document.querySelector('#right .ctl');
    ctlBox.classList.toggle('locked', locked && !this.whatifMode);
    document.querySelectorAll('#right .ctl button, #right .ctl input').forEach((x) => { x.disabled = locked && !this.whatifMode; });
    $('s-pending').textContent = d && d.icao24 === rec.id && d.pending.length
      ? t('strip.executing') + ' ' + d.pending.map((p) => `${p.kind}${p.value != null ? ' ' + p.value : ''}${p.direction ? ' ' + p.direction : ''}`).join(', ') : '';
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
      const predict = text.endsWith('?');
      if (predict) text = text.slice(0, -1).trim();
      const first = text.split(/\s+/)[0];
      const known = this.app.store.list.some((r) => r.cs.toUpperCase() === first || r.id.toUpperCase() === first);
      const sel = this.app.store.map.get(this.app.selectedId);
      if (!known && sel) text = `${sel.cs} ${text}`;
      if (predict || this.whatifMode) {
        this.app.whatif?.probe({ command: text });
        return;
      }
      const r = await this.call('/api/command', { text });
      if (r) {
        input.value = '';
        if (r.rejected.length) this.toast(r.rejected.join(' · '), true);
        this.app.select(r.aircraft);
      }
    };
    window.addEventListener('keydown', (e) => {
      const typing = ['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement?.tagName);
      if (e.key === 'Escape') {
        document.activeElement?.blur();
        this.app.select(null);
        this.app.whatif?.clear();
        $('settings').classList.add('hidden');
      }
      if (typing || document.querySelector('dialog[open]')) return;
      if (e.key === '/') { e.preventDefault(); $('cmd').focus(); }
      if (e.key === ' ') { e.preventDefault(); this.togglePause(); }
      if (e.key === 'h' || e.key === 'H') { e.preventDefault(); this.app.coach?.hint(); }
    });
  }
}
