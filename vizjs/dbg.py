# vizjs/dbg.py — rung 17/17 of the template join (issues
# #86 phase-3 / #299 A): __dbg debug handle (always last: captures everything). Moved VERBATIM from viz.py —
# every edit here changes graph.html: regen the bake (the standing
# template law; test_viz refuses a bake older than viz.py/vizjs).
_JS_DBG = r"""// debug handle last: everything it captures is initialized by here
window.__dbg = { pos, nodes, links, fedges, syncEdgePos, renderer, camera, THREE, sizes, degree,
  meta: DATA.meta, controls, get spinEnabled() { return spinEnabled; }, get hubCap() { return hubCapNow; },
  fns: DATA.fns || {},
  alpha: alphaArr, alphaTgt, hoverScale, hot, bucketMat, bucketOf, hwSlot, bucketPosIB, bucketColIB, slotOf,
  adjOut, adjIn, adj, outDeg, inDeg, get dirMode() { return dirMode; }, focusSeeds, level,
  get budgetLit() { return budgetLit; }, get hoverEdgeLi() { return hoverEdgeLi; },
  get showInst() { return showInst; },   // tier state probe (issue #39)
  get fnLines() { return fnLines; }, get hubRing() { return hubRing; },
  get fnArrows() { return fnArrows; }, get compactBallR() { return compactBallR; },
  get fnQuiet() { return fnQuiet; }, get fnTrunkN() { return fnTrunkN; },
  get fnQuietTrunkN() { return fnQuietTrunkN; },
  get fnBus() { return fnBus; }, get fnBusPx() { return fnBusRi; },
  get busPts() { return busPts; }, get fnJDot() { return fnJDot; },
  get fnJDotR() { return fnJDotR; }, get fnArrowR() { return fnArrowR; },
  get camera() { return camera; },
  get fnTrunkW() { return fnTrunkW; }, get fnJstubN() { return fnJstubN; },
  get fnQuietTrunkW() { return fnQuietTrunkW; },
  get fnStations() { return fnStationsArr; },
  get fnLod() { return fnLodV ? Object.assign({}, fnLodV) : null; }, get fnJclear() { return fnJclearV; },
  get lodServe() { return _lodServe; },   // serveAll master gate (chain-integrity census)
  get compactBall() {   // #97 shape probe: what the serve gate actually sees
    return { R: compactBallR,
             camDist: camera.position.distanceTo(controls.target),
             radFi: focusFileIdx >= 0 ? sphR(focusFileIdx) : null,
             nOthers: compactIdx ? compactIdx.length - 1 : 0 }; },
  get lodPxOf() { return _lod ? _lod.pxOf : null; },   // per-file box ref-px (probe hook)
  get alphaTgt() { return alphaTgt; },   // lit-set oracle (tier unification pin)
  get jDotArrays() { return { of: fnJDotOf, st: fnJDotSt, legs: fnJDotLegs, key: fnJDotKey, tgt: fnJDotTg }; },  // tgt: dodge axes (issue #5)
  get stubExits() { return stubExits; },  // EXPLAINED EXIT dissolve points
  get anchorBoostArr() { return anchorBoost; },  // corridor-boost px per fi (probe hook)
  get pinTint() { return { tinted: pinTinted ? pinTinted.slice() : [],
    orig: pinTintOrig ? pinTintOrig.slice() : [], key: pinTintKey }; },  // [issue #85 owner r4] probe hook
  get pinTint2() { return pinTint2.map(r => ({
    // exact-match labels; the else arm is unreachable (the frame filter
    // drops records whose mesh is gone) but self-documents as "gone"
    mesh: r.mesh === fnLines ? "fnLines" :
          r.mesh === fnQuiet ? "fnQuiet" :
          (focusArcs && r.mesh === focusArcs.lines)
            ? "focusArcs" : "gone",
    n: r.segs.length })); },   // [#196] wire-arc tint probe
  get pinCoverX() { return pinCoverX; },   // [#196] cross-surface cover
  get pinDots() { return pinPts ? pinPts.visible : null; },   // [#196] endpoint-dot visibility probe
  get pinChain() { return { a: pinChA, b: pinChB, boxA: pinBoxA, boxB: pinBoxB, ep0: pinEp0, ep1: pinEp1 }; },  // [issue #84] fn-box endpoint law probe hook
  get degFloorArr() { return degFloor; },  // zoomed-out min diameter px per fi (probe hook)
  get hlArr() { return hlArr; },  // search-highlight flags per fi (probe hook)
  get hlFnArr() { return [...hlFn]; },  // fn names matching the live query
  get arrowOcclPasses() { return _arrowOcclPasses; },  // chevron occlusion passes since boot
  get chevCensus() {
    // per delivery chevron: owning file, box ref-px, lit state, occlusion
    // and the shown flag aimArrows() actually painted (chevObs diagnosis
    // + harness pin surface; pure read, no frame effects)
    if (!fnArrows || !arrowFile) return null;
    const out = [];
    for (let i = 0; i < arrowFile.length; i++) {
      const fi = arrowFile[i];
      const lodOk = fi < 0 || (_lod ? _lod.resA(fi) : true);
      const low = fi >= 0 && _lod && fnArrowLeg &&
                  fnArrowLeg.length === arrowFile.length * 4 &&
                  _lod.pxOf(fi) < CHEV_FAN_PX;
      const shown = lodOk &&
        (!_arrowOccl[i] || (low && _arrowRide[i] >= 0));
      out.push({ i, fi, px: (fi >= 0 && _lod) ? +_lod.pxOf(fi).toFixed(2) : null,
                 alpha: fi >= 0 ? +alphaTgt[fi].toFixed(2) : 1,
                 occl: _arrowOccl[i] > 0, lodOk, shown,
                 ride: low && _arrowOccl[i] ? +_arrowRide[i].toFixed(2) : null,
                 carried: _chevCarry.length ? !!_chevCarry[i] : true });
    }
    // first-blocker classification + own-box visibility (diagnosis
    // only; same ray semantics as aimArrows())
    if (renderer && THREE && fnArrowPos) {
      const rc = new THREE.Raycaster(); rc.far = Infinity;
      const occ = (fileMesh.visible ? [fileMesh, fnMesh, fnBus] : [fnMesh, fnBus])
                  .filter(Boolean);
      const org = new THREE.Vector3().copy(camera.position);
      for (let i = 0; i < arrowFile.length; i++) {
        const o = out[i];
        const bo = fnArrowBox ? new THREE.Vector3(fnArrowBox[i*3], fnArrowBox[i*3+1],
                                                     fnArrowBox[i*3+2]) : null;
        const tgt = new THREE.Vector3(fnArrowPos[i*3], fnArrowPos[i*3+1], fnArrowPos[i*3+2]);
        const dir = tgt.clone().sub(org); const L = dir.length() || 1; dir.divideScalar(L);
        rc.set(org, dir); rc.far = L - 1;
        for (const h of rc.intersectObjects(occ, false)) {
          if (bo && h.point.distanceTo(bo) < 4) continue;
          o.blocker = h.object === fileMesh ? 'file' : h.object === fnMesh ? 'fnBox' : 'bus';
          o.blockDist = +h.distance.toFixed(1); break;
        }
        if (!o.blocker) o.blocker = 'none';
        if (bo) {   // is the target box itself in clear line of sight?
          rc.set(org, bo.clone().sub(org)); const BL = rc.ray.direction.length() || 1;
          rc.ray.direction.divideScalar(BL); rc.far = BL - 1;
          o.boxBlocked = rc.intersectObjects(occ, false)
            .some(h => h.point.distanceTo(bo) >= 4);
        }
      }
    }
    return out;
  },
  get chainGates() { return [...chainGate.entries()]; },  // per-chain first failing gate (sighting #10)
  get taperDbg() {   // EXPLAINED EXIT probe: per-leg gate state this frame
    const out = [];
    if (_lod && legTermOn) {
      for (const s of busPts) {
        if (typeof s.k !== "string" || s.k.charCodeAt(0) !== 76) continue;
        if (out.some(o => o.k === s.k)) continue;
        const p = s.k.split("|");
        out.push({ k: s.k, legOK: _lod.legOK(s.k), termOn: legTermOn.get(s.k),
                   ctx: focusFileIdx >= 0 && alphaTgt[+p[1]] > 0.5 });
      }
    }
    return out; },
  get legAnchorPx() {
    // rendered L| chains' own-file ANCHOR size: {k, fi, px, boxPx, boosted}
    // for chains actually serving THIS frame. px = max(fn-box px, live
    // sprite px incl. the corridor anchorBoost lift) — the corridor law
    // anchors legs on the SPRITE (size, not brightness), so the pin is
    // min(px) >= ANCHOR_PX. busPts keeps culled chains as inventory
    // (render cull = instance scale 0.0001): filter by served scale,
    // presence is NOT serve state.
    if (!busPts || !fnBus) return [];
    const m = fnBus.instanceMatrix.array, seen = new Map();
    for (let i = 0; i < busPts.length; i++) {
      const k = busPts[i].k;
      if (typeof k !== "string" || k.charCodeAt(0) !== 76) continue;
      if (seen.has(k)) continue;
      const r = Math.hypot(m[i * 16], m[i * 16 + 1], m[i * 16 + 2]);
      seen.set(k, r > 0.001);
    }
    const mat = new THREE.Matrix4();
    const tanH = Math.tan(camera.fov * Math.PI / 360);
    const out = [];
    for (const [k, served] of seen) {
      if (!served) continue;
      const fi = +k.split("|")[1];
      fileMesh.getMatrixAt(fi, mat);
      const dist = camera.position.distanceTo(
        new THREE.Vector3(pos[fi * 3], pos[fi * 3 + 1], pos[fi * 3 + 2]));
      const sprPx = mat.elements[0] * 900 / (tanH * dist);   // diameter, REF-px
      const boxPx = _lod ? _lod.pxOf(fi) : null;
      out.push({ k, fi, px: Math.max(sprPx, boxPx || 0), boxPx, boosted: anchorBoost[fi] > 0,
                 alpha: alphaTgt[fi] });
    }
    return out;
  },
  get corridorCensus() {
    // corridor-complete law census for independent harness verification:
    // every busPts chain (leg "L|fi|st|li" / trunk "sf>tf") with endpoint
    // world coords, serve state (instance scale; culled chains stay in the
    // inventory at scale 0.0001) and the anchor each SERVED end registers
    // to — nearest station within 55wu (station fan termini spread up to
    // ~45wu on the tangent line), else nearest fn box within 55wu, else
    // null = unattached. Stations carry attached serving-chain counts per
    // side. Node positions: d.pos[i*3..]; box screen px: d.lodPxOf(fi).
    if (!busPts) return null;
    const m = fnBus ? fnBus.instanceMatrix.array : null;
    const seen = new Map(), chains = [];
    for (let i = 0; i < busPts.length; i++) {
      const s = busPts[i];
      if (typeof s.k !== "string") continue;
      let e = seen.get(s.k);
      if (!e) {
        e = { k: s.k, kind: s.k.charCodeAt(0) === 76 ? "leg" : "trunk",
              a: [s.a[0], s.a[1], s.a[2]], b: [s.b[0], s.b[1], s.b[2]],
              served: false, anchorA: null, anchorB: null };
        seen.set(s.k, e); chains.push(e);
      }
      if (m) { const r = Math.hypot(m[i * 16], m[i * 16 + 1], m[i * 16 + 2]); if (r > 0.001) e.served = true; }
    }
    const stations = (fnStationsArr || []).map(S =>
      ({ fi: S.fi, id: S.id, p: [S.p[0], S.p[1], S.p[2]], trunks: 0, legs: 0 }));
    // anchor matching is SCREEN-SPACE first (station 14px, box 12px, junction
    // 12px) with world-unit fallbacks (55/55/20wu): depth foreshortening puts
    // fan termini >20wu from their junction yet 1.8px apart on screen — the
    // census must match what the eye matches, or harness pins report phantom
    // unattached ends. All iteration is over fixed arrays (deterministic).
    const proj = p => {
      const v = new THREE.Vector3(p[0], p[1], p[2]).project(camera);
      if (!isFinite(v.x) || !isFinite(v.y) || v.z >= 1) return null;
      const r = renderer.domElement.getBoundingClientRect();
      return { x: (v.x + 1) / 2 * r.width, y: (1 - v.y) / 2 * r.height };
    };
    const pickAnchor = (p, cands) => {
      // cands: [{kind, idx, p, pxTol, wuTol}] — best qualifying by screen px,
      // else by world distance. Candidate list order is fixed (deterministic).
      const sp = proj(p);
      let best = null, bestS = Infinity, bestW = Infinity;
      for (const c of cands) {
        const wu = Math.hypot(c.p[0] - p[0], c.p[1] - p[1], c.p[2] - p[2]);
        if (wu > c.wuTol * 1.2) continue;   // hard world ceiling either way
        let s = Infinity;
        if (sp) { const q = proj(c.p);
          if (q) s = Math.hypot(q.x - sp.x, q.y - sp.y); }
        const okS = s <= c.pxTol, okW = wu <= c.wuTol;
        if (!okS && !okW) continue;
        if (okS && s < bestS) { best = c; bestS = s; }
        else if (!okS && wu < bestW) { best = c; bestW = wu; }
      }
      return best;
    };
    const stCands = stations.map((st, si) => ({ kind: "station", idx: si, p: st.p, pxTol: 14, wuTol: 55 }));
    const boxCands = [];
    for (let bi = 0; bi < fnMeta.length; bi++) {
      const mb = fnMeta[bi];
      if (mb) boxCands.push({ kind: "box", idx: mb.file, p: mb.p, pxTol: 12, wuTol: 55 });
    }
    // junction bollards (fnJDot instances): interior corridor heads — a leg
    // end landing on one is attached (part of the full-path unit), though
    // the UNIT termini still owe a file anchor elsewhere
    const juncCands = [];
    if (fnJDot) {
      const jm = fnJDot.instanceMatrix.array;
      for (let ji = 0; ji < fnJDot.count; ji++)
        juncCands.push({ kind: "junc", idx: ji, p: [jm[ji * 16 + 12], jm[ji * 16 + 13], jm[ji * 16 + 14]], pxTol: 12, wuTol: 20 });
    }
    // leads-home fallback (rubric Amendment 4): an end that misses the tight
    // windows still attaches to the nearest IN-VIEW station within 300px —
    // fan-spread termini 18-40px out are visually leads-home, not floating
    // ink, and the harness pin reads unattached==0. Marked leadsHome with px
    // so the distinction stays queryable.
    const leadsHome = (p, which, c) => {
      const sp = proj(p);
      if (!sp) return false;
      let best = null, bd = 300;
      for (let si = 0; si < stations.length; si++) {
        const q = proj(stations[si].p);
        if (!q) continue;
        const d = Math.hypot(q.x - sp.x, q.y - sp.y);
        if (d < bd) { bd = d; best = si; }
      }
      if (best === null) return false;
      if (c.kind === "leg") stations[best].legs++;   // trunk feed owned by the post-loop pass
      c["anchor" + which.toUpperCase()] = { type: "station", st: best, leadsHome: Math.round(bd * 10) / 10 };
      return true;
    };
    const attach = (c, which) => {
      const p = c[which];
      for (const set of [stCands, boxCands, juncCands]) {
        const a = pickAnchor(p, set);
        if (!a) continue;
        const rec = { type: a.kind };
        if (a.kind === "station") {
          rec.st = a.idx;
          if (c.kind === "leg") stations[a.idx].legs++;   // legs claim where they land; trunks feed post-loop
        }
        else if (a.kind === "box") rec.fi = a.idx;
        else rec.j = a.idx;
        c["anchor" + which.toUpperCase()] = rec;
        return;
      }
      leadsHome(p, which, c);
    };
    for (const c of chains) {
      if (!c.served) continue;
      attach(c, "a");
      attach(c, "b");
    }
    // per-station trunkNearPx: min screen px to ANY served trunk endpoint,
    // and trunk FEED counts at the leads-home window (Amendment 4 bar
    // <= 300px): a station is trunk-fed when a served trunk endpoint lands
    // within 300px — fan spread puts termini 15-40px past the station dot,
    // still leads-home, not floating ink. One-sidedness = legs>0 &&
    // trunks==0 (fan without its trunk = the cut-bridge class); legs==0
    // stations are the no-fan ramp-bridged class (the file's own box anchors
    // via fnLines ramps, which the census does not carry).
    for (const st of stations) st.trunkNearPx = null;
    for (const c of chains) {
      if (!c.served || c.kind !== "trunk") continue;
      const fed = new Set();
      for (const p of [c.a, c.b]) {
        const sp = proj(p);
        if (!sp) continue;
        for (let si = 0; si < stations.length; si++) {
          const q = proj(stations[si].p);
          if (!q) continue;
          const d = Math.hypot(q.x - sp.x, q.y - sp.y);
          if (stations[si].trunkNearPx === null || d < stations[si].trunkNearPx)
            stations[si].trunkNearPx = Math.round(d * 10) / 10;
          if (d <= 300) fed.add(si);
        }
      }
      for (const si of fed) stations[si].trunks++;
    }
    return { chains, stations };
  },
  get legendOpen() { return legendOpen; }, get busPtsMeta() { return busPtsMeta; },
  get fnJclip() { return fnJclip; }, get fnLegN() { return fnLegN; },
  // probe hook: world -> screen px through the live camera + canvas rect
  projectPoint(x, y, z) {
    const v = new THREE.Vector3(x, y, z).project(camera);
    const r = renderer.domElement.getBoundingClientRect();
    return { x: r.left + (v.x + 1) / 2 * r.width,
             y: r.top + (1 - v.y) / 2 * r.height, z: v.z };
  },
  pickWireMeta, wireDesc, showWireTip, hideWireTip,
  get wirePin() { return wirePin; },   // [issue #82] {surface, kind, id, menu} | null
  get pinCover() { return pinCover; },
  get litSet() { return compactIdx; }, get compactScale() { return compactScale; },
  get overlaps() { return compactOverlaps; },
  get camTween() { return camTween; }, get focusStack() { return focusStack; },
  get focusFileIdx() { return focusFileIdx; }, get compactAnim() { return !!compactAnim; },
  get focusFold() { return { ...flabFoldState, unfold: flabUnfold }; },   // [#8] super-hub fold probe
  get focusHullProbe() {
    if (!focusHull) return null;
    return { fi: focusHull.fi, R: focusHull.R, boxes: focusHull.boxes,
      pos: [focusHull.fill.position.x, focusHull.fill.position.y,
            focusHull.fill.position.z],
      fillO: focusHull.fill.material.opacity,
      rimO: focusHull.rim.material.opacity, oHull: _oHull,
      transparent: focusHull.rim.material.transparent,
      depthWrite: focusHull.rim.material.depthWrite,
      ro: focusHull.rim.renderOrder,
      boxRO: fnMesh ? fnMesh.renderOrder : null };
  },
  get routeFns() { return __routeFns; },   // [#10] {turnsOf, mergeBends}
  get posSavedLive() { return posSaved !== null; },
  posAt: i => [pos[i*3], pos[i*3+1], pos[i*3+2]],
  compactTgtAt: i => compactTgt ? [compactTgt[i*3], compactTgt[i*3+1], compactTgt[i*3+2]] : null,
  get fileMesh() { return fileMesh; }, get fnMesh() { return fnMesh; },
  get controls() { return controls; },
  get fnMeta() { return fnMeta; }, get fnStalks() { return fnStalks; },
  get hovered() { return hovered; },
  get hoveredFn() { return hoveredFn; },   // [issue #112] hover-id teardown probe
  get groundGrid() { return groundGrid; },
  get groupsMode() { return groupsMode; }, groups,
  get spread() { return spread; }, get deadOnly() { return deadOnly; },
  get collapsed() { return collapsed; }, dpos, supCollapsed, refreshCollapse,
  colArr,
  get fnStalk() { return fnStalk; },
  syncFileMesh,
  mapPane: { canvas: mapPane, draw: drawMapPane },
  mwires, mapInfo, mapWireAt, wireKeyOf,   // [issue #84] probe surface: hit-test named wires
  get mapVars() { return mapVarsOn; }, mapExpandUser,
  get mapFrozenIx() { return mapFrozenIx; },   // [issue #113] stale-freeze probe
  get mapHover() { return mapHover; },   // [issue #113] stale wire-hover dim probe
  get mapVarsChipRect() { return mapVarsChipRect; },
  mapChipAt, get mapListChip() { return mapListChip; },
  get mapZ() { return mapZ; }, get mapPX() { return mapPX; },
  get mapPY() { return mapPY; }, mapClampView,
  get mapInkOn() { return mapInkOn; },
  get mapLodBand() { return mapLodBand; },   // [#77] 0 full / 1 thinned / 2 cluster
  get mapLodFitZ() { return mapLodFitZ; },   // [#77]
  get mapLodThresholds() { return { z1: MAP_LOD_Z1, z1x: MAP_LOD_Z1X,
    z2: MAP_LOD_Z2, z2x: MAP_LOD_Z2X, tapFold: MAP_TAP_FOLD_Z }; },   // [#77]/[#61] band + tap gates (probe)
  get mapCenterReq() { return mapCenterReq; }, get mapPulse() { return mapPulse; },
  get pickWireZ() { return pickWireZ; },   // [issue #87] ink depth at last pick
  get paneW() { return paneW; }, setMapVisible, divider,
  get glW() { return glW(); },
  get bucketMesh() { return bucketMesh; }, raycaster, linkFiltered, typeVisible,
  nodeFiltered, fnMode, supMem, strongPair,
  get cbFn() { return cbFnEl.checked; },   // [#59] live checkbox truth (fnMode above is a boot snapshot)
  get mapLayout() { return mapLayout; },
  get mapRects() { return mapRects; },
  get routeAudit() { return mapLayout && mapLayout.audit; },
  sphR,
  get focusArcRef() { return focusArcs; },
  get rfwProbe() { return { fa: !!focusArcs, focusActive, budgetN: budgetLit ? budgetLit.size : null, fi: focusFileIdx }; } ,
  get semAff() { return {   // [#279] affinity species probe — counts plus the
    rows: semAff.length,     // top row's endpoints so gates can drive cards
    served: semServed, on: showSemAff, lod: lodClose,
    sample: semAff.length ? { a: nodes[semAff[0][0]].path,
                              b: nodes[semAff[0][1]].path,
                              s: semAff[0][2] } : null }; },
  // [#123] quiescence probe for the page harness: camera tween done,
  // compaction done, every node alpha and hover-scale at target (the
  // same 0.003 / 0.004 snap thresholds the tick loop eases with). The
  // harness waits on this instead of blanket sleeps — reading a
  // mid-transition frame can pass a check a settled frame would fail.
  get settled() {
    if (camTween !== null || compactAnim !== null) return false;
    for (let i = 0; i < N; i++) {
      if (Math.abs(alphaTgt[i] - alphaArr[i]) >= 0.003) return false;
      if (Math.abs((i === hovered ? 1.8 : 1) - hoverScale[i]) >= 0.004) return false;
    }
    return true;
  } };
tick();
</script>
</body>
</html>
"""
