import * as THREE from 'three';
import { toWorld, FT } from './projection.js';
import { F, PALETTE } from './traffic.js';

const FONT = "11px 'JetBrains Mono', ui-monospace, Menlo, monospace";
const FONT_SMALL = "10px 'JetBrains Mono', ui-monospace, Menlo, monospace";
const LINE_H = 13;
const MAX_LABELS = 260;

/** 2D canvas layer on top of the WebGL scene for crisp ATC symbology and data blocks. */
export class Overlay {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.v = new THREE.Vector3();
    this.labels = [];      // hit boxes: {id, x, y, w, h}
    this.fixes = [];       // [{ident, kind, name, world}]
    this.options = { labelsAll: false };
    this.resize();
  }

  resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.w = window.innerWidth; this.h = window.innerHeight;
    this.canvas.width = this.w * dpr; this.canvas.height = this.h * dpr;
    this.canvas.style.width = this.w + 'px'; this.canvas.style.height = this.h + 'px';
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  setFixes(fixes) {
    this.fixes = fixes.map(([ident, kind, lat, lon, name]) => ({
      ident, kind, name, lat, lon, world: toWorld(lat, lon, 0),
    }));
  }

  refreshFixes() { for (const f of this.fixes) toWorld(f.lat, f.lon, 0, f.world); }

  project(world, camera) {
    const v = this.v.copy(world).project(camera);
    if (v.z > 1 || v.x < -1.2 || v.x > 1.2 || v.y < -1.2 || v.y > 1.2) return null;
    return [(v.x + 1) * 0.5 * this.w, (1 - v.y) * 0.5 * this.h];
  }

  draw(s) {
    const { ctx } = this;
    const { camera, store, selectedId, hoverId, conflicts, route, distance, time } = s;
    ctx.clearRect(0, 0, this.w, this.h);
    this.labels = [];

    // ---- navaids & airports
    if (distance < 2600) {
      ctx.font = FONT_SMALL;
      ctx.lineWidth = 1;
      for (const f of this.fixes) {
        const big = f.kind === 'APT_L';
        if (!big && distance > 1300) continue;
        if (f.kind === 'NDB' && distance > 500) continue;
        const p = this.project(f.world, camera);
        if (!p) continue;
        const [x, y] = p;
        const showName = distance < (big ? 1600 : 700);
        if (f.kind === 'VOR' || f.kind === 'DME') {
          ctx.strokeStyle = 'rgba(120,200,255,0.75)';
          ctx.beginPath();
          for (let k = 0; k < 6; k++) {
            const a = (k / 6) * Math.PI * 2;
            ctx[k ? 'lineTo' : 'moveTo'](x + Math.cos(a) * 5, y + Math.sin(a) * 5);
          }
          ctx.closePath(); ctx.stroke();
          ctx.fillStyle = 'rgba(120,200,255,0.9)';
          ctx.fillRect(x - 1, y - 1, 2, 2);
          if (showName) ctx.fillText(f.ident, x + 7, y + 3);
        } else if (f.kind === 'NDB') {
          ctx.fillStyle = 'rgba(200,150,255,0.7)';
          ctx.beginPath(); ctx.arc(x, y, 2.5, 0, Math.PI * 2); ctx.fill();
          if (showName) ctx.fillText(f.ident, x + 5, y + 3);
        } else {
          ctx.strokeStyle = big ? 'rgba(255,220,140,0.9)' : 'rgba(255,220,140,0.6)';
          ctx.beginPath(); ctx.arc(x, y, big ? 4.5 : 3, 0, Math.PI * 2); ctx.stroke();
          ctx.beginPath(); ctx.moveTo(x, y - 7); ctx.lineTo(x, y + 7); ctx.stroke();
          if (showName) { ctx.fillStyle = 'rgba(255,220,140,0.9)'; ctx.fillText(f.ident, x + 7, y - 4); }
        }
      }
    }

    // ---- aircraft screen positions
    for (const r of store.list) {
      const p = this.project(r.world, camera);
      r.onScreen = !!p;
      if (p) { r.sx = p[0]; r.sy = p[1]; }
    }

    // ---- selected route (planned trajectory) and direct-to leg
    if (route && route.points?.length) {
      const sel = store.map.get(selectedId);
      ctx.save();
      ctx.setLineDash([6, 5]);
      ctx.strokeStyle = 'rgba(92,200,255,0.85)';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      let started = false;
      if (sel?.onScreen) { ctx.moveTo(sel.sx, sel.sy); started = true; }
      const w = new THREE.Vector3();
      for (const [lat, lon, alt] of route.points) {
        const p = this.project(toWorld(lat, lon, alt * FT, w), camera);
        if (!p) { started = false; continue; }
        if (!started) { ctx.moveTo(p[0], p[1]); started = true; } else ctx.lineTo(p[0], p[1]);
      }
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = 'rgba(92,200,255,0.9)';
      for (const [lat, lon, alt] of route.points) {
        const p = this.project(toWorld(lat, lon, alt * FT, w), camera);
        if (p) ctx.fillRect(p[0] - 1.5, p[1] - 1.5, 3, 3);
      }
      if (route.dct && sel?.onScreen) {
        const p = this.project(toWorld(route.dct.lat, route.dct.lon, 0, w), camera);
        if (p) {
          ctx.strokeStyle = 'rgba(255,120,220,0.95)';
          ctx.beginPath(); ctx.moveTo(sel.sx, sel.sy); ctx.lineTo(p[0], p[1]); ctx.stroke();
          ctx.beginPath(); ctx.arc(p[0], p[1], 7, 0, Math.PI * 2); ctx.stroke();
        }
      }
      ctx.restore();
    }

    // ---- conflicts
    const blink = Math.floor(time / 400) % 2 === 0;
    ctx.font = FONT_SMALL;
    for (const [a, b, kind, tTo, h, v, relevant] of conflicts) {
      const ra = store.map.get(a), rb = store.map.get(b);
      if (!ra?.onScreen || !rb?.onScreen) continue;
      if (!relevant && distance > 900) continue;
      const col = kind === 'LOS' ? PALETTE.los : PALETTE.stca;
      ctx.strokeStyle = col;
      ctx.globalAlpha = relevant ? 1 : 0.45;
      ctx.lineWidth = 1.5;
      ctx.setLineDash(kind === 'LOS' ? [] : [4, 4]);
      ctx.beginPath(); ctx.moveTo(ra.sx, ra.sy); ctx.lineTo(rb.sx, rb.sy); ctx.stroke();
      ctx.setLineDash([]);
      if (relevant) {
        const mx = (ra.sx + rb.sx) / 2, my = (ra.sy + rb.sy) / 2;
        const txt = kind === 'LOS' ? `LOS ${h.toFixed(1)}NM ${v}ft` : `${Math.round(tTo)}s ${h.toFixed(1)}NM`;
        ctx.fillStyle = 'rgba(8,12,20,0.8)';
        ctx.fillRect(mx - 2, my - 10, ctx.measureText(txt).width + 4, 13);
        ctx.fillStyle = col;
        ctx.fillText(txt, mx, my);
      }
      ctx.globalAlpha = 1;
    }

    // ---- data blocks
    const zoomedIn = distance < 650;
    const cand = [];
    for (const r of store.list) {
      if (!r.onScreen) continue;
      const important = r.flags & (F.SECTOR | F.HUMAN | F.AI | F.LOS | F.STCA | F.REQUEST);
      const wanted = r.id === selectedId || important || zoomedIn || this.options.labelsAll;
      if (!wanted && r.id !== hoverId) continue;
      // Hovering only adds a label; it never reorders others (that would move the label away
      // from the pointer, end the hover and snap everything back: a flicker loop).
      const pr = r.id === selectedId ? 0 : (r.flags & (F.LOS | F.STCA)) ? 1
        : (r.flags & (F.HUMAN | F.AI | F.REQUEST)) ? 2 : (r.flags & F.SECTOR) ? 3 : wanted ? 4 : 5;
      // in tilted views, don't pile labels of far-away traffic up on the horizon
      if (pr >= 2 && camera.position.distanceTo(r.world) > distance * 2.2) continue;
      cand.push([pr, r]);
    }
    // stable order: priority, then id (ties must not reshuffle from frame to frame)
    cand.sort((a, b) => a[0] - b[0] || (a[1].id < b[1].id ? -1 : a[1].id > b[1].id ? 1 : 0));
    ctx.font = FONT;
    const n = Math.min(cand.length, MAX_LABELS);
    const placed = [];
    const drawn = [];
    for (let i = 0; i < n; i++) {
      const [pr, r] = cand[i];
      const dim = pr >= 4 && r.id !== hoverId;
      const fl = String(Math.round(r.pAlt / 100)).padStart(3, '0');
      const trend = r.vs > 250 ? '↑' : r.vs < -250 ? '↓' : ' ';
      const l1 = r.cs + (r.cls === 'H' ? ' H' : '');
      const l2 = `${fl}${trend}${r.cfl != null ? String(r.cfl).padStart(3, '0') : ''}`;
      const lat = r.lateral !== 'ROUTE' ? ' ' + r.lateral : '';
      const l3 = `${String(r.gs).padStart(3, ' ')}${lat}${r.spd ? ' S' + r.spd : ''}`;
      const lines = dim ? [l1, l2] : [l1, l2, l3];
      // quantised width: a changing digit or trend arrow must not change the box size
      const tw = Math.ceil(Math.max(...lines.map((l) => ctx.measureText(l).width)) / 14) * 14;
      const bw = tw + 6, bh = lines.length * LINE_H + 4;

      const { slot, overlap } = this.chooseSlot(r, bw, bh, placed, time);
      // Background labels that can't be placed cleanly are decluttered, with hysteresis so
      // they don't blink in and out. Selected/conflict/controlled/request labels always show.
      if (pr >= 3 && r.id !== hoverId) {
        const crowded = overlap > 0.3 * bw * bh;
        const clear = overlap < 0.1 * bw * bh;
        if (r.labelHidden) {
          r.clearSince = clear ? (r.clearSince ?? time) : null;
          if (r.clearSince == null || time - r.clearSince < 500) continue;
          r.labelHidden = false;
        } else {
          r.crowdedSince = crowded ? (r.crowdedSince ?? time) : null;
          if (r.crowdedSince != null && time - r.crowdedSince > 500) {
            r.labelHidden = true;
            r.clearSince = null;
            continue;
          }
        }
      } else {
        r.labelHidden = false;
      }
      const [px, py] = this.slotPos(r, slot, bw, bh);
      placed.push({ x: px, y: py, w: bw, h: bh });
      drawn.push({ r, pr, lines, tw, slot, px, py, dim });
    }

    // hovered label last so it sits on top
    drawn.sort((a, b) => (a.r.id === hoverId) - (b.r.id === hoverId));
    for (const { r, lines, tw, slot, px, py, dim } of drawn) {
      const color = PALETTE[r.colorKey] ?? PALETTE.other;
      const bx = px + 3, by = py + 2;
      const flash = (r.flags & (F.LOS | F.REQUEST)) && !blink;
      ctx.globalAlpha = dim ? 0.55 : 1;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      const ax = slot === 0 || slot === 3 ? bx - 3 : bx + tw + 3;
      const ay = slot < 2 ? by + lines.length * LINE_H + 2 : by - 2;
      ctx.beginPath(); ctx.moveTo(r.sx, r.sy); ctx.lineTo(ax, ay); ctx.stroke();
      ctx.fillStyle = r.id === hoverId ? 'rgba(10,18,32,0.92)' : 'rgba(6,10,18,0.62)';
      ctx.fillRect(bx - 3, by - 2, tw + 6, lines.length * LINE_H + 4);
      if (r.id === selectedId) { ctx.strokeStyle = PALETTE.selected; ctx.strokeRect(bx - 3.5, by - 2.5, tw + 7, lines.length * LINE_H + 5); }
      ctx.fillStyle = flash ? '#ffffff' : color;
      lines.forEach((l, k) => ctx.fillText(l, bx, by + (k + 1) * LINE_H - 3));
      ctx.globalAlpha = 1;
      this.labels.push({ id: r.id, x: bx - 3, y: by - 2, w: tw + 6, h: lines.length * LINE_H + 4 });
    }
  }

  /**
   * Pick the corner for a label: the one with the least overlap with labels already placed.
   * Hysteresis keeps it where it is unless moving clearly helps and it hasn't moved recently,
   * so dense traffic doesn't make labels hop between corners every frame.
   */
  chooseSlot(r, w, h, placed, time) {
    const overlapAt = (slot) => {
      const [x, y] = this.slotPos(r, slot, w, h);
      let a = 0;
      for (const q of placed) {
        const ox = Math.min(x + w, q.x + q.w) - Math.max(x, q.x);
        const oy = Math.min(y + h, q.y + q.h) - Math.max(y, q.y);
        if (ox > 0 && oy > 0) a += ox * oy;
      }
      // keep labels on screen
      if (x < 0 || y < 48 || x + w > this.w || y + h > this.h) a += w * h * 0.5;
      return a;
    };
    const cur = r.labelSlot ?? 0;
    const curOverlap = overlapAt(cur);
    if (curOverlap === 0) return { slot: cur, overlap: 0 };
    let best = cur, bestOverlap = curOverlap;
    for (let s = 0; s < 4; s++) {
      if (s === cur) continue;
      const o = overlapAt(s);
      if (o < bestOverlap) { best = s; bestOverlap = o; }
    }
    const worthIt = bestOverlap === 0 || bestOverlap < curOverlap - 0.25 * w * h;
    const settled = time - (r.labelMovedAt ?? -1e9) > 800;
    if (best !== cur && worthIt && settled) {
      r.labelSlot = best;
      r.labelMovedAt = time;
      return { slot: best, overlap: bestOverlap };
    }
    return { slot: cur, overlap: curOverlap };
  }

  /** Top-left corner of a label box in one of four slots around the target (0 NE, 1 NW, 2 SW, 3 SE). */
  slotPos(r, slot, w, h) {
    const gx = 14, gy = 12;
    const x = slot === 0 || slot === 3 ? r.sx + gx : r.sx - gx - w;
    const y = slot < 2 ? r.sy - gy - h : r.sy + gy;
    return [x, y];
  }

  hitTest(x, y, store) {
    for (let i = this.labels.length - 1; i >= 0; i--) {
      const l = this.labels[i];
      if (x >= l.x && x <= l.x + l.w && y >= l.y && y <= l.y + l.h) return l.id;
    }
    let best = null, bestD = 14 * 14;
    for (const r of store.list) {
      if (!r.onScreen) continue;
      const d = (r.sx - x) ** 2 + (r.sy - y) ** 2;
      if (d < bestD) { bestD = d; best = r.id; }
    }
    return best;
  }
}
