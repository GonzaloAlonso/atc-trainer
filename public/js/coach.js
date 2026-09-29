import { api, getContext, setContext } from './net.js';
import { t, tm } from './i18n.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const mmss = (s) => `${Math.floor(Math.max(0, s) / 60)}:${String(Math.floor(Math.max(0, s) % 60)).padStart(2, '0')}`;

/**
 * The coach in the working position: hints, a card after every graded decision, the training
 * center (exercises and past sessions) and the running exercise.
 */
export class CoachUI {
  constructor({ app, ui, store, feed, tutorial, debrief }) {
    this.app = app;
    this.ui = ui;
    this.store = store;
    this.feed = feed;
    this.tutorial = tutorial;
    this.debrief = debrief;
    this.seenGrades = new Set();
    this.seenHints = new Set();
    this.exercise = null;          // {id, ...} while an exercise runs
    this.autoDebriefed = null;
    this.focus = null;             // {ids, until}: aircraft the last hint points at
    this.bind();
  }

  bind() {
    $('btn-training').onclick = () => this.openHub();
    $('menu-training').onclick = () => { $('user-pop').classList.add('hidden'); this.openHub(); };
    $('hub-close').onclick = () => $('hub-dialog').close();
    document.querySelector('.hub-tabs').onclick = (e) => {
      const b = e.target.closest('[data-hub]');
      if (!b) return;
      for (const x of document.querySelectorAll('.hub-tabs [data-hub]')) x.classList.toggle('on', x === b);
      $('hub-exercises').classList.toggle('hidden', b.dataset.hub !== 'exercises');
      $('hub-history').classList.toggle('hidden', b.dataset.hub !== 'history');
      if (b.dataset.hub === 'history') this.renderHistory();
    };
    $('ex-exit').onclick = () => this.leaveExercise(true);
    $('ex-min').onclick = () => $('ex-hud').classList.toggle('min');
    $('ex-restart').onclick = () => this.exercise && this.startExercise(this.exercise.id);
    $('ex-debrief').onclick = () => {
      const s = this.store.frame?.exercise?.session ?? this.store.frame?.coach?.session;
      if (s) this.debrief.open(s);
    };
  }

  // ------------------------------------------------------------------ frames
  onFrame(frame) {
    const c = frame.coach;
    if (c) {
      for (const g of c.graded) {
        if (this.seenGrades.has(g.id)) continue;
        this.seenGrades.add(g.id);
        if (c.level !== 'off' && g.feedback && this.seenGrades.size > 0 && this._ready) this.gradeCard(g);
      }
      for (const o of c.open) {
        const k = `${o.dp}:${o.tier}`;
        if (o.hint && o.auto && !this.seenHints.has(k)) {
          this.seenHints.add(k);
          this.hintCard({ decision: o.dp, tier: o.tier, message: o.hint, auto: true });
        }
      }
      this._ready = true;          // grades that existed before this page opened don't pop up
    }
    this.updateExercise(frame.exercise);
  }

  onCoachEvent(e) {
    if (e.msg?.key === 'narr.act') this.card('narr', `<div class="cc-head"><span class="cc-tag ai">${esc(t('coachUi.demo'))}</span></div><div class="cc-text">${esc(tm(e.msg))}</div>`, 9000);
  }

  // ------------------------------------------------------------------ cards
  card(kind, html, ttl = 12000, id = null) {
    const box = $('coach-cards');
    if (id) box.querySelector(`[data-card="${id}"]`)?.remove();
    const el = document.createElement('div');
    el.className = `coach-card ${kind}`;
    if (id) el.dataset.card = id;
    el.innerHTML = `<button class="cc-x" aria-label="${esc(t('common.close'))}">×</button>${html}`;
    el.querySelector('.cc-x').onclick = () => el.remove();
    box.prepend(el);
    while (box.children.length > 3) box.lastChild.remove();
    if (ttl) setTimeout(() => el.remove(), ttl);
    return el;
  }

  gradeCard(g) {
    const letter = g.letter ?? '·';
    const el = this.card('grade', `
      <div class="cc-head"><span class="grade-big g${letter}">${letter}</span>
        <div><div class="cc-title">${esc(g.callsigns.join(' / '))}</div>
        <div class="cc-sub">${esc(t(`dec.kind.${g.kind}`))}${g.score != null ? ` · ${g.score}/100` : ''}</div></div></div>
      <div class="cc-text">${esc(tm(g.feedback))}</div>
      <div class="cc-actions"><button class="mini" data-act="debrief">${esc(t('coachUi.openDebrief'))}</button></div>`, 14000);
    el.querySelector('[data-act=debrief]').onclick = () => {
      const s = this.store.frame?.coach?.session;
      if (s) this.debrief.open(s, g.id);
    };
  }

  hintCard(h) {
    const el = this.card('hint', `
      <div class="cc-head"><span class="cc-tag">💡 ${esc(t('coachUi.hintN', { n: h.tier }))}${h.auto ? ' · ' + esc(t('coachUi.auto')) : ''}</span></div>
      <div class="cc-text">${esc(tm(h.message))}</div>
      ${h.tier < 3 ? `<div class="cc-actions"><button class="mini" data-act="more">${esc(t('coachUi.moreHint'))}</button></div>` : ''}`, 0, `hint-${h.decision}`);
    el.querySelector('[data-act=more]')?.addEventListener('click', () => this.hint(h.decision));
  }

  // ------------------------------------------------------------------ hints
  /** Ask for the next hint on a decision (default: the most urgent open one). */
  async hint(dpId) {
    const f = this.store.frame;
    if (!f?.coach) return;
    if (f.coach.level !== 'hints') {
      this.ui.toast(t('coachUi.hintsOnlyAtHints'), true);
      return;
    }
    if (!dpId) {
      const open = f.decisions.filter((d) => d.status === 'open');
      const conflictT = new Map(f.conflicts.map((c) => [[c[0], c[1]].sort().join(','), c[3]]));
      open.sort((a, b) => (conflictT.get(a.subjects.slice().sort().join(',')) ?? 999) - (conflictT.get(b.subjects.slice().sort().join(',')) ?? 999));
      dpId = open[0]?.id;
      if (!dpId) { this.ui.toast(t('coachUi.nothingToHint')); return; }
    }
    let h;
    try { h = await api('/api/coach/hint', { decision: dpId }); } catch (e) { this.ui.toast(e.message, true); return; }
    this.seenHints.add(`${h.decision}:${h.tier}`);
    this.hintCard(h);
    this.ui.sigs = {};
    if (h.tier === 1 && h.focus) {
      this.focus = { ids: h.aircraft, until: performance.now() + 9000 };
      this.app.flyTo(h.focus.lat, h.focus.lon, 650);
    }
    if (h.tier === 3) this.ui.openTab('decisions');
  }

  // ------------------------------------------------------------------ training center
  async openHub() {
    $('hub-dialog').showModal();
    await this.renderExercises();
  }

  async renderExercises() {
    const el = $('hub-exercises');
    let data;
    try { data = await api('/api/exercises'); } catch (e) { el.innerHTML = `<p class="form-error">${esc(e.message)}</p>`; return; }
    const me = this.tutorial.me;
    const tutState = me?.tutorial_state === 'completed' ? t('hub.tutDone') : me?.tutorial_step ? t('hub.tutAt', { n: me.tutorial_step + 1 }) : t('hub.tutNew');
    el.innerHTML = `
      <div class="ex-row tut-row">
        <div class="ex-main"><div class="ex-title">🎓 ${esc(t('hub.tutorial'))}</div><div class="ex-brief">${esc(t('hub.tutBrief'))}</div></div>
        <div class="ex-side"><span class="dim">${esc(tutState)}</span><button class="primary small" data-tut>${esc(t('hub.open'))}</button></div>
      </div>
      <h3 class="hub-h">${esc(t('hub.drills'))}</h3>
      ${data.exercises.map((x) => `
      <div class="ex-row">
        <div class="ex-main">
          <div class="ex-title">${esc(t(`ex.${x.id}.title`))} <span class="lvl lvl-${x.level}">${esc(t(`hub.level.${x.level}`))}</span></div>
          <div class="ex-brief">${esc(t(`ex.${x.id}.brief`))}</div>
          <div class="ex-meta">${esc(t('hub.meta', { min: Math.round(x.duration_s / 60), n: x.aircraft }))} · ${x.competencies.map((c) => esc(t(`comp.${c}`))).join(' · ')}</div>
        </div>
        <div class="ex-side">
          ${x.best ? `<span class="grade-big small g${x.best.letter}" title="${esc(t('hub.best'))}">${x.best.letter}</span>` : '<span class="dim">—</span>'}
          <button class="primary small" data-ex="${x.id}">${esc(t(x.best ? 'hub.again' : 'hub.start'))}</button>
        </div>
      </div>`).join('')}`;
    el.querySelector('[data-tut]').onclick = () => { $('hub-dialog').close(); this.tutorial.openFromMenu(); };
    el.querySelectorAll('[data-ex]').forEach((b) => { b.onclick = () => { $('hub-dialog').close(); this.startExercise(b.dataset.ex); }; });
  }

  async renderHistory() {
    const el = $('hub-history');
    let data;
    try { data = await api('/api/sessions?limit=40'); } catch (e) { el.innerHTML = `<p class="form-error">${esc(e.message)}</p>`; return; }
    if (!data.sessions.length) { el.innerHTML = `<div class="empty">${esc(t('hub.noSessions'))}</div>`; return; }
    el.innerHTML = data.sessions.map((s) => {
      const sum = s.summary ?? {};
      const what = s.context === 'exercise' ? t(`ex.${s.exercise}.title`) : s.context === 'live' ? t('hub.live', { day: s.day }) : t('hub.tutorial');
      return `<div class="ex-row sess" data-sid="${s.id}">
        <div class="ex-main"><div class="ex-title">${esc(what)}${s.parent ? ` <span class="dim">· ${esc(t('hub.retry'))}</span>` : ''}</div>
          <div class="ex-meta">${esc(new Date(s.started * 1000).toLocaleString())} · ${esc(t(`hub.status.${s.status}`))}${sum.decisions != null ? ' · ' + esc(t('hub.decisions', { n: sum.decisions })) : ''}</div></div>
        <div class="ex-side">${sum.letter ? `<span class="grade-big small g${sum.letter}">${sum.letter}</span>` : '<span class="dim">—</span>'}
          <button class="ghost-btn small">${esc(t('hub.view'))}</button></div>
      </div>`;
    }).join('');
    el.querySelectorAll('[data-sid]').forEach((r) => { r.onclick = () => { $('hub-dialog').close(); this.debrief.open(Number(r.dataset.sid)); }; });
  }

  // ------------------------------------------------------------------ exercises
  async startExercise(id) {
    if (this.tutorial.active) await this.tutorial.leave(null, true);
    let r;
    try { r = await api(`/api/exercises/${id}/start`, {}); } catch (e) { this.ui.toast(e.message, true); return; }
    this.exercise = r.exercise;
    this.autoDebriefed = null;
    this.seenGrades.clear();
    this.seenHints.clear();
    this._ready = false;
    setContext('exercise');
    document.body.classList.add('training', 'exercise');
    this.ui.resetForContext(t('ex.started', { title: t(`ex.${id}.title`) }));
    this.app.select(null);
    this.app.whatif?.clear();
    this.feed.reconnect();
    const [lat, lon, dist] = r.exercise.focus;
    this.app.flyTo(lat, lon, dist);
    $('ex-title').textContent = t(`ex.${id}.title`);
    $('ex-brief').textContent = t(`ex.${id}.brief`);
    $('ex-goal').textContent = t(`ex.${id}.goal`);
    $('ex-result').classList.add('hidden');
    $('ex-debrief').classList.remove('pulse');
    $('ex-hud').classList.remove('hidden', 'min');
  }

  updateExercise(ex) {
    if (getContext() !== 'exercise' || !ex) return;
    $('ex-step').textContent = t('ex.progress', { elapsed: mmss(ex.elapsed), total: mmss(ex.duration), attempt: ex.attempt });
    $('ex-bar').style.width = `${Math.min(100, (ex.elapsed / ex.duration) * 100)}%`;
    const done = ex.status === 'completed';
    const res = $('ex-result');
    res.classList.toggle('hidden', !done);
    if (done && ex.summary) {
      const s = ex.summary;
      res.innerHTML = `<div class="cc-head"><span class="grade-big g${s.letter ?? '·'}">${s.letter ?? '·'}</span>
        <div><div class="cc-title">${esc(t(s.passed ? 'ex.passed' : 'ex.notPassed'))}</div>
        <div class="cc-sub">${esc(t('ex.resultLine', { avg: s.average ?? '—', n: s.graded, los: s.los }))}</div></div></div>`;
      $('ex-debrief').classList.add('pulse');
      if (this.autoDebriefed !== ex.session) {
        this.autoDebriefed = ex.session;
        setTimeout(() => this.debrief.open(ex.session), 900);
      }
    }
  }

  async leaveExercise(stop) {
    if (stop) await api('/api/exercise/stop', {}).catch(() => {});
    this.exercise = null;
    $('ex-hud').classList.add('hidden');
    document.body.classList.remove('training', 'exercise');
    setContext(null);
    this.ui.resetForContext(t('ex.left'));
    this.app.select(null);
    this.feed.reconnect();
  }

  sandboxGone() {
    if (getContext() !== 'exercise') return;
    this.ui.toast(t('ex.ended'), true);
    this.leaveExercise(false);
  }

  /** Rewind the running exercise to just before a graded situation. */
  async rewind(entryId) {
    try {
      const r = await api('/api/exercise/rewind', { entry: entryId });
      this.autoDebriefed = null;
      this.seenGrades.clear();
      this._ready = false;
      this.ui.resetForContext(t('ex.rewound', { attempt: r.attempt }));
      $('ex-result').classList.add('hidden');
      $('ex-debrief').classList.remove('pulse');
      return r;
    } catch (e) {
      this.ui.toast(e.message, true);
      return null;
    }
  }
}
