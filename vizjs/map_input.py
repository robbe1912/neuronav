# vizjs/map_input.py — rung 14/17 of the template join (issues
# #86 phase-3 / #299 A): divider drag + map input. Moved VERBATIM from viz.py —
# every edit here changes graph.html: regen the bake (the standing
# template law; test_viz refuses a bake older than viz.py/vizjs).
_JS_MAP_INPUT = r"""const divider = document.getElementById("divider");
let divRaf = 0;
divider.addEventListener("pointerdown", e => {
  if (!mapVisible) return;
  divider.setPointerCapture(e.pointerId);
  divider.classList.add("drag");
  e.preventDefault();
});
divider.addEventListener("pointermove", e => {
  if (!divider.classList.contains("drag")) return;
  paneW = Math.round(Math.max(PANE_MIN, Math.min(paneMax(), innerWidth - e.clientX)));
  applyPaneW();
  if (!divRaf) divRaf = requestAnimationFrame(() => {
    divRaf = 0;
    resize3D();
    sizeMapPane();
    drawMapPane();   // rAF dirty-flag coalesces paints across drag frames
  });
});
divider.addEventListener("pointerup", () => {
  if (!divider.classList.contains("drag")) return;
  divider.classList.remove("drag");
  try { localStorage.setItem(PANE_KEY, String(paneW)); } catch {}
});
divider.addEventListener("pointercancel", () => divider.classList.remove("drag"));
// click a node rect = the hub-label jump: re-seed focus around that file
const mapToWorld = e => {
  const b = mapPane.getBoundingClientRect();
  return { x: (e.clientX - b.left) / mapZ + mapPX, y: (e.clientY - b.top) / mapZ + mapPY };
};
mapPane.addEventListener("wheel", e => {
  if (!mapVisible) return;
  e.preventDefault();
  mapClosePick();   // canvas input closes the picker [F11]
  const b = mapPane.getBoundingClientRect();
  const cx = e.clientX - b.left, cy = e.clientY - b.top;
  const wx = cx / mapZ + mapPX, wy = cy / mapZ + mapPY;
  mapZ = Math.max(0.2, Math.min(3, mapZ * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
  mapInkEval();   // hysteresis re-arm at the new zoom (paint-only tier)
  mapLodEval();   // [#77] band re-arm at the new zoom (paint-only tier)
  mapPX = wx - cx / mapZ;
  mapPY = wy - cy / mapZ;
  mapClampView();
  drawMapPane();
}, { passive: false });
mapPane.addEventListener("pointerdown", e => {
  if (!mapVisible) return;
  mapClosePick();   // canvas input closes the picker [F11]
  mapDrag = { x: e.clientX, y: e.clientY, px: mapPX, py: mapPY, moved: false };
  const db = mapPane.getBoundingClientRect();
  mapDownPt = { sx: e.clientX, sy: e.clientY,
                wx: (e.clientX - db.left) / mapZ + mapPX,
                wy: (e.clientY - db.top) / mapZ + mapPY };
  mapJitterHit = null;
  mapPane.setPointerCapture(e.pointerId);
});
mapPane.addEventListener("pointerup", e => {
  if (!mapDrag) return;
  mapDragged = mapDrag.moved;
  mapJitterHit = null;
  // [issue #84] skeptic #2a: real hands drift 5-12px between down/up.
  // A press that BEGAN on wire or spine ink and traveled <12px screen
  // is a pick, not a pan - resolve against the press origin (world
  // coords are stable under the sub-12px pan that already applied).
  if (mapDragged && mapDownPt && mapLayout) {
    if (Math.hypot(e.clientX - mapDownPt.sx, e.clientY - mapDownPt.sy) < 12) {
      const onInk = (wx, wy) => {
        if (mapWireAt(wx, wy) >= 0) return true;
        const tol2 = 10 / mapZ;
        for (const sp of mapLayout.spines) {
          if (!sp.pts || sp.pts.length < 2) continue;
          for (let k = 1; k < sp.pts.length; k++)
            if (segDist(wx, wy, sp.pts[k-1][0], sp.pts[k-1][1],
                           sp.pts[k][0], sp.pts[k][1]) < tol2) return true;
        }
        return false;
      };
      // resolve BOTH rescue classes at the press origin: the ink
      // latch picks the wire the press began on, and the slop click
      // keeps the header the hand aimed at - resolving at the release
      // point let 8-11px drifts exit the thin header band and die
      // (skeptic #22: measured boundary 6/8px per-axis, not 10)
      mapJitterHit = { x: mapDownPt.wx, y: mapDownPt.wy };
      if (!onInk(mapDownPt.wx, mapDownPt.wy)) {
        // [owner r5] click slop: the jitter latch rescues INK picks,
        // but every other target died on >4px drift - the whole click
        // was swallowed as a pan, so card headers stopped refocusing
        // under real hands. Undo the sub-slop pan and let the click
        // resolve through the normal path.
        mapPX = mapDrag.px; mapPY = mapDrag.py;
        mapClampView(); drawMapPane();
        mapDragged = false;
      }
    }
  }
  mapDrag = null;
});
mapPane.addEventListener("click", e => {
  if (mapDragged) {
    mapDragged = false;
    if (!mapJitterHit) return;   // it was a pan, not a pick
  }
  clearTimeout(mapRefocusTimer);
  // 0. screen-space furniture first: vars chip [F10]
  const mb = mapPane.getBoundingClientRect();
  const mpx = e.clientX - mb.left, mpy = e.clientY - mb.top;
  const cr = mapVarsChipRect;
  if (cr && mpx >= cr.x && mpx <= cr.x + cr.w && mpy >= cr.y && mpy <= cr.y + cr.h) {
    mapVarsOn = !mapVarsOn;
    drawMapPane();
    return;
  }
  const w = mapJitterHit || mapToWorld(e);
  mapJitterHit = null;
  const pp = mapLodPinPair();   // [#77] unbundled pair: its stroke unpickable
  // 1. bundle chip -> pinned enumeration list (section 7)
  const ci = mapChipAt(w.x, w.y);
  if (ci >= 0) { mapOpenList(ci); return; }
  // named wire vs boxes (L2): a wire within the 6px screen tolerance wins
  // over box-header refocus - wires visibly ride box borders (the failing
  // aim sat exactly ON a box's bottom edge) - unless the click genuinely
  // lands inside the header band (title strip = top NH px; a roster-less
  // box is all header). Row/picker zones yield to the wire too: the
  // tolerance is screen-fine and wires terminate at row pins on borders.
  const wi = mapWireAt(w.x, w.y);
  if (wi >= 0 && mapLayout) {
    let inHeader = false;
    for (let k = mapRects.length - 1; k >= 0; k--) {
      const rc = mapRects[k];
      if (w.x < rc.x || w.x > rc.x + rc.w || w.y < rc.y || w.y > rc.y + rc.h) continue;
      inHeader = w.y < rc.y + ((rc.rows.length || rc.more) ? NH : rc.h);
      break;
    }
    // [issue #84] skeptic #2b: a stroke riding the header band is still
    // the user's aim - the header only wins when the press is >4px screen
    // CLEAR of the stroke (mapWireLastD set by the mapWireAt call above)
    if (!inHeader || mapWireLastD < 4 / mapZ) {
      const wr = mapLayout.wires[wi];
      if (wr.ty === "var") showInfo(wr.df);   // member target is not a fn
      else mapShowFn(wr.df, wr.dfn);
      // [issue #82] the click that opens the wire's menu latches the pin
      // (single pin: a later selection replaces this one)
      wirePinSet({ surface: "map", kind: "wire", id: wireKeyOf(wr), menu: "info" });
      return;
    }
  }
  // [issue #82] trunk corridors (map surface): trunk/tap spines ride in
  // the bands between boxes — a click within the wire tolerance latches
  // the enumerated set (trunk + its taps) and opens the bus card, the
  // map-side twin of the 3D trunk click parity
  if (mapLayout) {
    // [issue #84] 10px band to match mapWireAt; singles/leaders (no .hub)
    // are pickable too - they are visible pair strokes, and #84 wants a
    // hit target on every visible segment. Ungated by design: spines
    // paint un-gated at every zoom (parity, see mapWireAt).
    const tol = 10 / mapZ;
    let th = -1, td = tol;
    mapLayout.spines.forEach((sp, six) => {
      if (!sp.pts || sp.pts.length < 2) return;
      // [#77] pick-vs-paint parity: band-2-folded intra-cluster corridors
      // and the unbundled pair's hidden stroke carry no hit target
      if (mapLodBand === 2 && !sp.hub &&
          nodes[sp.s].cluster === nodes[sp.t].cluster) return;
      if (pp && sp.pair === pp && !sp.hub) return;
      // [#61] pick-vs-paint parity: taps folded below the legibility
      // floor carry no hit target
      if (sp.hub === "tap" && mapZ < MAP_TAP_FOLD_Z) return;
      for (let k = 1; k < sp.pts.length; k++) {
        const d = segDist(w.x, w.y, sp.pts[k-1][0], sp.pts[k-1][1],
                             sp.pts[k][0], sp.pts[k][1]);
        if (d < td) { td = d; th = six; }
      }
    });
    if (th >= 0) {
      const sp = mapLayout.spines[th];
      if (!sp.hub) {
        // [issue #84] single/leader pair stroke: pin it like a wire; the
        // fn panel stays closed (spines carry pair identity, not fn rows)
        wirePinSet({ surface: "map", kind: "wire",
          id: "S|" + sp.s + "|" + sp.t + "|" + sp.wty,
          s: sp.s, t: sp.t, wty: sp.wty, menu: "tip" });
        wireTipEl.textContent = "wire pair " + nodes[sp.s].label +
          " \u2192 " + nodes[sp.t].label + "  \u00d7" + (sp.flowSum || 1);
        wireTipEl.style.display = "block";
        const padS = 14;
        let txS = e.clientX + padS, tyS = e.clientY + padS;
        const rS = wireTipEl.getBoundingClientRect();
        if (txS + rS.width > innerWidth - 8) txS = e.clientX - rS.width - padS;
        if (tyS + rS.height > innerHeight - 8) tyS = e.clientY - rS.height - padS;
        wireTipEl.style.left = txS + "px"; wireTipEl.style.top = tyS + "px";
        wireTipAnchor = null;
        return;
      }
      wirePinSet({ surface: "map", kind: "trunk",
        id: "T|" + sp.s + "|" + sp.t + "|" + sp.wty,
        s: sp.s, t: sp.t, wty: sp.wty, menu: "tip" });
      wireTipEl.textContent = "\ud83d\ude8c bus " + nodes[sp.s].label +
        " \u2192 " + nodes[sp.t].label +
        (sp.trunkW ? "  \u00d7" + sp.trunkW : "");
      wireTipEl.style.display = "block";
      const pad = 14;
      let tx2 = e.clientX + pad, ty2 = e.clientY + pad;
      const r2 = wireTipEl.getBoundingClientRect();
      if (tx2 + r2.width > innerWidth - 8) tx2 = e.clientX - r2.width - pad;
      if (ty2 + r2.height > innerHeight - 8) ty2 = e.clientY - r2.height - pad;
      wireTipEl.style.left = tx2 + "px"; wireTipEl.style.top = ty2 + "px";
      wireTipAnchor = null;   // transient card: the pin outlives it
      return;
    }
  }
  // 2. boxes: roster row (L3) / "+N more" (picker) / header (click refocus).
  // Tested before wires: boxes paint on top of them. Rects iterate
  // topmost-drawn first so an overlap resolves to the box the user sees.
  for (let k = mapRects.length - 1; k >= 0; k--) {
    const rc = mapRects[k];
    if (w.x < rc.x || w.x > rc.x + rc.w || w.y < rc.y || w.y > rc.y + rc.h) continue;
    if (rc.rows.length && mapLodBand < 1) {   // [#77] folded rows unpickable (parity)
      if (rc.more && w.y >= rc.more.y0 && w.y < rc.more.y1) { mapOpenPicker(rc); return; }
      for (const row of rc.rows) {
        if (w.y >= row.y0 && w.y < row.y1) {
          // L3 [F12]: local dim only, rows frozen, NO re-seed
          mapFrozenIx = mapFrozenIx === rc.i ? -1 : rc.i;
          drawMapPane();
          return;
        }
      }
    }
    // Header click = refocus (220ms so a dblclick can cancel into an
    // expand toggle). A wire within tolerance wins over the header
    // unless the point is genuinely inside the header band: wires route
    // through box bodies, and an L2 target must not be stolen by a box
    // it merely passes under.
    if (w.y >= rc.y + NH) continue;
    mapRefocusTimer = setTimeout(() => {
      pushFocusState(); showInfo(rc.i); focusSeeds.clear(); focusSeeds.add(rc.i);
      applyVisibility(); focus(rc.i);
    }, 220);
    return;
  }
  // void: unpin the list, close the picker, drop the freeze
  if (mapListEl.style.display === "block" || mapFrozenIx >= 0) mapOvCloseOne();
  // [issue #84] skeptic #6: uniform dismissal - a void click clears any
  // remaining pin (info/tip pins used to survive it while list pins died)
  if (wirePin) wirePinClear();
});
mapPane.addEventListener("dblclick", e => {
  if (!mapVisible) return;
  clearTimeout(mapRefocusTimer);
  const w = mapToWorld(e);
  for (const rc of mapRects) {
    if (w.x >= rc.x && w.x <= rc.x + rc.w && w.y >= rc.y && w.y <= rc.y + rc.h) {
      const open = mapLayout && mapLayout.expandedSet.has(rc.i);
      mapExpandUser.set(rc.i, !open);
      drawMapPane();
      return;
    }
  }
});
mapPane.addEventListener("pointermove", e => {
  if (!mapVisible) return;
  if (mapDrag) {
    const dx = e.clientX - mapDrag.x, dy = e.clientY - mapDrag.y;
    if (Math.hypot(dx, dy) > 4) mapDrag.moved = true;
    mapPX = mapDrag.px - dx / mapZ;
    mapPY = mapDrag.py - dy / mapZ;
    mapClampView();
    drawMapPane();
    // [issue #58] a press ends the hover context: the pane pans under the
    // pointer, so a parked L1 tip describes a wire that already slid away
    mapTipHide();
    return;
  }
  const w = mapToWorld(e);
  const ci = mapChipAt(w.x, w.y);
  const wi = ci < 0 && mapFrozenIx < 0 ? mapWireAt(w.x, w.y) : -1;
  if (mapHover !== wi) { mapHover = wi; drawMapPane(); }
  mapHoverChip = ci;
  mapPane.style.cursor = ci >= 0 || wi >= 0 || mapRects.some(rc =>
    w.x >= rc.x && w.x <= rc.x + rc.w && w.y >= rc.y && w.y <= rc.y + rc.h)
    ? "pointer" : "default";
  if (wi >= 0) {   // map-local tooltip (L1)
    mapTipEl.textContent = mapTipText(mapLayout.wires[wi]);
    mapTipEl.style.display = "block";
    const b = mapPane.getBoundingClientRect();
    mapTipEl.style.left = Math.max(4, Math.min(e.clientX - b.left + 14, mapPane.clientWidth - 290)) + "px";
    mapTipEl.style.top = Math.max(4, Math.min(e.clientY - b.top + 10,
      (b.height || innerHeight) - 100)) + "px";
  } else mapTipHide();
});
// [issue #58] pointer context ends -> the L1 hover tip dies with it:
// leaving the pane (pointer onto the 3D canvas or the chrome) stops
// mapPane pointermove, and window blur takes the pointer away entirely —
// neither may leave a hover tip behind. Pin-owned cards are untouched.
// [issue #113] the wire hover is the same pointer-context ink: it must die
// when the pointer leaves the pane, not linger dimming the diagram (the 2D
// analog of the #112-3 stalk law) - a pointer transiting the pane toward the
// search box otherwise latches a hover pick that dims the NEXT focus.
mapPane.addEventListener("pointerleave", () => {
  if (!mapVisible) return;
  mapTipHide();
  if (mapHover !== -1) { mapHover = -1; drawMapPane(); }
});
addEventListener("blur", () => {
  tip.style.display = "none";   // 3D hover tip dies with the surface
  mapTipHide();
});
addEventListener("resize", () => { if (mapVisible) { sizeMapPane(); drawMapPane(); } });
// test/debug surface: named-wire map introspection (harness contract)
const mapInfo = () => {
  if (!mapLayout) return null;
  const w0 = mapLayout.wires[0];
  let probe = null;
  if (w0) {   // midpoint of wire 0 in screen space (click-target probe)
    const m = w0.pts[Math.floor(w0.pts.length / 2)];
    probe = { sx: (m[0] - mapPX) * mapZ, sy: (m[1] - mapPY) * mapZ };
  }
  return {
    E: mapLayout.E,
    boxIxs: mapRects.map(r => r.i),
    wires: mapLayout.wires.length,
    chips: mapLayout.chips.length,
    rosterRows: mapLayout.rosterRows,
    expanded: mapLayout.expandedSet.size,
    // corridor trunk consolidation: admitted corridor polylines vs the
    // count actually stroked (riders con onto hub trunks / L-C leaders).
    // trunkGroups = umbrella (hub buses + L-C row-hop groups) so the gate
    // reads the whole trunk system, not one layer.
    spineTotal: mapLayout.spines.length,
    spinesDrawn: mapLayout.spines.filter(sp => !sp.con && sp.pts.length).length,
    trunkGroups: mapLayout.trunkTotal || mapLayout.trunkGroups || 0,
    hubTrunks: mapLayout.hubTrunks !== undefined ? mapLayout.hubTrunks
      : (mapLayout.audit ? mapLayout.audit.hubTrunks : 0) || 0,
    drawnPolys: mapLayout.underlays.length +
      mapLayout.spines.filter(sp => !sp.con && sp.pts.length).length +
      mapLayout.wires.length,
    probeWire: probe,
    // [issue #82] sticky-pin probe: identity + how many polylines the
    // current paint emphasizes (wire: 1; trunk set: trunk + taps)
    pin: wirePin ? { surface: wirePin.surface, kind: wirePin.kind, id: wirePin.id } : null,
    pinCover,
    pinCoverX,   // [#196] cross-surface emphasis cover
    // [#77] wire LOD bands + selection-time unbundling probes
    lodBand: mapLodBand,          // 0 full / 1 thinned / 2 cluster-aggregated
    lodFitZ: mapLodFitZ,
    lodKeep: !!mapLodPinPair(),   // unbundling live (a pin holds the pair open)
    lodUnbundled: mapLodUnbundled,
    lodAggChips: mapLodAggChips,
    lodSpinesPainted: mapLodSpinesPainted,
    rosterShown: mapLodRosterShown,   // rows painted THIS frame (band 1 folds)
    lodHiddenSpines: mapLayout.spines.filter(sp => !sp.con && sp.pts.length &&
      !sp.hub && nodes[sp.s].cluster === nodes[sp.t].cluster).length,
    instAfford: mapLodInstAfford,   // [#60] sole-relation underlays painted
    instAffordAlpha: mapLodInstAffordAlpha,   // [#60/#210] issued seg alpha
    instAffordWidth: mapLodInstAffordWidth,   // [#60/#210] issued seg width (screen px)
    tapsPainted: mapLodTapsPainted,   // [#61] hub taps stroked this frame
    tapsFolded: mapLodTapsFolded,     // [#61] taps folded below the floor
  };
};
document.getElementById("bGround").onclick = e => {
  showGround = !showGround;
  groundGrid.visible = showGround;
  e.target.classList.toggle("on", showGround);
};
"""
