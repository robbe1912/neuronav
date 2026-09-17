# vizjs/labels3d.py — rung 5/17 of the template join (issues
# #86 phase-3 / #299 A): 3D label planes + UI helpers. Moved VERBATIM from viz.py —
# every edit here changes graph.html: regen the bake (the standing
# template law; test_viz refuses a bake older than viz.py/vizjs).
_JS_LABELS3D = r"""// ---- UI ---------------------------------------------------------------------
const stats = document.getElementById("stats");
const m = DATA.meta;
// dead counts are function-level candidates; the map flags a file only when
// >= 40% of its funcs are candidates — surface both so "28+62" vs 9 lit
// files in dead-only mode doesn't read as missing data
const flagged = nodes.reduce((a, n) => a + (n.dead > 0 ? 1 : 0), 0);
stats.innerHTML = `${m.files} files · ${m.edges} links · ${m.clusters} clusters · <span title="${m.deadLikely} likely + ${m.deadReview} review-tier dead-FUNCTION candidates across the repo; a file is flagged (and shown in dead-only mode) when at least 40% of its funcs are candidates — currently ${flagged} files">dead ${m.deadLikely}+${m.deadReview} → ${flagged} files</span> · drag orbit · wheel zoom` +
  (m.strata ? " · height = call depth from entry" : "") +
  (m.generated_at ? `<br>gen ${m.generated_at}${m.git ? " · " + m.git : ""}` : "");
const edgeLegend = document.getElementById("edgeLegend");
// the legend is honest about what is on screen: the overview renders edges
// as weight-tinted gray, so the boot legend says so; typed colors return
// with a focus, one key per type still toggled on (var never shows — off)
function updateEdgeLegend(focusing) {
  edgeLegend.innerHTML = "";
  const addKey = (name, c) => {
    const k = document.createElement("span");
    k.className = "eKey";
    const sw = document.createElement("i");
    sw.style.borderTopColor = "#" + c.getHexString();
    k.appendChild(sw);
    k.appendChild(document.createTextNode(name));
    edgeLegend.appendChild(k);
  };
  if (!focusing) {
    addKey("edges", new THREE.Color(0.55, 0.60, 0.66));
    if (showSemAff) addKey("affinity", SEM_AFF_COLOR);
    const hint = document.createElement("span");
    hint.className = "eHint";
    hint.textContent = "colors appear when you focus a node";
    edgeLegend.appendChild(hint);
  } else {
    if (showCalls) addKey("call", TYPE_COLORS.call);
    if (showSignals) addKey("signal", TYPE_COLORS.signal);
    if (showInst) addKey("contains", TYPE_COLORS.inst);
    if (showVar) addKey("var", TYPE_COLORS.var);
    if (showSemAff) addKey("affinity", SEM_AFF_COLOR);
  }
}
// boot updateEdgeLegend(false) deleted - boot applyVisibility() re-runs it
// before the first render

// ---- containment: per-cluster halo ring + name at centroid (overview) -----
// names come from DATA.meta.clusterNames (extractor's cluster labeler fills
// the slot later); until then labels fall back to the cN chip id
const cNames = (m.clusterNames || {});
const clabsEl = document.getElementById("clabs");
let cLabs = [], cRings = [];
// rebuilt when the tests/dir filters change: hidden files leave their
// cluster, so centroids + halo extents must be recomputed (positions frozen)
function buildContainment() {
  cRings.forEach(r => { scene.remove(r); r.geometry.dispose(); r.material.dispose(); });
  cRings = []; cLabs.forEach(c => c.el.remove()); cLabs = [];
  const byC = {};
  // groups mode draws the halo ring per SUPERGROUP (matching node colors)
  const keyOf = n => groupsMode ? n.gid : n.cluster;
  nodes.forEach((n, i) => {
    const k = keyOf(n);
    if (k >= 0 && nodeVisible(n)) (byC[k] = byC[k] || []).push(i);
  });
  Object.entries(byC).sort((a, b) => b[1].length - a[1].length)
    .forEach(([cid, members]) => {
      let cx = 0, cy = 0, cz = 0;
      members.forEach(i => { cx += pos[i*3]; cy += pos[i*3+1]; cz += pos[i*3+2]; });
      cx /= members.length; cy /= members.length; cz /= members.length;
      let r = 60;
      members.forEach(i => {
        r = Math.max(r, Math.hypot(pos[i*3]-cx, pos[i*3+1]-cy, pos[i*3+2]-cz));
      });
      r *= 1.12;
      const col = new THREE.Color().setHSL(hue(+cid), 0.72, lightOf(+cid));
      // strata layout: halo rings assume planar cluster blobs — depth-
      // stretched clusters would ring mid-air. Keep the name labels only.
      if (!m.strata) {
        const segs = 72, pts = [];
        for (let s = 0; s <= segs; s++) {
          const a = s / segs * Math.PI * 2;
          pts.push(new THREE.Vector3(cx + Math.cos(a)*r, cy, cz + Math.sin(a)*r));
        }
        const ring = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts),
          new THREE.LineBasicMaterial({ color: col, transparent: true, opacity: 0.14,
            blending: THREE.AdditiveBlending, depthWrite: false }));
        scene.add(ring); cRings.push(ring);
      }
      const el = document.createElement("div");
      el.className = "clab";
      el.style.color = "#" + col.getHexString();
      const nm = groupsMode ? (gNames[+cid] || "g" + cid) : (cNames[cid] || "c" + cid);
      // collapsed clusters announce the merged mass on the label
      const sup = collapsed ? supCollapsed.get(+cid) : null;
      el.textContent = sup ? nm + " · " + sup.n + " files" : nm + " · " + members.length;
      clabsEl.appendChild(el);
      cLabs.push({ cx, cy, cz, el });
    });
}
// same hysteresis as hub labels: a cluster name keeps its placement (radial
// slot or row) while it stays collision-free at the freshly projected
// centroid; the candidate search reruns only on collision or after a hidden
// frame. Radial-outward wins first: 40px from the centroid along the
// screen-space direction away from the galaxy center of mass.
// Label placement rects without DOM reads. The placers used to call
// getBoundingClientRect per candidate — at engine scale that is ~800 forced
// layout reads per frame, the single largest frame cost (idle included;
// profiler: 63% of focus-frame time). Label text is fixed between rebuilds
// and only translate() moves, so the border-box size never changes: measure
// once per element, then rebuild rects in JS around the anchor the
// transform writes to. ay mirrors each placer's translate(-50%,ay*100%):
// 0 = box top at y (hubs), 0.5 = centered (clab/elab/xtlab), 1 = box bottom
// at y (flabs). Boxes are plain {left,top,right,bottom} — same shape the
// predicates already consume.
const hubBoxes = [];   // this frame's placed hub boxes; updateHubs (first
                       // placer in tick) fills it, every later placer
                       // collides against it instead of re-reading the DOM
function labBox(el, x, y, ay) {
  let w = el.__lw, h = el.__lh;
  if (w === undefined) {   // first frame visible: one real measure, cached
    const r = el.getBoundingClientRect();
    w = el.__lw = r.width; h = el.__lh = r.height;
  }
  const top = y - ay * h;
  return { left: x - w / 2, right: x + w / 2, top, bottom: top + h };
}
const clabOff = new Map();
function updateClusterLabs() {
  const w = renderer.domElement.clientWidth, h = renderer.domElement.clientHeight;
  // hub pills win collisions; cluster names try placements around the
  // centroid. Their boxes come from updateHubs (ran first this tick) —
  // no DOM reads here.
  const hubRects = hubBoxes;
  // project every centroid once; the mean of the on-screen projections is
  // the galaxy center of mass the radial candidates point away from
  const proj = [];
  let gx = 0, gy = 0, gn = 0;
  for (const c of cLabs) {
    if (clabsEl.style.display === "none") { proj.push(null); continue; }
    hubV.set(c.cx, c.cy, c.cz).project(camera);
    if (hubV.z > 1 || Math.abs(hubV.x) > 1.05 || Math.abs(hubV.y) > 1.05) {
      proj.push(null); continue;
    }
    const px = (hubV.x*0.5+0.5)*w, py = (-hubV.y*0.5+0.5)*h;
    proj.push({ px, py });
    gx += px; gy += py; gn++;
  }
  gx /= (gn || 1); gy /= (gn || 1);
  const taken = [];
  const clabObst = juncArrowObstacles(w, h);   // skeptic r4 #3
  cLabs.forEach((c, k) => {
    const pj = proj[k];
    if (!pj) { c.el.style.display = "none"; clabOff.delete(c.el.textContent); return; }
    c.el.style.display = "block";
    const px = pj.px, py = pj.py;
    const key = c.el.textContent;
    const dx = px - gx, dy2 = py - gy;
    const dl = Math.hypot(dx, dy2) || 1;
    const rx = px + dx / dl * 40, ry = py + dy2 / dl * 40;
    const ok = r => hubRects.every(hr => separate(r, hr, 4)) && taken.every(t => separate(r, t, 4)) &&
      clabObst.every(q => q[0] < r.left - 6 || q[0] > r.right + 6 ||
                          q[1] < r.top - 6 || q[1] > r.bottom + 6);
    const prev = clabOff.get(key);
    if (prev !== undefined) {
      const res = prev.rad ? placeLabels(c.el, rx, ry, [[0, 0]], "-50%,-50%", 0.5, ok)
                           : placeLabels(c.el, px, py, [[0, prev.dy]], "-50%,-50%", 0.5, ok);
      if (res.hit) { taken.push(res.r); return; }
    }
    const resRad = placeLabels(c.el, rx, ry, [[0, 0]], "-50%,-50%", 0.5, ok);
    if (resRad.hit) { clabOff.set(key, { rad: true }); taken.push(resRad.r); return; }
    const res = placeLabels(c.el, px, py, [0, -34, 34, -64, 64].map(dy => [0, dy]), "-50%,-50%", 0.5, ok);
    if (res.hit) { clabOff.set(key, { rad: false, dy: res.o[1] }); taken.push(res.r); return; }
    c.el.style.display = "none"; clabOff.delete(key);
  });
}

const tip = document.getElementById("tip");
const crumb = document.getElementById("crumb");
const esc = s => String(s).replace(/[&<>"]/g,
  ch => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;" })[ch]);
  const raycaster = new THREE.Raycaster();
  const mouse = new THREE.Vector2();
const _pickV = new THREE.Vector3();   // scratch for screen-space pick accuracy
  let hovered = -1, hoveredFn = -1;
  // hover greyout (Cosmograph pattern): hovering a node greys everything
  // outside its 1-hop neighborhood to 0.12 alpha — zero-click orientation.
  // hoverGreyIdx = the node currently greyed (-1 = clean overview);
  // focusActive mirrors applyVisibility's focusing so focus mode owns the
  // scene and hover must not fight its BFS dimming.
  let hoverGreyIdx = -1, focusActive = false;
let hubRing = null;   // additive halo marking the focused hub (budget fan origin)
let deadOnly = false, query = "";
let mutOnly = false;   // fn layer: show only functions that write state
const activeClusters = new Set();   // multi-select cluster filter (legend chips)
let showInst = false, showCalls = true, showSignals = true, showVar = false, showGhost = false, depth = 1;
// focus roots: single click replaces, shift-click stacks (BFS is multi-seed)
const focusSeeds = new Set();
// direction mode: 0 = both, 1 = out (downstream impact), 2 = in (upstream deps)
let dirMode = 0;
// tests/tools hidden by default (chip toggles them in); dir filter row works
// like the cluster chips — both only ever filter, never re-layout
// (declarations live above syncEdgePos: the boot-time geometry writer
// reads them through linkFiltered)
// overview LOD: intra-cluster edges stay hidden until the camera closes in
// (zoom threshold maintained by the controls 'change' listener below)
let lodClose = false, lodDist = 1e9;
const level = new Int16Array(N).fill(-1);
// hub edge budget: a focus used to light EVERY link in the lit subgraph —
// a 150-wire hub rendered as a radial starburst ('wire salad', unreadable).
// The budget keeps only the top HUB_EDGE_BUDGET links by weight (call
// count) fully lit; the rest drop to ghost ink: GHOST_K × the focus bucket
// opacity (0.75) ≈ alpha 0.06. Hovering a budgeted wire re-lights it
// (hoverEdgeLi) — detail on demand. The fn layer is UNCAPPED between lit
// files (amendment: the focus neighborhood is small + compacted, so every
// fn interconnection renders; HUB_FN_BUDGET is retired).
const HUB_EDGE_BUDGET = 12;
const GHOST_K = 0.08;
// ---- semantic-affinity wires (#279) ---------------------------------------
// The J9 mutual-kNN pairs the layout springs on, promoted to INK: violet
// dotted strands in their own color family (no structural type is violet),
// a hint layer, never a dependency. Serve law mirrors the straight chords:
// the row inventory always exists in the buffers (presence), but a row
// serves ink only while the toggle is on, the zoom tier resolves file
// boxes (lodClose — the corridor-tier gate; at overview these pairs read
// as salad) and both endpoints survive the current filters (ghost law:
// alphaTgt < 0.05 collapses the row, exactly like a chord).
let showSemAff = true;   // UI state only — DATA never changes (#279)
let semServed = 0;       // rows serving ink this frame (probe truth)
const semAff = DATA.semAff || [];
const SEM_AFF_COLOR = new THREE.Color(0.72, 0.57, 1.0);   // violet family
// card rows ride the SAME capped rows the wires ride (#279 parity)
const semByNode = new Map();
const semAdd = (i, j, s) => {
  if (!semByNode.has(i)) semByNode.set(i, []);
  semByNode.get(i).push({ j, s });
};
semAff.forEach(([a, b, s]) => { semAdd(a, b, s); semAdd(b, a, s); });
let semMesh = null, semPosIB = null, semColIB = null;
if (semAff.length) {
  const g = new LineSegmentsGeometry();
  g.setPositions(new Float32Array(semAff.length * 6));
  g.setColors(new Float32Array(semAff.length * 6));
  const m = new LineMaterial({
    vertexColors: true, linewidth: 1.25, worldUnits: false,
    // faint on purpose: hint ink must never compete with the call/signal
    // corridors — own hue + dotted + sub-bucket opacity
    transparent: true, opacity: 0.30, alphaToCoverage: false,
    blending: THREE.NormalBlending, depthWrite: false,
    dashed: true, dashSize: 2.5, gapSize: 6,
  });
  m.resolution.set(glW(), innerHeight);
  semMesh = new LineSegments2(g, m);
  semMesh.frustumCulled = false;   // positions mutate per frame
  scene.add(semMesh);
  semPosIB = g.attributes.instanceStart.data;
  semColIB = g.attributes.instanceColorStart.data;
}
function syncSemAff() {
  if (!semMesh) return;
  const pa = semPosIB.array, ca = semColIB.array;
  const gate = showSemAff && lodClose;
  semServed = 0;
  semAff.forEach(([a, b, s], k) => {
    const o = k * 6;
    const ax = dpos[a*3], ay = dpos[a*3+1], az = dpos[a*3+2];
    // collapse law (syncEdgePos): hidden rows degenerate onto endpoint a —
    // black alone is NOT hidden under normal blending
    if (!gate || alphaTgt[a] < 0.05 || alphaTgt[b] < 0.05 ||
        nodeFiltered(nodes[a]) || nodeFiltered(nodes[b])) {
      pa[o] = ax; pa[o+1] = ay; pa[o+2] = az;
      pa[o+3] = ax; pa[o+4] = ay + 0.05; pa[o+5] = az;
      ca[o] = ca[o+1] = ca[o+2] = ca[o+3] = ca[o+4] = ca[o+5] = 0;
      return;
    }
    const bx = dpos[b*3], by = dpos[b*3+1], bz = dpos[b*3+2];
    let ex = bx - ax, ey = by - ay, ez = bz - az;
    const el = Math.sqrt(ex*ex + ey*ey + ez*ez);
    const trA = trimAt(a), trB = trimAt(b);
    if (el - trA - trB <= 0.05) {   // stub law: never feed normalize(0)
      pa[o] = ax; pa[o+1] = ay; pa[o+2] = az;
      pa[o+3] = ax; pa[o+4] = ay + 0.05; pa[o+5] = az;
      ca[o] = ca[o+1] = ca[o+2] = ca[o+3] = ca[o+4] = ca[o+5] = 0;
      return;
    }
    ex /= el; ey /= el; ez /= el;
    pa[o]   = ax + ex * trA; pa[o+1] = ay + ey * trA; pa[o+2] = az + ez * trA;
    pa[o+3] = bx - ex * trB; pa[o+4] = by - ey * trB; pa[o+5] = bz - ez * trB;
    // brightness carries the score: cosine 0.45 (the J9 floor) -> 0.55,
    // cosine 1.0 (byte-identical twins) -> 1.0
    const kb = 0.55 + 0.45 * Math.min(1, (s - 0.45) / 0.55);
    ca[o] = ca[o+3] = SEM_AFF_COLOR.r * kb;
    ca[o+1] = ca[o+4] = SEM_AFF_COLOR.g * kb;
    ca[o+2] = ca[o+5] = SEM_AFF_COLOR.b * kb;
    semServed++;
  });
  semPosIB.needsUpdate = true;
  semColIB.needsUpdate = true;
  semMesh.visible = showSemAff;   // the toggle kills the species outright
  if (semMesh.visible) semMesh.computeLineDistances();   // dashes track endpoints
}
syncSemAff();   // boot fill; later syncs hook applyVisibility + tick's ease
"""
