# vizjs/pins.py — rung 11/17 of the template join (issues
# #86 phase-3 / #299 A): wire/node pins (latch + menu). Moved VERBATIM from viz.py —
# every edit here changes graph.html: regen the bake (the standing
# template law; test_viz refuses a bake older than viz.py/vizjs).
_JS_PINS = r"""// re-create the records; keys re-resolve against the fresh arrays.
let wirePin = null;    // {surface:'map'|'ball', kind:'wire'|'trunk'|'link', id, menu} | null
let pinCover = 0;      // polylines the last paint emphasized (mapInfo probe)
const wireKeyOf = w => "w|" + w.sf + "|" + w.sfn + "|" + w.df + "|" + w.dfn + "|" + w.ty;
function wirePinSet(p) {
  wirePin = p;
  // [#196] both covers re-resolve for the new identity; a same-id
  // re-latch re-applies too (restore nulls the tint key)
  pinCover = 0; pinCoverX = 0;
  if (pinTintKey != null) pinTintRestore();
  drawMapPane();
}
// [issue #85 owner r1] pinned-wire emphasis: the app accent (the same
// teal the search box, focus rows and bus tips use) - white-on-white
// pins were indistinguishable from the ambient wire mass. One constant
// shared by every map pin pass (wire, spine single, trunk corridor).
const PIN_ACCENT = "#1de9b6";
function pinTintRestore() {
  // [issue #85 owner r4] put the corridor instances' colors back. Skips a
  // rebuilt fnBus (indices would mismatch a fresh buffer); the new mesh
  // carries its own colors.
  // [#196] wire-arc tints restore the same way: per-record originals,
  // mesh-identity guarded (a rebuilt mesh carries stock colors).
  for (const r of pinTint2) {
    const g2 = r.mesh && r.mesh.geometry;
    if (!g2 || !g2.attributes.instanceColorStart ||
        !g2.attributes.instanceColorEnd) continue;
    const cs = g2.attributes.instanceColorStart.array;
    const ce = g2.attributes.instanceColorEnd.array;
    for (let q = 0; q < r.segs.length; q++) {
      const ix = r.segs[q], oc = r.orig[q];
      cs[ix*3] = oc[0]; cs[ix*3+1] = oc[1]; cs[ix*3+2] = oc[2];
      ce[ix*3] = oc[3]; ce[ix*3+1] = oc[4]; ce[ix*3+2] = oc[5];
    }
    g2.attributes.instanceColorStart.needsUpdate = true;
    g2.attributes.instanceColorEnd.needsUpdate = true;
  }
  pinTint2 = [];
  if (!pinTinted.length) return;
  if (pinTintBus && fnBus && pinTintBus === fnBus && fnBus.instanceColor) {
    const ca = fnBus.instanceColor.array;
    for (let q = 0; q < pinTinted.length; q++) {
      const ix = pinTinted[q], oc = pinTintOrig[q];
      ca[ix*3] = oc[0]; ca[ix*3+1] = oc[1]; ca[ix*3+2] = oc[2];
    }
    fnBus.instanceColor.needsUpdate = true;
  }
  pinTinted = []; pinTintOrig = []; pinTintBus = null; pinTintKey = null;
}
function wirePinClear() {
  if (!wirePin) return;
  pinTintRestore();
  wirePin = null; pinCover = 0; pinCoverX = 0;
  if (pinPts) pinPts.visible = false;
  // [issue #85 owner r2] the persistent pin tip dies with the pin on
  // every dismissal path (esc, right-click, void, refocus, reap)
  hideWireTip();
  drawMapPane();
}
// ---- 3D wire/bus tooltip (position:fixed, follows cursor over the WebGL
// canvas; describes the picked fn wire or bus trunk)
const wireTipEl = document.createElement("div");
wireTipEl.id = "wireTip";
function hideWireTip() {
  wireTipEl.style.display = "none"; wireTipAnchor = null;
  // [issue #82] the 3D tip is TRANSIENT (auto-hides on any press — orbit
  // drags included), so it is deliberately NOT a dismissal path: a tip-menu
  // pin outlives it, and Esc / right-click are this surface's dismissals
}
document.body.appendChild(wireTipEl);
// 3D vocabulary legend (user r5): collapsed '?' chip bottom-left; one line
// when open. legendOpen is harness-pinned via __dbg.
const lg3dEl = document.getElementById("lg3d"), lg3dxEl = document.getElementById("lg3dx");
if (lg3dEl) lg3dEl.addEventListener("click", () => {
  legendOpen = !legendOpen;
  lg3dxEl.style.display = legendOpen ? "flex" : "none";
});
// strongest named wires for a file-level link — shared by edge hover and
// wire-click descriptions
function strongPair(l) {
  const pair = [];
  mwires.forEach(w => {
    if ((w[1] === l.s && w[3] === l.t) || (w[1] === l.t && w[3] === l.s)) pair.push(w);
  });
  pair.sort((a, b) =>
    (wireDeg.get(b[3] + "::" + b[4]) || 0) - (wireDeg.get(a[3] + "::" + a[4]) || 0) ||
    (wireSrcDeg.get(b[1] + "::" + b[2]) || 0) - (wireSrcDeg.get(a[1] + "::" + a[2]) || 0) ||
    (a[4] < b[4] ? -1 : a[4] > b[4] ? 1 : 0) ||
    (a[2] < b[2] ? -1 : a[2] > b[2] ? 1 : 0) ||
    (a[5] - b[5]));
  return pair;
}
// rider enumeration (tooltip semantics, user sighting on 7e334e3): the
// fn→fn wires a file's bus actually holds. sid < 0 = all stations of fi.
function riderWiresOf(fi, sid) {
  const out = [], seen = new Set();
  const stations = fnStationsArr || [];
  for (const S of stations) {
    if (S.fi !== fi || (sid >= 0 && S.id !== sid)) continue;
    for (const tk of (S.tks || [])) {
      const tm = trunkMetaMap && trunkMetaMap.get(tk);
      if (!tm) continue;
      for (const m of (tm.mates || [])) {
        if (fnMeta[m.a].file !== fi && fnMeta[m.b].file !== fi) continue;
        const kk = m.a + ">" + m.b + "@" + m.ln;
        if (seen.has(kk)) continue;
        seen.add(kk);
        out.push(m);
      }
    }
  }
  return out;
}
function wireDesc(meta) {
  // what the wire contains and where it goes — fn names on both ends
  if (meta.kind === "link") {
    const pair = strongPair(links[meta.li]);
    if (!pair.length) {
      // inst/signal strands carry no named wires — describe the link itself
      const l = links[meta.li];
      return "hub wire\n" + nodes[l.s].path + "  \u2192  " + nodes[l.t].path +
        "  (" + l.ty + ", w=" + l.w + ")";
    }
    return "hub wire\n" + nodes[pair[0][1]].path + " :: " + pair[0][2] +
      "  @L" + pair[0][5] + "\n  ↓ into\n" + nodes[pair[0][3]].path + " :: " + pair[0][4] +
      (pair.length > 1 ? "\n+" + (pair.length - 1) + " more named wires on this pair" : "");
  }
  if (meta.kind === "trunk") {
    const mates = meta.mates || [];
    const head = "🚌 bus " + nodes[meta.sf].path + "  →  " +
      nodes[meta.tf].path + "  (" + mates.length + " wires)";
    const body = mates.map(m =>
      "  · " + fnMeta[m.a].name + "() → " + fnMeta[m.b].name + "()"
      + (m.ln >= 0 ? "  @L" + m.ln : "")).join("\n");
    return head + (body ? "\n" + body : "");
  }
  if (meta.kind === "jleg" || meta.kind === "sub" || meta.kind === "jof") {
    const pp = meta.k ? String(meta.k).split("|") : null;
    const sid = pp ? +pp[2] : -1;
    const mates = riderWiresOf(meta.fi, sid);
    const srcs = [...new Set(mates.map(m =>
      fnMeta[fnMeta[m.a].file === meta.fi ? m.a : m.b].name))];
    const dests = [...new Set(mates.map(m =>
      fnMeta[fnMeta[m.a].file === meta.fi ? m.b : m.a].file))];
    const head = (meta.kind === "jleg" ? "🚌 junction leg · " : "🚌 bus fan · ") +
      nodes[meta.fi].path + "\n" +
      srcs.slice(0, 4).join(", ") + (srcs.length > 4 ? " +" + (srcs.length - 4) : "") +
      "  →  bus to " + dests.slice(0, 3).map(fi2 => nodes[fi2].label).join(", ") +
      (dests.length > 3 ? " +" + (dests.length - 3) : "") +
      "  · " + mates.length + " wire" + (mates.length !== 1 ? "s" : "");
    const body = mates.slice(0, 8).map(m =>
      "  · " + fnMeta[m.a].name + "() → " + fnMeta[m.b].name + "()"
      + (m.ln >= 0 ? "  @L" + m.ln : "")).join("\n");
    return head + (body ? "\n" + body : "") +
      (mates.length > 8 ? "\n+" + (mates.length - 8) + " more" : "");
  }
  if (meta.kind === "station") {
    const mates = riderWiresOf(meta.fi, -1);
    const dests = [...new Set(mates.map(m =>
      fnMeta[fnMeta[m.a].file === meta.fi ? m.b : m.a].file))];
    const head = "🚌 bus · " + nodes[meta.fi].path + "  (" + mates.length + " wires)";
    const body = mates.slice(0, 8).map(m =>
      "  · " + fnMeta[m.a].name + "() → " + fnMeta[m.b].name + "()"
      + (m.ln >= 0 ? "  @L" + m.ln : "")).join("\n");
    return head + (body ? "\n" + body : "") +
      (mates.length > 8 ? "\n+" + (mates.length - 8) + " more" : "") +
      "\n→ " + dests.length + " corridor" + (dests.length !== 1 ? "s" : "") +
      ": " + dests.slice(0, 4).map(fi2 => nodes[fi2].label).join(", ") +
      (dests.length > 4 ? " +" + (dests.length - 4) : "");
  }
  const a = fnMeta[meta.a], b = fnMeta[meta.b];
  const src = nodes[a.file], dst = nodes[b.file];
  const ty = "call";
  return ty + " wire\n" + src.path + " :: " + a.name + "()" +
    (meta.ln >= 0 ? "  @L" + meta.ln : "") +
    "\n  ↓ into\n" + dst.path + " :: " + b.name + "()";
}
// [issue #85 owner r2] from -> to for a LATCHED pin: while the pin
// lives the tip surface carries this instead of fading after the
// click (the transient tip is what the owner never saw). Trunk/
// conduit pins show the bus-card pair summary; wire pins name both
// fns with their file context; link pins reuse the link description.
function pinDesc(pin) {
  // [skeptic #17] a pin can outlive the roster it indexed: a refocus
  // rebuild swaps fnMeta/links wholesale and the stale indices threw
  // TypeError out of the per-frame tick - which also stalled the #16
  // reap (the rAF chain dies with the throw). Guard every roster read;
  // a pin with no live description just gets no tip.
  if (pin.kind === "trunk") {
    const tm = trunkMetaMap && trunkMetaMap.get(pin.k);
    return tm ? wireDesc(tm) : null;
  }
  if (pin.kind === "link") {
    if (!links || pin.li < 0 || pin.li >= links.length) return null;
    return wireDesc({ kind: "link", li: pin.li });
  }
  // wire pins carry the same {a, b, ln} the line metas do - wireDesc
  // already resolves both ends' paths + fn names for that shape (a
  // hand-rolled fnMeta probe went stale against rebuilt rosters)
  if (pin.kind === "wire") {
    if (!fnMeta || pin.a < 0 || pin.a >= fnMeta.length ||
        pin.b < 0 || pin.b >= fnMeta.length) return null;
    return wireDesc({ kind: "wire", a: pin.a, b: pin.b, ln: pin.ln });
  }
  return null;
}
function showWireTip(meta, cx, cy) {
  wireTipEl.textContent = wireDesc(meta);
  wireTipEl.style.display = "block";
  wireTipAnchor = meta || null;   // sighting #11: lifetime-tracked anchor
  // [issue #82] the pin latch moved to the capture-click call site: it
  // needs the event to separate a deliberate canvas wire click from a DOM
  // .click() (legend chips, checkboxes, search rows dispatch clientX/Y 0,0,
  // which projects onto whatever wire sits at that corner — a transient tip
  // there is harmless, a sticky pin is not).

  const pad = 14;
  let x = cx + pad, y = cy + pad;
  const r = wireTipEl.getBoundingClientRect();
  if (x + r.width > innerWidth - 8) x = cx - r.width - pad;
  if (y + r.height > innerHeight - 8) y = cy - r.height - pad;
  wireTipEl.style.left = x + "px";
  wireTipEl.style.top = y + "px";
}
// ESC priority (section 8): picker (fn picker first, then map picker),
// then pinned list, then the L3 freeze
function mapOvCloseOne() {
  let closed = false;
  if (fnPickEl.style.display === "block") { fnClosePick(); closed = true; }
  if (mapPickEl.style.display === "block") { mapClosePick(); closed = true; }
  if (mapListEl.style.display === "block") { mapListEl.style.display = "none";
    mapListChip = -1; closed = true; }
  if (mapFrozenIx >= 0) { mapFrozenIx = -1; closed = true; drawMapPane(); }
  // [issue #82] closing the bundle list unpins the wire its row selected
  // (menu-close dismissal; the list was that pin's menu)
  if (wirePin && wirePin.menu === "list" && mapListEl.style.display === "none") wirePinClear();
  return closed;
}
// [issues #112/#113] focus teardown converges HERE for the 2D surface: every
// piece of map pick state a focus scope owned dies with that scope — the L3
// freeze (a stale mapFrozenIx dims the NEXT focus's whole diagram via
// dim()), the wire hover (dim()'s hov branch dims the same diagram while a
// stale mapHover keeps pointing at the torn-down focus's wires), the vars
// chip rect (a stale rect is an invisible toggle zone under the "focus a
// node" hint) and the chip hover. clearFocus, resetAll and the no-focus
// paint paths all route through this one function.
function mapTeardown() {
  mapFrozenIx = -1;
  mapHover = -1;
  mapVarsChipRect = null;
  mapHoverChip = -1;
}
// L2: click a named wire -> fn panel. showFnInfo reads fnMeta[k] only, so a
// miss pushes a temporary entry around the synchronous call (removed after).
function mapShowFn(fi, name) {
  let k = -1;
  for (let j = 0; j < fnMeta.length; j++)
    if (fnMeta[j].file === fi && fnMeta[j].name === name) { k = j; break; }
  const temp = k < 0;
  if (temp) { fnMeta.push({ file: fi, name, p: [0, 0, 0] }); k = fnMeta.length - 1; }
  showFnInfo(k);
  if (temp) fnMeta.splice(k, 1);
}
// 6px SCREEN-space wire hit test (section 8): world tolerance = 6 / mapZ
function mapWireAt(wx, wy) {
  // [issue #84] named wires are 1.5px strokes in dense walls - 6px screen
  // tolerance missed real aims; 10px is the pick band now. The ink gate
  // stays: named-wire ink paints ink-gated, so picks are gated identically
  // (parity; trunk/tap spines paint un-gated and their hit-test is too).
  if (!mapLayout || !mapInkOn) return -1;   // ink tier off: no invisible-wire hits
  const tol = 10 / mapZ;
  let best = -1, bd = tol;
  mapLayout.wires.forEach((w, ix) => {
    if (w.bez) {   // S-curve: sample the cubic pin-to-pin
      const x0 = w.pts[0][0], y0 = w.pts[0][1],
            x1 = w.pts[1][0], y1 = w.pts[1][1];
      let qx = x0, qy = y0;
      for (let k = 1; k <= 24; k++) {
        const t = k / 24, mt = 1 - t;
        const bx = mt * mt * mt * x0 + 3 * mt * mt * t * w.c1[0] +
                   3 * mt * t * t * w.c2[0] + t * t * t * x1;
        const by = mt * mt * mt * y0 + 3 * mt * mt * t * w.c1[1] +
                   3 * mt * t * t * w.c2[1] + t * t * t * y1;
        const d = segDist(wx, wy, qx, qy, bx, by);
        if (d < bd) { bd = d; best = ix; }
        qx = bx; qy = by;
      }
      return;
    }
    for (let s = 0; s < w.pts.length - 1; s++) {
      const d = segDist(wx, wy, w.pts[s][0], w.pts[s][1],
                           w.pts[s + 1][0], w.pts[s + 1][1]);
      if (d < bd) { bd = d; best = ix; }
    }
  });
  mapWireLastD = best >= 0 ? bd : Infinity;   // [issue #84] caller-side carve-outs
  return best;
}
let mapWireLastD = Infinity;
function mapChipAt(wx, wy) {
  // [issue #113] pick-vs-paint parity: hits test the SCREEN-space rect the
  // paint actually drew (ch.hit — collision-ladder displacement, LOD, zoom
  // fade and out-of-view all baked in), never the raw layout anchor. A
  // hidden chip has hit === null and is unclickable; a displaced chip
  // responds exactly where it is visible.
  if (!mapLayout) return -1;
  const sx = (wx - mapPX) * mapZ, sy = (wy - mapPY) * mapZ;
  for (let c = 0; c < mapLayout.chips.length; c++) {
    const h = mapLayout.chips[c].hit;
    if (h && sx >= h.x && sx <= h.x + h.w && sy >= h.y && sy <= h.y + h.h) return c;
  }
  return -1;
}
// L1 tooltip (section 8): call/var = A::sfn() -> B::dfn() + line + fio
// signature/writes/mutates; signal = scene > sig > B::handler [F5]
function mapTipText(w) {
  const A = nodes[w.sf], B = nodes[w.df];
  if (w.stub) {   // bus delivery: destination + member roster
    let t = "\uD83D\uDE8C bus \u2192 " + B.label + "::" + w.dfn +
      " (" + w.busN + " wires)";
    (w.mates || []).forEach(mn => { t += "\n  \u00b7 " + mn; });
    return t;
  }
  if (w.ty === "signal")
    return A.label + " > " + w.sfn + " > " + B.label + "::" + w.dfn;
  let t = A.label + "::" + w.sfn + "() \u2192 " + B.label + "::" + w.dfn + "()";
  if (w.line) t += "\nline " + w.line;
  const io = (DATA.fio || {})[fnKey(w.df, w.dfn)];
  if (io) {
    if (io.sig) t += "\n" + io.sig + (io.ret ? " -> " + io.ret : "");
    if (io.w.length) t += "\n\u270e " + io.w.join(", ");
    if (io.mp.length) t += "\n\u21c4 " + io.mp.join(", ");
  }
  return t;
}
// bundle list (section 7): every wire on the corridor, enumerated + scrollable
function mapOpenList(ci) {
  const ch = mapLayout.chips[ci];
  mapListChip = ci;   // [issue #113] the list belongs to this chip (re-anchor)
  mapListEl.innerHTML = "";
  const h = document.createElement("h3");
  h.textContent = ch.origin
    ? nodes[ch.s].label + " trunk  (\u00d7" + ch.n + " wires on the pipe)"
    : ch.peel
    ? nodes[ch.s].label + " \u2192 row " + ch.row + "  (\u00d7" + ch.n + " wires)"
    : nodes[ch.s].label + " \u2192 " + nodes[ch.t].label +
      "  (" + ch.ty + " \u00d7" + ch.n + ")";
  ch.wires.forEach(wr => {
    const row = document.createElement("div");
    row.className = "row";
    row.textContent = nodes[wr.sf].label + "::" + wr.sfn + " \u2192 " +
      nodes[wr.df].label + "::" + wr.dfn + " :" + wr.line;
    row.onclick = () => {
      if (wr.ty !== "var") {
        mapShowFn(wr.df, wr.dfn);
        // [issue #82] enumerated chip-list rows are wires: clicking one
        // pins THAT wire (menu = the open bundle list)
        wirePinSet({ surface: "map", kind: "wire", id: wireKeyOf(wr), menu: "list" });
      }
    };
    mapListEl.appendChild(row);
  });
  const a = mapListAnchor(ch);
  mapListEl.style.left = a[0] + "px";
  mapListEl.style.top = a[1] + "px";
  mapListEl.style.display = "block";
}
// [issue #113] the bundle list anchors to the chip's PAINTED rect when it
// has one (pick-vs-paint parity), else to its raw anchor — and the paint
// re-anchors an open list every frame, so wheel/pan/drag/resize can never
// leave live UI floating over unrelated geometry.
function mapListAnchor(ch) {
  const b = mapPane.getBoundingClientRect();
  const sx = Math.max(4, Math.min((ch.hit ? ch.hit.x + 16
    : (ch.x - mapPX) * mapZ + 16), mapPane.clientWidth - 262));
  const sy = Math.max(4, Math.min((ch.hit ? ch.hit.y + 10
    : (ch.y - mapPY) * mapZ + 10), (b.height || innerHeight) - 170));
  return [sx, sy];
}
// "+N more" picker (section 4 [F11]): edge-anchored, searchable, closes on
// canvas input + ESC (wire both below)
const mapPickFill = q => {
  if (!mapPickRc || !mapLayout) return;
  const rost = mapLayout.geo.get(mapPickRc.i).roster;
  mapPickRows.innerHTML = "";
  const ql = q.toLowerCase();
  rost.more.filter(r => !ql || r[0].toLowerCase().includes(ql)).slice(0, 80).forEach(r => {
    const row = document.createElement("div");
    row.className = "row";
    row.textContent = r[0] + "  :" + r[1];
    row.onclick = () => { mapShowFn(mapPickRc.i, r[0]); mapClosePick(); };
    mapPickRows.appendChild(row);
  });
};
function mapOpenPicker(rc) {
  mapPickRc = rc;
  mapPickFill("");
  const p = mapLayout.place.get(rc.i);
    mapPickEl.style.left = Math.max(4, Math.min((p.x + p.w - mapPX) * mapZ, mapPane.clientWidth - 258)) + "px";
  mapPickEl.style.top = Math.max(4, (rc.more.y0 - mapPY) * mapZ) + "px";
  mapPickEl.style.display = "block";
  mapPickIn.value = "";
  setTimeout(() => mapPickIn.focus(), 0);
}
mapPickIn.addEventListener("input", () => mapPickFill(mapPickIn.value));
mapPickIn.addEventListener("keydown", e => {
  if (e.key === "Escape") { e.stopPropagation(); mapClosePick(); }
});
// fn picker over the 3D view: clicking a per-file aggregate fn box ('n×')
// must not call showFnInfo with an empty fn name — it opens this picker
// listing the file's full roster (DATA.fns), ranked by incident-wire count
// then name; a row click = the same fn panel as clicking that fn box
// (mapShowFn covers roster fns that have no fnMeta box). Styling is
// #mapPick's via the shared selectors above.
const fnPickEl = document.createElement("div");
fnPickEl.id = "fnPick";
fnPickEl.innerHTML = '<input placeholder="filter fns..."><div class="rows"></div>';
document.body.appendChild(fnPickEl);
const fnPickIn = fnPickEl.querySelector("input");
const fnPickRows = fnPickEl.querySelector(".rows");
let fnPickFile = -1;
function fnClosePick() { fnPickEl.style.display = "none"; fnPickFile = -1; }
function fnPickFill(q) {
  if (fnPickFile < 0) return;
  const fi = fnPickFile;
  const inc = new Map();   // fn -> incident wire count (calls in + out)
  mwires.forEach(w => {
    if (w[1] === fi) inc.set(w[2], (inc.get(w[2]) || 0) + 1);
    if (w[3] === fi) inc.set(w[4], (inc.get(w[4]) || 0) + 1);
  });
  fnPickRows.innerHTML = "";
  const ql = q.toLowerCase();
  (mfns[nodes[fi].path] || [])
    .filter(r => !ql || r[0].toLowerCase().includes(ql))
    .map(r => [r[0], r[1], inc.get(r[0]) || 0])
    .sort((a, b) => (b[2] - a[2]) ||
      (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
    .slice(0, 80)
    .forEach(r => {
      const row = document.createElement("div");
      row.className = "row";
      row.textContent = r[0] + "  :" + r[1];
      row.onclick = () => { fnClosePick(); mapShowFn(fi, r[0]); };
      fnPickRows.appendChild(row);
    });
}
function openFnPicker(fi, x, y) {
  fnPickFile = fi;
  fnPickFill("");
  fnPickEl.style.left = Math.max(4, Math.min(x, innerWidth - 270)) + "px";
  fnPickEl.style.top = Math.max(4, Math.min(y, innerHeight - 40)) + "px";
  fnPickEl.style.display = "block";
  fnPickIn.value = "";
  setTimeout(() => fnPickIn.focus(), 0);
}
fnPickIn.addEventListener("input", () => fnPickFill(fnPickIn.value));
fnPickIn.addEventListener("keydown", e => {
  if (e.key === "Escape") { e.stopPropagation(); fnClosePick(); }
});
"""
