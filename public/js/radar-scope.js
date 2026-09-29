/**
 * Animated radar scope (PPI) for the sign-in page, drawn on a canvas.
 *
 * The scope is centred on the page and never moves; only the beam rotates clockwise around the
 * centre, with a bright leading edge and a fading afterglow behind it. Targets light up as the
 * beam passes over them and fade out afterwards, like phosphor persistence on a real radar.
 */

const ACCENT = [92, 200, 255];
const PERIOD_S = 6;          // one full rotation
const AFTERGLOW = 1.1;       // radians of trailing glow behind the beam
const PERSIST_S = 4.5;       // how long a target stays visible after being painted

const rgba = (a) => `rgba(${ACCENT[0]},${ACCENT[1]},${ACCENT[2]},${a})`;

export function startRadar(canvas) {
  const ctx = canvas.getContext('2d');
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let w = 0, h = 0, cx = 0, cy = 0, rMax = 0, rScale = 0, dpr = 1;
  let targets = [];
  let raf = 0, last = 0;

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = window.innerWidth; h = window.innerHeight;
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    canvas.style.width = w + 'px'; canvas.style.height = h + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    cx = w / 2; cy = h / 2;
    rMax = Math.hypot(w, h) / 2;                  // beam reaches every corner
    rScale = Math.min(w, h) * 0.46;               // radius of the bearing scale
    seedTargets();
  }

  function seedTargets() {
    // pseudo-random but stable positions (same scope on every visit)
    let s = 7;
    const rnd = () => ((s = (s * 16807) % 2147483647) / 2147483647);
    targets = Array.from({ length: 26 }, () => ({
      r: (0.18 + 0.8 * rnd()) * rMax,
      a: rnd() * Math.PI * 2,
      drift: (rnd() - 0.5) * 0.004,               // slow movement between sweeps
      painted: -1e9,
    }));
  }

  // bearing 0 = north (up), clockwise; canvas angles start at east and also run clockwise
  const toCanvas = (bearing) => bearing - Math.PI / 2;

  function drawStatic() {
    const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, rMax);
    g.addColorStop(0, '#0b1a2c');
    g.addColorStop(0.7, '#050b14');
    g.addColorStop(1, '#03070d');
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, w, h);

    // range rings
    ctx.lineWidth = 1;
    const step = Math.max(80, rScale / 4);
    for (let r = step; r < rMax; r += step) {
      ctx.strokeStyle = rgba(r <= rScale + 1 ? 0.13 : 0.07);
      ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();
    }
    // axes
    ctx.strokeStyle = rgba(0.07);
    ctx.beginPath();
    ctx.moveTo(cx - rMax, cy); ctx.lineTo(cx + rMax, cy);
    ctx.moveTo(cx, cy - rMax); ctx.lineTo(cx, cy + rMax);
    ctx.stroke();
    // bearing scale: ticks every 10°, labels every 30°
    ctx.font = "10px 'JetBrains Mono', ui-monospace, monospace";
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    for (let d = 0; d < 360; d += 5) {
      const a = toCanvas((d * Math.PI) / 180);
      const len = d % 30 === 0 ? 10 : d % 10 === 0 ? 6 : 3;
      ctx.strokeStyle = rgba(d % 30 === 0 ? 0.35 : 0.18);
      ctx.beginPath();
      ctx.moveTo(cx + Math.cos(a) * rScale, cy + Math.sin(a) * rScale);
      ctx.lineTo(cx + Math.cos(a) * (rScale - len), cy + Math.sin(a) * (rScale - len));
      ctx.stroke();
      if (d % 30 === 0) {
        ctx.fillStyle = rgba(0.4);
        ctx.fillText(String(d).padStart(3, '0'), cx + Math.cos(a) * (rScale + 14), cy + Math.sin(a) * (rScale + 14));
      }
    }
  }

  function frame(now) {
    raf = requestAnimationFrame(frame);
    if (now - last < 33) return;                   // ~30 fps is plenty for a background
    last = now;
    const t = now / 1000;
    const beam = ((t / PERIOD_S) % 1) * Math.PI * 2;   // bearing of the leading edge

    drawStatic();

    // afterglow behind the beam, fading with angular distance
    if (ctx.createConicGradient) {
      const g = ctx.createConicGradient(toCanvas(beam - AFTERGLOW), cx, cy);
      const end = AFTERGLOW / (Math.PI * 2);
      for (let i = 0; i <= 8; i++) g.addColorStop((i / 8) * end, rgba(0.16 * (i / 8) ** 2));
      g.addColorStop(Math.min(1, end + 0.001), rgba(0));
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, w, h);
    } else {
      const slices = 24;
      for (let i = 0; i < slices; i++) {
        ctx.fillStyle = rgba(0.16 * (1 - i / slices) ** 2);
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.arc(cx, cy, rMax, toCanvas(beam - ((i + 1) / slices) * AFTERGLOW), toCanvas(beam - (i / slices) * AFTERGLOW));
        ctx.closePath();
        ctx.fill();
      }
    }
    // the beam itself: a bright line from the centre
    const ab = toCanvas(beam);
    ctx.save();
    ctx.shadowColor = rgba(0.9); ctx.shadowBlur = 12;
    ctx.strokeStyle = rgba(0.85); ctx.lineWidth = 1.6;
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx + Math.cos(ab) * rMax, cy + Math.sin(ab) * rMax); ctx.stroke();
    ctx.restore();

    // targets: painted when the beam passes, then fading
    for (const p of targets) {
      p.a = (p.a + p.drift / 30 + Math.PI * 2) % (Math.PI * 2);
      const behind = (beam - p.a + Math.PI * 2) % (Math.PI * 2);
      if (behind < 0.06) p.painted = t;
      const age = t - p.painted;
      if (age > PERSIST_S) continue;
      const k = Math.exp(-age / (PERSIST_S / 3));
      const x = cx + Math.cos(toCanvas(p.a)) * p.r, y = cy + Math.sin(toCanvas(p.a)) * p.r;
      ctx.fillStyle = rgba(0.85 * k);
      ctx.beginPath(); ctx.arc(x, y, 2.4, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = rgba(0.18 * k);
      ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.fill();
    }

    // the centre of the scope: fixed
    ctx.fillStyle = rgba(0.9);
    ctx.beginPath(); ctx.arc(cx, cy, 2.5, 0, Math.PI * 2); ctx.fill();
  }

  resize();
  window.addEventListener('resize', resize);
  if (reduced) {
    drawStatic();
    return;
  }
  raf = requestAnimationFrame(frame);
  document.addEventListener('visibilitychange', () => {
    cancelAnimationFrame(raf);
    if (!document.hidden) raf = requestAnimationFrame(frame);
  });
}
