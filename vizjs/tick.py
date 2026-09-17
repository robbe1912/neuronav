# vizjs/tick.py — rung 4/17 of the template join (issues
# #86 phase-3 / #299 A): the render tick (pure function of camera pose + build data). Moved VERBATIM from viz.py —
# every edit here changes graph.html: regen the bake (the standing
# template law; test_viz refuses a bake older than viz.py/vizjs).
_JS_TICK = r"""// Pure function of camera pose + build data; no layout change.
function busLodInit() {
  const hpx = renderer.domElement.clientHeight || 900;
  const wuPerPx = 1 / refPxPerWu(1, hpx);   // #299 D: the shared fov law
  const memo = new Map();
  // REF-normalized px (user directive, round 5): all screen laws live in
  // REFERENCE pixels — px a size would have on a nominal 900px-tall canvas.
  // Thresholds become resolution-independent; a probe at any viewport reads
  // the same numbers.
  const REFH = 900, refK = REFH / hpx;
  const pxOf = fi => {
    let v = memo.get("px" + fi);
    if (v === undefined) {
      const sc = (fnBoxScale && fnBoxScale.get(fi)) || 4;
      const d = Math.hypot(pos[fi*3] - camera.position.x,
                           pos[fi*3+1] - camera.position.y,
                           pos[fi*3+2] - camera.position.z) || 1;
      v = (sc / (wuPerPx * d)) * refK;
      memo.set("px" + fi, v);
    }
    return v;
  };
  // FOCUS-STATE master gate (skeptic r5 objection): when the user asks for
  // the fn layer (focus active) and the FOCUS file's own box is readable,
  // serve the whole bus tier — camera distance alone gated the busiest
  // hub's d2 state (the top .tscn hub: 21/21 bollards at radius 0 because
  // its neighborhood shells sit farther out than the entry scene's). Zoomed-out
  // overview (user's droplet state) still gates: focus box < floor there.
  // Rotation-invariant master gate: autoRotate orbits the camera at constant
  // camDist, so a sphere-relative px floor oscillates with spin phase and
  // the tier flickers gate<->serve (measured: servePx 2.34 vs 1.80 across
  // reloads at the same nominal state). camDist-to-target is orbit-stable:
  // serve iff the camera is inside 2.2 compact-ball radii of the focus
  // (default focus camera = 1.69R serves; the user's droplet state = 7.9R
  // stays gated; ~1.5x default zoom-out is the cutoff)
  const serveAll = focusFileIdx >= 0 && compactBallR > 0 &&
    camera.position.distanceTo(controls.target) <= 2.2 * compactBallR;
  _lodServe = serveAll;
  if (fnLodV) { fnLodV.serveFi = focusFileIdx; fnLodV.servePx = focusFileIdx >= 0 ? pxOf(focusFileIdx) : -1; }
  // TIER/NODE UNIFICATION (sighting #9 + slider regression): res() is the
  // ONE focus-set oracle — a file serves the wire tier iff it is LIT
  // (alphaTgt > 0.5 encodes depth + filters + the 1-hop ghost law) AND its
  // box resolves. Before this, a depth-3 ghost (alpha 0.04) with a big box
  // still served corridors = wires to invisible files at any zoom.
  const res = fi => (serveAll || pxOf(fi) >= 2.5) && alphaTgt[fi] > 0.5;
  // the USER's 735h window (round-5b boot-straggler fix: pair census found
  // 3-4 dots over 0.8-1.2px boxes; 2.2 ref-px = 1.8 CSS px there)
  // chevron floor is LOWER: a delivery mark may ride a 1.5-2.5px box —
  // visible at probe-d2 zooms, where the 2.5 floor thinned on-screen
  // chevrons 18->3 (too sparse for route tracing). Below 1.5 the box is
  // a speck and the mark reads as noise on nothing
  const resA = fi => pxOf(fi) >= 1.5 && alphaTgt[fi] > 0.5;
  const tkPx = tk => {
    const p = String(tk).split(">");
    return p.length === 2 ? Math.min(pxOf(+p[0]), pxOf(+p[1])) : pxOf(+String(tk).split("|")[1]);
  };
  const trOK = tk => {
    const p = String(tk).split(">");
    return p.length === 2 ? res(+p[0]) && res(+p[1]) : res(+String(tk).split("|")[1]);
  };
  const stOK = fi => {
    if (!res(fi)) return false;
    const tks = stationTks && stationTks.get(fi);
    // no bare station heads: a station tree with NO trunk leaving it renders
    // dots + legs connected to nothing ("node heads in the void", 4th user
    // sighting) — the chain (legs + bollards) only serves when >=1 trunk
    // beyond the station also serves
    if (!tks || !tks.length) return false;
    for (const tk of tks) if (trOK(tk)) return true;
    return false;
  };
  // CHAIN-INTEGRITY law (user defect: orphan junction legs): a leg ("L|fi|…")
  // is connector ink between a file box and its station — it may serve ONLY
  // when its OWN file's box resolves on screen (strict px floor, never
  // serveAll-blind: the waiver exists for the FOCUS file's tier, and keying
  // it on the leg's own file keeps that accommodation intact) AND its
  // station context serves (stOK: station bollard + >=1 serving trunk).
  // Both ends resolved or the whole chain culls (all FS segments share k).
  const legOK = tk => {
    const fi = +String(tk).split("|")[1];
    // PERCEPTUAL_ANCHOR: 2.5px resolves geometrically but reads as a
    // speck — a leg chained to a sub-8px box is floating ink (3rd user
    // sighting, VLM-confirmed). Anchor READS when the box naturally
    // measures ANCHOR_PX OR a serving corridor has boosted the file's
    // SPRITE to the floor (corridor-complete law: size, not brightness —
    // dim files keep their dim color). Plus station context (stOK):
    // no leg without its trunk, no trunk without both ends.
    return (pxOf(fi) >= ANCHOR_PX || anchorBoost[fi] > 0) && stOK(fi);
  };
  return { res, resA, trOK, stOK, legOK, pxOf, tkPx };
}

function tick() {
  const nowT = performance.now();
  // map selection pulse: keep the pane repainting while the amber ring
  // breathes; drop it at end of life (pane closed -> pulse frozen, not lost)
  if (mapPulse && mapVisible) {
    if (performance.now() - mapPulse.t0 > MAP_PULSE_MS) mapPulse = null;
    drawMapPane();
  }
  // dash-flow while focusing: offsets walk each edge along its s→t vertex
  // order (caller→callee), with a per-bucket phase hashed from the bucket
  // index so the flow reads per-edge instead of one global march. Overview
  // keeps dashed fully off — USE_DASH leaves the shader, so nothing
  // shimmers at rest.
  const flowT = nowT / 1000;
  bucketMat.forEach((em, i) => {
    if (edgeFlowOn) {
      if (!em.dashed) { em.dashed = true; em.dashSize = 8; em.gapSize = 5; em.needsUpdate = true; }
      const period = em.dashSize + em.gapSize;
      const hashI = (i * 2654435761) % 997 / 997 * period;
      em.dashOffset = -((flowT * FLOW_SPEED + hashI * 13) % period);
    } else if (em.dashed) {
      em.dashed = false; em.dashOffset = 0; em.needsUpdate = true;
    }
  });
  // focus arcs share the dash-flow direction cue (single material — the
  // per-link phase lives in the lineDistance attribute)
  if (focusArcs && focusArcs.lines.visible && edgeFlowOn) {
    const fp = focusArcs.mat.dashSize + focusArcs.mat.gapSize;
    focusArcs.mat.dashOffset = -((flowT * FLOW_SPEED) % fp);
  }
  // fn wires: dash-flow direction cue + LOD fade — the layer dissolves as
  // the camera pulls away from the ball (1px salad reads as noise from afar)
  if (fnLines && fnLines.visible && edgeFlowOn) {
    const fp = 7 + 4;   // dashSize + gapSize of the fn-wire dashed material
    fnLines.material.dashOffset = -((flowT * FLOW_SPEED) % fp);
  }
  if (fnLines && compactBallR > 0 && hubRing) {
    const cd = camera.position.distanceTo(hubRing.position);
    let lod = Math.max(0, Math.min(1, (2.8 * compactBallR - cd) / (0.9 * compactBallR)));
    // the fade is a FAR-ZOOM noise control; when the focus-state gate
    // serves the tier the paint must be FULL — dim dots at the default
    // focus camera (0.60/0.40) made a geometrically-served state read
    // as spheres-only (skeptic r5-final objection)
    if (_lodServe) lod = 1;
    fnLines.material.opacity = EMPHASIS.FOCUS_TIER_OPACITY * lod;
    if (fnQuiet) fnQuiet.material.opacity = 0.16 * lod;
    if (fnBus) fnBus.material.opacity = 0.35 + 0.65 * lod;
    if (jdot) jdot.mesh.material.opacity = 0.35 + 0.55 * lod;
    if (fnArrows) fnArrows.material.opacity = 0.9 * lod;
    _oDot = jdot ? 0.35 + 0.55 * lod : 0;
    _oArrow = fnArrows ? 0.9 * lod : 0;
    // [#12] the container is focus-tier ink: served = full glass, far zoom
    // fades it to nothing — the file node collapses back to its dot
    if (focusHull) {
      focusHull.fill.material.opacity = 0.05 * lod;
      focusHull.rim.material.opacity = 0.16 * lod;
      _oHull = 0.16 * lod;
    } else _oHull = 0;
  } else { _oDot = 0; _oArrow = 0; _oHull = 0; }
  // conduit width is SCREEN-CONSTANT per segment: radius ∝ each segment's own
  // distance to the camera (~2.4px on screen at every depth, next to 1px wires)
  fnLodV = { bollardsShown: 0, bollardsGated: 0, conduitsShown: 0, conduitsGated: 0,
             chevShown: 0, minChevPx: Infinity, minServedBoxPx: Infinity, minBollardPx: Infinity,
             serveFi: -1, servePx: -1, oDot: _oDot, oArrow: _oArrow, segOccl: 0 };
  _lod = (fnBus || jdot) ? busLodInit() : null;   // after fnLodV: it stamps serveFi/servePx
  // endpoint anchor demands are per-frame (camera-pose dependent): reset,
  // then arcs, serving corridors and serving legs add theirs
  anchorBoost.fill(0);
  if (focusArcs && focusArcs.lines.visible && focusArcs.lines.userData.meta)
    for (const m of focusArcs.lines.userData.meta) {
      anchorBoost[links[m.li].s] = ANCHOR_PX; anchorBoost[links[m.li].t] = ANCHOR_PX;
    }
  // leg termini must LAND: a served leg whose BOTH ends project outside the
  // viewport renders as floating mid-view debris ("starting nowhere and
  // ending nowhere", user sighting #8 — orbit sweep found up to 11 such legs
  // at close zooms az 120-180). Build the per-key on-screen map before the
  // serve loop; legs failing it cull whole-chain (all FS segments share k).
  // Trunks stay exempt (edge-exiting highways read as leaving; user ruling).
  legTermOn.clear();   // module-scope Map (probe hook): chain-key -> terminus on-screen
  if (fnBus && busPts) {
    const v3 = new THREE.Vector3();
    const spans = new Map();
    for (const s of busPts) {
      let sp = spans.get(s.k);
      if (!sp) spans.set(s.k, { a: s.a, b: s.b });   // first segment's a = chain start
      else sp.b = s.b;                               // last segment's b = chain terminus
    }
    for (const [k, sp] of spans) {
      // SIGHTING #10: both rejection clauses here were PROJECTION-based —
      // a terminus sliding off-frame (|ndc|>1.05) or a projected span
      // crossing 35% of the diagonal flipped the WHOLE chain off in one
      // wheel click (en-bloc corridor+junction vanish: the cull also drops
      // its stAttKey, so the bollard goes too). Zoom law is now monotonic:
      // (a) terminus on-screen-ness is NOT a cull reason — ink is
      // world-anchored (draws-to-anchor), a wire exiting the frame is a
      // highway leaving view, not float; (b) the 35%-diagonal sprawl cap
      // (ruling b) is evaluated at the NOMINAL focus framing
      // (2.2·compactBallR, the serveAll reference distance), so it culls
      // corridors that would sprawl where they are MEANT to be read, and
      // zooming in can never cross it (px at nominal dist is
      // zoom-invariant; pan-invariant by construction).
      let ok = true;
      const wu = Math.hypot(sp.b[0] - sp.a[0], sp.b[1] - sp.a[1], sp.b[2] - sp.a[2]);
      if (isFinite(wu) && compactBallR > 0) {
        const cw = renderer.domElement.clientWidth || 1600;
        const ch2 = renderer.domElement.clientHeight || 900;
        const nominalD = 2.2 * compactBallR;
        const pxN = wu / refPxPerWu(nominalD, ch2);   // #299 D
        if (pxN > 0.35 * Math.hypot(cw, ch2)) ok = false;
      }
      legTermOn.set(k, ok);
    }
  }
  // highways). Trunk keys are "sf>tf".
  if (_lod && fnBus && busPts)
    for (const s of busPts) {
      if (typeof s.k !== "string" || s.k.charCodeAt(0) === 76) continue;
      if (!_lod.trOK(s.k)) continue;
      const p = s.k.split(">");
      if (p.length === 2) { anchorBoost[+p[0]] = ANCHOR_PX; anchorBoost[+p[1]] = ANCHOR_PX; }
    }
  // served fn-box SCREEN discs — shared by the conduit ink-occlusion law
  // (issue #7) and the bollard dodge (issue #5). Box ink is modeled at
  // (count?3:2)wu radius, the same formula the QA gate uses for jClear.
  const dv = new THREE.Vector3();
  const dRect = renderer.domElement.getBoundingClientRect();
  const dCh = renderer.domElement.clientHeight || 900;
  // #299 D: the one inline tan kept — its degenerate-fov ||0.4 guard has
  // no place in the shared leaf (fov is 55 here; the guard is belt+braces)
  const dHt = Math.tan(camera.fov * Math.PI / 360) || 0.4;
  const discs = [];
  for (let bi = 0; fnMeta && bi < fnMeta.length; bi++) {
    const m = fnMeta[bi];
    if (m.agg && !m.count) continue;
    if (!alphaTgt || !alphaTgt[m.file] || alphaTgt[m.file] < 0.5) continue;
    dv.set(m.p[0], m.p[1], m.p[2]).project(camera);
    toScreen(dv, dRect.width, dRect.height);   // #299 D: shared NDC->canvas
    const bx = _scr[0], by = _scr[1];
    if (bx < -60 || bx > dRect.width+60 || by < -60 || by > dRect.height+60) continue;
    const cd = Math.hypot(m.p[0]-camera.position.x, m.p[1]-camera.position.y, m.p[2]-camera.position.z);
    discs.push({ x: bx, y: by, r: (m.count?3:2)*(dCh/2)/(dHt*cd), wx: m.p[0], wy: m.p[1], wz: m.p[2] });
  }
  if (fnBus && busPts) {
    stAttKey.clear(); chainGate.clear(); inkKeys.clear();
    let dirty = false;
    const lod = _lod;
    const a = fnBus.instanceMatrix.array;
    const stubPts = [];
    let curK = null, curJ = 0;
    const _sv = new THREE.Vector3();
    // per-key segment totals (bridge taper runs from the focus-side end)
    const segN = new Map();
    for (const s of busPts)
      if (typeof s.k === "string") segN.set(s.k, (segN.get(s.k) || 0) + 1);
    for (let i = 0; i < busPts.length; i++) {
      const s = busPts[i];
      const d = Math.hypot((s.a[0]+s.b[0])/2 - camera.position.x,
                           (s.a[1]+s.b[1])/2 - camera.position.y,
                           (s.a[2]+s.b[2])/2 - camera.position.z);
      const isLeg = typeof s.k === "string" && s.k.charCodeAt(0) === 76;
      const termOk = !isLeg || legTermOn.get(s.k) !== false;
      let gateOk = lod ? (isLeg ? (lod.legOK(s.k) && termOk) : lod.trOK(s.k)) : true;
      if (!isLeg && gateOk && lod) {
        // sighting #9: bridge trunks (station↔station arcs whose files
        // attach directly, no fan) read as wire-in-the-void once either
        // anchor box drops below the 8px perceptual floor. The corridor
        // law now points BOTH ways: a trunk renders only when BOTH its
        // stations' file anchors read (box >= ANCHOR_PX or boosted).
        const pp = String(s.k).split(">");
        if (pp.length === 2) {
          const anch = fi => lod.pxOf(fi) >= ANCHOR_PX || anchorBoost[fi] > 0;
          if (!(anch(+pp[0]) && anch(+pp[1]))) gateOk = false;
        }
      }
      // sighting #10 forensic: first failing gate per chain (extend-only
      // probe surface — 'served' | 'legOK' | 'termOn' | 'trOK' | 'bridgeAnch')
      if (typeof s.k === "string" && !chainGate.has(s.k))
        chainGate.set(s.k, gateOk ? "served" :
          (isLeg ? (lod && !lod.legOK(s.k) ? "legOK" : "termOn")
                 : (lod && !lod.trOK(s.k) ? "trOK" : "bridgeAnch")));
      if (gateOk && typeof s.k === "string") {
        // empty-station law: bollards render only against attachments that
        // ACTUALLY served this frame — heads key on trunk endpoints "T|fi",
        // sub dots on their leg key (all FS segments of a chain share k)
        if (isLeg) stAttKey.add(s.k);
        else { const tp = String(s.k).split(">");
          if (tp.length === 2) { stAttKey.add("T|" + (+tp[0])); stAttKey.add("T|" + (+tp[1])); } }
      }
      if (fnLodV) {
        if (gateOk) { fnLodV.conduitsShown++;
          fnLodV.minServedBoxPx = Math.min(fnLodV.minServedBoxPx, lod ? lod.tkPx(s.k) : Infinity); }
        else fnLodV.conduitsGated++;
      }
      // EXPLAINED EXIT (user amendment): a chain culled while the user has
      // interaction context on it must not vanish silently — taper a short
      // stub from its attached end (radius ramping to 0 over 3 segments)
      // and label the dissolve point. Context = the chain's rider file is
      // LIT (focus file itself, or within the focus..depth visible set —
      // a fan's riders ARE the focus callees). Three legal states only:
      // ATTACHED / ABSENT / EXPLAINED EXIT. Overview stays quiet.
      if (s.k !== curK) { curK = s.k; curJ = 0; } else curJ++;
      const base = Math.max(0.05, Math.min(12, d * 0.0037 * (s.rf || 1)));
      let taperF = 0, destFi = -1, jx = curJ;
      if (!gateOk && focusFileIdx >= 0 && lod) {
        if (isLeg && lod.legOK(s.k)) {
          const pp = String(s.k).split("|");
          if (alphaTgt[+pp[1]] > 0.5) {
            taperF = Math.max(0, 1 - curJ / 3); destFi = +pp[1];
          }
        } else if (!isLeg && lod.trOK(s.k)) {
          const pp = String(s.k).split(">");
          if (pp.length === 2 && (+pp[0] === focusFileIdx || +pp[1] === focusFileIdx)) {
            // taper from the FOCUS-side end of the bridge
            const n = segN.get(s.k) || 1;
            jx = +pp[1] === focusFileIdx ? (n - 1 - curJ) : curJ;
            taperF = Math.max(0, 1 - jx / 3);
            destFi = +pp[0] === focusFileIdx ? +pp[1] : +pp[0];
          }
        }
      }
      let rT = gateOk ? base : (taperF > 0 ? base * taperF : 0.0001);
      // issue #7 ink occlusion: a mid-chain segment whose SCREEN ink falls
      // inside a served box disc + 4px parks — the corridor reads as
      // passing behind the box (same hide-law class as chevrons under
      // bollards). Endpoint segments are exempt: draws-to-anchor seams and
      // the empty-station endpoint census read them. Serve state stays
      // chain-level (corridor-complete law); only the ink under a box hides.
      if (gateOk && discs.length && typeof s.k === "string" &&
          curJ > 0 && curJ < (segN.get(s.k) || 1) - 1) {
        _sv.set(s.a[0], s.a[1], s.a[2]).project(camera);
        toScreen(_sv, dRect.width, dRect.height);   // #299 D
        const ax2 = _scr[0], ay2 = _scr[1];
        _sv.set(s.b[0], s.b[1], s.b[2]).project(camera);
        toScreen(_sv, dRect.width, dRect.height);
        const bx2 = _scr[0], by2 = _scr[1];
        const abx = bx2 - ax2, aby = by2 - ay2, ab2 = abx*abx + aby*aby || 1;
        for (let di2 = 0; di2 < discs.length; di2++) {
          const dc = discs[di2];
          let tt = ((dc.x-ax2)*abx + (dc.y-ay2)*aby) / ab2;
          tt = tt < 0 ? 0 : (tt > 1 ? 1 : tt);
          if (Math.hypot(ax2 + abx*tt - dc.x, ay2 + aby*tt - dc.y) < dc.r + 4) {
            rT = 0.0001; if (fnLodV) fnLodV.segOccl++;
            break;
          }
        }
      }
      if (taperF > 0 && jx === 2) {
        _sv.set((s.a[0]+s.b[0])/2, (s.a[1]+s.b[1])/2, (s.a[2]+s.b[2])/2).project(camera);
        if (isFinite(_sv.x) && _sv.z < 1)
          stubPts.push({ nx: _sv.x, ny: _sv.y, fi: destFi, dest: String(s.k) });
      }
      const f = rT / fnBusRi[i];
      // NO EXPLANATION WITHOUT PRESENCE (sighting #11): record which chains
      // carry ink this frame — the pinned wireTip's lifetime checks against
      // this set every tick and hides the moment its anchor loses ink
      if (typeof s.k === "string" && rT > 0.002) inkKeys.add(s.k);
      if (Math.abs(f - 1) > 0.06) {
        const o = i * 16;
        a[o] *= f; a[o+1] *= f; a[o+2] *= f;
        a[o+8] *= f; a[o+9] *= f; a[o+10] *= f;
        fnBusRi[i] = rT;
        dirty = true;
      }
    }
    if (dirty) fnBus.instanceMatrix.needsUpdate = true;
    stubExits = stubPts;
  }
  updateStubLabs();
  // bollards read at ANY camera distance: a 1-2px dot in a dark knot is not
  // a reroute node you can see — radius ∝ camera distance (~2.5-4px on screen)
  if (jdot && jdot.pos) {
    let dirty = false;
    const a = jdot.mesh.instanceMatrix.array;
    // anchor context (issue #5 + empty-station law): a dodged dot stays
    // lawful while it sits within 20px of RENDERED conduit ink — every
    // bezier sample of every rendered leg/trunk chain (busPts a/b, scale
    // gate). The dodge may slide a dot anywhere ALONG this ink network,
    // never off it: a dot stranded from all ink reads as an empty station.
    const anchors = [];
    if (fnBus && busPts && discs.length) {
      const fa2 = fnBus.instanceMatrix.array;
      for (let si = 0; si < busPts.length && si*16+2 < fa2.length; si++) {
        if (Math.hypot(fa2[si*16], fa2[si*16+1], fa2[si*16+2]) <= 0.001) continue;
        const s2 = busPts[si];
        dv.set(s2.a[0], s2.a[1], s2.a[2]).project(camera);
        toScreen(dv, dRect.width, dRect.height);   // #299 D
        anchors.push(_scr[0], _scr[1]);
        dv.set(s2.b[0], s2.b[1], s2.b[2]).project(camera);
        toScreen(dv, dRect.width, dRect.height);
        anchors.push(_scr[0], _scr[1]);
      }
    }
    // issue #5: bollards must clear fn-box/label INK on screen. Depth-packed
    // layouts park a bollard 60-90wu BEHIND a foreign box yet project it
    // inside the box disc — no world-space floor fixes that. Slide the
    // RENDERED instance along its own corridor ink (station: down the leg
    // fan; sub: along its leg toward the station; Jof: down its delivery
    // stub) until its disc clears every served box. Pure function of the
    // static arrays + camera: identical frames produce identical dodges.
    // True world arrays (jdot.pos, zero-gap anchors, census) are untouched.
    for (let i = 0; i < jdot.r.length; i++) {
      const d = Math.hypot(jdot.pos[i*3] - camera.position.x,
                           jdot.pos[i*3+1] - camera.position.y,
                           jdot.pos[i*3+2] - camera.position.z);
      // SCREEN-CONSTANT bollard radii, the same law class as conduit width
      // (skeptic B3: a world-radius floor shrinks with zoom while trunks
      // hold ~screen px — hierarchy inverted at hubzoom). Station ≈11px,
      // sub-junction ≈5.7px, Jof ≈5.4px (px-factor ~1073), scaled by the
      // dot's size factor — one dominant merge dot per station, tree dots
      // clearly subordinate (round-3 crop verdict: "cluster of mid-sized
      // balls" with 9px/6px was still too flat).
      const kf = jdot.k ? jdot.k[i] : 1;
      const lod = _lod;
      const hpxr = renderer.domElement.clientHeight || 900;
      let gateOk = lod ? (jdot.st[i] ? lod.stOK(jdot.of[i]) : lod.res(jdot.of[i])) : true;
      // sighting #9 (2) + empty-station law (user report): a bollard renders
      // only when its attachment ACTUALLY SERVED this frame — station heads
      // need a serving trunk endpoint ("T|fi" in stAttKey), sub dots their
      // own leg chain. Inventory legs/trunks do NOT earn ink (the user saw
      // fully-empty station bollards floating at the landing pose).
      if (gateOk && jdot.key && jdot.key[i] && !stAttKey.has(jdot.key[i]))
        gateOk = false;
      // viewport-FRACTION law (user directive r5): the world-slope law
      // r = kf*0.0102*d keeps every element a constant FRACTION of the
      // frame height — px-at-nominal-900 (ref-px) is invariant; the
      // skeptic's 1600x900 numbers are exactly the round-4 values
      const rRef = d * 0.0102 * kf;
      if (fnLodV) {
        if (gateOk) { fnLodV.bollardsShown++;
          fnLodV.minServedBoxPx = Math.min(fnLodV.minServedBoxPx, lod ? lod.pxOf(jdot.of[i]) : Infinity);
          const wpp = 1 / refPxPerWu(1, hpxr);   // #299 D
          fnLodV.minBollardPx = Math.min(fnLodV.minBollardPx, ((2 * rRef) / (wpp * d)) * (900 / hpxr)); }
        else fnLodV.bollardsGated++;
      }
      const rT = gateOk ? rRef : 0.0001;
      // issue #5 slide: step along ±tg (down and UP the own ink — a deg-240
      // hub's clear anchored bearing can sit ~100wu up the trunk fan, F1 #263)
      // in 3wu increments (cap 120wu) until the projected disc clears every
      // served box by 2px; every accepted step must stay anchored to conduit
      // ink (20px census law) or the dot keeps its true position. Topdown is
      // the degenerate case for the down-fan axis (−Y projects to ~0), so a
      // direction whose screen step dies falls back to sliding AWAY from the
      // worst violating box in XZ.
      if (discs.length && jdot.tg) {
        const rr = rT * (dCh/2)/(dHt*Math.max(d,1));
        const clearAt = (wx, wy, wz) => {
          dv.set(wx, wy, wz).project(camera);
          toScreen(dv, dRect.width, dRect.height);   // #299 D
          const sx = _scr[0], sy = _scr[1];
          for (let di = 0; di < discs.length; di++) {
            const dc = discs[di];
            if (Math.hypot(sx-dc.x, sy-dc.y) - dc.r - rr < 2) return { ok: false, sx, sy };
          }
          return { ok: true, sx, sy };
        };
        let qx = jdot.pos[i*3], qy = jdot.pos[i*3+1], qz = jdot.pos[i*3+2];
        const at0 = clearAt(qx, qy, qz);
        if (!at0.ok) {
          // worst violator (max overlap) drives the fallback axis
          let wd2 = null, wov = -1;
          for (const dc of discs) {
            const ov = dc.r + rr - Math.hypot(at0.sx-dc.x, at0.sy-dc.y) + 2;
            if (ov > wov) { wov = ov; wd2 = dc; }
          }
          // dodge axes: down the own-ink tangent AND its reverse (a deg-240
          // hub's clear anchored bearing is often UP the trunk fan, F1 #263),
          // plus away-from-worst-violator in XZ
          const dirs = [];
          const tx = jdot.tg[i*3], ty = jdot.tg[i*3+1], tz = jdot.tg[i*3+2];
          dirs.push([tx, ty, tz], [-tx, -ty, -tz]);
          if (wd2) {
            let ax = jdot.pos[i*3]-wd2.wx, az = jdot.pos[i*3+2]-wd2.wz;
            const al = Math.hypot(ax, az);
            if (al > 1e-6) dirs.push([ax/al, 0, az/al]);
            else dirs.push([0.8, 0, 0.6]);
          }
          // the census law (emptySt): a dot is lawful while it sits within
          // 20px of RENDERED conduit ink (any servedEnd sample, not just its
          // own chain terminus). So the slide may walk a dot anywhere ALONG
          // the ink network, but every accepted candidate must stay
          // anchored to it — a dot that detaches from all ink reads as an
          // empty station, which is worse than sharing a box's halo.
          const anchored = (sx, sy) => {
            for (let ai = 0; ai < anchors.length; ai += 2)
              if (Math.hypot(anchors[ai]-sx, anchors[ai+1]-sy) < 20) return true;
            return false;
          };
          const worstAt = (sx, sy) => {
            let w = 1e9;
            for (const dc of discs) {
              const g = Math.hypot(sx-dc.x, sy-dc.y) - dc.r - rr;
              if (g < w) w = g;
            }
            return w;   // min clearance; >= 2 is clear
          };
          let okBest = null, capBest = null;
          for (const [ux, uy, uz] of dirs) {
            // dead-on-screen axes cannot help (topdown −Y): skip them
            const p1c = clearAt(jdot.pos[i*3]+ux*3, jdot.pos[i*3+1]+uy*3, jdot.pos[i*3+2]+uz*3);
            const p0c = clearAt(jdot.pos[i*3], jdot.pos[i*3+1], jdot.pos[i*3+2]);
            if (Math.hypot(p1c.sx-p0c.sx, p1c.sy-p0c.sy) < 0.5) continue;
            for (let s = 1; s <= 40; s++) {
              const cx2 = jdot.pos[i*3] + ux*3*s, cy2 = jdot.pos[i*3+1] + uy*3*s, cz2 = jdot.pos[i*3+2] + uz*3*s;
              const cc = clearAt(cx2, cy2, cz2);
              if (!anchored(cc.sx, cc.sy)) continue;   // off-ink: would read as empty station
              const disp = Math.hypot(cc.sx-at0.sx, cc.sy-at0.sy);
              if (cc.ok) {
                if (!okBest || disp < okBest.disp) okBest = { disp, x: cx2, y: cy2, z: cz2 };
                break;
              }
              const clr = worstAt(cc.sx, cc.sy);
              if (!capBest || clr > capBest.clr) capBest = { clr, x: cx2, y: cy2, z: cz2 };
            }
          }
          // no anchored clear bearing: leave the dot at its true position —
          // it keeps its station, and the box halo overlap is the lesser
          // evil (never an empty-station regression).
          const pick = okBest || capBest;
          if (pick) { qx = pick.x; qy = pick.y; qz = pick.z; }
          const o16 = i*16;
          a[o16+12] = qx; a[o16+13] = qy; a[o16+14] = qz;
          dirty = true;
        } else {
          const o16 = i*16;
          if (a[o16+12] !== jdot.pos[i*3] || a[o16+13] !== jdot.pos[i*3+1] || a[o16+14] !== jdot.pos[i*3+2]) {
            a[o16+12] = jdot.pos[i*3]; a[o16+13] = jdot.pos[i*3+1]; a[o16+14] = jdot.pos[i*3+2];
            dirty = true;
          }
        }
      }
      const f = rT / jdot.r[i];
      if (Math.abs(f - 1) > 0.06) {
        const o = i * 16;
        a[o] *= f; a[o+5] *= f; a[o+10] *= f;   // uniform sphere scale
        jdot.r[i] = rT;
        dirty = true;
      }
    }
    if (dirty) jdot.mesh.instanceMatrix.needsUpdate = true;
  }
  // NO EXPLANATION WITHOUT PRESENCE (sighting #11): the pinned card's
  // lifetime is frame-synced to its anchor's rendered state — chain keys
  // must carry ink (inkKeys), bollards must render (jdot.r), plain wires
  // keep at least one lit endpoint. Anchor gone -> card hides, no exceptions.
  if (wireTipAnchor && wireTipEl.style.display !== "none") {
    const a = wireTipAnchor;
    let present = true;
    if ((a.kind === "trunk" || a.kind === "jleg") && a.k !== undefined)
      present = inkKeys.has(String(a.k));
    else if (a.__ji !== undefined)
      present = (jdot.r[a.__ji] || 0) > 0.001;
    else if (a.kind === "wire" && a.a !== undefined && a.b !== undefined)
      present = (alphaTgt[a.a] || 0) > 0.5 || (alphaTgt[a.b] || 0) > 0.5;
    if (!present) hideWireTip();
  }
  updateBallPin();   // [issue #82] ball-surface pin overlay, frame-synced
  // chevron aim is camera-dependent: recompute every frame
  aimArrows();
  // camera tween (focus / back-stack); a user drag cancels it
  if (camTween) {
    const u = Math.min(1, (performance.now() - camTween.t0) / camTween.dur);
    const e = 1 - Math.pow(1 - u, 3);
    controls.target.lerpVectors(camTween.fromT, camTween.toT, e);
    camera.position.lerpVectors(camTween.fromC, camTween.toC, e);
    if (u >= 1) camTween = null;
  }
  // focus-neighborhood compaction ease: pos walks from the saved layout to
  // the compacted targets (cubic ease-out); syncEdgePos re-derives the fan
  // per frame so wires stay attached while the set pulls together
  if (compactAnim) {
    const u = Math.min(1, (performance.now() - compactAnim.t0) / compactAnim.dur);
    const e = 1 - Math.pow(1 - u, 3);
    for (const i of compactIdx) {
      pos[i*3]   = posSaved[i*3]   + (compactTgt[i*3]   - posSaved[i*3])   * e;
      pos[i*3+1] = posSaved[i*3+1] + (compactTgt[i*3+1] - posSaved[i*3+1]) * e;
      pos[i*3+2] = posSaved[i*3+2] + (compactTgt[i*3+2] - posSaved[i*3+2]) * e;
    }
    syncEdgePos();
    syncSemAff();   // #279: affinity rows track the eased layout with the chords
    if (focusArcs && focusArcs.lines.visible) rebuildFocusWires();
    // fn boxes orbit owner spheres — park the layer while the spheres
    if (fnMesh) fnMesh.visible = false;
    if (fnStalks) fnStalks.visible = false;
    if (fnLines) fnLines.visible = false;
    // spheres and wires lerp under the chevrons: occluder geometry moves
    _arrowOcclDirty = true; _sfDirty = true;
    if (u >= 1) { compactAnim = null; applyVisibility(); }
  }
  // node alpha eases toward its target so filter/focus changes fade in
  // (visibility decisions read alphaTgt, so the fade is purely visual);
  // while any value still eases the sphere scales change geometry, which
  // the chevron occlusion cache must see
  for (let i = 0; i < N; i++) {
    const d = alphaTgt[i] - alphaArr[i];
    if (Math.abs(d) >= 0.003) { alphaArr[i] = alphaArr[i] + d * 0.15; _arrowOcclDirty = true; _sfDirty = true; }
    else alphaArr[i] = alphaTgt[i];
    const hsT = i === hovered ? 1.8 : 1;
    const dh = hsT - hoverScale[i];
    if (Math.abs(dh) >= 0.004) { hoverScale[i] = hoverScale[i] + dh * 0.18; _arrowOcclDirty = true; _sfDirty = true; }
    else hoverScale[i] = hsT;
  }
  syncFileMesh();
  // idle spin pauses while the pointer is down over the canvas or a fn box
  // is hovered; the cbSpin checkbox turns it off entirely
  controls.autoRotate = !(pointerDown && overCanvas) && hoveredFn < 0 && spinEnabled;
  controls.update();
  updateHubs();
  updateClusterLabs();
  updateEdgeLabels();
  updateXtLabels();
  updateFocusLabels();
  // hub ring: marks the focused node so the budgeted wire fan reads as
  // radiating from ONE subject; hidden in overview. Search focus fills
  // query, not focusSeeds — fall back to the strongest level-0 node.
  let ringHi = -1;
  if (focusActive) {
    if (focusSeeds.size) ringHi = focusSeeds.values().next().value;
    else
      for (let i = 0; i < N; i++)
        if (level[i] === 0 && !supMem[i] &&
            (ringHi < 0 || degree[i] > degree[ringHi])) ringHi = i;
  }
  if (ringHi >= 0) {
    if (!hubRing) {
      hubRing = new THREE.Mesh(
        new THREE.RingGeometry(1, 1.12, 48),
        new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true,
          opacity: 0.28, side: THREE.DoubleSide, depthWrite: false,
          blending: THREE.AdditiveBlending }));
      hubRing.renderOrder = 2;
      scene.add(hubRing);
    }
    hubRing.scale.setScalar(sphR(ringHi) + 4);
    hubRing.position.set(pos[ringHi*3], pos[ringHi*3+1], pos[ringHi*3+2]);
    hubRing.visible = true;
  } else if (hubRing) hubRing.visible = false;
  // hub ring billboards toward the camera every frame
  if (hubRing && hubRing.visible) hubRing.quaternion.copy(camera.quaternion);
  // DYNAMIC NEAR PLANE (user report: junction legs/trunks pop out mid-view
  // when zooming in — the static near=1 swallowed corridor geometry passing
  // close to the camera). Tie near to the orbit distance with hysteresis so
  // the projection matrix isn't rebuilt every frame; far stays 20000 (depth
  // precision is fine: near tracks dist*0.01, ratio bounded per pose).
  const camD = camera.position.distanceTo(controls.target);
  const wantNear = Math.max(0.01, camD * 0.01);
  if (Math.abs(camera.near - wantNear) > wantNear * 0.25) {
    camera.near = wantNear; camera.updateProjectionMatrix();
  }
  renderer.render(scene, camera);
  requestAnimationFrame(tick);
}

// frame camera on the graph centroid + bounding radius
function graphBounds() {
  const c = new THREE.Vector3(), lo = new THREE.Vector3(1e9,1e9,1e9), hi = new THREE.Vector3(-1e9,-1e9,-1e9);
  for (let i = 0; i < N; i++) {
    const p = new THREE.Vector3(pos[i*3], pos[i*3+1], pos[i*3+2]);
    c.add(p); lo.min(p); hi.max(p);
  }
  c.divideScalar(N || 1);
  return { center: c, radius: lo.distanceTo(hi) * 0.5 };
}
function frameGraph() {
  const b = graphBounds();
  const dir = new THREE.Vector3(camera.position.x - controls.target.x,
    camera.position.y - controls.target.y,
    camera.position.z - controls.target.z);
  if (dir.lengthSq() < 1) dir.set(0.42, 0.5, 0.76).normalize(); // elevated 3/4 view
  dir.normalize();
  camera.position.copy(b.center).addScaledVector(dir, Math.max(420, b.radius * 1.55));
  controls.target.copy(b.center);
  // perceptual recenter: the elevated 3/4 view + left info panel bias the
  // mass low-left on screen; aim slightly below the bbox center so the
  // galaxy lands mid-canvas
  const lift = b.radius * 0.06;
  camera.position.y -= lift;
  controls.target.y -= lift;
  // LOD threshold tracks the framing distance so overview stays overview
  // regardless of graph size
  lodDist = Math.max(420, b.radius * 1.1);
  // fog scales with the layout: an absolute density tuned for one graph
  // size fogs out every far node once the relax inflates the layout (the
  // "spheres render dim" regression). Visibility ~ 6 framing distances.
  scene.fog.density = 0.17 / Math.max(420, b.radius * 1.55);
}

// frame only the nodes the filters still show (cluster/dir isolates) — the
// selected island(s) deserve the camera, not a whole-galaxy view with a
// bright corner. Tweened: this is a navigation act, not a boot-time snap.
function frameVisible() {
  const p = new THREE.Vector3(1e9, 1e9, 1e9), q = new THREE.Vector3(-1e9, -1e9, -1e9);
  let n = 0;
  for (let i = 0; i < N; i++) {
    if (alphaTgt[i] < 0.5) continue;   // targets, not the eased display values
    p.set(Math.min(p.x, pos[i*3]), Math.min(p.y, pos[i*3+1]), Math.min(p.z, pos[i*3+2]));
    q.set(Math.max(q.x, pos[i*3]), Math.max(q.y, pos[i*3+1]), Math.max(q.z, pos[i*3+2]));
    n++;
  }
  if (!n) return;
  const c = p.clone().add(q).multiplyScalar(0.5);
  const r = Math.max(120, p.distanceTo(q) * 0.5);
  const dir = new THREE.Vector3().subVectors(camera.position, controls.target);
  if (dir.lengthSq() < 1) dir.set(0.42, 0.5, 0.76);
  dir.normalize();
  tweenCamTo(c, c.clone().addScaledVector(dir, Math.max(320, r * 1.8)));
}

"""
