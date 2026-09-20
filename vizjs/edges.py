# vizjs/edges.py — rung 3/17 of the template join (issues
# #86 phase-3 / #299 A): edge buckets, highway arcs, wire picker. Moved VERBATIM from viz.py —
# every edit here changes graph.html: regen the bake (the standing
# template law; test_viz refuses a bake older than viz.py/vizjs).
_JS_EDGES = r"""// instanced geometry whose buffers mutate in place (no per-frame rebuild)
const MAXL = links.length;
// full-saturation per-type hue: calls neutral-white, signals amber,
// contains (inst/attach) cyan, anything else green
const TYPE_COLORS = {
  call:   new THREE.Color(0.85, 0.88, 0.92),
  signal: new THREE.Color(1.00, 0.70, 0.22),
  inst:   new THREE.Color(0.24, 0.80, 0.95),
  attach: new THREE.Color(0.24, 0.80, 0.95),
  var:    new THREE.Color(0.45, 0.90, 0.55),
};
// overview opacity rises gently with bucket weight: width stays the main
// weight cue — and heavy edges must never read dimmer than trivial ones
const BUCKETS = [
  { max: 1, width: 2, op: 0.28 },            // w <= 1
  { max: 4, width: 2.2, op: 0.32 },          // 2..4
  { max: Infinity, width: 3.5, op: 0.36 },   // >= 5
];
const bucketOf = new Int8Array(MAXL);
const slotOf = new Int32Array(MAXL);
const hwSlot = new Int32Array(MAXL).fill(-1);
const bucketPosIB = [], bucketColIB = [], bucketMat = [], bucketMesh = [];
{
  const counts = [0, 0, 0];
  // arcs get no straight slot (their slots would stay zero-filled at the
  // galaxy origin — NaN streak quads); they live entirely in the hw span
  const hwSet = new Set(hw.map(e => e[0]));
  links.forEach((l, i) => {
    const b = BUCKETS.findIndex(x => l.w <= x.max);
    bucketOf[i] = b;
    if (!hwSet.has(i)) slotOf[i] = counts[b]++;
  });
  // highways: each bezier arc is 16 static segments (32 vertices, 96 floats)
  // appended after the straight links inside its bucket. hwSlot[i] is the
  // vertex-float base of link i's arc, or -1 for straight links.
  const hwCounts = [0, 0, 0];
  hw.forEach(([li, pts]) => {
    const b = bucketOf[li];
    // hwCounts counts ARCS: the slot math multiplies by 32 vertices/arc.
    // (A previous `+= 32` here double-multiplied, spacing arcs 1024
    // vertices apart and leaving ~500 zero-filled segments at the galaxy
    // origin per arc — NaN quads in LineMaterial's normalize(0) rendered
    // as the close-zoom streak artifact.)
    hwSlot[li] = (counts[b] * 2 + hwCounts[b] * 32) * 3;
    hwCounts[b] += 1;
  });
  BUCKETS.forEach(b => {
    const bi = BUCKETS.indexOf(b);
    // normal blending: additive stacking blew out hub fans into white glare
    // (hundreds of strands converge on 200+-degree hubs); overview opacity
    // capped per bucket so edges stay a quiet layer under the cluster hues.
    // #299 D: mkFatLines owns construction + the resize registry.
    const mesh = mkFatLines(
      new Float32Array((counts[bi] * 2 + hwCounts[bi] * 32) * 3),
      new Float32Array((counts[bi] * 2 + hwCounts[bi] * 32) * 3),
      { linewidth: b.width, opacity: b.op });
    bucketPosIB.push(mesh.geometry.attributes.instanceStart.data);
    bucketColIB.push(mesh.geometry.attributes.instanceColorStart.data);
    bucketMat.push(mesh.material);   // opacity tier laws + __dbg read it
    bucketMesh.push(mesh);   // dash distances are computed on the LineSegments2
  });
}
// per-link ink state, written by the k-pass in applyVisibility and read by
// the picker: pick exactly what renders. <0.02 = no pickable ink (0 = no ink
// at all — filtered/budget-under-arc/fn-wire-replaced; 0.012 dead-end dim
// reads as nothing; GHOST_K 0.08 ghosts stay pickable)
const edgeK = new Float32Array(MAXL);
let pickWireZ = 1;   // NDC depth of the last hit — node-front comparisons
// ONE picker for hover AND click: the meta of the wire under the pointer
// across every layer that actually RENDERS INK —
//   fnLines/fnQuiet arcs (fn wire mode), focusArcs (hub budget arcs) and
//   straight bucket chords (overview + signal/inst strands).
// Chords that render NO ink are skipped: fn-mode call chords between lit
// files (blacked — fn wires replace them; the old picker tested those and
// read as a "pre-curved collider"), budget chords under their arcs, and
// dead-end dim (0.012) ink that reads as nothing. Distances score to the
// INK EDGE (4px trunk beats 2px wire at ties; the quiet tier needs margin).
// [issue #82 / #196] 3D wire pin. The emphasis IS the corridor now:
// the pin tints the wire's SERVING INSTANCES (bus trunk + junction legs
// + the wire's own arcs) instead of drawing a constructed polyline over
// them - the highlight rides the real geometry, so curvature and LOD
// holes can never detach it (owner: the old overlay was "weirdly all
// over the place"). Endpoint dots stay (screen-constant, fn-box law).
// [#196] ONE latch paints on BOTH surfaces: a map pin resolves its 3D
// emphasis here, a ball pin's map twin paints in drawMapPane. The pin's
// home surface owns pinCover (#83 contract); the other surface's
// emphasis reads pinCoverX. Every dismissal path clears both because
// both paints key on the single pin object.
let pinPts = null, pinPtsPos = null;
let pinMisses = 0;   // [issue #84/#196] frames NO surface resolved the pin
let pinCoverX = 0;   // [#196] cross-surface cover (see above)
let pinTint2 = [];   // [#196] wire-arc tints: {mesh, segs[], orig[[6rgb]]}
const PIN_TINT = [0.114, 0.914, 0.714];   // #1de9b6 == PIN_ACCENT
const PIN_TINT_LERP = 0.55;
// [skeptic #16] roster generation: bumped on every fn-layer rebuild.
// Ball pins index fnMeta/links/arc buckets - after a refocus those
// arrays are fresh and the stale indices either throw (#17) or
// silently "find" ink that is not the pinned thing (a link pin kept
// its arc alive on the rebuilt roster). A pin dies with its roster.
let rosterGen = 0;   // [issue #85 owner r4] fn-layer generation counter
let pinTinted = [];      // [issue #85 owner r4] tinted busPts indices
let pinTintOrig = [];    // parallel [r,g,b] originals for restore
let pinTintBus = null;   // fnBus the tint was written into (swap guard)
let pinTintKey = null;   // wirePin.id the tint belongs to
let pinEp0 = null, pinEp1 = null, pinBoxA = null, pinBoxB = null;
let pinChA = -1, pinChB = -1;   // chain file pair (probe hook)
// [#196] resolve a map pin's (file, fn) strings against the LIVE
// roster - never cache indices across rebuilds (skeptic #17: stale
// fnMeta reads threw out of tick and stalled the reap). Aggregate
// boxes carry no single fn, so they never match - the pair corridor
// fallback carries those pins.
function pinFnOf(file, name) {
  if (file == null || file < 0 || !fnMeta) return -1;
  for (let i = 0; i < fnMeta.length; i++) {
    const m = fnMeta[i];
    if (m.file === file && m.name === name && !m.agg) return i;
  }
  return -1;
}
// [#196] pair-level trunk key: the first corridor in emission order
// serving the pair either orientation (deterministic)
function pinPairTrunkKey(A, B) {
  if (!busPtsMeta || A == null || A < 0 || B == null || B < 0) return null;
  for (let i = 0; i < busPtsMeta.length; i++) {
    const m = busPtsMeta[i];
    if (!m || m.kind !== "trunk") continue;
    const pp = String(m.k).split(">");
    if ((+pp[0] === A && +pp[1] === B) || (+pp[0] === B && +pp[1] === A))
      return String(m.k);
  }
  return null;
}
function updateBallPin() {
  // [skeptic #16] identity death, independent of ink/tip work: a ball
  // pin from a previous roster generation is meaningless (stale
  // fnMeta/links indices) - clear it outright instead of waiting for
  // the ink-based misses counter
  if (wirePin && wirePin.surface === "ball" && wirePin.gen !== rosterGen) {
    wirePinClear();
    return;
  }
  if (!pinPts) {
    pinPtsPos = new Float32Array(2 * 3);
    pinPts = new THREE.Points(
      new THREE.BufferGeometry().setAttribute("position",
        new THREE.BufferAttribute(pinPtsPos, 3)),
      new THREE.PointsMaterial({ color: 0xffffff, size: 10,   // [issue #84] endpoint sprite boost
        sizeAttenuation: false, transparent: true, opacity: 1,
        depthTest: false }));
    pinPts.renderOrder = 1000; pinPts.frustumCulled = false;
    pinPts.visible = false;   // dots exist only while a pin resolves them
    scene.add(pinPts);
  }
  // [#196] a rebuilt fn layer swaps the tint targets out from under
  // the records: drop them (their buffers are gone - a fresh mesh
  // carries its own stock colors) and force a re-apply so the live
  // pin re-tints the fresh instances.
  let dropped = false;
  if (pinTintBus && fnBus && pinTintBus !== fnBus) {
    pinTinted = []; pinTintOrig = []; pinTintBus = null; dropped = true;
  }
  if (pinTint2.length) {
    const keep = pinTint2.filter(r =>
      r.mesh === fnLines || r.mesh === fnQuiet ||
      (focusArcs && r.mesh === focusArcs.lines));
    if (keep.length !== pinTint2.length) dropped = true;
    pinTint2 = keep;
  }
  if (dropped) pinTintKey = null;
  // [skeptic #19] restore on ANY tint-key mismatch: a replaced pin
  // (trunk -> wire/link) never dies, so the only path that can put the
  // corridor back is this head guard
  if (!wirePin || pinTintKey !== wirePin.id) pinTintRestore();
  // [#196] collect + tint ONCE per pin identity (and once per fn-layer
  // rebuild): per-frame cost after that is the misses check below.
  if (wirePin && pinTintKey !== wirePin.id) {
    let A = -1, B = -1, tk = null, wa = -1, wb = -1;
    const idp = String(wirePin.id || "").split("|");
    if (wirePin.kind === "trunk") {
      if (wirePin.k != null) tk = String(wirePin.k);
      else if (wirePin.s != null) { A = +wirePin.s; B = +wirePin.t; }
    } else if (wirePin.kind === "link" && links &&
               wirePin.li >= 0 && wirePin.li < links.length) {
      A = links[wirePin.li].s; B = links[wirePin.li].t;
    } else if (wirePin.kind === "wire") {
      if (wirePin.a != null && wirePin.a >= 0 && fnMeta &&
          wirePin.a < fnMeta.length && wirePin.b >= 0 &&
          wirePin.b < fnMeta.length) {
        wa = wirePin.a; wb = wirePin.b;
        A = fnMeta[wa].file; B = fnMeta[wb].file;
      } else if (idp[0] === "w") {
        // map wire row "w|sf|sfn|df|dfn|ty": both fns re-resolve from
        // the id's immutable strings; unresolvable fns (var targets,
        // aggregates) ride the pair corridor instead
        wa = pinFnOf(+idp[1], idp[2]);
        wb = pinFnOf(+idp[3], idp[4]);
        A = +idp[1]; B = +idp[3];
      } else if (idp[0] === "S") { A = +idp[1]; B = +idp[2]; }
    }
    if (!tk && A >= 0 && B >= 0) tk = pinPairTrunkKey(A, B);
    let chainCover = 0, refA = null, refB = null;
    const chainTintIdx = [];
    if (tk) {
      const tp = tk.split(">"); A = +tp[0]; B = +tp[1];
      const prefixes = [];
      for (const S of (fnStationsArr || []))
        if (S.tks.indexOf(tk) >= 0)
          prefixes.push("L|" + S.fi + "|" + S.id + "|");
      if (fnBus && fnBus.visible && busPts && busPtsMeta &&
          fnBus.instanceMatrix) {
        const mx = fnBus.instanceMatrix.array;
        for (let i = 0; i < busPts.length; i++) {
          const m2 = busPtsMeta[i];
          if (!m2) continue;
          const k2 = String(m2.k || "");
          let hit = false;
          if (m2.kind === "trunk" && k2 === tk) hit = true;
          else if (prefixes.length && k2.charCodeAt(0) === 76)   // leg key
            for (const pref of prefixes)
              if (k2.startsWith(pref)) { hit = true; break; }
          if (!hit) continue;
          // pick/render parity: a culled instance parked at scale ~0 stays out
          if (!isServed(mx, i)) continue;
          chainTintIdx.push(i);
          const s2 = busPts[i];
          if (!refA) refA = s2.a;
          refB = s2.b;   // extremes in emission order (deterministic)
          chainCover++;
        }
      }
    }
    pinChA = A; pinChB = B;
    // [#196] the wire's own arcs: every call-site arc of the fn pair
    // across the wire meshes - the wire itself reads selected, not just
    // its corridor. Line2 vertex colors live in the per-instance
    // instanceColorStart/End attributes (setColors buffers).
    let arcN = 0;
    if (wirePin.kind === "wire" && wa >= 0 && wb >= 0) {
      for (const mesh of [fnLines, fnQuiet,
                          focusArcs && focusArcs.lines]) {
        if (!mesh || !mesh.visible) continue;
        const g2 = mesh.geometry;
        if (!g2 || !g2.attributes.instanceColorStart ||
            !g2.attributes.instanceColorEnd ||
            !g2.attributes.instanceStart) continue;
        const meta2 = mesh.userData.meta || [];
        const per2 = mesh.userData.seg || 8;
        const nseg2 = g2.attributes.instanceStart.count;
        for (let g = 0; g < meta2.length; g++) {
          const m2 = meta2[g];
          if (!m2 || m2.kind !== "wire" || m2.a !== wa || m2.b !== wb)
            continue;
          const first = g * per2;
          const last = Math.min(first + per2, nseg2);
          if (first >= last) continue;
          const cs = g2.attributes.instanceColorStart.array;
          const ce = g2.attributes.instanceColorEnd.array;
          const rec = { mesh: mesh, segs: [], orig: [] };
          for (let i2 = first; i2 < last; i2++) {
            rec.segs.push(i2);
            rec.orig.push([cs[i2*3], cs[i2*3+1], cs[i2*3+2],
                           ce[i2*3], ce[i2*3+1], ce[i2*3+2]]);
            cs[i2*3]   += (PIN_TINT[0] - cs[i2*3])   * PIN_TINT_LERP;
            cs[i2*3+1] += (PIN_TINT[1] - cs[i2*3+1]) * PIN_TINT_LERP;
            cs[i2*3+2] += (PIN_TINT[2] - cs[i2*3+2]) * PIN_TINT_LERP;
            ce[i2*3]   += (PIN_TINT[0] - ce[i2*3])   * PIN_TINT_LERP;
            ce[i2*3+1] += (PIN_TINT[1] - ce[i2*3+1]) * PIN_TINT_LERP;
            ce[i2*3+2] += (PIN_TINT[2] - ce[i2*3+2]) * PIN_TINT_LERP;
          }
          g2.attributes.instanceColorStart.needsUpdate = true;
          g2.attributes.instanceColorEnd.needsUpdate = true;
          pinTint2.push(rec);
          arcN++;
        }
      }
    }
    // corridor chain tint (the d6295ac pattern): originals captured
    // from the buffer before the write; the LOD serve pass only
    // rewrites matrices, so this paint-tier tint never fights culling
    // or picking (pickWireMeta is geometry-only, color-blind)
    if (chainTintIdx.length && fnBus && fnBus.instanceColor) {
      pinTinted = chainTintIdx.slice();
      pinTintBus = fnBus;
      const ca = fnBus.instanceColor.array;
      for (const q of pinTinted)
        pinTintOrig.push([ca[q*3], ca[q*3+1], ca[q*3+2]]);
      for (let q2 = 0; q2 < pinTinted.length; q2++) {
        const ix = pinTinted[q2], oc = pinTintOrig[q2];
        ca[ix*3]   = oc[0] + (PIN_TINT[0] - oc[0]) * PIN_TINT_LERP;
        ca[ix*3+1] = oc[1] + (PIN_TINT[1] - oc[1]) * PIN_TINT_LERP;
        ca[ix*3+2] = oc[2] + (PIN_TINT[2] - oc[2]) * PIN_TINT_LERP;
      }
      fnBus.instanceColor.needsUpdate = true;
    }
    pinTintKey = wirePin.id;
    // [issue #84] endpoint law: both termini sit ON the fn boxes the
    // legs serve (owner: 'from the actual start function to the actual
    // end function'). A fn-wire pin knows its exact fns; a trunk/link
    // pin takes each file's box nearest to that side's chain extreme
    // (deterministic: nearest, ties by fnMeta index).
    const boxOf = (file, refPt) => {
      if (file < 0 || !fnMeta || !fnMeta.length) return null;
      let best = null, bd2 = Infinity;
      for (let i2 = 0; i2 < fnMeta.length; i2++) {
        const m2 = fnMeta[i2];
        if (m2.file !== file || !m2.p) continue;
        if (m2.agg && !m2.count) continue;   // scale-0 stub, invisible
        if (!m2.p[0] && !m2.p[1] && !m2.p[2]) continue;   // unfilled
        const d2 = (m2.p[0] - refPt[0]) ** 2 +
                   (m2.p[1] - refPt[1]) ** 2 +
                   (m2.p[2] - refPt[2]) ** 2;
        if (d2 < bd2 - 1e-9) { bd2 = d2; best = m2.p; }
      }
      return best;
    };
    pinBoxA = pinBoxB = null;
    if (wirePin.kind === "wire" && wa >= 0 && wb >= 0 && fnMeta &&
        wa < fnMeta.length && wb < fnMeta.length) {
      const pa = fnMeta[wa].p, pb = fnMeta[wb].p;
      if (pa && (pa[0] || pa[1] || pa[2])) pinBoxA = pa;
      if (pb && (pb[0] || pb[1] || pb[2])) pinBoxB = pb;
    } else if (A >= 0 && B >= 0 && refA && refB) {
      pinBoxA = boxOf(A, refA);
      pinBoxB = boxOf(B, refB);
    }
    pinEp0 = pinBoxA; pinEp1 = pinBoxB;
    const covB = chainCover + arcN;
    if (wirePin.surface === "ball") pinCover = covB;
    else pinCoverX = covB;
    pinPts.visible = !!(pinEp0 || pinEp1);
    if (pinEp0) { pinPtsPos[0] = pinEp0[0]; pinPtsPos[1] = pinEp0[1];
                  pinPtsPos[2] = pinEp0[2]; }
    if (pinEp1) { pinPtsPos[3] = pinEp1[0]; pinPtsPos[4] = pinEp1[1];
                  pinPtsPos[5] = pinEp1[2]; }
    if (pinEp0 || pinEp1)
      pinPts.geometry.attributes.position.needsUpdate = true;
  }
  // [#196] reap: a pin lives while ANY surface resolves it. 3D-side
  // death alone no longer kills a map-carried pin (and vice versa);
  // nothing resolving anywhere for ~1s means the entities are gone.
  if (wirePin) {
    const ballCov = wirePin.surface === "ball" ? pinCover : pinCoverX;
    const mapCov = wirePin.surface === "map" ? pinCover : pinCoverX;
    if (ballCov > 0 || (mapVisible && mapCov > 0)) pinMisses = 0;
    else if (++pinMisses > 60) { pinMisses = 0; wirePinClear(); return; }
  }
  // [issue #85 owner r2] persistent from->to: while a ball pin lives
  // the tip surface carries the pin description. A fresh hover owns
  // the surface until the press hides it; the pin re-asserts next
  // frame (wireTipAnchor doubles as the pin - the lifetime tracker
  // reads the same .k / .a / .b fields pins carry).
  if (wirePin && wirePin.surface === "ball" && !wireTipAnchor) {
    let pd = null;
    try { pd = pinDesc(wirePin); } catch (e) { pd = null; }
    if (pd) {
      if (wireTipEl.textContent !== pd) wireTipEl.textContent = pd;
      wireTipEl.style.display = "block";
      wireTipAnchor = wirePin;
    }
  }
}
function pickWireMeta(e) {
  const rect = renderer.domElement.getBoundingClientRect();
  const px = e.clientX - rect.left, py = e.clientY - rect.top;
  const v = new THREE.Vector3(), w = new THREE.Vector3();
  let best = null, bestD = 8;
  for (const mesh of [fnLines, fnQuiet, focusArcs && focusArcs.lines]) {
    if (!mesh || !mesh.visible) continue;
    const a = mesh.geometry.attributes.instanceStart.array;
    const meta = mesh.userData.meta || [];
    const per = mesh.userData.seg || 8;
    const n = Math.min(a.length / 6, meta.length * per);
    const quiet = mesh === fnQuiet;
    for (let i = 0; i < n; i++) {
      const o = i * 6;
      v.set(a[o], a[o+1], a[o+2]).project(camera);
      if (v.z > 1) continue;
      toScreen(v, rect.width, rect.height);   // #299 D
      const vSx = _scr[0], vSy = _scr[1];
      w.set(a[o+3], a[o+4], a[o+5]).project(camera);
      if (w.z > 1) continue;
      toScreen(w, rect.width, rect.height);
      const d = segDist(px, py, vSx, vSy, _scr[0], _scr[1]);
      const m = meta[Math.floor(i / per)];
      // pick parity (sighting #11): a trunk meta on the wire tier must not
      // answer the picker when its conduit is LOD-culled — the card would
      // describe ink that isn't served (explanation without presence)
      if (m && m.kind === "trunk" && fnBus && busPts && !inkKeys.has(String(m.k))) continue;
      const dd = d - (m.kind === "trunk" ? 2 : 1) + (quiet ? 3 : 0);
      if (dd < bestD) { bestD = dd; best = m; pickWireZ = v.z + segT * (w.z - v.z); }
    }
  }
  for (let i = 0; i < links.length; i++) {
    // ink truth from the k-pass — pick exactly what renders (see edgeK decl)
    if (edgeK[i] < 0.02) continue;
    const arr = bucketPosIB[bucketOf[i]].array;
    const fo = hwSlot[i] >= 0 ? hwSlot[i] : slotOf[i] * 6;   // float offset
    const nseg = hwSlot[i] >= 0 ? 16 : 1;
    for (let s = 0; s < nseg; s++) {
      const o = fo + s * 6;
      v.set(arr[o], arr[o+1], arr[o+2]).project(camera);
      if (v.z > 1) break;
      toScreen(v, rect.width, rect.height);   // #299 D
      const vSx = _scr[0], vSy = _scr[1];
      w.set(arr[o+3], arr[o+4], arr[o+5]).project(camera);
      if (w.z > 1) break;
      toScreen(w, rect.width, rect.height);
      const d = segDist(px, py, vSx, vSy, _scr[0], _scr[1]);
      const dd = d - 1;
      if (dd < bestD) { bestD = dd; best = { kind: "link", li: i }; pickWireZ = v.z + segT * (w.z - v.z); }
    }
  }
  // click-parity pass (user r5): the WHITE trunk conduits + ivory junction
  // dots pick exactly like the colored bus wires — same metas, same tip.
  // Conduits get a wider forgiveness (tubes are 2-3x wire width); dots match
  // by projected center. Only while the tier actually renders (serve gate).
  if (fnBus && fnBus.visible && _lodServe) {
    if (busPts && busPtsMeta) {
      for (let i = 0; i < busPts.length; i++) {
        const m = busPtsMeta[i];
        if (!m) continue;
        const s = busPts[i];
        v.set(s.a[0], s.a[1], s.a[2]).project(camera);
        if (v.z > 1) continue;
        toScreen(v, rect.width, rect.height);   // #299 D
        const vSx = _scr[0], vSy = _scr[1];
        w.set(s.b[0], s.b[1], s.b[2]).project(camera);
        if (w.z > 1) continue;
        toScreen(w, rect.width, rect.height);
        const d = segDist(px, py, vSx, vSy, _scr[0], _scr[1]);
        const dd = d - 3;   // thick target: generous forgiveness
        // pick-vs-render parity (skeptic A8b): a culled chain (instance
        // scale parked at 0.0001) must not answer the picker — hovering
        // invisible ink is the tooltip-over-nothing class. Tapered EXPLAINED
        // EXIT stubs keep scale > 0 and stay pickable.
        if (!isServed(fnBus.instanceMatrix.array, i)) continue;
        if (dd < bestD) { bestD = dd; best = m; pickWireZ = v.z + segT * (w.z - v.z); }
      }
    }
    if (juncPickInfo) {
      for (let i = 0; i < juncPickInfo.length; i++) {
        const m = juncPickInfo[i];
        const p = jdot ? jdot.pos : null;
        if (!m || !p) continue;
        v.set(p[i*3], p[i*3+1], p[i*3+2]).project(camera);
        if (v.z > 1) continue;
        // pick-vs-render parity (sighting #11 class 2): a culled bollard
        // (radius parked at 0.0001) must not answer the picker — a card on
        // invisible ink is explanation without presence
        if ((jdot.r[i] || 0) <= 0.001) continue;
        m.__ji = i;   // presence check keys the card to this dot's live radius
        toScreen(v, rect.width, rect.height);   // #299 D
        const dx = _scr[0] - px, dy = _scr[1] - py;
        const dd = Math.hypot(dx, dy) - 8;   // dot radius + forgiveness
        if (dd < bestD) { bestD = dd; best = m; pickWireZ = v.z; }
      }
    }
  }
  return best;
}
// focus-mode dash-flow (direction cue): world-units/s of dashOffset travel,
// re-read each tick
const FLOW_SPEED = 2.5;
let edgeFlowOn = false;   // set by applyVisibility, read by tick's dash pass

// typed strands between the same file pair run parallel instead of
// overlapping: each gets a slot offset perpendicular to the strand
// filter state shared by applyVisibility (colors) and syncEdgePos
// (geometry) — declared here because syncEdgePos runs at module init
let showTests = false;
const activeDirs = new Set();   // multi-select dir filter
const isTestNode = n => n.path.startsWith("tests/") || n.path.startsWith("tools/") ||
  n.path.slice(n.path.lastIndexOf("/") + 1).startsWith("test_");
// single predicate for "is this node filtered out right now" - used by
// nodeVisible (node alpha) and linkFiltered (edge collapse). applyVisibility's
// per-endpoint copy of this logic once drifted and painted black full-length
// wires over content.
const nodeFiltered = n => (!showTests && isTestNode(n)) ||
  (activeDirs.size && !activeDirs.has(n.dir));
const linkFiltered = l => nodeFiltered(nodes[l.s]) || nodeFiltered(nodes[l.t]);
const pairKey = (s, t) => s < t ? s + "_" + t : t + "_" + s;
const pairLinks = new Map();
links.forEach((l, i) => {
  const k = pairKey(l.s, l.t);
  if (!pairLinks.has(k)) pairLinks.set(k, []);
  pairLinks.get(k).push(i);
});
// baked highway arc points by link index — syncEdgePos is the SINGLE
// geometry owner for arcs (collapse + restore + spread attachment)
const hwPts = new Map(hw);
// hub-budget state lives here (above syncEdgePos, whose boot call reads
// budgetLit for the pin-topology attachment — a later let would be TDZ)
let budgetLit = null;   // link indices allowed to render lit this focus
let hoverEdgeLi = -1;   // wire-hover budget bypass (hovered ghost wire)
let hoverVisNode = -1;  // node-hover budget bypass (reveals the node's fan)
// focus-neighborhood compaction state (lit set = focus + 1-hop)
let posSaved = null;      // frozen-layout snapshot taken at focus entry
let compactTgt = null;    // compacted targets (Float32Array 3N) or null
let compactIdx = [];      // lit-set node indices the ease animates
let compactAnim = null;   // { t0, dur } while the ease runs, else null
let compactScale = 1;     // exact-scale finisher factor (via __dbg)
let compactBallR = 0;     // focus ball radius — LOD gate for the fn-wire layer
let focusFileIdx = -1;   // the level-0 seed of the active focus (compaction)
let compactOverlaps = 0;  // residual violations after the pass (must be 0)
// attachment trim at node i's CURRENT body: its own sphere (world radius
// sizes*1.1*sqrt(spread) + 2 margin), or the supernode (4 + sqrt(members)
// + 2) when i's cluster is collapsed
const trimAt = i => supMem[i]
  ? 4 + Math.sqrt(supCollapsed.get(nodes[i].cluster).n) + 2
  : sphR(i) + 2;
function syncEdgePos() {
  links.forEach((l, i) => {
    if (hwSlot[i] >= 0) {
      // ATTACHMENT: highway arcs are geometry like any other edge — rescale
      // the baked arc shape affinely around the layout centroid so endpoints
      // track their (moved) nodes exactly. Arc data shape: 17 [x,y,z] points
      // (nested arrays — flat indexing here once produced NaN, killing every
      // line in the affected buckets). Filtered/ghost arcs collapse exactly
      // like straight edges — black color alone is NOT hidden under normal
      // blending.
      const arr = bucketPosIB[bucketOf[i]].array, b = hwSlot[i];
      const pts = hwPts.get(i);
      if (!pts || !pts.length) return;
      const ghost = alphaTgt[l.s] < 0.05 && alphaTgt[l.t] < 0.05;
      const hidden = linkFiltered(l) || ghost;
      for (let v = 0; v < 16; v++) {
        const o = b + v * 6;
        if (hidden) {
          // #299 D: stub-law sibling with a deliberate shape difference —
          // highway arcs stub BOTH verts at the +0.05-lifted point (a whole
          // arc collapses onto one spot), unlike stub()'s second-vert-only
          // lift used by the straight/semAff guards below.
          const sx = dpos[l.s*3], sy = dpos[l.s*3+1] + 0.05, sz = dpos[l.s*3+2];
          arr[o] = sx; arr[o+1] = sy; arr[o+2] = sz;
          arr[o+3] = sx; arr[o+4] = sy; arr[o+5] = sz;
          continue;
        }
        const A = pts[v], B = pts[v + 1];
        if (!A || !B) return;
        // per-endpoint re-rooted affine: the baked arc is mapped into live
        // space anchored at each endpoint's CURRENT attachment point
        // (dpos — supernode centroid when collapsed, the node itself
        // otherwise, which reproduces the plain centroid affine exactly),
        // then blended along the arc: v=0 maps purely through the s-anchor,
        // v=16 purely through the t-anchor. Retargets arcs onto supernodes
        // without disturbing uncollapsed ones.
        const p0 = pts[0], p16 = pts[16];
        const sax = dpos[l.s*3] + (A[0] - p0[0]) * spread,
              say = dpos[l.s*3+1] + (A[1] - p0[1]) * spread,
              saz = dpos[l.s*3+2] + (A[2] - p0[2]) * spread;
        const tax = dpos[l.t*3] + (A[0] - p16[0]) * spread,
              tay = dpos[l.t*3+1] + (A[1] - p16[1]) * spread,
              taz = dpos[l.t*3+2] + (A[2] - p16[2]) * spread;
        const sbx = dpos[l.s*3] + (B[0] - p0[0]) * spread,
              sby = dpos[l.s*3+1] + (B[1] - p0[1]) * spread,
              sbz = dpos[l.s*3+2] + (B[2] - p0[2]) * spread;
        const tbx = dpos[l.t*3] + (B[0] - p16[0]) * spread,
              tby = dpos[l.t*3+1] + (B[1] - p16[1]) * spread,
              tbz = dpos[l.t*3+2] + (B[2] - p16[2]) * spread;
        const u1 = v / 16, u2 = (v + 1) / 16;
        let ax = sax + (tax - sax) * u1, ay = say + (tay - say) * u1, az = saz + (taz - saz) * u1;
        let bx = sbx + (tbx - sbx) * u2, by = sby + (tby - sby) * u2, bz = sbz + (tbz - sbz) * u2;
        // surface trim at the two node-attached ends (baked arc endpoints
        // sit on the node centers): pull the terminal vertex along its own
        // segment by the node's world radius + margin, same rule as
        // straight edges. Interior segments untouched; a degenerate
        // segment (tiny spread) keeps its points rather than feed
        // normalize(0).
        if (v === 0 || v === 15) {
          const vx = bx - ax, vy = by - ay, vz = bz - az;
          const vl = Math.sqrt(vx*vx + vy*vy + vz*vz);
          const tr = trimAt(v === 0 ? l.s : l.t);
          if (vl > tr + 0.05) {
            const k = tr / vl;
            if (v === 0) { ax += vx * k; ay += vy * k; az += vz * k; }
            else { bx -= vx * k; by -= vy * k; bz -= vz * k; }
          }
        }
        arr[o]   = ax; arr[o+1] = ay; arr[o+2] = az;
        arr[o+3] = bx; arr[o+4] = by; arr[o+5] = bz;
      }
      bucketPosIB[bucketOf[i]].needsUpdate = true;
      return;
    }
    const s = l.s * 3, t = l.t * 3;
    // single owner of edge geometry: applyVisibility re-runs this after
    // every filter change, so filtered links collapse here and unfiltered
    // links always restore full positions.
    // ghost = both endpoints outside the current focus (alpha ~ 0): a line
    // between two invisible nodes is pure noise — collapse it too.
    const ghost = alphaTgt[l.s] < 0.05 && alphaTgt[l.t] < 0.05;
    if (linkFiltered(l) || ghost) {
      const a0 = bucketPosIB[bucketOf[i]].array, o0 = slotOf[i] * 6;
      a0[o0] = dpos[s]; a0[o0+1] = dpos[s+1]; a0[o0+2] = dpos[s+2];
      // tiny offset: an exactly-zero-length segment gives LineMaterial's
      // normalize(0) NaN screen quads (driver-dependent streaks)
      a0[o0+3] = dpos[s]; a0[o0+4] = dpos[s+1] + 0.05; a0[o0+5] = dpos[s+2];
      bucketPosIB[bucketOf[i]].needsUpdate = true;
      return;
    }
    let ox = 0, oy = 0;
    const g = pairLinks.get(pairKey(l.s, l.t));
    if (g.length > 1) {
      const slot = g.indexOf(i) - (g.length - 1) / 2;
      // perpendicular to the strand in the xy-plane; +X fallback when the
      // strand is nearly parallel to Z (cross with Z degenerates)
      const dx = dpos[t] - dpos[s], dy = dpos[t+1] - dpos[s+1];
      let px = dy, py = -dx;
      const pl = Math.sqrt(px*px + py*py);
      if (pl < 0.001) { px = 1; py = 0; } else { px /= pl; py /= pl; }
      ox = px * slot * 9; oy = py * slot * 9;
    }
    const a = bucketPosIB[bucketOf[i]].array, o = slotOf[i] * 6;
    // surface trim: pull each endpoint out of its attachment body (own
    // sphere or supernode — trimAt) so strands meet the surface, not the
    // center. A strand shorter than both trims would invert and feed
    // normalize(0) — fall back to the same stub the ghost path uses.
    const ex = dpos[t] - dpos[s], ey = dpos[t+1] - dpos[s+1], ez = dpos[t+2] - dpos[s+2];
    const el = Math.sqrt(ex*ex + ey*ey + ez*ez);
    const trimS = trimAt(l.s), trimT = trimAt(l.t);
    if (el - trimS - trimT <= 0.05) {
      a[o] = dpos[s]; a[o+1] = dpos[s+1]; a[o+2] = dpos[s+2];
      a[o+3] = dpos[s]; a[o+4] = dpos[s+1] + 0.05; a[o+5] = dpos[s+2];
      return;
    }
    const ndx = ex / el, ndy = ey / el, ndz = ez / el;
    // pin topology (focus): budgeted lit wires attach at distinct points
    // around the sphere rim — rotate the attachment bearing by a small
    // deterministic per-link angle (Rodrigues around an axis perpendicular
    // to the strand) so wires leaving a hub land at visibly separate pins
    // instead of stacking on one bearing line. Rotation slides the trim
    // point ALONG the surface, so the endpoint always sits on-rim.
    // Overview and ghost edges keep the exact center-to-center bearing.
    // budgetLit !== null only while a focus is active.
    let rx = ndx, ry = ndy, rz = ndz;
    if (budgetLit && budgetLit.has(i)) {
      const spin = ((i % 9) - 4) * 0.055;   // ±0.22 rad deterministic fan
      if (spin !== 0) {
        let ax = ndy, ay = -ndx, az = 0;    // n × Z
        if (ndx * ndx + ndy * ndy < 1e-6) { ax = 0; ay = ndz; az = -ndy; }  // n × X
        const al = Math.sqrt(ax * ax + ay * ay + az * az) || 1;
        ax /= al; ay /= al; az /= al;
        const cs = Math.cos(spin), sn = Math.sin(spin);
        const wx = ay * ndz - az * ndy, wy = az * ndx - ax * ndz, wz = ax * ndy - ay * ndx;
        rx = ndx * cs + wx * sn; ry = ndy * cs + wy * sn; rz = ndz * cs + wz * sn;
      }
    }
    a[o]   = dpos[s] + rx * trimS + ox; a[o+1] = dpos[s+1] + ry * trimS + oy; a[o+2] = dpos[s+2] + rz * trimS;
    a[o+3] = dpos[t] - rx * trimT + ox; a[o+4] = dpos[t+1] - ry * trimT + oy; a[o+5] = dpos[t+2] - rz * trimT;
  });
   bucketPosIB.forEach(ib => { ib.needsUpdate = true; });
   // dash support: lineDistance attributes must track every geometry
   // rewrite, but only USE_DASH reads them - and every edgeFlowOn false->true
   // transition flows through the applyVisibility() call that just ran this
  // syncEdgePos, so distances are fresh exactly when dashes can appear.
  if (edgeFlowOn) bucketMesh.forEach(ms => ms.computeLineDistances());
}
syncEdgePos();
// boot arc pre-fill deleted: syncEdgePos above already wrote the arcs
// (with surface trim), and boot applyVisibility re-runs it before the
// first render - the old raw re-write was dead weight + a stale comment.
// base colors: pure type hue scaled by weight-as-brightness; the TARGET-end
// vertex is tinted 55% toward the target node's cluster color so edges
// show direction (start vertex keeps the pure type hue)
const eColBase = new Float32Array(MAXL * 6);
links.forEach((l, i) => {
  const tc = TYPE_COLORS[l.ty] || TYPE_COLORS.var;
  const wb = 0.45 + Math.min(1, l.w / 6) * 0.55;
  const o = i * 6, t3 = l.t * 3;
  eColBase[o]   = tc.r * wb; eColBase[o+1] = tc.g * wb; eColBase[o+2] = tc.b * wb;
  eColBase[o+3] = tc.r * wb + (colArr[t3]   - tc.r * wb) * 0.55;
  eColBase[o+4] = tc.g * wb + (colArr[t3+1] - tc.g * wb) * 0.55;
  eColBase[o+5] = tc.b * wb + (colArr[t3+2] - tc.b * wb) * 0.55;
});
// boot color pre-fill deleted: nothing renders before boot applyVisibility()
// rewrites every link's color (bucket buffers start zeroed; first tick is
// after that pass).
bucketColIB.forEach(ib => { ib.needsUpdate = true; });

// ---- camera tween + focus back-stack -----------------------------------------
// 400ms ease-out camera transitions replace teleporting focus() jumps
let camTween = null;
function tweenCamTo(toTarget, toCam) {
  camTween = { t0: performance.now(), dur: 400,
    fromT: controls.target.clone(), toT: toTarget.clone(),
    fromC: camera.position.clone(), toC: toCam.clone() };
}
// focus back-stack: interactions that re-root the focus push the previous
// seeds + camera pose; Backspace / the ‹ chip pops back to them
let focusStack = [];
function pushFocusState() {
  if (!focusSeeds.size) return;
  focusStack.push({ seeds: [...focusSeeds],
    target: controls.target.clone(), cam: camera.position.clone() });
  if (focusStack.length > 20) focusStack.shift();
}
function popFocus() {
  const f = focusStack.pop();
  if (!f) return;
  focusSeeds.clear();
  f.seeds.forEach(s => focusSeeds.add(s));
  tweenCamTo(f.target, f.cam);
  applyVisibility();
}

// round-5 LOD gate (user-acceptance): a bus element is visible only when
// the boxes it serves RESOLVE on screen — at far zoom whole trunks read
// as "nowhere to nowhere" and sub-junction dots as droplets on wires.
"""
