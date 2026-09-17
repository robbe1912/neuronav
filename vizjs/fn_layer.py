# vizjs/fn_layer.py — rung 8/17 of the template join (issues
# #86 phase-3 / #299 A): fn-layer state block (fnMesh..rebuildFnLayer-1). Moved VERBATIM from viz.py —
# every edit here changes graph.html: regen the bake (the standing
# template law; test_viz refuses a bake older than viz.py/vizjs).
_JS_FN_LAYER = r"""let fnMesh = null, fnLines = null, fnStalks = null, fnMeta = [], fnArrows = null, fnQuiet = null;
  let fnTrunkN = 0;   // file-pair bus trunks in the current fn layer (via __dbg)
  // conduit lane law: ALWAYS +Y — a -Y lift drops the conduit down INTO
  // the fn-box swarm it is supposed to overfly. Consecutive shared-
  // corridor trunks (trunkGeom order = deterministic visEdges order) get
  // tiered control lifts, so quadratic apexes sit 0.11/0.135/0.16*dist
  // above the midpoint and never stack on each other.
  const CONDUIT_LIFT_BASE = 0.14;  // band 0.14-0.29 over 6 tiers, step 0.03
  // below (apex law >= 0.11*dist holds); 0.22 was overflight — the hills
  // interleaved on screen and read as braid even where chords never cross
  // quiet-tier consolidation: file pairs with >= QUIET_TRUNK_MIN quiet
  // wires collapse into ONE background trunk (QUIET_LIFT_FRAC apex lift)
  const QUIET_TRUNK_MIN = 3, QUIET_LIFT_FRAC = 0.22;
let fnTrunkW = 0;   // wires riding trunks (each emits entry+exit ramps)
let fnJstubN = 0;   // shared junction legs: Jof delivery stubs + station tree legs
let fnStationsArr = [];  // per-file bus stations of the current fn layer (via __dbg)
let fnJclearV = -1; // min world junction->box-center distance (via __dbg)
let fnLegN = 0;     // station tree legs (thin conduits, via __dbg)
// #299 D: the junction-bollard arrays (k size factor, of owner file, st
// station-class gate, legs per station, key attachment key) live as fields
// on the jdot record below — one teardown unit, not five more module vars
const stAttKey = new Set();  // served attachment keys (serve loop fills, jDot gate reads)
let fnBoxScale = null; // fi -> largest rendered fn-box world size
let stationTks = null; // fi -> trunk keys leaving that file's stations
let arrowFile = null;  // owner FILE index per delivery chevron
let _lod = null;       // per-frame LOD closures (res / trOK / stOK)
let _lodServe = false; // focus-state master gate (busLodInit) — clamps the
                       // distance fade below: served layer renders at full
                       // opacity (skeptic r5-final: 0.60/0.40 dims made the
                       // serving d2 state illegible — geometry served, paint
                       // faded)
let _oDot = 0, _oArrow = 0;   // effective opacities for fnLod reporting
// click-parity (user r5): the white trunk conduits + ivory junction dots
// must pick EXACTLY like the colored bus wires — same metas, same tip card
let trunkMetaMap = null;   // busPts key "a>b" -> {kind:"trunk", sf, tf, mates}
let busPtsMeta = null;     // parallel to busPts: per-conduit pick meta
let juncPickInfo = null;   // parallel to busJunc: per-dot pick meta
let legendOpen = false;    // 3D legend chip state (harness-pinned)
let fnLodV = null;     // per-frame LOD report (via __dbg.fnLod, round-5)
let fnJclip = 0;    // conduits whose obstacle lift hit the cap (via __dbg)
let fnQuietTrunkN = 0;  // quiet-tier trunk arcs (via __dbg)
let fnQuietTrunkW = 0;  // quiet wires absorbed into trunks (via __dbg)
let fnBus = null;   // trunk conduit bodies (InstancedMesh cylinders)
let busPts = null;  // segment endpoints for per-frame screen-constant rescale
let fnBusRi = null; // current per-segment radius (world units)
let jdot = null;  // #299 D: the junction record {mesh, pos, r, tg, of, st,
                  // legs, key, k} — reroute bollards + their parallel arrays,
                  // born in one build, dying in one registry entry (issue #5
                  // dodge axes ride along; __dbg.jDotArrays keeps its shape)
  let fnArrowPos = null, fnArrowR = null;   // arrowhead positions + current radii (screen-constant)
  let fnArrowTang = null;  // world wire tangent at each delivery (chevron aim)
  let fnArrowBox = null;  // delivery box center per arrow (screen-hug clamp)

// aimArrows(): every delivery chevron billboard-faces the camera with
// local +Y along the wire's projected tangent, sized by the EXACT
// screen-px law (half-height 5px -> ~10px tall chevron at any zoom —
// skeptic r4 bar >=8px). All inputs (camera, fov, viewport, arrow world
// data) are deterministic per view, so R10 determinism holds.
let _arrowOccl = new Float32Array(0);   // 1 = delivery chevron occluded
let _arrowOcclCam = null;              // camera pos of the last occlusion pass
let _arrowOcclDirty = true;            // occluder geometry moved since last pass
let _arrowOcclPasses = 0;              // probe: occlusion passes since boot
let fnArrowLeg, fnArrowHalo = null;  // per-arrow delivery leg [from(3), liftFrac] (issue #6)
let _arrowRide = new Float32Array(0);  // low-lod ride t (-1 = no visible leg sample)
let _arrowShown = new Uint8Array(0);   // shown flag this frame (consolidation pick)
let _chevCarry = new Uint8Array(0);    // 1 = arrow carried into fnArrows.count this frame
const _chevPick = new Map();           // fi -> carried arrow (low-lod consolidation)
// issue #6: below 2x ANCHOR_PX a file's per-fn-box delivery fan collapses to
// a speck blob at land distances (cu land: 13 arrows on a 6px box cluster, all
// buried behind the compact ball). The file then carries ONE mark which rides
// its delivery leg; zoomed-in fans (>= CHEV_FAN_PX) are untouched.
const CHEV_FAN_PX = 16;
// near-box steps first: the label solver reserves the <=14px band
// around each box for its delivery mark, so a rider must sit as
// close to its box as visibility allows (t=1 is the box end)
const RIDE_TS = [0.95, 0.9, 0.85, 0.8, 0.6, 0.4, 0.2, 0.05];
// #299 D: the fn layer's asset registry — one teardown entry per heap
// asset, registered once. rebuildFnLayer walks the list instead of nine
// hand-maintained if-blocks (the #58 missed-teardown family: every new
// asset needed its own block, and blocks were missed). Entries are
// idempotent null-guards (dispose/killFatLines no-op on null); adding
// an asset = adding its entry here.
const layerAssets = [
  () => { fnMesh = dispose(fnMesh, true); },
  () => { fnLines = killFatLines(fnLines); },
  () => { fnQuiet = killFatLines(fnQuiet); },
  () => { fnBus = dispose(fnBus); busPts = null; fnBusRi = null; busPtsMeta = null; trunkMetaMap = null; juncPickInfo = null; },
  () => { if (jdot) { jdot.mesh = dispose(jdot.mesh); } jdot = null; },
  () => { fnArrows = dispose(fnArrows); fnArrowPos = null; fnArrowR = null; fnArrowTang = null; fnArrowBox = null; arrowFile = null; },
  // issue #6: halo + delivery legs are rebuilt with the bus -- tear them
  // down with the arrows or ghost discs persist past Escape (#58 family)
  // and a stale leg array could alias a same-count rebuild
  () => { if (fnArrowHalo) { scene.remove(fnArrowHalo); fnArrowHalo.geometry.dispose(); fnArrowHalo.material.dispose(); } fnArrowHalo = null; fnArrowLeg = null; },
  () => { fnStalks = dispose(fnStalks); },
  () => { if (focusHull) {
    scene.remove(focusHull.fill);
    scene.remove(focusHull.rim);
    focusHull.fill.geometry.dispose(); focusHull.fill.material.dispose();
    focusHull.rim.geometry.dispose(); focusHull.rim.material.dispose();
    focusHull = null; _oHull = 0;
  } },
];
function teardownLayer() { for (const fn of layerAssets) fn(); }
// delivery-leg bezier (emitArc curve: mid + liftFrac*dist in +Y, B = box).
// legPt/legTan reproduce the painted arc from the captured leg params —
// through the SAME q-leaves emitArc paints with, so rider and arc cannot
// drift apart (the measured chevron-off-wire bug class).
function legMid(i) {
  const ax = fnArrowLeg[i*4], ay = fnArrowLeg[i*4+1], az = fnArrowLeg[i*4+2];
  const bx = fnArrowBox[i*3], by = fnArrowBox[i*3+1], bz = fnArrowBox[i*3+2];
  return qMid(ax, ay, az, bx, by, bz,
              fnArrowLeg[i*4+3] * qSpan(ax, ay, az, bx, by, bz));
}
function legPt(i, t, out) {
  const ax = fnArrowLeg[i*4], ay = fnArrowLeg[i*4+1], az = fnArrowLeg[i*4+2];
  const bx = fnArrowBox[i*3], by = fnArrowBox[i*3+1], bz = fnArrowBox[i*3+2];
  const m = legMid(i);
  return out.copy(qPt(ax, ay, az, m[0], m[1], m[2], bx, by, bz, t));
}
function legTan(i, t, out) {
  const ax = fnArrowLeg[i*4], ay = fnArrowLeg[i*4+1], az = fnArrowLeg[i*4+2];
  const bx = fnArrowBox[i*3], by = fnArrowBox[i*3+1], bz = fnArrowBox[i*3+2];
  const m = legMid(i);
  return out.copy(qTan(ax, ay, az, m[0], m[1], m[2], bx, by, bz, t));
}
function aimArrows() {
  if (!fnArrows || !fnArrowPos || !fnArrowTang || !fnArrowR) return;
  const hpx = renderer.domElement.clientHeight || 900;
  const wuPerPx = 1 / refPxPerWu(1, hpx);
  const M = new THREE.Matrix4(), X = new THREE.Vector3(), Y = new THREE.Vector3(),
        Z = new THREE.Vector3(), P = new THREE.Vector3(), T = new THREE.Vector3();
  const a = fnArrows.instanceMatrix.array;
  const lod = _lod || busLodInit();   // round-5: no chevrons on sub-pixel boxes
  // VISIBLE-OR-GONE (skeptic r4): a chevron buried behind a sphere swarm
  // reads as noise — depthTest:false paints it over everything anyway, so
  // gate on a real ray. Recomputed only when the camera moves (closed-form
  // inputs; determinism per view holds).
  if (_arrowOccl.length !== fnArrowR.length) {
    _arrowOccl = new Float32Array(fnArrowR.length);
    _arrowOcclCam = null;
    _arrowRide = new Float32Array(fnArrowR.length);
    _arrowShown = new Uint8Array(fnArrowR.length);
    _chevCarry = new Uint8Array(fnArrowR.length);
  }
  // Camera-ε + dirty gate (issue #33 perf round): the pass is a pure
  // function of (camera pose, occluder geometry), so it only needs to
  // re-run when one of those moved. Damping frames keep re-running while
  // the pose drifts; the last pass lands within ε (1e-4 wu ≈ 1e-5 px) of
  // the settled pose, so the settle-moment state stays a pure function
  // of the final view — the determinism the old recompute-every-frame
  // guaranteed, without its 0.4ms/ray cost at rest. Geometry moves
  // without the camera (compactAnim lerp, alpha/hover eases, visibility
  // flips) set _arrowOcclDirty from their sites.
  if (_arrowOcclDirty || !_arrowOcclCam ||
      _arrowOcclCam.distanceToSquared(camera.position) > 1e-8) {
    _arrowOcclPasses++;
    _arrowOccl.fill(0);
    const rc = new THREE.Raycaster();
    rc.far = Infinity;
    const dir = new THREE.Vector3(), org = new THREE.Vector3();
    const occ = (fileMesh.visible ? [fileMesh, fnMesh, fnBus] : [fnMesh, fnBus])
                .filter(Boolean);
    for (let i = 0; i < fnArrowR.length; i++) {
      org.copy(camera.position);
      dir.set(fnArrowPos[i*3], fnArrowPos[i*3+1], fnArrowPos[i*3+2]).sub(org);
      const L = dir.length() || 1;
      dir.divideScalar(L);
      rc.set(org, dir);
      rc.far = L - 1;   // anything solid closer than the chevron blocks it
      const hits = rc.intersectObjects(occ, false);
      // the delivery chevron rides 3.5wu off its TARGET box center — when
      // the wire arrives from the far side, the box's near face legitimately
      // sits between camera and chevron. That is the DELIVERY, not an
      // occluder: ignore hits on the own target box (within 4wu of it)
      let blocked = false;
      const bo = fnArrowBox ? [fnArrowBox[i*3], fnArrowBox[i*3+1], fnArrowBox[i*3+2]] : null;
      for (const h of hits) {
        if (bo && h.point.distanceTo(new THREE.Vector3(bo[0], bo[1], bo[2])) < 4) continue;
        blocked = true; break;
      }
      if (blocked) _arrowOccl[i] = 1;
    }
    // LOW-LOD RIDE (issue #6): at land distances most delivery boxes
    // sit behind the compact ball (cu land: 13/18) -- the occlusion
    // above is honest, so instead of un-hiding (chevCrowd wall), the
    // mark slides DOWN ITS OWN delivery leg to the last visible
    // bezier sample: direction rides where the wire ink is visible.
    // Samples ignore hits near the sample (the wire itself) and near
    // the own box, same occluder set as the box ray. High-LOD arrows
    // (pxOf >= CHEV_FAN_PX) never ride -- zoomed views are unchanged.
    if (fnArrowLeg && fnArrowLeg.length === fnArrowR.length * 4 &&
        arrowFile && arrowFile.length) {
      const S = new THREE.Vector3(), org2 = new THREE.Vector3(),
            dir2 = new THREE.Vector3(), bo2 = new THREE.Vector3();
      for (let i = 0; i < fnArrowR.length; i++) {
        _arrowRide[i] = -1;
        const fi = arrowFile[i];
        if (fi < 0 || lod.pxOf(fi) >= CHEV_FAN_PX) continue;
        if (!lod.resA(fi)) continue;         // not even served: hidden
        if (!_arrowOccl[i]) { _arrowRide[i] = 1; continue; }  // box clear
        bo2.set(fnArrowBox[i*3], fnArrowBox[i*3+1], fnArrowBox[i*3+2]);
        for (let r = 0; r < RIDE_TS.length; r++) {
          legPt(i, RIDE_TS[r], S);
          org2.copy(camera.position);
          dir2.copy(S).sub(org2);
          const L2 = dir2.length() || 1; dir2.divideScalar(L2);
          rc.set(org2, dir2); rc.far = L2 - 1;
          let clear = true;
          for (const h of rc.intersectObjects(occ, false)) {
            if (h.point.distanceTo(S) < 4) continue;    // the wire itself
            if (h.point.distanceTo(bo2) < 4) continue;  // own delivery box
            clear = false; break;
          }
          if (clear) { _arrowRide[i] = RIDE_TS[r]; break; }
        }
      }
    }
    if (!_arrowOcclCam) _arrowOcclCam = new THREE.Vector3();
    _arrowOcclCam.copy(camera.position);
    _arrowOcclDirty = false;
  }
  const w = renderer.domElement.clientWidth || 1600, h = hpx;
  const hasLeg = fnArrowLeg && fnArrowLeg.length === fnArrowR.length * 4;
  // pass 1 -- shown flags: the box-site law (lodOk + clear ray) plus the
  // low-lod ride override (issue #6): a buried box keeps its mark when the
  // delivery leg has a visible sample to ride
  for (let i = 0; i < fnArrowR.length; i++) {
    const fi = arrowFile && arrowFile.length ? arrowFile[i] : -1;
    const lodOk = fi < 0 || lod.resA(fi);
    const low = fi >= 0 && hasLeg && lod.pxOf(fi) < CHEV_FAN_PX;
    _arrowShown[i] = lodOk &&
      (!_arrowOccl[i] || (low && _arrowRide[i] >= 0)) ? 1 : 0;
  }
  // pass 2 -- low-lod consolidation (issue #6, fewer marks carry the
  // signal): a file whose boxes are specks (< CHEV_FAN_PX) carries ONE
  // delivery mark -- the first arrow with a visible anchor, else the first
  // arrow. Unowned and zoomed-in arrows always carry: high-LOD fans are
  // exactly today's.
  let KC = fnArrowR.length;
  if (arrowFile && arrowFile.length && hasLeg) {
    _chevPick.clear(); KC = 0;
    for (let i = 0; i < fnArrowR.length; i++) {
      const fi = arrowFile[i];
      if (fi < 0 || lod.pxOf(fi) >= CHEV_FAN_PX) { _chevCarry[i] = 1; KC++; continue; }
      const prev = _chevPick.get(fi);
      if (prev === undefined) { _chevPick.set(fi, i); _chevCarry[i] = 1; KC++; }
      else if (!_arrowShown[prev] && _arrowShown[i]) {
        _chevCarry[prev] = 0; _chevPick.set(fi, i); _chevCarry[i] = 1;
      } else _chevCarry[i] = 0;
    }
  } else _chevCarry.fill(1);
  // pass 3 -- anchors + matrices. Carried arrows pack into slots [0,KC)
  // and fnArrows.count = KC (the marks this view carries); dropped arrows
  // keep a collapsed-at-box matrix in tail slots, so label obstacles and
  // census discs see exactly the discs a hidden arrow shows today.
  let slotC = 0, slotT = KC;
  for (let i = 0; i < fnArrowR.length; i++) {
    const fi = arrowFile && arrowFile.length ? arrowFile[i] : -1;
    const low = fi >= 0 && hasLeg && lod.pxOf(fi) < CHEV_FAN_PX;
    const ride = low && _arrowOccl[i] ? _arrowRide[i] : -1;
    P.set(fnArrowPos[i*3], fnArrowPos[i*3+1], fnArrowPos[i*3+2]);
    const bx0 = fnArrowBox ? fnArrowBox[i*3] : 0, by0 = fnArrowBox ? fnArrowBox[i*3+1] : 0,
          bz0 = fnArrowBox ? fnArrowBox[i*3+2] : 0;
    if (ride >= 0 && ride < 1) {
      // riding the delivery leg: anchor = bezier at the last visible
      // sample, V aims along the LOCAL wire tangent (issue #6)
      legPt(i, ride, P);
      legTan(i, ride, T);
    } else T.set(fnArrowTang[i*3], fnArrowTang[i*3+1], fnArrowTang[i*3+2]);
    Z.subVectors(camera.position, P).normalize();
    // project the wire tangent into the billboard plane; degenerate
    // (tangent along the view axis) falls back to world-up
    Y.copy(T).addScaledVector(Z, -T.dot(Z));
    if (Y.lengthSq() < 1e-6) Y.set(0, 1, 0).addScaledVector(Z, -Z.y);
    Y.normalize();
    X.crossVectors(Y, Z);
    // screen-hug clamp: at grazing angles a 3.5wu world offset projects
    // 30-60px from the box (skeptic aFar >25px bar) -- pull the anchor
    // toward the box until it sits <=14px from it ON SCREEN. Riding
    // anchors (ride >= 0) sit mid-leg ON the wire -- the hug would
    // drag them back into the occluded pile, so it only applies to
    // box anchors (ride < 0).
    if (ride < 0 && fnArrowBox) {
      _obstV.set(fnArrowBox[i*3], fnArrowBox[i*3+1], fnArrowBox[i*3+2]).project(camera);
      toScreen(_obstV, w, h);   // #299 D: shared NDC->canvas
      const qx = _scr[0], qy = _scr[1];
      for (let t = 1; t > 0.02; t -= 0.08) {
        _obstV.set(bx0 + (fnArrowPos[i*3]-bx0)*t, by0 + (fnArrowPos[i*3+1]-by0)*t,
                   bz0 + (fnArrowPos[i*3+2]-bz0)*t).project(camera);
        toScreen(_obstV, w, h);
        const ax2 = _scr[0], ay2 = _scr[1];
        if (Math.hypot(ax2-qx, ay2-qy) <= 14) { P.set(
          bx0 + (fnArrowPos[i*3]-bx0)*t, by0 + (fnArrowPos[i*3+1]-by0)*t,
          bz0 + (fnArrowPos[i*3+2]-bz0)*t); break; }
        if (t <= 0.12) P.set(bx0, by0, bz0);   // extreme grazing: park ON the box
      }
    }
    // not-shown deliveries collapse INTO their box (scale 0) -- tested a
    // 0.55x/0.45x hint V to lift chevObs, but each un-hidden arrow adds
    // chevCrowd pairs at delivery sites (hub6 zoomin 16->19, tol +2):
    // hidden stays. (issue #6 rides/consolidates instead of un-hiding.)
    if (!_arrowShown[i] && fnArrowBox) P.set(fnArrowBox[i*3], fnArrowBox[i*3+1], fnArrowBox[i*3+2]);
    const d = P.distanceTo(camera.position) || 1;
    // 8px-floor law, ref-px form: half-height 8px on the nominal 900px
    // canvas at ANY window -- a fraction of frame height, so the V's
    // saturated mass stays readable at the smallest window we test
    const s = 8 * (2 * Math.tan(camera.fov * Math.PI / 360) / 900) * d
             * (_arrowShown[i] ? 1 : 0);
    if (fnLodV && s > 0.01 && _chevCarry[i]) {
      fnLodV.chevShown++;
      fnLodV.minChevPx = Math.min(fnLodV.minChevPx, ((2 * s) / (wuPerPx * d)) * (900 / hpx));
    }
    // keep the probe-visible record in sync (fnArrowPos = CURRENT anchor)
    fnArrowPos[i*3] = P.x; fnArrowPos[i*3+1] = P.y; fnArrowPos[i*3+2] = P.z;
    M.makeBasis(X.multiplyScalar(s), Y.multiplyScalar(s), Z);
    M.setPosition(P);
    M.toArray(a, (_chevCarry[i] ? slotC++ : slotT++) * 16);
    if (fnArrowHalo) {
      // same anchor/billboard, 0.95 disc vs the V's 0.72 half-width;
      // X/Y already carry s (0 when hidden), so the halo appears
      // exactly where a chevron does and hides with it
      M.makeBasis(X, Y, Z);
      M.setPosition(P);
      M.toArray(fnArrowHalo.instanceMatrix.array, i * 16);
    }
  }
  fnArrows.count = KC;
  fnArrows.instanceMatrix.needsUpdate = true;
  if (fnArrowHalo) { fnArrowHalo.count = fnArrowR.length;
    fnArrowHalo.instanceMatrix.needsUpdate = true; }
}

// ---- focus labels: name neighboring files + function satellites on focus ----
const flabsEl = document.getElementById("flabs");
let fLabs = [];
function rebuildFocusLabels(focusing) {
  fLabs = [];
  flabsEl.innerHTML = "";
  const sig = [...focusSeeds].join(",");
  if (sig !== flabFoldSig) { flabFoldSig = sig; flabUnfold = false; }
  const fi = focusFileIdx;
  const superHub = fi >= 0 && degree[fi] > SUPER_DEG && !flabUnfold;
  flabFoldState = { super: superHub, shown: 0, folded: 0 };
  if (!focusing) return;
  // every directly-connected file node gets its name back (hubs own
  // theirs); on a super-hub landing only the top SUPER_FLAB_N by link
  // weight survive — the rest fold into one count chip
  const hubIdx = new Set(hubs.map(h => h.i));
  const nbrs = [];
  for (let i = 0; i < N; i++)
    if (level[i] === 1 && !hubIdx.has(i) && alphaTgt[i] > 0.5) nbrs.push(i);
  if (superHub) {
    const wgt = new Map();   // weight ranking mirrors the budget-arc pass
    links.forEach(l => {
      if (!typeVisible(l.ty)) return;
      const o = l.s === fi ? l.t : l.t === fi ? l.s : -1;
      if (o >= 0) wgt.set(o, (wgt.get(o) || 0) + l.w);
    });
    nbrs.sort((a, b) => (wgt.get(b) || 0) - (wgt.get(a) || 0) || a - b);
  }
  const shown = superHub ? nbrs.slice(0, SUPER_FLAB_N) : nbrs;
  flabFoldState.shown = shown.length;
  for (const i of shown) {
    const el = document.createElement("div");
    el.className = "flab";
    el.textContent = nodes[i].label;
    el.title = nodes[i].path;
    armLabelDrag(el);
    el.onclick = () => { pushFocusState(); focusSeeds.clear(); focusSeeds.add(i); showInfo(i); buildContainment(); applyVisibility(); focus(i); };
    flabsEl.appendChild(el);
    fLabs.push({ kind: 0, i, ix: -1, el });
  }
  if (superHub && nbrs.length > shown.length) {
    flabFoldState.folded = nbrs.length - shown.length;
    const el = document.createElement("div");
    el.className = "flab fold";
    el.textContent = "+" + flabFoldState.folded + " more";
    const names = nbrs.slice(SUPER_FLAB_N).map(i => nodes[i].label);
    el.title = flabFoldState.folded + " folded neighbors\n" +
      names.slice(0, 12).join(", ") +
      (names.length > 12 ? " +" + (names.length - 12) + " more" : "");
    el.onclick = () => { flabUnfold = true; rebuildFocusLabels(focusSeeds.size > 0); };
    flabsEl.appendChild(el);
    fLabs.push({ kind: 0, i: fi, ix: -1, el, fold: true });
  }
  // function satellites: focused file's own fns first, then one hop out —
  // capped so the layer stays readable
  if (fnMeta.length) {
    const own = [], near = [];
    fnMeta.forEach((m, ix) => {
      if (level[m.file] === 0) own.push(ix);
      else if (level[m.file] === 1 && alphaTgt[m.file] > 0.5) near.push(ix);
    });
    // cap + prefer state writers: labels are the densest channel, so when
    // the neighborhood is busy the pure functions yield their labels first
    const cands = own.concat(near);
    const writerFirst = (a, b) => {
      const ioa = DATA.fio && DATA.fio[fnKey(fnMeta[a].file, fnMeta[a].name)];
      const iob = DATA.fio && DATA.fio[fnKey(fnMeta[b].file, fnMeta[b].name)];
      return ((iob && iob.w.length) ? 1 : 0) - ((ioa && ioa.w.length) ? 1 : 0);
    };
    cands.sort(writerFirst).slice(0, 32).forEach(ix => {
      const m = fnMeta[ix];
      if (m.agg && !m.count) return;   // box collapsed into a wire aggregate
      const el = document.createElement("div");
      el.className = "flab fn";
      if (m.count) {   // per-file aggregate: 'n×' badge; tooltip lists the names
        const names = fnMeta.filter(x => x.agg && !x.count && x.file === m.file)
                            .map(x => x.name);
        const listed = names.slice(0, 12).join(", ") +
                       (names.length > 12 ? " +" + (names.length - 12) + " more" : "");
        el.textContent = m.count + "×";
        el.title = nodes[m.file].path + " :: " + m.count + " fns" + (listed ? "\n" + listed : "");
        flabsEl.appendChild(el);
        fLabs.push({ kind: 1, i: m.file, ix, el });
        return;
      }
      const io = DATA.fio && DATA.fio[fnKey(m.file, m.name)];
      // writes-state badge (tier-2 metadata per the LOD ladder; single
      // glyph channel — color stays cluster-owned)
      el.textContent = "ƒ " + m.name + (io && io.w.length ? " ✎" + io.w.length : "");
      if (mutOnly && (!io || !io.w.length)) return;   // mutators-only filter
      el.title = nodes[m.file].path + " :: " + m.name;
      if (hlFn.has(m.name)) el.classList.add("hl");
      armLabelDrag(el);
      el.onclick = () => showFnInfo(ix);
      flabsEl.appendChild(el);
      fLabs.push({ kind: 1, i: m.file, ix, el });
    });
  }
}
const _flabV = new THREE.Vector3();
function updateFocusLabels() {
  if (!fLabs.length) return;
  const w = renderer.domElement.clientWidth, h = renderer.domElement.clientHeight;
  const hubRects = hubBoxes;   // placed by updateHubs earlier this tick
  const taken = [];
  // station dots are label obstacles (skeptic R8): project them once per
  // frame — no flab may cover a junction bollard
  const stPts = [];
  for (const S of fnStationsArr) {
    for (const q of [S.p, ...S.subJ]) {
      _flabV.set(q[0], q[1], q[2]).project(camera);
      if (_flabV.z <= 1 && Math.abs(_flabV.x) <= 1.02 && Math.abs(_flabV.y) <= 1.02) {
        toScreen(_flabV, w, h);   // #299 D
        stPts.push([_scr[0], _scr[1]]);
      }
    }
  }
  // 14px margin: at 0.72 kf the marginal label straddled the old 10px
  // line and flickered 0<->1 overlap across settle frames
  const clearDots = r => stPts.every(q =>
    q[0] < r.left - 14 || q[0] > r.right + 14 || q[1] < r.top - 14 || q[1] > r.bottom + 14);
  // delivery arrowheads are obstacles too (a label chip sat ON an arrow
  // in the r3 crop — direction markers must never be covered)
  if (fnArrows) {
    const am = fnArrows.instanceMatrix.array;
    for (let i = 0; i < am.length / 16; i++) {
      _flabV.set(am[i*16+12], am[i*16+13], am[i*16+14]).project(camera);
      if (_flabV.z <= 1 && Math.abs(_flabV.x) <= 1.02 && Math.abs(_flabV.y) <= 1.02)
        stPts.push([(_flabV.x * 0.5 + 0.5) * w, (-_flabV.y * 0.5 + 0.5) * h]);
    }
  }
  const superHub = focusFileIdx >= 0 && degree[focusFileIdx] > SUPER_DEG;   // [#8] halo tier
  for (const f of fLabs) {
    const p = f.kind === 1 ? fnMeta[f.ix].p : null;
    _flabV.set(
      f.kind === 1 ? p[0] : pos[f.i*3],
      f.kind === 1 ? p[1] : pos[f.i*3+1],
      f.kind === 1 ? p[2] : pos[f.i*3+2]).project(camera);
    if (_flabV.z > 1 || Math.abs(_flabV.x) > 1.02 || Math.abs(_flabV.y) > 1.02) {
      f.el.style.display = "none"; continue;
    }
    const x = (_flabV.x * 0.5 + 0.5) * w, y = (-_flabV.y * 0.5 + 0.5) * h;
    const base = "translate(" + x.toFixed(1) + "px," + y.toFixed(1) + "px) translate(-50%,-100%)";
    f.el.style.display = "block";
    f.el.style.transform = base;
    // fn satellites now mutually collide-avoid (ink#4): 18px screen gap to
    // every placed rect — the dense-swarm label shagpile; a blocked label
    // hides (element stays; next frame re-tries as the camera moves)
    if (f.kind === 1) {
      const r1 = labBox(f.el, x, y, 1);
      const clear18 = t => r1.right < t.left - 18 || t.right < r1.left - 18 ||
        r1.bottom < t.top - 18 || t.bottom < r1.top - 18;
      if (!taken.every(clear18) || !clearDots(r1)) { f.el.style.display = "none"; continue; }
      taken.push(r1);
      continue;
    }
    // the fold count chip anchors at the hub itself — it is hub metadata,
    // not part of the neighbor wall, so the halo band does not apply to it;
    // its offset ladder adds sideways rungs so it can clear the hub chip
    const offs = f.fold
      ? [[0, -16], [0, 18], [0, -38], [0, 40], [130, -16], [-130, -16], [0, -62], [0, 64]]
      : [[0, 0], ...[16, -14, 32, -30].map(dy => [0, dy])];
    const res = placeLabels(f.el, x, y, offs,
      "-50%,-100%", 1, r => r.left >= 310 &&   // [#8] never under the left panel (the hub-chip law)
        hubRects.every(hr => separate(r, hr, superHub && hr === focusHubBox && !f.fold ? SUPER_HALO : 2)) &&
        taken.every(t => separate(r, t, 2)) && clearDots(r));
    if (res.hit) taken.push(res.r); else { f.el.style.display = "none"; continue; }
  }
}
// boot rebuildFocusLabels(false) deleted - boot applyVisibility() re-runs it
"""
