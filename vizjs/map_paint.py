# vizjs/map_paint.py — rung 13/17 of the template join (issues
# #86 phase-3 / #299 A): map pane paint loop. Moved VERBATIM from viz.py —
# every edit here changes graph.html: regen the bake (the standing
# template law; test_viz refuses a bake older than viz.py/vizjs).
_JS_MAP_PAINT = r"""function mapConsumeCenterReq() {
  if (mapCenterReq < 0) return;
  const rc = mapRects.find(r => r.i === mapCenterReq);
  mapCenterReq = -1;
  if (!rc) return;
  const cwView = mapPane.clientWidth || 440,
        chView = mapPane.clientHeight || innerHeight;
  const dx = (rc.x + rc.w / 2 - mapPX) * mapZ - cwView / 2;
  const dy = (rc.y + rc.h / 2 - mapPY) * mapZ - chView / 2;
  if (Math.abs(dx) > MAP_CENTER_TOL_PX || Math.abs(dy) > MAP_CENTER_TOL_PX) {
    mapPX += dx / mapZ;
    mapPY += dy / mapZ;
    mapClampView();
  }
  mapPulse = { i: rc.i, t0: performance.now() };
}
function mapPaint(ctx, dpr, cwView, chView, capNote) {
  const L = mapLayout;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#0b0f14";
  ctx.fillRect(0, 0, cwView, chView);
  if (capNote) {
    ctx.fillStyle = "#546e7a";
    ctx.font = MAP_FONT(11);
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    ctx.fillText("top " + MAP_MAX + " of " + capNote + " files (by connectivity)", cwView / 2, 8);
  }
  // window on the world: pan/zoom = pure transform of the cached layout
  ctx.setTransform(dpr * mapZ, 0, 0, dpr * mapZ, -mapPX * dpr * mapZ, -mapPY * dpr * mapZ);
  // disclosure dimming: L1 hover dims non-incident to 0.12; L3 freeze to 0.06
  const hov = mapHover >= 0 && L.wires[mapHover] ? L.wires[mapHover] : null;
  const dim = (a, b) => {
    if (mapFrozenIx >= 0) return (a === mapFrozenIx || b === mapFrozenIx) ? 1 : EMPHASIS.MAP_FREEZE_ALPHA;
    if (hov) return (a === hov.sf || a === hov.df || b === hov.sf || b === hov.df) ? 1 : EMPHASIS.DIM_ALPHA;
    return 1;
  };
  const seg = (rec, color, width, dash, alpha) => {
    ctx.globalAlpha = alpha;
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.setLineDash(dash || []);
    ctx.beginPath();
    if (rec.bez) {
      ctx.moveTo(rec.pts[0][0], rec.pts[0][1]);
      ctx.bezierCurveTo(rec.c1[0], rec.c1[1], rec.c2[0], rec.c2[1],
                        rec.pts[1][0], rec.pts[1][1]);
    } else {
      rec.pts.forEach((p, k) => k ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]));
    }
    ctx.stroke();
  };
  // render order (section 9): underlays -> spines (taps, singles, trunks) ->
  // hub junction dots -> wires -> boxes/rosters -> labels/chips/terminators.
  // Underlay alpha 0.12 (ink budget, declutter lever 5). Zoom-gated ink tiers: the fine
  // layers (underlays, named wires, port dots/arrowheads) hide when zoomed
  // out - PAINT-ONLY, the layout never changes (mapInkEval hysteresis).
  // Structure (spines, buses, junction dots, boxes, chips) stays on always.
  // [#60] sole-relation underlays are STRUCTURE, not declutter ink: a pair
  // with no stroked spine and no named wire (an inst/attach-only pair - the
  // whole wiring of an inst-dominant hub) has nothing else to demote under,
  // so it paints at the section-1 affordance alpha with a 1-screen-px width
  // floor, exempt from the fine-ink tier (the map twin of the 3D zero-wire
  // affordance ruling, C2.2). Band-2 intra-cluster fold applies with parity
  // to the spine law (#77).
  mapLodInstAfford = 0;
  mapLodInstAffordAlpha = 0;
  mapLodInstAffordWidth = 0;
  const namedPairs = new Set(L.wires.map(w => w.sf + "_" + w.df));
  L.underlays.forEach(u => {
    const afford = !L.pairW.has(u.s + "_" + u.t) &&
                   !namedPairs.has(u.s + "_" + u.t);   // [#60]
    if (mapLodBand === 2 && nodes[u.s].cluster === nodes[u.t].cluster)
      return;   // [#77] cluster-distance fold (parity with spines)
    if (!afford && !mapInkOn) return;   // demoted ink keeps the fine-ink tier
    const ua = (afford ? 0.40 : 0.12) * dim(u.s, u.t);   // [#60]/[#78]
    const uw = afford ? Math.max(1, 1 / mapZ) : 1;   // [#60] countable at any zoom
    if (afford) {
      mapLodInstAfford++;
      // [#210] probe the ISSUED ink, not the code path: capture what
      // seg() receives so the gate fails under paint-parameter sabotage
      mapLodInstAffordAlpha = Math.max(mapLodInstAffordAlpha, ua);
      mapLodInstAffordWidth = Math.max(mapLodInstAffordWidth, uw);
    }
    seg(u, MGLYPH[u.ty0] ? MGLYPH[u.ty0].c : MGLYPH.attach.c,
        uw, MGLYPH.attach.dash, ua);
    // T-junction terminator: short tick across the entry, no arrow
    ctx.globalAlpha = ua;
    ctx.setLineDash([]);
    ctx.beginPath();
    ctx.moveTo(u.tx - 4, u.ty); ctx.lineTo(u.tx + 4, u.ty);
    ctx.stroke();
  });
  const pinPair = mapLodPinPair();   // [#77] pair unbundled this frame (or null)
  mapLodSpinesPainted = 0;
  mapLodTapsPainted = 0;
  mapLodTapsFolded = 0;
  L.spines.forEach(sp => {
    if (sp.con || !sp.pts.length) return;  // riders: ink rides the trunk
    // [#77] band 2 folds intra-cluster corridors (same cluster both ends):
    // at cluster distance the map reads cluster-to-cluster traffic; hub
    // trunks/taps are the aggregated form and always stay. The pick scan
    // applies the same predicate (pick-vs-paint parity, #113).
    if (mapLodBand === 2 && !sp.hub &&
        nodes[sp.s].cluster === nodes[sp.t].cluster) return;
    // [#77] unbundling: the pinned pair's own corridor stroke hides while
    // its riders draw straight (pass below); trunks carry other pairs too
    // and stay
    if (pinPair && sp.pair === pinPair && !sp.hub) return;
    // [#61] tap legibility floor: below MAP_TAP_FOLD_Z a 1-world-px tap
    // renders sub-half-pixel - fold it; the floored trunk + peel dots
    // carry the read (paint + pick parity, #113)
    if (sp.hub === "tap" && mapZ < MAP_TAP_FOLD_Z) { mapLodTapsFolded++; return; }
    mapLodSpinesPainted++;
    // wire color = TYPE (Blueprint law); wty carries it (the old build let
    // the y-coordinate overwrite the type, painting everything call-gray)
    const color = sp.amber ? "#ffb347" : (MGLYPH[sp.wty] || MGLYPH.call).c;
    if (sp.hub === "trunk") {
      // thick trunk: width reads the summed admitted weight (log2 taper,
      // cap 5.5), floored at 1.3 screen px so trunks stay fat when zoomed
      // out; full-alpha - the trunk IS the structure (InkKnobs #3)
      seg(sp, color, Math.max(Math.min(2 + 0.85 * Math.log2(sp.flowSum), 5.5), 1.3 / mapZ),
          sp.back ? [2, 3] : null, 0.78 * dim(sp.s, sp.t));
    } else if (sp.hub === "tap") {
      mapLodTapsPainted++;
      // thin tap at the rider's type color: access road, not corridor
      seg(sp, color, 1, null, 0.6 * dim(sp.s, sp.t));
    } else {
      // single spine / L-C trunk leader
      const lead = sp.trunkW >= 2;
      // [#61] leaders are trunks: the 1.3-screen-px floor extends from hub
      // trunks so a fan-in super-hub's L-C leaders stay legible zoomed out
      seg(sp, color, lead
          ? Math.max(Math.min(2 + 0.85 * Math.log2(sp.flowSum), 5.5), 1.3 / mapZ)
          : Math.min(2 + 0.85 * Math.log2(sp.flowSum), 5.5),
          sp.back ? [2, 3] : null, (lead ? 0.78 : 0.45) * dim(sp.s, sp.t));
    }
  });
  // hub junction dots (Blueprint reroute nodes): origin + peel points sit ON
  // the routed trunk/tap paths; drawn after the spine pass so converging ink
  // visually terminates ON the marker. Screen-size floor keeps them visible
  // when zoomed out (InkKnobs #16).
  ctx.setLineDash([]);
  (L.hubDots || []).forEach(d => {
    ctx.globalAlpha = 0.95;
    ctx.beginPath();
    ctx.arc(d.x, d.y, Math.max(3.2, 2.5 / mapZ), 0, Math.PI * 2);
    ctx.fillStyle = (MGLYPH[d.c] || MGLYPH.call).c;
    ctx.fill();
    ctx.strokeStyle = "#0a0e12";
    ctx.lineWidth = 1;
    ctx.stroke();
  });
  if (mapInkOn) L.wires.forEach(w => {
    const g = MGLYPH[w.ty] || MGLYPH.call;
    // backward edges (against flow gravity) read as dashed; type color kept
    seg(w, g.c, 1.5, w.back ? [2, 3] : g.dash, 0.9 * dim(w.sf, w.df));
  });
  // [#77] selection-time unbundling (AVI'12): the pinned pair's riders
  // leave the corridor and draw straight/individual at ANY band or zoom -
  // tracing a bundled wire is exactly when the bundle must open. Paint
  // tier only; paths come from the layout-time pairRiders ledger above.
  mapLodUnbundled = 0;
  if (pinPair && L.pairRiders && L.pairRiders[pinPair]) {
    L.pairRiders[pinPair].forEach(r => {
      const g = MGLYPH[r.ty] || MGLYPH.call;
      seg(r, g.c, 1.5, r.back ? [2, 3] : g.dash,
          0.9 * dim(r.sf, r.df));
      mapLodUnbundled++;
    });
  }
  // boxes + rosters
  ctx.setLineDash([]);
  mapLodRosterShown = 0;
  L.lit.forEach(i => {
    const p = L.place.get(i), g = L.geo.get(i);
    if (!p || !g) return;   // pinned subject can outlive the placer (belt)
    const c = mapCols(nodes[i].cluster);
    const a = dim(i);
    const isSel = i === mapFrozenIx;   // amber = selection emphasis (L3 freeze)
    ctx.globalAlpha = a;
    ctx.fillStyle = c.f;
    ctx.strokeStyle = isSel ? "#ffb347" : c.s;
    ctx.lineWidth = isSel ? 3 : 1.5;
    const rad = 6;
    ctx.beginPath();
    ctx.moveTo(p.x + rad, p.y);
    ctx.arcTo(p.x + p.w, p.y, p.x + p.w, p.y + g.h, rad);
    ctx.arcTo(p.x + p.w, p.y + g.h, p.x, p.y + g.h, rad);
    ctx.arcTo(p.x, p.y + g.h, p.x, p.y, rad);
    ctx.arcTo(p.x, p.y, p.x + p.w, p.y, rad);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = c.t;
    ctx.font = MAP_FONT(11);
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    // [#77] band 1 shortens box labels (9 chars + ellipsis, the spec-6
    // narrow-label law): mid-distance reads stems, not smears
    const lbl = mapLodBand >= 1 && nodes[i].label.length > 9
      ? nodes[i].label.slice(0, 9) + "\u2026" : nodes[i].label;
    ctx.fillText(lbl, p.x + p.w / 2, p.y + 11 + 0.5);
    if (g.roster) {
      if (mapLodBand >= 1) {
        // [#77] band 1 folds the roster to a count (thinned labels,
        // VISSOFT'25 mid band). PAINT + PICK parity (#113): folded rows
        // are unpickable (see the click gate); the header keeps refocus.
        // Box geometry stays - heights are layout, this is paint tier.
        ctx.globalAlpha = a * 0.85;
        ctx.fillStyle = "#546e7a";
        ctx.font = MAP_FONT(10);
        ctx.textAlign = "left";
        ctx.fillText("+" + g.roster.rows.length + " fns", p.x + 8,
          p.y + 22 + RH / 2 + 0.5);
      } else {
        ctx.font = MAP_FONT(10);
        ctx.textAlign = "left";
        mapLodRosterShown += g.roster.rows.length;
        g.roster.rows.forEach((r, k) => {
          const ry = p.y + 22 + k * RH + RH / 2 + 0.5;
          ctx.globalAlpha = a;
          ctx.fillStyle = c.t;
          ctx.fillText(r.nm, p.x + 8, ry);
          if (r.io && r.io.w.length) {   // writes badge (section 4)
            ctx.fillStyle = "#80cbc4";
            ctx.textAlign = "right";
            ctx.fillText("\u270e" + r.io.w.length, p.x + p.w - 6, ry);
            ctx.textAlign = "left";
          }
        });
        if (g.roster.more.length) {
          ctx.fillStyle = "#546e7a";
          ctx.fillText("+" + g.roster.more.length + " more...",
            p.x + 8, p.y + 22 + g.roster.rows.length * RH + RH / 2 + 0.5);
        }
      }
    }
  });
  // chips (click -> pinned enumeration list) + labels share ONE screen-space
  // collision ladder: overlap with boxes, chips or earlier labels = slide
  // down a few steps, still colliding = hidden. Never on top of text.
  const m2s = (x, y) => ({ x: (x - mapPX) * mapZ, y: (y - mapPY) * mapZ });
  const taken2 = [];
  mapRects.forEach(rc => {
    const a = m2s(rc.x, rc.y);
    taken2.push({ x: a.x, y: a.y, w: rc.w * mapZ, h: rc.h * mapZ });
  });
  const hit2 = (r) => taken2.some(t =>
    r.x < t.x + t.w && r.x + r.w > t.x && r.y < t.y + t.h && r.y + r.h > t.y);
  const place2 = (x, y, w, h) => {
    // slide ladder with an X leg: badges first try straight up/down off the
    // ink, then slide ALONG their anchor row (screen px) to escape box edges
    // and neighboring badges - a chip with no vertical room still finds a
    // home beside the trunk instead of overlapping (D4)
    for (const dy of [0, -10, 10, -20, 20, -30, 30, -40])
      for (const dx of [0, 14, -14, 28, -28, 42, -42]) {
        const r = { x: x + dx - w / 2, y: y + dy - h / 2, w, h };
        if (!hit2(r)) { taken2.push(r); return { dy, dx }; }
      }
    return null;   // no room: hide rather than stack
  };
  const inView = (x, y) => {
    const sx = (x - mapPX) * mapZ, sy = (y - mapPY) * mapZ;
    return sx >= -30 && sx <= cwView + 30 && sy >= -10 && sy <= chView + 10;
  };
  ctx.font = MAP_FONT(10);
  // selection pulse (paint-only): amber rounded-rect breathing around the
  // clicked node's box — inflates and fades over MAP_PULSE_MS, tick()
  // drops it at end of life. Never touches layout or picking.
  if (mapPulse) {
    const prc = mapRects.find(r => r.i === mapPulse.i);
    if (prc) {
      const t = (performance.now() - mapPulse.t0) / MAP_PULSE_MS;
      if (t >= 0 && t <= 1) {
        const inf = (4 + 10 * t) / mapZ;
        ctx.globalAlpha = 0.9 * (1 - t);
        ctx.strokeStyle = "#ffb347";
        ctx.lineWidth = 3 / mapZ;
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(prc.x - inf, prc.y - inf,
                                         prc.w + 2 * inf, prc.h + 2 * inf, 6);
        else ctx.rect(prc.x - inf, prc.y - inf,
                      prc.w + 2 * inf, prc.h + 2 * inf);
        ctx.stroke();
        ctx.globalAlpha = 1;
      }
    }
  }
  // [issue #78] fit-zoom badge LOD (paint tier): below z 0.85 the fit view
  // keeps only the top peel badges and top origin badges by rider count -
  // the badge flood lived at fit zoom, spread over many small trunks (a
  // per-hub budget cannot cut it: most hubs own one peel). Layout keeps
  // every chip (mapInfo contract); zoom-in restores the full set.
  const chipLOD = new Set();
  if (mapLodBand < 2 && mapZ < 0.85) {
    // [#77] band 1 tightens the badge LOD 6 -> 3 (thinned label plane)
    const kk = mapLodBand >= 1 ? 3 : 6;
    const top = (a, k) => a.sort((p, q) => q.n - p.n || p.row - q.row ||
      p.s - q.s).slice(0, k).forEach(ch => chipLOD.add(ch));
    top(L.chips.filter(ch => ch.peel), kk);
    top(L.chips.filter(ch => ch.origin), kk);
  }
  const paintChip = ch => {
    ch.hit = null; ch.disp = null;   // [issue #113] pick-vs-paint parity: the hit rect is
    // exactly what THIS paint draws — every skip below (LOD, out-of-view,
    // no ladder room, zoom fade) leaves it null = unclickable
    if (!ch.agg) {   // [#77] aggregate badges bypass the per-chip skips
      if ((ch.peel || ch.origin) && !chipLOD.has(ch)) return;
      // [#77] band 2 replaces every pair badge with cluster aggregates
      // (built below); the unbundled pair's badge hides with its corridor
      if (mapLodBand === 2) return;
      if (pinPair && ch.pair === pinPair) return;
    }
    const g = MGLYPH[ch.ty] || MGLYPH.call;
    const sw = ch.w * mapZ, sh = ch.h * mapZ;
    const a = m2s(ch.x + ch.w / 2, ch.y + ch.h / 2);
    if (!inView(ch.x, ch.y)) return;
    const p2 = place2(a.x, a.y, sw, sh);
    if (p2 === null) return;
    // zoom fade (InkKnobs #7): badges dissolve below z~0.45, solid by 0.70 -
    // at overview zoom they were unreadable smudges doubling the wire count.
    // [#77] cluster aggregates are exempt: one count per cluster pair
    // REPLACES that smudge field, it does not add to it.
    const zfRaw = Math.max(0, Math.min(1, (mapZ - 0.45) / 0.25));
    const zf = ch.agg ? Math.max(zfRaw, 0.7) : zfRaw;
    if (zf <= 0) return;
    ch.disp = p2;   // [issue #198] ladder slot (screen px): paint draws
    // dyW = dy/mapZ world px = dy screen px, so the hit rect rides plain dy
    ch.hit = { x: a.x + p2.dx - sw / 2, y: a.y + p2.dy - sh / 2, w: sw, h: sh };
    const dyW = p2.dy / mapZ, dxW = p2.dx / mapZ;  // screen px -> world px
    ctx.globalAlpha = dim(ch.s, ch.t) * zf;
    ctx.fillStyle = "rgba(8,12,16,.85)";
    ctx.strokeStyle = g.c;
    ctx.lineWidth = 1;
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(ch.x + dxW, ch.y + dyW, ch.w, ch.h, 4);
    else ctx.rect(ch.x + dxW, ch.y + dyW, ch.w, ch.h);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = g.c;
    ctx.font = "bold " + MAP_FONT(10);
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("\u00d7" + ch.n, ch.x + dxW + ch.w / 2, ch.y + dyW + ch.h / 2 + 0.5);
    ctx.font = MAP_FONT(10);
  };
  L.chips.forEach(paintChip);
  // [#77] band 2 cluster aggregation: per-pair/peel/origin badges merge
  // into ONE count per (source cluster -> target cluster) - at cluster
  // distance the map reads cluster-to-cluster traffic, not pair inventory.
  // Deterministic (chip order); visual summary - not clickable: the agg
  // recs never join mapLayout.chips, so mapChipAt never sees them (#113)
  // even though paintChip sets hit on them; wheel in one notch for pair
  // interaction.
  mapLodAggChips = 0;
  if (mapLodBand === 2) {
    const agg = new Map();
    L.chips.forEach(ch => {
      if (ch.t < 0 && !ch.peel && !ch.origin) return;
      const key = nodes[ch.s].cluster + "\u2192" +
        (ch.t >= 0 ? nodes[ch.t].cluster : nodes[ch.s].cluster);
      let rec = agg.get(key);
      if (!rec) {
        rec = { n: 0, wires: [], s: ch.s, t: Math.max(ch.t, 0), ty: ch.ty,
                x: ch.x, y: ch.y, w: ch.w, h: ch.h, row: ch.row,
                pair: null, peel: false, origin: false, agg: true };
        agg.set(key, rec);
      }
      rec.n += ch.n;
      if (rec.wires.length < 200) rec.wires = rec.wires.concat(ch.wires);
    });
    agg.forEach(rec => {
      const txt = "\u00d7" + rec.n;
      rec.w = Math.max(rec.w, txt.length * 6 + 10);
      paintChip(rec);
      if (rec.disp) mapLodAggChips++;
    });
  }
  // [issue #113] an open bundle list is LIVE UI over a moving diagram: every
  // repaint re-anchors it, so wheel / pan / divider-drag / resize can never
  // strand it over unrelated geometry. A chip this paint hid (LOD, zoom
  // fade, no ladder room) still labels a real bundle — the list keeps
  // tracking its world anchor rather than closing, preserving the paint-tier
  // pin its rows selected across zoom (#82 pin-survives-pan/zoom contract).
  if (mapListEl.style.display === "block") {
    const lc = mapListChip >= 0 && mapListChip < L.chips.length
      ? L.chips[mapListChip] : null;
    if (!lc) mapOvCloseOne();   // layout genuinely replaced it
    else {
      const an = mapListAnchor(lc);   // painted rect, else the chip anchor
      mapListEl.style.left = an[0] + "px";
      mapListEl.style.top = an[1] + "px";
    }
  }
  // ink-tier gated with the wires themselves - invisible wires wear no
  // arrowheads
  ctx.setLineDash([]);
  if (mapInkOn) L.wires.forEach(w => {
    const g = MGLYPH[w.ty] || MGLYPH.call;
    ctx.globalAlpha = dim(w.sf, w.df);
    // port dot: the wire leaves from a NAMED row — pin the origin (the
    // arrowhead marks the destination; the dot marks where it starts).
    // Bus stubs start at the junction dot — no port dot there.
    ctx.setLineDash([]);
    if (!w.stub) {
      ctx.beginPath();
      ctx.arc(w.pts[0][0], w.pts[0][1], 2.2, 0, Math.PI * 2);
      ctx.fillStyle = g.c;
      ctx.fill();
    }
    if (w.noArr) return;   // bus member: the junction dot is the terminus
    // arrowhead points along the FINAL segment's cardinal direction —
    // horizontal entries get side arrows, drops get up/down arrows
    const pv = w.pts[w.pts.length - 2];
    const adx = w.tx - pv[0], ady = w.ty - pv[1];
    const horiz = Math.abs(adx) > Math.abs(ady);
    if (g.term === "tri" || g.term === "hollow") {
      ctx.beginPath();
      if (horiz) {
        const s = adx > 0 ? 1 : -1;
        ctx.moveTo(w.tx, w.ty);
        ctx.lineTo(w.tx - s * 8, w.ty - 4.5);
        ctx.lineTo(w.tx - s * 8, w.ty + 4.5);
      } else {
        const off = ady > 0 ? -8 : 8;   // arriving from above -> points down
        ctx.moveTo(w.tx, w.ty);
        ctx.lineTo(w.tx - 4.5, w.ty + off);
        ctx.lineTo(w.tx + 4.5, w.ty + off);
      }
      ctx.closePath();
      if (g.term === "tri") { ctx.fillStyle = g.c; ctx.fill(); }
      else { ctx.strokeStyle = g.c; ctx.lineWidth = 1.5; ctx.stroke(); }
    } else if (g.term === "dot") {
      ctx.beginPath();
      ctx.arc(w.tx, w.ty, 3, 0, Math.PI * 2);
      ctx.fillStyle = g.c;
      ctx.fill();
    }
  });
  // bus junction dots (Blueprint reroute nodes): drawn after wires so the
  // converging lanes visually terminate ON the marker
  (L.buses || []).forEach(bs => {
    const g = MGLYPH[bs.ty] || MGLYPH.call;
    ctx.globalAlpha = 0.95;
    ctx.beginPath();
    ctx.arc(bs.x, bs.y, Math.max(3.2, 2.5 / mapZ), 0, Math.PI * 2);
    ctx.fillStyle = g.c;
    ctx.fill();
    ctx.strokeStyle = "#0a0e12";
    ctx.lineWidth = 1;
    ctx.stroke();
  });
  // [issue #82/#196] pinned-wire emphasis: re-stroke the resolved path
  // ON TOP at full emphasis (the pin is explicit user intent - it
  // outranks the zoom-gated fine-ink tiers, like structure ink) with
  // white-ringed endpoint dots. The key re-resolves on every paint, so
  // pan/zoom/rebuild all keep the highlight alive; nothing here touches
  // the layout. [#196] the resolver is surface-agnostic: a ball pin's
  // map twin paints from its pair/fn identity, a map pin paints as
  // before; the home surface owns pinCover, the other pinCoverX.
  let pinCovM = 0;
  const pinTgts = [];
  if (wirePin && L) {
    const pid = String(wirePin.id || "");
    const pfx = pid.charCodeAt(0);
    if (wirePin.kind === "wire" && pfx !== 83 /* not "S" */) {
      // map wire pins are "w|..." rows (exact key); ball wire pins are
      // "F|a|b|ln" - the map row(s) of that fn pair (any wire type),
      // else the pair's trunk corridors / singles / plain rows
      if (wirePin.surface === "map") {
        const pw = L.wires.find(x => wireKeyOf(x) === wirePin.id);
        if (pw) pinTgts.push({ w: pw });
      } else {
        // "F|a|b|ln": a = idp[1], b = idp[2] (idp[3] is the LINE)
        const idp = pid.split("|");
        if (fnMeta && +idp[1] >= 0 && +idp[1] < fnMeta.length &&
            +idp[2] >= 0 && +idp[2] < fnMeta.length &&
            fnMeta[+idp[1]] && fnMeta[+idp[2]]) {
          const sf = fnMeta[+idp[1]].file, df = fnMeta[+idp[2]].file;
          const sfn = fnMeta[+idp[1]].name, dfn = fnMeta[+idp[2]].name;
          for (const x of L.wires)
            if (x.sf === sf && x.sfn === sfn && x.df === df && x.dfn === dfn)
              pinTgts.push({ w: x });
          if (!pinTgts.length)
            for (const sp of L.spines)
              if ((sp.hub === "trunk" || !sp.hub) &&
                  sp.pts && sp.pts.length > 1 &&
                  ((sp.s === sf && sp.t === df) ||
                   (sp.s === df && sp.t === sf))) pinTgts.push({ sp: sp });
          if (!pinTgts.length)
            // small pairs draw as plain wire rows (no spine at all)
            for (const x of L.wires)
              if ((x.sf === sf && x.df === df) ||
                  (x.sf === df && x.df === sf)) pinTgts.push({ w: x });
        }
      }
    } else if (wirePin.kind === "wire" && pfx === 83 /* "S" */) {
      // [issue #84] spine single/leader pair stroke: stroke the spine itself
      const ps = L.spines.find(x2 => !x2.hub && x2.s === wirePin.s &&
                                     x2.t === wirePin.t &&
                                     x2.wty === wirePin.wty &&
                                     x2.pts && x2.pts.length > 1);
      if (ps) pinTgts.push({ sp: ps, single: true });
    } else if (wirePin.kind === "trunk" || wirePin.kind === "link") {
      // [issue #82] trunk set: the corridor AND its taps. A map trunk
      // pin keys (s,t,wty) exactly; a ball trunk/link pin is pair-level
      // ("K|s>t" / link li) and matches every corridor of the pair,
      // either orientation (deterministic: spine order)
      let ts = wirePin.s, tt = wirePin.t;
      if (ts == null && wirePin.k != null) {
        const pp = String(wirePin.k).split(">"); ts = +pp[0]; tt = +pp[1];
      }
      if (ts == null && wirePin.kind === "link" && links &&
          wirePin.li >= 0 && wirePin.li < links.length) {
        ts = links[wirePin.li].s; tt = links[wirePin.li].t;
      }
      if (ts != null) {
        if (wirePin.wty != null) {
          const tr = L.spines.find(sp => sp.hub === "trunk" &&
            sp.pts && sp.pts.length > 1 &&
            sp.s === ts && sp.t === tt && sp.wty === wirePin.wty);
          if (tr) pinTgts.push({ sp: tr });
        } else {
          // trunk corridors first; a pair the map drew as singles
          // (below the trunk admission threshold) still emphasizes -
          // the pair identity is what the pin carries
          for (const sp of L.spines)
            if (sp.hub === "trunk" && sp.pts && sp.pts.length > 1 &&
                ((sp.s === ts && sp.t === tt) ||
                 (sp.s === tt && sp.t === ts))) pinTgts.push({ sp: sp });
          if (!pinTgts.length)
            for (const sp of L.spines)
              if (!sp.hub && sp.pts && sp.pts.length > 1 &&
                  ((sp.s === ts && sp.t === tt) ||
                   (sp.s === tt && sp.t === ts))) pinTgts.push({ sp: sp });
          if (!pinTgts.length)
            // small pairs draw as plain wire rows (no spine at all)
            for (const x of L.wires)
              if ((x.sf === ts && x.df === tt) ||
                  (x.sf === tt && x.df === ts)) pinTgts.push({ w: x });
        }
      }
    }
  }
  for (const T of pinTgts) {
    if (T.w) {
      const pw = T.w;
      ctx.setLineDash([]);
      // [issue #85 owner r1] accent emphasis (see PIN_ACCENT)
      seg(pw, PIN_ACCENT, 3, pw.back ? [2, 3] : null, Math.max(dim(pw.sf, pw.df), 0.95));
      pinCovM++;
      ctx.globalAlpha = 1;
      ctx.lineWidth = 1.4; ctx.strokeStyle = "#fff"; ctx.fillStyle = PIN_ACCENT;
      for (const [ex, ey] of [pw.pts[0], [pw.tx, pw.ty]]) {
        ctx.beginPath();
        ctx.arc(ex, ey, 3.4, 0, Math.PI * 2);
        ctx.fill(); ctx.stroke();
      }
    } else if (T.single || !T.sp.hub) {
      // [issue #84] spine single/leader pair stroke
      const ps = T.sp;
      if (ps && ps.pts && ps.pts.length > 1) {
        ctx.setLineDash([]);
        // [issue #85 owner r1] emphasis is the accent, not the wire's own
        // type color (call wires ARE the white mass the pin hides in)
        seg(ps, PIN_ACCENT, 3, null, Math.max(dim(ps.s, ps.t), 0.95));
        pinCovM++;
        ctx.globalAlpha = 1;
        ctx.lineWidth = 1.4; ctx.strokeStyle = "#fff"; ctx.fillStyle = PIN_ACCENT;
        const pe = ps.pts[ps.pts.length - 1];
        for (const [ex, ey] of [ps.pts[0], pe]) {
          ctx.beginPath(); ctx.arc(ex, ey, 3.4, 0, Math.PI * 2);
          ctx.fill(); ctx.stroke();
        }
      }
    } else if (T.sp.hub === "trunk") {
      // [issue #82] trunk set: the corridor AND its taps - the enumerated
      // set the pin selected; endpoints ringed like wire pins
      const tr = T.sp;
      const wT = Math.max(Math.min(2 + 0.85 *
        Math.log2(tr.flowSum || tr.trunkW || 2), 5.5) * 1.6, 5 / mapZ);
      ctx.setLineDash([]);
      // [issue #85 owner r1] accent emphasis (see PIN_ACCENT)
      seg(tr, PIN_ACCENT, wT, [], Math.max(dim(tr.s, tr.t), 0.95));
      pinCovM++;
      mapLayout.spines.forEach(sp2 => {
        if (sp2.hub === "tap" && sp2.tapBus && sp2.tapBus.trunk === tr) {
          seg(sp2, PIN_ACCENT, 2, [], 0.9);
          pinCovM++;
        }
      });
      ctx.globalAlpha = 1;
      ctx.lineWidth = 1.4;
      ctx.strokeStyle = "#fff";
      ctx.fillStyle = PIN_ACCENT;
      for (const p of [tr.pts[0], [tr.tx, tr.ty]]) {
        if (!p) continue;
        ctx.beginPath();
        ctx.arc(p[0], p[1], 3.4, 0, Math.PI * 2);
        ctx.fill(); ctx.stroke();
      }
    }
  }
  if (wirePin) {
    if (wirePin.surface === "map") pinCover = pinCovM;
    else pinCoverX = pinCovM;
  }
  ctx.globalAlpha = 1;
  ctx.globalAlpha = 1;
  // screen-space furniture: map-local vars chip [F10] + footer
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  mapVarsChipRect = { x: 8, y: 26, w: 46, h: 16 };
  ctx.fillStyle = mapVarsOn ? "rgba(29,233,182,.25)" : "rgba(8,12,16,.85)";
  ctx.strokeStyle = mapVarsOn ? "#1de9b6" : "#263238";
  ctx.lineWidth = 1;
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(8, 26, 46, 16, 4);
  else ctx.rect(8, 26, 46, 16);
  ctx.fill();
  ctx.stroke();
  ctx.fillStyle = mapVarsOn ? "#1de9b6" : "#78909c";
  ctx.font = MAP_FONT(10);
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText("vars", 31, 34.5);
  ctx.fillStyle = "#546e7a";
  ctx.textAlign = "left"; ctx.textBaseline = "bottom";
  let foot = "\u25b2 in  \u25bc out  \u2014 call  \u2013 signal  \u00b7 var";
  const mr = DATA.meta || {};
  if (mr.sig_unresolved)
    foot += "  |  sig " + (mr.sig_resolved || 0) + " ok / " +
            mr.sig_unresolved + " unres";
  const bd = mr.budget || {};
  if (bd.wireRowsDropped || bd.hwArcsDropped || bd.fioDropped) {
    const dropped = [];
    if (bd.wireRowsDropped) dropped.push(bd.wireRowsDropped + " wire");
    if (bd.hwArcsDropped) dropped.push(bd.hwArcsDropped + " hw");
    if (bd.fioDropped) dropped.push(bd.fioDropped + " fio");
    foot += "  |  budget: " + dropped.join(", ") + " dropped";
  }
  ctx.fillText(foot, 8, chView - 6);
}
// keep the window inside the world: pan/zoom can never strand the layout
// off-screen (drag = pan must stay recoverable at every zoom)
function mapClampView() {
  if (!mapLayout) return;
  const cwView = mapPane.clientWidth || 440, chView = mapPane.clientHeight || innerHeight;
  // window clamp; world smaller than the window => negative bounds, and the
  // position snaps to the centered midpoint (far-out orientability: content
  // floats mid-pane with equal dead space, never corner-stranded)
  const clampC = (p, world, view) => {
    const lo = Math.min(0, world - view / mapZ), hi = Math.max(0, world - view / mapZ);
    p = Math.max(lo, Math.min(hi, p));
    return hi === 0 && lo < 0 ? (lo + hi) / 2 : p;
  };
  mapPX = clampC(mapPX, mapLayout.worldW, cwView);
  mapPY = clampC(mapPY, mapLayout.worldH || 0, chView);
}
// rAF dirty-flag single draw (section 5 [F7]): every caller coalesces here
function drawMapPane() {
  if (!mapVisible || mapDirty) return;
  mapDirty = true;
  requestAnimationFrame(() => {
    mapDirty = false;
    try { mapRender(); }
    catch (err) { console.warn("map render failed:", err); }
  });
}
document.getElementById("bMap").onclick = () => setMapVisible(!mapVisible);
// mapVisible drives the whole split: pane + divider visibility, info-panel
// shift, 3D region size. Single entry point — boot and #bMap both use it.
function setMapVisible(v) {
  mapVisible = v;
  document.getElementById("bMap").classList.toggle("on", v);
  mapPane.classList.toggle("collapsed", !v);
  divider.classList.toggle("collapsed", !v);
  document.body.classList.toggle("mapOpen", v);
  // keep the node info panel clear of the pane instead of underneath it
  info.classList.toggle("mapShift", v);
  resize3D();
  // [issue #58] opening the pane is a surface switch: it covers the canvas
  // region the pointer was hovering and no canvas pointermove will fire
  // under it, so the 3D hover tip must die here. The pin-owned wireTip is
  // exempt (persists to dismissal, issue #85).
  if (v) { tip.style.display = "none"; sizeMapPane(); drawMapPane(); }
  else { mapTipHide(); mapOvCloseOne(); mapCenterReq = -1; mapPulse = null; }
}
// ---- divider drag: resize the split (rAF-throttled), never orbits the 3D ---
// the divider is its own element — OrbitControls listens on the canvas only,
// so a drag here cannot start a camera move by construction
"""
