import { api, setContext } from './net.js';
import { F } from './traffic.js';
import { t } from './i18n.js';

const $ = (id) => document.getElementById(id);

// Aircraft of the scripted training scenario (atc/training.py)
const TRN303 = '7a0303';
const PAIR = ['7c0101', '7c0202'];
const SECTOR = 'ALP-U';
const compact = () => window.matchMedia('(max-width: 1100px)').matches;
const narrow = () => window.matchMedia('(max-width: 900px)').matches;

/**
 * Lessons. Each has a goal the wizard detects from the live frame (check), instructions (how),
 * the UI element to highlight (target), and optional hooks:
 *   enter(t)   runs when the lesson opens (e.g. triggers the scripted conflict)
 *   prepare(t) puts the scenario in the state the lesson needs when earlier lessons were
 *              skipped or the tutorial was resumed in a fresh sandbox
 *   skip(t)    applies the lesson's outcome when the trainee skips it
 */
// Where things are, depending on the layout: the lists are a tab on the left or a sheet behind ☰;
// secondary top-bar controls sit in the ⋯ panel on compact screens.
const list = (tab) => t(narrow() ? 'tut.where.sheet' : 'tut.where.tab', { tab: t(`tabs.${tab}`) });
const bar = () => t(compact() ? 'tut.where.panel' : 'tut.where.topBar');

const STEPS = [
  { id: 'welcome', info: true },
  {
    id: 'scope',
    check: (x) => { const v = x.app.cameraInfo(); return v.distance < 900 && v.lat > 42 && v.lat < 47 && v.lon > 1 && v.lon < 12; },
    showMe: (x) => x.app.flyTo(44.6, 6.6, 700),
  },
  {
    id: 'sector',
    how: () => t('tut.sector.how', { list: list('sectors'), bar: bar() }),
    target: `[data-sact="take"][data-sid="${SECTOR}"]`,
    check: (x) => (x.frame?.me?.sectors ?? []).includes(SECTOR),
    skip: () => api(`/api/sectors/${SECTOR}/take`, {}),
    prepare: (x) => ((x.frame?.me?.sectors ?? []).includes(SECTOR) ? null : api(`/api/sectors/${SECTOR}/take`, {})),
  },
  {
    id: 'datablock',
    check: (x) => x.app.selectedId === TRN303,
    showMe: (x) => x.app.select(TRN303, true),
  },
  {
    id: 'heading',
    target: '#cmd',
    check: (x) => { const r = x.rec(TRN303); return !!r && (r.ahdg != null || r.lateral?.startsWith('H')); },
  },
  {
    id: 'direct',
    target: '#c-dct',
    check: (x) => !!x.rec(TRN303)?.dct,
    enter: (x) => { if (x.app.selectedId !== TRN303) x.app.select(TRN303); },
  },
  {
    id: 'resume',
    target: '#c-ron',
    check: (x) => { const r = x.rec(TRN303); return !!r && r.dct == null && r.ahdg == null && (r.flags & F.HUMAN); },
    enter: (x) => { if (x.app.selectedId !== TRN303) x.app.select(TRN303); },
  },
  {
    id: 'level',
    target: '#c-fl',
    check: (x) => x.rec(TRN303)?.cfl === 330,
    enter: (x) => { if (x.app.selectedId !== TRN303) x.app.select(TRN303); },
  },
  {
    id: 'spot',
    how: () => t('tut.spot.how', { list: list('alerts'), speed: compact() ? t('tut.where.inPanel') : '' }),
    target: () => (narrow() ? '#btn-lists' : '[data-tab=alerts]'),
    check: (x) => x.pairInConflict() && PAIR.includes(x.app.selectedId),
    enter: (x) => {
      if (!x.pairPresent()) api('/api/tutorial/scenario', { event: 'conflict' }).catch(() => {});
      x.app.flyTo(44.3, 7.3, 650);
    },
    prepare: (x) => (x.pairPresent() ? null : api('/api/tutorial/scenario', { event: 'conflict' })),
  },
  {
    id: 'resolve',
    target: '#cmd',
    check: (x) => {
      const cleared = PAIR.some((id) => { const r = x.rec(id); return r && (r.cfl != null || r.ahdg != null); });
      if (x.pairInConflict()) x.state.sawConflict = true;
      return x.state.sawConflict && cleared && !x.pairInConflict() && x.pairPassed()
        && (x.frame?.score?.los ?? 0) === 0;
    },
    failed: (x) => ((x.frame?.score?.los ?? 0) > 0 ? t('tut.resolve.failed') : null),
    retry: (x) => { x.state.sawConflict = false; return api('/api/tutorial/scenario', { event: 'conflict' }); },
    prepare: (x) => (x.pairPresent() ? null : api('/api/tutorial/scenario', { event: 'conflict' })),
  },
  {
    id: 'coach',
    how: () => t('tut.coach.how', { bar: bar() }),
    target: '#coach-level',
    check: (x) => x.frame?.coach?.level === 'advise',
  },
  {
    id: 'request',
    how: () => t('tut.request.how', { list: list('decisions') }),
    target: () => (narrow() ? '#btn-lists' : '[data-tab=decisions]'),
    check: (x) => x.rec(TRN303)?.cfl === 290
      || (x.frame?.decisions ?? []).some((d) => d.kind === 'level_request' && d.subjects.includes(TRN303) && d.status === 'executed'),
    prepare: (x) => (x.rec(TRN303)?.cfl === 330 ? null : api('/api/command', { text: 'TRN303 C 330' })),
  },
  {
    id: 'score',
    how: () => t('tut.score.how', { bar: compact() ? t('tut.where.inPanel') : t('tut.where.nextToPause') }),
    target: '#speed-seg',
    check: (x) => (x.frame?.speed ?? 1) >= 2,
  },
  { id: 'ready', info: true, last: true },
];

export class Tutorial {
  constructor({ app, ui, store, feed }) {
    this.app = app;
    this.ui = ui;
    this.store = store;
    this.feed = feed;
    this.active = false;
    this.i = 0;
    this.done = new Set();
    this.state = {};
    this.me = null;
    this.bind();
  }

  get frame() { return this.store.frame; }
  rec(id) { return this.store.map.get(id); }
  pairPresent() { return PAIR.every((id) => this.store.map.has(id)); }
  /** TRN101 flies east, TRN202 west: once TRN101 is east of TRN202 they have passed each other. */
  pairPassed() {
    const [a, b] = PAIR.map((id) => this.rec(id));
    return !a || !b || a.lon > b.lon;
  }

  pairInConflict() {
    return (this.frame?.conflicts ?? []).some(([a, b]) => PAIR.includes(a) && PAIR.includes(b));
  }

  bind() {
    $('tut-next').onclick = () => this.next();
    $('tut-back').onclick = () => this.go(this.i - 1);
    $('tut-skip').onclick = () => this.skip();
    $('tut-exit').onclick = () => this.exit();
    $('tut-min').onclick = () => $('tutorial').classList.toggle('min');
    $('tut-show').onclick = () => STEPS[this.i].showMe?.(this);
    $('tut-retry').onclick = () => STEPS[this.i].retry?.(this);
    $('tut-start').onclick = () => { $('tut-offer').close(); this.start(this.me?.tutorial_step || 0); };
    $('tut-later').onclick = () => $('tut-offer').close();
    $('tut-dismiss').onclick = async () => {
      $('tut-offer').close();
      await this.ui.call('/api/tutorial/dismiss', {});
      this.refreshMe();
      this.ui.toast(t('tut.dismissed'));
    };
    $('menu-tutorial').onclick = () => this.openFromMenu();
  }

  /** Open the tutorial: unfinished resumes where it was left, completed or declined starts over. */
  openFromMenu() {
    $('user-pop').classList.add('hidden');
    if (this.active) { $('tutorial').classList.remove('min'); return; }
    this.start(this.me?.tutorial_state == null ? this.me?.tutorial_step || 0 : 0);
  }

  async refreshMe() {
    try { this.me = await api('/api/auth/me'); } catch { return; }
    $('tut-pill').classList.toggle('hidden', this.me.tutorial_state === 'completed');
    $('btn-training').classList.toggle('recommended', this.me.tutorial_state !== 'completed');
  }

  /** At sign-in: offer the tutorial until completed or declined for good. */
  async offer() {
    await this.refreshMe();
    if (!this.me || this.me.tutorial_state != null) return;
    const step = this.me.tutorial_step || 0;
    $('tut-start').textContent = step > 0 ? t('tut.resumeAt', { n: step + 1 }) : t('tutOffer.start');
    $('tut-offer-resume').classList.toggle('hidden', step === 0);
    $('tut-offer').showModal();
  }

  async start(step = 0) {
    if (this.app.coach?.exercise) await this.app.coach.leaveExercise(true);
    try {
      await api('/api/tutorial/start', {});
    } catch (e) {
      this.ui.toast(e.message, true);
      return;
    }
    setContext('tutorial');
    this.active = true;
    this.done = new Set();
    this.state = {};
    document.body.classList.add('training');
    this.ui.resetForContext(t('tut.started'));
    this.app.select(null);
    this.feed.reconnect();
    this.app.flyTo(44.6, 6.6, 2400);
    $('tutorial').classList.remove('hidden', 'min');
    await this.go(Math.min(step, STEPS.length - 1), true);
  }

  /** Back to live traffic. quiet: an exercise takes over the sandbox, so don't reconnect. */
  async leave(message, quiet = false) {
    this.active = false;
    this.highlight(null);
    document.body.classList.remove('training');
    $('tutorial').classList.add('hidden');
    setContext(null);
    this.refreshMe();
    if (quiet) return;
    this.ui.resetForContext(message);
    this.app.select(null);
    this.feed.reconnect();
  }

  async exit() {
    if (!this.active) return;
    await api('/api/tutorial/stop', {}).catch(() => {});
    await this.leave(t('tut.backToLive'));
  }

  /** The sandbox vanished (idle timeout, or restarted in another tab). */
  sandboxGone() {
    if (!this.active) return;
    this.ui.toast(t('tut.endedToast'), true);
    this.leave(t('tut.ended'));
  }

  async go(i, resumed = false) {
    if (i < 0 || i >= STEPS.length) return;
    this.i = i;
    const s = STEPS[i];
    if (resumed || !this.done.has(i - 1)) {
      try { await s.prepare?.(this); } catch { /* shown as unmet goal */ }
    }
    await s.enter?.(this);
    api('/api/tutorial/progress', { step: i }).catch(() => {});
    this.render();
  }

  async next() {
    const s = STEPS[this.i];
    if (s.last) {
      try { await api('/api/tutorial/complete', {}); } catch (e) { this.ui.toast(e.message, true); return; }
      await this.leave(t('tut.completedMsg'));
      this.ui.toast(t('tut.completed'));
      return;
    }
    this.go(this.i + 1);
  }

  async skip() {
    const s = STEPS[this.i];
    try { await s.skip?.(this); } catch { /* ignore */ }
    this.go(this.i + 1);
  }

  render() {
    const s = STEPS[this.i];
    const k = (part) => t(`tut.${s.id}.${part}`);
    $('tut-step').textContent = t('tut.lessonOf', { n: this.i + 1, total: STEPS.length });
    $('tut-title').textContent = k('title');
    $('tut-body').innerHTML = k('body');
    $('tut-goal').classList.toggle('hidden', !!s.info);
    $('tut-goal-text').textContent = s.info ? '' : k('goal');
    $('tut-how').innerHTML = s.info ? '' : (s.how ? s.how(this) : k('how'));
    $('tut-back').disabled = this.i === 0;
    $('tut-skip').classList.toggle('hidden', !!s.info);
    $('tut-show').classList.toggle('hidden', !s.showMe);
    $('tut-next').textContent = t(s.last ? 'tut.finish' : 'tut.next');
    $('tut-bar').style.width = `${((this.i + 1) / STEPS.length) * 100}%`;
    this.update(true);
  }

  /** Called a few times per second: evaluate the goal and keep the highlight in place. */
  update(force = false) {
    if (!this.active) return;
    const s = STEPS[this.i];
    let met = !!s.info;
    if (!met) {
      try { met = !!s.check(this); } catch { met = false; }
      if (met) this.done.add(this.i);
      met = met || this.done.has(this.i);
    }
    const goal = $('tut-goal');
    if (force || goal.classList.contains('met') !== met) {
      goal.classList.toggle('met', met);
      $('tut-next').disabled = !met;
      $('tut-next').classList.toggle('pulse', met && !s.info);
      if (met) {
        $('tutorial').classList.remove('min');
        // on phones the lists sheet covers the wizard: close it so the trainee sees the tick
        if (!force && !s.info && narrow() && document.body.classList.contains('lists-open')) this.ui.setLists(false);
      }
    }
    const failure = s.failed?.(this);
    $('tut-fail').innerHTML = failure ?? '';
    $('tut-fail').classList.toggle('hidden', !failure);
    $('tut-retry').classList.toggle('hidden', !failure || !s.retry);
    this.highlight(met ? null : (typeof s.target === 'function' ? s.target() : s.target));
  }

  highlight(selector) {
    // lists re-render (e.g. the Sectors tab when assignments change): re-apply if the mark is gone
    if (selector === this._hl && (!selector || document.querySelector('.tut-highlight'))) return;
    document.querySelectorAll('.tut-highlight').forEach((el) => el.classList.remove('tut-highlight'));
    this._hl = selector;
    if (!selector) return;
    const el = document.querySelector(selector);
    if (!el) { this._hl = undefined; return; }        // not rendered yet: retry on the next tick
    // controls that live in the compact header panel or the lists sheet must be visible first
    if (compact() && el.closest('#top-extra')) this.ui.setMore(true);
    if (narrow() && el.closest('#left')) this.ui.setLists(true);
    if (el.closest('.tab')) document.querySelector(`[data-tab=${el.closest('.tab').id.slice(4)}]`)?.click();
    const mark = el.closest('label.field') ?? el;
    mark.classList.add('tut-highlight');
    // long lists (e.g. Sectors): bring the control into view
    if (mark.closest('.tab-body')) mark.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }
}
