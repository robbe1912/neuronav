# vizjs/legend.py — rung 10/17 of the template join (issues
# #86 phase-3 / #299 A): legend chips (clusters + supergroups). Moved VERBATIM from viz.py —
# every edit here changes graph.html: regen the bake (the standing
# template law; test_viz refuses a bake older than viz.py/vizjs).
_JS_LEGEND = r"""const legend = document.getElementById("legend");
// chips double as the empty-state undo handles, so keep a cid -> element map
const legendChips = new Map();
function buildLegend() {
  legend.innerHTML = ""; legendChips.clear();
  if (groupsMode && groups) {
    // supergroup chips: one per group; clicking isolates all member
    // fine-clusters at once (activeClusters stays fine-grained underneath)
    const byG = {};
    nodes.forEach(n => { if (n.gid >= 0) byG[n.gid] = (byG[n.gid] || 0) + 1; });
    Object.entries(byG).sort((a, b) => b[1] - a[1]).forEach(([gid, count]) => {
      const g = +gid;
      const c = new THREE.Color().setHSL(hue(g), 0.72, lightOf(g));
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.style.background = `#${c.getHexString()}22`;
      chip.style.color = `#${c.getHexString()}`;
      chip.textContent = `${gNames[g] || "g" + g} · ${count}`;
      const cids = (groups.find(gr => gr.id === g) || {}).cids || [];
      chip.onclick = () => {
        const on = !chip.classList.contains("on");
        cids.forEach(cid => { if (on) activeClusters.add(cid); else activeClusters.delete(cid); });
        chip.classList.toggle("on", on);
        applyVisibility();
        if (activeClusters.size || activeDirs.size) frameVisible(); else frameGraph();
      };
      legend.appendChild(chip);
      cids.forEach(cid => legendChips.set(cid, chip));
    });
    return;
  }
  // fine clusters: ALL clusters get a chip (the panel scrolls)
  const topClusters = Object.entries(
    nodes.reduce((acc, n) => { if (n.cluster >= 0) acc[n.cluster] = (acc[n.cluster]||0)+1; return acc; }, {})
  ).sort((a,b) => b[1]-a[1]);
  topClusters.forEach(([cid, count]) => {
    const c = new THREE.Color().setHSL(hue(+cid), 0.72, lightOf(+cid));
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.style.background = `#${c.getHexString()}22`;
    chip.style.color = `#${c.getHexString()}`;
    chip.textContent = `${cNames[cid] || "c" + cid} · ${count}`;
    chip.onclick = () => {
      // multi-select: chips stack, each toggles its cluster independently
      if (activeClusters.has(+cid)) { activeClusters.delete(+cid); chip.classList.remove("on"); }
      else { activeClusters.add(+cid); chip.classList.add("on"); }
      applyVisibility();
      if (activeClusters.size || activeDirs.size) frameVisible(); else frameGraph();
    };
    legend.appendChild(chip);
    legendChips.set(+cid, chip);
  });
}
buildLegend();
if (!groups) document.getElementById("bGroups").style.display = "none";
document.getElementById("bGroups").onclick = e => {
  groupsMode = !groupsMode;
  e.target.classList.toggle("on", groupsMode);
  // fine-cluster selection from the other level is stale — drop it
  activeClusters.clear();
  buildLegend();
  buildContainment();
  applyVisibility();
};

// dir filter row: top-8 directories as chips + a tests chip (tests/tools
// files are hidden by default; toggling them in rebuilds containment)
const dirsEl = document.getElementById("dirs");
const dirChips = new Map();   // dir -> chip element (empty-state undo)
{
  const byDir = {};
  nodes.forEach(n => { if (!isTestNode(n)) byDir[n.dir] = (byDir[n.dir] || 0) + 1; });
  Object.entries(byDir).sort((a, b) => b[1] - a[1]).slice(0, 8).forEach(([dir, count]) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.style.color = "#b0bec5";
    // root-level files carry an empty dir string — label the chip, not a blank
    chip.textContent = (dir === "" ? "(root)" : dir) + " · " + count;
    chip.onclick = () => {
      // multi-select: chips stack, each toggles its dir independently
      if (activeDirs.has(dir)) { activeDirs.delete(dir); chip.classList.remove("on"); }
      else { activeDirs.add(dir); chip.classList.add("on"); }
      buildContainment(); applyVisibility();
      if (activeDirs.size || activeClusters.size) frameVisible(); else frameGraph();
    };
    dirsEl.appendChild(chip);
    dirChips.set(dir, chip);
  });
  const tchip = document.createElement("span");
  tchip.className = "chip";
  tchip.style.color = "#ffb74d";
  tchip.textContent = "tests";
  tchip.onclick = () => {
    // toggles tests/tools visibility only — dir selections are independent
    showTests = !showTests;
    tchip.classList.toggle("on", showTests);
    buildContainment(); applyVisibility();
  };
  dirsEl.appendChild(tchip);
}

document.getElementById("bDead").onclick = e => {
  deadOnly = !deadOnly;
  e.target.classList.toggle("on", deadOnly);
  applyVisibility();
  // frame the dead archipelago: 9-90 scattered files read better zoomed
  if (deadOnly) frameVisible();
};
document.getElementById("bCyc").onclick = e => {
  cycOnly = !cycOnly;
  e.target.classList.toggle("on", cycOnly);
  applyVisibility();
  if (cycOnly) frameVisible();
};
document.getElementById("bCalls").onclick = e => {
  showCalls = !showCalls;
  e.target.classList.toggle("on", showCalls);
  applyVisibility();
};
document.getElementById("bSignals").onclick = e => {
  showSignals = !showSignals;
  e.target.classList.toggle("on", showSignals);
  applyVisibility();
};
document.getElementById("bMut").onclick = e => {
  // fn layer must be on for the mutators filter to mean anything
  if (!fnMode) { fnMode = true; document.getElementById("cbFn").checked = true; }
  mutOnly = !mutOnly;
  e.target.classList.toggle("on", mutOnly);
  applyVisibility();
};
document.getElementById("bInst").onclick = e => {
  showInst = !showInst;
  e.target.classList.toggle("on", showInst);
  applyVisibility();
};
document.getElementById("bVar").onclick = e => {
  showVar = !showVar;
  e.target.classList.toggle("on", showVar);
  applyVisibility();
};
document.getElementById("bSemAff").onclick = e => {
  showSemAff = !showSemAff;   // UI state only — DATA never changes
  e.target.classList.toggle("on", showSemAff);
  applyVisibility();
};
document.getElementById("bGhost").onclick = e => {
  showGhost = !showGhost;   // quiet layer: ghost wires behind the budget
  e.target.classList.toggle("on", showGhost);
  applyVisibility();
};

const mapPane = document.getElementById("mapPane");
const MAP_MAX = 40;            // lit-node cap: past this the pane refuses
const FN_PORT_MAX = 4;          // roster rows per expanded box, then "+N more"
const NH = 22, RH = 13, GAPX = 12, TOP = 46;   // header / row / wrap gap / first-row Y
const GAPX_MAX = 120;   // placement stretches band gaps up to this (spread)
const CLUSTER_GAP_X = 32;    // horizontal air between cluster blocks in a band
const MAP_HUB_T1 = 8;        // hub degree threshold (median damping, trunks)
const MAP_FONT = sz => sz + "px ui-monospace, Menlo, Consolas, monospace";
// type glyphs (spec section 3): stroke color / dash pattern / terminator.
// one font constant (above) covers ALL map text.
const MGLYPH = {
  call:   { c: "#cfe0ea", dash: null,   term: "tri" },   // cooler: stops matching roster gray
  signal: { c: "#ffb347", dash: [6, 4], term: "hollow" },
  var:    { c: "#73e68c", dash: [2, 3], term: "dot" },
  attach: { c: "#3dccf2", dash: [1, 3], term: "tbar" },
  inst:   { c: "#3dccf2", dash: [1, 3], term: "tbar" },
};
// named-wire rows (spec section 0): [ty, sf, sfn, df, dfn, line, extra]
const mwires = DATA.mwires || [];
const mfns = DATA.fns || {};                // path -> [[fn, line], ...]
// wire degree (callee / caller in-degree over ALL rows): rank for the 3D
// wire-hover tooltip's "strongest wire" pick (map F14 rank family)
const wireDeg = new Map(), wireSrcDeg = new Map();
mwires.forEach(w => {
  const dk = w[3] + "::" + w[4], sk = w[1] + "::" + w[2];
  wireDeg.set(dk, (wireDeg.get(dk) || 0) + 1);
  wireSrcDeg.set(sk, (wireSrcDeg.get(sk) || 0) + 1);
});
let mapZ = 0, mapPX = 0, mapPY = 0;      // view: zoom + pan over the world
// zoom-gated ink tiers (paint-only declutter): the fine layers (underlays,
// named wires, port dots/arrowheads) hide when zoomed out and return with
// hysteresis so wheel jitter at the threshold cannot flicker. LAYOUT IS
// NEVER TOUCHED - the wiring diagram stays THE layout at every zoom.
// Thresholds are FIT-RELATIVE (set when a layout fits): fine ink stays ON at
// the fit zoom (wire clicks work at the overview) and drops one notch out.
let mapInkLo = 0.55, mapInkHi = 0.62;
let mapInkOn = true;
const mapInkEval = () => {
  if (!mapInkOn && mapZ >= mapInkHi) mapInkOn = true;
  else if (mapInkOn && mapZ < mapInkLo) mapInkOn = false;
};
// [#77] distance-gated wire LOD bands (paint tier, continues the ink-tier
// ladder): band 1 thins the label plane (roster rows fold to a count, box
// labels truncate, badge LOD tightens), band 2 aggregates to cluster
// corridors (intra-cluster spines fold away, per-pair badges merge into
// cluster-pair counts). LAYOUT IS NEVER TOUCHED - bands only choose what
// paint draws (VISSOFT'25: 15/16 viewers prefer distance-gated LOD; the
// map keeps ONE layout at every zoom, #63). Hysteresis like the ink tier;
// both entry thresholds sit far below the fit floor (world-width cap 1100
// keeps fit z >= ~0.5 on any sane pane), so the overview stays full
// fidelity and wheel jitter cannot flicker a band boundary.
const MAP_LOD_Z1 = 0.42, MAP_LOD_Z1X = 0.47, MAP_LOD_Z2 = 0.28, MAP_LOD_Z2X = 0.33;
let mapLodBand = 0;        // 0 full fidelity / 1 thinned / 2 cluster-aggregated
let mapLodFitZ = 0;        // zoom the current layout fit at (probe)
let mapLodSpinesPainted = 0;   // [#77] spine strokes this frame (probe)
let mapLodRosterShown = 0;     // [#77] roster rows painted this frame (probe)
let mapLodAggChips = 0;        // [#77] cluster-aggregate badges painted (probe)
let mapLodUnbundled = 0;       // [#77] riders painted unbundled (probe)
// [#61] hub-tap legibility floor: a tap is a 1-world-px access road; below
// MAP_TAP_FOLD_Z its stroke renders under half a screen px - illegible
// full fidelity on a super-hub fan. The tap folds (paint + pick parity)
// and the floored trunk + peel dots carry the read - the #77 band law
// extended to the aggregated hub tier.
const MAP_TAP_FOLD_Z = 0.5;
let mapLodInstAfford = 0;    // [#60] sole-relation underlays at affordance alpha (probe)
let mapLodInstAffordAlpha = 0;   // [#60/#210] max seg() alpha issued for afford ink (probe)
let mapLodInstAffordWidth = 0;   // [#60/#210] max seg() width issued for afford ink, screen px (probe)
let mapLodTapsPainted = 0;   // [#61] hub taps stroked this frame (probe)
let mapLodTapsFolded = 0;    // [#61] taps folded below the floor this frame (probe)
const mapLodEval = () => {
  if (mapLodBand === 0 && mapZ < MAP_LOD_Z1) mapLodBand = 1;
  else if (mapLodBand === 1 && mapZ >= MAP_LOD_Z1X) mapLodBand = 0;
  else if (mapLodBand === 1 && mapZ < MAP_LOD_Z2) mapLodBand = 2;
  else if (mapLodBand === 2 && mapZ >= MAP_LOD_Z2X) mapLodBand = 1;
};
// [#77] selection-time unbundling (AVI'12: bundling measurably degrades
// path tracing): while a map pin lives, the pinned PAIR leaves its bundle -
// riders draw straight/individual and the pair's corridor stroke + badge
// hide. The pin is the ONLY state; every frame derives both directions, so
// dismissal (Escape / void / focus change) restores band ink atomically.
const mapLodPinPair = () => {
  if (!wirePin || wirePin.surface !== "map" || !mapLayout) return null;
  if (wirePin.s !== undefined) return wirePin.s + "_" + wirePin.t;  // trunk/single
};
let mapDrag = null, mapDragged = false;
let mapDownPt = null;    // [issue #84] press origin: jitter-click resolution
let mapJitterHit = null; // [issue #84] press-on-ink + <10px drift = a pick
let mapRects = [];             // last drawn node rects (click hit-testing)
// map-center-on-selection: a 3D node click asks the 2D pane to pan the
// node's box to pane center (exactly once) and pulse it. Pane closed =
// clean no-op — nothing deferred to reopen.
let mapCenterReq = -1;
let mapPulse = null;
const MAP_CENTER_TOL_PX = 12, MAP_PULSE_MS = 900;
const mapCenterOn = i => { if (mapVisible) mapCenterReq = i; };
let mapVarsOn = false;         // var wires OFF by default, map-local chip [F10]
const mapExpandUser = new Map();   // file ix -> bool override (dblclick)
let mapHover = -1;             // hovered named-wire ix (L1 disclosure)
let mapHoverChip = -1;         // hovered bundle chip ix (cursor affordance)
let mapFrozenIx = -1;          // L3 roster-row pick: local dim 0.08, rows frozen
let mapLayout = null;          // layout cache - keyed (focus, expansion, size)
let mapDirty = false;          // rAF dirty flag: one draw per frame [F7]
let mapRefocusTimer = 0;       // click-vs-dblclick discriminator on headers
let mapVarsChipRect = null;    // screen-space vars chip rect (click hit)
function sizeMapPane() {
  const dpr = Math.min(devicePixelRatio || 1, 2);
  mapPane.width = Math.round((mapPane.clientWidth || 440) * dpr);
  mapPane.height = Math.round((mapPane.clientHeight || innerHeight) * dpr);
}
// css twin of the 3D cluster palette: stroke / translucent body fill / text
function mapCols(c) {
  if (c < 0) return { s: "hsl(198,8%,62%)", f: "hsl(198,10%,14%)", t: "hsl(198,8%,84%)" };
  const h = Math.round(hue(c) * 360), l = Math.round(lightOf(c) * 100);
  return { s: `hsl(${h},72%,${l}%)`, f: `hsl(${h},30%,12%)`, t: `hsl(${h},72%,${Math.min(92, l +
  22)}%)` };
}
// ---- map-local DOM overlays: tooltip (L1), bundle list (section 7),
// "+N more" fn picker (section 4). One container spans the pane area.
const mapOvEl = document.createElement("div");
mapOvEl.id = "mapOv";
mapOvEl.innerHTML = '<div id="mapTip"></div><div id="mapList"></div>' +
  '<div id="mapPick"><input placeholder="filter fns..."><div class="rows"></div></div>';
document.body.appendChild(mapOvEl);
const mapTipEl = mapOvEl.querySelector("#mapTip");
const mapListEl = mapOvEl.querySelector("#mapList");
const mapPickEl = mapOvEl.querySelector("#mapPick");
const mapPickIn = mapOvEl.querySelector("#mapPick input");
const mapPickRows = mapOvEl.querySelector("#mapPick .rows");
let mapPickRc = null;
let mapListChip = -1;   // [issue #113] chip ix whose bundle list is open
function mapTipHide() { mapTipEl.style.display = "none"; }
function mapClosePick() { mapPickEl.style.display = "none"; mapPickRc = null; }
// ---- [issue #82] sticky wire selection -----------------------------------
// ONE pinned wire at a time, PAINT-TIER ONLY: pinning never touches the
// layout cache (ONE-layout law: sig unchanged, byte-stable bake, wires
// set invariant). The emphasis re-resolves from an identity KEY on every
// paint, so the pin survives pan / zoom / hover-out / repaints until it
// is explicitly dismissed. Indices are NOT identity: layout rebuilds
"""
