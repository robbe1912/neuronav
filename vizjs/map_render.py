# vizjs/map_render.py — rung 12/17 of the template join (issues
# #86 phase-3 / #299 A): 2D map pane layout + build. Moved VERBATIM from viz.py —
# every edit here changes graph.html: regen the bake (the standing
# template law; test_viz refuses a bake older than viz.py/vizjs).
_JS_MAP_RENDER = r"""function mapRender() {
  if (!mapVisible) return;
  const ctx = mapPane.getContext("2d");
  const cwView = mapPane.clientWidth || 440;
  const chView = mapPane.clientHeight || innerHeight;
  const dpr = mapPane.width / cwView || 1;
  // world width follows the pane: the scan ceiling grows with it so wide
  // panes get wide worlds, capped at 1400 so a maximized window cannot wrap
  // a small repo into one unbounded megaband. The fit scan below picks the
  // real width for THIS pane (floor 0.30 protects text on pathological repos)
  const cw = Math.max(480, Math.min(1400, Math.floor(cwView * 1.6 / 20) * 20));   // scan ceiling
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#0b0f14";
  ctx.fillRect(0, 0, cwView, chView);
  const hint = txt => {
    ctx.fillStyle = "#546e7a";
    ctx.font = MAP_FONT(12);
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(txt, cwView / 2, chView / 2);
  };
  // NOTE: mapRects is NOT cleared here - a cache-hit repaint below skips the
  // rebuild and must keep the last hit-test rects; only discard/rebuild paths
  // touch it
  if (!focusActive) { mapTeardown(); mapLayout = null; mapRects = []; hint("focus a node to see its map"); return; }
  const litAll = [];
  for (let i = 0; i < N; i++) if (level[i] >= 0 && nodeVisible(nodes[i])) litAll.push(i);
  // cap by connectivity: keep the MAP_MAX most-connected lit files so a hub
  // focus still draws a diagram instead of a "narrow the focus" shrug
  let lit = litAll;
  const capNote = litAll.length > MAP_MAX ? litAll.length : 0;
  if (capNote) {
    lit = litAll.slice().sort((a, b) => (degree[b] - degree[a]) || (a - b)).slice(0, MAP_MAX);
  }
  if (!lit.length) { mapTeardown(); mapLayout = null; mapRects = []; hint("focus a node to see its map"); return; }
  // ONE layout at every zoom (owner mandate): no admission tiers, no doc
  // scoping - the wiring diagram below is zoom-independent. Relayouts fire
  // only on focus/visibility/expansion changes (cache key below).
  // focus signature ("focusVersion"): every input that changes the lit set
  // or the typed admission. pan/zoom never touch it (section 5).
  const sig = lit.join(",") + "|" + mapVarsOn + "|" +
    typeVisible("call") + typeVisible("signal") + typeVisible("inst");
  // [issue #84] skeptic #5: a REBUILT layout (sig change = focus/pivot
  // change) invalidates every map-pin id - clear instead of keeping an
  // invisible stale selection. Guarded by the rebuild itself: mapRender
  // repaints every frame, and pinning must survive repaints (paint tier).
  // (the ball surface reaps unresolved pins frame-counted in updateBallPin)
  if (mapLayout && mapLayout.sig !== sig) {
    mapZ = 0;   // focus change -> refit
    // [issue #84] skeptic #14: the rebuilt layout also invalidates the
    // overlays keyed to it - the trunk bundle list's rows enumerate the
    // OLD corridor (stale wire ids, stale anchor), so it cannot survive
    // the rebuild. mapOvCloseOne dismisses the overlay family (its #82
    // rule unpins the list-menu pin); the direct clear below catches map
    // pins whose menu was not the list.
    mapOvCloseOne();
    if (wirePin && wirePin.surface === "map") wirePinClear();
    // [issue #113] the hover pick is keyed to this layout too: a stale
    // mapHover index survives into the rebuilt wires array and dim()'s
    // hov branch keeps dimming the new diagram by the old focus's wire.
    mapHover = -1;
  }
  // tier-1 admission: file skeleton unchanged (survives section 10)
  const litSet = new Set(lit);
  const cand = [];
  const seenPair = new Set();
  links.forEach(l => {
    if (l.s === l.t || !litSet.has(l.s) || !litSet.has(l.t) || !typeVisible(l.ty)) return;
    const k = l.s + "_" + l.t;
    if (seenPair.has(k)) return;
    seenPair.add(k);
    cand.push(l);
  });
  const top8 = new Set([...lit].sort((a, b) => degree[b] - degree[a] || a - b).slice(0, 8));
  cand.sort((a, b) => (b.w || 1) - (a.w || 1) || a.s - b.s || a.t - b.t);
  let edges = cand.filter((l, i) =>
    (l.w || 1) >= 2 || i < 120 || top8.has(l.s) || top8.has(l.t)).slice(0, 160);
  const E = edges.length;
  // expansion set: user dblclick override > every wired box opens (roster
  // rows are THE layout - named wires terminate on fn rows) > seeds open
  // at L0. Zoom-independent: structure never changes with zoom.
  const wireInc = new Map();
  edges.forEach(l => {
    wireInc.set(l.s, (wireInc.get(l.s) || 0) + 1);
    wireInc.set(l.t, (wireInc.get(l.t) || 0) + 1);
  });
  const expand = new Set();
  lit.forEach(i => {
    const u = mapExpandUser.get(i);
    const wired = (wireInc.get(i) || 0) >= 1;
    // seed = BFS level 0 (click seeds AND query matches): roster open at L0
    if (u !== undefined ? u : (wired || level[i] === 0))
      expand.add(i);
  });
  // layout cache (section 5 [F7]): hit = pure repaint under pan/zoom
  const key = sig + "||" +
    [...expand].sort((a, b) => a - b).join(",") + "||" +
    Math.round(cwView) + "x" + Math.round(chView);
  if (mapLayout && mapLayout.key === key) {
    mapConsumeCenterReq();
    mapPaint(ctx, dpr, cwView, chView, capNote);
    return;
  }
  // ---- tier-2 named wires over the admitted corridors (section 2 [F1]) ----
  const pairSet = new Set(edges.map(l => l.s + "_" + l.t));
  const vw = [];
  mwires.forEach(w => {
    if (w[0] === "var" ? !mapVarsOn : !typeVisible(w[0])) return;
    const sf = w[1], df = w[3];
    if (typeof sf !== "number" || typeof df !== "number") return;
    if (!pairSet.has(sf + "_" + df)) return;   // named wires ride admitted pairs
    vw.push({ ty: w[0], sf, sfn: String(w[2]), df, dfn: String(w[4]), line: w[5] || 0 });
  });
  const inDegFn = new Map();   // callee in-degree: head of the F14 rank
  vw.forEach(w => {
    const k = w.df + "::" + w.dfn;
    inDegFn.set(k, (inDegFn.get(k) || 0) + 1);
  });
  const byPair = new Map();
  vw.forEach(w => {
    const k = w.sf + "_" + w.df;
    let a = byPair.get(k);
    if (!a) byPair.set(k, a = []);
    a.push(w);
  });
  const rk = (a, b) =>
    ((inDegFn.get(b.df + "::" + b.dfn) || 0) - (inDegFn.get(a.df + "::" + a.dfn) || 0)) ||
    (a.dfn < b.dfn ? -1 : a.dfn > b.dfn ? 1 : 0) ||
    (a.sf - b.sf) ||
    (a.sfn < b.sfn ? -1 : a.sfn > b.sfn ? 1 : 0) ||
    (a.line - b.line);
  // [issue #78] pair aggregation: every admitted pair used to draw its top-1
  // named wire, so a dense focus set fanned 50+ individual wires through the
  // mid-band channels - the "wire wall" the audit measured at wy 720-960.
  // Thresholds (clutter research): N=1 stays a labeled wire; 2-3 keep the
  // top-1 wire + corridor + xN chip; N>=4 collapse to corridor + chip; any
  // pair touching a hub (degree >= MAP_HUB_T1) is corridor + chip unless it
  // sits in that hub's top-4 by multiplicity - hubs keep a legible budget
  // of individual wires, everything else rides its corridor.
  const pairN = new Map();
  byPair.forEach((a, k) => pairN.set(k, a.length));
  const hubTop = new Map();    // hub i -> top-4 pair keys allowed a wire
  byPair.forEach((a, k) => {
    for (const end of [a[0].sf, a[0].df]) {
      if (degree[end] < MAP_HUB_T1) continue;
      let s = hubTop.get(end);
      if (!s) hubTop.set(end, s = []);
      s.push(k);
    }
  });
  hubTop.forEach((ks, i) => hubTop.set(i, new Set(ks.sort((p, q) =>
    (pairN.get(q) || 0) - (pairN.get(p) || 0) || (p < q ? -1 : p > q ? 1 : 0))
    .slice(0, 4))));
  const indiv = [];            // top-1 per pair: drawn + labeled [F14]
  byPair.forEach((a, k) => {
    a.sort(rk);
    const hubEnd = degree[a[0].sf] >= MAP_HUB_T1 ? a[0].sf
      : degree[a[0].df] >= MAP_HUB_T1 ? a[0].df : -1;
    const inBudget = hubEnd >= 0 && hubTop.get(hubEnd).has(k);
    if (!inBudget && (a.length >= 4 || hubEnd >= 0)) return;  // corridor + xN
    indiv.push(a[0]);
  });
  indiv.sort(rk);
  const indivSet = new Set(indiv);
  // ---- rosters (section 4) ----
  const fioMap = DATA.fio || {};
  const rosterOf = i => {
    const all = (mfns[nodes[i].path] || []).slice();   // complete roster
    if (!all.length) return null;
    const pin = new Set();
    byPair.forEach(arr => arr.forEach(w => {   // every named wire pins its rows
      if (w.df === i) pin.add(w.dfn);
      if (w.sf === i) pin.add(w.sfn);
    }));
    indiv.forEach(w => {   // a named wire must terminate on its fn row
      if (w.df === i) pin.add(w.dfn);
      if (w.sf === i) pin.add(w.sfn);
    });
    const rank = nm => inDegFn.get(i + "::" + nm) || 0;
    all.sort((a, b) => ((pin.has(b[0]) ? 1 : 0) - (pin.has(a[0]) ? 1 : 0)) ||
      rank(b[0]) - rank(a[0]) ||
      (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0) || (a[1] - b[1]));
    const shown = all.slice(0, FN_PORT_MAX);
    return {
      rows: shown.map(r => ({ nm: r[0], ln: r[1], io: fioMap[fnKey(i, r[0])] })),
      more: all.slice(FN_PORT_MAX),
    };
  };
  // geometry (F6: expansion resolved BEFORE the row wrap); NH/RH are module consts
  ctx.font = MAP_FONT(10);
  const txtW = t => Math.ceil(ctx.measureText(t).width);
  const wOf = i => Math.max(38, txtW(nodes[i].label) + 18);
  const geo = new Map();
  let rosterRows = 0;
  lit.forEach(i => {
    if (!expand.has(i)) { geo.set(i, { w: wOf(i), h: NH, roster: null }); return; }
    const r = rosterOf(i);
    if (!r || !r.rows.length) { geo.set(i, { w: wOf(i), h: NH, roster: null }); return; }
    let w = wOf(i);
    r.rows.forEach(row => {
      w = Math.max(w, txtW(row.nm) +
        (row.io && row.io.w.length ? txtW("\u270e" + row.io.w.length) + 8 : 0) + 16);
    });
    w = Math.min(420, w);
    rosterRows += r.rows.length;
    geo.set(i, { w, h: NH + (r.rows.length + (r.more.length ? 1 : 0)) * RH, roster: r, more: r.more });
  });
  // flow rows: longest call-path depth INSIDE the lit set (Bellman-Ford
  // relaxation, cycle-capped by lit.length) so callers sit above callees —
  // one flow direction (Mermaid/Blueprint law). BFS level rings callers and
  // callees together (wires then read upward); level still seeds lit and
  // expansion. Every lit node gets an fd, so the placer covers lit even
  // when the pinned subject sits outside the search BFS (old level -1 case).
  const callF = edges.filter(l => l.ty === "call");
  const fd = new Map();
  lit.forEach(i => fd.set(i, 0));
  for (let sweep = 0; sweep < lit.length; sweep++) {
    let moved = 0;
    callF.forEach(l => {
      const d = fd.get(l.s) + 1;
      if (l.s !== l.t && d > fd.get(l.t) && d < lit.length) { fd.set(l.t, d); moved++; }
    });
    if (!moved) break;
  }
  const rows = [];
  lit.forEach(i => (rows[fd.get(i)] = rows[fd.get(i)] || []).push(i));
  // [issue #78] ordering: the plain 3-sweep barycenter is replaced by
  // degree-damped TSE93 weighted-median sweeps + a transpose pass. Undamped
  // means ARE the cram cause (every hub neighbour averaged toward the hub's
  // column): hubs (deg >= MAP_HUB_T1) hold the plain median so their many
  // wires keep spread, the rest take the fig 3-2 weighted median. Cluster
  // blocks then regroup contiguously (block order = median position) so
  // CLUSTER_GAP_X can separate them in placement.
  const preds = new Map(), succs = new Map();
  edges.forEach(l => {
    if (!preds.has(l.t)) preds.set(l.t, []);
    preds.get(l.t).push(l.s);
    if (!succs.has(l.s)) succs.set(l.s, []);
    succs.get(l.s).push(l.t);
  });
  const degOf = new Map();
  edges.forEach(l => {
    degOf.set(l.s, (degOf.get(l.s) || 0) + 1);
    degOf.set(l.t, (degOf.get(l.t) || 0) + 1);
  });
  const cidOf = i => nodes[i].cluster;
  const gRow = new Map();         // node -> row index
  rows.forEach((row, r) => row.forEach(i => gRow.set(i, r)));
  const col = new Map();
  rows.forEach(row => row.forEach((i, k) => col.set(i, k)));
  const wmed = (i, down) => {     // TSE93 weighted median of neighbour cols
    const src = down ? preds.get(i) : succs.get(i);
    const ps = (src || []).map(p => col.get(p)).sort((x, y) => x - y);
    if (!ps.length) return col.get(i);
    if (degOf.get(i) >= MAP_HUB_T1) return ps[ps.length >> 1];   // hub: plain median
    const m = ps.length >> 1;
    if (ps.length === 1) return ps[0];
    if (ps.length === 2) return (ps[0] + ps[1]) / 2;
    const left = ps[m - 1] - ps[0], right = ps[ps.length - 1] - ps[m];
    return left + right > 0 ? (ps[m - 1] * right + ps[m] * left) / (left + right) : ps[m];
  };
  for (let sw = 0; sw < 4; sw++) {
    const down = sw % 2 === 0;
    const seq = [];
    for (let r = 0; r < rows.length; r++) (down ? seq.push(r) : seq.unshift(r));
    seq.forEach(r => {
      const row = rows[r];
      if (!row || row.length < 2) return;   // fd rows can have holes
      const nb = row.map(i => ({ i, b: wmed(i, down) }));
      nb.sort((a, b) => a.b - b.b || a.i - b.i);
      rows[r] = nb.map(x => x.i);
      rows[r].forEach((i, k) => col.set(i, k));
    });
  }
  // cluster regroup: blocks (same cluster) become contiguous, block order =
  // the members' median swept position, so the regroup preserves the sweep
  // optimum while giving placement a clean cluster boundary to pad
  rows.forEach(row => {
    if (row.length < 2) return;
    const pos = new Map(row.map((i, k) => [i, k]));
    const blocks = new Map();     // cid -> [{i, p}] in swept order
    row.forEach(i => {
      const c = cidOf(i);
      if (!blocks.has(c)) blocks.set(c, []);
      blocks.get(c).push({ i, p: pos.get(i) });
    });
    const bl = [...blocks.entries()].map(([c, ms]) => {
      const ps = ms.map(m => m.p).sort((x, y) => x - y);
      return { c, med: ps[ps.length >> 1], ms };
    }).sort((a, b) => a.med - b.med || a.c - b.c);
    const out = [];
    bl.forEach(b => {
      b.ms.sort((x, y) => x.p - y.p || x.i - y.i);
      b.ms.forEach(m => out.push(m.i));
    });
    row.length = 0; row.push(...out);
  });
  rows.forEach(row => row.forEach((i, k) => col.set(i, k)));
  // transpose: swap adjacent pairs while their incident-edge inversion count
  // strictly drops (TSE93 transpose, bounded 2 passes, re-checks swaps)
  const pairCross = (a, b) => {   // a-left crossings minus b-left crossings
    let d = 0;
    const oa = [], ob = [];
    edges.forEach(l => {
      if (l.s === a) oa.push(l.t); else if (l.t === a) oa.push(l.s);
      if (l.s === b) ob.push(l.t); else if (l.t === b) ob.push(l.s);
    });
    oa.forEach(x => ob.forEach(y => {
      if (x === y || gRow.get(x) !== gRow.get(y)) return;
      d += col.get(x) > col.get(y) ? 1 : -1;
    }));
    return d;                     // > 0: swapping strictly reduces crossings
  };
  for (let pass = 0; pass < 2; pass++) {
    let swapped = false;
    for (let r = 0; r < rows.length; r++) {
      const row = rows[r];
      if (!row) continue;   // fd rows can have holes
      for (let k = 0; k + 1 < row.length; k++) {
        if (pairCross(row[k], row[k + 1]) > 0) {
          const t = row[k]; row[k] = row[k + 1]; row[k + 1] = t;
          swapped = true; k--;    // re-examine after the swap
        }
      }
    }
    if (!swapped) break;
  }
  rows.forEach(row => row.forEach((i, k) => col.set(i, k)));

  rows.forEach(row => row.forEach((i, k) => col.set(i, k)));
  // rows WRAP to world width against the resolved box widths. The world
  // width is CHOSEN: a narrow world wraps into more/taller chunks, a wide
  // one into fewer/flatter - the fit zoom z(W) saturates once W stops
  // re-wrapping (h constant). Wrap once per candidate width (cheap: <=40
  // boxes), keep the width that maximizes z, ties -> WIDEST: the tie zone
  // is exactly where a sub-1 zoom would stare at a narrow column through
  // dead side margins, and growing W there costs nothing (same z, same
  // wrap) while giving lanes and ports the spare width. Deterministic:
  // same data + pane => same scan.
  const wrapChunks = W => {
    const ch = [];
    for (let r = 0; r < rows.length; r++) {
      const row = rows[r];
      if (!row) continue;
      let cur = [], twc = -GAPX;
      row.forEach(i => {
        const w = geo.get(i).w;
        if (twc + GAPX + w > W && cur.length) { ch.push(cur); cur = []; twc = -GAPX; }
        cur.push(i); twc += GAPX + w;
      });
      if (cur.length) ch.push(cur);
    }
    return ch;
  };
  // band demand: how many admitted edges cross each chunk boundary. Hot
  // corridors (hub adjacency) get TALLER gap bands so channel capacity
  // grows with traffic - horizontals ladder instead of piling on the band
  // floor (the near-parallel wall). Deterministic from edges order.
  const bandPads = ch => {
    const cOf = new Map();
    ch.forEach((chunk, g) => chunk.forEach(i => cOf.set(i, g)));
    const cross = ch.map(() => 0);
    edges.forEach(l => {
      const a = cOf.get(l.s), b = cOf.get(l.t);
      if (a === undefined || b === undefined || a === b) return;
      for (let g = Math.min(a, b); g < Math.max(a, b); g++) cross[g]++;
    });
    return cross.map(n => 36 + 7 * Math.max(0, Math.min(16, n - 4)));
  };
  const wrapH = ch => {   // per-chunk rowH = max(56, tallest + band pad) [F6]
    // band pads grow with crossing demand (capacity for hot corridors) but
    // under a loose height budget: past it the fit zoom would fall below
    // the 0.30 floor and extra band capacity buys nothing on screen.
    // Extras scale down to fit.
    const Hmax = chView / 0.30;
    const rowHOf = (tall, pad) => Math.max(56, tall + pad);
    const total = pads => {
      let y = TOP;
      ch.forEach((chunk, g) => {
        let tall = NH;
        chunk.forEach(i => { tall = Math.max(tall, geo.get(i).h); });
        y += rowHOf(tall, pads[g]);
      });
      return y + 20;
    };
    let pads = bandPads(ch);
    const base = ch.map(() => 36);
    const H1 = total(pads);
    if (H1 > Hmax && H1 > total(base)) {
      const s = Math.max(0, (Hmax - total(base)) / (H1 - total(base)));
      pads = pads.map((p, g) => base[g] + Math.round((p - base[g]) * s));
    }
    const tops = [];
    ch.forEach((chunk, g) => {
      let tall = NH;
      chunk.forEach(i => { tall = Math.max(tall, geo.get(i).h); });
      tops.push(tall);
      pads[g] = rowHOf(tall, pads[g]) - tall;   // effective pad after floor
    });
    return { h: Math.max(chView, total(pads)), tops, pads };
  };
  const fitZOf = (W, h) => Math.max(0, Math.min(1.0, (cwView - 48) / W, (chView - 48) / h));
  let cwBest = 480, zBest = -1;
  for (let W = 480; W <= cw; W += 20) {
    const z = fitZOf(W, wrapH(wrapChunks(W)).h);
    if (z > zBest + 1e-9 || (z >= zBest - 1e-9 && W > cwBest)) { zBest = z; cwBest = W; }
  }
  const cwL = cwBest;                    // resolved world width for THIS pane
  const chunks = wrapChunks(cwL);
  const wrapRes = wrapH(chunks);
  const chunkRowH = [], chunkY = [], chunkTop = [];
  let wy = TOP;
  chunks.forEach((chunk, g) => {
    let tall = NH;
    chunk.forEach(i => { tall = Math.max(tall, geo.get(i).h); });
    chunkTop.push(tall);
    chunkRowH.push(Math.max(56, tall + wrapRes.pads[g]));
    chunkY.push(wy);
    wy += Math.max(56, tall + wrapRes.pads[g]);
  });
  const worldH = Math.max(chView, wy + 20);
  // [issue #78] slack spreading: wrapping stays tight (GAPX) so the scan
  // sees the flattest world, but PLACEMENT stretches each band's gaps up
  // to GAPX_MAX so a wide world reads as full-width bands instead of a
  // centered column with dead side margins. Cluster boundaries take
  // CLUSTER_GAP_X (block separation); leftover centers the band.
  const place = new Map();
  chunks.forEach((chunk, rr) => {
    const sw = chunk.reduce((a, i) => a + geo.get(i).w, 0);
    const n = chunk.length;
    const cbnd = [];               // cluster change at gap k (between k, k+1)
    let nb = 0;
    for (let k = 0; k + 1 < n; k++) {
      const chg = cidOf(chunk[k]) !== cidOf(chunk[k + 1]);
      cbnd.push(chg); if (chg) nb++;
    }
    // cluster gaps must never push the extent past the world; if they
    // would, they degrade to the spread gap (band stays inside cwL - 16)
    let cg = CLUSTER_GAP_X;
    let gap = n > 1
      ? Math.min(GAPX_MAX, Math.max(GAPX, (cwL - 16 - sw - cg * nb) / (n - 1))) : GAPX;
    if (sw + cg * nb + gap * (n - 1 - nb) > cwL - 16) {
      cg = GAPX;
      gap = n > 1
        ? Math.min(GAPX_MAX, Math.max(GAPX, (cwL - 16 - sw) / (n - 1))) : GAPX;
    }
    let ext = sw;
    for (let k = 0; k < n - 1; k++) ext += cbnd[k] ? cg : gap;
    let x = Math.max(8, (cwL - ext) / 2);
    const y = chunkY[rr];
    chunk.forEach((i, k) => {
      place.set(i, { x, y, w: geo.get(i).w, h: geo.get(i).h, row: rr });
      if (k + 1 < n) x += geo.get(i).w + (cbnd[k] ? cg : gap);
    });
  });
  if (!mapZ) {   // focus change / first draw: fit BOTH dims [F15 rev2]
    // [issue #78] 24px fit margin + 0.30 floor: matches fitZOf above, so
    // the scan's optimum materializes exactly; boxes never touch pane edges
    mapZ = Math.max(0.30, Math.min(1.0, Math.min((cwView - 48) / cwL, (chView - 48) / worldH)));
    mapPX = (cwL - cwView / mapZ) / 2;    // negative when world < pane: centers
    mapPY = (worldH - chView / mapZ) / 2; // the shrunken content (D2)
    // fit-relative ink tier: fine ink ON at the overview (wire clicks work
    // there), one wheel-notch out it drops; restore needs +14% (hysteresis)
    mapInkLo = Math.max(0.35, Math.min(0.85, mapZ * 0.97));
    mapInkHi = Math.min(1.0, mapInkLo * 1.14);
    mapLodFitZ = mapZ;   // [#77] probe: zoom this layout fit at
  }
  mapInkEval();   // hysteresis re-arm after every zoom change
  mapLodEval();   // [#77] band re-arm (paint-only tier)
  // inter-row gap bands + lane machinery (survives section 10)
  const rects = [];
  place.forEach(p => rects.push({ x0: p.x, x1: p.x + p.w, y0: p.y, y1: p.y + p.h }));
  const gapY = [];
  for (let g = 0; g < chunks.length - 1; g++)
    gapY.push({ y0: chunkY[g] + chunkTop[g] + 2, y1: chunkY[g + 1] - 2 });
  const laneX = [];
  const crossedBands = (ya, yb) => {
    const out = [];
    for (let g = 0; g < gapY.length; g++)
      if (gapY[g].y1 > ya && gapY[g].y0 < yb) out.push(g);
    return out;
  };
  let maxX = 90;
  geo.forEach(g => { maxX = Math.max(maxX, g.w); });   // freeX radius scales [F6]
  const freeX = (x, ya, yb, maxR, pad) => {
    x = Math.max(6, Math.min(cwL - 6, x));   // lanes never leave the world
    const span = rects.filter(r => r.y1 > ya && r.y0 < yb);
    const bands = crossedBands(ya, yb);
    const spanAt = c => span.some(r => c >= r.x0 && c <= r.x1);
    // 7px exclusivity: parallel verticals closer than this read as ONE line
    // at 1x zoom (user: "vertical lines overlap") — lanes are a scarce
    // resource, bundling only as the last resort. Trunks scan at 12px so two
    // thick corridors never parallel inside one screen glance (pad param)
    const laneAt = c =>
      bands.some(g => laneX[g] && laneX[g].some(u => Math.abs(u - c) < (pad || 7)));
    // cost 0 = clean lane, 1 = shares a band lane (last resort), 2 = crosses
    // a box (never acceptable) — scan outward, take the first clean slot.
    // Long hauls may scan the whole world: with maxX exhausted they used to
    // accept a box-crossing x (the piercing bug) — a clean column always
    // exists near the world margins, and the trip there is worth it.
    const cost = c => (spanAt(c) ? 2 : 0) + (laneAt(c) ? 1 : 0);
    let bestX = x, bestB = cost(x);
    for (let d = 5; d <= (maxR || maxX) && bestB > 0; d += 5) {
      for (const c of [x + d, x - d]) {
        if (c < 6 || c > cwL - 6) continue;
        const b = cost(c);
        if (b < bestB) { bestB = b; bestX = c; }
        if (b === 0) break;
      }
    }
    // never a bezier; a shared lane bundles, a box-span never survives
    return { x: bestX, ok: bestB < 2, clean: bestB === 0 };
  };
  const claimLane = (x, ya, yb) => {
    for (const g of crossedBands(ya, yb)) (laneX[g] || (laneX[g] = [])).push(x);
  };
  const usedY = [];
  const spanHits = (x0, x1, y) => rects.some(r =>
    y >= r.y0 && y <= r.y1 && x1 >= r.x0 && x0 <= r.x1);
  const nextY = (x0, x1, startY, limitY, step) => {
    step = step || 7;   // 7px channel lattice; thin tap rails may pack at 5
    // channel claims are X-AWARE: two horizontals may share a y when their
    // x-spans barely overlap (a shared channel far apart reads as one line
    // anyway); within an overlapping span the lattice keeps them >=step apart
    const xa = Math.min(x0, x1), xb = Math.max(x0, x1);
    const blocked = y =>
      usedY.some(u => Math.abs(u.y - y) < step &&
        Math.min(u.b, xb) - Math.max(u.a, xa) > 10) ||
      spanHits(xa, xb, y);
    let y = startY;
    while (blocked(y) && y < limitY) y += step;
    // a blocked band admits exhaustion — never let the step overshoot PAST
    // the band floor onto the box tops of the next chunk. Landing EXACTLY on
    // the floor counts as exhaustion too (the 7px lattice from below hits it
    // dead on), and the floor spreads: each exhausted caller climbs one
    // lattice step above the last so exhaustion never stacks two overlapping
    // horizontals within the pairing gate.
    if (y >= limitY) {
      y = limitY;
      while (blocked(y) && y - step >= Math.min(startY, limitY)) y -= step;
    }
    usedY.push({ y, a: xa, b: xb });
    return { y, ok: !blocked(y) };
  };
  // roster row lookup: "fileIx<null>fn" -> row index (wires terminate on rows)
  const rowOf = new Map();
  place.forEach((p, i) => {
    const g = geo.get(i);
    if (!g.roster) return;
    g.roster.rows.forEach((r, k) => rowOf.set(i + "\x00" + r.nm, k));
  });
  // port spreads: box-level for spines/underlays/row-less wires, row-level
  // ---- individual named wires (tier-2 top-1/pair): terminate ON their fn rows
  // Blueprint-reroute buses (2D twin of the 3D bus law): named wires of the
  // same type converging on ONE fn row (>=2) merge at a junction dot parked
  // in open air beside the destination box; members route to the junction
  // (arrowless), ONE shared stub delivers the whole bus into the fn row with
  // a single arrowhead. Shared-destination only (McGee & Dingliana 2012).
  // [issue #78] moved ahead of the port ledger: the ledger's pre-pass needs
  // bus membership to skip destination asks for bus members (their terminus
  // is the junction, not the box edge).
  const busGroups = new Map();
  indiv.forEach(w => {
    if (w.ty === "var") return;         // var wires keep their own dot terminus
    const k = w.df + "\x00" + w.dfn + "\x00" + w.ty;
    let a = busGroups.get(k);
    if (!a) busGroups.set(k, a = []);
    a.push(w);
  });
  const busOf = new Map();               // wire record -> its bus
  const buses = [];                      // junction records for paint + audit
  busGroups.forEach(a => {
    if (a.length < 2) return;
    const B = place.get(a[0].df);
    if (!B) return;
    const dRow = rowOf.get(a[0].df + "\x00" + a[0].dfn);
    if (dRow === undefined) return;      // row-less dests keep individual routes
    // approach side: count source boxes left vs right of the destination
    let Lc = 0, Rc = 0;
    a.forEach(w => {
      const A0 = place.get(w.sf);
      if (!A0) return;
      if (A0.x + A0.w <= B.x) Lc++;
      else if (A0.x >= B.x + B.w) Rc++;
    });
    const side = Lc >= Rc ? -1 : 1;
    const jy = B.y + NH + dRow * RH + RH / 2;      // destination row centre
    // junction must sit in open air: nudge outward twice, else scan the
    // inter-box gaps at 2px pads - depth-varied chunk-row neighbours sit
    // 10-15px apart, and the old 4px pads rejected the whole gap, leaving
    // 9-wire arrival fans where a bus belonged (P3 root cause)
    const jHit = (x, pad) => rects.some(r =>
      x > r.x0 - pad && x < r.x1 + pad && jy > r.y0 - 3 && jy < r.y1 + 3);
    let jx = side < 0 ? B.x - 14 : B.x + B.w + 14;
    if (jHit(jx, 4)) jx = side < 0 ? jx - 10 : jx + 10;
    if (jHit(jx, 4)) {
      let ok = false;
      for (let s = 4; s <= 44 && !ok; s += 2) {
        for (const dx of (side < 0 ? [-s, s] : [s, -s])) {
          const c = B.x + B.w * (side < 0 ? 0 : 1) + (side < 0 ? -14 : 14) + dx;
          if (!jHit(c, 2)) { jx = c; ok = true; break; }
        }
      }
      if (!ok) return;
    }
    const bus = { df: a[0].df, dfn: a[0].dfn, ty: a[0].ty,
                  x: jx, y: jy, n: a.length, wires: a };
    buses.push(bus);
    a.forEach(w => busOf.set(w, bus));
  });
  // [issue #78] port ledger: every box-edge termination registers (box, edge
  // line, caller id, target centre x); ONE packing pass then assigns evenly
  // spaced, target-ORDERED ports per line. The old per-category (k+1)/(n+1)
  // spreads had independent denominators per counter family, so a box-level
  // port and a last-row port could land on the SAME edge line at the SAME
  // fraction (audit: coincident ports, minGap 0) and port order ignored
  // where targets sat. Lookups key on the caller's stable id (pass + array
  // index), not an ordinal: per-box ordinal counters interleave asks across
  // several edge lines of one box, so any drift between this pre-pass and
  // the routing passes landed an ask on a foreign ordinal and the fraction
  // fallback then put two termini on the same pixel. Same filters + same
  // array order => same id in both passes; the hash-spread fallback (a miss
  // is a replica bug) cannot coincide with a packed slot by construction.
  const portLedger = new Map();          // "i|y" -> [{id, tx}]
  const portXY = new Map();              // "i|y|id" -> x offset from box left
  const portAsk = (i, y, id, tx) => {
    const k = i + "|" + (y | 0);
    let a = portLedger.get(k);
    if (!a) portLedger.set(k, a = []);
    a.push({ id, tx });
  };
  const portX = (i, y, id, A) => {
    const x = portXY.get(i + "|" + (y | 0) + "|" + id);
    if (x !== undefined) return A.x + x;
    let h = 0;
    for (let c = 0; c < id.length; c++) h = (h * 31 + id.charCodeAt(c)) % 9973;
    return A.x + 4 + (h / 9973) * Math.max(8, A.w - 8);
  };
  {
    edges.forEach((l, ix) => {            // underlay pass (attach/inst only)
      if (l.ty !== "attach" && l.ty !== "inst") return;
      const A = place.get(l.s), B = place.get(l.t);
      if (!A || !B) return;
      const sy = A.y + A.h;
      const sameRow = A.row === B.row;
      const ty = (sameRow || B.y + B.h <= sy) ? B.y + B.h : B.y;
      portAsk(l.s, sy, "u" + ix + "s", B.x + B.w / 2);
      portAsk(l.t, ty, "u" + ix + "d", A.x + A.w / 2);
    });
    edges.forEach((l, ix) => {            // spine pass (call/signal only)
      if (l.ty === "attach" || l.ty === "inst") return;
      const A = place.get(l.s), B = place.get(l.t);
      if (!A || !B) return;
      const sy = A.y + A.h;
      const sameRow = A.row === B.row;
      const ty = (sameRow || B.y + B.h <= sy) ? B.y + B.h : B.y;
      portAsk(l.s, sy, "s" + ix + "s", B.x + B.w / 2);
      portAsk(l.t, ty, "s" + ix + "d", A.x + A.w / 2);
    });
    indiv.forEach((w, ix) => {            // named wires: box/row terminations
      const A = place.get(w.sf), B = place.get(w.df);
      if (!A || !B) return;
      const sRow = rowOf.get(w.sf + "\x00" + w.sfn);
      const dRow = rowOf.get(w.df + "\x00" + w.dfn);
      const sameRow = A.row === B.row;
      const sy = sRow === undefined ? A.y + A.h : A.y + NH + (sRow + 1) * RH;
      portAsk(w.sf, sy, "w" + ix + "s", B.x + B.w / 2);
      if (busOf.get(w)) return;           // bus member: junction is terminus
      const upW = !sameRow && B.y + B.h <= sy;
      portAsk(w.df, dRow === undefined
        ? (sameRow || upW ? B.y + B.h : B.y)
        : (sameRow || upW ? B.y + NH + (dRow + 1) * RH : B.y + NH + dRow * RH),
        "w" + ix + "d", A.x + A.w / 2);
    });
    portLedger.forEach((a, k) => {        // pack: target order, even pitch
      const i = +k.split("|")[0];
      const A2 = geo.get(i);
      const inset = 4, len = Math.max(8, A2.w - 2 * inset);
      a.sort((p, q) => p.tx - q.tx || (p.id < q.id ? -1 : p.id > q.id ? 1 : 0));
      const pitch = Math.max(1.2, Math.min(24, len / a.length));
      a.forEach((rec, ix2) => portXY.set(k + "|" + rec.id, inset + pitch * (ix2 + 0.5)));
    });
  }
  // [#10] bend census + merger. The router's chamfers and lane-clearing
  // jogs paint serial same-direction bends that read as ONE path on long
  // hauls (the staircase class: a 6-band haul carried ~30 counted turns).
  // turnsOf counts direction changes over ~10deg (45deg chamfer legs
  // count); mergeBends is the deterministic post-pass the issue asks for:
  // collinear points drop, corner-cut diagonals flatten to right angles
  // on over-budget polylines, and micro-jogs (a short cross-run flanked
  // by same-direction runs) consolidate into the through-line. Endpoints
  // never move - terminators, arrowheads and the #63 rebuild-parity pins
  // anchor on them, and the merged ink stays inside the union of the
  // cleared column spans (the diagonal's own bounding box).
  const TURN_FLAT = 12;   // flatten diagonals only on over-budget hauls
  const JOG_PX = 6;       // cross-run short enough to read as one line
  const JOG_MIN_SLANT = 0.10;  // merged jog diagonal >= ~5.7deg off axis
  const turnsOf = pts => {
    let n = 0;
    for (let k = 2; k < pts.length; k++) {
      const ax = pts[k - 1][0] - pts[k - 2][0], ay = pts[k - 1][1] - pts[k - 2][1];
      const bx = pts[k][0] - pts[k - 1][0], by = pts[k][1] - pts[k - 1][1];
      const la = Math.hypot(ax, ay) || 1, lb = Math.hypot(bx, by) || 1;
      if ((ax * bx + ay * by) / (la * lb) < 0.984) n++;
    }
    return n;
  };
  const mergeBends = pts => {
    if (!pts || pts.length < 3) return pts;
    let q = pts;
    for (let round = 0; round < 6; round++) {
      // 1) collinear/zero-length collapse (endpoints kept)
      const c = [q[0]];
      for (let k = 1; k < q.length - 1; k++) {
        const a = c[c.length - 1], b = q[k], e = q[k + 1];
        const d1x = b[0] - a[0], d1y = b[1] - a[1];
        const d2x = e[0] - b[0], d2y = e[1] - b[1];
        if (Math.abs(d1x * d2y - d1y * d2x) < 1e-9 && d1x * d2x + d1y * d2y > 0)
          continue;
        c.push(b);
      }
      c.push(q[q.length - 1]);
      // 2) flatten the corner-cut diagonals on over-budget polylines: a
      // chamfer leg becomes the right angle at its corner (2 turns -> 1)
      let r = c;
      if (turnsOf(c) > TURN_FLAT) {
        const f = [c[0]];
        for (let k = 1; k < c.length; k++) {
          const a = c[k - 1], b = c[k];
          const dx = b[0] - a[0], dy = b[1] - a[1];
          if (dx !== 0 && dy !== 0 &&
              Math.abs(Math.abs(dx) - Math.abs(dy)) < 0.01 &&
              Math.abs(dx) <= 8)
            f.push([a[0], b[1]]);
          f.push(b);
        }
        // an S-pair of chamfers flattens 3 turns into 4 - keep the cut
        // only when the whole path reads fewer bends afterwards
        if (turnsOf(f) < turnsOf(c)) r = f;
      }
      // 3) micro-jog consolidation: a cross-run of <= JOG_PX flanked by
      // same-direction runs on the through axis collapses to the single
      // diagonal the eye already reads (within half the jog of the
      // original everywhere). Shallow result diagonals stay jogs - at
      // JOG_MIN_SLANT they would parallel the corridor lanes (np metric).
      const m = [r[0]];
      let j = 1;
      while (j < r.length - 2) {
        const a = m[m.length - 1], b = r[j], e = r[j + 1], e2 = r[j + 2];
        const d1x = b[0] - a[0], d1y = b[1] - a[1];
        const d2x = e[0] - b[0], d2y = e[1] - b[1];
        const d3x = e2[0] - e[0], d3y = e2[1] - e[1];
        const jogV = d1x === 0 && d2y === 0 && d3x === 0 &&
          Math.sign(d1y) === Math.sign(d3y) &&
          Math.abs(d2x) > 0.5 && Math.abs(d2x) <= JOG_PX &&
          Math.abs(d2x) / Math.max(1, Math.abs(e2[1] - a[1])) >= JOG_MIN_SLANT;
        const jogH = d1y === 0 && d2x === 0 && d3y === 0 &&
          Math.sign(d1x) === Math.sign(d3x) &&
          Math.abs(d2y) > 0.5 && Math.abs(d2y) <= JOG_PX &&
          Math.abs(d2y) / Math.max(1, Math.abs(e2[0] - a[0])) >= JOG_MIN_SLANT;
        if (jogV || jogH) { j += 2; continue; }   // drop b and e: a -> e2
        m.push(b);
        j++;
      }
      m.push(r[r.length - 2], r[r.length - 1]);
      // monotone: the merger may never ADD a bend or a point. The old
      // accept-on-length-only let a flattened S-pair (3 turns -> 4) slip
      // through and lengthen the staircase class it exists to shorten.
      if (turnsOf(m) > turnsOf(q) || m.length >= q.length) return m;
      q = m;
    }
    return q;
  };
  __routeFns = { turnsOf, mergeBends };   // [#10] probe surface for the pins
  const underlays = [], spines = [], wires = [];
  const routeOrtho = (A, B, sy, ty, sameRow, sx0, tx0, bus, lanePad, yPad) => {
    // long hauls cross every chunk between the two rows — hand the router
    // the FULL band range so the staircase can hop chunk-by-chunk. The old
    // 1px window at the target edge returned at most one band, so the
    // staircase never fired and every haul fell to the single-band channel.
    let bands = sameRow
      ? crossedBands(sy, sy + 1)
      : crossedBands(Math.min(sy, ty), Math.max(sy, ty));
    if (!bands.length) {
      // same-row dip: drop to the first gap band BELOW the row
      const bi = gapY.findIndex(g => g.y0 > sy);
      if (bi >= 0) bands = [bi];
    }
    // exit-hoist: if A is shorter than a row-mate, the bottom-edge exit
    // horizontal would slice through it — drop to the row's true bottom
    // first (the sx0 drop is clean: same-row boxes never overlap A's x-span)
    let syE = sy;
    for (const r of rects) {
      if (r === A || r.y0 >= sy - 2 || r.y1 <= sy + 2) continue;
      syE = Math.max(syE, r.y1);
    }
    const up = ty < sy;
    // STAIRCASE for long hauls (ELK between-layer law): a clean column
    // through EVERY chunk rarely exists, so hop band-by-band — one vertical
    // per chunk, each cleared against that chunk only, channels accumulating
    // in the bands as a metro yard. Verticals can no longer pierce a row.
    if (bands.length >= 2) {
      // both build directions run top-to-bottom (up-hauls start at the
      // target's bottom edge), so bands ascend toward the cursor either way
      const seq = bands;
      const xStart = up ? tx0 : sx0, xEnd = up ? sx0 : tx0;
      // attach-edge law: up-hauls leave the source TOP edge, down-hauls the
      // bottom (syE hoist) — the vertical to the first band then only ever
      // crosses the 2px margin, never the row's own boxes
      const yTop = up ? ty : syE, yBot = up ? A.y : ty;
      const B2 = up ? A : B;
      const cols = [];
      let seed = xStart, prevY = yTop;
      for (let i = 0; i < seq.length; i++) {
        const g = gapY[seq[i]];
        const yc = nextY(Math.min(seed, xEnd), Math.max(seed, xEnd),
                         g.y0 + 3 + (yPad || 0), g.y1);
        const fx = freeX(seed, prevY, yc.y, cwL, lanePad);
        if (fx.ok) claimLane(fx.x, prevY, yc.y);
        cols.push({ x: fx.x, yCh: yc.y, yFloor: g.y1 });
        seed = fx.x;
        prevY = yc.y;
      }
      const fxN = freeX(seed, prevY, yBot, cwL, lanePad);
      const xN = Math.max(B2.x + 2, Math.min(B2.x + B2.w - 2, fxN.x));
      if (fxN.ok) claimLane(xN, prevY, yBot);
      const p = [[xStart, yTop]];
      let cx = xStart, cy = yTop;
      for (let i = 0; i < cols.length; i++) {
        const c = cols[i], nx = i + 1 < cols.length ? cols[i + 1].x : xN;
        const d = nx >= c.x ? 1 : -1;
        const chIn = Math.max(0, Math.min(8,
          Math.abs(c.x - cx) / 2, (c.yCh - cy) / 2));
        if (Math.abs(c.x - cx) > 0.5) p.push([c.x, cy]);
        p.push([c.x, c.yCh - chIn], [c.x + d * chIn, c.yCh]);
        // chamfer descent never leaves the band: floor-clamped, else the
        // post-channel horizontal slices the next chunk's box tops
        const chOut = Math.max(0, Math.min(8, Math.abs(nx - c.x) / 2,
          c.yFloor - c.yCh));
        p.push([nx - d * chOut, c.yCh], [nx, c.yCh + chOut]);
        cx = nx; cy = c.yCh + chOut;
      }
      // orthogonal arrival: chamfer into the target column, then drop/rise
      // vertically onto the port — the final leg must never be a diagonal
      const dE = xEnd >= cx ? 1 : -1;
      const chE = Math.max(0, Math.min(8, Math.abs(xEnd - cx) / 2,
        Math.abs(yBot - cy) / 2));
      p.push([xEnd - dE * chE, cy], [xEnd, cy + (yBot >= cy ? chE : -chE)],
             [xEnd, yBot]);
      if (up) p.reverse();
      if (!up && syE > sy) p.unshift([sx0, sy]);
      // terminator anchors on pts[last] = (tx0, ty) for BOTH directions;
      // the old up ? sx0 floated arrowheads / T-ticks off the path end
      return { pts: p, bez: false, tx: tx0, ty,
               back: ty < sy };
    }
    // channel placement: scan each gap band in order; a band that admits a
    // clear horizontal wins, an exhausted band is only the bundled fallback
    // (channels never slice through the chunk between bands)
    let yc = null;
    for (const bi of bands) {
      const g = gapY[bi];
      if (!g) continue;
      const t = nextY(Math.min(sx0, tx0), Math.max(sx0, tx0),
                      g.y0 + 3 + (yPad || 0), g.y1);
      if (!yc) yc = t;
      if (t.ok) { yc = t; break; }
    }
    if (!yc) yc = { y: (sy + ty) / 2, ok: false };
    const yCh = yc.y;
    const sY = up ? A.y : syE;   // attach edge = the side facing the channel
    const fx = freeX(sx0, Math.min(sY, yCh), Math.max(sY, yCh), null, lanePad);
    const sx = fx.x;
    if (fx.ok) claimLane(sx, sY, yCh);
    const fx2 = freeX(tx0, Math.min(yCh, ty), Math.max(yCh, ty), null, lanePad);
    const tx = Math.max(B.x + 2, Math.min(B.x + B.w - 2, fx2.x));
    if (fx2.ok) claimLane(tx, yCh, ty);
    // freeX/nextY no longer fail: clean lanes first, else bundled corridors —
    // the bezier fallback died with the diagonal layer
    const dir = tx >= sx ? 1 : -1;
    const ch = Math.max(0, Math.min(8, Math.abs(tx - sx) / 2,
      Math.abs(yCh - sY) / 2, Math.abs(yCh - ty) / 2));
    const tyDir = ty >= yCh ? 1 : -1;   // approach side of the channel
    const sDir = yCh >= sY ? 1 : -1;    // source side of the channel
    return {
      pts: !up && syE > sy
        ? [[sx0, sy], [sx0, syE], [sx, syE], [sx, yCh - ch],
           [sx + dir * ch, yCh], [tx - dir * ch, yCh],
           [tx, yCh + tyDir * ch], [tx, ty]]
        : [[sx0, sY], [sx, sY], [sx, yCh - sDir * ch], [sx + dir * ch, yCh],
           [tx - dir * ch, yCh], [tx, yCh + tyDir * ch],
           [tx, ty]],
      bez: false, tx, ty,
      back: !sameRow && ty < sy,
    };
  };
  // 1) attach/inst underlays: anonymous + demoted (section 1) - 1px, alpha
  //    0.40, T-junction entry, routed FIRST so named wires claim lanes first
  edges.forEach((l, ix) => {
    if (l.ty !== "attach" && l.ty !== "inst") return;
    const A = place.get(l.s), B = place.get(l.t);
    if (!A || !B) return;
    const sy = A.y + A.h;
    // same CHUNK row, not same BFS depth: depth rows wrap to world width,
    // so equal fd can land in adjacent chunks — routing those as a same-row
    // dip dropped the channel mid-air (yCh = midpoint) through box interiors
    const sameRow = A.row === B.row;
    // up-hauls enter the target's bottom edge (the source sits below it)
    const ty = (sameRow || B.y + B.h <= sy) ? B.y + B.h : B.y;
    const sx0 = portX(l.s, sy, "u" + ix + "s", A);
    const tx0 = portX(l.t, ty, "u" + ix + "d", B);
    const ur = routeOrtho(A, B, sy, ty, sameRow, sx0, tx0, true);
    ur.flow = sameRow ? "same" : (ty > sy ? "down" : "up");
    underlays.push(Object.assign({ s: l.s, t: l.t, ty0: l.ty }, ur));
  });
  // 2) corridor spines (section 2 tier-1): records first - routing waits for
  //    the hub-bus grouping, which decides who rides a shared trunk. A
  //    signal pair that resolved zero handlers renders as an anonymous amber
  //    corridor [F13]. wty = wire TYPE (sp.ty stays the y-coordinate that
  //    routeOrtho returns; the old build let the y overwrite l.ty, so every
  //    spine painted call-gray - the "near-identical gray wires" complaint).
  edges.forEach((l, ix) => {
    if (l.ty === "attach" || l.ty === "inst") return;
    const A = place.get(l.s), B = place.get(l.t);
    if (!A || !B) return;
    const sy = A.y + A.h;
    // same CHUNK row, not same BFS depth: depth rows wrap to world width,
    // so equal fd can land in adjacent chunks (see underlays)
    const sameRow = A.row === B.row;
    // up-hauls enter the target's bottom edge (the source sits below it)
    const ty = (sameRow || B.y + B.h <= sy) ? B.y + B.h : B.y;
    const sx0 = portX(l.s, sy, "s" + ix + "s", A);
    const tx0 = portX(l.t, ty, "s" + ix + "d", B);
    spines.push({ s: l.s, t: l.t, wty: l.ty, pair: l.s + "_" + l.t,
      amber: l.ty === "signal" && !(byPair.get(l.s + "_" + l.t) || []).length,
      sRow: A.row, tRow: B.row, sx0, tx0, sy, ty, sameRow,
      flowSum: l.w || 1, con: false, pts: [], bez: false,
      flow: sameRow ? "same" : (ty > sy ? "down" : "up"), back: false });
  });
  // ---- hub buses (Blueprint reroute, tier-1): every corridor leaving ONE
  // source in ONE direction (down/up/same) merges onto a single thick trunk
  // that rides routeOrtho to the deterministic farthest rider's REAL box;
  // each rider is delivered by a thin type-colored tap peeling off the trunk
  // in the band adjacent to its row. Peel points are router OUTPUT (points
  // on the routed trunk), never invented geometry - routeOrtho stays the one
  // authority, so the no-diagonal law holds by construction.
  // Deterministic: buses keyed source+direction, insertion = edges order
  // (w desc, s, t); farthest row numeric; port picks are lower medians.
  const hubBuses = [];                  // {trunk, taps, peels, members}
  const hubDots = [];                   // junction dots painted ON paths
  let hubTrunks = 0, hubTaps = 0, hubRiders = 0, hubPeels = 0;
  const dirOf = sp => sp.sameRow ? "same" : (sp.tRow > sp.sRow ? "down" : "up");
  const spineBusMap = new Map();
  spines.forEach(sp => {
    if (sp.amber) return;               // F13: amber corridors stay individual
    const k = sp.s + "|" + dirOf(sp);
    let a = spineBusMap.get(k);
    if (!a) spineBusMap.set(k, a = []);
    a.push(sp);
  });
  spineBusMap.forEach(a => {
    if (a.length < 2) return;
    const dir = dirOf(a[0]);
    // same-row dips need a band below the source chunk to park the channel
    if (dir === "same" && !gapY[a[0].sRow]) return;
    const rows = [...new Set(a.map(x => x.tRow))].sort((p, q) => p - q);
    const farRow = dir === "up" ? rows[0] : rows[rows.length - 1];
    const inFar = a.filter(x => x.tRow === farRow)
      .sort((x, y) => x.tx0 - y.tx0 || (x.pair < y.pair ? -1 : 1));
    const far = inFar[Math.floor((inFar.length - 1) / 2)];
    const A = place.get(far.s), Bf = place.get(far.t);
    const flowSum = a.reduce((s, x) => s + x.flowSum, 0);
    const wTr = Math.min(2 + 0.85 * Math.log2(flowSum), 5.5);
    // trunk: STRAIGHT-COLUMN FIRST (metro few-bends law): if one clean
    // vertical lane spans the whole haul (freeX cost 0 - usually near the
    // world margins), the trunk is exit-channel -> column -> arrival-channel
    // (5 bends) instead of a per-band staircase (2+ bends per band crossed).
    // Staircase is the fallback when every column pierces a box.
    // Wide lane pad (no two trunks parallel inside 12px), channel floor
    // seeded below the stroke half-width so it never bleeds onto box tops.
    // mapZ can never seed layout decisions: the layout is zoom-independent by
    // contract (the oracle caches by sig+expand+size, and a rebuild at a
    // non-fit zoom would otherwise change lane spacing between sessions).
    // The old zoom-scaled pad leaked view state into the layout; the fixed
    // world-space 12px matches the "trunks scan at 12px" rule in freeX.
    const lanePad = 12, yPad = 3 + Math.ceil(wTr / 2);
    let tr = null;
    if (dir !== "same") {
      const bi = dir === "down" ? A.row : A.row - 1;      // exit-side band
      const ai = dir === "down" ? farRow - 1 : farRow;    // arrival band
      const gb = gapY[bi], ga = gapY[ai];
      const sYx = dir === "down" ? A.y + A.h : A.y;
      if (gb && ga && ((dir === "down" && sYx <= gb.y0) ||
                       (dir === "up" && sYx >= gb.y1))) {
        const fx = freeX(far.sx0, gb.y0 + 3, ga.y1, cwL, lanePad);
        if (fx.clean) {
          const yEx = nextY(Math.min(far.sx0, fx.x), Math.max(far.sx0, fx.x),
            gb.y0 + 3 + yPad, gb.y1);
          const yAr = nextY(Math.min(fx.x, far.tx0), Math.max(fx.x, far.tx0),
            ga.y0 + 3 + yPad, ga.y1);
          claimLane(fx.x, yEx.y, yAr.y);
          tr = { pts: [[far.sx0, sYx], [far.sx0, yEx.y], [fx.x, yEx.y],
                       [fx.x, yAr.y], [far.tx0, yAr.y], [far.tx0, far.ty]],
                 bez: false, tx: far.tx0, ty: far.ty, back: dir === "up" };
        }
      }
    }
    if (!tr) tr = routeOrtho(A, Bf, far.sy, far.ty, far.sameRow, far.sx0,
      far.tx0, true, lanePad, yPad);
    // dominant type color: honest flow per type, ties by member count, name
    const tySum = new Map();
    a.forEach(x => tySum.set(x.wty, (tySum.get(x.wty) || 0) + x.flowSum));
    const tyCnt = new Map();
    a.forEach(x => tyCnt.set(x.wty, (tyCnt.get(x.wty) || 0) + 1));
    const domTy = [...tySum.keys()].sort((p, q) =>
      tySum.get(q) - tySum.get(p) || tyCnt.get(q) - tyCnt.get(p) ||
      (p < q ? -1 : 1))[0];
    const trunk = Object.assign({ s: far.s, t: far.t, wty: domTy, pair: far.pair,
      hub: "trunk", trunkW: a.length, flowSum }, tr);
    trunk.flow = dir === "same" ? "same" : dir;
    spines.push(trunk);   // strokes in the spine pass, before its taps
    // peel points: the trunk's horizontal run inside each rider row's
    // adjacent band (down: band above the row; up/same: band below it)
    const peels = [];
    rows.forEach(r => {
      const bi = dir === "down" ? r - 1 : r;
      const g = gapY[bi];
      if (!g || bi < 0) return;
      let seg = null;
      for (let i = 0; i < trunk.pts.length - 1; i++) {
        const p = trunk.pts[i], q = trunk.pts[i + 1];
        if (p[1] === q[1] && p[1] > g.y0 && p[1] < g.y1) {
          seg = { x0: Math.min(p[0], q[0]), x1: Math.max(p[0], q[0]), y: p[1] };
          break;
        }
      }
      if (!seg) {   // degenerate: vertical trunk through the band - peel at
        let best = null, bd = 1e9;   // the waypoint nearest the band centre
        trunk.pts.forEach(p => {
          const d = Math.abs(p[1] - (g.y0 + g.y1) / 2);
          if (d < bd) { bd = d; best = p; }
        });
        if (best) seg = { x0: best[0], x1: best[0], y: best[1] };
      }
      if (!seg) return;
      const mr = a.filter(x => x.tRow === r).map(x => x.tx0).sort((p, q) => p - q);
      const med = mr[Math.floor((mr.length - 1) / 2)];
      const px = seg.x1 - seg.x0 >= 8
        ? Math.max(seg.x0 + 2, Math.min(seg.x1 - 2, med)) : (seg.x0 + seg.x1) / 2;
      peels.push({ row: r, x: px, y: seg.y });
    });
    const busRec = { trunk, peels, members: a, taps: [] };
    // taps: ONE delivery rail per (bus x row), 7px off the trunk's channel
    // (side facing the targets), placed by the same nextY/usedY machinery.
    // Each tap = peel dot -> rail -> port drop: strict orthogonal, 2 bends,
    // no chamfers - the EDA "one thick trunk, thin perpendicular taps" read.
    // One rail instead of k fan polylines keeps channels - and the 4px
    // near-parallel budget - free for other traffic.
    const served = new Set();
    peels.forEach(P => {
      const bi = dir === "down" ? P.row - 1 : P.row;
      const g = gapY[bi];
      if (!g) return;
      const members = a.filter(sp => sp.tRow === P.row);
      const ports = members.map(sp => sp.tx0);
      // rail = 7px off the trunk channel, on the target-facing side. If that
      // would clamp onto the band floor (where exhausted traffic piles into
      // one gray mass), flip to the channel's other side instead - rails
      // must ladder, never join the floor pile.
      const off = dir === "down" ? 7 : -7;
      let railSeed = P.y + off;
      if (railSeed > g.y1 - 3 || railSeed < g.y0 + 3) railSeed = P.y - off;
      railSeed = Math.max(g.y0 + 3, Math.min(g.y1 - 3, railSeed));
      const rail = nextY(Math.min(P.x, Math.min(...ports)),
        Math.max(P.x, Math.max(...ports)), railSeed, g.y1, 5);
      members.forEach((sp, mk) => {
        // [issue #78] the far rider's own terminus IS the trunk terminus
        // (trunk routes to far.tx0/far.ty) - the trunk already delivers
        // that port; a tap there would re-terminate on the same pixel
        if (sp === far) { served.add(sp); return; }
        // [issue #78] taps sharing one peel stagger their exit x (member
        // order) so no two taps start on the same pixel at the peel dot
        const ox = (mk - (members.length - 1) / 2) *
          Math.max(2, Math.min(3, 12 / members.length));
        spines.push({ s: sp.s, t: sp.t, wty: sp.wty, pair: sp.pair,
          hub: "tap", tapBus: busRec, bez: false, tx: sp.tx0, ty: sp.ty,
          back: false, flow: dir === "same" ? "up" : dir,
          pts: [[P.x + ox, P.y], [P.x + ox, rail.y], [sp.tx0, rail.y], [sp.tx0, sp.ty]] });
        busRec.taps.push(spines[spines.length - 1]);
        served.add(sp);
        hubTaps++;
      });
    });
    a.forEach(sp => {
      if (!served.has(sp)) return;       // unserved riders stroke solo below
      sp.con = true;                     // rider: chip carrier, no stroke
      sp.gLeader = trunk;
      sp.bus = busRec;
      sp.pts = [];
    });
    hubDots.push({ x: trunk.pts[0][0], y: trunk.pts[0][1], c: domTy });
    peels.forEach(p => hubDots.push({ x: p.x, y: p.y, c: domTy }));
    hubBuses.push(busRec);
    hubTrunks++; hubRiders += a.length; hubPeels += peels.length;
  });
  // band consolidation backstop (L-C): leftover cross-row corridors sharing
  // the same chunk-row hop still share ONE trunk - the leader's route at
  // combined width. Hub-bus riders and amber corridors are not eligible;
  // eligibility is sRow !== tRow (the old xb field died with its unordered
  // crossedBands(sy, ty) args - up-hauls always read as band-less).
  const spineGroups = new Map();
  spines.forEach(sp => {
    if (sp.amber || sp.bus || sp.con || sp.hub || sp.sameRow) return;
    const k = sp.sRow + ">" + sp.tRow;
    let a = spineGroups.get(k);
    if (!a) spineGroups.set(k, a = []);
    a.push(sp);
  });
  let trunkGroups = 0;
  spineGroups.forEach(a => {
    if (a.length < 2) return;
    trunkGroups++;
    a[0].trunkW = a.length;               // leader strokes at combined width
    a[0].flowSum = a.reduce((s, x) => s + x.flowSum, 0);   // honest flow
    for (let j = 1; j < a.length; j++) {
      a[j].con = true;                    // twin: chip carrier, no stroke
      a[j].gLeader = a[0];
      a[j].gIx = j;
    }
  });
  // routing pass: everyone still stroking gets its route now, in edges
  // order, AFTER the hub trunks/taps claimed their lanes (deterministic)
  spines.forEach(sp => {
    if (sp.con || sp.hub) return;
    const sr = routeOrtho(place.get(sp.s), place.get(sp.t), sp.sy, sp.ty,
      sp.sameRow, sp.sx0, sp.tx0, true);
    Object.assign(sp, sr);
  });
  // stroked-pair -> honest flow: the width driver mapPaint reads. Riders
  // are excluded - they stroke nothing, their ink rides the trunk.
  // zero-length waypoints (staircase corner artifacts) inflate bend counts
  // and segment censuses - collapse consecutive duplicate points
  spines.forEach(sp => {
    if (sp.pts.length < 2) return;
    const q = [sp.pts[0]];
    for (let k = 1; k < sp.pts.length; k++) {
      const l = q[q.length - 1];
      if (Math.hypot(sp.pts[k][0] - l[0], sp.pts[k][1] - l[1]) > 0.01) q.push(sp.pts[k]);
    }
    sp.pts = q;
  });
  const pairW = new Map();
  spines.forEach(sp => { if (!sp.con && sp.pts.length) pairW.set(sp.pair, sp.flowSum); });
  // 3) individual named wires (tier-2 top-1/pair): terminate ON their fn rows
  // Blueprint-reroute buses (2D twin of the 3D bus law): named wires of the
  // same type converging on ONE fn row (>=2) merge at a junction dot parked
  // in open air beside the destination box; members route to the junction
  // (arrowless), ONE shared stub delivers the whole bus into the fn row with
  // a single arrowhead. Shared-destination only (McGee & Dingliana 2012).
  indiv.forEach((w, ix) => {
    const A = place.get(w.sf), B = place.get(w.df);
    if (!A || !B) return;
    const sRow = rowOf.get(w.sf + "\x00" + w.sfn);
    const dRow = rowOf.get(w.df + "\x00" + w.dfn);
    const sameRow = A.row === B.row;   // chunk-row truth: fd wraps (see spines)
    const sy = sRow === undefined ? A.y + A.h : A.y + NH + (sRow + 1) * RH;
    const sx0 = portX(w.sf, sy, "w" + ix + "s", A);
    const bus = busOf.get(w);
    if (bus) {
      // reroute member: source port -> junction dot. A 2px virtual box at
      // the junction keeps routeOrtho's lane/claim machinery authoritative.
      // [issue #78] members fan into the junction (per-member x offset,
      // bus order) instead of every terminus stacking on the exact junction
      // pixel - a tight fan reads as convergence, identical endpoints read
      // as one wire.
      const mi = bus.wires.indexOf(w);
      const mOff = (mi - (bus.wires.length - 1) / 2) *
        Math.min(4, 24 / bus.wires.length);
      const JB = { x: bus.x - 1 + mOff, w: 2, y: bus.y - 1, h: 2 };
      const wr = routeOrtho(A, JB, sy, bus.y, false, sx0, bus.x + mOff);
      wr.flow = bus.y > sy ? "down" : "up";
      wr.noArr = true;                   // the junction dot is the terminus
      wires.push(Object.assign({
        sf: w.sf, sfn: w.sfn, df: w.df, dfn: w.dfn, ty: w.ty, line: w.line,
        up: false, pair: w.sf + "_" + w.df,
      }, wr));
      return;
    }
    const upW = !sameRow && B.y + B.h <= sy;
    const ty = dRow === undefined
      ? (sameRow || upW ? B.y + B.h : B.y)
      : (sameRow || upW ? B.y + NH + (dRow + 1) * RH
                        : B.y + NH + dRow * RH);
    const tx0 = portX(w.df, ty, "w" + ix + "d", B);
    // cardinal routing: two boxes side by side on the SAME row with a clear
    // corridor connect STRAIGHT ACROSS — exit one side edge, enter the other
    // (Unreal/Mermaid law: no dip-down-up detour for a horizontal neighbor).
    // A box standing between the pair blocks the corridor -> dip route.
    let wr;
    const side = B.x >= A.x + A.w ? 1 : (B.x + B.w <= A.x ? -1 : 0);
    const gapL = Math.min(A.x + A.w, B.x + B.w);
    const gapR = Math.max(A.x, B.x);
    const blocked = !side || rects.some(r =>
      r !== A && r !== B && sy >= r.y0 && sy <= r.y1 &&
      r.x1 > gapL && r.x0 < gapR);
    // the shortcut is same-row-only: side is x-only, so for cross-row pairs
    // it drew a straight line at the source port height through foreign boxes
    if (sameRow && !blocked) {
      const x0 = side > 0 ? A.x + A.w : A.x;
      const x1 = side > 0 ? B.x : B.x + B.w;
      const ch2 = Math.max(0, Math.min(8, Math.abs(x1 - x0) / 2));
      // straight-across rides the SOURCE row line; a hidden source fn exits
      // at the box BOTTOM, which can sit past the target row - jog onto the
      // row so the terminus never floats below the box (P2)
      const p = [[sx0, sy], [x0 + side * ch2, sy],
                 [x1 - side * ch2, sy], [tx0, sy]];
      if (Math.abs(ty - sy) > 0.5) p.push([tx0, ty]);
      wr = { pts: p,
             bez: false, tx: tx0, ty, back: false };
    } else wr = routeOrtho(A, B, sy, ty, sameRow, sx0, tx0);
    wr.flow = sameRow ? "same" : (ty > sy ? "down" : "up");
    wires.push(Object.assign({
      sf: w.sf, sfn: w.sfn, df: w.df, dfn: w.dfn, ty: w.ty, line: w.line,
      up: sameRow, pair: w.sf + "_" + w.df,
    }, wr));
  });
  // bus delivery stubs: one shared arrival per junction (Blueprint reroute
  // law: many in, one corridor, all arrive at the same end)
  buses.forEach(bus => {
    const B = place.get(bus.df);
    if (!B) return;
    const dRow = rowOf.get(bus.df + "\x00" + bus.dfn);
    if (dRow === undefined) return;
    const ty = B.y + NH + dRow * RH;            // fn-row top = delivery port
    // stubs sharing a destination fn row spread off centre deterministically
    // (bus order) instead of stacking every arrival on the box centre
    const lineMates = buses.filter(u =>
      place.get(u.df) === B && u.dfn === bus.dfn);
    const c = lineMates.indexOf(bus);
    const spread = Math.min(14, B.w / (lineMates.length + 1));
    const tx0 = B.x + B.w / 2 + (c - (lineMates.length - 1) / 2) * spread;
    wires.push({
      sf: bus.df, sfn: bus.dfn, df: bus.df, dfn: bus.dfn, ty: bus.ty,
      line: -1, up: false, pair: bus.df + "_bus", stub: true, busN: bus.n,
      mates: bus.wires.map(m =>
        nodes[m.sf].label + "::" + m.sfn + " \u2192 @" + m.line),
      // [issue #78] stub departs just below the junction dot so it never
      // shares a pixel with the centered member's arrival
      pts: [[bus.x, bus.y + 2.5], [bus.x, ty], [tx0, ty]],
      bez: false, tx: tx0, ty, back: false, flow: "down",
    });
  });
  // quiet edges (addendum rule 5): no wire text by default - identity is
  // the pin a wire leaves from + the hover tooltip; chips carry bundles
  // ---- bundle chips (section 7 [F3]): typed "xN" micro-chips on the spine
  const chipAnchor = pts => {
    let tot = 0;
    for (let s = 0; s < pts.length - 1; s++)
      tot += Math.hypot(pts[s + 1][0] - pts[s][0], pts[s + 1][1] - pts[s][1]);
    let acc = 0;
    for (let s = 0; s < pts.length - 1; s++) {
      const d = Math.hypot(pts[s + 1][0] - pts[s][0], pts[s + 1][1] - pts[s][1]);
      if (acc + d >= tot / 2) {
        const t = (tot / 2 - acc) / (d || 1);
        return [pts[s][0] + (pts[s + 1][0] - pts[s][0]) * t,
                pts[s][1] + (pts[s + 1][1] - pts[s][1]) * t];
      }
      acc += d;
    }
    return pts[0];
  };
  const chips = [];
  const ridersOf = sps => sps.flatMap(sp =>
    (byPair.get(sp.pair) || []).filter(w => !indivSet.has(w)));
  // peel badges: ONE aggregate per (hub bus x delivery row), anchored at the
  // peel dot where that row's taps split off (EDA: badge at the tap). The
  // badge lists every named wire riding the trunk into that row; x1 badges
  // are suppressed - a single rider is the visible wire itself.
  hubBuses.forEach(bus => {
    bus.peels.forEach(P => {
      const members = bus.members.filter(m => m.tRow === P.row);
      const riders = ridersOf(members);
      if (riders.length < 2) return;
      const txt = "\u00d7" + riders.length;
      const cwid = txtW(txt) + 10;
      chips.push({ pair: null, s: bus.members[0].s, t: -1, row: P.row,
        peel: true, ty: bus.trunk.wty, n: riders.length,
        x: P.x - cwid / 2, y: P.y - 21, w: cwid, h: 14, wires: riders });
    });
    // origin badge: ONE per bus at the trunk's departure dot, n = TOTAL
    // riders - disambiguates the per-row peel badges (x4 at a row of a
    // 12-rider trunk now reads as tap-count vs pipe-count)
    const all = ridersOf(bus.members);
    if (all.length >= 2 && bus.trunk.pts && bus.trunk.pts.length) {
      const t0 = bus.trunk.pts[0];
      const txt0 = "\u00d7" + all.length;
      const cw0 = txtW(txt0) + 10;
      chips.push({ pair: null, s: bus.members[0].s, t: -1, row: -1,
        origin: true, ty: bus.trunk.wty, n: all.length,
        x: t0[0] - cw0 - 6, y: t0[1] - 20, w: cw0, h: 14, wires: all });
    }
  });
  // remaining stroked spines keep per-pair badges (L-C leaders aggregate
  // their twins' riders; singles list their own), same n>=2 gate
  const chipSpines = spines.filter(sp =>
    !sp.bus && !sp.con && sp.pts.length && !sp.hub);
  const donePair = new Set();
  chipSpines.forEach(sp => {
    if (donePair.has(sp.pair)) return;
    donePair.add(sp.pair);
    const grp = spineGroups.get(sp.sRow + ">" + sp.tRow);
    const members = grp && grp[0] === sp ? grp : [sp];
    const riders = ridersOf(members);
    if (riders.length < 2) return;
    const anc = chipAnchor(sp.pts);
    const txt = "\u00d7" + riders.length;
    const cwid = txtW(txt) + 10;
    chips.push({ pair: sp.pair, s: sp.s, t: sp.t, ty: sp.wty,
      n: riders.length, x: anc[0] - cwid / 2, y: anc[1] - 15,
      w: cwid, h: 14, wires: riders });
  });
  // badge de-overlap at LAYOUT time: first-clear grid over dy (band-side
  // first) x dx offsets against box rects and earlier chips (2px pad).
  // Deterministic in chips order; the paint ladder remains the last resort.
  chips.forEach((c, ci) => {
    const hitR = (x, y) =>
      rects.some(r => x - 2 < r.x1 && x + c.w + 2 > r.x0 &&
                      y - 2 < r.y1 && y + c.h + 2 > r.y0) ||
      chips.some((o, oi) => oi < ci &&
        x - 2 < o.x + o.w && x + c.w + 2 > o.x &&
        y - 2 < o.y + o.h && y + c.h + 2 > o.y);
    if (!hitR(c.x, c.y)) return;
    const xs = [0, c.w + 8, -(c.w + 8), 2 * (c.w + 8), -2 * (c.w + 8)];
    const ys = [0, 7, -7, 14, -14, 21, -21, 28, -28, 35, -35, 42];
    outer:
    for (const dy of ys) for (const dx of xs) {
      const nx = Math.max(6, Math.min(cwL - c.w - 6, c.x + dx));
      if (!hitR(nx, c.y + dy)) { c.x = nx; c.y += dy; break outer; }
    }
  });
  // wires + bus stubs: collapse zero-length waypoints (same law as spines -
  // the lone stray arrowhead read its direction off a zero-length tail)
  wires.forEach(w => {
    if (!w.pts || w.pts.length < 2) return;
    const q = [w.pts[0]];
    for (let k = 1; k < w.pts.length; k++) {
      const l = q[q.length - 1];
      if (Math.hypot(w.pts[k][0] - l[0], w.pts[k][1] - l[1]) > 0.01) q.push(w.pts[k]);
    }
    w.pts = q;
  });
  // [#10] bend merger application: every routed polyline that paints gets
  // the post-pass - underlays, spines (stroked + riders ride the trunk),
  // wires and bus stubs. Deterministic pure function of pts, so the #63
  // rebuild-parity pins (trunk geometry across zoom rebuilds) hold.
  [underlays, spines, wires].forEach(arr => arr.forEach(rec => {
    if (rec.pts && rec.pts.length > 2) rec.pts = mergeBends(rec.pts);
  }));
  mapRects = [];
  place.forEach((p, i) => {
    const g = geo.get(i);
    const rc = { i, x: p.x, y: p.y, w: p.w, h: g.h, rows: [], more: null };
    if (g.roster) {
      g.roster.rows.forEach((r, k) => rc.rows.push({
        nm: r.nm, y0: p.y + NH + k * RH, y1: p.y + NH + (k + 1) * RH }));
      if (g.more.length)
        rc.more = { y0: p.y + NH + g.roster.rows.length * RH,
                    y1: p.y + NH + (g.roster.rows.length + 1) * RH };
    }
    mapRects.push(rc);
  });
  // route audit: numeric truth for the merge gate (VLM reads of 1.5px
  // curves are unreliable). Rebuilt on every layout build; cached repaints
  // keep the last build's numbers — they describe the same layout.
  // [#77] unbundling ledger (layout-derived, deterministic): for every
  // corridor pair the riders plus ONE straight paint path per rider
  // (source-row port -> dest-row port, clamped to the boxes). Consumed
  // ONLY by paint while the pair is pinned (AVI'12 tracing aid); corridor
  // ink itself is untouched - this adds fields, changes no bytes.
  // [#325 audit] byPair keys are separator-constructed "sf_df" pair
  // strings — never a bare prototype name; safe as {}.
  const pairRiders = {};  byPair.forEach((ws, pr) => {
    const riders = ws.filter(w => !indivSet.has(w));
    if (!riders.length) return;
    const paths = riders.map(w => {
      const A = place.get(w.sf), B = place.get(w.df);
      if (!A || !B) return null;
      const gA = geo.get(w.sf), gB = geo.get(w.df);
      const nA = gA && gA.roster ? gA.roster.rows.length : 0;
      const nB = gB && gB.roster ? gB.roster.rows.length : 0;
      const sRow = rowOf.has(w.sf + "\0" + w.sfn) ? rowOf.get(w.sf + "\0" + w.sfn) : nA - 1;
      const dRow = rowOf.has(w.df + "\0" + w.dfn) ? rowOf.get(w.df + "\0" + w.dfn) : nB - 1;
      const sy = Math.min(Math.max(A.y + NH + (Math.max(0, sRow) + 1) * RH, A.y + 6), A.y + A.h - 2);
      const ty = Math.min(Math.max(B.y + NH + (Math.max(0, dRow) + 1) * RH, B.y + 6), B.y + B.h - 2);
      const fwd = B.x + B.w / 2 >= A.x + A.w / 2;   // draw toward the dest
      const sx = fwd ? A.x + A.w : A.x;
      const tx = fwd ? B.x : B.x + B.w;
      return { sf: w.sf, sfn: w.sfn, df: w.df, dfn: w.dfn, ty: w.ty, line: w.line,
               pts: [[sx, sy], [tx, ty]], bez: false, tx, ty, back: false };
    }).filter(Boolean);
    if (paths.length) pairRiders[pr] = paths;
  });
  const audit = { named: vw.length, indiv: indiv.length, admitted: E,
    spines: 0, underlays: underlays.length, wires: wires.length,
    trunkGroups, buses: buses.length,
    busW: buses.reduce((s, b) => s + b.n, 0),
    hubTrunks, hubTaps, hubRiders, hubPeels,
    bez: 0, back: 0, down: 0, up: 0, sameRow: 0,
    // [#10] post-merge turn census over STROKED ink (named wires + spines
    // that paint; riders ride trunks and count via their trunk): the
    // staircase-class metric, readable from routeAudit by the pins.
    maxTurns: 0, over20: 0, turnsSum: 0, turnsN: 0, maxTrunkTurns: 0 };
  // stroked spines only (riders carry pts:[] - their ink rides the trunk)
  spines.forEach(sp => { if (!sp.con && sp.pts.length) audit.spines++; });
  [underlays, spines, wires].forEach(arr => arr.forEach(rec => {
    if (!rec.pts.length && !rec.bez) return;   // rider stubs census nothing
    if (rec.bez) audit.bez++;
    if (rec.back) audit.back++;
    if (rec.flow === "same") audit.sameRow++;
    else if (rec.flow) audit[rec.flow]++;
  }));
  // [#10] turn census over stroked ink (post-merge geometry): the numbers
  // the staircase-class pins read.
  wires.forEach(w => {
    if (!w.pts || w.pts.length < 2) return;
    const t = turnsOf(w.pts);
    audit.turnsN++; audit.turnsSum += t;
    if (t > audit.maxTurns) audit.maxTurns = t;
    if (t > 20) audit.over20++;
  });
  spines.forEach(sp => {
    if (sp.con || !sp.pts || sp.pts.length < 2) return;
    const t = turnsOf(sp.pts);
    audit.turnsN++; audit.turnsSum += t;
    if (t > audit.maxTurns) audit.maxTurns = t;
    if (t > 20) audit.over20++;
    if (sp.hub === "trunk" && t > audit.maxTrunkTurns) audit.maxTrunkTurns = t;
  });
  mapLayout = {
    key, sig, lit, edges, E, place, geo, rects, wires, spines, underlays,
    chips, rosterRows, expandedSet: expand, worldH, worldW: cwL, capNote,
    trunkGroups, trunkTotal: trunkGroups + hubTrunks, hubBuses, hubDots,
    pairW, chunkY, chunkRowH, audit, buses,
    pairRiders,   // [#77] corridor riders + straight unbundle paths
  };
  window.routeAudit = mapLayout.audit;
  mapConsumeCenterReq();
  mapPaint(ctx, dpr, cwView, chView, capNote);
}
// consume a pending center request: pan the box to pane center when it is
// meaningfully off-center (tol), then pulse it. Runs in BOTH mapRender
// paint paths (cache-hit + rebuild) so the aim survives layout caching.
// Pan only — mapZ untouched (refit owns zoom). Consumed exactly once, so
// it never fights later manual pans; the pulse fires even when the pan is
// skipped (box already centered).
"""
