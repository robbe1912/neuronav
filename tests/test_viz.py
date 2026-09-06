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
                   return Math.abs(arr[b] - d.pos[s*3]) + Math.abs(arr[b+93] - d.pos[t*3]);
                 }
                 return -1; }""")
        check("hidden-test arcs collapse geometrically",
              arc_hidden == 0 and (arc_shown < 0 or 2 <= arc_shown <= 15.5),
              f"hidden span {arc_hidden} / shown endpoint err {arc_shown} (surface-trimmed ~sizes*1.1+2)")


        # 4. focus + functions: fn boxes exist and sit ON the wires.
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
        wire = page.evaluate(
            """() => { const d = window.__dbg; const nm = d.fnMesh, meta = d.fnMeta;
                 if (!nm || !meta || !meta.length) return { fail: 'no fn layer' };
                 const pos = d.pos, adj = {};
                 d.links.forEach(l => { (adj[l.s]=adj[l.s]||new Set()).add(l.t);
                                        (adj[l.t]=adj[l.t]||new Set()).add(l.s); });
                 let maxOff = 0;
                 for (const fm of meta) {
                   const A = fm.file, p = fm.p; let best = Infinity;
                   for (const B of (adj[A]||[])) {
                     const ax=pos[A*3],ay=pos[A*3+1],az=pos[A*3+2];
                     const bx=pos[B*3],by=pos[B*3+1],bz=pos[B*3+2];
                     const abx=bx-ax,aby=by-ay,abz=bz-az;
                     const apx=p[0]-ax,apy=p[1]-ay,apz=p[2]-az;
                     const ab2=abx*abx+aby*aby+abz*abz;
                     let tt=ab2>0?(apx*abx+apy*aby+apz*abz)/ab2:0;
                     tt=Math.max(0,Math.min(1,tt));
                     const dx=apx-abx*tt,dy=apy-aby*tt,dz=apz-abz*tt;
                     best=Math.min(best,Math.sqrt(dx*dx+dy*dy+dz*dz));
                   }
                   maxOff=Math.max(maxOff,best);
                 }
                 return { count: nm.count, geom: nm.geometry.type,
                          isInstanced: nm.isInstancedMesh, maxOff }; }"""
        )
        check("fn boxes instanced cubes",
              wire.get("isInstanced") and wire.get("geom") == "BoxGeometry", str(wire))
        check("fn boxes on wires", wire.get("maxOff", 99) <= 15.5,
              f"maxOff={wire.get('maxOff')} count={wire.get('count')}")

        # 5. hover a fn box -> tooltip shows path :: name
        hover = page.evaluate(
            """() => { const d = window.__dbg; const fm = d.fnMeta[0]; if (!fm) return null;
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
            """() => { const d = window.__dbg; const fm = d.fnMeta[0]; if (!fm) return null;
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
        # (auto-spin off first: rotation moves the projection we aim at)
        page.evaluate("() => document.getElementById('cbSpin').click()")
        page.wait_for_timeout(150)
        scale = page.evaluate(
            """() => { const d = window.__dbg;
                 const mat = new d.THREE.Matrix4();
                 const mx = i => { d.fileMesh.getMatrixAt(i, mat); return mat.elements[0]; };
                 const move = (x, y) => d.renderer.domElement.dispatchEvent(
                   new PointerEvent('pointermove', { clientX: x, clientY: y, bubbles: true }));
                 // reuse the still-hovered neighbor from 5b (or any lit node)
                 let idx = d.hovered;
                 if (idx < 0) { for (let j = 0; j < d.nodes.length; j++)
                   if (d.alphaTgt[j] > 0.5) { idx = j; break; } }
                 if (idx < 0) return { fail: 'no lit node' };
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
                     // 3) off again -> decays back to base
                     move(-500, -500);
                     setTimeout(() => res({ before, grown, after: mx(idx),
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

        # 5ec. auto-spin: checkbox is the truth (tick re-derives autoRotate
        # every frame — reads need a settle after each click). Harness left
        # spin OFF at 5bb, so: on -> verify -> off -> verify -> back on.
        spin = page.evaluate(
            """() => new Promise(res => { const d = window.__dbg;
                 const box = document.getElementById('cbSpin');
                 if (!box) return res({ fail: 'no cbSpin' });
                 const set = v => { box.checked = v; box.dispatchEvent(new Event('change')); };
                 set(true);
                 setTimeout(() => { const on0 = d.controls.autoRotate;
                   set(false);
                   setTimeout(() => { const off1 = !d.controls.autoRotate;
                     set(true);
                     setTimeout(() => res({ on0, off1, on2: d.controls.autoRotate }), 120);
                   }, 120); }, 120); })"""
        )
        check("auto-spin toggles via checkbox",
              bool(spin) and "fail" not in spin
              and all(spin.get(k) for k in ("on0", "off1", "on2")), str(spin))

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
                  const base = i => Math.min(10, 3.5 + Math.sqrt(deg[i]));
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


