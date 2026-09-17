# vizjs/core.py — rung 2/17 of the template join (issues
# #86 phase-3 / #299 A): boot, scene/camera/renderer, shared JS leaves, sprite pass. Moved VERBATIM from viz.py —
# every edit here changes graph.html: regen the bake (the standing
# template law; test_viz refuses a bake older than viz.py/vizjs).
_JS_CORE = r"""<script type="module">
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { LineSegments2 } from "three/addons/lines/LineSegments2.js";
import { LineSegmentsGeometry } from "three/addons/lines/LineSegmentsGeometry.js";
import { LineMaterial } from "three/addons/lines/LineMaterial.js";

const DATA = __DATA__;
const nodes = DATA.nodes, links = DATA.links, fedges = DATA.fedges || [], hw = DATA.hw || [];
const N = nodes.length;
// shared 2D point-to-segment distance (screen space). Also records the hit
// param in segT so callers can interpolate depth at the hit point.
let segT = 0;
function segDist(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay;
  const L2 = dx * dx + dy * dy;
  let t = L2 > 0 ? ((px - ax) * dx + (py - ay) * dy) / L2 : 0;
  t = Math.max(0, Math.min(1, t));
  segT = t;
  return Math.hypot(ax + t * dx - px, ay + t * dy - py);
}
// AABB separation predicate for the 3D label placers: true when a and b
// clear each other by gap on every axis (one geometry, per-site gaps).
function separate(a, b, gap) {
  return a.right < b.left - gap || b.right < a.left - gap ||
         a.bottom < b.top - gap || b.bottom < a.top - gap;
}
// D12: one candidate-ladder engine for the screen-space label placers.
// Each site keeps its own candidate order (load-bearing: the nearest-first
// reading differs per surface), obstacle families and gap; the engine owns
// the transform write + box measure + first-clear-wins walk. Returns the
// last try: hit=true when a candidate cleared (r = its rect, o = its offset).
function placeLabels(el, x, y, offs, anchor, pad, ok) {
  let r = null, o = null;
  for (const c of offs) {
    o = c;
    el.style.transform = "translate(" + (x + c[0]).toFixed(1) + "px," + (y + c[1]).toFixed(1) +
      "px) translate(" + anchor + ")";
    r = labBox(el, x + c[0], y + c[1], pad);
    if (ok(r)) return { hit: true, r, o };
  }
  return { hit: false, r, o };
}
// Emphasis alphas (D13): the dim levels that must stay in lockstep across
// surfaces — 3D greyout (node alpha clamp, ghost endpoints, wire color), 2D
// map freeze/hover disclosure, the focused-tier 3D material opacity, the DOM
// label layers' greyout opacity. The map underlay's 0.12 ink-budget alpha is
// deliberately NOT here: same number, different lever.
const EMPHASIS = { DIM_ALPHA: 0.12, MAP_FREEZE_ALPHA: 0.06, FOCUS_TIER_OPACITY: 0.75, LABEL_DIM_OPACITY: 0.25 };
// fn-identity keys mirror graph.fn_key/split_key (graph.py: FN_KEY_SEP "::",
// first separator wins; the ::tscn/::SIGNAL:/::VAR: pseudo-forms are bake-side
// only). The fio/hw maps the bake emits are keyed path::name.
const fnKey = (fi, name) => nodes[fi].path + "::" + name;
// cycle lens (madge cyclicNodeColor steal): files inside call cycles
// (SCC size > 1), baked by _strata_analysis
const cycSet = new Set(DATA.meta.cycIds || []);
let cycOnly = false;
nodes.forEach((n, i) => { n.cyc = cycSet.has(i) ? 1 : 0; });
// undirected adjacency for focus BFS + directed halves for the in/out
// direction modes ("what breaks if I change X" needs OUT = who I affect
// downstream, IN = who feeds me)
const adj = Array.from({ length: N }, () => []);
const adjOut = Array.from({ length: N }, () => []);
const adjIn = Array.from({ length: N }, () => []);
links.forEach(l => {
  adj[l.s].push(l.t); adj[l.t].push(l.s);
  adjOut[l.s].push(l.t); adjIn[l.t].push(l.s);
});
const outDeg = adjOut.map(a => new Set(a).size);
const inDeg = adjIn.map(a => new Set(a).size);
// fn-name index for search seeding: fn name -> Set(owning/calling files)
const fnOf = {};
fedges.forEach(e => {
  (fnOf[e[1]] = fnOf[e[1]] || new Set()).add(e[0]);
  (fnOf[e[3]] = fnOf[e[3]] || new Set()).add(e[2]);
});
const fnNames = Object.keys(fnOf);
// ---- search highlight (issue #33): typing never focuses — it flags
// matches in place and fills the results list; focus/fn tier starts only
// from a node (or results-row) click. Match rule is EXACTLY the seed rule
// computeLevels used (path/cls includes; fn-name includes at >=2 chars) so
// highlight and focus agree on what a query means — same DATA, same rule.
const hlArr = new Float32Array(N);   // 1 = file matches the live query
let hlFn = new Set();                // fn names matching the live query
function applyHighlight() {
  hlArr.fill(0); hlFn.clear();
  _sfDirty = true;   // search-match brightness lift reads hlArr
  if (query) {
    for (let i = 0; i < N; i++) {
      const n = nodes[i];
      if (n.path.toLowerCase().includes(query) ||
          n.cls.toLowerCase().includes(query)) hlArr[i] = 1;
    }
    if (query.length >= 2) for (const nm of fnNames) {
      if (!nm.toLowerCase().includes(query)) continue;
      hlFn.add(nm);
      for (const f of fnOf[nm]) hlArr[f] = 1;
    }
  }
  hubs.forEach(h => h.el.classList.toggle("hl", !!hlArr[h.i]));
  fLabs.forEach(f => {
    if (f.kind === 1) f.el.classList.toggle("hl", hlFn.has(fnMeta[f.ix].name));
    else f.el.classList.remove("hl");
  });
  buildSearchResults();
}
// results dropdown: capped, deterministic (files by degree desc, then fn
// names by owning-file count desc). Row click == node click: same focus
// path, compaction and framing the canvas click takes.
const searchResEl = document.getElementById("searchResults");
const RES_CAP = 30;
function focusFromSearch(i) {
  hideSearchResults();
  pushFocusState();
  focusSeeds.clear(); focusSeeds.add(i);
  applyVisibility();
  focus(i);
  showInfo(i);
  mapCenterOn(i);
}
function hideSearchResults() {
  searchResEl.style.display = "none";
  searchResEl.innerHTML = "";
}
function buildSearchResults() {
  if (!query) { hideSearchResults(); return; }
  const files = [];
  for (let i = 0; i < N; i++) if (hlArr[i]) files.push(i);
  files.sort((a, b) => degree[b] - degree[a] || a - b);
  const fns = [...hlFn];
  fns.sort((a, b) => fnOf[b].size - fnOf[a].size || (a < b ? -1 : a > b ? 1 : 0));
  const total = files.length + fns.length;
  if (!total) { hideSearchResults(); return; }
  const rows = [];
  const nFiles = Math.min(files.length, RES_CAP);
  for (let k = 0; k < nFiles; k++) {
    const i = files[k];
    rows.push({ html: esc(nodes[i].label) + " · " + Math.round(degree[i]),
                title: nodes[i].path, fn: false, i });
  }
  const nFns = Math.min(fns.length, Math.max(0, RES_CAP - nFiles));
  for (let k = 0; k < nFns; k++) {
    const nm = fns[k];
    // deterministic owner: highest-degree file the name lives on
    let best = -1;
    for (const f of fnOf[nm]) if (best < 0 || degree[f] > degree[best] ||
        (degree[f] === degree[best] && f < best)) best = f;
    rows.push({ html: "ƒ <b>" + esc(nm) + "</b> · " + fnOf[nm].size + " files",
                title: nm, fn: true, i: best });
  }
  let html = rows.map((r, k) =>
    '<div class="row' + (r.fn ? " fnrow" : "") + '" data-k="' + k + '" title="' +
    esc(r.title) + '">' + r.html + "</div>").join("");
  if (total > rows.length)
    html += '<div class="more">+' + (total - rows.length) +
            " more — refine or click a node</div>";
  searchResEl.innerHTML = html;
  const r0 = searchEl.getBoundingClientRect();
  searchResEl.style.left = r0.left + "px";
  searchResEl.style.top = (r0.bottom + 6) + "px";
  searchResEl.style.display = "block";
  searchResEl.querySelectorAll(".row").forEach(el => {
    // pointerdown beats the input blur so the row click still lands
    el.onpointerdown = e => { e.preventDefault(); focusFromSearch(rows[+el.dataset.k].i); };
  });
}
// per-file typed edge weight (for the hover summary line)
const typedCount = Array.from({ length: N }, () => ({}));
links.forEach(l => {
  typedCount[l.s][l.ty] = (typedCount[l.s][l.ty] || 0) + l.w;
  typedCount[l.t][l.ty] = (typedCount[l.t][l.ty] || 0) + l.w;
});
function edgeSummary(i) {
  const c = typedCount[i], parts = [];
  if (c.call) parts.push(Math.round(c.call) + " calls");
  if (c.signal) parts.push(Math.round(c.signal) + " signals");
  const cont = (c.inst || 0) + (c.attach || 0);
  if (cont) parts.push(Math.round(cont) + " contains");
  return parts.join(" · ");
}
// BFS path from a hovered node to the current focus seed(s) — the "where am I
// relative to what I focused" answer. Seeds = clicked seeds plus query matches
// (the same seed set computeLevels uses), so the path matches the lit graph.
function pathToSeed(i) {
  const seeds = new Set(focusSeeds);
  if (query) {
    const q = query.toLowerCase();
    nodes.forEach((n, j) => {
      if (n.path.toLowerCase().includes(q) || (n.cls || "").toLowerCase().includes(q)) seeds.add(j);
    });
  }
  if (!seeds.size || seeds.has(i)) return null;
  const prev = new Map([[i, -1]]);
  const queue = [i];
  for (let qi = 0; qi < queue.length; qi++) {
    const u = queue[qi];
    for (const v of (adj[u] || [])) {
      if (prev.has(v)) continue;
      prev.set(v, u);
      if (seeds.has(v)) {
        const hops = [];
        for (let x = v; x !== -1; x = prev.get(x)) hops.push(nodes[x].label);
        return hops.reverse();
      }
      queue.push(v);
    }
  }
  return null;   // no route: hovered node is outside the focus component
}

// ---- scene -----------------------------------------------------------------
// 3D/map split: the map pane is a permanent part of the layout (collapsible
// via #bMap). The 3D renderer owns everything left of it; both sizes derive
// from paneW so a divider drag reflows both at once.
const PANE_MIN = 180, PANE_DEFAULT = 800;
const paneMax = () => Math.max(PANE_MIN + 120, innerWidth - 320);   // 3D keeps >=320
const PANE_KEY = "neuronav.mapPaneW";
let mapVisible = true;   // pane ships open; #bMap collapses/expands it
let paneW = Math.max(PANE_MIN, Math.min(paneMax(),
  parseInt(localStorage.getItem(PANE_KEY) || "", 10) || PANE_DEFAULT));
const applyPaneW = () =>
  document.documentElement.style.setProperty("--pane-w", paneW + "px");
applyPaneW();
const glW = () => Math.max(320, innerWidth - (mapVisible ? paneW : 0));
const renderer = new THREE.WebGLRenderer({ antialias:true });
renderer.domElement.id = "gl";
renderer.setSize(glW(), innerHeight);
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
document.body.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.fog = new THREE.FogExp2(0x000000, 0.00022); // heavier fog washed out cluster hues at overview distance
const camera = new THREE.PerspectiveCamera(55, glW()/innerHeight, 1, 20000);
camera.position.set(0, 0, 1400);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
// idle auto-spin (the cbSpin checkbox toggles it); tick() pauses it
// while the user drags or inspects a fn box
controls.autoRotate = false;
controls.autoRotateSpeed = 0.35;
let spinEnabled = false;

// cluster hue: golden angle spread; dead files tinted toward red.
// lightness bands break golden-angle hue collisions across long cid runs
const hue = c => c < 0 ? 0.08 : (c * 0.61803398875 + 0.55) % 1;
const lightOf = c => 0.52 + 0.09 * (Math.floor(c / 13) % 3);
const colorOf = n => {
  // cycles lens: tint overrides cluster hue while the toggle is on
  if (cycOnly && n.cyc) return new THREE.Color(0xff6c60);
  // groups mode colors by supergroup id (few ids, well-spread hues)
  const cc = (groupsMode && n.gid >= 0) ? n.gid : n.cluster;
  // unclustered: neutral desaturated gray (old hue 0.08 read orange-red)
  const col = cc < 0 ? new THREE.Color().setHSL(0.55, 0.08, 0.62)
                     : new THREE.Color().setHSL(hue(cc), 0.72, lightOf(cc));
  if (n.dead > 0) col.lerp(new THREE.Color(1.0, 0.3, 0.2), n.dead >= 1 ? 0.85 : 0.68);
  return col;
};

const pos = new Float32Array(N * 3);
const colArr = new Float32Array(N * 3);
const sizes = new Float32Array(N);
const degree = new Float32Array(N);
// git-churn channel (optional): DATA.hot[i] in 0..1 = how often node i's
// file was touched in the last 90 days (max-touched file = 1). Absent when
// the generator ran outside a git repo — every fallback below no-ops.
const hot = DATA.hot || null;
// two-level navigation (optional): coarse supergroups from clusters.py;
// the "groups" toggle recolors nodes, halos and the legend by supergroup.
// Absent (no scipy/embeddings) → button hidden, nothing else changes.
const groups = DATA.groups || null;
const gNames = {};
if (groups) groups.forEach(g => { gNames[g.id] = g.label; });
let groupsMode = false;
// frozen baked layout: positions were settled offline in Python (seeded,
// deterministic) — the browser only renders. A missing DATA.pos means the
// offline pass failed and the build aborted halfway: fail loudly here
// rather than silently fall back to a live sim.
const frozenPos = DATA.pos;
if (!Array.isArray(frozenPos)) throw new Error("DATA.pos missing — offline layout failed");
links.forEach(l => { degree[l.s] += l.w; degree[l.t] += l.w; });
nodes.forEach((n, i) => {
  pos[i*3] = frozenPos[i][0]; pos[i*3+1] = frozenPos[i][1]; pos[i*3+2] = frozenPos[i][2];
  const c = colorOf(n);
  colArr[i*3] = c.r; colArr[i*3+1] = c.g; colArr[i*3+2] = c.b;
  // churn boost rides on top of the connectivity size (up to +35% radius
  // for the most-touched file) — subtle, never shrinks
  sizes[i] = Math.min(12, 4.5 + Math.sqrt(degree[i]) * 1.0) * (hot ? 1 + 0.35 * hot[i] : 1);
});
if (hot) document.getElementById("caption").textContent += " · size also encodes 90-day churn";

// spread control: uniformly re-scale the baked layout around its centroid.
// basePos holds the baked coordinates; pos is the live (scaled) copy that
// every renderer reads, so a slider change just re-derives pos + re-syncs.
const basePos = Float32Array.from(pos);
let spread = 1;
const supMem = new Uint8Array(N);
let fnMode = false;   // hoisted: the eval-time syncFileMesh() call below reads it via sphR
let focusHull = null;   // [#12] fn-tier focus container: {fill, rim, R, fi, boxes}
let _oHull = 0;         // effective hull rim opacity (lod fade, via __dbg)
let __routeFns = null;  // [#10] page probe surface: {turnsOf, mergeBends}
// satellite allowance: files with many fn boxes grow the sphere so the box
// ring keeps readable spacing (user call: bigger sphere, not smaller boxes).
// Threshold mirrors AGG_MAX (fn-layer local). Deterministic: pure fn of DATA.
const fnCount = nodes.map(n => (DATA.fns && DATA.fns[n.path] || []).length);
const satBoost = i =>
  fnCount[i] > 6 ? 1 + Math.min(0.8, 0.25 * Math.log2(fnCount[i] / 6)) : 1;
const sphR = i => sizes[i] * 1.1 * Math.sqrt(spread) *
  (fnMode && !supMem[i] ? satBoost(i) : 1);
// centroid of the baked layout — the affine center for spread transforms
let baseCx = 0, baseCy = 0, baseCz = 0;
for (let i = 0; i < N; i++) { baseCx += basePos[i*3]; baseCy += basePos[i*3+1]; baseCz += basePos[i*3+2]; }
baseCx /= N; baseCy /= N; baseCz /= N;
// absolute fog density must track the layout size (frameGraph recomputes
// it on reset/isolates): a constant tuned for one graph size fogs out far
// nodes once the relax inflates the layout
scene.fog.density = 0.17 / Math.max(420, graphBounds().radius * 1.55);
function applySpread(s) {
  if (s === spread) return;   // no-op: never fight camera tweens / mid-flight syncs
  const spreadPrev = spread;
  spread = s;
  // centroid of basePos is constant since boot (baseCx/Cy/Cz) - recomputing
  // per call was redundant with syncEdgePos's arc anchor
  const cx = baseCx, cy = baseCy, cz = baseCz;
  for (let i = 0; i < N; i++) {
    pos[i*3]   = cx + (basePos[i*3]   - cx) * s;
    pos[i*3+1] = cy + (basePos[i*3+1] - cy) * s;
    pos[i*3+2] = cz + (basePos[i*3+2] - cz) * s;
  }
  // fn satellites re-derive from their wires and edges re-attach — all via
  // applyVisibility (owns the fn layer + edge geometry + labels).
  // NO frameGraph here: spread must keep the user's zoom (reframing halved
  // apparent node size and made the graph unrecognizable); spheres grow by
  // sqrt(spread) in syncFileMesh so they stay readable as gaps open.
  // fog must weaken as the galaxy expands or far clusters sink into black;
  // frameGraph owns the absolute part (layout-size-derived), spread scales
  // it relatively
  scene.fog.density *= spreadPrev / s;
  syncFileMesh();
  applyVisibility();
  buildContainment();
  // the raycaster caches ONE bounding sphere for the whole instanced mesh on
  // its first hit-test; after a spread rescale that sphere is stale and every
  // node outside it silently fails to pick (frustumCulled=false does NOT
  // affect raycasting). Recompute so picking tracks the real layout.
  if (fileMesh) fileMesh.computeBoundingSphere();
}

const alphaArr = new Float32Array(N).fill(1);
// per-node alpha TARGETS: alphaArr eases toward these each tick (fade);
// visibility checks (raycast, edge kill, labels) read the targets
const alphaTgt = new Float32Array(N).fill(1);
// in-scene hover feedback: per-node scale eases toward 1.8 while hovered
// (lerped in tick's instance-matrix sync; tooltip text is unchanged)
const hoverScale = new Float32Array(N).fill(1);

// supernode collapse: when ON, each cluster with >= 3 visible members
// merges into one sphere at the member centroid. pos NEVER moves (the
// layout is frozen); dpos is the edge-ATTACHMENT copy — identical to pos
// normally, but a collapsed member's slot points at its cluster centroid
// so every edge re-targets the supernode.
let collapsed = false, fnWasOn = false;   // fnWasOn: fn-layer state across a collapse round-trip
const dpos = new Float32Array(N * 3);
dpos.set(pos);
// cluster -> { cx, cy, cz, n, first } for currently-collapsed clusters,
// plus per-node membership (alphaTgt is zeroed for members, so the edge
// dim pass needs this to keep supernode-carried edges bright)
const supCollapsed = new Map();
// satellite allowance block moved above the eval-time syncFileMesh() call
// PERCEPTUAL ANCHOR law (user sightings 3+4): ink arriving at a node implies
// that node's anchor READS. anchorBoost[i] = minimum DIAMETER in REF-px
// (normalized to 900px canvas height; the user's ~840h window shows 8 REF-px
// as ~7.5 real px) the sprite must draw while ink terminates on it; 6 was
// still speck-class to the eye, 8 REF-px diameter is the ruled bar.
// syncFileMesh lifts scale to meet it (capped so hierarchy survives).
const ANCHOR_PX = 8;   // endpoint anchor bar, DIAMETER ref-px
const anchorBoost = new Float32Array(N);
// zoomed-out size encoding: at engine scale the camera sits so far back
// that world-unit size differences (4.5 + sqrt(deg), capped 12) collapse
// to sub-pixel — every file renders the same ~1px speck and the
// size-by-connectivity signal is gone (corr(px, deg) 0.744 on the engine
// bake, p50 diameter 1.2px). degFloor[i] = minimum projected DIAMETER in
// px, scaled by log2(1+deg) so ordering survives: 2px for leaves, 7px
// for the hottest hubs (measured projection: engine corr 0.744 -> 0.982
// at +1.6% viewport ink; game corr 0.788 -> 0.998 at +0.44% — floors
// barely bind there, overview reads unchanged). syncFileMesh lifts the
// REST size to meet it (ANCHOR_PX precedent, same 4x cap, so near-field
// hierarchy is untouched and the floor self-disarms up close).
const degFloor = new Float32Array(N);
for (let i = 0; i < N; i++) {
  const f = 2 + 0.5 * Math.log2(1 + degree[i]);
  degFloor[i] = Math.max(2, Math.min(7, f));
}

// true 3D node geometry (billboard sprites read flat on screen): files =
// shaded spheres, functions = boxes orbiting their owner file sphere,
// variables later = tetrahedra. Per-instance color carries the cluster hue;
// hidden nodes collapse to scale 0 (zero rasterized fragments); dimmed nodes
// darken toward black instead of fading, so shading stays readable.
scene.add(new THREE.AmbientLight(0xffffff, 0.55));
const dirLight = new THREE.DirectionalLight(0xffffff, 1.4);
dirLight.position.set(0.4, 0.8, 0.65);
scene.add(dirLight);

// fixed-up cue: subtle polar grid below the galaxy — when orbiting deep,
// it is the only thing that keeps "down" readable (off by default; the
// ground button toggles it and its state survives resetAll)
let showGround = false;
let _minY = 1e9, _maxR = 0;
for (let i = 0; i < N; i++) {
  _minY = Math.min(_minY, pos[i*3+1]);
  _maxR = Math.max(_maxR, Math.hypot(pos[i*3], pos[i*3+2]));
}
const groundGrid = new THREE.PolarGridHelper(
  Math.max(600, _maxR * 1.05), 12, 5, 64, 0x27455c, 0x1a2f42);
groundGrid.position.y = _minY - 110;
groundGrid.material.transparent = true;
groundGrid.material.opacity = 0.35;
groundGrid.visible = showGround;
scene.add(groundGrid);

const fileMesh = new THREE.InstancedMesh(
  new THREE.SphereGeometry(1, 14, 10),
  new THREE.MeshLambertMaterial(),
  N
);
fileMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
// the union bounding sphere is computed once from boot positions; after a
// spread rescale it is stale and frustum-culls periphery instances from
// BOTH rendering and raycasting (outside nodes became unpickable)
fileMesh.frustumCulled = false;
scene.add(fileMesh);
const _dummy = new THREE.Object3D();
const _col = new THREE.Color();
const _sfTmp = new THREE.Vector3();
// whole-mesh skip: the loop's outputs are a pure function of (camera pose,
// per-node alpha/hover/anchor/highlight state, canvas height, fn sizing).
// At rest every input reproduces bit-for-bit, so the pass — 3,223 matrix
// composes + writes + a 245KB GPU upload — is skipped until one moves.
// _sfDirty is set wherever an input mutates (ease loop, compactAnim,
// applyVisibility, rebuildFnLayer, applyHighlight, refreshCollapse,
// resize3D, hover changes); camera motion is caught by the pose compare.
let _sfDirty = true;
const _sfCam = new THREE.Vector3();
function syncFileMesh() {
  // fn-ownership: while a fn box is hovered its owning file lifts hard
  // stale pick guard: fnMeta is rebuilt/cleared by rebuildFnLayer (focus
  // cleared, collapse, fn toggle) — a hoveredFn pointing past it would
  // crash this per-frame read and kill the tick loop
  if (hoveredFn >= 0 && !fnMeta[hoveredFn]) hoveredFn = -1;
  if (!_sfDirty && _sfCam.distanceToSquared(camera.position) < 1e-8) return;
  const fnOwner = hoveredFn >= 0 ? fnMeta[hoveredFn].file : -1;
  for (let i = 0; i < N; i++) {
    const a = alphaArr[i];
    let x = 0, y = 0, z = 0, sc = 0;
    if (a >= 0.01) {
      x = pos[i*3]; y = pos[i*3+1]; z = pos[i*3+2];
      // dead-only mode boosts the survivors so the red set reads at overview distance
      // sqrt(spread) size compensation: gaps scale ~spread, nodes scale
      // ~sqrt(spread) so pulling apart leaves them readable without a
      sc = sphR(i) * (deadOnly && nodes[i].dead > 0 ? 1.7 : 1) * hoverScale[i] * (0.45 + 0.55 * a);
      // perceptual anchor: if ink terminates on this node, hold the sprite
      // at ANCHOR_PX screen diameter — but floor the REST size and let
      // hoverScale ease from the FLOORED rest. Flooring the post-hover size
      // instead made the lift vanish the moment hover grew the sprite past
      // the floor, so the eased 1.8x read as 1.1x of the visible rest
      // (harness pin + user expectation: hover grows what the eye sees).
      if (anchorBoost[i] > 0 || degFloor[i] > 0 && alphaTgt[i] >= 0.5) {
        const dist = camera.position.distanceTo(_sfTmp.set(x, y, z));
        const hs = hoverScale[i] || 1;
        const base = sc / hs;   // rest size (alpha included), hover lifted out
        const rpxBase = base * (renderer.domElement.clientHeight / 2) /
                        (Math.tan(camera.fov * Math.PI / 360) * dist);
        if (rpxBase > 0.001 && rpxBase < anchorBoost[i])
          sc = base * Math.min(4.0, anchorBoost[i] / rpxBase) * hs;
        // degree-scaled minimum DIAMETER (rpxBase is a radius): only binds
        // when the projected size drops under the floor — up close, or on
        // the compact game layout, natural sizes win and nothing moves
        else if (rpxBase > 0.001 && rpxBase * 2 < degFloor[i])
          sc = base * Math.min(4.0, degFloor[i] / (2 * rpxBase)) * hs;
      }
    }
    // hovered nodes also lift slightly in brightness alongside the scale ease
    // search-match lift: pure brightness (no size change — labBox caches
    // label boxes and degFloor sizes; a multiplier here keeps both stable)
    const lift = a * (1 + 0.35 * (hoverScale[i] - 1)) * (i === fnOwner ? 1.9 : 1) *
                 (hlArr[i] ? 1.8 : 1);
    // [#12] the focused file dims to a core inside its container hull
    const hullDim = focusHull && i === focusHull.fi ? 0.62 : 1;
    const r = colArr[i*3] * lift * hullDim, g = colArr[i*3+1] * lift * hullDim,
          b = colArr[i*3+2] * lift * hullDim;
    _dummy.position.set(x, y, z);
    _dummy.scale.setScalar(sc);
    _dummy.updateMatrix();
    fileMesh.setMatrixAt(i, _dummy.matrix);
    _col.setRGB(r, g, b);
    fileMesh.setColorAt(i, _col);
  }
  fileMesh.instanceMatrix.needsUpdate = true;
  if (fileMesh.instanceColor) fileMesh.instanceColor.needsUpdate = true;
  _sfCam.copy(camera.position);
  _sfDirty = false;
}

// supernode spheres: second instanced mesh, capacity = cluster count.
// count + matrices/colors are rewritten by refreshCollapse whenever the
// collapse set changes; hidden entirely while the toggle is off.
let CLMAX = -1;
for (let i = 0; i < N; i++) if (nodes[i].cluster > CLMAX) CLMAX = nodes[i].cluster;
const supMesh = new THREE.InstancedMesh(
  new THREE.SphereGeometry(1, 14, 10),
  new THREE.MeshLambertMaterial(),
  Math.max(1, CLMAX + 1)
);
supMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
supMesh.frustumCulled = false;
supMesh.count = 0;
supMesh.visible = false;
scene.add(supMesh);
// rebuild dpos + the collapse set from the CURRENT alphaTgt (called from
// applyVisibility right after per-node targets are set). Members of a
// collapsed cluster get alphaTgt 0 — the existing hide path fades their
// spheres out and drops their hub labels — while their dpos slots point
// at the centroid so syncEdgePos re-targets every edge they carry.
function refreshCollapse() {
  dpos.set(pos);
  supCollapsed.clear(); supMem.fill(0);
  if (!collapsed) {
    supMesh.count = 0; supMesh.visible = false;
    return;
  }
  const byC = {};
  for (let i = 0; i < N; i++) {
    if (alphaTgt[i] <= 0.05) continue;   // "visible" = would render under current filters/focus
    const c = nodes[i].cluster;
    if (c < 0) continue;
    (byC[c] = byC[c] || []).push(i);
  }
  // ascending cluster ids, ascending member indices — deterministic
  Object.keys(byC).map(Number).sort((a, b) => a - b).forEach(c => {
    const members = byC[c];
    if (members.length < 3) return;
    let cx = 0, cy = 0, cz = 0;
    members.forEach(i => { cx += pos[i*3]; cy += pos[i*3+1]; cz += pos[i*3+2]; });
    cx /= members.length; cy /= members.length; cz /= members.length;
    supCollapsed.set(c, { cx, cy, cz, n: members.length, first: members[0] });
    members.forEach(i => {
      dpos[i*3] = cx; dpos[i*3+1] = cy; dpos[i*3+2] = cz;
      alphaTgt[i] = 0;
      supMem[i] = 1;
    });
  });
  let k = 0;
  for (const c of [...supCollapsed.keys()].sort((a, b) => a - b)) {
    const s = supCollapsed.get(c);
    _dummy.position.set(s.cx, s.cy, s.cz);
    // radius encodes the merged mass: 4 + sqrt(memberCount)
    _dummy.scale.setScalar(4 + Math.sqrt(s.n));
    _dummy.updateMatrix();
    supMesh.setMatrixAt(k, _dummy.matrix);
    // cluster hue via a member's frozen color slot
    _col.setRGB(colArr[s.first*3], colArr[s.first*3+1], colArr[s.first*3+2]);
    supMesh.setColorAt(k, _col);
    k++;
  }
  supMesh.count = k;
  supMesh.visible = k > 0;
  supMesh.instanceMatrix.needsUpdate = true;
  if (supMesh.instanceColor) supMesh.instanceColor.needsUpdate = true;
  if (k > 0) supMesh.computeBoundingSphere();
}

// edges: LineMaterial renders true pixel-width lines (WebGL caps
// LineBasicMaterial linewidth at 1px); one linewidth per material, so links
// are split into three weight buckets, each its own LineSegments2 over an
"""
