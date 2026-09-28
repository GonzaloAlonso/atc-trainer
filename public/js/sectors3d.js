import * as THREE from 'three';
import { toWorld, FT } from './projection.js';
import { holderColor, holderName, FREE } from './holders.js';

const DISPLAY_CEILING_FL = 460;

const WALL_VERTEX = `varying vec2 vUv;
  #include <common>
  #include <logdepthbuf_pars_vertex>
  void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0);
  #include <logdepthbuf_vertex>
  }`;
const WALL_FRAGMENT = `uniform vec3 color; uniform float strength; varying vec2 vUv;
  #include <logdepthbuf_pars_fragment>
  void main(){
    #include <logdepthbuf_fragment>
    float edge = pow(1.0 - vUv.y, 2.0) * 0.5 + pow(vUv.y, 6.0) * 0.6;
    float bands = step(0.96, fract(vUv.y * 6.0)) * 0.25;
    gl_FragColor = vec4(color * (0.08 + edge + bands) * strength, 1.0);
  }`;

/**
 * The whole sectorization in 3D: every sector is a volume over its region between its floor and
 * ceiling, so vertical layers stack on top of each other. Styling follows who holds it:
 * mine = bright walls, other controllers = their colour, AI = violet, free = faint outline.
 */
export class SectorLayer {
  constructor(scene) {
    this.group = new THREE.Group();
    this.group.name = 'sectors';
    scene.add(this.group);
    this.items = new Map();
    this.catalogue = null;
    this.state = { holders: new Map(), me: null };
    this._sig = '';
  }

  build(catalogue) {
    this.catalogue = catalogue;
    for (const s of catalogue.sectors) {
      const g = new THREE.Group();
      const top = Math.min(s.fl_max, DISPLAY_CEILING_FL) * 100 * FT;
      const bottom = s.fl_min * 100 * FT;
      const ring = [...s.poly, s.poly[0]];
      const lo = ring.map(([lat, lon]) => toWorld(lat, lon, bottom));
      const hi = ring.map(([lat, lon]) => toWorld(lat, lon, top));

      const pos = [], vUv = [];
      for (let i = 0; i < ring.length - 1; i++) {
        const a = lo[i], b = lo[i + 1], c = hi[i], d = hi[i + 1];
        pos.push(a.x, a.y, a.z, b.x, b.y, b.z, c.x, c.y, c.z, c.x, c.y, c.z, b.x, b.y, b.z, d.x, d.y, d.z);
        vUv.push(0, 0, 1, 0, 0, 1, 0, 1, 1, 0, 1, 1);
      }
      const wallGeo = new THREE.BufferGeometry();
      wallGeo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
      wallGeo.setAttribute('uv', new THREE.Float32BufferAttribute(vUv, 2));
      const walls = new THREE.Mesh(wallGeo, new THREE.ShaderMaterial({
        transparent: true, depthWrite: false, side: THREE.DoubleSide, blending: THREE.AdditiveBlending,
        uniforms: { color: { value: new THREE.Color(FREE) }, strength: { value: 0 } },
        vertexShader: WALL_VERTEX, fragmentShader: WALL_FRAGMENT,
      }));
      walls.renderOrder = 5;
      g.add(walls);

      const linePos = [];
      const pushLoop = (pts) => { for (let i = 0; i < pts.length - 1; i++) linePos.push(pts[i].x, pts[i].y, pts[i].z, pts[i + 1].x, pts[i + 1].y, pts[i + 1].z); };
      pushLoop(lo); pushLoop(hi);
      for (let i = 0; i < ring.length - 1; i++) linePos.push(lo[i].x, lo[i].y, lo[i].z, hi[i].x, hi[i].y, hi[i].z);
      const lineGeo = new THREE.BufferGeometry();
      lineGeo.setAttribute('position', new THREE.Float32BufferAttribute(linePos, 3));
      const lines = new THREE.LineSegments(lineGeo, new THREE.LineBasicMaterial({
        color: FREE, transparent: true, opacity: 0.2, depthWrite: false,
      }));
      g.add(lines);

      // label anchor: centre of the region, in the middle of the layer
      const lat = s.poly.reduce((a, p) => a + p[0], 0) / s.poly.length;
      const lon = s.poly.reduce((a, p) => a + p[1], 0) / s.poly.length;
      const anchor = toWorld(lat, lon, (bottom + top) / 2);

      this.group.add(g);
      this.items.set(s.id, { group: g, walls, lines, sector: s, anchor });
    }
    this.apply();
  }

  /** sectorization rows from the frame: [sector id, holder, aircraft count]; me = my holder key. */
  update(sectorization, me) {
    const sig = me + '|' + sectorization.map((r) => r[0] + ':' + (r[1] ?? '')).join(',');
    if (sig === this._sig) return;
    this._sig = sig;
    this.state = { holders: new Map(sectorization.map(([sid, h]) => [sid, h])), me };
    this.apply();
  }

  apply() {
    const { holders, me } = this.state;
    for (const [sid, it] of this.items) {
      const h = holders.get(sid) ?? null;
      const color = holderColor(h, me);
      const mine = h && h === me;
      it.walls.visible = !!h;
      it.walls.material.uniforms.color.value.set(color);
      it.walls.material.uniforms.strength.value = mine ? 1 : 0.35;
      it.lines.material.color.set(color);
      it.lines.material.opacity = mine ? 0.95 : h ? 0.7 : 0.2;
    }
  }

  /** Labels for manned sectors (drawn by the overlay). */
  labels() {
    const { holders, me } = this.state;
    const out = [];
    for (const [sid, it] of this.items) {
      const h = holders.get(sid);
      if (!h) continue;
      out.push({ world: it.anchor, text: `${sid} · ${holderName(h, me)}`, color: holderColor(h, me), mine: h === me });
    }
    return out;
  }

  /** Rebuild after a vertical-exaggeration change. */
  rebuild() {
    for (const it of this.items.values()) {
      this.group.remove(it.group);
      it.group.traverse((o) => { o.geometry?.dispose(); o.material?.dispose?.(); });
    }
    this.items.clear();
    if (this.catalogue) this.build(this.catalogue);
  }
}
