import { api, getContext } from './net.js';
import { t, tm, getLang } from './i18n.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const hhmmss = (x) => (x ? new Date(x * 1000).toISOString().slice(11, 19) : '--:--:--');
const PARTS = [['safety', 50], ['timeliness', 25], ['efficiency', 25]];

/**
 * Debrief of a coached session: the grade, every situation (what was predicted, what you did,
 * the AI's answer and why, what happened), the coach's words and questions, a 3D replay of the
 * recorded traffic, and "Rewind & retry" while the exercise is still open.
 */
export class Debrief {
  constructor({ app, ui, store, sectorLayer }) {
    this.app = app;
    this.ui = ui;
    this.store = store;
    this.sectorLayer = sectorLayer;
    this.sid = null;
    this.data = null;
    this.player = null;
    $('db-close').onclick = () => $('debrief').close();
    $('db-ask').onsubmit = (e) => { e.preventDefault(); this.ask(); };
    this.bindReplay();
  }

  async open(sid, focusEntry = null) {
    this.sid = sid;
    let data;
    try { data = await api(`/api/sessions/${sid}`); } catch (e) { this.ui.toast(e.message, true); return; }
    this.data = data;
    this.render(focusEntry);
    if (!$('debrief').open) $('debrief').showModal();
    this.loadNarrative();
  }

  canRewind() {
    const ex = this.ui.frame?.exercise;              // the last live frame (not a replayed one)
    return getContext() === 'exercise' && !!ex && ex.session === this.sid;
  }

  render(focusEntry) {
    const { session: s, entries, exercise } = this.data;
    const sum = s.summary ?? {};
    const attempt = sum.attempt ? ' · ' + t('debrief.attempt', { n: sum.attempt }) : '';
    $('db-kicker').textContent = t(`debrief.context.${s.context}`) + attempt;
    $('db-title').textContent = exercise ? t(`ex.${exercise.id}.title`) : s.context === 'live' ? t('hub.live', { day: s.day }) : t('hub.tutorial');

    const comp = Object.entries(sum.competencies ?? {});
    $('db-summary').innerHTML = `
      <div class="db-grade"><span class="grade-big huge g${sum.letter ?? '·'}">${sum.letter ?? '·'}</span>
        <div><div class="db-avg">${sum.average != null ? `${sum.average}<small>/100</small>` : '—'}</div>
        <div class="dim">${esc(t(sum.passed ? 'ex.passed' : sum.graded ? 'ex.notPassed' : 'debrief.noGrades'))}</div></div></div>
      <div class="db-stats">
        <div><b>${sum.graded ?? 0}</b><span>${esc(t('debrief.graded'))}</span></div>
        <div class="${sum.los ? 'bad' : ''}"><b>${sum.los ?? 0}</b><span>${esc(t('debrief.los'))}</span></div>
        <div><b>${sum.hints ?? 0}</b><span>${esc(t('debrief.hints'))}</span></div>
        <div><b>${sum.demonstrated ?? 0}</b><span>${esc(t('debrief.demos'))}</span></div>
      </div>
      <div class="db-parts">${PARTS.map(([k, max]) => {
        const v = sum.parts?.[k];
        return `<div class="part"><span>${esc(t(`debrief.part.${k}`))}</span><div class="bar"><i style="width:${v != null ? Math.round((v / max) * 100) : 0}%"></i></div><b>${v != null ? v : '—'}</b></div>`;
      }).join('')}
      ${comp.length ? `<div class="db-comp">${comp.map(([k, v]) => `<span class="chip-comp" title="${esc(t('debrief.compTip'))}">${esc(t(`comp.${k}`))} <b>${v}</b></span>`).join('')}</div>` : ''}</div>`;

    // timeline: every situation between the session's first and last moment
    const t0 = s.sim_t0 ?? entries[0]?.opened_t;
    const t1 = Math.max(s.sim_t1 ?? 0, ...entries.map((e) => e.closed_t ?? e.opened_t), (t0 ?? 0) + 60);
    $('db-timeline').innerHTML = entries.length && t0 ? `
      <div class="tl-bar">${entries.map((e) => {
        const g = e.grade ?? {};
        const x = ((e.opened_t - t0) / (t1 - t0)) * 100;
        return `<button class="tl-dot g${g.letter ?? 'x'}" style="left:${Math.max(0, Math.min(100, x))}%" data-goto="${e.id}" title="${esc(hhmmss(e.opened_t) + ' ' + e.callsigns.join('/'))}">${g.letter ?? '·'}</button>`;
      }).join('')}</div>
      <div class="tl-ends"><span>${hhmmss(t0)}</span><span>${hhmmss(t1)}</span></div>` : '';

    const rewind = this.canRewind();
    const replay = this.data.replay;
    $('db-entries').innerHTML = entries.length ? entries.map((e) => this.entryHtml(e, rewind, replay)).join('')
      : `<div class="empty">${esc(t('debrief.noEntries'))}</div>`;
    const root = $('debrief');
    root.querySelectorAll('[data-goto]').forEach((b) => {
      b.onclick = () => {
        const card = root.querySelector(`[data-entry="${b.dataset.goto}"]`);
        card?.scrollIntoView({ behavior: 'smooth', block: 'center' });
        card?.classList.add('flash');
        setTimeout(() => card?.classList.remove('flash'), 1400);
      };
    });
    root.querySelectorAll('[data-replay]').forEach((b) => { b.onclick = () => this.startReplay(Number(b.dataset.replay)); });
    root.querySelectorAll('[data-rewind]').forEach((b) => {
      b.onclick = async () => {
        const r = await this.app.coach.rewind(b.dataset.rewind);
        if (r) $('debrief').close();
      };
    });
    if (focusEntry) setTimeout(() => root.querySelector(`[data-goto="${focusEntry}"]`)?.click(), 250);
    this.renderChat();
  }

  entryHtml(e, rewind, replay) {
    const g = e.grade ?? {};
    const sit = e.situation ?? {};
    let what = '';
    if (e.kind === 'conflict') {
      const c = sit.conflict ?? {};
      what = c.type === 'MTCD'
        ? t('debrief.sitEarly', { t: c.time_to_conflict_s, h: c.predicted_h_nm })
        : t('debrief.sitConflict', { t: c.time_to_conflict_s, h: c.predicted_h_nm, v: c.predicted_v_ft });
    } else {
      const r = sit.request ?? {};
      what = e.kind === 'level_request' ? t('debrief.sitLevel', { fl: String(r.requested_fl ?? e.requested_fl ?? '').padStart(3, '0') }) : t('debrief.sitRoute');
    }
    const acts = e.actions?.length
      ? e.actions.map((a) => `<li><span class="mono">${hhmmss(a.t)}</span> ${esc(a.callsign)}, ${esc(a.phrases.join(', '))}${a.lead_s != null ? ` <span class="dim">(${esc(t('debrief.lead', { s: a.lead_s }))})</span>` : ''}</li>`).join('')
      : e.response ? `<li><span class="mono">${hhmmss(e.response.t)}</span> ${esc(t(`debrief.role.${e.response.role}`))}${e.response.phrases?.length ? ': ' + esc(e.response.phrases.join(', ')) : ''}</li>`
        : `<li class="dim">${esc(t(e.by_ai ? 'debrief.byAi' : 'debrief.noAction'))}</li>`;
    const best = e.best ? `<div class="db-ai"><b>${esc(t('debrief.aiAnswer'))}</b> ${esc(e.best.label)}${e.best.why ? `<div class="dim">${esc(tm(e.best.why))}</div>` : ''}</div>` : '';
    const a = e.actual ?? {};
    // vertically separated when they passed; otherwise the tightest horizontal distance at the same level
    const outcome = a.los ? `<span class="bad">${esc(t('debrief.outLos', { h: a.los_h, v: a.los_v }))}</span>`
      : a.cpa_h != null && (a.cpa_v ?? 0) >= 900 ? esc(t('debrief.outV', { h: a.cpa_h, v: a.cpa_v }))
        : a.min_h != null ? esc(t('debrief.outH', { h: a.min_h })) : esc(t(`debrief.outcome.${e.outcome}`));
    const parts = g.parts && g.score != null ? `<div class="db-parts mini">${PARTS.map(([k, max]) => `<div class="part"><span>${esc(t(`debrief.part.${k}`))}</span><div class="bar"><i style="width:${Math.round(((g.parts[k] ?? 0) / max) * 100)}%"></i></div><b>${g.parts[k] ?? 0}</b></div>`).join('')}${g.parts.hints ? `<div class="part"><span>${esc(t('debrief.part.hints'))}</span><div></div><b>${g.parts.hints}</b></div>` : ''}</div>` : '';
    return `<article class="db-entry" data-entry="${e.id}">
      <header><span class="grade-big small g${g.letter ?? 'x'}">${g.letter ?? '·'}</span>
        <div class="db-eh"><b>${esc(e.callsigns.join(' / '))}</b><span class="dim">${esc(t(`dec.kind.${e.kind}`))} · ${hhmmss(e.opened_t)}${e.early ? ' · ' + esc(t('debrief.early')) : ''}${g.score != null ? ` · ${g.score}/100` : ''}</span></div></header>
      <div class="db-row"><span class="lbl">${esc(t('debrief.situation'))}</span><span>${esc(what)}</span></div>
      <div class="db-row"><span class="lbl">${esc(t('debrief.you'))}</span><ul>${acts}</ul></div>
      ${best ? `<div class="db-row"><span class="lbl">${esc(t('debrief.ai'))}</span>${best}</div>` : ''}
      <div class="db-row"><span class="lbl">${esc(t('debrief.result'))}</span><span>${outcome}</span></div>
      ${g.feedback ? `<div class="db-fb">${esc(tm(g.feedback))}</div>` : ''}
      ${parts}
      <div class="db-actions">
        ${replay ? `<button class="ghost-btn small" data-replay="${Math.max(0, e.opened_t - 45)}">▶ ${esc(t('debrief.replay'))}</button>` : ''}
        ${rewind ? `<button class="primary small" data-rewind="${e.id}">⟲ ${esc(t('debrief.rewind'))}</button>` : ''}
      </div>
    </article>`;
  }

  // ------------------------------------------------------------------ the coach's words
  async loadNarrative() {
    const box = $('db-narrative');
    const sid = this.sid;
    box.innerHTML = `<p class="dim">${esc(t('debrief.thinking'))}</p>`;
    let out;
    try { out = await api(`/api/sessions/${sid}/debrief`, { lang: getLang() }); } catch (e) { box.innerHTML = `<p class="form-error">${esc(e.message)}</p>`; return; }
    if (sid !== this.sid) return;
    box.innerHTML = out.text ? this.formatText(out.text) : `<ul class="db-msgs">${out.messages.map((m) => `<li>${esc(tm(m))}</li>`).join('')}</ul>`;
    const coach = this.ui.languageCoach;
    const llm = coach?.available;
    $('db-ask').classList.toggle('hidden', !llm);
    $('db-ask-note').textContent = llm ? t('debrief.askNote') : t('debrief.noLlm');
  }

  formatText(text) {
    const lines = text.split(/\n+/).map((l) => l.trim()).filter(Boolean);
    let html = '', list = false;
    for (const l of lines) {
      const bullet = /^[-•*]\s+/.test(l);
      if (bullet && !list) { html += '<ul>'; list = true; }
      if (!bullet && list) { html += '</ul>'; list = false; }
      html += bullet ? `<li>${esc(l.replace(/^[-•*]\s+/, ''))}</li>` : `<p>${esc(l)}</p>`;
    }
    return html + (list ? '</ul>' : '');
  }

  renderChat() {
    $('db-chat').innerHTML = (this.data?.chats ?? []).map((c) => `<div class="chat ${c.role}">${c.role === 'coach' ? this.formatText(c.text) : esc(c.text)}</div>`).join('');
  }

  async ask() {
    const q = $('db-question').value.trim();
    if (!q) return;
    $('db-question').value = '';
    this.data.chats.push({ role: 'user', text: q });
    this.renderChat();
    const pending = document.createElement('div');
    pending.className = 'chat coach dim';
    pending.textContent = t('debrief.thinking');
    $('db-chat').appendChild(pending);
    try {
      const out = await api(`/api/sessions/${this.sid}/ask`, { question: q, lang: getLang() });
      this.data.chats.push({ role: 'coach', text: out.text ?? out.messages.map((m) => tm(m)).join(' ') });
    } catch (e) {
      this.data.chats.push({ role: 'coach', text: e.message });
    }
    this.renderChat();
    $('db-chat').scrollTop = $('db-chat').scrollHeight;
  }

  // ------------------------------------------------------------------ 3D replay
  bindReplay() {
    $('replay-close').onclick = () => this.stopReplay(true);
    $('replay-play').onclick = () => { if (this.player) { this.player.playing = !this.player.playing; this.player.stamp(); } };
    $('replay-speed').onclick = (e) => {
      const s = Number(e.target.dataset.rs);
      if (!s || !this.player) return;
      this.player.stamp();
      this.player.speed = s;
      for (const b of $('replay-speed').children) b.classList.toggle('on', b === e.target);
    };
    $('replay-pos').oninput = (e) => {
      const p = this.player;
      if (!p) return;
      p.t = p.t0 + (Number(e.target.value) / 1000) * (p.t1 - p.t0);
      p.stamp();
      p.idx = -1;
    };
  }

  async startReplay(fromT) {
    let frames;
    try { frames = (await api(`/api/sessions/${this.sid}/replay`)).frames; } catch (e) { this.ui.toast(e.message, true); return; }
    if (!frames.length) return;
    $('debrief').close();
    const live = this.store.frame;
    const p = {
      frames, t0: frames[0].t, t1: frames[frames.length - 1].t, t: Math.max(frames[0].t, fromT), idx: -1,
      speed: 4, playing: true, live, real: performance.now(),
      stamp() { this.base = this.t; this.real = performance.now(); },
    };
    p.stamp();
    this.player = p;
    this.app.replay = p;
    document.body.classList.add('replaying');
    $('replay-bar').classList.remove('hidden');
    this.app.select(null);
    this.app.whatif?.clear();
    const e = this.data.entries.find((x) => x.opened_t >= fromT) ?? this.data.entries[0];
    if (e?.focus) this.app.flyTo(e.focus.lat, e.focus.lon, 650);
    this.tickReplay();
  }

  tickReplay() {
    const p = this.player;
    if (!p) return;
    if (p.playing) p.t = Math.min(p.t1, p.base + ((performance.now() - p.real) / 1000) * p.speed);
    if (p.t >= p.t1) { p.playing = false; p.stamp(); }
    // the recorded frame at or before t
    let i = p.frames.findIndex((f) => f.t > p.t) - 1;
    if (i < 0) i = p.t >= p.t1 ? p.frames.length - 1 : 0;
    if (i !== p.idx) {
      p.idx = i;
      const fr = p.frames[i];
      const live = p.live ?? {};
      this.store.applyFrame({
        type: 'frame', t: fr.t, mode: 'replay', speed: p.speed, paused: !p.playing, lockstep: false,
        ac: fr.ac, conflicts: fr.conflicts.map(([a, b, kind]) => [a, b, kind, 0, 0, 0, []]),
        decisions: [], sectorization: live.sectorization ?? [], me: live.me ?? {}, score: live.score ?? {},
      });
    }
    this.store.rate = p.playing ? p.speed : 0;
    $('replay-play').textContent = p.playing ? '❚❚' : '▶';
    $('replay-pos').value = String(Math.round(((p.t - p.t0) / Math.max(1, p.t1 - p.t0)) * 1000));
    $('replay-time').textContent = hhmmss(p.t);
    requestAnimationFrame(() => this.tickReplay());
  }

  stopReplay(reopen) {
    const live = this.player?.live;
    this.player = null;
    this.app.replay = null;
    document.body.classList.remove('replaying');
    $('replay-bar').classList.add('hidden');
    if (live) this.store.applyFrame(live);             // back to the live picture at once
    if (reopen && this.sid) this.open(this.sid);
  }
}
