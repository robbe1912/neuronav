# vizjs/labels3d_tail.py — rung 7/17 of the template join (issues
# #86 phase-3 / #299 A): label solver tail (depth-tier label sets). Moved VERBATIM from viz.py —
# every edit here changes graph.html: regen the bake (the standing
# template law; test_viz refuses a bake older than viz.py/vizjs).
_JS_LABELS3D_TAIL = r"""// across data drift while depth 2-3 sets stay clean
const DETAIL_MAX = Math.max(120, nodes.length * 0.35);
const elabsEl = document.getElementById("elabs");
let eLabs = [], detailMode = false;
const TY_NAME = { call: "call", signal: "signal", inst: "contains",
  attach: "contains", var: "var" };
const tyCss = c => "rgb(" + Math.round(c.r * 255) + "," +
  Math.round(c.g * 255) + "," + Math.round(c.b * 255) + ")";
function rebuildEdgeLabels(focusing) {
  eLabs = [];
  elabsEl.innerHTML = "";
  detailMode = false;
  if (!focusing) return;
  let lit = 0;
  for (let i = 0; i < N; i++) if (level[i] >= 0 && nodeVisible(nodes[i])) lit++;
  if (lit === 0 || lit > DETAIL_MAX) return;
  detailMode = true;
  // strongest edges claim labels first (weight desc); collision-skip in
  // updateEdgeLabels hides the rest
  const cand = [];
  links.forEach((l, i) => {
    if (!typeVisible(l.ty) || level[l.s] < 0 || level[l.t] < 0) return;
    if (budgetLit && !budgetLit.has(i)) return;   // ghost wires carry no label
    cand.push({ i, w: l.w });
  });
  cand.sort((a, b) => b.w - a.w);
  cand.slice(0, 120).forEach(({ i }) => {
    const l = links[i];
    const el = document.createElement("div");
    el.className = "elab";
    el.textContent = TY_NAME[l.ty] + " ×" + l.w;
    el.style.color = tyCss(TYPE_COLORS[l.ty] || TYPE_COLORS.var);
    elabsEl.appendChild(el);
    eLabs.push({ i, el });
  });
}
function updateEdgeLabels() {
  if (!detailMode) return;
  const w = renderer.domElement.clientWidth, h = renderer.domElement.clientHeight;
  const placed = [];
  for (const { i, el } of eLabs) {
    const l = links[i];
    if (alphaTgt[l.s] < 0.5 || alphaTgt[l.t] < 0.5) { el.style.display = "none"; continue; }
    hubV.set((pos[l.s*3] + pos[l.t*3]) / 2, (pos[l.s*3+1] + pos[l.t*3+1]) / 2,
      (pos[l.s*3+2] + pos[l.t*3+2]) / 2).project(camera);
    if (hubV.z > 1 || Math.abs(hubV.x) > 1.02 || Math.abs(hubV.y) > 1.02) {
      el.style.display = "none"; continue;
    }
    const x = (hubV.x * 0.5 + 0.5) * w, y = (-hubV.y * 0.5 + 0.5) * h;
    el.style.display = "block";
    // try a small vertical nudge before letting collision-skip hide it
    el.style.transform = "translate(" + x.toFixed(1) + "px," + y.toFixed(1) +
      "px) translate(-50%,-50%)";
    placed.push({ el, x, y, r: labBox(el, x, y, 0.5) });
  }
  // collision-skip: hub labels win, then stronger (earlier) edge labels win;
  // losing labels try a small nudge before hiding (hub boxes from
  // updateHubs — no DOM reads)
  const hubRects = hubBoxes;
  const taken = [];
  for (const p of placed) {
    if (taken.length >= 24) { p.el.style.display = "none"; continue; }
    const res = placeLabels(p.el, p.x, p.y, [[0, 0], ...[-13, 11, -26].map(dy => [0, dy])],
      "-50%,-50%", 0.5, r => hubRects.every(hr => separate(r, hr, 2)) && taken.every(t => separate(r, t, 2)));
    if (res.hit) taken.push(res.r); else p.el.style.display = "none";
  }
}

// ---- crosstalk corridors: top inter-cluster pairs, labeled at the arc ----
// baked in DATA.meta.crosstalk (count desc); each pair claims the heaviest
// highway arc between its two clusters and labels that arc's baked midpoint
// (pts[8]). Overview-only: hidden while focusing or once the camera closes
// inside the LOD threshold (same gate family as the overview edge state).
const xtEl = document.getElementById("xtlabs");
let xtLabs = [];
(function buildXtLabs() {
  const xt = m.crosstalk || [];
  xt.forEach(p => {
    let best = null;
    for (const [li, pts] of hw) {
      const l = links[li];
      const ca = nodes[l.s].cluster, cb = nodes[l.t].cluster;
      if ((ca === p.a && cb === p.b) || (ca === p.b && cb === p.a)) {
        if (!best || l.w > best.w) best = { w: l.w, mid: pts[8] };
      }
    }
    if (!best) return;
    const el = document.createElement("div");
    el.className = "xtlab";
    el.textContent = (cNames[p.a] || "c" + p.a) + " - " +
      (cNames[p.b] || "c" + p.b) + " ×" + p.n;
    xtEl.appendChild(el);
    xtLabs.push({ mid: best.mid, el });
  });
})();
function updateXtLabels() {
  // collapse hides crosstalk labels too: the arcs they annotate re-target to
  // supernode centroids, so the "A - B ×n" captions would float over merged
  // piles pointing at nothing
  const overview = !collapsed && !focusSeeds.size &&
    camera.position.distanceTo(controls.target) >= lodDist;
  if (!overview) {
    xtLabs.forEach(k => { k.el.style.display = "none"; });
    return;
  }
  const w = renderer.domElement.clientWidth, h = renderer.domElement.clientHeight;
  // greedy collision skip: two corridor labels stacked on the same screen
  // region read as garbage; the later one yields (first come = highest
  // crosstalk count, since meta.crosstalk is baked count-desc)
  const taken = [];
  for (const k of xtLabs) {
    hubV.set(k.mid[0], k.mid[1], k.mid[2]).project(camera);
    if (hubV.z > 1 || Math.abs(hubV.x) > 1.05 || Math.abs(hubV.y) > 1.05) {
      k.el.style.display = "none"; continue;
    }
    k.el.style.display = "block";
    const x = (hubV.x * 0.5 + 0.5) * w, y = (-hubV.y * 0.5 + 0.5) * h;
    const res = placeLabels(k.el, x, y, [[0, 0]], "-50%,-50%", 0.5,
      r => taken.every(t => separate(r, t, 4)));
    if (res.hit) taken.push(res.r); else k.el.style.display = "none";
  }
}

// ---- hub labels: top-degree visible files, projected to screen each frame ---
// label LOD (Gource --dir-name-depth / Obsidian text-fade steal): overview
// keeps the classic top-12 landmarks; zooming in raises the cap so context
// names appear exactly when the user is close enough to read them
// [issue #84] skeptic #3: hub/file labels are pointer-events:auto
// chips floating over the 3D canvas. A press on a label never reaches
// the canvas, so an orbit grab that starts on a label was dead (the
// label swallowed the pointerdown; OrbitControls binds it on the
// canvas only). Once the press moves >4px we hand the gesture over
// with one synthetic pointerdown at the current point - OrbitControls
// (r160) binds move/up on ownerDocument, so the rest of the drag is
// native. The label's own click is suppressed for that press (chromium
// fires click even after a large same-element drag, which would have
// refocused the node on every orbit release).
function armLabelDrag(el) {
  let sx = 0, sy = 0, fwd = false;
  el.addEventListener("pointerdown", e => { sx = e.clientX; sy = e.clientY; fwd = false; });
  el.addEventListener("pointermove", e => {
    if (fwd || !sx) return;
    if (Math.hypot(e.clientX - sx, e.clientY - sy) < 4) return;
    fwd = true; el.__labDrag = true;
    renderer.domElement.dispatchEvent(new PointerEvent("pointerdown", {
      clientX: e.clientX, clientY: e.clientY, pointerId: e.pointerId,
      pointerType: "mouse", isPrimary: true, buttons: 1, bubbles: true }));
  });
  const done = () => { sx = 0; fwd = false; };
  el.addEventListener("pointerup", done);
  el.addEventListener("pointercancel", done);
  el.addEventListener("click", e => {
    if (el.__labDrag) {
      el.__labDrag = false; done();
      e.stopImmediatePropagation(); e.preventDefault();
    }
  }, true);
}

const HUB_N = 12;
const HUB_MAX = 40;
let hubCapNow = HUB_N;   // live zoom-driven cap, exposed via __dbg.hubCap
// [#8] super-hub landing degrade: past SUPER_DEG (weight-sum degree —
// the number the hub chip shows) a landing folds its 1-hop label fan —
// the magnitude tier behind the #77 band law, applied to the 3D chip
// plane. Labels only: spheres, wires and the corridor census are
// untouched (corridor-complete law); the fold chip is the folded set's
// affordance and one click unfolds the full fan for this landing.
const SUPER_DEG = 100;
const SUPER_FLAB_N = 16;   // top-weight neighbor chips the fold keeps
const SUPER_HALO = 24;     // clean band (px) around the focused hub chip
let flabUnfold = false;    // the fold chip's one-click expansion state
let flabFoldSig = "";      // seed signature the unfold belongs to
let flabFoldState = { super: false, shown: 0, folded: 0 };   // __dbg.focusFold
let focusHubBox = null;    // focused hub's placed chip rect this frame
const hubsEl = document.getElementById("hubs");
let hubs = [];
const hubV = new THREE.Vector3();
function rebuildHubs() {
  const vis = [];
  // degree-0 files render near-invisible spheres - their labels would
  // float as orphans, so they never earn one
  for (let i = 0; i < N; i++) if (degree[i] > 0 && nodeVisible(nodes[i])) vis.push(i);
  vis.sort((a, b) => degree[b] - degree[a]);
  hubsEl.innerHTML = "";
  hubs = vis.slice(0, HUB_MAX).map(i => {
    const el = document.createElement("div");
    el.className = "hub" + (hlArr[i] ? " hl" : "");
    el.textContent = nodes[i].label + " · " + Math.round(degree[i]);
    el.title = nodes[i].path;
    el.onpointerenter = () => { tip.style.display = "none"; };
    armLabelDrag(el);
    el.onclick = () => { pushFocusState(); showInfo(i); focusSeeds.clear(); focusSeeds.add(i); applyVisibility(); focus(i); mapCenterOn(i); };
    hubsEl.appendChild(el);
    return { i, el };
  });
}
// label hysteresis: each hub remembers the offset that last placed cleanly
// (keyed by node index). While that offset stays collision-free at the new
// projected position it is reused verbatim — no candidate re-search, so
// labels stop hopping between rows while the camera orbits. The search
// reruns only on a real collision or after a hidden frame.
const hubOff = new Map();
// junction bollards + delivery chevrons as screen-space obstacle points —
// shared by ALL label placers (skeptic r4 #3: hub/cluster labels sat on
// dots and trunks; only fn labels avoided them before)
const _obstV = new THREE.Vector3();
function juncArrowObstacles(w, h) {
  const pts = [];
  for (const mesh of [jdot && jdot.mesh, fnArrows]) {
    if (!mesh) continue;
    const am = mesh.instanceMatrix.array;
    for (let i = 0; i < am.length / 16; i++) {
      // issue #6: a RIDING mark paints mid-leg, but its delivery
      // reservation is the box site -- hidden arrows always parked
      // there, so pinning riders to fnArrowBox keeps the obstacle
      // set (and therefore label placement) identical to the
      // box-site law. Direct chevron-label hits stay measured
      // against the painted anchor by the census (chevInLabel).
      if (mesh === fnArrows && fnArrowBox && _arrowShown.length === am.length / 16
          && _arrowShown[i] && _arrowOccl[i]
          && _arrowRide[i] >= 0 && _arrowRide[i] < 1)
        _obstV.set(fnArrowBox[i*3], fnArrowBox[i*3+1], fnArrowBox[i*3+2]).project(camera);
      else _obstV.set(am[i*16+12], am[i*16+13], am[i*16+14]).project(camera);
      if (_obstV.z <= 1 && Math.abs(_obstV.x) <= 1.05 && Math.abs(_obstV.y) <= 1.05) {
        toScreen(_obstV, w, h);   // #299 D
        pts.push([_scr[0], _scr[1]]);
      }
    }
  }
  return pts;
}

function updateHubs() {
  const w = renderer.domElement.clientWidth, h = renderer.domElement.clientHeight;
  // zoom-driven cap: squared falloff so labels bloom in as you approach
  const camDist = camera.position.distanceTo(controls.target);
  const cap = camDist >= lodDist * 1.2 ? HUB_N
    : Math.min(HUB_MAX, Math.max(HUB_N, Math.round(HUB_N / Math.pow(camDist / (lodDist * 1.2), 2))));
  hubCapNow = cap;
  // junction bollards + delivery chevrons are obstacles (skeptic r4 #3)
  const obstPts = juncArrowObstacles(w, h);
  const clearOfDots = r => obstPts.every(q =>
    q[0] < r.left - 8 || q[0] > r.right + 8 || q[1] < r.top - 8 || q[1] > r.bottom + 8);
  // greedy placement against cached boxes (see labBox); transforms only
  // touch these few absolutely-positioned nodes, so no DOM reads remain.
  // vertical rows first (keeps label near its node), then sideways nudges
  hubBoxes.length = 0;   // this frame's placed boxes — the module-level
                         // array the other placers collide against
  focusHubBox = null;    // [#8] refreshed below when the focused hub places
  const fixed = hubBoxes;
  for (let hi = 0; hi < hubs.length; hi++) {
    const { i, el } = hubs[hi];
    if (hi >= cap || alphaTgt[i] < 0.5) { el.style.display = "none"; hubOff.delete(i); continue; }
    hubV.set(pos[i*3], pos[i*3+1], pos[i*3+2]).project(camera);
    if (hubV.z > 1 || Math.abs(hubV.x) > 1.02 || Math.abs(hubV.y) > 1.02) {
      el.style.display = "none"; hubOff.delete(i); continue;
    }
    const rawX = (hubV.x * 0.5 + 0.5) * w;
    // panel-avoidance: a label shoved >80px sideways from its node to
    // clear the info panel reads as an orphan — hide it instead
    if (310 - rawX > 80) { el.style.display = "none"; hubOff.delete(i); continue; }
    // clamp inside the viewport but clear of the left info panel
    const x = Math.max(310, Math.min(w - 30, rawX)), y = Math.max(16, Math.min(h - 26, (-hubV.y * 0.5 + 0.5) * h));
    el.style.display = "block";
    const ok = r => fixed.every(f => separate(r, f, 4)) && clearOfDots(r);
    const prev = hubOff.get(i);
    if (prev) {
      const res = placeLabels(el, x, y, [[prev.dx, prev.dy]], "-50%,0", 0, ok);
      if (res.hit) { fixed.push(res.r); if (i === focusFileIdx) focusHubBox = res.r; continue; }
    }
    const offs = [];
    for (const dy of [-19, 17, -42, 41, -65, 65, -88, 88])
      for (const dx of [0, 100, -100]) offs.push([dx, dy]);
    const res = placeLabels(el, x, y, offs, "-50%,0", 0, ok);
    if (res.hit) hubOff.set(i, { dx: res.o[0], dy: res.o[1] });
    fixed.push(res.r);
    if (i === focusFileIdx) focusHubBox = res.r;
  }
}
let stubExits = [];     // EXPLAINED EXIT dissolve points this frame (focus-file
const legTermOn = new Map();  // per-leg chain-key -> terminus projects on-screen (tick fills)
const chainGate = new Map();  // per-chain first failing gate this frame (sighting #10 probe)
const inkKeys = new Set();    // chains carrying ink this frame (serve loop fills; sighting #11)
let wireTipAnchor = null;     // pinned card's anchor meta — lifetime-tracked per frame
function updateStubLabs() {
  // EXPLAINED EXIT labels: compact "→ station · file" at each dissolve
  // point (cap 4). On-viewport stubs first (busPts order — deterministic);
  // off-screen dissolve points get no label — nothing to explain where
  // there is no ink. Hidden when no exits.
  const host = document.getElementById("stubLabs");
  if (!host) return;
  const vw = renderer.domElement.clientWidth || 1600, vh = renderer.domElement.clientHeight || 900;
  // NDC -> px at LABEL time (canvas may resize between capture and draw;
  // map pane shifts #gl width — glW() — so live conversion stays true)
  const px = stubExits.map(e => ({ x: (e.nx + 1) / 2 * vw, y: (1 - e.ny) / 2 * vh, fi: e.fi, dest: e.dest }));
  // #panel (z10) and the map pane occlude canvas ink — a dissolve point
  // hidden under them has no visible terminus, so no label either (the
  // law binds labels to VISIBLE dissolve ink, not to geometry)
  const panel = document.getElementById("panel");
  const pr = panel ? panel.getBoundingClientRect() : null;
  const mapP = document.getElementById("mapPane");
  const mr = mapP && document.body.classList.contains("mapOpen") ? mapP.getBoundingClientRect() : null;
  const occl = (x, y) => (pr && x > pr.left - 8 && x < pr.right + 8 && y > pr.top - 8 && y < pr.bottom + 8) ||
                         (mr && x > mr.left - 8 && x < mr.right + 8);
  const on = px.filter(e => e.x > 8 && e.x < vw - 8 && e.y > 8 && e.y < vh - 8 && !occl(e.x, e.y));
  const want = (on.length ? on : []).slice(0, 4);
  while (host.children.length < want.length) {
    const el = document.createElement("div");
    el.className = "stublab"; host.appendChild(el);
  }
  for (let i = 0; i < host.children.length; i++) {
    const el = host.children[i];
    if (i < want.length) {
      const nm = nodes[want[i].fi] && nodes[want[i].fi].label || "?";
      el.style.display = "block";
      el.style.transform = "translate(" + Math.round(want[i].x + 8) + "px," +
                           Math.round(want[i].y - 8) + "px)";
      el.textContent = "→ station · " + nm;
    } else el.style.display = "none";
  }
}
// boot rebuildHubs() deleted - boot applyVisibility() re-runs it before the
// first render

// ---- function-level layer (files inside the current focus) -------------------
"""
