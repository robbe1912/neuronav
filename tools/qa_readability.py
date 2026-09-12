# Objective readability gate for the neuronav visualizer.
# Serves the state dir of the active config, loads graph.html in headless Chrome, drives the SAME
# focus state as tests/test_viz.py (highest-degree node stem), and measures
# ink-clutter metrics in both layers straight from window.__dbg.
# Run: .venv/Scripts/python.exe -X utf8 tools/qa_readability.py   (exit 0 = ok)
# Outputs: .tmp/qa/readability_base.json + .tmp/qa/gate_base_3d.png + .tmp/qa/gate_base_map.png
#
# Declutter battery (team/declutter round):
#   python -X utf8 tools/qa_readability.py --declutter
#     Capture the multi-angle, multi-hub baseline (5 angles x [global + top-8
#     hubs]) into .tmp/qa/declutter_base.json (+ sha-suffixed snapshot copy) and
#     .tmp/qa/declut_base_<subject>_<angle>.png screenshots.
#   python -X utf8 tools/qa_readability.py --after [--base .tmp/qa/declutter_base.json]
#     Same battery against the current build; prints a per-metric delta table
#     vs the baseline and exits 1 on any clutter regression.
#   (both need a python with playwright; PIL NOT required — PNG ink analysis
#   decodes the CDP quarter-scale capture in pure stdlib)
import argparse
import base64
import http.server
import json
import re
import socketserver
import sys
import threading
from functools import partial
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import nav  # noqa: E402  (the bake lives in the active config's state dir)

STATE = nav.STATE_DIR
QA = ROOT / ".tmp" / "qa"

# --- focus token: highest-degree node's path stem (tests/test_viz.py:140-150)
TOK_JS = """() => { const d = window.__dbg;
     let best = 0;
     for (let i = 1; i < d.nodes.length; i++)
       if ((d.adj[i]||[]).length > (d.adj[best]||[]).length) best = i;
     return d.nodes[best].path.split('/').pop().replace(/\\.[^.]+$/, '').toLowerCase(); }"""

# --- 3D fn-layer clutter metrics (all world coords via __dbg) ---------------
JS_3D = r"""() => {
  const d = window.__dbg;
  if (!d.fnMesh || !d.fnMeta || !d.fnMeta.length) return { fail: 'no fn layer' };
  const R2 = v => Math.round(v * 100) / 100;

  // junctions: decode fnJDot InstancedMesh instanceMatrix translation column
  const J = [];
  if (d.fnJDot && d.fnJDot.instanceMatrix) {
    const a = d.fnJDot.instanceMatrix.array;
    for (let i = 0; i < d.fnJDot.count; i++) {
      const o = i * 16;
      J.push([a[o + 12], a[o + 13], a[o + 14]]);
    }
  }
  let minJJ = Infinity, jjPair = null;
  for (let i = 0; i < J.length; i++)
    for (let k = i + 1; k < J.length; k++) {
      const dx = J[i][0]-J[k][0], dy = J[i][1]-J[k][1], dz = J[i][2]-J[k][2];
      const dd = Math.sqrt(dx*dx + dy*dy + dz*dz);
      if (dd < minJJ) { minJJ = dd; jjPair = [i, k]; }
    }

  // fn wire segments: LineSegments2 instanceStart/instanceEnd (world coords)
  const grab = mesh => {
    const out = [];
    if (!mesh) return out;
    const g = mesh.geometry.attributes;
    const s = g.instanceStart.array, e = g.instanceEnd.array;
    for (let i = 0; i < g.instanceStart.count; i++)
      out.push([[s[i*3], s[i*3+1], s[i*3+2]], [e[i*3], e[i*3+1], e[i*3+2]]]);
    return out;
  };
  const wseg = grab(d.fnLines), qseg = grab(d.fnQuiet);
  const ptSeg = (p, a, b) => {
    const abx=b[0]-a[0], aby=b[1]-a[1], abz=b[2]-a[2];
    const apx=p[0]-a[0], apy=p[1]-a[1], apz=p[2]-a[2];
    const dd = abx*abx + aby*aby + abz*abz;
    let t = dd > 0 ? (apx*abx + apy*aby + apz*abz) / dd : 0;
    t = t < 0 ? 0 : (t > 1 ? 1 : t);
    const dx = apx - t*abx, dy = apy - t*aby, dz = apz - t*abz;
    return Math.sqrt(dx*dx + dy*dy + dz*dz);
  };
  // per-junction count of wire segments within R = 8/12/16
  const RS = [8, 12, 16];
  const perJ = J.map(p => {
    const rec = { n8: 0, n12: 0, n16: 0, q12: 0 };
    for (const sg of wseg) {
      const dd = ptSeg(p, sg[0], sg[1]);
      if (dd <= 8) rec.n8++;
      if (dd <= 12) rec.n12++;
      if (dd <= 16) rec.n16++;
    }
    for (const sg of qseg) if (ptSeg(p, sg[0], sg[1]) <= 12) rec.q12++;
    return rec;
  });
  const agg = key => {
    if (!perJ.length) return { max: 0, mean: 0, hit: 0 };
    let mx = 0, s = 0, hit = 0;
    perJ.forEach(r => { if (r[key] > mx) mx = r[key]; s += r[key]; if (r[key] > 0) hit++; });
    return { max: mx, mean: R2(s / perJ.length), hit };
  };
  let worst = null;
  perJ.forEach((r, i) => {
    if (!worst || r.n12 > worst.n12) worst = { i, n8: r.n8, n12: r.n12, n16: r.n16, q12: r.q12 };
  });

  // fn boxes: instanceMatrix translation + scale (unit BoxGeometry)
  const boxes = [];
  {
    const a = d.fnMesh.instanceMatrix.array;
    for (let i = 0; i < d.fnMesh.count; i++) {
      const o = i * 16;
      const sx = Math.hypot(a[o], a[o+1], a[o+2]);
      const sy = Math.hypot(a[o+4], a[o+5], a[o+6]);
      const sz = Math.hypot(a[o+8], a[o+9], a[o+10]);
      boxes.push({ c: [a[o+12], a[o+13], a[o+14]], r: Math.hypot(sx, sy, sz) / 2 });
    }
  }
  let boxNear = 0, minBJ = Infinity;
  for (const b of boxes) {
    let bm = Infinity;
    for (const p of J) {
      const dd = Math.hypot(b.c[0]-p[0], b.c[1]-p[1], b.c[2]-p[2]) - b.r;
      if (dd < bm) bm = dd;
    }
    if (bm < minBJ) minBJ = bm;
    if (bm <= 12) boxNear++;
  }

  // conduit self-overlap: busPts segment pairs (different trunk keys, so a
  // conduit's own smooth chain never counts against itself) closer than 6
  const bs = d.busPts || [];
  const segSeg = (p1, q1, p2, q2) => {
    const d1 = [q1[0]-p1[0], q1[1]-p1[1], q1[2]-p1[2]];
    const d2 = [q2[0]-p2[0], q2[1]-p2[1], q2[2]-p2[2]];
    const r = [p1[0]-p2[0], p1[1]-p2[1], p1[2]-p2[2]];
    const a = d1.reduce((s, v, i) => s + v*d1[i], 0);
    const e = d2.reduce((s, v, i) => s + v*d2[i], 0);
    const f = d1.reduce((s, v, i) => s + v*d2[i], 0);

    const c = d1.reduce((s, v, i) => s + v*r[i], 0);
    const b = d2.reduce((s, v, i) => s + v*r[i], 0);
    const den = a*e - f*f;
    let s, t;
    if (den > 1e-9) { s = Math.max(0, Math.min(1, (b*f - c*e) / den)); }
    else s = 0;
    t = (s*f + b) / (e || 1e-9);
    if (t < 0) { t = 0; s = Math.max(0, Math.min(1, -c / (a || 1e-9))); }
    else if (t > 1) { t = 1; s = Math.max(0, Math.min(1, (f - c) / (a || 1e-9))); }
    const cx = d1[0]*s - d2[0]*t + r[0];
    const cy = d1[1]*s - d2[1]*t + r[1];
    const cz = d1[2]*s - d2[2]*t + r[2];
    return Math.sqrt(cx*cx + cy*cy + cz*cz);
  };
  let ov6 = 0, minCD = Infinity, conduitKeys = new Set();
  for (const s of bs) conduitKeys.add(s.k);
  for (let i = 0; i < bs.length; i++)
    for (let k = i + 1; k < bs.length; k++) {
      if (bs[i].k === bs[k].k) continue;
      const dd = segSeg(bs[i].a, bs[i].b, bs[k].a, bs[k].b);
      if (dd < minCD) minCD = dd;
      if (dd < 6) ov6++;
    }


  return {
    junctions: J.length,
    minJunctionGap: isFinite(minJJ) ? R2(minJJ) : null,
    wireSegments: wseg.length,
    quietSegments: qseg.length,
    junctionWireProximity: {
      r8: agg('n8'), r12: agg('n12'), r16: agg('n16'),
      quietR12: agg('q12'), worstJunction: worst,
    },
    perJunction: perJ,
    fnBoxes: boxes.length,
    boxesWithin12OfJunction: boxNear,
    minBoxJunctionSurface: isFinite(minBJ) ? R2(minBJ) : null,
    conduitSegments: bs.length,
    conduitTrunks: conduitKeys.size,
    conduitPairsUnder6: ov6,
    minConduitGap: isFinite(minCD) ? R2(minCD) : null,
    trunks: d.fnTrunkN, trunkRiders: d.fnTrunkW, junctionStubs: d.fnJstubN,
    quietTrunks: d.fnQuietTrunkN, quietTrunkRiders: d.fnQuietTrunkW,
  };
}"""

# --- 2D map metrics (mapLayout polylines, world coords) ---------------------
JS_2D = r"""() => {
  const d = window.__dbg;
  const L = d.mapLayout;
  if (!L) return { fail: 'no mapLayout' };
  const info = d.mapInfo();
  // stroked ink only: all named wires + spines that are NOT consolidated
  // twins (sp.con true rides the shared trunk and paints no polyline)
  const polys = [];
  L.wires.forEach((w, i) => polys.push({ pts: w.pts, kind: 'w', id: 'w' + i }));
  L.spines.forEach((sp, i) => { if (!sp.con) polys.push({ pts: sp.pts, kind: 's', id: 's' + i }); });
  const segs = [];
  polys.forEach(p => {
    for (let s = 0; s + 1 < p.pts.length; s++) {
      const a = p.pts[s], b = p.pts[s + 1];
      const dx = b[0] - a[0], dy = b[1] - a[1];
      const len = Math.hypot(dx, dy);
      if (len < 1e-6) continue;
      segs.push({ x1: a[0], y1: a[1], dx, dy, len, pid: p.id, kind: p.kind });
    }
  });
  // near-parallel overlap pair: angle < 5 deg (normalized cross < sin5),
  // perpendicular gap <= 4px at both ends of the OVERLAPPING stretch,
  // colinear overlap > 10px. Different polylines only.
  const SIN5 = 0.0872;
  let pairs = 0, ww = 0, ss = 0, sw = 0;
  const perPoly = {};
  for (let i = 0; i < segs.length; i++) {
    const A = segs[i];
    const ux = A.dx / A.len, uy = A.dy / A.len;
    const nx = -uy, ny = ux;
    for (let j = i + 1; j < segs.length; j++) {
      const B = segs[j];
      if (A.pid === B.pid) continue;
      const cross = Math.abs(A.dx * B.dy - A.dy * B.dx) / (A.len * B.len);
      if (cross > SIN5) continue;
      const t1 = (B.x1 - A.x1) * ux + (B.y1 - A.y1) * uy;
      const t2 = (B.x1 + B.dx - A.x1) * ux + (B.y1 + B.dy - A.y1) * uy;
      const c1 = Math.max(0, Math.min(A.len, t1));
      const c2 = Math.max(0, Math.min(A.len, t2));
      if (Math.abs(c2 - c1) <= 10) continue;
      const o1 = (B.x1 - A.x1) * nx + (B.y1 - A.y1) * ny;
      const o2 = o1 + B.dx * nx + B.dy * ny;
      const dt = t2 - t1;
      const oo1 = dt ? o1 + (o2 - o1) * ((c1 - t1) / dt) : o1;
      const oo2 = dt ? o1 + (o2 - o1) * ((c2 - t1) / dt) : o1;
      if (Math.abs(oo1) > 4 || Math.abs(oo2) > 4) continue;
      pairs++;
      if (A.kind === 'w' && B.kind === 'w') ww++;
      else if (A.kind === 's' && B.kind === 's') ss++;
      else sw++;
      perPoly[A.pid] = (perPoly[A.pid] || 0) + 1;
      perPoly[B.pid] = (perPoly[B.pid] || 0) + 1;
    }
  }
  let worstPoly = null;
  for (const k in perPoly) if (!worstPoly || perPoly[k] > worstPoly.n) worstPoly = { id: k, n: perPoly[k] };
  return {
    mapInfo: info,
    routeAudit: window.routeAudit || null,
    buses: L.buses.length,
    chips: L.chips.length,
    spinesTotal: L.spines.length,
    spinesDrawn: L.spines.filter(sp => !sp.con).length,
    wires: L.wires.length,
    nearParallel: {
      pairs, wireWire: ww, spineSpine: ss, spineWire: sw,
      segments: segs.length, polylines: polys.length,
      worstPolyline: worstPoly,
    },
  };
}"""



# =============================================================================
# Declutter battery: multi-angle x multi-hub screen-space clutter metrics.
# Every number is read from the live page (window.__dbg + DOM label rects) at a
# SETTLED camera (tween fully stopped, double-read identical), so baseline and
# --after runs are comparable byte-for-byte on the same graph.html bake.
# =============================================================================

# top-N hubs by baked connection count (undirected adj degree), deterministic
# tiebreak on path
HUB_TOPN = 8
HUBS_JS = r"""() => { const d = window.__dbg;
  const arr = [];
  for (let i = 0; i < d.nodes.length; i++)
    arr.push({ i, p: d.nodes[i].path, deg: (d.adj[i]||[]).length });
  arr.sort((a, b) => b.deg - a.deg || (a.p < b.p ? -1 : a.p > b.p ? 1 : 0));
  return arr.slice(0, """ + str(HUB_TOPN) + r"""); }"""

# camera moves are explicit and reproducible: each angle is applied to the
# SAVED subject base pose (post focus fly-in), never chained onto the previous
# angle, so angle k is independent of angle k-1
CAM_SAVE = r"""() => { const d = window.__dbg, c = d.camera, t = d.controls.target;
  window.__gateCam = { p: [c.position.x, c.position.y, c.position.z],
                       t: [t.x, t.y, t.z] }; return true; }"""
CAM_RESTORE = r"""() => { const d = window.__dbg, s = window.__gateCam;
  d.camera.position.set(s.p[0], s.p[1], s.p[2]);
  d.controls.target.set(s.t[0], s.t[1], s.t[2]);
  d.controls.update(); return true; }"""
CAM_ORBIT = r"""(deg) => { const d = window.__dbg, t = d.controls.target;
  const off = d.camera.position.clone().sub(t);
  const a = deg * Math.PI / 180;
  const x = off.x * Math.cos(a) - off.z * Math.sin(a);
  const z = off.x * Math.sin(a) + off.z * Math.cos(a);
  d.camera.position.set(t.x + x, t.y + off.y, t.z + z);
  d.controls.update(); return true; }"""
CAM_TOP = r"""() => { const d = window.__dbg, t = d.controls.target;
  const dist = d.camera.position.distanceTo(t);
  d.camera.position.set(t.x, t.y + dist, t.z + dist * 0.02);
  d.controls.update(); return true; }"""
CAM_ZOOM = r"""() => { const d = window.__dbg, t = d.controls.target;
  d.camera.position.lerp(t, 0.5);
  d.controls.update(); return true; }"""

ANGLES = [("overview", None), ("orbit45", ("orbit", 45)),
          ("orbitm45", ("orbit", -45)), ("topdown", "top"), ("zoomin", "zoom")]

# one settled read: crossings, label overlaps, label-wire overlaps, chevron
# crowding, node occlusion — all screen-space, all from __dbg / DOM rects
JS_DECLUT = r"""() => { const d = window.__dbg;
  if (!d || !d.projectPoint) return { BROKEN: true };
  const R1 = v => Math.round(v * 10) / 10;
  const cw = d.renderer.domElement.clientWidth,
        ch = d.renderer.domElement.clientHeight;
  const fovY = d.camera.fov * Math.PI / 180;
  const V3 = (x, y, z) => new d.THREE.Vector3(x, y, z);
  const camD = p => d.camera.position.distanceTo(V3(p[0], p[1], p[2]));
  const pxwu = p => (ch / 2) / (Math.tan(fovY / 2) * camD(p));
  const P = p => d.projectPoint(p[0], p[1], p[2]);
  const on = q => q.x >= -40 && q.x <= cw + 40 && q.y >= -40 && q.y <= ch + 40;
  const out = { camDist: R1(d.camera.position.distanceTo(d.controls.target)) };

  // ---- wire inventory: projected segments tagged wire/quiet/trunk/leg/link.
  // LineSegments2 geometries keep start+end in ONE interleaved buffer
  // (6 floats/seg); instanceColorStart exists where setColors was used and
  // black (sum < 0.02) marks filtered/ghost segments to skip ----
  const segs = [];
  const grab = (mesh, kind) => { if (!mesh) return;
    const g = mesh.geometry.attributes;
    const buf = g.instanceStart.array;
    const col = g.instanceColorStart ? g.instanceColorStart.array : null;
    for (let i = 0; i < g.instanceStart.count; i++) {
      if (col && col[i*6] + col[i*6+1] + col[i*6+2] < 0.02) continue;
      const a = P([buf[i*6], buf[i*6+1], buf[i*6+2]]),
            b = P([buf[i*6+3], buf[i*6+4], buf[i*6+5]]);
      if (on(a) || on(b)) segs.push({ a: [a.x, a.y], b: [b.x, b.y], k: kind });
    } };
  grab(d.fnLines, 'wire'); grab(d.fnQuiet, 'quiet');
  grab(d.focusArcRef && d.focusArcRef.lines, 'arc');
  (d.bucketMesh || []).forEach(ms => grab(ms, 'link'));
  if (d.busPts) for (const s of d.busPts) {
    const a = P(s.a), b = P(s.b);
    if (on(a) || on(b)) segs.push({ a: [a.x, a.y], b: [b.x, b.y],
      k: String(s.k).startsWith('L|') ? 'leg' : 'trunk' });
  }
  out.wireSegs = { wire: segs.filter(s => s.k === 'wire').length,
                   quiet: segs.filter(s => s.k === 'quiet').length,
                   trunk: segs.filter(s => s.k === 'trunk').length,
                   leg: segs.filter(s => s.k === 'leg').length,
                   arc: segs.filter(s => s.k === 'arc').length,
                   link: segs.filter(s => s.k === 'link').length };

  // ---- junction bollards + delivery chevrons as screen discs ----
  const J = [], A = [];
  if (d.fnJDot && d.fnJDotR) { const m = d.fnJDot.instanceMatrix.array;
    for (let i = 0; i < d.fnJDotR.length; i++) {
      const p = [m[i*16+12], m[i*16+13], m[i*16+14]], q = P(p);
      if (on(q)) J.push({ x: q.x, y: q.y, r: (d.fnJDotR[i] || 0) * pxwu(p) });
    } }
  if (d.fnArrows && d.fnArrowR) { const m = d.fnArrows.instanceMatrix.array;
    for (let i = 0; i < d.fnArrowR.length; i++) {
      const p = [m[i*16+12], m[i*16+13], m[i*16+14]], q = P(p);
      if (on(q)) A.push({ x: q.x, y: q.y, r: (d.fnArrowR[i] || 0) * pxwu(p) });
    } }
  out.junctions = J.length; out.chevrons = A.length;

  // ---- trunk-trunk screen crossings (per trunk group; legs counted as TL) --
  let xTT = 0, xTL = 0; const xSites = [];
  if (d.busPts) {
    const groups = {};
    for (const s of d.busPts) (groups[s.k] = groups[s.k] || []).push(s);
    const keys = Object.keys(groups);              // insertion order: busPts order
    const segInt = (p1, p2, p3, p4) => {
      const d1 = (p4[0]-p3[0])*(p1[1]-p3[1])-(p4[1]-p3[1])*(p1[0]-p3[0]);
      const d2 = (p4[0]-p3[0])*(p2[1]-p3[1])-(p4[1]-p3[1])*(p2[0]-p3[0]);
      const d3 = (p2[0]-p1[0])*(p3[1]-p1[1])-(p2[1]-p1[1])*(p3[0]-p1[0]);
      const d4 = (p2[0]-p1[0])*(p4[1]-p1[1])-(p2[1]-p1[1])*(p4[0]-p1[0]);
      return ((d1>0&&d2<0)||(d1<0&&d2>0)) && ((d3>0&&d4<0)||(d3<0&&d4>0)); };
    const isLeg = k => String(k).startsWith('L|');
    for (let a = 0; a < keys.length; a++) for (let b = a + 1; b < keys.length; b++) {
      const la = isLeg(keys[a]), lb = isLeg(keys[b]);
      if (la && lb) continue;
      for (const s1 of groups[keys[a]]) for (const s2 of groups[keys[b]]) {
        const p1 = P(s1.a), p2 = P(s1.b), p3 = P(s2.a), p4 = P(s2.b);
        if (!(on(p1) || on(p2) || on(p3) || on(p4))) continue;
        if (segInt([p1.x, p1.y], [p2.x, p2.y], [p3.x, p3.y], [p4.x, p4.y])) {
          if (la || lb) xTL++; else xTT++;
          if (xSites.length < 8)
            xSites.push([Math.round((p1.x + p2.x + p3.x + p4.x) / 4),
                         Math.round((p1.y + p2.y + p3.y + p4.y) / 4)]);
        } } } }
  out.crossTT = xTT; out.crossTL = xTL; out.crossSites = xSites;

  // ---- visible DOM label rects ----
  const layers = ['flabs', 'hubs', 'elabs', 'clabs'];
  const L = [];
  for (const ly of layers)
    for (const el of document.querySelectorAll('#' + ly + ' > *')) {
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) continue;
      if (r.right < 0 || r.bottom < 0 || r.left > cw || r.top > ch) continue;
      L.push({ ly, t: (el.textContent || '').slice(0, 24),
               x: r.left, y: r.top, w: r.width, h: r.height });
    }
  out.labels = { n: L.length,
    byLayer: layers.map(ly => ({ ly, n: L.filter(l => l.ly === ly).length })) };

  // label-label overlaps (pairwise rect intersection)
  let ll = 0; const llSites = [];
  for (let i = 0; i < L.length; i++) for (let j = i + 1; j < L.length; j++) {
    const a = L[i], b = L[j];
    if (a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h) {
      ll++;
      if (llSites.length < 8) llSites.push(a.ly + ':' + a.t + ' / ' + b.ly + ':' + b.t);
    } }
  out.labelLabelPairs = ll; out.labelLabelSites = llSites;

  // label-wire overlaps (Liang-Barsky seg-rect clip against the wire inventory)
  const segHitsRect = (s, l) => {
    let t0 = 0, t1 = 1;
    const dx = s.b[0] - s.a[0], dy = s.b[1] - s.a[1];
    const p = [-dx, dx, -dy, dy];
    const q = [s.a[0] - l.x, l.x + l.w - s.a[0], s.a[1] - l.y, l.y + l.h - s.a[1]];
    for (let k = 0; k < 4; k++) {
      if (p[k] === 0) { if (q[k] < 0) return false; }
      else { const r = q[k] / p[k];
        if (p[k] < 0) { if (r > t1) return false; if (r > t0) t0 = r; }
        else { if (r < t0) return false; if (r < t1) t1 = r; } } }
    return t0 <= t1; };
  let lwLabels = 0, lwSegHits = 0; const lwSites = [];
  for (const l of L) { let hit = 0;
    for (const s of segs) if (segHitsRect(s, l)) { hit++; lwSegHits++; }
    if (hit) { lwLabels++; if (lwSites.length < 8) lwSites.push(l.ly + ':' + l.t + ' x' + hit); } }
  out.labelWireLabels = lwLabels; out.labelWireSegHits = lwSegHits;
  out.labelWireSites = lwSites;

  // ---- chevron/label crowding events ----
  let cInLabel = 0, cPairs = 0, cBollard = 0;
  const inRect = (p, l, m) => p.x >= l.x - m && p.x <= l.x + l.w + m &&
                              p.y >= l.y - m && p.y <= l.y + l.h + m;
  for (const a of A) { for (const l of L) if (inRect(a, l, 4)) { cInLabel++; break; } }
  for (let i = 0; i < A.length; i++) for (let j = i + 1; j < A.length; j++)
    if (Math.hypot(A[i].x - A[j].x, A[i].y - A[j].y) < 14) cPairs++;
  for (const a of A) { for (const j of J)
    if (Math.hypot(a.x - j.x, a.y - j.y) < a.r + j.r + 2) { cBollard++; break; } }
  out.chevInLabel = cInLabel; out.chevPairsLt14 = cPairs; out.chevBollardTouch = cBollard;
  out.chevCrowdEvents = cInLabel + cPairs + cBollard;

  // ---- node occlusion fraction (fn boxes when fn layer is on, else spheres)
  const nodesOcc = [];
  if (d.fnMesh && d.fnMeta) {
    for (const m of d.fnMeta) {
      if (m.agg && !m.count) continue;            // scale-0 collapsed member
      const q = P(m.p); if (!on(q)) continue;
      nodesOcc.push({ x: q.x, y: q.y, r: (m.count ? 3 : 2) * pxwu(m.p), cd: camD(m.p) });
    } }
  else if (d.sphR) {
    for (let i = 0; i < d.nodes.length; i++) {
      if (d.alphaTgt[i] < 0.5) continue;
      const p = [d.pos[i*3], d.pos[i*3+1], d.pos[i*3+2]], q = P(p);
      if (!on(q)) continue;
      nodesOcc.push({ x: q.x, y: q.y, r: d.sphR(i) * pxwu(p), cd: camD(p) });
    } }
  let occHit = 0; const occSites = [];
  for (let i = 0; i < nodesOcc.length; i++) {
    const a = nodesOcc[i];
    for (let j = 0; j < nodesOcc.length; j++) {
      if (i === j) continue;
      const b = nodesOcc[j];
      if (b.cd + 4 >= a.cd) continue;             // occluder must be 4wu nearer
      if (Math.hypot(a.x - b.x, a.y - b.y) < b.r + 0.55 * a.r) {
        occHit++;
        if (occSites.length < 8) occSites.push([Math.round(a.x), Math.round(a.y)]);
        break; } } }
  out.occNodes = nodesOcc.length; out.occHit = occHit;
  out.nodeOcclFrac = nodesOcc.length ? +(occHit / nodesOcc.length).toFixed(3) : 0;

  // ---- extras: worst-site probes adjacent to the deferred defects ----
  let jClear = null, jjLt10 = 0, aFar = 0;
  if (J.length) {
    let jc = Infinity;
    const boxes = d.fnMeta ? d.fnMeta.filter(m => !(m.agg && !m.count)) : [];
    for (const j of J) { let best = Infinity;
      for (const m of boxes) { const q = P(m.p);
        const cl = Math.hypot(j.x - q.x, j.y - q.y) - (m.count ? 3 : 2) * pxwu(m.p);
        if (cl < best) best = cl; }
      jc = Math.min(jc, best - j.r); }
    jClear = isFinite(jc) ? jc : null;
    for (let i = 0; i < J.length; i++) for (let k = i + 1; k < J.length; k++)
      if (Math.hypot(J[i].x - J[k].x, J[i].y - J[k].y) < 10) jjLt10++;
    for (const a of A) { let near = Infinity;
      for (const j of J) near = Math.min(near, Math.hypot(a.x - j.x, a.y - j.y));
      if (near > 25) aFar++; }
  }
  out.jClearMinPx = jClear === null ? null : R1(jClear);
  out.jjPairsLt10 = jjLt10; out.arrowFarFromDelivery = aFar;

  // ---- orphan junction-leg census (user defect 2026-09-09: rendered leg
  // ink whose end context is unresolved under serveAll). A station tree
  // leg (busPtsMeta kind 'jleg', key L|fi|station|li) is orphaned when its
  // OWN-FILE box resolves under the res() floor (probe-px approximation of
  // the 2.5 REF-px law) OR no RENDERED bollard (scale>0 fnJDot instance
  // owned by that file) sits within 12 wu of either leg endpoint.
  // Informational until the next anchor cut gives it a baseline cell ----
  let legN = 0, oAny = 0, oBox = 0, oSt = 0, oBoth = 0, oSpeck = 0; const oSites = [];
  let eSt = 0; const eSites = [];                    // empty-station law violations
  if (d.busPts && d.busPtsMeta) {
    // rendered bollards only: the render gate parks gated dots at
    // r=0.0001 (fnJDotR), so radius > 0.001 wu == visible ink
    const bols = [];
    if (d.fnJDot && d.fnJDotR) { const im = d.fnJDot.instanceMatrix.array;
      for (let i = 0; i < d.fnJDotR.length; i++) {
        if ((d.fnJDotR[i] || 0) <= 0.001) continue;
        bols.push([im[i*16+12], im[i*16+13], im[i*16+14]]); } }
    // ANCHOR INK px per fi: the endpoint ink a user actually reads — the
    // node sprite when lit (alpha >= 0.5, the perceptual-anchor law's own
    // exemption: a lit 19-56px sphere anchors a leg whose fn box is 2-3px),
    // else the largest rendered fn-box px. Measuring the box alone
    // miscounted lit-sprite legs as specks on the 51a4555 bake.
    const anchorPxOf = new Map();
    const put = (fi, px) => { if (!anchorPxOf.has(fi) || px > anchorPxOf.get(fi)) anchorPxOf.set(fi, px); };
    if (d.fnMeta) for (const m of d.fnMeta) {
      if (m.agg && !m.count) continue;
      put(m.file, (m.count ? 6 : 4) * pxwu(m.p));  // pxOf units: DIAMETER px
    }
    if (d.sphR && d.alphaTgt && d.pos)
      for (let fi = 0; fi < d.nodes.length; fi++) {
        if (d.alphaTgt[fi] < 0.5) continue;         // unlit sprite = not anchor ink
        const p = [d.pos[fi*3], d.pos[fi*3+1], d.pos[fi*3+2]];
        put(fi, 2 * d.sphR(fi) * pxwu(p));          // sprite diameter px
      }
    const wuD = (a, b) => Math.hypot(a[0]-b[0], a[1]-b[1], a[2]-b[2]);
    // group chain pieces into one visual leg per station: key L|fi|st|li
    const chains = new Map();                      // "L|fi|st" -> {fi, ends:[], scr:[xy..]}
    for (let i = 0; i < d.busPts.length; i++) {
      const m = d.busPtsMeta[i]; if (!m || m.kind !== 'jleg') continue;
      const s = d.busPts[i], qa = P(s.a), qb = P(s.b);
      const vis = on(qa) || on(qb);
      const gk = String(d.busPts[i].k).split('|').slice(0, 3).join('|');
      let g = chains.get(gk);
      if (!g) chains.set(gk, g = { fi: m.fi, ends: [], scr: [], vis: false });
      g.ends.push(s.a, s.b);
      if (vis) { g.vis = true; g.scr.push([qa.x, qa.y], [qb.x, qb.y]); }
    }
    for (const g of chains.values()) {
      if (!g.vis) continue;                        // census covers on-screen ink
      legN++;
      const anchorPx = anchorPxOf.get(g.fi) || 0;
      const anchorOK = anchorPx >= 2.5;                // geometric res() floor
      // anchor speck (perceptual anchor law): anchor ink passes the 2.5px
      // geometric floor but lands under the ~6px PERCEPTUAL_ANCHOR — leg
      // endpoint lawfully served yet perceptually a speck
      if (anchorPx >= 2.5 && anchorPx < 6) oSpeck++;
      let stOK = false;
      outer: for (const e of g.ends) for (const b of bols)
        if (wuD(b, e) < 12) { stOK = true; break outer; }
      if (!anchorOK) oBox++;
      if (!stOK) oSt++;
      if (!anchorOK || !stOK) oAny++;
      if (!anchorOK && !stOK) { oBoth++;
        if (oSites.length < 8) { const c = g.scr[0] || [0, 0];
          oSites.push(((d.nodes[g.fi] && d.nodes[g.fi].path) || String(g.fi)) +
            ' @' + Math.round(c[0]) + ',' + Math.round(c[1])); } }
    }
    // EMPTY-STATION LAW (viz 7e334e3): a bollard renders only against an
    // attachment that actually served this frame. Geometric proxy (the
    // law's own key set stAttKey is not exported): a rendered, on-screen
    // bollard with NO rendered-conduit endpoint within 12 wu is floating
    // anchor-less ink — the exact "empty station dot floating at the
    // landing pose" sighting. Rendered conduit = fnBus instance scale >
    // 0.001 (the serve loop parks culled segments at 0.0001; busPts[i] is
    // parallel to fnBus instances).
    if (d.fnBus && d.fnBus.instanceMatrix) {
      const fa = d.fnBus.instanceMatrix.array;
      const servedEnds = [];
      for (let i = 0; i < d.busPts.length && i * 16 + 2 < fa.length; i++) {
        const sc = Math.hypot(fa[i*16], fa[i*16+1], fa[i*16+2]);
        if (sc <= 0.001) continue;
        servedEnds.push(d.busPts[i].a, d.busPts[i].b);
      }
      // context tests in screen px. (1) bus-conduit endpoint within 20px —
      // the terminus law's own bar (7e334e3 measured max gap 4.8px vs 20);
      // (2) any color-filtered wire tier (wire/quiet/arc/link) passing
      // within 12px — bollards lawfully ride non-bus wires too (mp site2:
      // magenta dot ON a lit link line); (3) anchor-ink EDGE within 12px
      // — depth-overlapped dots project onto big sprites whose centers
      // are far but whose ink covers the dot (mp site1).
      const distSeg = (px, py, s) => {
        const dx = s.b[0] - s.a[0], dy = s.b[1] - s.a[1], L2 = dx*dx + dy*dy;
        let t = L2 ? ((px - s.a[0])*dx + (py - s.a[1])*dy) / L2 : 0;
        t = Math.max(0, Math.min(1, t));
        return Math.hypot(px - (s.a[0] + t*dx), py - (s.a[1] + t*dy)); };
      const inkSegs = segs.filter(s => s.k !== 'leg' && s.k !== 'trunk');
      for (const bp of bols) {
        const sp = P(bp);
        if (!on(sp)) continue;                    // census covers visible ink
        let served = false;
        outer2: for (const e of servedEnds) { const q = P(e);
          if (Math.hypot(q.x - sp.x, q.y - sp.y) < 20) { served = true; break outer2; } }
        if (!served) for (const s of inkSegs)
          if (distSeg(sp.x, sp.y, s) < 12) { served = true; break; }
        if (!served) for (const [fi, px] of anchorPxOf) {
          if (px < 2.5) continue;
          const q = P([d.pos[fi*3], d.pos[fi*3+1], d.pos[fi*3+2]]);
          if (Math.hypot(q.x - sp.x, q.y - sp.y) < 12 + px / 2) { served = true; break; }
        }
        if (!served) { eSt++;
          if (eSites.length < 8) eSites.push('@' + Math.round(sp.x) + ',' + Math.round(sp.y)); }
      }
    }
  }
  // DEPTH-OWNED LIT RADIUS (viz 7e334e3): the depth slider owns which
  // sprites are lit — a lit file must sit at level <= depth (level -1 =
  // outside the focus tree). Lit ink beyond the law = ghosts leaking
  // outside the visible stratum.
  let oLit = 0; const lSites = [];
  // the law binds only when a focus tree exists — at rest (global, no
  // search) every file is lit by design and level is all -1
  if (d.alphaTgt && d.level && d.level.some(l => l >= 0)) {
    const dv = +(document.getElementById('depth').value || 1);
    for (let i = 0; i < d.alphaTgt.length; i++) {
      if (d.alphaTgt[i] < 0.5) continue;
      const lv = d.level[i];
      if (lv >= 0 && lv <= dv) continue;
      oLit++;
      if (lSites.length < 8) lSites.push(((d.nodes[i] && d.nodes[i].path) || String(i)) +
        ' lvl=' + lv + ' depth=' + dv);
    }
  }
  // ZERO-GAP ATTACHMENT (viz 73f6b2b): every served chain seam vertex must
  // equal its anchor marker position EXACTLY (=== on world floats). Semantics
  // mirror Lead's s12_zerogap.py: served = busPts[i] with fnBus instance
  // scale > 0.001; chains grouped by key; legs "L|fi|sid|li" — first.a ===
  // fnStations[sid].subJ[li], last.b === station p; trunks "sf>tf" —
  // first.a equals some station p of sf, last.b equals some station p of tf.
  // Taper stubs (explained exits) are culled (scale <= 0.001) and excluded.
  let zG = 0; const zSites = [];
  if (d.busPts && d.fnBus && d.fnBus.instanceMatrix && d.fnStations) {
    const fa2 = d.fnBus.instanceMatrix.array;
    const first = new Map(), last = new Map();
    for (let i = 0; i < d.busPts.length; i++) {
      if (Math.hypot(fa2[i*16], fa2[i*16+1], fa2[i*16+2]) <= 0.001) continue;
      const s = d.busPts[i], k = String(s.k);
      if (!first.has(k)) first.set(k, s);
      last.set(k, s);
    }
    const eq = (p, q) => p && q && p[0] === q[0] && p[1] === q[1] && p[2] === q[2];
    const stByFi = new Map();
    for (const S of d.fnStations) {
      if (!stByFi.has(S.fi)) stByFi.set(S.fi, []);
      stByFi.get(S.fi).push(S);
    }
    const stById = new Map(d.fnStations.map(S => [S.id, S]));
    for (const [k, s0] of first) {
      const s1 = last.get(k);
      const bad = why => { zG++;
        if (zSites.length < 8) zSites.push(k.slice(0, 24) + ' ' + why); };
      if (k.charCodeAt(0) === 76) {           // leg "L|fi|sid|li"
        const pp = k.split('|'), sid = +pp[2], li = +pp[3];
        const S = stById.get(sid);
        if (!S) { bad('no-station'); continue; }
        const sp = S.subJ && S.subJ[li];
        if (sp && !eq(s0.a, sp)) bad('a!=subJ');
        if (!eq(s1.b, S.p)) bad('b!=station');
      } else {                                 // trunk "sf>tf"
        const pp = k.split('>'), sf = +pp[0], tf = +pp[1];
        if (!(stByFi.get(sf) || []).some(S => eq(S.p, s0.a))) bad('a!=station(sf)');
        if (!(stByFi.get(tf) || []).some(S => eq(S.p, s1.b))) bad('b!=station(tf)');
      }
    }
  }
  out.zeroGapViol = zG; out.zeroGapSites = zSites;
  out.emptyStViol = eSt; out.emptyStSites = eSites;
  out.outsideLit = oLit; out.outsideLitSites = lSites;
  out.legN = legN; out.orphanJLegs = oAny;
  out.orphanJLegBoxFloor = oBox; out.orphanJLegStationGated = oSt;
  out.orphanJLegBothEnds = oBoth; out.orphanJLegSites = oSites;
  out.anchorSpecks = oSpeck;                      // perceptual-anchor bar: 0
  return out; }"""


def _png_rows(data: bytes):
    """Minimal dependency-free PNG decode (8-bit RGB/RGBA) -> rows of bytes."""
    import struct
    import zlib
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, idat, w, h, bd, ct = 8, b"", 0, 0, 8, 6
    while pos + 8 <= len(data):
        ln, typ = struct.unpack(">I4s", data[pos:pos + 8])
        pos += 8
        body = data[pos:pos + ln]
        pos += ln + 4
        if typ == b"IHDR":
            w, h, bd, ct = struct.unpack(">IIBB", body[:10])
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
    nch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ct]
    assert bd == 8, f"unexpected bit depth {bd}"
    bpp, stride = nch, w * nch
    raw = zlib.decompress(idat)
    rows, prev = [], bytearray(stride)
    for y in range(h):
        f = raw[y * (stride + 1)]
        row = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if f == 1:
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 255
        elif f == 2:
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                b, c = prev[i], (prev[i - bpp] if i >= bpp else 0)
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[i] = (row[i] + pr) & 255
        rows.append(row)
        prev = row
    return w, h, nch, rows


def ink_metrics(cdp, rect):
    """Drawn-pixel fraction (max channel >= 32) over the canvas: full frame +
    central band (middle 50% x 50%) — the panel-free, legend-free core."""
    x, y, w, h = [int(v) for v in rect]
    shot = cdp.send("Page.captureScreenshot", {
        "format": "png",
        "clip": {"x": x, "y": y, "width": w, "height": h, "scale": 0.25},
    })
    W, H, nch, rows = _png_rows(base64.b64decode(shot["data"]))
    x0, x1, y0, y1 = int(W * 0.25), int(W * 0.75), int(H * 0.25), int(H * 0.75)
    full_hit = full_n = band_hit = band_n = 0
    for yy, row in enumerate(rows):
        band_y = y0 <= yy < y1
        for xx in range(W):
            o = xx * nch
            lit = row[o] >= 32 or row[o + 1] >= 32 or row[o + 2] >= 32
            full_n += 1
            full_hit += lit
            if band_y and x0 <= xx < x1:
                band_n += 1
                band_hit += lit
    return {"inkFull": round(full_hit / full_n, 4) if full_n else None,
            "inkCentral": round(band_hit / band_n, 4) if band_n else None,
            "sample": [W, H]}


def settle(page, rounds=14):
    """Wait until the camera fly-to tween is fully stopped (3 identical reads)."""
    prev, same = None, 0
    for _ in range(rounds):
        cur = page.evaluate(
            "() => { const d = window.__dbg, c = d.camera.position, t = d.controls.target;"
            "return [c.x, c.y, c.z, t.x, t.y, t.z].map(x => +x.toFixed(4)).join(','); }")
        same = same + 1 if cur == prev else 0
        if same >= 2:
            return
        prev = cur
        page.wait_for_timeout(200)
def probe(page):
    """Settled JS_DECLUT read; two consecutive identical reads required."""
    b = None
    for _ in range(4):
        settle(page)
        a = page.evaluate(JS_DECLUT)
        b = page.evaluate(JS_DECLUT)
        if json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True):
            return a
    return b


def _apply_angle(page, op):
    if op is None:
        return
    if isinstance(op, tuple):
        page.evaluate(CAM_ORBIT, op[1])
    elif op == "top":
        page.evaluate(CAM_TOP)
    elif op == "zoom":
        page.evaluate(CAM_ZOOM)


def declut_subject(page, cdp, subject, prefix, qa):
    """All angles for one camera subject. Angles branch from the SAVED base
    pose, never chained, so each is reproducible standalone."""
    page.wait_for_timeout(300)
    rect = page.evaluate(
        "() => { const r = window.__dbg.renderer.domElement.getBoundingClientRect();"
        "return [r.left, r.top, r.width, r.height]; }")
    page.evaluate(CAM_SAVE)
    recs = {}
    for name, op in ANGLES:
        page.evaluate(CAM_RESTORE)
        _apply_angle(page, op)
        m = probe(page)
        if not m or m.get("BROKEN"):
            print(f"BROKEN probe at {subject}/{name}")
            sys.exit(2)
        m["ink"] = ink_metrics(cdp, rect)
        page.screenshot(path=str(qa / f"declut_{prefix}_{subject}_{name}.png"),
                        type="png")
        recs[name] = m
        print(f"  {subject}/{name}: crossTT={m['crossTT']} llPairs={m['labelLabelPairs']}"
              f" lwLabels={m['labelWireLabels']} crowd={m['chevCrowdEvents']}"
              f" occl={m['nodeOcclFrac']} inkC={m['ink']['inkCentral']}"
              f" orphanJLegs={m['legN']}/{m['orphanJLegs']}"
              f" (box={m['orphanJLegBoxFloor']} st={m['orphanJLegStationGated']}"
              f" both={m['orphanJLegBothEnds']}) anchorSpecks={m['anchorSpecks']}"
              f" emptySt={m['emptyStViol']} outsideLit={m['outsideLit']} zeroGap={m['zeroGapViol']}")
    page.evaluate(CAM_RESTORE)
    return recs


def _stem(path):
    s = re.sub(r"[^a-z0-9]+", "_", path.split("/")[-1].lower()).strip("_")
    return s or "hub"


def run_declut(page, qa: Path, prefix: str):
    """Spin off + map pane collapsed: the battery measures the 3D pane full-width."""
    page.evaluate("() => { const sb = document.getElementById('cbSpin');"
                  " if (sb && sb.checked) sb.click(); }")
    page.wait_for_timeout(150)
    page.evaluate("() => { const b = document.getElementById('bMap');"
                  " if (b.classList.contains('on')) b.click(); }")
    page.wait_for_timeout(600)
    cdp = page.context.new_cdp_session(page)
    hubs = page.evaluate(HUBS_JS)
    views = {}
    print(f"== declutter battery ({prefix}): global + {len(hubs)} hubs x {len(ANGLES)} angles ==")
    views["global"] = declut_subject(page, cdp, "global", prefix, qa)
    for rank, hub in enumerate(hubs, 1):
        subj = f"hub{rank}_{_stem(hub['p'])}"
        page.fill("#search", hub["p"])
        page.dispatch_event("#search", "input")
        if not page.is_checked("#cbFn"):
            page.check("#cbFn")
        page.wait_for_timeout(1500)
        rec = declut_subject(page, cdp, subj, prefix, qa)
        hub["level0N"] = page.evaluate(
            "() => window.__dbg.level.filter(x => x === 0).length")
        rec["_hub"] = hub
        views[subj] = rec
    return {"hubTopN": hubs, "views": views}


# gate: a declutter round must not make any view or any metric TOTAL worse,
# beyond the battery's own measured run-to-run spread. Cell tolerances were
# calibrated empirically (two full --after runs on build 469263d, after three
# bit-identical runs at dc34469): label collision resolution is history-
# dependent, so grazing label/crowd cells flip +-1..2 between identical runs;
# crossTT and nodeOcclFrac were bit-stable; inkCentral (GPU raster) drifted
# up to 0.0058. crossTT additionally carries the skeptic bar (<=3 per view,
# rubric C3): a view's limit is max(base, bar) — it may rise to the bar when
# baseline sits below it, but never above baseline when baseline exceeds it.
GATE_KEYS = [
    # key, label, cell tolerance, hard bar (or None)
    ("crossTT", "trunk-trunk screen crossings", 0, 3),
    ("labelLabelPairs", "label-label overlaps", 1, None),
    ("labelWireLabels", "label-wire overlaps", 1, None),
    ("chevCrowdHard", "chevron/label crowding (hard)", 2, None),
    ("nodeOcclFrac", "node occlusion fraction", 0.02, None),
    ("inkCentral", "central-band ink density", 0.008, None),
    # chain-integrity + perceptual-anchor laws (51a4555 bake): hard bar 0 —
    # no rendered leg may end unresolved (orphan) or at a speck anchor.
    # Baselines for these keys exist only in the anchor2+ anchors; gates vs
    # the v2 anchor skip them (get_metric returns None when base lacks it).
    ("orphanJLegs", "orphan junction legs", 0, 0),
    ("anchorSpecks", "speck-anchored legs (perceptual)", 0, 0),
    ("emptyStViol", "anchor-less bollards (empty-station law)", 0, 0),
    ("outsideLit", "lit beyond depth law", 0, 0),
    ("zeroGapViol", "seam != anchor (zero-gap law)", 0, 0),
]
# net-total guard (same calibration, totals across three 469263d runs):
# aggregate clutter must not regress beyond the instrument's resolution —
# crossTT totals were bit-identical across runs; the label/crowd totals
# jitter because their cells do. Totals are capped ABSOLUTELY against the
# current anchor (no per-round ratchet: later rounds face the same ceiling).
# llPairs total tol 2 = MEASURED same-build spread: anchor5 cut vs --after on
# the identical 73f6b2b bake read totals 5 vs 7 (four ±1 hub5 grazing-label
# cell flips; chromafix-era A/B also read [5,7]). Cells stay at +1.
TOTAL_TOL = {"crossTT": 0, "labelLabelPairs": 2, "labelWireLabels": 3,
             "chevCrowdHard": 5, "orphanJLegs": 0, "anchorSpecks": 0,
             "emptyStViol": 0, "outsideLit": 0, "zeroGapViol": 0}

# Metrics a rubric-sanctioned hub affordance (degree-hint / ghost-tier reveal)
# legitimately ADDS in zero-wire .tscn hub views. Exempted per-subject only via
# the explicit --affordance flag; structural clutter (crossTT, chevCrowdHard,
# nodeOcclFrac) stays hard-gated everywhere.
AFFORD_KEYS = {"labelLabelPairs", "labelWireLabels", "inkCentral"}


def get_metric(view, key):
    if key == "inkCentral":
        return (view.get("ink") or {}).get("inkCentral")
    if key == "chevCrowdHard":
        # clutter components of crowding only: chevron-chevron pairs and
        # chevrons inside label rects. chevBollardTouch is EXCLUDED on
        # purpose: occlusion-lifted chevrons land beside their delivery
        # junction bollards by design (chevObs work), and bollard
        # legibility is gated independently (jClearMinPx in the rubric,
        # occl here). Still recorded in the JSON + printed as INFO.
        return (view.get("chevPairsLt14") or 0) + (view.get("chevInLabel") or 0)
    return view.get(key)


def gate_declut(base_doc, after_doc, afford=frozenset()):
    rows, violations = [], []
    totals = {"base": {}, "after": {}}
    for subj, angles in after_doc["views"].items():
        base_subj = base_doc["views"].get(subj)
        for ang, m in angles.items():
            if ang.startswith("_"):
                continue
            base_m = (base_subj or {}).get(ang)
            for key, label, tol, bar in GATE_KEYS:
                a = get_metric(m, key)
                b = get_metric(base_m, key) if base_m else None
                if a is None:
                    rows.append((subj, ang, key, b, a, "MISSING"))
                    violations.append(f"{subj}/{ang}/{key}: probe missing value (after={a})")
                    continue
                if b is None:
                    # anchor predates this metric: no baseline cell, evaluate
                    # against the hard bar alone (legacy anchors stay usable)
                    if bar is None:
                        rows.append((subj, ang, key, b, a, "NOBASE"))
                        continue
                    b = 0
                exempt = subj in afford and key in AFFORD_KEYS
                if key in TOTAL_TOL and not exempt:
                    for side, val in (("base", b), ("after", a)):
                        totals[side][key] = totals[side].get(key, 0) + val
                limit = b + tol
                if bar is not None:
                    limit = max(limit, bar)
                ok = a <= limit
                if not ok and exempt:
                    rows.append((subj, ang, key, b, a, "AFFORD"))
                    continue
                rows.append((subj, ang, key, b, a, "ok" if ok else "REGRESS"))
                if not ok:
                    violations.append(
                        f"{subj}/{ang}/{label}: {b} -> {a} (limit {limit})")
    for subj, angles in after_doc["views"].items():
        base_subj = base_doc["views"].get(subj)
        for ang, m in angles.items():
            if ang.startswith("_"):
                continue
            base_m = (base_subj or {}).get(ang)
            a, b = m.get("chevBollardTouch"), (base_m or {}).get("chevBollardTouch")
            if a is not None and b is not None and a != b:
                print(f"  INFO   {subj}/{ang} chevBollardTouch: {b} -> {a} "
                      f"(informational: lifted chevrons near their junctions; "
                      f"gated via chevObs/jClear in the rubric)")
    for key, ttol in TOTAL_TOL.items():
        ba = totals["base"].get(key)
        af = totals["after"].get(key)
        if ba is None or af is None:
            continue  # missing cells already flagged above
        if af > ba + ttol:
            violations.append(
                f"TOTAL/{key}: {ba} -> {af} (net regression, tol +{ttol})")
            print(f"  REGRESS TOTAL/{key}: {ba} -> {af} (tol +{ttol})")
    print(f"== gate: {len(rows)} cell checks + {len(TOTAL_TOL)} total checks,"
          f" {len(violations)} violations"
          f" (+{sum(1 for r in rows if r[5] == 'AFFORD')} affordance-exempt) ==")
    for subj, ang, key, b, a, st in rows:
        if st != "ok":
            print(f"  {st:7s} {subj}/{ang} {key}: {b} -> {a}")
    if not violations:
        print("  all cells within tolerance, totals at-or-below baseline")
    return 1 if violations else 0


def expand_affordance(spec, base_doc):
    """'tscn' -> every hub subject whose hub file is .tscn; 'hubs' -> all
    hub subjects; otherwise comma-separated literal subject names."""
    names = set()
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        if item in ("tscn", "hubs"):
            for subj, rec in base_doc["views"].items():
                p = (rec.get("_hub") or {}).get("p", "")
                if item == "hubs" or (item == "tscn" and p.endswith(".tscn")):
                    names.add(subj)
        else:
            names.add(item)
    return frozenset(names)


class ReuseTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def _serve():
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(STATE))
    # #132: ephemeral loopback port — the gate runs beside test_viz (or a
    # second copy of itself) without contending for a fixed port, and a
    # rerun never trips over the TIME_WAIT socket a previous run left.
    httpd = ReuseTCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def _git_sha():
    import subprocess
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=10
                              ).stdout.strip()
    except Exception:
        return ""


def run_declut_battery(prefix: str, port: int):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 900})
            page.route("**/favicon.ico", lambda r: r.fulfill(status=200, body=""))
            page.goto(f"http://127.0.0.1:{port}/graph.html", wait_until="load")
            page.wait_for_timeout(4000)
            if not page.evaluate("() => !!window.__dbg"):
                print("BROKEN BUILD: window.__dbg missing/null at boot")
                sys.exit(2)
            return run_declut(page, QA, prefix)
        finally:
            browser.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0]
                                 if __doc__ else None)
    ap.add_argument("--declutter", action="store_true",
                    help="capture the declutter baseline battery")
    ap.add_argument("--after", action="store_true",
                    help="run the battery and gate it against the baseline")
    ap.add_argument("--base", default=".tmp/qa/declutter_base.json",
                    help="baseline JSON for --after (default %(default)s)")
    ap.add_argument("--affordance", default="", metavar="SUBJECTS",
                    help="with --after: comma list of subjects whose sanctioned "
                         "hub-affordance ink (labelLabelPairs/labelWireLabels/"
                         "inkCentral) is exempt from the gate; 'tscn' expands to "
                         "all .tscn hub subjects, 'hubs' to all hub subjects")
    args = ap.parse_args()
    QA.mkdir(parents=True, exist_ok=True)
    httpd, port = _serve()
    try:
        if args.declutter:
            doc = run_declut_battery("base", port)
            out = QA / "declutter_base.json"
            out.write_text(json.dumps(doc, indent=1), encoding="utf-8")
            sha = _git_sha()
            if sha:
                snap = QA / f"declutter_base_{sha}.json"
                snap.write_text(json.dumps(doc, indent=1), encoding="utf-8")
                print(f"wrote {out} + snapshot {snap}")
            else:
                print(f"wrote {out} (no git sha; snapshot skipped)")
            return
        if args.after:
            basep = (ROOT / args.base).resolve()
            if not Path(args.base).is_absolute() and not basep.exists():
                basep = Path(args.base).resolve()
            base_doc = json.loads(Path(basep).read_text(encoding="utf-8"))
            afford = expand_affordance(args.affordance, base_doc)
            if afford:
                print(f"affordance-exempt subjects: {', '.join(sorted(afford))}")
            doc = run_declut_battery("after", port)
            (QA / "declutter_after.json").write_text(
                json.dumps(doc, indent=1), encoding="utf-8")
            sys.exit(gate_declut(base_doc, doc, afford))
        run(QA, port)
    finally:
        httpd.shutdown()
        httpd.server_close()


def run(qa: Path, port: int):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 900})
            page.route("**/favicon.ico", lambda r: r.fulfill(status=200, body=""))
            errors = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.goto(f"http://127.0.0.1:{port}/graph.html", wait_until="load")
            page.wait_for_timeout(4000)  # settle (no animation dependence)
            page.evaluate("""() => { const sb = document.getElementById('cbSpin');
                                      if (sb && sb.checked) sb.click(); }""")
            page.wait_for_timeout(150)

            # --- 3D focus state (mirrors tests/test_viz.py:140-150) ---
            tok = page.evaluate(TOK_JS)
            page.fill("#search", tok)
            page.dispatch_event("#search", "input")
            page.check("#cbFn")
            page.wait_for_timeout(1200)
            m3 = page.evaluate(JS_3D)
            if not m3 or m3.get("fail"):
                print(f"FAIL 3D metrics: {m3}")
                sys.exit(1)
            # full-window 3D shot: collapse the map pane
            page.evaluate("""() => { const b = document.getElementById('bMap');
                                      if (b.classList.contains('on')) b.click(); }""")
            page.wait_for_timeout(800)
            page.screenshot(path=str(qa / "gate_base_3d.png"), type="png")

            # --- 2D map: reopen pane, read layout-derived metrics ---
            page.evaluate("""() => { const b = document.getElementById('bMap');
                                      if (!b.classList.contains('on')) b.click(); }""")
            page.wait_for_timeout(800)
            m2 = page.evaluate(JS_2D)
            if not m2 or m2.get("fail"):
                print(f"FAIL 2D metrics: {m2}")
                sys.exit(1)
            page.locator("#mapPane").screenshot(path=str(qa / "gate_base_map.png"), type="png")

            real_errors = [e for e in errors if "favicon" not in e]
            doc = {"tok": tok, "consoleErrors": real_errors, "d3": m3, "map": m2}
            (qa / "readability_base.json").write_text(
                json.dumps(doc, indent=1, sort_keys=False), encoding="utf-8")

            jwp = m3["junctionWireProximity"]
            np_ = m2["nearParallel"]
            print(f"== 3D focus tok='{tok}' ==")
            print(f"junctions={m3['junctions']} minJunctionGap={m3['minJunctionGap']}"
                  f" wireSegs={m3['wireSegments']} quietSegs={m3['quietSegments']}")
            print(f"junction wire proximity  r8 max/mean/hit={jwp['r8']}  r12={jwp['r12']}  r16={jwp['r16']}")
            print(f"worst junction: {jwp['worstJunction']}  quietNear12={jwp['quietR12']}")
            print(f"fnBoxes={m3['fnBoxes']} within12OfJunction={m3['boxesWithin12OfJunction']}"
                  f" minBoxSurface={m3['minBoxJunctionSurface']}")
            print(f"conduits: trunks={m3['conduitTrunks']} segs={m3['conduitSegments']}"
                  f" pairsUnder6={m3['conduitPairsUnder6']} minGap={m3['minConduitGap']}")
            print(f"trunkRiders={m3['trunkRiders']} junctionStubs={m3['junctionStubs']}"
                  f" quietTrunks={m3['quietTrunks']}({m3['quietTrunkRiders']} wires)")
            print("== 2D map ==")
            print(f"E={m2['mapInfo']['E']} wires={m2['wires']} spines {m2['spinesDrawn']}/{m2['spinesTotal']}"
                  f" trunkGroups={m2['mapInfo']['trunkGroups']} buses={m2['buses']}"
                  f" chips={m2['chips']} rosterRows={m2['mapInfo']['rosterRows']}")
            print(f"nearParallel pairs={np_['pairs']} (ww={np_['wireWire']} ss={np_['spineSpine']}"
                  f" sw={np_['spineWire']}) over {np_['segments']} segs; worst={np_['worstPolyline']}")
            print(f"consoleErrors={len(real_errors)}")
            print(f"wrote {qa / 'readability_base.json'}, gate_base_3d.png, gate_base_map.png")
            if real_errors:
                sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
