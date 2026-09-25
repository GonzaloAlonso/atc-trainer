import * as THREE from 'three';
import { MapControls } from 'three/addons/controls/MapControls.js';
import { AREA_WORLD, toWorld, view, worldToLatLon, FT } from './projection.js';
import { TileManager, STYLES } from './tiles.js';
import { Environment } from './environment.js';
import { SectorLayer } from './sectors3d.js';
import { TrafficStore, AircraftLayer } from './traffic.js';
import { Overlay } from './overlay.js';
import { UI } from './ui.js';
import { api, connect } from './net.js';

// ---------------------------------------------------------------- renderer & scene
const canvas = document.getElementById('gl');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, logarithmicDepthBuffer: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(window.innerWidth, window.innerHeight);

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(50, window.innerWidth / window.innerHeight, 0.02, 80000);

const env = new Environment(scene);
const tiles = new TileManager(scene, renderer, 'radar');
const sectorLayer = new SectorLayer(scene);
const store = new TrafficStore();
const aircraft = new AircraftLayer(scene);
const overlay = new Overlay(document.getElementById('overlay'));

// ---------------------------------------------------------------- camera
const controls = new MapControls(camera, canvas);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.screenSpacePanning = false;
controls.maxPolarAngle = 1.42;
controls.minDistance = 4;
controls.maxDistance = 5200;
controls.zoomToCursor = true;
controls.zoomSpeed = 1.3;
controls.target.copy(toWorld(49.5, 6.5, 0));
camera.position.copy(controls.target).add(new THREE.Vector3(0, 1500, 1250));

function clampTarget() {
  const t = controls.target;
  const x = THREE.MathUtils.clamp(t.x, AREA_WORLD.xMin, AREA_WORLD.xMax);
  const z = THREE.MathUtils.clamp(t.z, AREA_WORLD.zMin, AREA_WORLD.zMax);
  const dx = x - t.x, dz = z - t.z;
  if (dx || dz || t.y !== 0) {
    camera.position.x += dx; camera.position.z += dz;
    camera.position.y -= t.y;
    t.set(x, 0, z);
  }
}

let flight = null;
function flyTo(lat, lon, distance) {
  const to = toWorld(lat, lon, 0);
  const offset = camera.position.clone().sub(controls.target);
  if (distance) offset.setLength(distance);
  flight = { from: controls.target.clone(), to, fromOff: camera.position.clone().sub(controls.target), toOff: offset, t0: performance.now(), dur: 900 };
}

function updateFlight(now) {
  if (!flight) return;
  const k = Math.min(1, (now - flight.t0) / flight.dur);
  const e = k < 0.5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
  controls.target.lerpVectors(flight.from, flight.to, e);
  camera.position.copy(controls.target).add(flight.fromOff.clone().lerp(flight.toOff, e));
  if (k >= 1) flight = null;
}

window.addEventListener('resize', () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
  overlay.resize();
});

// ---------------------------------------------------------------- app state
let sectors = [];
const app = {
  store,
  selectedId: null,
  route: null,
  select(id, fly = false) {
    const changed = id !== app.selectedId;
    app.selectedId = id;
    if (!id) { app.route = null; ui.detail = null; ui.showStrip(null); return; }
    const rec = store.map.get(id);
    if (fly && rec) flyTo(rec.pLat ?? rec.lat, rec.pLon ?? rec.lon, Math.min(camera.position.distanceTo(controls.target), 700));
    if (changed) { ui.detail = null; app.route = null; }
    if (rec) ui.showStrip(rec, changed);
    app.refreshDetail();
  },
  async refreshDetail() {
    const id = app.selectedId;
    if (!id) return;
    try {
      const d = await api(`/api/aircraft/${id}`);
      if (id !== app.selectedId) return;
      ui.setDetail(d);
      const dctIdent = d.assigned?.dct;
      const dct = dctIdent ? d.fixes.find((f) => f.ident === dctIdent) : null;
      app.route = { points: d.route, dct };
    } catch { /* aircraft left */ }
  },
  onSector(id) {
    sectorLayer.setActive(id);
    const s = sectors.find((x) => x.id === id);
    if (s && app._lastSector !== id) {
      const lat = s.poly.reduce((a, p) => a + p[0], 0) / s.poly.length;
      const lon = s.poly.reduce((a, p) => a + p[1], 0) / s.poly.length;
      flyTo(lat, lon, 900);
    }
    app._lastSector = id;
  },
  setStyle(key) {
    tiles.setStyle(key);
    env.setTheme(key);
    for (const b of document.getElementById('style-seg').children) b.classList.toggle('on', b.dataset.style === key);
    document.getElementById('attribution').textContent = `${STYLES[key].attribution} · Terrain: Mapzen/AWS · Data: The OpenSky Network · Navdata: OurAirports`;
    try { localStorage.setItem('visor.style', key); } catch { /* ignore */ }
  },
};
const ui = new UI(app);

let savedStyle = 'radar';
try { savedStyle = localStorage.getItem('visor.style') || 'radar'; } catch { /* ignore */ }
app.setStyle(STYLES[savedStyle] ? savedStyle : 'radar');

// display settings
const vex = document.getElementById('vexag');
vex.oninput = () => {
  view.vexag = Number(vex.value);
  document.getElementById('vexag-out').textContent = `${vex.value}×`;
  clearTimeout(vex._t);
  vex._t = setTimeout(() => { tiles.rebuild(); sectorLayer.rebuild(sectors); }, 250);
};
document.getElementById('vec-min').onchange = (e) => { aircraft.options.vectorMin = Number(e.target.value); };
document.getElementById('opt-trails').onchange = (e) => { aircraft.options.trails = e.target.checked; };
document.getElementById('opt-drops').onchange = (e) => { aircraft.options.drops = e.target.checked; };
document.getElementById('opt-labels').onchange = (e) => { overlay.options.labelsAll = e.target.checked; };

// ---------------------------------------------------------------- picking
let down = null;
let hoverId = null;
let pointer = null;
canvas.addEventListener('pointerdown', (e) => { down = { x: e.clientX, y: e.clientY }; flight = null; });
canvas.addEventListener('pointermove', (e) => { pointer = { x: e.clientX, y: e.clientY }; });
canvas.addEventListener('pointerleave', () => { pointer = null; hoverId = null; });
canvas.addEventListener('pointerup', (e) => {
  if (!down || Math.hypot(e.clientX - down.x, e.clientY - down.y) > 4 || e.button !== 0) return;
  app.select(overlay.hitTest(e.clientX, e.clientY, store));
});
canvas.addEventListener('dblclick', (e) => {
  const id = overlay.hitTest(e.clientX, e.clientY, store);
  if (id) app.select(id, true);
});

// ---------------------------------------------------------------- data
connect((frame) => {
  store.applyFrame(frame);
  ui.onFrame(frame);
  if (app.selectedId && !store.map.has(app.selectedId)) app.select(null);
}, (up) => { if (!up) ui.toast('Connection lost — reconnecting…', true); });

(async () => {
  try {
    const [nav, secs] = await Promise.all([api('/api/navdata'), api('/api/sectors')]);
    overlay.setFixes(nav.fixes);
    sectors = secs;
    sectorLayer.build(sectors);
    ui.setSectors(sectors);
    if (store.frame?.sector) app.onSector(store.frame.sector);
  } catch (e) { ui.toast('Failed to load navdata: ' + e.message, true); }
  const pollStatus = async () => {
    try { ui.setStatus(await api('/api/status')); } catch { /* ignore */ }
  };
  pollStatus();
  setInterval(pollStatus, 5000);
})();
setInterval(() => app.selectedId && app.refreshDetail(), 3000);

// ---------------------------------------------------------------- loop
function frame(now) {
  updateFlight(now);
  controls.update();
  clampTarget();
  const distance = camera.position.distanceTo(controls.target);
  camera.near = Math.max(0.01, distance * 0.001);
  camera.updateProjectionMatrix();
  camera.updateMatrixWorld();

  tiles.update(camera);
  env.update(camera, distance);
  const simNow = store.simNow();
  aircraft.update(store, simNow, camera, app.selectedId);
  renderer.render(scene, camera);

  if (pointer) {
    hoverId = overlay.hitTest(pointer.x, pointer.y, store);
    canvas.style.cursor = hoverId ? 'pointer' : '';
  }
  overlay.draw({
    camera, store, selectedId: app.selectedId, hoverId, distance, time: now,
    conflicts: store.frame?.conflicts ?? [], route: app.route,
  });
  const sel = app.selectedId && store.map.get(app.selectedId);
  if (sel) ui.showStrip(sel, false);
  ui.tickClock(simNow);
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

window.visor = { scene, camera, controls, store, tiles, app, overlay, worldToLatLon, toWorld, FT };
