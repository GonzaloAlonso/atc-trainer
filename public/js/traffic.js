import * as THREE from 'three';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';
import { toWorld, worldPerNm, FT } from './projection.js';
import { holderColor } from './holders.js';

// Flag bits. HUMAN, AI and PENDING come from the server (Engine._flags); the per-viewer ones
// (SECTOR = in one of my sectors, STCA/LOS = my alerts, REQUEST = my pilot requests) are derived
// here from my own, already filtered, conflicts and decisions.
export const F = { SECTOR: 1, HUMAN: 2, AI: 4, STCA: 8, LOS: 16, REQUEST: 32, PENDING: 64 };

export const PALETTE = {
  other: '#7f93aa', sector: '#eaf2ff', human: '#4dff9a', ai: '#d08cff',
  request: '#4cc9ff', stca: '#ffb020', los: '#ff3b3b', selected: '#ffe14d',
};
const COLORS = Object.fromEntries(Object.entries(PALETTE).map(([k, v]) => [k, new THREE.Color(v)]));

export function colorKey(rec, selectedId) {
  const f = rec.flags;
  if (f & F.LOS) return 'los';
  if (f & F.STCA) return 'stca';
  if (rec.id === selectedId) return 'selected';
  if (f & F.REQUEST) return 'request';
  if (f & F.HUMAN) return 'human';
  if (f & F.AI) return 'ai';
  if (f & F.SECTOR) return 'sector';
  if (rec.holder) return 'foreign';          // in another controller's sector: their colour, dimmed
  return 'other';
}

const tintCache = new Map();
export function tintOf(hex) {
  let c = tintCache.get(hex);
  if (!c) { c = new THREE.Color(hex).multiplyScalar(0.75); tintCache.set(hex, c); }
  return c;
}

const DEG = Math.PI / 180;
const TRAIL_LEN = 8;
const TRAIL_EVERY_S = 12;

// ---------------------------------------------------------------- client-side traffic state
export class TrafficStore {
  constructor() {
    this.map = new Map();
    this.list = [];
    this.simBase = 0;
    this.realBase = performance.now();
    this.rate = 0;
    this.frame = null;
  }

  simNow() {
    const dtReal = Math.min((performance.now() - this.realBase) / 1000, 1.5);
    return this.simBase + dtReal * this.rate;
  }

  /** Position at sim time `t` extrapolated from the last frame, with smoothing of corrections. */
  position(rec, t, out = {}) {
    const dt = t - rec.t;
    const d = (rec.gs * dt) / 3600;
    const h = rec.hdg * DEG;
    let lat = rec.lat + (d * Math.cos(h)) / 60;
    let lon = rec.lon + (d * Math.sin(h)) / (60 * Math.cos(rec.lat * DEG));
    let alt = rec.alt + (rec.vs * dt) / 60;
    if (rec.cfl != null) {
      const c = rec.cfl * 100;
      if ((rec.vs > 0 && alt > c && rec.alt <= c) || (rec.vs < 0 && alt < c && rec.alt >= c)) alt = c;
    }
    const k = Math.max(0, 1 - (performance.now() - rec.corrAt) / 450);
    if (k > 0) { lat += rec.cLat * k; lon += rec.cLon * k; alt += rec.cAlt * k; }
    out.lat = lat; out.lon = lon; out.alt = Math.max(0, alt);
    return out;
  }

  applyFrame(frame) {
    const nowSim = this.frame ? this.simNow() : frame.t;
    this.frame = frame;
    this.simBase = frame.t;
    this.realBase = performance.now();
    this.rate = frame.paused || frame.lockstep ? 0 : frame.speed;
    const seen = new Set();
    const tmp = {};
    // per-viewer state: my sectors, my alerts, my pilot requests
    const me = frame.me?.holder ?? null;
    const mine = new Set(frame.me?.sectors ?? []);
    const holders = new Map((frame.sectorization ?? []).map(([sid, h]) => [sid, h]));
    const alert = new Map();
    for (const [a, b, kind] of frame.conflicts ?? []) {
      for (const i of [a, b]) if (alert.get(i) !== 'LOS') alert.set(i, kind);
    }
    const requests = new Set((frame.decisions ?? []).filter((d) => d.status === 'open' && d.kind !== 'conflict')
      .flatMap((d) => d.subjects));
    for (const r of frame.ac) {
      const [id, cs, lat, lon, alt, hdg, gs, vs, cfl, rowFlags, cls, lateral, ahdg, dct, spd, sec, ctl] = r;
      let flags = rowFlags & (F.HUMAN | F.AI | F.PENDING);
      if (sec && mine.has(sec)) flags |= F.SECTOR;
      const lvl = alert.get(id);
      if (lvl === 'LOS') flags |= F.LOS; else if (lvl === 'STCA') flags |= F.STCA;
      if (requests.has(id)) flags |= F.REQUEST;
      const holder = sec ? holders.get(sec) ?? null : null;
      seen.add(id);
      let rec = this.map.get(id);
      if (!rec) {
        rec = { id, trail: [], trailT: -1e9, cLat: 0, cLon: 0, cAlt: 0, corrAt: 0, turnRate: 0,
          world: new THREE.Vector3(), sx: 0, sy: 0, onScreen: false };
        this.map.set(id, rec);
      } else {
        // blend from where we were drawing it to the new truth
        this.position(rec, nowSim, tmp);
        const dtf = frame.t - rec.t;
        if (dtf > 0) {
          let dh = hdg - rec.hdg;
          dh = ((dh + 540) % 360) - 180;
          rec.turnRate = rec.turnRate * 0.5 + (dh / dtf) * 0.5;
        }
        rec.cLat = tmp.lat - lat; rec.cLon = tmp.lon - lon; rec.cAlt = tmp.alt - alt;
        if (Math.abs(rec.cLat) > 0.2 || Math.abs(rec.cLon) > 0.2) { rec.cLat = rec.cLon = rec.cAlt = 0; }
        rec.corrAt = performance.now();
      }
      Object.assign(rec, {
        cs, lat, lon, alt, hdg, gs, vs, cfl, flags, cls, lateral, ahdg, dct, spd, t: frame.t,
        sec, ctl, holder: holder === me ? null : holder, tint: holder && holder !== me ? holderColor(holder, me) : null,
      });
      if (frame.t - rec.trailT >= TRAIL_EVERY_S || frame.t < rec.trailT) {
        rec.trail.push([lat, lon, alt]);
        if (rec.trail.length > TRAIL_LEN) rec.trail.shift();
        rec.trailT = frame.t;
      }
    }
    for (const id of this.map.keys()) if (!seen.has(id)) this.map.delete(id);
    this.list = [...this.map.values()];
  }
}

// ---------------------------------------------------------------- 3D model
function buildAircraftGeometry() {
  const parts = [];
  const fus = new THREE.CylinderGeometry(0.055, 0.045, 0.82, 10, 1).rotateX(Math.PI / 2);
  parts.push(fus);
  parts.push(new THREE.ConeGeometry(0.055, 0.16, 10).rotateX(Math.PI / 2).translate(0, 0, 0.49));
  parts.push(new THREE.ConeGeometry(0.045, 0.12, 10).rotateX(-Math.PI / 2).translate(0, 0.01, -0.47));
  const wing = (side) => {
    const g = new THREE.BoxGeometry(0.5, 0.018, 0.17).translate(side * 0.27, -0.02, 0.02);
    const shear = new THREE.Matrix4().set(1, 0, 0, 0, 0, 1, 0, 0, side * -0.45, 0, 1, 0, 0, 0, 0, 1);
    return g.applyMatrix4(shear);
  };
  parts.push(wing(1), wing(-1));
  const stab = (side) => {
    const g = new THREE.BoxGeometry(0.19, 0.012, 0.09).translate(side * 0.1, 0.02, -0.4);
    const shear = new THREE.Matrix4().set(1, 0, 0, 0, 0, 1, 0, 0, side * -0.5, 0, 1, 0, 0, 0, 0, 1);
    return g.applyMatrix4(shear);
  };
  parts.push(stab(1), stab(-1));
  const fin = new THREE.BoxGeometry(0.014, 0.17, 0.12).translate(0, 0.1, -0.4);
  fin.applyMatrix4(new THREE.Matrix4().set(1, 0, 0, 0, 0, 1, 0, 0, 0, -0.6, 1, 0, 0, 0, 0, 1));
  parts.push(fin);
  for (const side of [1, -1]) {
    parts.push(new THREE.CylinderGeometry(0.028, 0.024, 0.13, 8).rotateX(Math.PI / 2).translate(side * 0.2, -0.05, 0.1));
  }
  const g = mergeGeometries(parts.map((p) => p.toNonIndexed()));
  g.computeVertexNormals();
  return g;
}

const CAP = 8192;

export class AircraftLayer {
  constructor(scene) {
    this.material = new THREE.MeshStandardMaterial({
      color: 0xffffff, roughness: 0.45, metalness: 0.2, emissive: 0x303844,
    });
    this.mesh = new THREE.InstancedMesh(buildAircraftGeometry(), this.material, CAP);
    this.mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    this.mesh.setColorAt(0, COLORS.other);
    this.mesh.frustumCulled = false;
    this.mesh.count = 0;
    scene.add(this.mesh);

    const lineSet = (n, opacity, depthTest = true) => {
      const g = new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(n * 3), 3).setUsage(THREE.DynamicDrawUsage));
      g.setAttribute('color', new THREE.BufferAttribute(new Float32Array(n * 3), 3).setUsage(THREE.DynamicDrawUsage));
      const m = new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity, depthTest, depthWrite: false });
      const l = new THREE.LineSegments(g, m);
      l.frustumCulled = false;
      scene.add(l);
      return l;
    };
    const pointSet = (n, size, opacity, depthTest = true) => {
      const g = new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(n * 3), 3).setUsage(THREE.DynamicDrawUsage));
      g.setAttribute('color', new THREE.BufferAttribute(new Float32Array(n * 3), 3).setUsage(THREE.DynamicDrawUsage));
      const m = new THREE.PointsMaterial({ size, sizeAttenuation: false, vertexColors: true, transparent: true, opacity, depthTest, depthWrite: false });
      const p = new THREE.Points(g, m);
      p.frustumCulled = false;
      scene.add(p);
      return p;
    };
    this.drops = lineSet(CAP * 2, 0.45);
    this.vectors = lineSet(CAP * 2, 0.9);
    this.blips = pointSet(CAP, 3, 0.8, false);
    this.trails = pointSet(CAP * TRAIL_LEN, 2.5, 0.75);
    this.options = { vectorMin: 2, trails: true, drops: true };

    this._m = new THREE.Matrix4();
    this._q = new THREE.Quaternion();
    this._e = new THREE.Euler(0, 0, 0, 'YXZ');
    this._s = new THREE.Vector3();
    this._p = {};
    this._fwd = new THREE.Vector3();
    this._tmp = new THREE.Vector3();
  }

  update(store, simNow, camera, selectedId) {
    const recs = store.list;
    const n = Math.min(recs.length, CAP);
    const dropPos = this.drops.geometry.attributes.position.array;
    const dropCol = this.drops.geometry.attributes.color.array;
    const vecPos = this.vectors.geometry.attributes.position.array;
    const vecCol = this.vectors.geometry.attributes.color.array;
    const blipPos = this.blips.geometry.attributes.position.array;
    const blipCol = this.blips.geometry.attributes.color.array;
    const trPos = this.trails.geometry.attributes.position.array;
    const trCol = this.trails.geometry.attributes.color.array;
    const camPos = camera.position;
    const vecMin = this.options.vectorMin;
    let tn = 0;

    for (let i = 0; i < n; i++) {
      const r = recs[i];
      const p = store.position(r, simNow, this._p);
      const w = toWorld(p.lat, p.lon, p.alt * FT, r.world);
      r.pLat = p.lat; r.pLon = p.lon; r.pAlt = p.alt;
      r.colorKey = colorKey(r, selectedId);
      const col = r.colorKey === 'foreign' ? tintOf(r.tint) : COLORS[r.colorKey];

      // model
      const dist = camPos.distanceTo(w);
      const s = Math.max(dist * 0.017, 0.07) * (r.id === selectedId ? 1.5 : 1);
      const pitch = THREE.MathUtils.clamp(r.vs / 4000, -1, 1) * 0.22;
      const roll = THREE.MathUtils.clamp(r.turnRate * 0.18, -0.45, 0.45);
      this._e.set(-pitch, Math.PI - r.hdg * DEG, roll);
      this._q.setFromEuler(this._e);
      this._m.compose(w, this._q, this._s.set(s, s, s));
      this.mesh.setMatrixAt(i, this._m);
      this.mesh.setColorAt(i, col);

      // drop line, ground blip
      const o6 = i * 6;
      dropPos[o6] = w.x; dropPos[o6 + 1] = w.y; dropPos[o6 + 2] = w.z;
      dropPos[o6 + 3] = w.x; dropPos[o6 + 4] = 0; dropPos[o6 + 5] = w.z;
      dropCol[o6] = col.r; dropCol[o6 + 1] = col.g; dropCol[o6 + 2] = col.b;
      dropCol[o6 + 3] = col.r * 0.15; dropCol[o6 + 4] = col.g * 0.15; dropCol[o6 + 5] = col.b * 0.15;
      blipPos[i * 3] = w.x; blipPos[i * 3 + 1] = 0.05; blipPos[i * 3 + 2] = w.z;
      blipCol[i * 3] = col.r; blipCol[i * 3 + 1] = col.g; blipCol[i * 3 + 2] = col.b;

      // speed vector (vecMin minutes ahead)
      const len = ((r.gs * vecMin) / 60) * worldPerNm(p.lat);
      const h = r.hdg * DEG;
      vecPos[o6] = w.x; vecPos[o6 + 1] = w.y; vecPos[o6 + 2] = w.z;
      vecPos[o6 + 3] = w.x + Math.sin(h) * len; vecPos[o6 + 4] = w.y; vecPos[o6 + 5] = w.z - Math.cos(h) * len;
      vecCol[o6] = col.r; vecCol[o6 + 1] = col.g; vecCol[o6 + 2] = col.b;
      vecCol[o6 + 3] = col.r * 0.3; vecCol[o6 + 4] = col.g * 0.3; vecCol[o6 + 5] = col.b * 0.3;

      // history trail
      if (this.options.trails) {
        const tr = r.trail;
        for (let k = 0; k < tr.length - 1; k++) {
          const tw = toWorld(tr[k][0], tr[k][1], tr[k][2] * FT, this._tmp);
          const f = 0.25 + (0.6 * k) / TRAIL_LEN;
          trPos[tn * 3] = tw.x; trPos[tn * 3 + 1] = tw.y; trPos[tn * 3 + 2] = tw.z;
          trCol[tn * 3] = col.r * f; trCol[tn * 3 + 1] = col.g * f; trCol[tn * 3 + 2] = col.b * f;
          tn++;
        }
      }
    }
    this.mesh.count = n;
    this.mesh.instanceMatrix.needsUpdate = true;
    if (this.mesh.instanceColor) this.mesh.instanceColor.needsUpdate = true;
    const setRange = (obj, count) => {
      obj.geometry.setDrawRange(0, count);
      obj.geometry.attributes.position.needsUpdate = true;
      obj.geometry.attributes.color.needsUpdate = true;
    };
    setRange(this.drops, this.options.drops ? n * 2 : 0);
    setRange(this.vectors, vecMin > 0 ? n * 2 : 0);
    setRange(this.blips, n);
    setRange(this.trails, tn);
  }
}
