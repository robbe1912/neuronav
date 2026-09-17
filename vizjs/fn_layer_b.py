# vizjs/fn_layer_b.py — rung 9/17 of the template join (issues
# #86 phase-3 / #299 A): rebuildFnLayer — the fn/graph bus build. Moved VERBATIM from viz.py —
# every edit here changes graph.html: regen the bake (the standing
# template law; test_viz refuses a bake older than viz.py/vizjs).
_JS_FN_LAYER_B = r"""function rebuildFnLayer(focusing) {
  teardownLayer();   // #299 D: one walk, every layer asset (see layerAssets)
  fnMeta = [];
  rosterGen++;   // stale ball pins die with their roster (#16)
  if (!fnMode || !focusing) return;
  // fn-tier sizing (satBoost reads fnCount) changes file-sphere scales
  _sfDirty = true; _arrowOcclDirty = true;
  // pass 1: visible cross-file fn edges
  const visEdges = [];
  fedges.forEach(e => {
    const sf = e[0], df = e[2];
    if (level[sf] < 0 || level[df] < 0) return;
    // hidden files (tests/tools filter, dir filter): their function nodes
    // and edges must not render in the fn layer either
    if (alphaTgt[sf] <= 0.5 || alphaTgt[df] <= 0.5) return;
    visEdges.push(e);
  });
  // fn-layer scope: rank wires deterministically (file-pair link weight,
  // then name/line) so eidx order is stable across reloads. No cap — every
  // fn interconnection between lit files renders (amendment).
  const lw = new Map();
  links.forEach(l => lw.set(l.s + ":" + l.t, l.w));
  const eW = e => lw.get(e[0] + ":" + e[2]) || 0;
  visEdges.sort((a, b) => (eW(b) - eW(a)) || (a[0] - b[0]) ||
    (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0) || (a[2] - b[2]) ||
    (a[3] < b[3] ? -1 : a[3] > b[3] ? 1 : 0) || (a[4] - b[4]));
  // amendment: every fn interconnection between lit files renders — no
  // budget cap at fn grain (the lit set is 1-hop + compacted; AGG_MAX
  // still declutters per-file box rings, but wires stay complete)
  if (!visEdges.length) return;
  const fIdx = new Map(), fpos = [], fcol = [], eidx = [], wireRows = [];
  // mutators-only filter: when on, fn satellites for functions with no
  // member writes are not created at all (their wires collapse with them)
  const ioOf = (fi, name) => (DATA.fio || {})[fnKey(fi, name)];
  const isMutator = (fi, name) => {
    const io = ioOf(fi, name);
    return !!(io && io.w.length);
  };
  const nodeOf = (e, isSrc) => {
    const fi = isSrc ? e[0] : e[2], name = isSrc ? e[1] : e[3];
    if (mutOnly && !isMutator(fi, name)) return -1;
    const k = fi + "::" + name;
    let ix = fIdx.get(k);
    if (ix === undefined) {
      ix = fnMeta.length; fIdx.set(k, ix);
      // position filled by the owner-arc pass below (m.p is the hover and
      // click anchor, so it must end up at the rendered box)
      fnMeta.push({ file: fi, name, p: [0, 0, 0] });
      fpos.push(0, 0, 0);
      fcol.push(colArr[fi*3], colArr[fi*3+1], colArr[fi*3+2]);
    }
    return ix;
  };
  visEdges.forEach(e => {
    const a = nodeOf(e, true), b = nodeOf(e, false);
    if (a < 0 || b < 0) return;   // filtered out by mutators-only
    eidx.push(a, b);
    wireRows.push(e);   // parallel row for wire descriptions (line number)
  });
  if (!fnMeta.length) return;
  // pass 2: fn boxes orbit their OWNER file sphere. Each lit file's fns
  // sit on a 120-degree arc around the sphere (radius grows +6 every 3rd
  // box so consecutive boxes never stack), centered on the mean XZ
  // direction toward the centroid of the owner's outgoing-wire targets —
  // neighbors sorted by index so the sum is deterministic; owners with no
  // outgoing wires face their callers instead. Ownership then reads at a
  // glance: box color, a short stalk to the sphere surface, and the arc's
  // facing all point at the owner, where wire midpoints blurred it.
  const AGG_MAX = 6;
  const byFile = new Map();
  for (let i = 0; i < fnMeta.length; i++) {
    const fi = fnMeta[i].file;
    let arr = byFile.get(fi);
    if (!arr) byFile.set(fi, arr = []);
    arr.push(i);
  }
  const aggs = [];
  for (const [fi, arr] of byFile) {
    const tgts = new Set(), srcs = new Set();
    for (const e of visEdges) {
      if (e[0] === fi) tgts.add(e[2]);
      if (e[2] === fi) srcs.add(e[0]);
    }
    const facing = [...(tgts.size ? tgts : srcs)].sort((a, b) => a - b);
    let mx = 0, mz = 0;
    for (const j of facing) { mx += pos[j*3] - pos[fi*3]; mz += pos[j*3+2] - pos[fi*3+2]; }
    const th = (mx || mz) ? Math.atan2(mz, mx) : 0;
    const oR = sphR(fi);   // rendered sphere radius (satellite-boosted)
    const arcR = oR + 14;
    const n = arr.length;
    // multi-ring arc: 12 boxes per ring, +8 out per ring — a 40-fn subject
    // spreads over 4 readable rings instead of collapsing into one 'n×'
    const rows = Math.max(1, Math.ceil(n / 12));
    const per = Math.ceil(n / rows);
    for (let i = 0; i < n; i++) {
      const m = fnMeta[arr[i]];
      const ring = Math.floor(i / per), k = i % per;
      const cnt = Math.min(per, n - ring * per);
      const ang = th - Math.PI / 3 + (Math.PI * 2 / 3) * (k + 0.5) / cnt;
      const r = arcR + ring * 8;
      m.p[0] = pos[fi*3] + Math.cos(ang) * r;
      m.p[1] = pos[fi*3+1];
      m.p[2] = pos[fi*3+2] + Math.sin(ang) * r;
      fpos[arr[i]*3] = m.p[0]; fpos[arr[i]*3+1] = m.p[1]; fpos[arr[i]*3+2] = m.p[2];
    }
    // file aggregation: a file owning more than AGG_MAX fns collapses to
    // ONE 'n×' box at the arc center — a ring of 7+ scale-4 boxes reads as
    // clutter, and '7×' carries the information denser. fnMeta keeps its
    // indices (flabs + hover map by ix): members keep their arc slot but
    // render scale-0 and are not hoverable; the aggregate is an appended
    // entry with count=n.
    if (n > AGG_MAX && level[fi] > 0) {   // the focus subject keeps every fn visible
      for (const ix of arr) fnMeta[ix].agg = true;
      // sphereClear parity: individual boxes ride rings arcR..arcR+12; the
      // aggregate takes the OUTER ring (arcR+12) so a big owner sphere plus
      // its hover lift never swallows it
      aggs.push({ file: fi, count: n,
        p: [pos[fi*3] + Math.cos(th) * (arcR + 12), pos[fi*3+1],
            pos[fi*3+2] + Math.sin(th) * (arcR + 12)] });
    }
  }
  for (const ag of aggs) {
    fpos.push(ag.p[0], ag.p[1], ag.p[2]);
    fcol.push(colArr[ag.file*3], colArr[ag.file*3+1], colArr[ag.file*3+2]);
    fnMeta.push({ file: ag.file, name: "", p: ag.p, count: ag.count });
  }
  // collapsed members (agg, no count) render at scale 0 — wires and
  // chevrons that touch them must land on the file's VISIBLE aggregate
  // box, else deliveries aim at invisible boxes and read as floating
  const aggP = new Map();
  for (const ag of aggs) aggP.set(ag.file, ag.p);
  // fn boxes: true 3D cubes on their owner arcs, colored by owning cluster hue
  const fdummy = new THREE.Object3D();
  fnMesh = new THREE.InstancedMesh(
    new THREE.BoxGeometry(1, 1, 1),
    new THREE.MeshLambertMaterial(),
    fnMeta.length
  );
  fnMesh.frustumCulled = false;   // positions rebuilt on every focus change
  for (let i = 0; i < fnMeta.length; i++) {
    fdummy.position.set(fpos[i*3], fpos[i*3+1], fpos[i*3+2]);
    const m = fnMeta[i];
    fdummy.scale.setScalar(m.count ? 6 : (m.agg ? 0 : 4));
    fdummy.updateMatrix();
    fnMesh.setMatrixAt(i, fdummy.matrix);
    _col.setRGB(fcol[i*3], fcol[i*3+1], fcol[i*3+2]);
    fnMesh.setColorAt(i, _col);
  }
  fnMesh.instanceMatrix.needsUpdate = true;
  if (fnMesh.instanceColor) fnMesh.instanceColor.needsUpdate = true;
  scene.add(fnMesh);
  // persistent fn-ownership stalks: one SHORT segment per rendered box from
  // the box down to its owner's sphere SURFACE, in the owner's own hue —
  // the arc already says "these fns live here", the stalk pins each box to
  // its sphere. The bright hover stalk (fnStalk) rides on top of these;
  // lifetime matches the fn layer (rebuilt + disposed with it, so cbFn
  // visibility carries over).
  const stk = new Float32Array(fnMeta.length * 6);
  const stc = new Float32Array(fnMeta.length * 6);
  for (let i = 0; i < fnMeta.length; i++) {
    const m = fnMeta[i], o = i * 6;
    const collapsed = m.agg && !m.count;   // no box → no stalk
    let ux = m.p[0] - pos[m.file*3], uy = m.p[1] - pos[m.file*3+1], uz = m.p[2] - pos[m.file*3+2];
    const ul = Math.sqrt(ux*ux + uy*uy + uz*uz) || 1;
    const oR = sphR(m.file);
    stk[o]   = m.p[0]; stk[o+1] = m.p[1]; stk[o+2] = m.p[2];
    stk[o+3] = collapsed ? m.p[0] : pos[m.file*3] + ux / ul * oR;
    stk[o+4] = collapsed ? m.p[1] : pos[m.file*3+1] + uy / ul * oR;
    stk[o+5] = collapsed ? m.p[2] : pos[m.file*3+2] + uz / ul * oR;
    for (let v = 0; v < 6; v += 3) {
      stc[o+v] = fcol[i*3]; stc[o+v+1] = fcol[i*3+1]; stc[o+v+2] = fcol[i*3+2];
    }
  }
  // [#12] translucent focus container: the focused file renders as a glass
  // hull HOLDING its fn boxes — radius = farthest rendered box corner +
  // margin, centered on the file sphere (the dimmed core). Faint front-side
  // fill + back-side rim (the cheap fresnel: only the silhouette reads).
  // depthWrite off + renderOrder -1: fn ink inside draws over it, corridor
  // geometry (stations/bollards sit at ringOut+STATION_R, beyond this
  // radius) stays outside — corridor-complete law and pick parity intact.
  // No raycast targets include the hull: the sphere behind it keeps the
  // file card pick, boxes stay pickable (glass does not steal clicks).
  if (focusFileIdx >= 0) {
    let hR = 0, hN = 0;
    for (const m of fnMeta) {
      if (m.file !== focusFileIdx) continue;
      if (m.agg && !m.count) continue;   // collapsed member: no ink
      hN++;
      const half = m.count ? 3 : 2;      // rendered box scales 6 / 4
      const d = Math.hypot(m.p[0] - pos[focusFileIdx*3],
                           m.p[1] - pos[focusFileIdx*3+1],
                           m.p[2] - pos[focusFileIdx*3+2]) + half;
      if (d > hR) hR = d;
    }
    if (hN && hR > 0) {
      const hGeom = new THREE.SphereGeometry(hR + 8, 28, 20);
      const mkHull = (side, op) => {
        const mm = new THREE.Mesh(hGeom, new THREE.MeshBasicMaterial(
          { color: new THREE.Color().setHSL(0.10, 0.10, 0.86),
            transparent: true, opacity: op, depthWrite: false, side,
            fog: false }));
        mm.position.set(pos[focusFileIdx*3], pos[focusFileIdx*3+1],
                        pos[focusFileIdx*3+2]);
        mm.frustumCulled = false;
        mm.renderOrder = -1;
        scene.add(mm);
        return mm;
      };
      focusHull = { fill: mkHull(THREE.FrontSide, 0.05),
                    rim: mkHull(THREE.BackSide, 0.16),
                    R: hR + 8, fi: focusFileIdx, boxes: hN };
    }
  }
  const g3 = new THREE.BufferGeometry();
  g3.setAttribute("position", new THREE.BufferAttribute(stk, 3));
  g3.setAttribute("color", new THREE.BufferAttribute(stc, 3));
  fnStalks = new THREE.LineSegments(g3, new THREE.LineBasicMaterial(
    { vertexColors: true, transparent: true, opacity: 0.5, depthWrite: false }));
  scene.add(fnStalks);
  // call wires connect box to box — arcs park near their owners, so these
  // read as short owner-to-owner jumps through the fn boxes
  // call wires connect box to box as gentle arcs (per-wire lift separates
  // crossings in height). Two TIER laws keep a depth-1 hub readable:
  //   - BRIGHT: wires touching a focus-subject file (level 0) — 0.75 ink,
  //     gradient source→target, arrowhead cone, dash-flow
  //   - QUIET: neighbor↔neighbor wires — same geometry, 0.16 ink, no arrows
  //     (the user: "extreme clutter on the game manager ... wires all over
  //     the place"). Present for tracing, silent in the overview.
  const FS = 8;
  const mk = () => ({ ep: [], ec: [], ed: [], meta: [] });
  const tierB = mk(), tierQ = mk();
  const aPos = [], aDir = [], aCol = [], aBox = [], aLeg = [];
  const cA = new THREE.Color(), cB = new THREE.Color();
  // BUS pass — two granularities, both SHARED-DESTINATION only (blind
  // bundling hurts path tracing, McGee & Dingliana 2012):
  //  1. FILE-PAIR TRUNKS: >=3 bright wires between the same two files
  //     collapse into ONE trunk arc (the 2D spine law brought to 3D).
  //     Member wires become short taps from their fn box to the trunk
  //     midpoint — no per-wire arrows (the cone storm was the clutter);
  //     the trunk carries the one direction arrow.
  //  2. FN JUNCTIONS: among non-trunked wires, >=3 into the same fn box
  //     merge at an onramp short of the box (one trunk, one pin arrow).
  const pairCnt = new Map(), qPairCnt = new Map(), wireTier = [];
  for (let i = 0; i < eidx.length; i += 2) {
    const a = eidx[i], b = eidx[i+1];
    const bright = level[fnMeta[a].file] === 0 || level[fnMeta[b].file] === 0;
    wireTier.push(bright ? 1 : 0);
    const k = fnMeta[a].file + ">" + fnMeta[b].file;
    if (bright) pairCnt.set(k, (pairCnt.get(k) || 0) + 1);
    else qPairCnt.set(k, (qPairCnt.get(k) || 0) + 1);
  }
  const trunked = new Set();
  for (const [k, n] of pairCnt) if (n >= 3) trunked.add(k);
  const qTrunked = new Set();
  for (const [k, n] of qPairCnt) if (n >= QUIET_TRUNK_MIN) qTrunked.add(k);
  // ---- bus stations (bus3d_spec D1-D3): per-file junctions on a ring
  // BEYOND the outermost occupied fn-box ring — provably open air. The
  // old sphR*1.16 entry shell sat INSIDE ring 1 for every lit file
  // (1.16*sphR < sphR+14 whenever sphR < 87.5; max seen 32.1), which is
  const STATION_R = 32, SUBJ_R = 27;
  // shell could never escape the swarm radially.
  const ringOut = new Map();   // fi -> outermost occupied fn-box ring
  for (const [fi, arr] of byFile) {
    // aggregated files (n > AGG_MAX, non-focus) render ONE 'n×' box at
    // arcR+12 — 12 PAST the member rings; it owns the outermost radius
    const agg = arr.length > AGG_MAX && level[fi] > 0;
    ringOut.set(fi, Math.max(sphR(fi) + 14 + (Math.max(1, Math.ceil(arr.length / 12)) - 1) * 8,
                               agg ? sphR(fi) + 26 : 0));
  }
  const boxesOf = new Map();   // fi -> rendered fn indices (agg members out)
  for (let i = 0; i < fnMeta.length; i++) {
    const m = fnMeta[i]; if (m.agg && !m.count) continue;
    let arr = boxesOf.get(m.file); if (!arr) boxesOf.set(m.file, arr = []);
    arr.push(i);
  }
  const angMin = (a, b) => { const d = Math.abs(a - b) % (2*Math.PI);
    return d > Math.PI ? 2*Math.PI - d : d; };
  const cirMean = bs => { let x = 0, z = 0;
    for (const b of bs) { x += Math.cos(b); z += Math.sin(b); }
    return Math.atan2(z, x); };
  const brgOf = (fi, p) => Math.atan2(p[2] - pos[fi*3+2], p[0] - pos[fi*3]);
  // scored bearing: 16 fixed candidates + the mean-fan bearing — argmin
  // occlusion against EVERY rendered box (own swarm first, foreign
  // swarms too — rubric clearance is file-agnostic) + a small pull
  // toward the mean. Ties: lower index.
  const allBoxes = [];
  for (const [, arr] of boxesOf) for (const ix of arr) allBoxes.push(ix);
  const stationAt = (fi, mean) => {
    const R = (ringOut.get(fi) || sphR(fi)) + STATION_R;
    const cx = pos[fi*3], cy = pos[fi*3+1], cz = pos[fi*3+2];
    let best = null;
    for (let ci = 0; ci <= 16; ci++) {
      const phi = ci < 16 ? ci * Math.PI / 8 : mean;
      const px = cx + Math.cos(phi) * R, pz = cz + Math.sin(phi) * R;
      let cost = 0.4 * angMin(phi, mean) * angMin(phi, mean);
      for (const ix of allBoxes) {
        const dx = px - fpos[ix*3], dy = cy - fpos[ix*3+1], dz = pz - fpos[ix*3+2];
        const d2 = dx*dx + dy*dy + dz*dz;
        if (d2 > 3600) continue;   // 60 wu and out: no say
        cost += 1 / Math.max(d2, 36);
      }
      // +Y depth stagger: same-plane dots merge edge-on (skeptic R3) —
      // stations float 12 wu above the box plane; tree legs approach in
      // the empty lane BELOW the horizontal trunk fan
      if (!best || cost < best.cost - 1e-12)
        best = { cost, phi, p: [px, cy + 16, pz] };
    }
    return best;
  };
  // sector split: cut at the largest circular gap when spread > 2.1 rad,
  // one more internal cut if a side is still wide (<= 3 groups).
  const sectorize = items => {
    if (items.length < 2) return [items.slice()];
    const s = items.slice().sort((a, b) => a.brg - b.brg);
    let gi = 0, gw = -1;
    for (let i = 0; i < s.length; i++) {
      const nx = i + 1 < s.length ? s[i+1].brg : s[0].brg + 2*Math.PI;
      if (nx - s[i].brg > gw) { gw = nx - s[i].brg; gi = i; }
    }
    if (2*Math.PI - gw <= 2.1) return [s];
    const cut2 = g => {
      if (g.length < 2 || g[g.length-1].brg - g[0].brg <= 2.1) return [g];
      let j = 0, w = -1;
      for (let i = 0; i + 1 < g.length; i++)
        if (g[i+1].brg - g[i].brg > w) { w = g[i+1].brg - g[i].brg; j = i; }
      return [g.slice(0, j+1), g.slice(j+1)];
    };
    return cut2(s.slice(gi + 1)).concat(cut2(s.slice(0, gi + 1)));
  };
  // per-FILE trunked-pair groups (arrivals and departures SHARE one
  // station per sector — a Blueprint reroute is bidirectional, and split
  // in/out stations 13 wu apart made their trunk fans graze at ~0.4 wu)
  const stGrp = new Map();
  for (const k of trunked) {
    const parts = k.split(">"), sf = +parts[0], tf = +parts[1];
    for (const [fi, oth, role] of [[sf, tf, 0], [tf, sf, 1]]) {
      let g = stGrp.get(fi);
      if (!g) stGrp.set(fi, g = { fi, items: [], wires: 0 });
      g.items.push({ tk: k, role, w: pairCnt.get(k),
        brg: Math.atan2(pos[oth*3+2] - pos[fi*3+2], pos[oth*3] - pos[fi*3]) });
      g.wires += pairCnt.get(k);
    }
  }
  const feedOf = new Map();   // tk -> {out:[src box], in:[tgt box]}
  for (let i = 0, p = 0; i < eidx.length; i += 2, p++) {
    if (!wireTier[p]) continue;
    const k = fnMeta[eidx[i]].file + ">" + fnMeta[eidx[i+1]].file;
    if (!trunked.has(k)) continue;
    let f = feedOf.get(k); if (!f) feedOf.set(k, f = { out: [], in: [] });
    if (!fnMeta[eidx[i]].agg || fnMeta[eidx[i]].count) f.out.push(eidx[i]);
    if (!fnMeta[eidx[i+1]].agg || fnMeta[eidx[i+1]].count) f.in.push(eidx[i+1]);
  }
  const stations = new Map();  // tk -> {0: srcStation, 1: tgtStation}
  const stList = [];           // all stations, stable order
  for (const [, g] of stGrp)
    for (const grp of sectorize(g.items)) {
      const mean = cirMean(grp.map(x => x.brg));
      const st = stationAt(g.fi, mean);
      const wires = grp.reduce((sm, x) => sm + x.w, 0);
      const S = { id: stList.length, fi: g.fi, p: st.p, brg: st.phi,
                  wires, tks: [], subJ: [], boxSub: new Map() };
      for (const x of grp) S.tks.push(x.tk);
      // shallow fan tree (spec D3): merges with >3 wires split into moat
      // sub-junctions by box bearing — taps leave radially OUTWARD and no
      // dot sees more than ~3 legs (skeptic R7). boxSub keyed by role:box
      // (a box can both feed and receive through this station).
      if (wires > 3) {
        const feed = new Map();   // role:ix -> ix (dedup, stable)
        for (const x of grp)
          for (const ix of (feedOf.get(x.tk) || {out:[],in:[]})[x.role ? "in" : "out"])
            feed.set(x.role + ":" + ix, ix);
        const fb = [...feed.keys()].map(key => feed.get(key))
          .sort((a, b) => brgOf(g.fi, [fpos[a*3], 0, fpos[a*3+2]]) -
                        brgOf(g.fi, [fpos[b*3], 0, fpos[b*3+2]]));
        const nSub = Math.min(6, Math.max(2, Math.ceil(wires / 4))), per = Math.ceil(fb.length / nSub);
        const R = (ringOut.get(g.fi) || sphR(g.fi)) + SUBJ_R;
        // subJ bearings are FORCED APART around the station bearing (0.9
        // rad steps, half-shifted so no dot sits ON the bearing): member
        // boxes still assign by sorted bearing, but the dots themselves
        // never sit collinear with the tangent slot axis (skeptic B1) —
        // and the Y ladder runs DOWNWARD below the swarm plane (station
        // holds +Y): in-plane gaps foreshortened by the d2 camera split on
        // the vertical axis instead (skeptic R3 real-pairs)
        for (let si = 0; si < nSub; si++) {
          const mem = fb.slice(si * per, (si + 1) * per);
          if (!mem.length) continue;
          const sb = S.brg + (si - (nSub - 1) / 2) * 0.9 + 0.45;
          const sp = [pos[g.fi*3] + Math.cos(sb) * R,
                      pos[g.fi*3+1] - 4 - Math.min(20, 5 * si),
                      pos[g.fi*3+2] + Math.sin(sb) * R];
          for (const ix of mem) {
            // a box in both roles maps by whichever role the wire uses
            S.boxSub.set("0:" + ix, sp);
            S.boxSub.set("1:" + ix, sp);
          }
          S.subJ.push(sp);
        }
      }
      for (const x of grp) {
        let m = stations.get(x.tk); if (!m) stations.set(x.tk, m = {});
        m[x.role] = S;
      }
      stList.push(S);
    }
  // ISSUE #5 hard floor for stations: placement cost above only SOFTLY
  // avoids boxes (1/d2 within 60 wu); a packed satellite field can still
  // win the cost and park the station bollard inside foreign box ink —
  // the -10..-17px jClearMinPx readings. Slide the station along its own
  // ring to the first bearing clear of EVERY rendered box by >=16 wu and
  // of earlier stations by >=14 wu (3D gaps: stations float cy+16), then
  // grow the ring as a last resort. In-place mutation: trunk termini,
  // leg anchors and busJunc all alias S.p; S.brg follows so leg tangents
  // stay perpendicular. (Subs + Jof get the same treatment, on their own
  // rings, inside the moat block below.)
  {
    const clear16 = (x, y, z) => {
      for (const ix of allBoxes)
        if (Math.hypot(x - fpos[ix*3], y - fpos[ix*3+1], z - fpos[ix*3+2]) < 16)
          return false;
      return true;
    };
    for (const S of stList) {
      if (clear16(S.p[0], S.p[1], S.p[2])) {
        let ok = true;
        for (const Q of stList)
          if (Q !== S && Math.hypot(S.p[0]-Q.p[0], S.p[1]-Q.p[1], S.p[2]-Q.p[2]) < 14) { ok = false; break; }
        if (ok) continue;
      }
      const cx = pos[S.fi*3], cy = pos[S.fi*3+1], cz = pos[S.fi*3+2];
      const stOk = (x, y, z, self) => {
        for (const Q of stList)
          if (Q !== self && Math.hypot(x-Q.p[0], y-Q.p[1], z-Q.p[2]) < 14) return false;
        return true;
      };
      let done = false;
      for (let grow = 0; !done && grow <= 4; grow++) {
        const R = (ringOut.get(S.fi) || sphR(S.fi)) + STATION_R + grow * 8;
        const b0 = Math.atan2(S.p[2] - cz, S.p[0] - cx);
        for (let k = 0; k <= 24 && !done; k++) {
          for (const sgn of (k ? [1, -1] : [0])) {
            const phi = b0 + sgn * k * Math.PI / 24;
            const x = cx + Math.cos(phi) * R, z = cz + Math.sin(phi) * R;
            if (!clear16(x, cy + 16, z) || !stOk(x, cy + 16, z, S)) continue;
            S.p[0] = x; S.p[1] = cy + 16; S.p[2] = z; S.brg = phi;
            done = true; break;
          }
        }
      }
    }
  }
  const inB = new Map(), dirB = new Map();
  for (let i = 0, p = 0; i < eidx.length; i += 2, p++) {
    const a = eidx[i], b = eidx[i+1];
    if (!wireTier[p] || trunked.has(fnMeta[a].file + ">" + fnMeta[b].file)) continue;
    inB.set(b, (inB.get(b) || 0) + 1);
    const dx = fpos[b*3] - fpos[a*3], dy = fpos[b*3+1] - fpos[a*3+1],
          dz = fpos[b*3+2] - fpos[a*3+2];
    const l = Math.hypot(dx, dy, dz) || 1;
    const d = dirB.get(b) || [0, 0, 0];
    d[0] += dx / l; d[1] += dy / l; d[2] += dz / l;
    dirB.set(b, d);
  }
  const Jof = new Map(), JofR = new Map();
  for (const [b, n] of inB) if (n >= 2) {
    // moat anchor: radially OUTWARD past the outermost occupied radius
    // (spec D4) — the old 14-off-box dot sat inside the swarm band
    const fi = fnMeta[b].file;
    const ox = fpos[b*3] - pos[fi*3], oz = fpos[b*3+2] - pos[fi*3+2];
    const ol = Math.hypot(ox, oz) || 1;
    const L = Math.max(SUBJ_R, (ringOut.get(fi) || sphR(fi)) - ol + SUBJ_R);
    Jof.set(b, [fpos[b*3] + ox / ol * L, pos[fi*3+1], fpos[b*3+2] + oz / ol * L]);
    JofR.set(b, L);
  }
  // final moat relaxation: rotate sub-junctions + Jof dots away from ANY
  // same-file dot within 10 wu and any box center within 16 wu — 8 bounded
  // rounds of clamped tangential rotation, deterministic scan order.
  {
    const moat = [];
    for (const S of stList)
      for (const sp of S.subJ)
        moat.push({ fi: S.fi, p: sp, r: (ringOut.get(S.fi) || sphR(S.fi)) + SUBJ_R });
    for (const [b, J] of Jof)
      moat.push({ fi: fnMeta[b].file, p: J, r: JofR.get(b), jofB: b });
    const sgn = (a, b) => ((a - b + Math.PI) % (2*Math.PI) + 2*Math.PI) % (2*Math.PI) - Math.PI >= 0 ? 1 : -1;
    for (let round = 0; round < 8; round++)
      for (const md of moat) {
        const brg = brgOf(md.fi, md.p);
        let rot = 0;
        for (const S of stList) {
          if (S.fi !== md.fi) continue;
          for (const q of [S.p, ...S.subJ]) {
            if (q === md.p) continue;
            if (Math.hypot(md.p[0]-q[0], md.p[1]-q[1], md.p[2]-q[2]) < 10)
              rot += sgn(brg, brgOf(md.fi, q)) * 0.10;
          }
        }
        for (const om of moat) {
          if (om === md || om.fi !== md.fi) continue;
          if (Math.hypot(md.p[0]-om.p[0], md.p[1]-om.p[1], md.p[2]-om.p[2]) < 10)
            rot += sgn(brg, brgOf(md.fi, om.p)) * 0.10;
        }
        // cross-file moat dots: adjacent files' rings can intersect (a vfx
        // subJ and a gm subJ sat 22 wu apart, 2.4 wu depth gap, 5.8px on
        // screen at d2) — rotate away from foreign dots too, gentler weight
        for (const om of moat) {
          if (om === md || om.fi === md.fi) continue;
          if (Math.hypot(md.p[0]-om.p[0], md.p[1]-om.p[1], md.p[2]-om.p[2]) < 24)
            rot += sgn(brg, brgOf(md.fi, om.p)) * 0.05;
        }
        for (const ix of allBoxes) {
          const q = [fpos[ix*3], fpos[ix*3+1], fpos[ix*3+2]];
          if (Math.hypot(md.p[0]-q[0], md.p[1]-q[1], md.p[2]-q[2]) < 16)
            rot += sgn(brg, brgOf(md.fi, q)) * 0.06;
        }
        rot = Math.max(-0.15, Math.min(0.15, rot));
        if (rot) {
          const nb = brg + rot;
          md.p[0] = pos[md.fi*3] + Math.cos(nb) * md.r;
          md.p[2] = pos[md.fi*3+2] + Math.sin(nb) * md.r;
        }
    }
    // ISSUE #5 hard floor for tree dots (subs + Jof): a soft nudge cannot
    // escape a dense field — the packed-satellite views park sub bollards
    // a couple of wu from foreign box centers. Scan the dot's own ring
    // (deterministic +/- steps from the current bearing) for the first
    // bearing clear of every rendered box by >=15 wu and of stations by
    // >=10 wu, growing the ring radius as a last resort. In-place p keeps
    // boxSub / leg termini / busJunc aliasing the same array; a grown Jof
    // ring must re-publish JofR (separation re-anchors at the ring radius).
    const boxClearAt = (x, y, z, floor) => {
      for (const ix of allBoxes)
        if (Math.hypot(x - fpos[ix*3], y - fpos[ix*3+1], z - fpos[ix*3+2]) < floor)
          return false;
      return true;
    };
    for (const md of moat) {
      if (boxClearAt(md.p[0], md.p[1], md.p[2], 15)) continue;
      const y0 = md.p[1];
      const cx = pos[md.fi*3], cz = pos[md.fi*3+2];
      const stOk = (x, z) => {
        for (const S of stList)
          if (Math.hypot(x - S.p[0], z - S.p[2]) < 10) return false;
        return true;
      };
      let done = false;
      for (let grow = 0; !done && grow <= 4; grow++) {
        const r = md.r + grow * 6;
        const b0 = Math.atan2(md.p[2] - cz, md.p[0] - cx);
        for (let k = 0; k <= 16 && !done; k++) {
          for (const sgn of (k ? [1, -1] : [0])) {
            const phi = b0 + sgn * k * Math.PI / 16;
            const x = cx + Math.cos(phi) * r, z = cz + Math.sin(phi) * r;
            if (!boxClearAt(x, y0, z, 15) || !stOk(x, z)) continue;
            md.p[0] = x; md.p[2] = z; md.r = r;
            if (md.jofB !== undefined) JofR.set(md.jofB, r);
            done = true; break;
          }
        }
      }
    }
  }
  // ISSUE #7 experiment log: a world-side polyline floor on the arcs
  // (arcDodge: Gauss-Seidel control-point pushes, +Y and lateral
  // variants, 9-11 wu floors) was built, measured, and DELETED — every
  // variant reshuffled fan curvature for +1..2 crossTT over base
  // (hub2/orbit45) while the screen-side park law in the tick already
  // owns the visible fn-box ink band. Placement floors above keep the
  // endpoints out of box fields; obsLift keeps the tall-obstacle masts.
  // shared arc emitter: 8 quadratic segments (16 verts — the harness
  // counts wires as verts/16), optional arrowhead at the end tangent
  const emitArc = (T, ax, ay, az, bx, by, bz,
                   ar, ag, ab, br, bg, bb, phase, liftFrac, arrow, meta) => {
    if (meta) T.meta.push(meta);   // 1 meta entry per arc (8 segments each)
    const dist = qSpan(ax, ay, az, bx, by, bz);
    // lift ALWAYS +Y (no sign hack, no -Y dives) — the qMid conduit law
    const m = qMid(ax, ay, az, bx, by, bz, liftFrac * dist);
    let px = ax, py = ay, pz = az, pd = 0;
    for (let s = 1; s <= FS; s++) {
      const q = qPt(ax, ay, az, m[0], m[1], m[2], bx, by, bz, s / FS);
      const x = q.x, y = q.y, z = q.z;
      const dd = pd + Math.hypot(x-px, y-py, z-pz);
      T.ep.push(px, py, pz, x, y, z);
      T.ec.push(ar, ag, ab, br, bg, bb);
      T.ed.push(pd + phase, dd + phase);
      if (s === FS && arrow) {
        // delivery arrow sits EXACTLY at the arc end with the end tangent
        // (t=1 derivative 2(B-M)) — tips must land on the delivery
        // geometry, not one segment short of it (skeptic R4: 1.9px)
        qTan(ax, ay, az, m[0], m[1], m[2], bx, by, bz, 1);
        aPos.push(bx, by, bz);
        aDir.push(_qT.x, _qT.y, _qT.z);
        aCol.push(br, bg, bb);
        aBox.push(bx, by, bz);
        // issue #6 ride: delivery leg = this arc (from -> box,
        // liftFrac bulge) so a buried box can slide its mark down
        // the wire to the last visible sample
        aLeg.push(ax, ay, az, liftFrac);
      }
      px = x; py = y; pz = z; pd = dd;
    }
  };
  // file-pair trunks: surface-to-surface arc between each pair's fn-box
  // centroids, colored by the DESTINATION file (the bus feeds that file)
  const fnCen = new Map(), fnCnt = new Map();
  for (let i = 0; i < fnMeta.length; i++) {
    const fi = fnMeta[i].file;
    const c = fnCen.get(fi) || [0, 0, 0];
    c[0] += fpos[i*3]; c[1] += fpos[i*3+1]; c[2] += fpos[i*3+2];
    fnCen.set(fi, c);
    fnCnt.set(fi, (fnCnt.get(fi) || 0) + 1);
  }
  const surf = (fi, c) => {
    const dx = c[0] - pos[fi*3], dy = c[1] - pos[fi*3+1], dz = c[2] - pos[fi*3+2];
    const l = Math.hypot(dx, dy, dz) || 1;
    const r = sphR(fi);
    return [pos[fi*3] + dx / l * r, pos[fi*3+1] + dy / l * r, pos[fi*3+2] + dz / l * r];
  };
  const trunkEnds = new Map();  // reroute junctions: {e, x, c, m: trunk meta}
  const busSegs = [];   // {a:[x,y,z], b:[x,y,z], col:[r,g,b], k} conduit pieces
  const busJunc = [];   // junction bollards: {p:[x,y,z], c:[r,g,b]}
  trunkMetaMap = new Map();
  const jcons = [];     // junction constraints: {p, kind:"box", b, r}
  const trunkGeom = []; // deferred trunk emission (after junction separation)
  fnTrunkN = 0;
  fnTrunkW = 0;
  fnJstubN = 0;
  fnQuietTrunkN = 0;
  fnQuietTrunkW = 0;
  fnLegN = 0;
  fnJclip = 0;
  const corridorTier = new Map();   // "srcId>tgtId" -> 0..2 (first-seen order)
  const corridorOrd = new Map();    // "srcId>tgtId" -> trunk count so far
  // fan trunk termini out of shared stations: corridors leaving/arriving at
  // one bollard offset along the station tangent (ranked by the OTHER end's
  // bearing) so coincident first/last conduit segments separate instead of
  // stacking at 0 wu; trunks still read as leaving the dot. Sector stations
  // of one bollard CLUSTER are grouped by proximity (60 wu) — object
  // identity split the visual fan and left whole clusters unranked.
  const stTermR = new Map();  // "k:end" -> fan rank (0..n-1, by other-end bearing)
  const claimed = new Set();  // stations already ranked via an earlier cluster rep
  for (const S of stList) {
    if (claimed.has(S)) continue;
    const terms = [];
    for (const S2 of stList) {
      if (Math.hypot(S2.p[0]-S.p[0], S2.p[1]-S.p[1], S2.p[2]-S.p[2]) > 60) continue;
      claimed.add(S2);
      for (const k of trunked) {
        const stB = stations.get(k);
        if (!stB) continue;
        if (stB[0] === S2) terms.push({ k, end: 0, other: stB[1].p });
        if (stB[1] === S2) terms.push({ k, end: 1, other: stB[0].p });
      }
    }
    if (terms.length < 2) continue;
    terms.sort((a, b) => brgOf(S.fi, a.other) - brgOf(S.fi, b.other));
    // ZERO-GAP ATTACHMENT (user sighting #12): trunk termini land exactly
    // ON the station bollard (S.p) — no tangent-line fan slots. Ranking is
    // retained only as fanR, which terraces APEX HEIGHTS so stacked trunks
    // still read over/under instead of braiding at the shared dot.
    terms.forEach((t, r) => { stTermR.set(t.k + ":" + t.end, r); });
  }
  for (const k of trunked) {
    const parts = k.split(">");
    const sf = +parts[0], tf = +parts[1];
    const c0 = fnCen.get(sf), c1 = fnCen.get(tf);
    if (!c0 || !c1) continue;
    const stBoth = stations.get(k);
    const stS = stBoth && stBoth[0], stT = stBoth && stBoth[1];
    if (!stS || !stT) continue;
    // entry/exit = the two files' stations (shared per sector — ONE bollard
    // per sector, not one per pair). Delivery legs run station/subJ -> box
    // per wire; single-destination trunks need no special stop.
    // ZERO-GAP (user sighting #12): trunk termini land exactly ON the
    // station bollard — the vertex IS the marker position, no fan slots.
    const p0 = stS.p, p1 = stT.p;
    // trunk color = DESTINATION FILE's cluster color (colArr is per-file;
    // fcol is per-fn-box — indexing it by file reads garbage => black tubes)
    const tc = [colArr[tf*3], colArr[tf*3+1], colArr[tf*3+2]];
    const tmeta = { kind: "trunk", k, sf, tf, mates: [] };
    trunkMetaMap.set(k, tmeta);
    const ck = stS.id + ">" + stT.id;
    if (!corridorTier.has(ck)) corridorTier.set(ck, corridorTier.size % 3);
    // depth-banding (skeptic B5/R5): trunks of ONE corridor fly at distinct
    // heights — corridor seed + per-trunk ordinal over 6 tiers
    const ord = corridorOrd.get(ck) || 0; corridorOrd.set(ck, ord + 1);
    // apex terrace (declutter R1, sighting-#12 revision): sibling corridors
    // sharing a bollard descend in fan-rank order via APEX height only —
    // landings stay exactly on the dot, so ranks read over/under instead
    // of braiding at a shared landing slot
    const fanR = (stTermR.get(k + ":0") || 0) + (stTermR.get(k + ":1") || 0);
    trunkGeom.push({ p0, p1, tc, tmeta, tier: (corridorTier.get(ck) + ord) % 6, fanR });
    trunkEnds.set(k, { e: p0, x: p1, c: tc, m: tmeta });
  }
  // station bollards: a DISJOINT ivory family (S<=0.12, L>=0.84) — cluster
  // hues fill the wheel densely (golden-ratio over 29 clusters), so hue
  // disjointness is impossible; saturation+lightness distance is provable:
  // fn boxes sit at S=0.72, L<=0.70. Bollards are the only near-white
  // elements, so a merge dot is identifiable BY COLOR ALONE at any zoom
  // (skeptic round-3 pre-ruling 3.ii; assert after the instancing below).
  const BOL_COL = [ new THREE.Color().setHSL(0.10, 0.12, 0.90),   // station
                    new THREE.Color().setHSL(0.12, 0.07, 0.87),   // sub-junction
                    new THREE.Color().setHSL(0.12, 0.05, 0.84) ]; // Jof
  for (const S of stList) {
    busJunc.push({ p: S.p, c: [BOL_COL[0].r, BOL_COL[0].g, BOL_COL[0].b], k: 1, of: S.fi, st: 1,
      nl: S.subJ.length, hd: 1, tg: [0, -1, 0],   // dodge axis: down the leg fan
      info: { kind: "station", fi: S.fi, wires: S.wires, trks: S.tks.length } });
    for (let li = 0; li < S.subJ.length; li++) {
      const sp = S.subJ[li];
      // dodge axis (issue #5): along THIS leg's own ink, toward the station
      const dl = Math.hypot(S.p[0]-sp[0], S.p[1]-sp[1], S.p[2]-sp[2]) || 1;
      busJunc.push({ p: sp, c: [BOL_COL[1].r, BOL_COL[1].g, BOL_COL[1].b], k: 0.72, of: S.fi, st: 1,
        nl: 1, lk: "L|" + S.fi + "|" + S.id + "|" + li,
        tg: [(S.p[0]-sp[0])/dl, (S.p[1]-sp[1])/dl, (S.p[2]-sp[2])/dl],
        info: { kind: "sub", fi: S.fi, wires: S.wires } });
      // legs land on a tangent line 5 wu BELOW the station — the empty
      // lane under the horizontal trunk fan (trunks bow +Y from termini
      // on the mid line), slotted wide of them: >=9 horizontal + >=5
      // vertical clearance by construction
      const tx = -Math.sin(S.brg), tz = Math.cos(S.brg);
      const off = (li - (S.subJ.length - 1) / 2) * 18;
      const ep = [S.p[0] + tx * off, S.p[1] - 5, S.p[2] + tz * off];
      const dist = qSpan(sp[0], sp[1], sp[2], ep[0], ep[1], ep[2]);
      // Apex 0.24*dist keeps the lane law (>= 0.10*dist); endpoint
      // clearance owns near-box termini, the screen park law the ink.
      // #299 D: same q-leaves the trunk arcs and riders use — one
      // +Y-lift law, one tangent math, no per-site restatement to drift.
      const m = qMid(sp[0], sp[1], sp[2], ep[0], ep[1], ep[2], 0.24 * dist);
      let lx = sp[0], ly = sp[1], lz = sp[2];
      for (let s = 1; s <= FS; s++) {
        const q = qPt(sp[0], sp[1], sp[2], m[0], m[1], m[2], ep[0], ep[1], ep[2], s / FS);
        const x = q.x, y = q.y, z = q.z;
        busSegs.push({ a: [lx, ly, lz], b: [x, y, z],
                       col: [BOL_COL[1].r, BOL_COL[1].g, BOL_COL[1].b],
                       k: "L|" + S.fi + "|" + S.id + "|" + li, rf: 0.55 });
        lx = x; ly = y; lz = z;
      }
      // DRAWS-TO-ITS-ANCHOR (user sighting #8 ruling): the leg's terminus
      // IS the station bollard — the tangent-offset ep was a visual seam
      // the leads-home clause legalized as float (up to 262px at close
      // zoom). One closing segment lands the ink on the dot itself; it
      // shares k so the whole chain culls/serves atomically and picks as
      // a jleg. The junction end already lands on its subJ bollard (sp).
      busSegs.push({ a: [lx, ly, lz], b: [S.p[0], S.p[1], S.p[2]],
                     col: [BOL_COL[1].r, BOL_COL[1].g, BOL_COL[1].b],
                     k: "L|" + S.fi + "|" + S.id + "|" + li, rf: 0.55 });
      fnLegN++;
    }
  }
  // quiet-tier trunks: >= QUIET_TRUNK_MIN neighbor↔neighbor wires between
  // the same file pair collapse into ONE background arc between the
  // surfaced fn-centroid points — the bright-trunk law mirrored down a
  // tier. No junction-separation pass: background ink, build cost flat.
  const qTrunkGeo = new Map();   // tk -> {e, x, c, tm} for member taps
  for (const k of qTrunked) {
    const parts = k.split(">");
    const sf = +parts[0], tf = +parts[1];
    const c0 = fnCen.get(sf), c1 = fnCen.get(tf);
    if (!c0 || !c1) continue;
    const p0 = surf(sf, [c0[0]/fnCnt.get(sf), c0[1]/fnCnt.get(sf), c0[2]/fnCnt.get(sf)]);
    const p1 = surf(tf, [c1[0]/fnCnt.get(tf), c1[1]/fnCnt.get(tf), c1[2]/fnCnt.get(tf)]);
    const tc = [colArr[tf*3], colArr[tf*3+1], colArr[tf*3+2]];
    const tm = { kind: "trunk", k, sf, tf, mates: [] };
    trunkMetaMap.set(k, tm);
    emitArc(tierQ, p0[0], p0[1], p0[2], p1[0], p1[1], p1[2],
            tc[0], tc[1], tc[2], tc[0], tc[1], tc[2], 0,
            QUIET_LIFT_FRAC, false, tm);
    fnQuietTrunkN++;
    qTrunkGeo.set(k, { e: p0, x: p1, c: tc, tm });
  }
  // junction separation: 3 deterministic depenetration rounds — box-anchored
  // junctions (fn stops + Jof moat dots) closer than 6 spread apart
  // tangentially around their fn, then re-anchor at their own radius
  for (const [b, J] of Jof) {
    // bollard instancing happens AFTER the separation rounds + merge pass
    // (this push only registers the dot for the rounds; J mutates in place)
    jcons.push({ p: J, kind: "box", b, r: JofR.get(b) });
  }
  for (let round = 0; round < 3; round++) {
    for (let i = 0; i < jcons.length; i++) for (let j = i + 1; j < jcons.length; j++) {
      const jcA = jcons[i], jcB = jcons[j];
      const A = jcA.p, B = jcB.p;
      const dx = B[0] - A[0], dy = B[1] - A[1], dz = B[2] - A[2];
      const dd = Math.hypot(dx, dy, dz);
      if (dd > 6 || dd < 1e-6) continue;
      const push = 6 - dd;
      const slideBox = (jc, away) => {
        // slide a box-anchored junction around its fn ring away from `away` —
        // radial push loses to re-anchor, tangential displacement survives it
        const fb = [fpos[jc.b*3], fpos[jc.b*3+1], fpos[jc.b*3+2]];
        const P = jc.p;
        const rl = Math.hypot(P[0]-fb[0], P[1]-fb[1], P[2]-fb[2]) || 1;
        const rx = (P[0]-fb[0]) / rl, ry = (P[1]-fb[1]) / rl, rz = (P[2]-fb[2]) / rl;
        const sx = away[0]-P[0], sy = away[1]-P[1], sz = away[2]-P[2];
        const radial = sx*rx + sy*ry + sz*rz;
        const tx = sx - radial*rx, ty = sy - radial*ry, tz = sz - radial*rz;
        const tl = Math.hypot(tx, ty, tz);
        if (tl < 1e-6) return;   // head-on: next round settles it
        P[0] += tx / tl * push; P[1] += ty / tl * push; P[2] += tz / tl * push;
      };
      if (jcA.b === jcB.b) slideBox(jcB, A);   // same fn: one ring, later yields
      else { slideBox(jcB, A); slideBox(jcA, B); }   // different fns: both yield
    }
    for (const jc of jcons) {
      const fb = [fpos[jc.b*3], fpos[jc.b*3+1], fpos[jc.b*3+2]];
      const dx = jc.p[0] - fb[0], dy = jc.p[1] - fb[1], dz = jc.p[2] - fb[2];
      const l = Math.hypot(dx, dy, dz) || 1;
      const r = jc.r || 14;
      jc.p[0] = fb[0] + dx / l * r;
      jc.p[1] = fb[1] + dy / l * r;
      jc.p[2] = fb[2] + dz / l * r;
    }
  }
  // Jof merge pass + Y-ladder (skeptic R3 real-pairs): same-depth moat dots
  // within 22 wu collapse onto ONE shared reroute point (bollards are file-
  // neutral ivory now, so one dot serving several stubs tells no lie), and
  // the survivors get a deterministic +7wu-per-index Y ladder so in-plane
  // separations foreshortened by the d2 camera still split on screen.
  {
    const jofs = [...Jof.entries()].sort((A, B) => A[0] - B[0]);
    for (let i = 0; i < jofs.length; i++) {
      if (!Jof.has(jofs[i][0])) continue;
      for (let j = i + 1; j < jofs.length; j++) {
        if (!Jof.has(jofs[j][0]) || jofs[j][1] === jofs[i][1]) continue;
        const A = jofs[i][1], B = jofs[j][1];
        if (Math.hypot(A[0]-B[0], A[1]-B[1], A[2]-B[2]) < 26) {
          A[0] = (A[0]+B[0])/2; A[1] = (A[1]+B[1])/2; A[2] = (A[2]+B[2])/2;
          Jof.set(jofs[j][0], A);   // share the array: fans + stubs follow
        }
      }
    }
    // snap pass: a Jof dot within 18 wu of a station/subJ tree dot SHARES
    // that dot (sub|jof pairs at 12 wu projected 4.8px at d2 — two ivory
    // bus dots that close read as one smear). Tree points are skipped by
    // the Y-ladder and the instancing loop — they are already bollards.
    const treePts = [];
    for (const S of stList) { treePts.push(S.p); for (const q of S.subJ) treePts.push(q); }
    const isTree = J2 => treePts.some(q => q === J2);
    for (const [b, J] of Jof) {
      if (isTree(J)) continue;
      for (const S of stList) {
        const near = [S.p, ...S.subJ].find(q =>
          Math.hypot(q[0]-J[0], q[1]-J[1], q[2]-J[2]) < 18);
        if (near) { Jof.set(b, near); break; }
      }
    }
    const seen = new Set(); let ord = 0;
    for (const [b, J] of Jof) {   // map iteration: merged points share refs
      if (isTree(J)) continue;
      const kk = J[0].toFixed(2) + "," + J[1].toFixed(2) + "," + J[2].toFixed(2);
      if (!seen.has(kk)) { seen.add(kk); J[1] += 7 * (ord % 3); ord++; }
    }
    // ISSUE #5, Jof tail: separation + merge can drift a dot back toward
    // box ink (re-anchor restores the ring radius; the merge midpoint
    // averages two cleared dots toward each other's boxes). Slide the
    // survivors in XZ, in place, away from every box inside 14 wu —
    // shared arrays move as one dot; tree points are already-cleared
    // bollards and stay put.
    const jofSeen = new Set();
    for (const J of Jof.values()) {
      if (isTree(J)) continue;
      const kk = J[0].toFixed(2) + "," + J[2].toFixed(2);
      if (jofSeen.has(kk)) continue;
      jofSeen.add(kk);
      for (let it = 0; it < 6; it++) {
        let vx = 0, vz = 0, bad = false;
        for (const ix of allBoxes) {
          const dx = J[0] - fpos[ix*3], dz = J[2] - fpos[ix*3+2];
          if (Math.hypot(dx, J[1] - fpos[ix*3+1], dz) < 14) {
            bad = true;
            const l = Math.hypot(dx, dz) || 1;
            vx += dx / l; vz += dz / l;
          }
        }
        if (!bad) break;
        const vl = Math.hypot(vx, vz) || 1;
        J[0] += vx / vl * 4; J[2] += vz / vl * 4;
      }
    }
    // instancing: one bollard per DISTINCT merged point
    const done = new Set();
    for (const [b, J] of Jof) {
      if (isTree(J)) continue;
      const kk = J[0].toFixed(2) + "," + J[1].toFixed(2) + "," + J[2].toFixed(2);
      if (done.has(kk)) continue;
      done.add(kk);
      // dodge axis (issue #5): along the delivery stub's own ink, toward
      // its box — the dot slides down ink it already sits on
      const gl = Math.hypot(fpos[b*3]-J[0], fpos[b*3+1]-J[1], fpos[b*3+2]-J[2]) || 1;
      busJunc.push({ p: [J[0], J[1], J[2]], c: [BOL_COL[2].r, BOL_COL[2].g, BOL_COL[2].b], k: 0.72, of: fnMeta[b].file, st: 0,
        tg: [(fpos[b*3]-J[0])/gl, (fpos[b*3+1]-J[1])/gl, (fpos[b*3+2]-J[2])/gl],
        info: { kind: "jof", fi: fnMeta[b].file } });
    }
  }
  // obstacle-aware conduit lift (spec D5): raise the control point so the
  // arc clears foreign swarm hulls that straddle the chord. Closed form:
  // height(t) = base(t) + 2t(1-t)*lift >= obstacle top over the crossing
  // interval. Cap keeps masts sane; capped trunks are counted in fnJclip.
  const litFiles = [];
  {
    const seen = new Set();
    for (const e of visEdges) {
      if (!seen.has(e[0])) { seen.add(e[0]); litFiles.push(e[0]); }
      if (!seen.has(e[2])) { seen.add(e[2]); litFiles.push(e[2]); }
    }
  }
  const obsLift = (p0, p1, sf, tf) => {
    const dx = p1[0] - p0[0], dz = p1[2] - p0[2];
    const A2 = dx*dx + dz*dz;
    if (A2 < 1e-9) return 0;
    let need = 0;
    for (const fi of litFiles) {
      if (fi === sf || fi === tf) continue;
      const sr = sphR(fi), ro = ringOut.get(fi);
      const hull = ro !== undefined ? ro : sr;
      for (const [R, top] of [[sr, pos[fi*3+1] + sr + 3], [hull + 5, pos[fi*3+1] + 6]]) {
        const fx = p0[0] - pos[fi*3], fz = p0[2] - pos[fi*3+2];
        const B = 2*(fx*dx + fz*dz), C = fx*fx + fz*fz - R*R;
        const disc = B*B - 4*A2*C;
        if (disc <= 0) continue;
        const sq = Math.sqrt(disc);
        const t1 = Math.max(0.12, (-B - sq) / (2*A2)), t2 = Math.min(0.88, (-B + sq) / (2*A2));
        if (t2 <= t1) continue;
        for (const t of [t1, t2, (t1 + t2) / 2]) {
          const base = p0[1] + (p1[1] - p0[1]) * t;
          const n = (top + 4 - base) / (2*t*(1 - t));
          if (n > need) need = n;
      }
    }
    }
    return need;
  };
  // phase 2: trunk geometry, emitted AFTER separation so arcs, conduits and
  // the shared reroute ends all read the final junction positions
  for (const g of trunkGeom) {
    const p0 = g.p0, p1 = g.p1, tc = g.tc, tmeta = g.tmeta;
    // 1px lines reads as nothing. Tier by corridor (same station pair =>
    // same corridor), raised by the obstacle law, then TERRACED down by fan
    // rank (fanR, declutter R1) so siblings sharing a bollard read as a
    // staircase instead of interleaved hills; apex stays >= 0.11*dist and
    // ALWAYS +Y (conduit lane law). The guide arc rides the SAME lift as
    // the tube — one path, two inks.
    const dist = Math.hypot(p1[0]-p0[0], p1[1]-p0[1], p1[2]-p0[2]) || 1;
    const Ltier = (CONDUIT_LIFT_BASE + 0.03 * g.tier) * dist;
    // ISSUE #7: swarm hulls (obsLift) for the tall silhouettes. fn-box ink
    // is owned by the screen-side park law in the tick; a world-side
    // polyline floor (arcDodge) was tried and deleted — every variant
    // (lift add, lateral bulge) reshuffled fan curvature for +1..2
    // crossTT over base. Cap 0.70*dist; the 0.80 experiment cleared
    // boxes but braided the fan (crossTT 59->61).
    const cap = 0.70 * dist;
    const lift = Math.min(cap, Math.max(0.11 * dist,
                    Math.max(Ltier, obsLift(p0, p1, tmeta.sf, tmeta.tf)) - 8 * (g.fanR || 0)));
    emitArc(tierB, p0[0], p0[1], p0[2], p1[0], p1[1], p1[2],
            tc[0], tc[1], tc[2], tc[0], tc[1], tc[2], 0, lift / dist, false,
            tmeta);
    fnTrunkN++;
    // #299 D: the bollard-dodge control polyline rides the SAME bezier
    // law the ink arc above it emits — one curve, two consumers, zero
    // drift between what renders and what the dodge clears against.
    const qc = qMid(p0[0], p0[1], p0[2], p1[0], p1[1], p1[2], lift);
    let bx2 = p0[0], by2 = p0[1], bz2 = p0[2];
    for (let s = 1; s <= FS; s++) {
      const q = qPt(p0[0], p0[1], p0[2], qc[0], qc[1], qc[2], p1[0], p1[1], p1[2], s / FS);
      const x = q.x, y = q.y, z = q.z;
      busSegs.push({ a: [bx2, by2, bz2], b: [x, y, z], col: tc, k: tmeta.k });
      bx2 = x; by2 = y; bz2 = z;
    }
  }
  const Jstub = new Set();   // fns already carrying a junction delivery stub
  const tgtArrow = new Set();   // delivery arrows: ONE per target box (R4)
  // ONE delivery chevron per RENDERED box, all paths. Key must be the
  // RENDERED position, not the original box index: collapsed members remap
  // to their file's aggregate box, and keying on `b` stacked five chevrons
  // on one projected point (pair-engineer census, round 5b)
  const boxKey = b => (fnMeta[b].agg && !fnMeta[b].count) ? "agg" + fnMeta[b].file : "b" + b;
  const boxArrow = new Set();
  for (let i = 0, p = 0; i < eidx.length; i += 2, p++) {
    const a = eidx[i], b = eidx[i+1];
    const T = wireTier[p] ? tierB : tierQ;
    // collapsed members (scale-0) land on their file's VISIBLE aggregate
    // box — deliveries must read at a rendered box, not open air
    const ap = fnMeta[a].agg && !fnMeta[a].count ? aggP.get(fnMeta[a].file) : null;
    const bp = fnMeta[b].agg && !fnMeta[b].count ? aggP.get(fnMeta[b].file) : null;
    const ax = ap ? ap[0] : fpos[a*3], ay = ap ? ap[1] : fpos[a*3+1], az = ap ? ap[2] : fpos[a*3+2];
    const bx = bp ? bp[0] : fpos[b*3], by = bp ? bp[1] : fpos[b*3+1], bz = bp ? bp[2] : fpos[b*3+2];
    cA.setRGB(fcol[a*3], fcol[a*3+1], fcol[a*3+2]);
    cB.setRGB(fcol[b*3], fcol[b*3+1], fcol[b*3+2]);
    const phase = ((i + 1) * 2654435761 >>> 3) % 911 / 911 * 13;
    const ln = wireRows[p] ? wireRows[p][4] : -1;   // source line for the tip
    let done = false;
    if (T === tierB) {
      const tk = fnMeta[a].file + ">" + fnMeta[b].file;
      const ends = trunked.has(tk) ? trunkEnds.get(tk) : null;
      if (ends) {
        // Blueprint reroute node: the wire leaves its fn, merges at its
        // sector sub-junction (or the station), rides the trunk, and
        // arrives via the exit-side fan — every wire of the pair at the
        // same end. Taps dim to the trunk hue (ink#1) and carry no arrow
        // (ink#2 — direction lives on the ONE trunk tip).
        done = true;
        fnTrunkW++;
        const wmeta = { kind: "wire", a, b, ln, tk };
        const dc = [ends.c[0]*0.38, ends.c[1]*0.38, ends.c[2]*0.38];
        const stBoth = stations.get(tk);
        ends.m.mates.push(wmeta);
        // ZERO-GAP ATTACHMENT (user sighting #12): rider ink lands exactly
        // ON the merge point — the sub-junction/station dot. The old 14wu
        // moat (gap handoff) left visible gaps at every junction; the dot
        // itself is the merge, so the arc ends on it.
        const e0 = stBoth && stBoth[0] ? (stBoth[0].boxSub.get("0:" + a) || stBoth[0].p) : ends.e;
        emitArc(T, ax, ay, az, e0[0], e0[1], e0[2],
                dc[0], dc[1], dc[2], dc[0], dc[1], dc[2], phase, 0.16, false, wmeta);
        // exit leg: delivery fan leaves from the target box's sub-junction
        // (or the in-station directly on quiet merges), also landing exactly
        // on the dot. Emitted BOX-FIRST (like the entry tap) so both ramp
        // kinds read as leaving the box and joining the merge.
        const e1 = stBoth && stBoth[1] ? (stBoth[1].boxSub.get("1:" + b) || stBoth[1].p) : ends.x;
        emitArc(T, bx, by, bz, e1[0], e1[1], e1[2],
                cB.r, cB.g, cB.b, dc[0], dc[1], dc[2], phase + 2.5, 0.16, false, wmeta);
        // DELIVERY chevron (skeptic r4 #2): one per trunked TARGET BOX, at
        // its face aimed inward — not at the station terminus, where the
        // 11px merge disk swallowed every trunk-tip arrow (measured 0
        // amber px under the disk)
        if (!boxArrow.has(boxKey(b))) {
          boxArrow.add(boxKey(b));
          let ddx = bx-e1[0], ddy = by-e1[1], ddz = bz-e1[2];
          const dl = Math.hypot(ddx, ddy, ddz) || 1;
          ddx /= dl; ddy /= dl; ddz /= dl;
          aPos.push(bx - ddx*3.5, by - ddy*3.5, bz - ddz*3.5);
          aDir.push(ddx, ddy, ddz);
          aCol.push(cB.r, cB.g, cB.b);
          aBox.push(bx, by, bz);
          aLeg.push(e1[0], e1[1], e1[2], 0.16);   // issue #6 ride: exit leg
        }
      } else {
        const J = Jof.get(b);
        if (J) {
          // reroute junction: wires merge AT the moat junction, then ONE
          // shared stub delivers into the fn box — the junction must not
          // be a dead end in open space ("supposed to go into
          // a cross-system call). Fan dims to the target hue (ink#1).
          const d6 = [cB.r*0.38, cB.g*0.38, cB.b*0.38];
          emitArc(T, ax, ay, az, J[0], J[1], J[2],
                  d6[0], d6[1], d6[2], d6[0], d6[1], d6[2], phase, 0.16, false,
                  { kind: "wire", a, b, ln });
          if (!Jstub.has(b)) {
            Jstub.add(b);
            fnJstubN++;
            const jArr = !boxArrow.has(boxKey(b)); boxArrow.add(boxKey(b));
            emitArc(T, J[0], J[1], J[2], bx, by, bz,
                    cB.r, cB.g, cB.b, cB.r, cB.g, cB.b, phase, 0.16, jArr,
                    { kind: "wire", a, b, ln });
          }
          done = true;
        }
      }
    }
    if (!done && T === tierQ) {
      const qtk = fnMeta[a].file + ">" + fnMeta[b].file;
      const qt = qTrunkGeo.get(qtk);
      if (qt) {
        // absorbed by the quiet trunk: entry tap fn -> trunk head, exit
        // tap trunk tail -> fn (bright-trunk tap pattern, minus arrows —
        // the arrow overlay is tier-blind and quiet ink stays silent)
        done = true;
        fnQuietTrunkW++;
        const wmeta = { kind: "wire", a, b, ln, tk: qtk };
        qt.tm.mates.push(wmeta);
        emitArc(T, ax, ay, az, qt.e[0], qt.e[1], qt.e[2],
                cA.r, cA.g, cA.b, qt.c[0], qt.c[1], qt.c[2],
                phase, 0.10, false, wmeta);
        emitArc(T, qt.x[0], qt.x[1], qt.x[2], bx, by, bz,
                qt.c[0], qt.c[1], qt.c[2], cB.r, cB.g, cB.b,
                phase + 2.5, 0.10, false, wmeta);
      }
    }
    if (!done) {
      // direct wire: one delivery arrow per TARGET box — parallel wires into
      // the same fn share the direction cue (skeptic R4: 31 -> ~18 arrows)
      const arr = T === tierB && !boxArrow.has(boxKey(b));
      if (T === tierB) boxArrow.add(boxKey(b));
      emitArc(T, ax, ay, az, bx, by, bz,
              cA.r, cA.g, cA.b, cB.r, cB.g, cB.b, phase,
              0.08 + 0.10 * (((i + 1) * 2654435761 >>> 0) % 97) / 97,
              arr, { kind: "wire", a, b, ln });
    }
  }
  const makeWires = (T, op) => {
    // fat lines: WebGL ignores linewidth on classic LineSegments — Line2
    // renders real screen-space width (default wires read at 2px).
    // #299 D: mkFatLines owns construction + the resize registry; dashes
    // still need per-rebuild distances, so they stay the caller's call.
    const ls = mkFatLines(new Float32Array(T.ep), new Float32Array(T.ec),
      { opacity: op, linewidth: 2, dashed: true, dashSize: 7, gapSize: 4 });
    ls.computeLineDistances();
    return ls;
  };
  fnLines = makeWires(tierB, 0.75);
  if (tierQ.ep.length) fnQuiet = makeWires(tierQ, 0.16);
  fnLines.userData.meta = tierB.meta;   // wire pick descriptions
  if (fnQuiet) fnQuiet.userData.meta = tierQ.meta;
  if (busSegs.length) {
    const cyl = new THREE.CylinderGeometry(1, 1, 1, 6);
    fnBus = new THREE.InstancedMesh(cyl, new THREE.MeshBasicMaterial({
      transparent: true, opacity: 1.0 }), busSegs.length);
    const M = new THREE.Matrix4(), Q = new THREE.Quaternion(),
          V = new THREE.Vector3(), UP = new THREE.Vector3(0, 1, 0),
          S1 = new THREE.Vector3(), C = new THREE.Color();
    busSegs.forEach((s, k) => {
      const ax = new THREE.Vector3(...s.a), bx = new THREE.Vector3(...s.b);
      const d = bx.clone().sub(ax), len = d.length() || 1;
      Q.setFromUnitVectors(UP, d.normalize());
      V.copy(ax).addScaledVector(d, len / 2);
      const rr = 4 * (s.rf || 1);   // legs start at 0.55x — base radius
      S1.set(rr, len, rr);   // tick rescales to keep ~4px on screen
      M.compose(V, Q, S1);
      fnBus.setMatrixAt(k, M);
      fnBus.setColorAt(k, C.setRGB(s.col[0], s.col[1], s.col[2]));
    });
    fnBusRi = new Float32Array(busSegs.length).map((_, q) => 4 * (busSegs[q].rf || 1));
    if (fnBus.instanceColor) fnBus.instanceColor.needsUpdate = true;
    fnBus.frustumCulled = false;
    busPts = busSegs;
  busPtsMeta = busSegs.map(s => String(s.k).startsWith("L|")
    ? { kind: "jleg", fi: +String(s.k).split("|")[1], k: String(s.k) }
    : (trunkMetaMap.get(s.k) || null));
    scene.add(fnBus);
  }
  // probe surfaces (extend-only __dbg contract): the station map + the
  // worst junction->box clearance, so QA can assert open-air placement
  fnStationsArr = stList.map(S => ({ fi: S.fi, p: S.p, id: S.id,
    brg: S.brg, wires: S.wires, tks: S.tks.slice(), subJ: S.subJ.map(p => [p[0], p[1], p[2]]) }));
  fnJclearV = -1;
  if (busJunc.length) {
    let jmin = Infinity;
    for (const j of busJunc) for (let i = 0; i < fnMeta.length; i++) {
      const m = fnMeta[i];
      if (m.agg && !m.count) continue;
      const dd = Math.hypot(j.p[0]-m.p[0], j.p[1]-m.p[1], j.p[2]-m.p[2]);
      if (dd < jmin) jmin = dd;
    }
    if (isFinite(jmin)) fnJclearV = jmin;
  }
  // per-junction size factors (screen-constant bollard law in the tick).
  // #299 D: the whole bollard family builds into ONE record — the eight
  // parallel fnJDot* vars are fields on it, so teardown is a single null
  // in layerAssets, not eight.
  stationTks = null; fnBoxScale = null; arrowFile = null; _lod = null;
  juncPickInfo = busJunc.map(j => j.info || null);
  stationTks = new Map();
  for (const S of stList) {
    let tks = stationTks.get(S.fi);
    if (!tks) stationTks.set(S.fi, tks = []);
    for (const tk of S.tks) tks.push(tk);
  }
  fnBoxScale = new Map();
  for (const m of fnMeta) {
    if (m.agg && !m.count) continue;
    const sc = m.count ? 6 : 4;
    if (sc > (fnBoxScale.get(m.file) || 0)) fnBoxScale.set(m.file, sc);
  }
  if (busJunc.length) {
    // reroute bollards: the junction must be a THING — a dot where the fan
    // merges and where the bus delivers onto the fn box (Blueprint reroute)
    const jg = new THREE.SphereGeometry(3, 8, 6);
    const jm = new THREE.InstancedMesh(jg, new THREE.MeshBasicMaterial({
      transparent: true, opacity: 0.9, depthWrite: false }), busJunc.length);
    jm.renderOrder = 3;   // junctions sit ON TOP of the wire tangle
    jm.material.depthTest = false;  // occlusion may never fully hide a dot (pre-ruling 3.iii)
    const M = new THREE.Matrix4(), C = new THREE.Color();
    const pos = new Float32Array(busJunc.length * 3);
    const r = new Float32Array(busJunc.length).fill(2.6);
    const tg = new Float32Array(busJunc.length * 3);
    busJunc.forEach((j, k) => {
      M.makeTranslation(j.p[0], j.p[1], j.p[2]);
      jm.setMatrixAt(k, M);
      pos[k*3] = j.p[0]; pos[k*3+1] = j.p[1]; pos[k*3+2] = j.p[2];
      const t = j.tg || [0, -1, 0];
      tg[k*3] = t[0]; tg[k*3+1] = t[1]; tg[k*3+2] = t[2];
      jm.setColorAt(k, C.setRGB(j.c[0], j.c[1], j.c[2]));
    });
    // disjointness assert (skeptic pre-ruling 3.ii): every bollard is
    // near-white (S<=0.15, L>=0.80) while every RENDERED fn box carries a
    // cluster color at S=0.72, L<=0.70 — bollards are identifiable by
    // color alone; a violation means the family drifted
    const hsl = { h: 0, s: 0, l: 0 };
    for (const j of busJunc) {
      C.setRGB(j.c[0], j.c[1], j.c[2]).getHSL(hsl);
      if (hsl.s > 0.15 || hsl.l < 0.80) console.error("[bus] bollard family drift", hsl);
    }
    jm.instanceMatrix.needsUpdate = true;
    if (jm.instanceColor) jm.instanceColor.needsUpdate = true;
    jm.frustumCulled = false;
    scene.add(jm);
    jdot = {
      mesh: jm,   // reroute junction bollards (InstancedMesh spheres)
      pos,        // world positions
      r,          // radii (screen-constant law)
      tg,         // dodge axes (issue #5)
      of: Int32Array.from(busJunc, j => j.of | 0),   // owner FILE index (round-5 LOD gate)
      st: Uint8Array.from(busJunc, j => j.st | 0),   // 1 = station-class gate
      legs: Int32Array.from(busJunc, j => j.nl | 0), // legs per STATION (sighting #9 gate)
      key: busJunc.map(j => j.hd ? "T|" + (j.of | 0) : (j.lk || null)),  // attachment key: a bollard renders only when its attachment SERVED this frame
      k: Float32Array.from(busJunc, j => j.k || 1),  // per-junction size factor (screen-constant law)
    };
  }
  if (aPos.length) {
    // 2D SCREEN-SPACE CHEVRON (skeptic r4): 3D cones render as round dots
    // edge-on at 5px; a flat chevron billboarded to the camera with its
    // local +Y aimed along the wire's projected tangent reads as
    // DIRECTION at any camera. Amber family: S 0.92 / L 0.55 is disjoint
    // from the ivory bollards (S<=0.12, L>=0.84) AND the golden-ratio box
    // family (S=0.72, L<=0.70) on BOTH axes — arrows are the only warm
    // saturated mid-light element in the layer.
    // CHUNKY chevron: the r4 shape (arms 0.30 wide) painted only ~5px of
    // saturated amber inside its 12px vertex span at the USER's window —
    // the V read as a small dot (pair-engineer pixel census). Fat arms +
    // a short notch keep the V legible at any viewport
    const chev = new THREE.Shape();
    chev.moveTo(-0.72, -0.42); chev.lineTo(0, 0.62); chev.lineTo(0.72, -0.42);
    chev.lineTo(0.30, -0.42); chev.lineTo(0, 0.10); chev.lineTo(-0.30, -0.42);
    chev.closePath();
    const arrowGeo = new THREE.ShapeGeometry(chev);
    // fog:false — scene fog blended the chevron toward the dark fog color
    // at hub distance (isolated-pixel census: brightest core only
    // (200,162,105), a ~0.6-opacity ghost). toneMapped is left at the
    // default: the renderer never sets a toneMapping (r160 defaults to
    // NoToneMapping), so the MeshBasicMaterial flag would be a no-op.
    const arrowMat = new THREE.MeshBasicMaterial({ transparent: true,
      opacity: 1.0, depthWrite: false, side: THREE.DoubleSide,
      fog: false });
    fnArrows = new THREE.InstancedMesh(arrowGeo, arrowMat, aPos.length / 3);
    fnArrows.frustumCulled = false;
    fnArrows.renderOrder = 4;   // above tubes AND bollards: cone tips sit at
    fnArrows.material.depthTest = false;  // termini, half-inside tube volumes
    const C = new THREE.Color().setHSL(0.085, 0.92, 0.60);   // amber chevron
    for (let k = 0; k < aPos.length / 3; k++) fnArrows.setColorAt(k, C);
    fnArrowTang = new Float32Array(aDir);
    fnArrowBox = new Float32Array(aBox.length ? aBox : aPos);
    // round-5 LOD: owning FILE per chevron (nearest rendered box — aBox
    // may carry the remapped aggregate position)
    arrowFile = new Int32Array(aPos.length / 3);
    const rBoxes = [];
    for (const m of fnMeta) if (!(m.agg && !m.count)) rBoxes.push(m);
    for (let ai = 0; ai < arrowFile.length; ai++) {
      let best = -1, bd = Infinity;
      for (let bi = 0; bi < rBoxes.length; bi++) {
        const dd = Math.hypot(rBoxes[bi].p[0]-fnArrowBox[ai*3],
                              rBoxes[bi].p[1]-fnArrowBox[ai*3+1],
                              rBoxes[bi].p[2]-fnArrowBox[ai*3+2]);
        if (dd < bd) { bd = dd; best = bi; }
      }
      arrowFile[ai] = best >= 0 ? rBoxes[best].file : -1;
    }
    // matrices come from aimArrows() (billboard basis); build-time call so
    // the first painted frame is already correct
    aimArrows();
    fnArrows.instanceMatrix.needsUpdate = true;
    if (fnArrows.instanceColor) fnArrows.instanceColor.needsUpdate = true;
    scene.add(fnArrows);
    fnArrowPos = new Float32Array(aPos);
    fnArrowR = new Float32Array(aPos.length / 3).fill(1);   // half-height wu
    // issue #6: delivery-leg params per arrow [from(3), liftFrac] --
    // same order as aPos; rebuilt with the bus every focus
    fnArrowLeg = new Float32Array(aPos.length / 3 * 4);
    fnArrowLeg.set(aLeg.slice(0, aPos.length / 3 * 4));
    // issue #6 sketch: dark halo disc behind each chevron -- amber
    // chevrons ride amber wires and vanish into them (VLM: 'gold-on-
    // gold'); the disc restores figure-ground at crowded sites
    fnArrowHalo = new THREE.InstancedMesh(
      new THREE.CircleGeometry(0.95, 24),
      new THREE.MeshBasicMaterial({ color: 0x0a0a10, transparent: true,
        opacity: 0.6, depthTest: false, depthWrite: false, fog: false }),
      fnArrowR.length);
    fnArrowHalo.frustumCulled = false;
    fnArrowHalo.renderOrder = 3;
    fnArrowHalo.count = 0;
    scene.add(fnArrowHalo);
  }
}

"""
