# Objective readability gate for the neuronav visualizer.
# Serves the repo root, loads graph.html in headless Chrome, drives the SAME
# focus state as tests/test_viz.py (highest-degree node stem), and measures
# ink-clutter metrics in both layers straight from window.__dbg.
# Run: .venv/Scripts/python.exe -X utf8 tools/qa_readability.py   (exit 0 = ok)
# Outputs: qa/readability_base.json + qa/gate_base_3d.png + qa/gate_base_map.png
import http.server
import json
import socketserver
import sys
import threading
from functools import partial
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PORT = 8951
QA = ROOT / "qa"

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


class ReuseTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def main():
    QA.mkdir(parents=True, exist_ok=True)
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT))
    with ReuseTCPServer(("127.0.0.1", PORT), handler) as httpd:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            run(QA)
        finally:
            httpd.shutdown()
            httpd.server_close()


def run(qa: Path):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 900})
            page.route("**/favicon.ico", lambda r: r.fulfill(status=200, body=""))
            errors = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.goto(f"http://127.0.0.1:{PORT}/graph.html", wait_until="load")
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
