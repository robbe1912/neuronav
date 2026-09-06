# neuronav viz QA harness — drives the real page in headless Chrome.
# Run: .venv/Scripts/python.exe -X utf8 tests/test_viz.py  (exit 0 = all pass)
# Uses system Chrome via channel="chrome" (no browser download needed).
import http.server
import socketserver
import threading
import sys
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PORT = 8931
FAILURES = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"{tag} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def main():
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("127.0.0.1", PORT), handler) as httpd:
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            run_tests()
        finally:
            httpd.shutdown()


def run_tests():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 900})
        page.route("**/favicon.ico", lambda r: r.fulfill(status=200, body=""))
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(f"http://127.0.0.1:{PORT}/graph.html", wait_until="load")
        page.wait_for_timeout(4000)  # frozen layout — settle only
        # tests chip is dynamically created (span, text "tests") inside #dirs
        tchip = page.locator("#dirs span").filter(has_text=re.compile(r"^tests$")).first

        # 1. overview loads: title, stats, no product console errors
        check("title", page.title() == "neuronav — code graph", page.title())
        real_errors = [e for e in errors if "favicon" not in e]
        check("console clean", not real_errors, "; ".join(real_errors[:3]))
        stats = page.text_content("#stats") or ""
        m = re.search(r"(\d+) files · (\d+) links · (\d+) clusters", stats)
        check("stats line", bool(m), stats.strip()[:90])
        n_files = int(m.group(1)) if m else 0

        # 1b. strata reading order: stats must announce the depth channel
        strata = page.evaluate("() => window.__dbg.meta ? window.__dbg.meta.strata : false")
        if strata:
            check("strata channel noted in stats",
                  "height = call depth from entry" in stats, stats.strip()[:90])

        # 2. true 3D: files are instanced spheres
        shapes = page.evaluate(
            """() => { const d = window.__dbg; return {
                 isInstanced: d.fileMesh.isInstancedMesh,
                 fileGeom: d.fileMesh.geometry.type,
                 lit: d.alphaTgt.reduce((s, a) => s + (a > 0.5 ? 1 : 0), 0) }; }"""
        )
        check("files instanced spheres",
              shapes["isInstanced"] and shapes["fileGeom"] == "SphereGeometry", str(shapes))
        lit_base = shapes["lit"]

        # 3. hidden tests: scale-0 collapse + roundtrip restore
        tchip.click()  # show tests
        lit_shown = page.evaluate(
            "() => window.__dbg.alphaTgt.reduce((s, a) => s + (a > 0.5 ? 1 : 0), 0)")
        tchip.click()  # hide again
        lit_hidden = page.evaluate(
            "() => window.__dbg.alphaTgt.reduce((s, a) => s + (a > 0.5 ? 1 : 0), 0)")
        check("tests toggle roundtrip", lit_shown > lit_hidden and lit_hidden == lit_base,
              f"base {lit_base} / shown {lit_shown} / hidden {lit_hidden}")

        # 3b. regression: hidden-test WIRE geometry collapses (not just black
        # color — normal blending paints black pixels over content). Find a
        # highway arc with a test endpoint; while tests are hidden its arc
        # slot must be degenerate (all 16 segments at the source node).
        arc_hidden = page.evaluate(
            """() => { const d = window.__dbg;
                 const isTest = j => d.nodes[j].path.startsWith('tests/') || d.nodes[j].path.startsWith('tools/');
                 for (let i = 0; i < d.links.length; i++) {
                   if (d.hwSlot[i] < 0) continue;
                   if (!isTest(d.links[i].s) && !isTest(d.links[i].t)) continue;
                   const arr = d.bucketPosIB[d.bucketOf[i]].array, b = d.hwSlot[i];
                   const s = d.links[i].s;
                   let span = 0;
                   for (let v = 0; v < 16; v++) {
                     const o = b + v * 6;
                     span += Math.abs(arr[o] - d.pos[s*3]) + Math.abs(arr[o+2] - d.pos[s*3+2]);
                   }
                   return span;
                 }
                 return -1; }""")
        tchip.click()  # show tests again for later checks
        arc_shown = page.evaluate(
            """() => { const d = window.__dbg;
                 for (let i = 0; i < d.links.length; i++) {
                   if (d.hwSlot[i] < 0) continue;
                   const isTest = j => d.nodes[j].path.startsWith('tests/') || d.nodes[j].path.startsWith('tools/');
                   if (!isTest(d.links[i].s) && !isTest(d.links[i].t)) continue;
                   const arr = d.bucketPosIB[d.bucketOf[i]].array, b = d.hwSlot[i];
                   const s = d.links[i].s, t = d.links[i].t;
                   // both endpoints are surface-trimmed (radius + 2 margin):
                   // expected total offset = trim_s + trim_t
                   const exp = d.sizes[s] * 1.1 + 2 + d.sizes[t] * 1.1 + 2;
                   const d3 = (o, j) => {
                     const dx = arr[o] - d.pos[j*3], dy = arr[o+1] - d.pos[j*3+1], dz = arr[o+2] - d.pos[j*3+2];
                     return Math.sqrt(dx*dx + dy*dy + dz*dz);
                   };
                   const err = d3(b, s) + d3(b + 93, t);
                   return { err: +err.toFixed(1), exp: +exp.toFixed(1),
                            ssz: +d.sizes[s].toFixed(1), tsz: +d.sizes[t].toFixed(1),
                            a0: [+arr[b].toFixed(1), +arr[b+1].toFixed(1)],
                            pS: [+d.pos[s*3].toFixed(1), +d.pos[s*3+1].toFixed(1)],
                            path: d.nodes[s].path };
                 }
                 return { err: -1, exp: 0 }; }""")
        check("hidden-test arcs collapse geometrically",
              arc_hidden == 0 and (arc_shown.get("err", -1) < 0 or
                                   0.5 * arc_shown["exp"] <= arc_shown["err"] <= arc_shown["exp"] + 2.0),
              f"hidden span {arc_hidden} / shown trim {arc_shown}")


        # 4. focus + functions: fn boxes exist and orbit their OWNER file
        # sphere (arc placement) instead of sitting on the wire midpoints.
        # The focus token must exist in THIS index (harness is config-agnostic
        # since the self-index landed): aim at the highest-degree node's
        # path stem — 'magicplayer' hardcoding died when config left SWMG.
        tok = page.evaluate(
            """() => { const d = window.__dbg;
                 let best = 0;
                 for (let i = 1; i < d.nodes.length; i++)
                   if ((d.adj[i]||[]).length > (d.adj[best]||[]).length) best = i;
                 return d.nodes[best].path.split('/').pop().replace(/\\.[^.]+$/, '').toLowerCase(); }"""
        )
        page.fill("#search", tok)
        page.dispatch_event("#search", "input")
        page.check("#cbFn")
        page.wait_for_timeout(1200)
        vic = page.evaluate(
            """() => { const d = window.__dbg; const nm = d.fnMesh, meta = d.fnMeta;
                 if (!nm || !meta || !meta.length) return { fail: 'no fn layer' };
                 const pos = d.pos, sizes = d.sizes;
                 let maxOff = 0;
                 for (const fm of meta) {
                   const A = fm.file, p = fm.p;
                   const dx = p[0]-pos[A*3], dy = p[1]-pos[A*3+1], dz = p[2]-pos[A*3+2];
                   // owner-vicinity: boxes sit on arcs of radius
                   // ownerRadius + 14 + (i%3)*6 — at most 26 past the surface
                   maxOff = Math.max(maxOff, Math.sqrt(dx*dx+dy*dy+dz*dz) - sizes[A] * 1.1);
                 }
                 return { count: nm.count, geom: nm.geometry.type,
                          isInstanced: nm.isInstancedMesh, maxOff }; }"""
        )
        check("fn boxes instanced cubes",
              vic.get("isInstanced") and vic.get("geom") == "BoxGeometry", str(vic))
        check("fn boxes orbit owner sphere", vic.get("maxOff", 99) <= 30,
              f"maxOff={vic.get('maxOff')} count={vic.get('count')}")

        # 5. hover a fn box -> tooltip shows path :: name
        hover = page.evaluate(
            """() => { const d = window.__dbg;
                 // hover a REAL box: aggregated members render scale-0 and cannot be raycast
                 const fm = d.fnMeta.find(m => !m.agg || m.count) || d.fnMeta[0]; if (!fm) return null;
                 const v = new d.THREE.Vector3(fm.p[0], fm.p[1], fm.p[2]).project(d.camera);
                 const r = d.renderer.domElement.getBoundingClientRect();
                 const sx = (v.x*0.5+0.5)*r.width + r.left, sy = (-v.y*0.5+0.5)*r.height + r.top;
                 d.renderer.domElement.dispatchEvent(
                   new PointerEvent('pointermove', { clientX: sx, clientY: sy, bubbles: true }));
                 return new Promise(res => setTimeout(() => {
                   const tip = document.getElementById('tip');
                   res({ shown: tip.style.display === 'block',
                         text: tip.textContent,
                         want: d.nodes[fm.file].path + ' :: ' + fm.name }); }, 300)); }"""
        )
        check("hover tooltip on fn box", bool(hover) and hover["shown"]
              and hover["text"] == hover["want"], str(hover))

        # 5a. fn hover makes OWNERSHIP visible: white stalk box→owner file
        # plus the owner's instance color lifting above its base cluster hue
        own = page.evaluate(
            """() => { const d = window.__dbg;
                 // hover a REAL box: aggregated members render scale-0 and cannot be raycast
                 const fm = d.fnMeta.find(m => !m.agg || m.count) || d.fnMeta[0]; if (!fm) return null;
                 const v = new d.THREE.Vector3(fm.p[0], fm.p[1], fm.p[2]).project(d.camera);
                 const r = d.renderer.domElement.getBoundingClientRect();
                 const sx = (v.x*0.5+0.5)*r.width + r.left, sy = (-v.y*0.5+0.5)*r.height + r.top;
                 d.renderer.domElement.dispatchEvent(
                   new PointerEvent('pointermove', { clientX: sx, clientY: sy, bubbles: true }));
                 return new Promise(res => setTimeout(() => {
                   // instance color = colArr × alpha × lift; lift==1.9 for the
                   // owner, 1 otherwise — compare against the un-lifted render
                   // value colArr × alpha, not raw colArr (focus dims level-1
                   // owners below their own base hue)
                   const f = fm.file;
                   res({ vis: !!(d.fnStalk && d.fnStalk.visible),
                         lifted: d.fileMesh.instanceColor.array[f*3]
                                 > d.colArr[f*3] * d.alpha[f] * 1.5 }); }, 300)); }"""
        )
        check("fn hover stalks + lifts owner file",
              bool(own) and own["vis"] and own["lifted"], str(own))

        # 5a2. persistent ownership stalks: one faint segment per fn box,
        # rebuilt with the fn layer (agg boxes render degenerate by design)
        stalks = page.evaluate(
            """() => { const d = window.__dbg;
                 if (!d.fnStalks) return { fail: 'no fnStalks mesh' };
                 if (!d.fnStalks.visible) return { fail: 'stalks not visible' };
                 const arr = d.fnStalks.geometry.attributes.position.array;
                 let real = 0;
                 for (let i = 0; i < arr.length; i += 6)
                   if (arr[i] !== arr[i+3] || arr[i+1] !== arr[i+4] || arr[i+2] !== arr[i+5]) real++;
                 return { fns: d.fnMeta.length, real,
                          agg: d.fnMeta.filter(m => m.agg).length }; }"""
        )
        check("persistent fn stalks present",
              bool(stalks) and "fail" not in stalks and stalks.get("real", 0) > 0,
              str(stalks))

        # pointer still parked on the fn box -> capture the stalk evidence
        page.screenshot(path=str(ROOT / "tests" / "qa_stalk.png"), scale="css", type="png")

        # 5b. hover a lit neighbor while focused -> tooltip shows BFS path to seed
        hop = page.evaluate(
            """() => { const d = window.__dbg;
                 // query focus: seeds are the search matches (pathToSeed uses the
                 // same set), so pick the first lit match as the seed
                 let seed = -1;
                 for (let j = 0; j < d.nodes.length; j++) {
                   if (d.nodes[j].path.toLowerCase().includes('__TOK__') && d.alphaTgt[j] > 0.5) { seed = j; break; }
                 }
                 if (seed < 0) return { fail: 'no lit query match' };
                 let nb = -1;
                 const A = d.adj || {};
                 for (const v of (A[seed] || [])) {
                   if (d.alphaTgt[v] > 0.5 && !d.nodes[v].path.toLowerCase().includes('__TOK__')) { nb = v; break; } }
                 if (nb < 0) return { fail: 'no lit neighbor' };
                 const v = new d.THREE.Vector3(d.pos[nb*3], d.pos[nb*3+1], d.pos[nb*3+2]).project(d.camera);
                 const r = d.renderer.domElement.getBoundingClientRect();
                 const sx = (v.x*0.5+0.5)*r.width + r.left, sy = (-v.y*0.5+0.5)*r.height + r.top;
                 d.renderer.domElement.dispatchEvent(
                   new PointerEvent('pointermove', { clientX: sx, clientY: sy, bubbles: true }));
                 return new Promise(res => setTimeout(() => {
                   const tip = document.getElementById('tip');
                   res({ shown: tip.style.display === 'block',
                         text: tip.textContent,
                          seedLabel: d.nodes[seed].label,
                          nbLabel: d.nodes[nb].label }); }, 300)); }""".replace("__TOK__", tok)
        )
        check("hover path to seed",
              bool(hop) and hop.get("shown")
              and "→ seed:" in (hop.get("text") or "")
              and hop.get("seedLabel") in (hop.get("text") or "")
              and hop.get("nbLabel") in (hop.get("text") or ""),
              str(hop))

        # 5ba. direction flow: while focusing, call-edge materials animate
        check("dash-flow on while focusing",
              bool(page.evaluate("() => window.__dbg.bucketMat.some(m => m.dashed)")),
              "dashed on call materials during focus")

        # 5bb. hover feedback in-scene: sphere scale lerps to ~1.8x then back
        # (auto-spin off first: rotation moves the projection we aim at;
        # fn layer off too — its boxes steal the raycast pick near the
        # focused node)
        page.evaluate("""() => { const sb = document.getElementById('cbSpin');
                                 if (sb.checked) sb.click();
                                 if (document.getElementById('cbFn').checked)
                                   document.getElementById('cbFn').click(); }""")
        page.wait_for_timeout(150)
        scale = page.evaluate(
            """() => { const d = window.__dbg;
                 const mat = new d.THREE.Matrix4();
                 const mx = i => { d.fileMesh.getMatrixAt(i, mat); return mat.elements[0]; };
                 const move = (x, y) => d.renderer.domElement.dispatchEvent(
                   new PointerEvent('pointermove', { clientX: x, clientY: y, bubbles: true }));
                 // reuse the still-hovered neighbor from 5b, else the node
                 // nearest the camera target (post-hop that's the framed
                 // node; "first lit node" may be behind the camera)
                 let idx = d.hovered;
                 if (idx >= 0 && d.alphaTgt[idx] <= 0.9) idx = -1;
                 if (idx < 0) {
                   const t = d.controls.target;
                   let best = 1e18;
                   for (let j = 0; j < d.nodes.length; j++) {
                     if (d.alphaTgt[j] <= 0.9) continue;   // fully lit only
                     const dx = d.pos[j*3]-t.x, dy = d.pos[j*3+1]-t.y, dz = d.pos[j*3+2]-t.z;
                     const q = dx*dx+dy*dy+dz*dz;
                     if (q < best) { best = q; idx = j; }
                   }
                 }
                 if (idx < 0) return { fail: 'no lit node' };
                 const vv = new d.THREE.Vector3(d.pos[idx*3], d.pos[idx*3+1], d.pos[idx*3+2]).project(d.camera);
                 const v = new d.THREE.Vector3(d.pos[idx*3], d.pos[idx*3+1], d.pos[idx*3+2]).project(d.camera);
                 const r = d.renderer.domElement.getBoundingClientRect();
                 const sx = (v.x*0.5+0.5)*r.width + r.left, sy = (-v.y*0.5+0.5)*r.height + r.top;
                 // 1) park the pointer offscreen so any residual hover decays
                 move(-500, -500);
                 return new Promise(res => setTimeout(() => {
                   const before = mx(idx);
                   // 2) hover 500ms (ease ~0.18/frame) -> ~1.8x
                   move(sx, sy);
                   setTimeout(() => {
                     const grown = mx(idx);
                     const hAt = d.hovered;
                     // 3) off again -> decays back to base
                     move(-500, -500);
                     setTimeout(() => res({ before, grown, after: mx(idx),
                                            hoveredAt: hAt,
                                            vinfo: { z: +vv.z.toFixed(4), sx: Math.round(sx), sy: Math.round(sy),
                                                     a: d.alphaTgt[idx], fn: d.hoveredFn },
                                            hoveredOff: d.hovered === -1,
                                            hoverScale: d.hoverScale[idx] }), 700);
                   }, 500);
                 }, 700)); }"""
        )
        check("hover scale eases 1.8x and back",
              bool(scale) and "fail" not in scale
              and scale.get("grown", 0) > scale.get("before", 1) * 1.5
              and scale.get("after", 99) < scale.get("before", 1) * 1.1
              and scale.get("hoveredOff", False),
              str(scale))

        # 5bb2. hover greyout: hovering a node dims everything outside its
        # 1-hop neighborhood to ~0.12 and restores on leave (Cosmograph steal).
        # Clear the 5b search focus first — greyout defers to focus mode.
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        grey_setup = page.evaluate(
            """() => { const d = window.__dbg;
                 // pick a visible hub with >= 2 neighbors and a far-away node
                 let hub = -1;
                 for (let j = 0; j < d.nodes.length; j++) {
                   if (d.alphaTgt[j] <= 0.5) continue;
                   if ((d.adj[j] || []).length >= 2) { hub = j; break; } }
                 if (hub < 0) return { fail: 'no hub' };
                 const nb = d.adj[hub][0];
                 let far = -1;
                 for (let j = 0; j < d.nodes.length; j++) {
                   if (j === hub || j === nb || (d.adj[j] || []).includes(hub)) continue;
                   if (d.alphaTgt[j] > 0.5) { far = j; break; } }
                 if (far < 0) return { fail: 'no unconnected node' };
                 const move = (x, y) => d.renderer.domElement.dispatchEvent(
                   new PointerEvent('pointermove', { clientX: x, clientY: y, bubbles: true }));
                 const v = new d.THREE.Vector3(d.pos[hub*3], d.pos[hub*3+1], d.pos[hub*3+2]).project(d.camera);
                 const r = d.renderer.domElement.getBoundingClientRect();
                 move((v.x*0.5+0.5)*r.width + r.left, (-v.y*0.5+0.5)*r.height + r.top);
                 return { hub, nb, far }; }"""
        )
        page.wait_for_timeout(700)
        grey_on = page.evaluate(
            """(s) => { const d = window.__dbg;
                 return { hub: d.alphaTgt[s.hub], nb: d.alphaTgt[s.nb], far: d.alphaTgt[s.far] }; }""",
            grey_setup)
        page.screenshot(path=str(ROOT / "tests" / "qa_grey.png"), scale="css", type="png")
        page.evaluate(
            """() => window.__dbg.renderer.domElement.dispatchEvent(
                 new PointerEvent('pointermove', { clientX: -500, clientY: -500, bubbles: true }))"""
        )
        page.wait_for_timeout(700)
        grey_back = page.evaluate(
            """(s) => { const d = window.__dbg;
                 return { hubTgt: d.alphaTgt[s.hub], farBack: d.alphaTgt[s.far] }; }""",
            grey_setup)
        check("hover greys non-neighbors",
              bool(grey_setup) and "fail" not in grey_setup
              and grey_on.get("hub", 0) > 0.9
              and grey_on.get("nb", 0) > 0.9
              and grey_on.get("far", 1) <= 0.13
              and grey_back.get("farBack", 0) > 0.9,
              f"on {grey_on} / back {grey_back}")

        # 5c. cluster chip isolate -> camera tweens to frame the island
        # (clear focus first so chips act on the overview)
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        check("dash-flow off after focus cleared",
              not page.evaluate("() => window.__dbg.bucketMat.some(m => m.dashed)"),
              "solid lines at overview")
        island = page.evaluate(
            """() => { const chip0 = document.querySelector('#legend .chip');
                 if (!chip0) return { fail: 'no legend chip' };
                 const d = window.__dbg;
                 const d0 = d.camera.position.distanceTo(d.controls.target);
                 chip0.click();
                 return new Promise(res => setTimeout(() => {
                   const dd = window.__dbg;
                   const d1 = dd.camera.position.distanceTo(dd.controls.target);
                   res({ d0: Math.round(d0), d1: Math.round(d1) });
                 }, 600)); }"""
        )
        check("cluster chip frames island",
              bool(island) and "fail" not in island
              and island.get("d1", 0) < island.get("d0", 1) * 0.9,
              str(island))

        # 5cb. label LOD: zoom-driven hub cap rises as the camera closes in.
        # (Mechanism-level assert — placement/offscreen culling then decides
        # how many labels actually show, which the other label tests cover.)
        lod = page.evaluate(
            """() => new Promise(res => { const d = window.__dbg;
                 // restore overview framing first: the chip test left the
                 // camera at island distance, which already raised the cap
                 document.getElementById('bReset').click();
                 setTimeout(() => {
                   const far0 = d.hubCap;
                   const t = d.controls.target.clone();
                   d.camera.position.lerp(t, 0.45);
                   setTimeout(() => { res({ far0, near1: d.hubCap }); }, 400);
                 }, 600); })"""
        )
        check("label LOD: hub cap rises on zoom-in",
              bool(lod) and "fail" not in lod
              and lod.get("far0", 0) > 0
              and lod.get("near1", 0) > lod.get("far0", 0),
              str(lod))

        # 5eb. strata geometry: callees sit strictly below callers in Y.
        # y = half - depth*spacing + jitter(<=0.15 spacing), so a +1-depth
        # edge clears at least 0.7 spacing of drop; sample every link.
        strata_y = page.evaluate(
            """() => { const d = window.__dbg;
                 if (!d.meta || !d.meta.strata) return { skip: true };
                 const depth = d.meta.depth;
                 let maxY = -1e9, minY = 1e9, maxd = 0;
                 for (let j = 0; j < d.nodes.length; j++) {
                   maxY = Math.max(maxY, d.pos[j*3+1]); minY = Math.min(minY, d.pos[j*3+1]); }
                 for (let j = 0; j < depth.length; j++) maxd = Math.max(maxd, depth[j]);
                 if (maxd === 0) return { skip: true, flat: true };
                 const spacing = (maxY - minY) / maxd;
                 let checked = 0, bad = 0;
                 for (const l of d.links) {
                   if (depth[l.t] <= depth[l.s]) continue;
                   checked++;
                   if (d.pos[l.t*3+1] > d.pos[l.s*3+1] - 0.4 * spacing) bad++;
                 }
                 return { checked, bad }; }"""
        )
        if strata_y.get("skip"):
            print(f"SKIP strata Y geometry — {strata_y}")
        else:
            check("strata: callees below callers",
                  strata_y.get("checked", 0) > 0 and strata_y.get("bad", 1) == 0,
                  f"{strata_y.get('checked')} depth-rising links, {strata_y.get('bad')} inverted")

        # 5ec. auto-spin: OFF at boot by default (eye tracking); checkbox is
        # the truth (tick re-derives autoRotate every frame — reads need a
        # settle after each change): off at boot -> on after enable ->
        # off after disable.
        spin = page.evaluate(
            """() => new Promise(res => { const d = window.__dbg;
                 const box = document.getElementById('cbSpin');
                 if (!box) return res({ fail: 'no cbSpin' });
                 const set = v => { box.checked = v; box.dispatchEvent(new Event('change')); };
                 setTimeout(() => { const off0 = !box.checked && !d.controls.autoRotate;
                   set(true);
                   setTimeout(() => { const on1 = d.controls.autoRotate;
                     set(false);
                     setTimeout(() => res({ off0, on1, off2: !d.controls.autoRotate }), 120);
                   }, 120); }, 120); })"""
        )
        check("auto-spin off at boot, toggles via checkbox",
              bool(spin) and "fail" not in spin
              and all(spin.get(k) for k in ("off0", "on1", "off2")), str(spin))

        # 5d. dead-only toggle frames the dead set. Data-gated: with zero dead
        # files the toggle may legitimately be inert (nothing to frame) — and
        # MUST be toggled back off or every later check measures zeroed
        # instances (all nodes hidden).
        page.evaluate("() => document.getElementById('bReset').click()")
        page.wait_for_timeout(200)
        dead = page.evaluate(
            """() => { const b = document.getElementById('bDead');
                 const nd = window.__dbg.nodes.filter(n => n.dead > 0).length;
                 const d0 = window.__dbg.camera.position.distanceTo(window.__dbg.controls.target);
                 b.click();
                 return new Promise(res => setTimeout(() => {
                   const dd = window.__dbg;
                   const out = { nd,
                                 d0: Math.round(d0),
                                 d1: Math.round(dd.camera.position.distanceTo(dd.controls.target)) };
                   b.click();   // always restore: deadOnly must not leak downstream
                   res(out);
                 }, 600)); }"""
        )
        if dead.get("nd", 0) == 0:
            check("dead only inert with no dead files",
                  abs(dead["d1"] - dead["d0"]) <= 2, str(dead))
        else:
            check("dead only frames dead set",
                  bool(dead) and dead.get("d1", 0) < dead.get("d0", 1) * 0.85,
                  str(dead))

        # 5d2. cycles lens: toggle frames the SCC-member set (madge steal).
        # Data-gated on meta.cycIds; restores state for downstream checks.
        cyc = page.evaluate(
            """() => new Promise(res => { const d = window.__dbg;
                 const b = document.getElementById('bCyc');
                 if (!b) return res({ fail: 'no bCyc' });
                 const nc = (d.meta.cycIds || []).length;
                 if (!nc) return res({ skip: 'no call cycles in this index' });
                 // visible cyc members (some may be tests-filtered)
                 const cycVis = d.nodes.filter(n => n.cyc && d.alphaTgt[d.nodes.indexOf(n)] > 0.5).length;
                 const d0 = d.camera.position.distanceTo(d.controls.target);
                 b.click();
                 setTimeout(() => {
                   let lit = 0;
                   for (let j = 0; j < d.nodes.length; j++)
                     if (d.alphaTgt[j] > 0.5) lit++;
                   const d1 = d.camera.position.distanceTo(d.controls.target);
                   b.click();
                   setTimeout(() => res({ nc, cycVis, lit, d0, d1,
                     off: !document.getElementById('bCyc').classList.contains('on') }), 500);
                 }, 600); })"""
        )
        if cyc and "skip" in cyc:
            print(f"SKIP cycles lens — {cyc.get('skip')}")
        else:
            check("cycles lens frames SCC set",
                  bool(cyc) and "fail" not in cyc
                  and cyc.get("lit") == cyc.get("cycVis")
                  and cyc.get("cycVis", 0) > 0
                  and cyc.get("off"),
                  str(cyc))

        # 5e. ground grid: off by default, button toggles, state survives resetAll
        ground = page.evaluate(
            """() => { const d = window.__dbg;
                 const btn = document.getElementById('bGround');
                 const off0 = !d.groundGrid.visible && !btn.classList.contains('on');
                 btn.click();
                 const on1 = d.groundGrid.visible && btn.classList.contains('on');
                 document.getElementById('bReset').click();
                 const survived = d.groundGrid.visible && btn.classList.contains('on');
                 btn.click();
                 const off2 = !d.groundGrid.visible && !btn.classList.contains('on');
                 return { off0, on1, survived, off2 }; }"""
        )
        check("ground grid toggles and survives reset",
              bool(ground) and all(ground.get(k) for k in ("off0", "on1", "survived", "off2")),
              str(ground))

        # 5f. cluster supernode collapse: members hide, dpos re-targets to
        # centroids, uncollapse restores. Data-gated (needs a 3+ cluster).
        # Membership = VISIBLE members measured pre-click (tests stay hidden
        # through a collapse cycle and must not count against restore).
        sup = page.evaluate(
            """() => new Promise(res => { const d = window.__dbg;
                 const b = document.getElementById('bCollapse');
                 if (!b) return res({ fail: 'no bCollapse' });
                 // baseline: visible members per cluster
                 const vis = new Map();
                 for (let j = 0; j < d.nodes.length; j++) {
                   if (d.alphaTgt[j] <= 0.5) continue;
                   const c = d.nodes[j].cluster;
                   if (c < 0) continue;
                   if (!vis.has(c)) vis.set(c, []);
                   vis.get(c).push(j);
                 }
                 let cid = -1, mems = [];
                 for (const [c, list] of vis)
                   if (list.length >= 3 && list.length > mems.length) { cid = c; mems = list; }
                 if (cid < 0) { b.click(); return res({ skip: 'no 3+ member cluster' }); }
                 b.click();
                 setTimeout(() => {
                   if (!d.supCollapsed.has(cid))
                     { b.click(); return res({ skip: 'chosen cluster not collapsed' }); }
                   const info = d.supCollapsed.get(cid);
                   let memHidden = 0, dposOk = 0;
                   for (const j of mems) {
                     if (d.alphaTgt[j] < 0.05) memHidden++;
                     const dx = d.dpos[j*3] - info.cx, dy = d.dpos[j*3+1] - info.cy,
                           dz = d.dpos[j*3+2] - info.cz;
                     if (Math.sqrt(dx*dx + dy*dy + dz*dz) < 1) dposOk++;
                   }
                   window.__supMems = mems;
                   res({ collapsed: d.collapsed, mems: mems.length,
                         memHidden, dposOk, cx: info.cx, cy: info.cy, cz: info.cz });
                 }, 500); })"""
        )
        if not (sup and "skip" in sup) and sup and "fail" not in sup:
            page.screenshot(path=str(ROOT / "tests" / "qa_collapse.png"), scale="css", type="png")
        sup2 = page.evaluate(
            """() => new Promise(res => { const d = window.__dbg;
                 const b = document.getElementById('bCollapse');
                 if (!d.collapsed) return res({ state: 'already restored' });
                 b.click();
                 setTimeout(() => {
                   let restored = 0;
                   for (const j of (window.__supMems || []))
                     if (d.alphaTgt[j] > 0.5) restored++;
                   res({ collapsed2: d.collapsed, restored,
                         want: (window.__supMems || []).length });
                 }, 500); })"""
        )
        if sup and "skip" in sup:
            print(f"SKIP supernode collapse — {sup.get('skip')}")
        else:
            check("supernode collapse re-targets and restores",
                  bool(sup) and "fail" not in sup
                  and sup.get("collapsed") and not sup2.get("collapsed2")
                  and sup.get("memHidden") == sup.get("mems")
                  and sup.get("dposOk") == sup.get("mems")
                  and sup2.get("restored") == sup2.get("want"),
                  f"{sup} / {sup2}")

        # 6. git-churn channel: DATA.hot normalized 0..1, size boost applied
        # to the hottest file, cold files untouched, caption notes the channel.
        # The dead-only cycle above zeroed alphaTgt for a moment; alphaArr
        # eases back slowly, so wait until every node that SHOULD be visible
        # has finished easing — otherwise matrices measure as scale-0 (flaky).
        page.wait_for_function(
            """() => { const d = window.__dbg;
                    for (let i = 0; i < d.nodes.length; i++)
                      if (d.alphaTgt[i] > 0.5 && d.alpha[i] <= 0.5) return false;
                    return true; }""", timeout=20000)
        churn = page.evaluate(
            """() => { const d = window.__dbg;
                 const h = d.hot;
                 if (!h) return { fail: 'no DATA.hot' };
                 let arg = 0;
                 for (let j = 1; j < h.length; j++) if (h[j] > h[arg]) arg = j;
                 let cold = -1;
                 for (let j = 0; j < h.length; j++)
                   if (h[j] === 0 && d.alphaTgt[j] > 0.5) { cold = j; break; }
                 // degree = sum of link weights (same recurrence as sizes)
                 const deg = new Float32Array(d.nodes.length);
                 d.links.forEach(l => { deg[l.s] += l.w; deg[l.t] += l.w; });
                  const base = i => Math.min(12, 4.5 + Math.sqrt(deg[i]));
                  const mat = new d.THREE.Matrix4();
                  const mx = i => { d.fileMesh.getMatrixAt(i, mat); return mat.elements[0]; };
                  // instance scale = sizes*1.1 × dim-shrink (0.45+0.55a) at
                  // overview (no dead boost/hover); alpha eases, read it live
                  const dim = i => 0.45 + 0.55 * d.alpha[i];
                  const expHot = base(arg) * 1.1 * (1 + 0.35 * h[arg]) * dim(arg);
                  return { n: h.length, max: h[arg], min: Math.min(...h),
                          hotOk: Math.abs(mx(arg) / expHot - 1) < 0.02,
                          // data-gated: an index where every file has churn
                          // (young repo, all touched recently) has no cold file
                          coldOk: cold < 0 ? true
                                  : Math.abs(mx(cold) / (base(cold) * 1.1 * dim(cold)) - 1) < 0.02,
                          noted: document.getElementById('caption').textContent.includes('churn') }; }"""
        )
        check("git churn sizes hottest file and notes caption",
              bool(churn) and "fail" not in churn
              and churn.get("n") == page.evaluate("() => window.__dbg.nodes.length")
              and 0 <= churn.get("min", -1) and 0 < churn.get("max", 0) <= 1
              and churn.get("hotOk") and churn.get("coldOk") and churn.get("noted"),
              str(churn))

        # 7. groups toggle: supergroup recoloring + legend swap + reset
        grp = page.evaluate(
            """() => { const d = window.__dbg;
                 const btn = document.getElementById('bGroups');
                 if (btn.style.display === 'none') return { skip: 'no groups channel' };
                 const chips = () => document.querySelectorAll('#legend .chip');
                 const fineN = chips().length;
                 if (fineN <= 3) return { skip: 'too few fine clusters to supergroup' };
                 let idx = -1;
                 // pick a clustered node whose fine vs group hues differ
                 // enough that recoloring is measurable in RGB
                 const hue = c => c < 0 ? 0.08 : (c * 0.61803398875 + 0.55) % 1;
                 for (let j = 0; j < d.nodes.length; j++) {
                   const nd = d.nodes[j];
                   if (nd.cluster < 0 || nd.gid < 0 || d.alphaTgt[j] <= 0.5) continue;
                   let dh = Math.abs(hue(nd.gid) - hue(nd.cluster));
                   dh = Math.min(dh, 1 - dh);
                   if (dh > 0.15 && nd.dead <= 0) { idx = j; break; }
                 }
                 if (idx < 0) return { fail: 'no recolorable node' };
                 const cArr = d.fileMesh.instanceColor.array;
                 const c0 = [cArr[idx*3], cArr[idx*3+1], cArr[idx*3+2]];
                 btn.click();
                 return new Promise(res => setTimeout(() => {
                   const c1 = [cArr[idx*3], cArr[idx*3+1], cArr[idx*3+2]];
                   const groupN = chips().length;
                   const firstChip = chips()[0].textContent;
                   const mode1 = d.groupsMode;
                   document.getElementById('bReset').click();
                   setTimeout(() => {
                     res({ fineN, groupN, mode1, firstChip,
                           recolored: Math.max(...c0.map((v, k) => Math.abs(v - c1[k]))) > 0.02,
                           modeAfterReset: d.groupsMode,
                           restoredN: chips().length });
                   }, 150);
                 }, 250)); }"""
        )
        if grp and "skip" in grp:
            print(f"SKIP groups toggle — {grp.get('skip')}")
        else:
            check("groups toggle recolors and swaps legend",
                  bool(grp) and "fail" not in grp
                  and grp.get("mode1") and not grp.get("modeAfterReset")
                  and grp.get("groupN", 99) < grp.get("fineN", 0)
                  and grp.get("restoredN") == grp.get("fineN")
                  and grp.get("recolored"),
                  str(grp))

        # 8. map pane (map-spec-v2): named wires over fn rosters. Data-gated
        # on DATA.mwires — an index without the named-wire exports skips.
        mw = page.evaluate("() => window.__dbg.mwires || []")
        if not mw:
            print("SKIP map pane — no DATA.mwires in this index")
        else:
            page.fill("#search", tok)
            page.dispatch_event("#search", "input")
            page.wait_for_timeout(600)
            page.evaluate("() => document.getElementById('bMap').click()")
            page.wait_for_timeout(500)   # rAF-coalesced paint
            minfo = page.evaluate("() => window.__dbg.mapInfo()")
            check("map named wires drawn",
                  bool(minfo) and minfo.get("wires", 0) > 0, str(minfo))
            check("map roster rows rendered",
                  bool(minfo) and minfo.get("rosterRows", 0) > 0, str(minfo))
            # bundle chips need an admitted pair carrying 2+ same-type wires
            has_mult = page.evaluate(
                """() => { const d = window.__dbg; const cnt = new Map();
                     (d.mwires || []).forEach(w => {
                       if (w[0] === 'var') return;
                       const k = w[1] + '_' + w[3];
                       cnt.set(k, (cnt.get(k) || 0) + 1); });
                     for (const v of cnt.values()) if (v >= 2) return true;
                     return false; }""")
            if has_mult:
                check("map bundle chips present",
                      bool(minfo) and minfo.get("chips", 0) > 0, str(minfo))
            else:
                print("SKIP map bundle chips — no multi-wire pair in this index")
            # label ladder: per-target budget 2 holds and something is shown
            check("map label ladder budget",
                  bool(minfo) and minfo.get("budgetOk")
                  and minfo.get("labels", 1) <= 2 * max(1, minfo.get("labelTargets", 0)),
                  str(minfo))
            check("map labels shown", bool(minfo) and minfo.get("shownLabels", 0) > 0,
                  str(minfo))
            # vars chip: off at boot, toggles via a real click on its rect
            check("map vars chip default off",
                  page.evaluate("() => !window.__dbg.mapVars"))
            bb = page.evaluate(
                "() => document.getElementById('mapPane').getBoundingClientRect()")
            page.mouse.click(bb["x"] + 31, bb["y"] + 34)   # pane-local (31,34)
            page.wait_for_timeout(300)
            check("map vars chip toggles on",
                  page.evaluate("() => window.__dbg.mapVars"))
            page.mouse.click(bb["x"] + 31, bb["y"] + 34)
            page.wait_for_timeout(300)
            # L2: click the first named wire -> showFnInfo panel (CALLED BY
            # section), full caller list, no "+N more hidden" 24-cap
            probe = page.evaluate("() => window.__dbg.mapInfo().probeWire")
            if probe:
                bb = page.evaluate(
                    "() => document.getElementById('mapPane').getBoundingClientRect()")
                page.mouse.click(bb["x"] + probe["sx"], bb["y"] + probe["sy"])
                page.wait_for_timeout(300)
                fn_info = page.evaluate(
                    """() => ({ open: document.getElementById('info').style.display === 'block',
                         title: document.getElementById('iTitle').textContent })""")
                check("map wire click opens fn panel",
                      fn_info["open"] and fn_info["title"].endswith("()"), str(fn_info))
                check("fn panel lists all callers (no 24-cap)",
                      page.locator("#iUsedBy li.more").count() == 0)
            else:
                print("SKIP map wire click — no probeable wire")
            # corridor trunk consolidation (declutter): corridors spanning
            # the same chunk-row hop share one trunk — distinct stroked
            # corridor polylines must drop below the admitted corridor
            # count. Data-gated: E>12 AND same-hop groups present in this
            # index/focus (fit layout, after the probe above — a wire
            # click opens the info panel without relayout).
            zinfo = page.evaluate("() => window.__dbg.mapInfo()")
            if zinfo and zinfo.get("E", 0) > 12 and zinfo.get("trunkGroups", 0) > 0:
                check("map trunk consolidation reduces drawn polylines",
                      zinfo.get("spinesDrawn", 0) < zinfo.get("spineTotal", 0)
                      and zinfo.get("drawnPolys", 0) <
                          zinfo.get("spineTotal", 0) + zinfo.get("wires", 0),
                      str(zinfo))
            else:
                print(f"SKIP map trunk consolidation — {zinfo}")
            page.screenshot(path=str(ROOT / "tests" / "qa_map.png"), scale="css", type="png")
            print("artifact: tests/qa_map.png")
            page.evaluate("() => document.getElementById('bMap').click()")  # close pane
            page.keyboard.press("Escape")

        # artifact: screenshot of the focused fn-layer state
        page.screenshot(path=str(ROOT / "tests" / "last_run.png"), scale="css", type="png")
        print("artifact: tests/last_run.png")
        browser.close()

    print(f"\n{n_files} files indexed · {len(FAILURES)} failure(s)")
    if FAILURES:
        print("FAILED:", ", ".join(FAILURES))
        sys.exit(1)
    print("ALL TESTS PASS")


if __name__ == "__main__":
    main()


