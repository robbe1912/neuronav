# vizjs/focus_vis.py — rung 6/17 of the template join (issues
# #86 phase-3 / #299 A): focus entry, budget wires, compaction. Moved VERBATIM from viz.py —
# every edit here changes graph.html: regen the bake (the standing
# template law; test_viz refuses a bake older than viz.py/vizjs).
_JS_FOCUS_VIS = r"""
// BFS from clicked seeds up to `depth` (issue #33: focus starts ONLY from
// a node click — search typing highlights in place, it never seeds); the
// direction mode picks which adjacency half the walk follows
function computeLevels() {
  level.fill(-1);
  const seeds = [];
  for (const s of focusSeeds) if (level[s] < 0) { level[s] = 0; seeds.push(s); }
  for (let qi = 0; qi < seeds.length; qi++) {
    const u = seeds[qi];
    if (level[u] >= depth) continue;
    const nbrs = dirMode === 1 ? adjOut[u] : dirMode === 2 ? adjIn[u] : adj[u];
    for (const v of nbrs) if (level[v] < 0) { level[v] = level[u] + 1; seeds.push(v); }
  }
  return seeds.length > 0;
}

function nodeVisible(n) {
  if (deadOnly && n.dead <= 0) return false;
  if (cycOnly && !n.cyc) return false;
  if (activeClusters.size && !activeClusters.has(n.cluster)) return false;
  if (!showTests && isTestNode(n)) return false;
  if (activeDirs.size && !activeDirs.has(n.dir)) return false;
  return true;
}
function typeVisible(ty) {
  if (ty === "call") return showCalls;
  if (ty === "signal") return showSignals;
  if (ty === "inst" || ty === "attach") return showInst;
  if (ty === "var") return showVar;   // member-var refs: dense, opt-in via the var toggle
  return true;
}
// focus-neighborhood compaction targets (see applyVisibility's hook): the
// lit set lerps 0.6 toward its centroid, then the Python layout's
// non-overlap guarantees are re-established deterministically
function compactLitSet() {
  const lit = [];
  for (let i = 0; i < N; i++)
    if (level[i] >= 0 && level[i] <= 1 && nodeVisible(nodes[i]) && !supMem[i]) lit.push(i);
  compactIdx = lit;
  compactScale = 1; compactOverlaps = 0;
  if (!lit.length) { compactTgt = null; return; }
  // Always compute from the pristine saved base (not live pos): the
  // anim-end applyVisibility re-run and mid-focus filter changes must
  // land on the SAME settled layout, not compound the centroid lerp.
  const work = new Float32Array(N * 3);
  work.set(posSaved || pos);
  const sp = Math.sqrt(spread);
  const rad = i => sphR(i);
  const CLR = 8;   // wire clearance (fn arcs ride at +14)
  const minD = (a, b) => rad(a) + rad(b) + CLR;
  // COMPACT BALL: center = focus seed; neighbors on a golden-spiral shell
  // sized so adjacent points start above min spacing - the depenetration +
  // exact-scale passes below guarantee the final no-overlap state.
  const fi = lit.find(i => level[i] === 0) ?? lit[0];
  focusFileIdx = fi;
  const cx = work[fi*3], cy = work[fi*3+1], cz = work[fi*3+2];
  let maxRad = 0;
  for (const i of lit) maxRad = Math.max(maxRad, rad(i));
  const others = lit.filter(i => i !== fi);
  const SP = 2 * maxRad + CLR + 6;
  const R = Math.max(rad(fi) + maxRad + CLR + SP * 0.5,
                     Math.sqrt(others.length || 1) * SP * 0.5);
  const GA = Math.PI * (3 - Math.sqrt(5));
  compactBallR = R;
  others.forEach((i, k) => {
    const y = 1 - (2 * (k + 0.5)) / others.length;
    const rr = Math.sqrt(Math.max(0, 1 - y * y));
    const th = GA * k;
    work[i*3]   = cx + Math.cos(th) * rr * R;
    work[i*3+1] = cy + y * R * 0.55;
    work[i*3+2] = cz + Math.sin(th) * rr * R;
  });
  work[fi*3] = cx; work[fi*3+1] = cy; work[fi*3+2] = cz;
  // depenetrate fused pairs on the deterministic hash axis (_layout parity)
  for (let sweep = 0; sweep < 8; sweep++) {
    let fused = 0;
    for (let x = 0; x < lit.length; x++) for (let y = x + 1; y < lit.length; y++) {
      const a = lit[x], b = lit[y];
      const dx = work[a*3] - work[b*3], dy = work[a*3+1] - work[b*3+1],
            dz = work[a*3+2] - work[b*3+2];
      if (Math.sqrt(dx*dx + dy*dy + dz*dz) >= minD(a, b) * 0.4) continue;
      const h = (a * 2654435761 + b * 40503) % 9973;
      const ang = h / 9973.0 * 6.2831853;
      let ax = Math.cos(ang), ay = 0.35 * Math.sin(ang * 1.7), az = Math.sin(ang);
      const al = Math.sqrt(ax*ax + ay*ay + az*az) || 1;
      ax /= al; ay /= al; az /= al;
      const sep = minD(a, b) * 1.2;
      const mx = (work[a*3] + work[b*3]) / 2, my = (work[a*3+1] + work[b*3+1]) / 2,
            mz = (work[a*3+2] + work[b*3+2]) / 2;
      work[a*3] = mx - ax * sep * 0.5; work[a*3+1] = my - ay * sep * 0.5;
      work[a*3+2] = mz - az * sep * 0.5;
      work[b*3] = mx + ax * sep * 0.5; work[b*3+1] = my + ay * sep * 0.5;
      work[b*3+2] = mz + az * sep * 0.5;
      fused++;
    }
    if (!fused) break;
  }
  // exact-scale finisher about the centroid: scaling is linear in the
  // offsets, so one multiply clears every lit pair. Alternating with the
  // wire clamp: a clamp push can shrink a pair below minD and a scale can
  // re-pierce a wire, so the two passes converge together (bounded rounds).
  let s = 1;   // cumulative exact-scale factor (reported via __dbg)
  const scalePass = () => {
    let s2 = 1;
    for (let x = 0; x < lit.length; x++) for (let y = x + 1; y < lit.length; y++) {
      const a = lit[x], b = lit[y];
      const dx = work[a*3] - work[b*3], dy = work[a*3+1] - work[b*3+1],
            dz = work[a*3+2] - work[b*3+2];
      const d = Math.sqrt(dx*dx + dy*dy + dz*dz);
      if (d > 1e-3) s2 = Math.max(s2, minD(a, b) / d);
    }
    if (s2 > 1) {
      for (const i of lit) {
        work[i*3]   = cx + (work[i*3]   - cx) * s2 * 1.01;
        work[i*3+1] = cy + (work[i*3+1] - cy) * s2 * 1.01;
        work[i*3+2] = cz + (work[i*3+2] - cz) * s2 * 1.01;
      }
      s *= s2 * 1.01;
    }
  };
  scalePass();
  // node-vs-wire clamp: push a lit sphere off any lit wire it pierces
  const seg = [];
  links.forEach(l => {
    if (level[l.s] < 0 || level[l.s] > 1 || level[l.t] < 0 || level[l.t] > 1) return;
    if (supMem[l.s] || supMem[l.t]) return;
    if (!typeVisible(l.ty) || nodeFiltered(nodes[l.s]) || nodeFiltered(nodes[l.t])) return;
    seg.push([l.s, l.t]);
  });
  const segD = (px, py, pz, a, b) => {
    const abx = work[b*3] - work[a*3], aby = work[b*3+1] - work[a*3+1],
          abz = work[b*3+2] - work[a*3+2];
    const apx = px - work[a*3], apy = py - work[a*3+1], apz = pz - work[a*3+2];
    const L = abx*abx + aby*aby + abz*abz;
    const t = L > 1e-6 ? Math.min(1, Math.max(0, (apx*abx + apy*aby + apz*abz) / L)) : 0;
    const qx = work[a*3] + abx*t, qy = work[a*3+1] + aby*t, qz = work[a*3+2] + abz*t;
    const dx = px - qx, dy = py - qy, dz = pz - qz;
    return { d: Math.sqrt(dx*dx + dy*dy + dz*dz), qx, qy, qz,
             ex: dx, ey: dy, ez: dz };
  };
  const clampPass = () => {
    let pushed = 0;
    for (const i of lit) for (const [a, b] of seg) {
      if (i === a || i === b) continue;
      const hit = segD(work[i*3], work[i*3+1], work[i*3+2], a, b);
      const need = (rad(i) + CLR) * 1.05;
      if (hit.d >= need) continue;
      // push along the TRUE perpendicular (node minus closest point) — a
      // midpoint-direction push is near-parallel to the wire for grazing
      // nodes and slides them along it without clearing
      let ex = hit.ex, ey = hit.ey, ez = hit.ez;
      const el = Math.sqrt(ex*ex + ey*ey + ez*ez);
      if (el < 1e-3) {
        const h = (i * 2654435761 + a * 40503) % 9973;
        const ang = h / 9973.0 * 6.2831853;
        ex = Math.cos(ang); ey = 0.35 * Math.sin(ang * 1.7); ez = Math.sin(ang);
      } else { ex /= el; ey /= el; ez /= el; }
      const push = need - hit.d;
      work[i*3] += ex * push; work[i*3+1] += ey * push; work[i*3+2] += ez * push;
      pushed++;
    }
    return pushed;
  };
  for (let round = 0; round < 8; round++) {
    scalePass();
    if (!clampPass()) break;
  }
  // residual violations (target 0): pairwise + sphere-vs-wire
  let bad = 0;
  for (let x = 0; x < lit.length; x++) for (let y = x + 1; y < lit.length; y++) {
    const a = lit[x], b = lit[y];
    const dx = work[a*3] - work[b*3], dy = work[a*3+1] - work[b*3+1],
          dz = work[a*3+2] - work[b*3+2];
    if (Math.sqrt(dx*dx + dy*dy + dz*dz) < minD(a, b)) bad++;
  }
  for (const i of lit) for (const [a, b] of seg) {
    if (i === a || i === b) continue;
    if (segD(work[i*3], work[i*3+1], work[i*3+2], a, b).d < rad(i) + CLR) bad++;
  }
  compactOverlaps = bad;
  compactScale = +s.toFixed(4);   // honest cumulative factor (margin already inside s)
  compactTgt = work;
}
// focus wire arcs: budget wires render as gentle bezier arcs (lift = 14% of
// span on +Y) in the map pane's wire-type palette. Straight chords crossing
// at one gray value were the confusion driver (Purchase/Huang: crossings
// dominate legibility); arcs separate crossing wires in height and the type
// color carries identity — same law as the 2D pane. One LineSegments,
// rebuilt from budgetLit (budget-capped 12 + hover reveals), so rebuilds
// stay cheap even per-frame during the compaction ease.
const TYPE_C3D = { call: 0xd9e2eb, signal: 0xffb347, var: 0x73e68c,
  attach: 0x3dccf2, inst: 0x3dccf2 };
let focusArcs = null;   // { lines, geo, mat }
const ARC_SEG = 14;
function rebuildFocusWires() {
  const list = [];
  // zero-wire .tscn affordance (C2.2 ruling): a deg-57+ hub whose whole
  // neighborhood sits in the ghost tier renders NOTHING about its
  // connectedness. Reveal the focused .tscn hub's top-8 budget links as
  // dim-but-traceable arcs — countable ink, not a full wire re-add.
  const revealed = new Set();
  const fi = focusFileIdx;
  const tscn = fi >= 0 && /\.tscn$/i.test(nodes[fi].path || "");
  if (tscn) for (let i = 0; i < links.length; i++) {
    const l = links[i];
    if (l.s !== fi && l.t !== fi) continue;
    if (budgetLit && budgetLit.size && budgetLit.has(i)) continue;  // budget ink
    revealed.add(i);
  }
  if (focusActive && budgetLit) budgetLit.forEach(i => {
    const l = links[i];
    if (alphaTgt[l.s] <= 0.05 && alphaTgt[l.t] <= 0.05) return;   // ghost pair
    list.push(i);
  });
  if (revealed.size) {
    const top = [...revealed].sort((a, b) => links[b].w - links[a].w || a - b).slice(0, 8);
    top.forEach(i => { revealed.delete(i); list.push(i); });
    top.forEach(i => revealed.add(i));
    list.sort((a, b) => a - b);   // deterministic vertex order
  } else if (!list.length) {
    if (focusArcs) {
      scene.remove(focusArcs.lines); focusArcs.lines.geometry.dispose();
      focusArcs = null;
    }
    return;
  }
  if (focusArcs) {
    scene.remove(focusArcs.lines); focusArcs.lines.geometry.dispose();
    focusArcs = null;
  }
  const n = list.length;
  const vCount = n * ARC_SEG * 2;
  const P = new Float32Array(vCount * 3);
  const C = new Float32Array(vCount * 3);
  const D = new Float32Array(vCount);
  const col = new THREE.Color();
  list.forEach((li, k) => {
    const l = links[li];
    const ax = pos[l.s*3], ay = pos[l.s*3+1], az = pos[l.s*3+2];
    const bx = pos[l.t*3], by = pos[l.t*3+1], bz = pos[l.t*3+2];
    const dist = Math.hypot(bx-ax, by-ay, bz-az) || 1;
    // control point: midpoint lifted 14% of the span — every arc rises, so
    // two crossing wires separate in height instead of sharing a pixel
    const mx = (ax + bx) / 2, my = (ay + by) / 2 + dist * 0.14, mz = (az + bz) / 2;
    const vis = Math.min(alphaTgt[l.s], alphaTgt[l.t]);
    col.setHex(TYPE_C3D[l.ty] || 0xd9e2eb);
    if (revealed.has(li)) col.multiplyScalar(0.42);   // C2.2 dim-but-traceable
    else if (vis < 0.5) col.multiplyScalar(EMPHASIS.DIM_ALPHA);   // dead-end / ghost endpoint
    const phase = ((li * 2654435761) % 997) / 997 * 13;   // per-link dash phase
    let px = 0, py = 0, pz = 0, pd = 0;
    for (let s = 0; s <= ARC_SEG; s++) {
      const t = s / ARC_SEG, u = 1 - t;
      const x = u*u*ax + 2*u*t*mx + t*t*bx;
      const y = u*u*ay + 2*u*t*my + t*t*by;
      const z = u*u*az + 2*u*t*mz + t*t*bz;
      const dd = s ? pd + Math.hypot(x-px, y-py, z-pz) : 0;
      if (s) {
        const vi = (k * ARC_SEG + s - 1) * 2;
        const p3 = vi * 3;
        P[p3] = px; P[p3+1] = py; P[p3+2] = pz;
        P[p3+3] = x; P[p3+4] = y; P[p3+5] = z;
        C[p3] = col.r; C[p3+1] = col.g; C[p3+2] = col.b;
        C[p3+3] = col.r; C[p3+4] = col.g; C[p3+5] = col.b;
        D[vi] = pd + phase; D[vi+1] = dd + phase;
      }
      px = x; py = y; pz = z; pd = dd;
    }
  });
  // fat-line rebuild per call: the budget is tiny (≤ hub budget arcs) and
  // Line2 gives the hub fan real 2px ink like every other wire
  const fgeo = new LineSegmentsGeometry();
  fgeo.setPositions(P);
  fgeo.setColors(C);
  const fmat = new LineMaterial({ vertexColors: true,
    transparent: true, opacity: 0.95, linewidth: 2, worldUnits: false,
    dashed: true, dashSize: 8, gapSize: 5, depthWrite: false,
    blending: THREE.NormalBlending, alphaToCoverage: false });
  fmat.resolution.set(glW(), innerHeight);
  const flines = new LineSegments2(fgeo, fmat);
  flines.computeLineDistances();
  flines.frustumCulled = false; flines.renderOrder = 3;
  scene.add(flines);
  focusArcs = { lines: flines, geo: fgeo, mat: fmat };
  // the arcs REPLACE the budget links' straight bucket chords (k-pass blacks
  // those) — hover/click must test THESE chords, or the collider stays on
  // the invisible pre-curve straight line
  flines.userData.meta = list.map(li => ({ kind: "link", li }));
  flines.userData.seg = ARC_SEG;
  focusArcs.lines.visible = true;
}
// ---- focus ink seeding (issue #39) ----------------------------------
// A scene hub (.tscn) can have EVERY link inst-typed: with the inst tier
// off (the boot default) focus lights its ball but draws zero budget
// ink — every wire the hub has is type-gated away. Focus is an explicit
// drill-down, so the boot→focus TRANSITION seeds the tier (the map pane
// auto-seeds its own ink tier the same way). One-shot only: never
// re-forced during the focus (the user's toggles win); Escape/reset
// return the boot default with every other focus-scoped control.
let focusWasOn = false;   // focus active as of the last applyVisibility
function instTierOnFocus() {
  if (showInst) return;
  const i = focusSeeds.values().next().value;
  let tot = 0, inst = 0;
  links.forEach(l => {
    if (l.s !== i && l.t !== i) return;
    tot++;
    if (l.ty === "inst" || l.ty === "attach") inst++;   // showInst-gated
  });
  if (!tot || inst * 2 <= tot) return;   // inst-dominant majority only
  showInst = true;
  document.getElementById("bInst").classList.add("on");
}
function applyVisibility() {
  // visibility flips change which occluder geometry exists (scale-0 gate)
  // and rewrite alphaTgt — both fileMesh inputs
  _arrowOcclDirty = true; _sfDirty = true;
  // the only moment the inst tier may auto-seed is the boot→focus
  // TRANSITION — before computeLevels so the lit/budget passes below
  // already see the tier on (issue #39).
  const entering = focusSeeds.size > 0 && !focusWasOn;
  focusWasOn = focusSeeds.size > 0;
  if (entering) instTierOnFocus();
  const focusing = computeLevels();
  focusActive = focusing;   // hover greyout defers to focus mode
  edgeFlowOn = focusing;   // tick's dash-flow pass reads this
  // edges are a quiet layer at overview (per-bucket caps) and open up when
  // a focus set is lit
  bucketMat.forEach((mat, bi) => { mat.opacity = focusing ? EMPHASIS.FOCUS_TIER_OPACITY : BUCKETS[bi].op; });
  updateEdgeLegend(focusing);
  // fn layer only makes sense inside a focus — say so instead of ignoring clicks
  cbFnEl.disabled = !focusing;
  cbFnEl.parentElement.title = focusing ? "" : "function layer needs a focus (click a node)";
  for (let i = 0; i < N; i++) {
    let a;
    if (!nodeVisible(nodes[i])) a = 0.0;   // size-0 gate = true disable
    else if (focusing) a = level[i] < 0 ? 0.0 : (level[i] <= depth ? 1 : 0.04);   // lit set = focus..`depth` hops (slider-owned radius); strata beyond ghost near-zero (0.04 < the 0.05 ghost kill, so their deep-deep wires collapse outright)
    else a = 1;
    alphaTgt[i] = a;
    if (a > 0.5) {
      const c = colorOf(nodes[i]);
      colArr[i*3] = c.r; colArr[i*3+1] = c.g; colArr[i*3+2] = c.b;
    }
  }
  // focus-neighborhood compaction: pull the lit set toward its centroid,
  // then mirror the Python layout's guarantees — deterministic hash-axis
  // depenetration for fused pairs, an exact-scale finisher about the
  // centroid (linear in the offsets: one multiply clears every pair), and
  // a node-vs-wire clamp so no lit wire pierces a lit sphere. pos is
  // mutated in place (picks, labels, fn arcs and the hub ring all read
  // pos); posSaved restores the frozen layout on unfocus.
  if (focusing) {
    if (!posSaved) {
      posSaved = pos.slice();
      compactLitSet();
      if (compactTgt) compactAnim = { t0: performance.now(), dur: 450 };
    } else if (!compactAnim) {
      // filters changed mid-focus: recompute deterministically from the
      // saved base (the running ease keeps ownership until it lands)
      compactLitSet();
      if (compactTgt) for (const i of compactIdx) {
        pos[i*3] = compactTgt[i*3]; pos[i*3+1] = compactTgt[i*3+1]; pos[i*3+2] = compactTgt[i*3+2];
      }
    }
  } else if (posSaved) {
    pos.set(posSaved); posSaved = null; compactTgt = null;
    compactIdx = []; compactAnim = null; compactScale = 1; compactOverlaps = 0;
    compactBallR = 0; focusFileIdx = -1;
  }
  // collapse pass: re-derives dpos + the supernode set from the targets
  // just computed; zeroes member alphaTgt (existing hide path fades the
  // spheres and drops hub labels)
  refreshCollapse();
  syncFileMesh();
  // dim edges: hidden endpoints, filtered types, or focus distance
  // (dimmed eColBase written straight into each bucket's instanced colors).
  // Overview palette: edge-type hues are demoted to weight-tinted gray so
  // cluster colors carry the overview; full type colors return on focus.
    const grayMix = focusing ? 0 : 0.6;
  // hub edge budget: rank the focus-lit links by weight desc; only the top
  // HUB_EDGE_BUDGET render lit, the rest ghost down. Candidates mirror the
  // lit branch below exactly — edges that would render k=0 (fn wire mode),
  // k=0.012 (dead-end dim) or k=0 (ghost/filtered) must not consume budget
  // slots. A hovered wire (hoverEdgeLi) is force-admitted: hover = reveal.
  if (focusing) {
    const deadEnd = j => alphaTgt[j] <= 0.5 && !supMem[j];
    const cand = [];
    links.forEach((l, i) => {
      if (!typeVisible(l.ty)) return;
      if (nodeFiltered(nodes[l.s]) || nodeFiltered(nodes[l.t])) return;
      if (alphaTgt[l.s] < 0.05 && alphaTgt[l.t] < 0.05) return;
      if (deadEnd(l.s) || deadEnd(l.t)) return;
      if (fnMode && l.ty === "call" && level[l.s] >= 0 && level[l.t] >= 0) return;
      cand.push([i, l.w]);
    });
    cand.sort((a, b) => b[1] - a[1] || a[0] - b[0]);
    budgetLit = new Set(cand.slice(0, HUB_EDGE_BUDGET).map(c => c[0]));
    if (hoverEdgeLi >= 0) budgetLit.add(hoverEdgeLi);
    // endpoint-hover reveal: hovering a node admits every surviving link
    // incident to it (the ghost layer hides by default; hover = show the
    // fan). Filters mirror the candidate loop INCLUDING the dead-end bar:
    // under hover greyout the far end can sit at alphaTgt 0.12, and a lit
    // arc into a near-invisible speck is the "signal wire with no visible
    // terminus" class (user sighting #6, upper-left) — focus-lit plain
    // links render only onto anchors that read (alpha above the lit
    // threshold; the arc endpoint's sprite gets the ANCHOR_PX floor).
    const fanLit = j => alphaTgt[j] > 0.5 || supMem[j];
    if (hovered >= 0) links.forEach((l, i) => {
      if (l.s !== hovered && l.t !== hovered) return;
      if (!typeVisible(l.ty) || nodeFiltered(nodes[l.s]) || nodeFiltered(nodes[l.t])) return;
      if (!fanLit(l.s) || !fanLit(l.t)) return;
      if (fnMode && l.ty === "call" && level[l.s] >= 0 && level[l.t] >= 0) return;
      budgetLit.add(i);
    });
  } else {
    budgetLit = null;
    hoverEdgeLi = -1;
  }
  const touched = [false, false, false];
  links.forEach((l, i) => {
    let k;
    // dir-filtered endpoints (tests/tools hidden, active dir isolation):
    // edges are killed outright, not dimmed — additive blending makes even
    // 1% gray visible when dozens of test edges converge on a hub
    const sFiltered = nodeFiltered(nodes[l.s]);
    const tFiltered = nodeFiltered(nodes[l.t]);
    // ghost: both endpoints outside the focus — kill outright (the arc
    // collapse in the k===0 branch below removes its baked geometry too)
    const ghost = alphaTgt[l.s] < 0.05 && alphaTgt[l.t] < 0.05;
    if (sFiltered || tFiltered || ghost) k = 0.0;
    // collapsed members carry alphaTgt 0 but their edges LIVE (re-targeted
    // to the supernode) — only non-member dim endpoints take the 0.012 dim
    else if (!typeVisible(l.ty) ||
      ((alphaTgt[l.s] <= 0.5 && !supMem[l.s]) || (alphaTgt[l.t] <= 0.5 && !supMem[l.t]))) k = 0.012;
    else if (fnMode && focusing && l.ty === "call" && level[l.s] >= 0 && level[l.t] >= 0) k = 0; // wire mode: fn wires replace the aggregate call line; geometry collapse (k===0 branch) handles invisibility — a ghost 0.04 double-draws under the additive fn wires
    else if (focusing) k = budgetLit && budgetLit.has(i)
      // budget wires render as curved type-colored arcs (rebuildFocusWires)
      // — the straight bucket segment goes black underneath them
      ? 0
      // quiet layer: non-budget wires hide by default (a ball full of dim
      // gray diagonals reads as noise); the ghosts toggle or a hover on an
      // endpoint brings them back
      : (showGhost ? GHOST_K : 0);
    else {
      // overview edge budget: single-ref wires are noise at full extent
      // (1530 lines summing to white over the core under additive blending)
      // - only multi-ref links earn a line until a focus opens the scene.
      // Corridors (hw arcs) are the intended carriers - they keep more ink
      // than same-band straight chords, which fade to near-nothing.
      k = l.w >= 2 ? 1 : 0.0;
      if (k > 0.0) k = hwSlot[i] >= 0 ? Math.min(1, k * 1.5) : k * 0.5;
    }
    if (!focusing && !lodClose && k > 0.04) {
      const dx = pos[l.s*3] - pos[l.t*3], dy = pos[l.s*3+1] - pos[l.t*3+1],
            dz = pos[l.s*3+2] - pos[l.t*3+2];
      const el3 = Math.sqrt(dx*dx + dy*dy + dz*dz);
      const sameC = nodes[l.s].cluster >= 0 && nodes[l.s].cluster === nodes[l.t].cluster;
      // overview edge-cut: intra-cluster edges hide entirely and long
      // ring-diameter chords dim out — otherwise they cross the whole
      // galaxy and re-form the hairball the layout just removed.
      // highway arcs are exempt from the chord cut: bundled beziers ARE
      // the intended inter-cluster carriers
      if (sameC) { if (k > 0.05) k = 0.05; }
      else if (el3 > 200 && hwSlot[i] < 0) k = 0.04;
    }
    edgeK[i] = k;   // ink truth for the picker — see edgeK decl
    const b = bucketOf[i], o6 = i * 6;
    const tgt = bucketColIB[b].array;
    if (k === 0) {
      // filtered-out edge (tests/tools hidden, dir filter, focus ghost):
      // colors go black here; GEOMETRY is syncEdgePos's job (it collapses
      // filtered/ghost arcs + straight edges — this pass used to write
      // arc positions too, racing the re-derivation and letting black
      // full-length wires paint over content)
      if (hwSlot[i] >= 0) {
        tgt.fill(0, hwSlot[i], hwSlot[i] + 96);
      } else {
        tgt.fill(0, slotOf[i] * 6, slotOf[i] * 6 + 6);
      }
      touched[b] = true;
      return;
    }
    if (hwSlot[i] >= 0) {
      // highway: replicate the (possibly grayed/dimmed) color across all
      // 16 segments. Geometry is NOT touched here — syncEdgePos() right
      // below re-derives arc positions in the CURRENT coordinate space
      // (this block used to restore baked arcs, detaching them from
      // spread-moved nodes)
      const base = hwSlot[i];
      if (grayMix > 0) {
        const g = (0.10 + 0.18 * Math.min(1, l.w / 8)) * k;
        for (let v = 0; v < 16; v++) {
          const s6 = base + v * 6;
          for (let c = 0; c < 6; c++) tgt[s6+c] = eColBase[o6+c] * (1 - grayMix) + g * grayMix;
        }
      } else {
        for (let v = 0; v < 16; v++) {
          const s6 = base + v * 6;
          for (let c = 0; c < 6; c++) tgt[s6+c] = eColBase[o6+c] * k;
        }
      }
      touched[b] = true;
      return;
    }
    const s6 = slotOf[i] * 6;
    if (grayMix > 0) {
      // dim weight-tinted gray: edges stay legible structure hints without
      // outshining the cluster-colored nodes under additive blending
      const g = (0.10 + 0.18 * Math.min(1, l.w / 8)) * k;
      for (let c = 0; c < 6; c++) tgt[s6+c] = eColBase[o6+c] * (1 - grayMix) + g * grayMix;
    } else {
      for (let c = 0; c < 6; c++) tgt[s6+c] = eColBase[o6+c] * k;
    }
    touched[b] = true;
  });
  touched.forEach((t, b) => { if (t) bucketColIB[b].needsUpdate = true; });
  syncEdgePos();   // geometry follows the new filter state (collapse/restore)
  syncSemAff();   // #279: affinity serve state follows the filter pass
  hoverGreyIdx = -1;   // baseline rebuilt — next hover re-greys from here
  rebuildFocusWires();   // budget arcs track budgetLit + live pos
  if (focusing) {
    let lit = 0;
    for (let i = 0; i < N; i++) if (alphaTgt[i] > 0.5) lit++;   // [#62] live lit set (level <= depth, filters folded in) — was pinned to the 1-hop ball, so the count went stale the moment the depth slider grew the set
    const first = focusSeeds.values().next().value;
    const label = focusSeeds.size === 1 ? esc(nodes[first].label)
      : focusSeeds.size + " files";
    crumb.innerHTML = "focus: <b>" + label + "</b> · depth " + depth +
      (dirMode ? " · dir " + (dirMode === 1 ? "out" : "in") : "") + " · " + lit +
      " files lit" +
      (focusStack.length ? "<span class='back' title='back (Backspace)'>‹</span>" : "") +
      "<span class='x' title='clear focus (Esc)'>✕</span>" +
      "<span class='hint'>Esc/right-click clears · ⌫ back</span>";
    crumb.style.display = "flex";
    crumb.querySelector(".x").onclick = clearFocus;
    const back = crumb.querySelector(".back");
    if (back) back.onclick = popFocus;
  } else {
    // empty state: nothing passes the filters — say so and offer one-chip undo
    let vis = 0;
    for (let i = 0; i < N; i++) if (nodeVisible(nodes[i])) vis++;
    if (vis === 0) {
      const clears = [];
      if (deadOnly) clears.push(["dead only", () => {
        deadOnly = false; bDeadEl.classList.remove("on");
      }]);
      activeClusters.forEach(c => clears.push([cNames[c] || "c" + c, () => {
        activeClusters.delete(c);
        const ch = legendChips.get(c); if (ch) ch.classList.remove("on");
      }]));
      activeDirs.forEach(d => clears.push([d, () => {
        activeDirs.delete(d);
        const ch = dirChips.get(d); if (ch) ch.classList.remove("on");
      }]));
      crumb.innerHTML = "0 files visible — " + clears.map((c, k) =>
        "<span class='f' data-k='" + k + "'>" + esc(c[0]) + " ✕</span>").join("");
      crumb.style.display = "flex";
      crumb.querySelectorAll(".f").forEach((el, k) =>
        el.onclick = () => { clears[k][1](); buildContainment(); applyVisibility(); });
    } else crumb.style.display = "none";
  }
  // cluster identity is an overview cue — hide the name labels on focus
  clabsEl.style.display = focusing ? "none" : "block";
  rebuildFnLayer(focusing);
  rebuildHubs();
  rebuildEdgeLabels(focusing);
  rebuildFocusLabels(focusing);
  drawMapPane();   // mermaid pane mirrors the new focus state
}

// ---- edge labels: focus detail mode (small lit set) ---------------------------
// when the focus set is small, label each in-set link's midpoint with its
// type and weight; large sets skip labels entirely to avoid clutter
// threshold is relative to graph size so depth-1 neighborhoods stay labeled
"""
