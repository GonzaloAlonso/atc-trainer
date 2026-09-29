import * as THREE from 'three';
import { api } from './net.js';
import { toWorld, FT } from './projection.js';
import { t, tm } from './i18n.js';

const COLORS = ['#5cc8ff', '#ffe14d', '#c7a4ff', '#9fb3c8'];
const RING_STEPS = 40;

/**
 * What-if in 3D: the predicted future of a decision option (hover it in the Decisions list) or
 * of a clearance you are thinking about (end a command with "?", or use the strip's ? mode).
 *
 * The server flies the aircraft and their neighbours ahead (the same fast-time prediction the
 * decision points use). Here the ghost tracks are drawn in the scene, projected from their 3D
 * positions (so they follow the vertical exaggeration), with the closest point of approach, a
 * ring of the horizontal minimum around it and the ±1000 ft band above and below.
 */
export class WhatIf {
  constructor(app, ui) {
    this.app = app;
    this.ui = ui;
    this.data = null;
    this.until = 0;
    this.sticky = false;
    this.req = 0;
    this.v = new THREE.Vector3();
  }

  clear() {
    this.data = null;
    clearTimeout(this._hoverT);
  }

  async showOption(dpId, optId, sticky = false) {
    clearTimeout(this._hoverT);
    const key = `${dpId}:${optId}`;
    if (this.data?.key === key) {
      if (sticky) { this.sticky = true; this.until = performance.now() + 20000; }
      return;
    }
    const n = ++this.req;
    this._hoverT = setTimeout(async () => {
      try {
        const d = await api(`/api/decisions/${dpId}/whatif?option=${encodeURIComponent(optId)}`);
        if (n !== this.req) return;
        this.set(d, key, sticky);
      } catch (e) {
        if (sticky) this.ui.toast(e.message, true);
      }
    }, sticky ? 0 : 160);
  }

  hoverEnd() {
    clearTimeout(this._hoverT);
    this.req++;
    if (!this.sticky) this._hoverT = setTimeout(() => { if (!this.sticky) this.data = null; }, 350);
  }

  async probe(body) {
    const n = ++this.req;
    try {
      const d = await api('/api/probe', body);
      if (n !== this.req) return;
      this.set(d, 'probe', true);
      const p = d.predicted;
      const what = `${d.aircraft} ${d.clearances.join(', ')}`;
      const msg = p.los
        ? t('whatif.probeLos', { what, others: d.conflicts_with.join(', ') || '—', t: p.first_los_s })
        : t('whatif.probeOk', { what, h: p.min_h_nm ?? p.cpa?.h ?? '—' });
      this.ui.toast(msg, p.los);
    } catch (e) {
      this.ui.toast(e.message, true);
    }
  }

  set(d, key, sticky) {
    d.key = key;
    this.data = d;
    this.sticky = sticky;
    this.until = performance.now() + (sticky ? 20000 : 600000);
  }

  project(overlay, camera, lat, lon, altFt) {
    return overlay.project(toWorld(lat, lon, altFt * FT, this.v), camera);
  }

  /** Called by the overlay every frame. */
  draw(ctx, overlay, camera, time) {
    const d = this.data;
    if (!d) return;
    if (performance.now() > this.until) { this.data = null; return; }
    const subjects = d.subjects ?? [];
    const ids = Object.keys(d.tracks);
    ids.sort((a, b) => (subjects.indexOf(a) === -1 ? 9 : subjects.indexOf(a)) - (subjects.indexOf(b) === -1 ? 9 : subjects.indexOf(b)));
    const pulse = 0.65 + 0.35 * Math.sin(time / 260);
    ctx.save();
    ctx.lineWidth = 2;
    ids.forEach((id, k) => {
      const color = COLORS[Math.min(k, COLORS.length - 1)];
      const pts = d.tracks[id];
      ctx.strokeStyle = color;
      ctx.globalAlpha = 0.9;
      ctx.setLineDash([7, 5]);
      ctx.beginPath();
      let started = false;
      for (const [lat, lon, alt] of pts) {
        const p = this.project(overlay, camera, lat, lon, alt);
        if (!p) { started = false; continue; }
        if (!started) { ctx.moveTo(p[0], p[1]); started = true; } else ctx.lineTo(p[0], p[1]);
      }
      ctx.stroke();
      ctx.setLineDash([]);
      // one dot per minute ahead
      ctx.fillStyle = color;
      pts.forEach(([lat, lon, alt], i) => {
        if (i === 0 || i % Math.round(60 / (d.step_s || 10)) !== 0) return;
        const p = this.project(overlay, camera, lat, lon, alt);
        if (p) { ctx.beginPath(); ctx.arc(p[0], p[1], 2.6, 0, Math.PI * 2); ctx.fill(); }
      });
      const end = pts[pts.length - 1];
      const pe = this.project(overlay, camera, end[0], end[1], end[2]);
      if (pe) {
        ctx.font = "10px 'JetBrains Mono', monospace";
        ctx.fillText(`${d.names[id] ?? id} ${String(Math.round(end[2] / 100)).padStart(3, '0')}`, pe[0] + 6, pe[1] - 4);
      }
    });

    // closest point of approach: both positions, the minimum around it and the vertical band
    const cpa = d.cpa;
    if (cpa && d.cpa_at) {
      const [pa, pb] = d.cpa_at;
      const hMin = d.minima?.h_nm ?? 5;
      const safe = !(d.predicted?.los);
      const col = safe ? '#4dff9a' : '#ff3b3b';
      ctx.globalAlpha = 0.85 * pulse;
      ctx.strokeStyle = col;
      ctx.lineWidth = 1.5;
      for (const dAlt of [0, 1000, -1000]) {
        ctx.globalAlpha = (dAlt === 0 ? 0.9 : 0.35) * pulse;
        ctx.setLineDash(dAlt === 0 ? [] : [3, 4]);
        ctx.beginPath();
        let started = false;
        for (let k = 0; k <= RING_STEPS; k++) {
          const b = (k / RING_STEPS) * Math.PI * 2;
          const lat = pa[0] + (hMin * Math.cos(b)) / 60;
          const lon = pa[1] + (hMin * Math.sin(b)) / (60 * Math.cos(pa[0] * Math.PI / 180));
          const p = this.project(overlay, camera, lat, lon, pa[2] + dAlt);
          if (!p) { started = false; continue; }
          if (!started) { ctx.moveTo(p[0], p[1]); started = true; } else ctx.lineTo(p[0], p[1]);
        }
        ctx.stroke();
      }
      ctx.setLineDash([]);
      const p1 = this.project(overlay, camera, pa[0], pa[1], pa[2]);
      const p2 = this.project(overlay, camera, pb[0], pb[1], pb[2]);
      ctx.globalAlpha = 1;
      if (p1 && p2) {
        ctx.beginPath(); ctx.moveTo(p1[0], p1[1]); ctx.lineTo(p2[0], p2[1]); ctx.stroke();
        for (const p of [p1, p2]) { ctx.beginPath(); ctx.arc(p[0], p[1], 5, 0, Math.PI * 2); ctx.stroke(); }
        const mm = Math.floor(cpa.t / 60), ss = String(Math.round(cpa.t % 60)).padStart(2, '0');
        const txt = t('whatif.cpa', { time: `${mm}:${ss}`, h: cpa.h, v: cpa.v }) + (safe ? ' ✓' : ' ✗');
        ctx.font = "11px 'JetBrains Mono', monospace";
        const w = ctx.measureText(txt).width + 10;
        const mx = (p1[0] + p2[0]) / 2 + 10, my = (p1[1] + p2[1]) / 2 - 18;
        ctx.fillStyle = 'rgba(6,10,18,0.85)';
        ctx.fillRect(mx - 5, my - 12, w, 17);
        ctx.strokeStyle = col;
        ctx.strokeRect(mx - 5.5, my - 12.5, w + 1, 18);
        ctx.fillStyle = col;
        ctx.fillText(txt, mx, my);
      }
    }
    // the option's explanation, top of the scene
    const head = d.label ? `${d.label}${d.why ? ' — ' + tm(d.why) : ''}` : (d.clearances ? `${d.aircraft} ${d.clearances.join(', ')}?` : '');
    if (head) {
      ctx.font = "12px Inter, sans-serif";
      const w = Math.min(ctx.measureText(head).width + 20, overlay.w - 40);
      const x = (overlay.w - w) / 2, y = 64;
      ctx.globalAlpha = 0.92;
      ctx.fillStyle = 'rgba(9,15,25,0.9)';
      ctx.fillRect(x, y, w, 24);
      ctx.strokeStyle = 'rgba(92,200,255,0.45)';
      ctx.strokeRect(x + 0.5, y + 0.5, w - 1, 23);
      ctx.fillStyle = '#dfe8f5';
      ctx.fillText(head, x + 10, y + 16, w - 20);
    }
    ctx.restore();
  }
}
