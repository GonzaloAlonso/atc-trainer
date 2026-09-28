import { api, setContext } from './net.js';
import { F } from './traffic.js';

const $ = (id) => document.getElementById(id);

// Aircraft of the scripted training scenario (atc/training.py)
const TRN303 = '7a0303';
const PAIR = ['7c0101', '7c0202'];
const SECTOR = 'ALPS-UPPER';
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
const STEPS = [
  {
    title: 'Welcome to your practice sector',
    body: `<p>As an air traffic controller your job is to keep every aircraft in your sector
      <b>safely separated</b> (at least <b>5 NM</b> horizontally or <b>1000 ft</b> vertically) while
      getting them where they want to go <b>efficiently</b>.</p>
      <p>This tutorial runs in a <b>private training sector</b>: the traffic is scripted and nothing you do
      affects other controllers. It takes about 10–15 minutes. You can leave at any time and resume later.</p>`,
    info: true,
  },
  {
    title: 'Find your way around the scope',
    body: '<p>The scope is a 3D map of Europe. Aircraft carry a <b>data block</b> (label) with their details.</p>',
    goal: 'Zoom in on the Alps so the training traffic fills your screen.',
    how: `<ul><li><b>Pan</b>: drag with the left mouse button (one finger on touch screens).</li>
      <li><b>Zoom</b>: mouse wheel (pinch on touch screens).</li>
      <li><b>Rotate / tilt</b>: drag with the right mouse button (two fingers on touch screens).</li></ul>`,
    check: (t) => { const v = t.app.cameraInfo(); return v.distance < 900 && v.lat > 44.5 && v.lat < 49.5 && v.lon > 5 && v.lon < 15; },
    showMe: (t) => t.app.flyTo(46.9, 9.8, 700),
  },
  {
    title: 'Take responsibility for a sector',
    body: `<p>Controllers work a <b>sector</b>: a volume of airspace with lateral limits and a floor and ceiling.
      Once you take one, its traffic checks in on the radio and counts for your score.</p>`,
    goal: 'Take the “Alps Upper” sector.',
    how: () => `<p>Choose <b>Alps Upper</b> in the <b>Sector</b> menu${compact() ? ' (open the <b>⋯</b> button in the header)' : ' in the top bar'}.</p>`,
    target: '#sector',
    check: (t) => t.frame?.sector === SECTOR,
    skip: (t) => api('/api/sector', { sector: SECTOR }),
    prepare: (t) => (t.frame?.sector === SECTOR ? null : api('/api/sector', { sector: SECTOR })),
  },
  {
    title: 'Read a data block',
    body: `<p>Each data block shows, line by line:</p>
      <ul><li><b>Callsign</b> (e.g. <code>TRN303</code>), plus <b>H</b> for heavy aircraft.</li>
      <li><b>Flight level</b> (<code>330</code> = 33,000 ft), a trend arrow ↑/↓ when climbing or descending, then the <b>cleared level</b>.</li>
      <li><b>Ground speed</b> in knots and any lateral instruction (heading or direct-to).</li></ul>
      <p>The line ahead of each aircraft is its <b>speed vector</b>: where it will be in 2 minutes.</p>`,
    goal: 'Select TRN303, your practice aircraft.',
    how: '<p>Click (tap) the aircraft or its data block. Its <b>flight strip</b> opens with all its details and the clearance controls.</p>',
    check: (t) => t.app.selectedId === TRN303,
    showMe: (t) => t.app.select(TRN303, true),
  },
  {
    title: 'Give a heading',
    body: `<p>Clearances are instructions to a pilot. You can type them on the <b>command line</b> using ATC shorthand.
      The pilot reads the clearance back on the radio and executes it after a few seconds.</p>`,
    goal: 'Turn TRN303 right by 20 degrees.',
    how: `<p>Click the command line (or press <kbd>/</kbd>) and type <code>TRN303 TR 20D</code>, then Enter.
      With TRN303 selected you can omit the callsign: <code>TR 20D</code>.</p>
      <p class="dim">Other forms: <code>TL 270</code> turn left heading 270 · <code>H 090</code> fly heading 090.</p>`,
    target: '#cmd',
    check: (t) => { const r = t.rec(TRN303); return !!r && (r.ahdg != null || r.lateral?.startsWith('H')); },
  },
  {
    title: 'Proceed direct to a fix',
    body: '<p>Instead of vectoring, you can send an aircraft straight to a <b>navaid</b> or <b>airport</b>. After passing it, the aircraft rejoins its route.</p>',
    goal: 'Clear TRN303 direct to a nearby navaid.',
    how: `<p>In TRN303's flight strip, pick a fix from the <b>Direct</b> list and press <b>DCT</b>.
      Or type <code>TRN303 DCT</code> followed by the fix identifier.</p>`,
    target: '#c-dct',
    check: (t) => !!t.rec(TRN303)?.dct,
    enter: (t) => { if (t.app.selectedId !== TRN303) t.app.select(TRN303); },
  },
  {
    title: 'Resume own navigation',
    body: '<p>When you no longer need an aircraft off its route, let the crew continue on their own.</p>',
    goal: 'Tell TRN303 to resume own navigation.',
    how: '<p>Press <b>Resume own nav</b> in the flight strip, or type <code>TRN303 RON</code>.</p>',
    target: '#c-ron',
    check: (t) => { const r = t.rec(TRN303); return !!r && r.dct == null && r.ahdg == null && (r.flags & F.HUMAN); },
    enter: (t) => { if (t.app.selectedId !== TRN303) t.app.select(TRN303); },
  },
  {
    title: 'Change level',
    body: '<p>Vertical clearances are the most common tool: climbing or descending an aircraft by 1000 ft is often enough to keep it clear of others.</p>',
    goal: 'Climb TRN303 to flight level 370.',
    how: `<p>In the flight strip set <b>Level</b> to <code>370</code> (use + / −) and press <b>Climb</b>.
      Or type <code>TRN303 C 370</code>.</p>`,
    target: '#c-fl',
    check: (t) => t.rec(TRN303)?.cfl === 370,
    enter: (t) => { if (t.app.selectedId !== TRN303) t.app.select(TRN303); },
  },
  {
    title: 'Spot a conflict',
    body: `<p>Two new aircraft, <b>TRN101</b> and <b>TRN202</b>, are flying towards each other at the same level.
      The <b>short-term conflict alert (STCA)</b> predicts losses of separation up to 2 minutes ahead:
      the aircraft turn <b style="color:var(--warn)">amber</b> and the alert appears in the <b>Alerts</b> list.</p>`,
    goal: 'Wait for the STCA between TRN101 and TRN202, then select one of them.',
    how: () => `<p>Open the <b>Alerts</b> ${narrow() ? 'list with the <b>☰</b> button' : 'tab on the left'}
      and click the alert. It appears in about 2 minutes. To wait less, set <b>2×</b> or <b>4×</b> speed${compact() ? ' (in the <b>⋯</b> panel)' : ''}.</p>`,
    target: () => (narrow() ? '#btn-lists' : '[data-tab=alerts]'),
    check: (t) => t.pairInConflict() && PAIR.includes(t.app.selectedId),
    enter: (t) => {
      if (!t.pairPresent()) api('/api/tutorial/scenario', { event: 'conflict' }).catch(() => {});
      t.app.flyTo(46.85, 11.2, 650);
    },
    prepare: (t) => (t.pairPresent() ? null : api('/api/tutorial/scenario', { event: 'conflict' })),
  },
  {
    title: 'Resolve the conflict',
    body: `<p>Separate them before they get closer than 5 NM at the same level.
      A <b>vertical</b> solution is usually the simplest: move one of them 1000 ft up or down.</p>`,
    goal: 'Give TRN101 or TRN202 a new level and keep them separated until they have passed.',
    how: `<p>Select one and type e.g. <code>TRN101 C 360</code> or <code>TRN202 D 340</code>.
      The <b>Decisions</b> tab also offers ready-made options, each with its predicted outcome.
      The goal is reached once they have passed each other <b>without</b> a loss of separation.</p>`,
    target: '#cmd',
    check: (t) => {
      const cleared = PAIR.some((id) => { const r = t.rec(id); return r && (r.cfl != null || r.ahdg != null); });
      if (t.pairInConflict()) t.state.sawConflict = true;
      return t.state.sawConflict && cleared && !t.pairInConflict() && t.pairPassed()
        && (t.frame?.score?.los ?? 0) === 0;
    },
    failed: (t) => ((t.frame?.score?.los ?? 0) > 0
      ? 'Separation was lost. Press <b>Replay situation</b> to try again: act as soon as the STCA appears.' : null),
    retry: (t) => { t.state.sawConflict = false; return api('/api/tutorial/scenario', { event: 'conflict' }); },
    prepare: (t) => (t.pairPresent() ? null : api('/api/tutorial/scenario', { event: 'conflict' })),
  },
  {
    title: 'Get help from the decision assistant',
    body: `<p>For every conflict and pilot request the simulator builds a <b>decision point</b>: candidate clearances,
      each tested by flying the traffic a few minutes ahead (✓ separated, ✗ loss of separation).</p>
      <p>An <b>AI</b> can recommend (<b>Advisory</b>) or act on its own (<b>Autonomous</b>). This is the interface a decision-making AI such as Jev plugs into.</p>`,
    goal: 'Switch the AI to Advisory mode.',
    how: () => `<p>Set <b>AI</b> to <b>Advisory</b>${compact() ? ' in the <b>⋯</b> panel' : ' in the top bar'}.
      From now on its suggestion is highlighted in the <b>Decisions</b> list; you still decide with <b>Accept</b>.</p>`,
    target: '#ai-mode',
    check: (t) => t.frame?.ai?.mode === 'advisory',
  },
  {
    title: 'Answer a pilot request',
    body: `<p>Pilots ask for what they need. TRN303 is at FL370, but its flight plan is at FL330,
      so the crew will soon request a descent. Unanswered requests expire and cost points.</p>`,
    goal: 'Grant TRN303’s request to descend to FL330.',
    how: () => `<p>When “TRN303, request descent FL330” appears on the radio, open <b>Decisions</b>
      ${narrow() ? '(☰ button)' : 'on the left'} and press <b>Issue</b> on the approve option (or <b>Accept</b> the AI suggestion).
      You can also type <code>TRN303 D 330</code>. Speed up time if you are waiting.</p>`,
    target: () => (narrow() ? '#btn-lists' : '[data-tab=decisions]'),
    check: (t) => t.rec(TRN303)?.cfl === 330
      || (t.frame?.decisions ?? []).some((d) => d.kind === 'level_request' && d.subjects.includes(TRN303) && d.status === 'executed'),
    prepare: (t) => (t.rec(TRN303)?.cfl === 370 ? null : api('/api/command', { text: 'TRN303 C 370' })),
  },
  {
    title: 'Time and score',
    body: `<p>The score rewards safe, efficient work: <b>+2</b> per aircraft handled, <b>+10</b> per request granted,
      <b>−2</b> per STCA, <b>−10</b> per ignored request and <b>−50</b> per loss of separation.</p>
      <p>Live traffic always runs in real time. In <b>Replay</b> scenarios (recorded traffic) you can speed up to 16×.</p>`,
    goal: 'Speed up the simulation to 2× or more.',
    how: () => `<p>Use the <b>1× 2× 4× 8× 16×</b> buttons${compact() ? ' in the <b>⋯</b> panel' : ' next to pause'}.
      <kbd>Space</kbd> pauses and resumes.</p>`,
    target: '#speed-seg',
    check: (t) => (t.frame?.speed ?? 1) >= 2,
  },
  {
    title: 'You are ready',
    body: `<p>Well done: you have navigated the scope, issued headings, direct-tos and level changes,
      resolved a conflict, used the decision assistant and answered a pilot request.</p>
      <p>Next: pick a sector with live or replayed traffic and keep it safe. The tutorial is always available
      from the account menu (<b>Tutorial</b>).</p>`,
    info: true,
    last: true,
  },
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
      this.ui.toast('The tutorial stays available in the account menu.');
    };
    const open = () => {
      $('user-pop').classList.add('hidden');
      if (this.active) { $('tutorial').classList.remove('min'); return; }
      // unfinished: resume where they left off; completed or declined: start over
      this.start(this.me?.tutorial_state == null ? this.me?.tutorial_step || 0 : 0);
    };
    $('menu-tutorial').onclick = open;
    $('btn-tutorial').onclick = open;
  }

  async refreshMe() {
    try { this.me = await api('/api/auth/me'); } catch { return; }
    $('tut-pill').classList.toggle('hidden', this.me.tutorial_state === 'completed');
    $('btn-tutorial').classList.toggle('recommended', this.me.tutorial_state !== 'completed');
  }

  /** At sign-in: offer the tutorial until completed or declined for good. */
  async offer() {
    await this.refreshMe();
    if (!this.me || this.me.tutorial_state != null) return;
    const step = this.me.tutorial_step || 0;
    $('tut-start').textContent = step > 0 ? `Resume at lesson ${step + 1}` : 'Start the tutorial';
    $('tut-offer-resume').classList.toggle('hidden', step === 0);
    $('tut-offer').showModal();
  }

  async start(step = 0) {
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
    this.ui.resetForContext('Training session started in your private Alps Upper sector.');
    this.app.select(null);
    this.feed.reconnect();
    this.app.flyTo(46.9, 9.8, 2400);
    $('tutorial').classList.remove('hidden', 'min');
    await this.go(Math.min(step, STEPS.length - 1), true);
  }

  async leave(message) {
    this.active = false;
    this.highlight(null);
    document.body.classList.remove('training');
    $('tutorial').classList.add('hidden');
    setContext(null);
    this.ui.resetForContext(message);
    this.app.select(null);
    this.feed.reconnect();
    this.refreshMe();
  }

  async exit() {
    if (!this.active) return;
    await api('/api/tutorial/stop', {}).catch(() => {});
    await this.leave('Back to live traffic. Resume the tutorial from the account menu.');
  }

  /** The sandbox vanished (idle timeout, or restarted in another tab). */
  sandboxGone() {
    if (!this.active) return;
    this.ui.toast('Training session ended. Resume it from the account menu.', true);
    this.leave('Training session ended.');
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
      await this.leave('Tutorial completed. Welcome aboard, controller!');
      this.ui.toast('Tutorial completed');
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
    const html = (v) => (typeof v === 'function' ? v(this) : v ?? '');
    $('tut-step').textContent = `Lesson ${this.i + 1} of ${STEPS.length}`;
    $('tut-title').textContent = s.title;
    $('tut-body').innerHTML = html(s.body);
    $('tut-goal').classList.toggle('hidden', !!s.info);
    $('tut-goal-text').textContent = s.goal ?? '';
    $('tut-how').innerHTML = html(s.how);
    $('tut-back').disabled = this.i === 0;
    $('tut-skip').classList.toggle('hidden', !!s.info);
    $('tut-show').classList.toggle('hidden', !s.showMe);
    $('tut-next').textContent = s.last ? 'Finish' : 'Next';
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
      if (met) $('tutorial').classList.remove('min');
    }
    const failure = s.failed?.(this);
    $('tut-fail').innerHTML = failure ?? '';
    $('tut-fail').classList.toggle('hidden', !failure);
    $('tut-retry').classList.toggle('hidden', !failure || !s.retry);
    this.highlight(met ? null : (typeof s.target === 'function' ? s.target() : s.target));
  }

  highlight(selector) {
    if (selector === this._hl) return;
    document.querySelectorAll('.tut-highlight').forEach((el) => el.classList.remove('tut-highlight'));
    this._hl = selector;
    if (!selector) return;
    const el = document.querySelector(selector);
    if (!el) return;
    // controls that live in the compact header panel or the lists sheet must be visible first
    if (compact() && el.closest('#top-extra')) this.ui.setMore(true);
    if (narrow() && el.closest('#left')) this.ui.setLists(true);
    if (el.closest('.tab')) document.querySelector(`[data-tab=${el.closest('.tab').id.slice(4)}]`)?.click();
    (el.closest('label.field') ?? el).classList.add('tut-highlight');
  }
}
