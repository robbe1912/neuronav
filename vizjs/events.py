# vizjs/events.py — rung 16/17 of the template join (issues
# #86 phase-3 / #299 A): pointer/keyboard events, resize3D. Moved VERBATIM from viz.py —
# every edit here changes graph.html: regen the bake (the standing
# template law; test_viz refuses a bake older than viz.py/vizjs).
_JS_EVENTS = r"""const greyLabelEls = ["hubs", "clabs", "flabs"].map(id => document.getElementById(id));
function greyLabelsDim(on) {
  greyLabelEls.forEach(el => { el.style.opacity = on ? EMPHASIS.LABEL_DIM_OPACITY : ""; });
}
function hoverGrey(i) {
  if (i === hoverGreyIdx) return;
  if (pointerDown || focusActive || deadOnly) {
    if (hoverGreyIdx >= 0) { hoverGreyIdx = -1; applyVisibility(); greyLabelsDim(false); }
    return;
  }
  if (i >= 0 && alphaTgt[i] <= 0.5) i = -1;   // can't grey around a ghost
  if (i < 0) {
    if (hoverGreyIdx >= 0) { hoverGreyIdx = -1; applyVisibility(); greyLabelsDim(false); }
    return;
  }
  // full baseline first (restores any previous grey), then dim to 0.12
  hoverGreyIdx = -1;
  applyVisibility();
  greyLabelsDim(true);
  hoverGreyIdx = i;
  const lit = new Set([i]);
  (adj[i] || []).forEach(j => lit.add(j));
  for (let j = 0; j < N; j++)
    if (!lit.has(j) && alphaTgt[j] > EMPHASIS.DIM_ALPHA) alphaTgt[j] = EMPHASIS.DIM_ALPHA;
  links.forEach((l, k) => {
    if (alphaTgt[l.s] > 0.5 && alphaTgt[l.t] > 0.5) return;
    const b = bucketOf[k], tgt = bucketColIB[b].array;
    // grey = 12% of the edge's own color; collapsed/black links stay black.
    // Buffers are SLOT-laid (applyVisibility writes at slotOf[i]*6 / hwSlot):
    // indexing by link index k*6 dimmed whatever edge owned that slot.
    if (hwSlot[k] >= 0) {
      for (let o6 = hwSlot[k]; o6 < hwSlot[k] + 96; o6 += 6) {
        tgt[o6] *= EMPHASIS.DIM_ALPHA; tgt[o6+1] *= EMPHASIS.DIM_ALPHA; tgt[o6+2] *= EMPHASIS.DIM_ALPHA;
        tgt[o6+3] *= EMPHASIS.DIM_ALPHA; tgt[o6+4] *= EMPHASIS.DIM_ALPHA; tgt[o6+5] *= EMPHASIS.DIM_ALPHA;
      }
    } else {
      const o6 = slotOf[k] * 6;
      tgt[o6] *= EMPHASIS.DIM_ALPHA; tgt[o6+1] *= EMPHASIS.DIM_ALPHA; tgt[o6+2] *= EMPHASIS.DIM_ALPHA;
      tgt[o6+3] *= EMPHASIS.DIM_ALPHA; tgt[o6+4] *= EMPHASIS.DIM_ALPHA; tgt[o6+5] *= EMPHASIS.DIM_ALPHA;
    }
    bucketColIB[b].needsUpdate = true;
  });
}

renderer.domElement.addEventListener("pointermove", e => {
  // raycast NDC + screen-space picks are CANVAS-relative: with the map pane
  // owning the right edge, the canvas is no longer the whole window
  const cr = renderer.domElement.getBoundingClientRect();
  const ndcX = (e.clientX - cr.left) / cr.width, ndcY = (e.clientY - cr.top) / cr.height;
  mouse.x = ndcX*2-1; mouse.y = -ndcY*2+1;
  raycaster.setFromCamera(mouse, camera);
  const targets = fnMesh ? [fileMesh, fnMesh] : [fileMesh];
  const hits = raycaster.intersectObjects(targets);
  const hovPrev = hovered, hovFnPrev = hoveredFn;
  hovered = -1; hoveredFn = -1;
  // skip invisible nodes: filtered-out tests/tools keep raycast geometry,
  // but hovering a ghost must not pop a tooltip. Among visible hits, pick
  // by SCREEN-SPACE accuracy (cursor distance vs projected radius), not
  // depth — at high spread a foreground sphere's rim otherwise steals the
  // pick from the node the user is actually pointing at (occlusion).
  const px = ndcX * cr.width, py = ndcY * cr.height;
  let bestPx = 18;   // cursor forgiveness radius in pixels
  for (const h of hits) {
    if (h.object === fnMesh) {
      const fm = fnMeta[h.instanceId];
      if (!fm || alphaTgt[fm.file] <= 0.5 || (fm.agg && !fm.count)) continue;
      const v = _pickV.set(fm.p[0], fm.p[1], fm.p[2]).project(camera);
      const sx = (v.x*0.5+0.5)*cr.width, sy = (-v.y*0.5+0.5)*cr.height;
      const dist = Math.hypot(sx-px, sy-py);
      if (dist < bestPx) { bestPx = dist; hoveredFn = h.instanceId; hovered = -1; }
    } else if (h.object === fileMesh && alphaTgt[h.instanceId] > 0.5) {
      const v = _pickV.set(pos[h.instanceId*3], pos[h.instanceId*3+1], pos[h.instanceId*3+2]).project(camera);
      const sx = (v.x*0.5+0.5)*cr.width, sy = (-v.y*0.5+0.5)*cr.height;
      const dist = Math.hypot(sx-px, sy-py);
      if (dist < bestPx) { bestPx = dist; hovered = h.instanceId; hoveredFn = -1; }
    }
  }
  // hover identity feeds syncFileMesh (hoverScale ease + fnOwner lift)
  if (hovered !== hovPrev || hoveredFn !== hovFnPrev) _sfDirty = true;
  if (hoveredFn >= 0) fnStalkUpdate(fnMeta[hoveredFn].file, fnMeta[hoveredFn].p);
  else fnStalkHide();
  hoverGrey(hoveredFn >= 0 ? fnMeta[hoveredFn].file : hovered);
  let txt = null;
  if (hoveredFn >= 0) {
    const fm = fnMeta[hoveredFn];
    txt = fm.count ? nodes[fm.file].path + " :: " + fm.count + " calls"
                   : nodes[fm.file].path + " :: " + fm.name;
  } else if (hovered >= 0) {
    txt = nodes[hovered].path;
    const s = edgeSummary(hovered);
    if (s) txt += "\n" + s;
    const path = pathToSeed(hovered);
    if (path) {
      const shown = path.length <= 5 ? path.join(" → ")
        : path.slice(0, 2).join(" → ") + " → …(" + (path.length - 3) + ")→ " + path[path.length - 1];
      txt += "\n→ seed: " + shown;
    }
  }
  // node-hover reveal: refresh the edge pass when the hovered node changes
  // during focus (same contract as wire hover -> applyVisibility)
  if (focusActive && hovered !== hoverVisNode) {
    hoverVisNode = hovered;
    applyVisibility();
  }
  // wire hover: no node under the cursor -> raycast the edge buckets and
  // name the strongest named wire on that file pair ('A::sfn -> B::dfn').
  // Enabled during focus too: the hub edge budget ghosts most wires, so
  // hover is the on-demand reveal — the hovered wire re-lights (budget
  // bypass) AND shows its tooltip. Suppressed while dragging. Filtered/
  // ghost edges never match.
  if (!txt && !pointerDown) {
    const m = pickWireMeta(e);
    const hli = m && m.kind === "link" ? m.li : -1;
    if (focusActive && hli !== hoverEdgeLi) {
      hoverEdgeLi = hli;
      applyVisibility();   // re-runs the budget pass with the hover bypass
    }
    if (m) {
      if (m.kind === "link") {
        const pair = strongPair(links[m.li]);
        if (pair.length) {
          txt = nodes[pair[0][1]].label + "::" + pair[0][2] +
            " → " + nodes[pair[0][3]].label + "::" + pair[0][4];
        }
      } else if (m.kind === "trunk") {
        txt = "🚌 bus " + nodes[m.sf].label + " → " + nodes[m.tf].label +
          "  (" + (m.mates || []).length + " wires)";
      } else if (m.kind === "jleg" || m.kind === "sub" || m.kind === "jof") {
        const pp = m.k ? String(m.k).split("|") : null;
        const mates = riderWiresOf(m.fi, pp ? +pp[2] : -1);
        const srcs = [...new Set(mates.map(w =>
          fnMeta[fnMeta[w.a].file === m.fi ? w.a : w.b].name))];
        const dests = [...new Set(mates.map(w =>
          fnMeta[fnMeta[w.a].file === m.fi ? w.b : w.a].file))];
        txt = (m.kind === "jleg" ? "🚌 junction leg · " : "🚌 bus fan · ") +
          nodes[m.fi].label + " · " +
          srcs.slice(0, 3).join(", ") + (srcs.length > 3 ? "…" : "") +
          "  →  " + dests.slice(0, 2).map(fi2 => nodes[fi2].label).join(", ") +
          (dests.length > 2 ? " +" + (dests.length - 2) : "") +
          "  · " + mates.length + " wires";
      } else if (m.kind === "station") {
        const mates = riderWiresOf(m.fi, -1);
        const dests = [...new Set(mates.map(w =>
          fnMeta[fnMeta[w.a].file === m.fi ? w.b : w.a].file))];
        txt = "🚌 bus · " + nodes[m.fi].label + " · " + mates.length + " wires → " +
          dests.slice(0, 3).map(fi2 => nodes[fi2].label).join(", ") +
          (dests.length > 3 ? " +" + (dests.length - 3) : "");
      } else {
        const a = fnMeta[m.a], b = fnMeta[m.b];
        txt = nodes[a.file].label + "::" + a.name + "() → " +
              nodes[b.file].label + "::" + b.name + "()" +
              (m.ln >= 0 ? "  @L" + m.ln : "");
      }
    }
  }
  if (txt) {
    tip.style.display = "block";
    tip.style.left = (e.clientX+14)+"px"; tip.style.top = (e.clientY+14)+"px";
    tip.textContent = txt;
    renderer.domElement.style.cursor = "pointer";
  } else { tip.style.display = "none"; renderer.domElement.style.cursor = "grab"; }
});
// drag-vs-click: OrbitControls uses pointer drags; a release over a node
// after rotating the camera must not select it
let downX = 0, downY = 0;
// auto-spin interaction gating (read by tick)
let pointerDown = false, overCanvas = false;
renderer.domElement.addEventListener("pointerdown", e => {
  downX = e.clientX; downY = e.clientY;
  pointerDown = true;
  fnClosePick();   // canvas input closes the fn picker (map picker parity)
  camTween = null;   // user grab beats the tween
  if (hovered < 0 && hoveredFn < 0) renderer.domElement.style.cursor = "grabbing";
});
// [issue #112] a user zoom beats the tween: OrbitControls handles wheel on
// its own listener (a pointerdown-only cancel let the tick's tween block
// re-lerp the camera over every mid-flight zoom). passive: we never scroll.
renderer.domElement.addEventListener("wheel", () => { camTween = null; },
  { passive: true });
renderer.domElement.addEventListener("pointerup", () => {
  pointerDown = false;
  renderer.domElement.style.cursor = "grab";   // pointermove corrects to pointer over a node
});
renderer.domElement.addEventListener("pointerenter", () => { overCanvas = true; });
renderer.domElement.addEventListener("pointerleave", () => { overCanvas = false; pointerDown = false;
  // [issue #58] the hover tip dies with its pointer context: the pointer
  // left the canvas (onto the pane, the chrome, or out the window) and no
  // further canvas pointermove arrives to clear it. The pin-owned wireTip
  // is not hover state — it lives to dismissal (issue #85).
  tip.style.display = "none";
  // [issue #112] the white stalk is hover ink of the same family: with the
  // pointer gone no pointermove arrives to hide it, so it stayed drawn at
  // the last fn-box coordinates after the cursor left the surface
  fnStalkHide(); hoveredFn = -1; _sfDirty = true; });
// multi-root camera: frame the centroid of all focus seeds at a distance
// set by their spread (single seed falls back to the tight focus)
function focusSeedsCamera() {
  const arr = [...focusSeeds];
  if (arr.length === 1) { focus(arr[0]); return; }
  const c = new THREE.Vector3();
  arr.forEach(i => c.add(new THREE.Vector3(pos[i*3], pos[i*3+1], pos[i*3+2])));
  c.divideScalar(arr.length);
  let r = 120;
  arr.forEach(i => r = Math.max(r, c.distanceTo(
    new THREE.Vector3(pos[i*3], pos[i*3+1], pos[i*3+2]))));
  const dir = new THREE.Vector3(camera.position.x - controls.target.x,
    camera.position.y - controls.target.y,
    camera.position.z - controls.target.z);
  if (dir.lengthSq() < 1) dir.set(0.42, 0.5, 0.76);
  dir.normalize();
  tweenCamTo(c, c.clone().addScaledVector(dir, Math.min(900, 240 + r * 2)));
}
// wires & buses pick in the CAPTURE phase: fn-box labels (.flab) and the
// canvas itself sit above the wires' pixels — without this, clicking a wire
// near the hub opens the fn instead. No wire nearby -> event passes through.
document.addEventListener("pointerdown", e => {
  downX = e.clientX; downY = e.clientY;   // fresh drag-guard origin anywhere
  hideWireTip();   // any new press dismisses the tip (drag, right-click);
}, true);          // a wire click re-shows it right after
document.addEventListener("click", e => {
  if (!focusActive || !fnLines) return;
  // [issues #82/#81/#111] click routing is by surface: only the 3D canvas
  // and the fn-box labels (.flab) sit above the wires' pixels, so a wire
  // may claim a press there — and only there. Every other surface (the 2D
  // map pane + its overlays, the #info panel + its rows, legend/dir chips,
  // crumb buttons, fnPick rows, search results) owns its click outright:
  // this handler runs in the CAPTURE phase, so a stopPropagation below
  // would kill the chrome's own handler before it ever fired — the legend
  // chip (#81) and the #info rows (#111) went dead exactly that way when
  // ink met their click points.
  if (e.target !== renderer.domElement &&
      !(e.target.classList && e.target.classList.contains("flab"))) return;
  if (Math.hypot(e.clientX - downX, e.clientY - downY) > 5) return;
  const wHit = pickWireMeta(e);
  if (!wHit) return;
  // [issue #87] depth decides between wire ink and the fn-hover band.
  // The hover raycast sets hoveredFn for any box the ray physically
  // threads — including dim background boxes under lifted corridor ink
  // — and this handler used to return on hoveredFn alone, stranding
  // every wire pixel behind a threaded box (the ~18px dead zone:
  // camera-density dependent, no pin, no tip). Same law as the file
  // spheres below: ink STRICTLY NEARER than the hovered box claims the
  // press; the box keeps its own face — terminal ink lands at the box's
  // depth (ties resolve to the box), and presses with no ink here at
  // all fall through to the canvas click handler's hoveredFn branch
  // (showFnInfo / openFnPicker) and its .flab label.
  if (hoveredFn >= 0) {
    const fm2 = fnMeta[hoveredFn];
    const bz = new THREE.Vector3(fm2.p[0], fm2.p[1], fm2.p[2])
      .project(camera).z;
    if (pickWireZ >= bz) return;
  }
  // the hub sphere's projected disk covers lifted bus arcs (hover
  // raycast is sphere-only and can't see the arc in front), so depth
  // decides there too.
  if (hovered >= 0) {
    const np = [pos[hovered * 3], pos[hovered * 3 + 1], pos[hovered * 3 + 2]];
    const nz = new THREE.Vector3(np[0], np[1], np[2]).project(camera).z;
    if (pickWireZ >= nz) return;
  }
  e.stopPropagation();   // the label/canvas click handlers stay out
  showWireTip(wHit, e.clientX, e.clientY);
                    // [issue #82] latch the ball-surface pin — only for
                    // clicks that originated on the 3D canvas itself (DOM
                    // .click() events target their element and carry 0,0
                    // coords; the transient tip above must not become a
                    // sticky pin from a UI-chip click).
                    if (e.target === renderer.domElement &&
                        (wHit.kind === "link" || wHit.kind === "wire" ||
                         wHit.kind === "trunk" || wHit.kind === "jleg")) {
                      // [issue #85 owner r3] corridor-complete: a
                      // junction leg stands for its corridor. The leg
                      // key is "L|fi|stationId|subIdx" (it carries no
                      // trunk), so resolve the station and pin its
                      // DOMINANT trunk - most rider wires, ties break
                      // by station order. Deterministic.
                      let latch = null;
                      if (wHit.kind === "jleg") {
                        const sid = +String(wHit.k).split("|")[2];
                        const S = (fnStationsArr || []).find(
                          x => x.fi === wHit.fi && x.id === sid);
                        let bestTk = null, bestN = -1;
                        for (const tk2 of (S ? S.tks : [])) {
                          const tm = trunkMetaMap && trunkMetaMap.get(tk2);
                          const n2 = tm && tm.mates ? tm.mates.length : 0;
                          if (n2 > bestN) { bestN = n2; bestTk = tk2; }
                        }
                        if (bestTk != null)
                          latch = { surface: "ball", kind: "trunk",
                            id: "K|" + bestTk, k: bestTk, menu: "tip",
                            gen: rosterGen };
                      } else {
                        const pinId = wHit.kind === "link" ? "L|" + wHit.li
                          : wHit.kind === "trunk" ? "K|" + wHit.k
                          : "F|" + wHit.a + "|" + wHit.b + "|" + wHit.ln;
                        latch = { surface: "ball", kind: wHit.kind,
                          id: pinId, li: wHit.li, a: wHit.a, b: wHit.b,
                          ln: wHit.ln, k: wHit.k, menu: "tip",
                          gen: rosterGen };
                      }
                      if (latch) {
                        wirePinSet(latch);
                        // [issue #85 owner r2] the tip is pin-owned
                        // from the first frame: the transient
                        // wireDesc the click showed must give way to
                        // the pin's from->to (its "↓ into" wording
                        // has no arrow and outlives stale)
                        let pd2 = null;
                        try { pd2 = pinDesc(latch); } catch (e) { pd2 = null; }
                        if (pd2) {
                          wireTipEl.textContent = pd2;
                          wireTipEl.style.display = "block";
                          wireTipAnchor = latch;
                        }
                      }
                    }
}, true);
renderer.domElement.addEventListener("click", e => {
  if (Math.hypot(e.clientX - downX, e.clientY - downY) > 5) return;
  if (hoveredFn >= 0) {
    // aggregate box ('n×') has no single fn behind it: open the picker over
    // that file's roster instead of showFnInfo with an empty fn name
    const fm = fnMeta[hoveredFn];
    if (fm.count) openFnPicker(fm.file, e.clientX, e.clientY);
    else showFnInfo(hoveredFn);
    return;
  }
  if (hovered >= 0) {
    if (e.shiftKey && focusSeeds.size) {
      // shift-click stacks focus roots (click a selected root to drop it);
      // dropping the LAST root is an implicit "clear" — same scope as Esc
      if (focusSeeds.has(hovered)) {
        focusSeeds.delete(hovered);
        if (!focusSeeds.size) { clearFocus(); return; }
      } else { pushFocusState(); focusSeeds.add(hovered); }
      applyVisibility();
      focusSeedsCamera();
      showInfo(hovered);
      mapCenterOn(hovered);
    } else {
      pushFocusState();
      focusSeeds.clear(); focusSeeds.add(hovered);
      applyVisibility();
      focus(hovered);
      showInfo(hovered);
      mapCenterOn(hovered);
    }
  }
});
function resize3D() {
  const w = glW(), h = innerHeight;
  camera.aspect = w/h; camera.updateProjectionMatrix();
  renderer.setSize(w, h);
  bucketMat.forEach(m => m.resolution.set(w, h));
  if (semMesh) semMesh.material.resolution.set(w, h);
  // fat-line overlays live in screen px too — stale resolution = wrong width
  if (fnLines) fnLines.material.resolution.set(w, h);
  if (fnQuiet) fnQuiet.material.resolution.set(w, h);
  if (focusArcs) focusArcs.mat.resolution.set(w, h);
  _sfDirty = true;   // canvas height feeds the anchor/deg-floor px laws
}
addEventListener("resize", resize3D);
// LOD zoom threshold: crossing it reveals/hides intra-cluster edges at
// overview (filters never re-layout — this only recomputes edge colors)
controls.addEventListener("change", () => {
  const c = camera.position.distanceTo(controls.target) < lodDist;
  if (c !== lodClose) {
    lodClose = c;
    if (!focusSeeds.size) applyVisibility();
  }
});

// apply the overview palette + LOD once at boot (initial buffer fill is
// full-color; this demotes it to the overview state without waiting for
// user interaction)
buildContainment();
applyVisibility();
frameGraph();
renderer.domElement.style.cursor = "grab";
// the map pane ships open — apply the split (canvas size, overlay clamp,
// info shift) once everything it touches exists
setMapVisible(true);
"""
