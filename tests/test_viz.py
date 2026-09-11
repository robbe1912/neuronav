# neuronav viz QA harness — drives the real page in headless Chrome.
# Run: .venv/Scripts/python.exe -X utf8 tests/test_viz.py  (exit 0 = all pass)
# Uses system Chrome via channel="chrome" (no browser download needed).
import http.server
import socketserver
import threading
import sys
import re
from functools import partial
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / ".tmp" / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
import nav  # noqa: E402  (the bake lives in the active config's state dir)

PORT = 8931
FAILURES = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"{tag} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def main():
    handler = partial(http.server.SimpleHTTPRequestHandler,
                      directory=str(nav.STATE_DIR))  # bake is per-project now
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

        # 1c. boot-state LOD law: the default view gates the whole bus
        # tier — junction bollards and trunk conduits render ONLY when
        # their served boxes are resolvable (viewport-fraction floors).
        # The boot camera must show no droplets, no open-ended buses
        # (the round-5 user report, now pinned).
        lod0 = page.evaluate(
            """() => { const d = window.__dbg;
                 return d.fnLod ? { b: d.fnLod.bollardsShown,
                                    c: d.fnLod.conduitsShown,
                                    ch: d.fnLod.chevShown,
                                    mch: d.fnLod.minChevPx } : null; }"""
        )
        check("fnLod exposed in __dbg", bool(lod0), "boot read")
        check("boot view gates bus tier (no droplets, no open buses)",
              lod0 and lod0["b"] == 0 and lod0["c"] == 0, str(lod0))
        check("boot chevrons meet the 8px floor or hide",
              lod0 and (lod0["ch"] == 0 or lod0["mch"] >= 8), str(lod0))

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
                    // both endpoints are surface-trimmed (rendered radius
                    // + 2 margin): expected total offset = trim_s + trim_t
                    // (sphR = rendered radius incl spread + fn-sat boost)
                    const exp = d.sphR(s) + 2 + d.sphR(t) + 2;
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
        # path stem — hardcoded target stems died with config profiles.
        # focus target: highest-degree node carrying at least one
        # DEFAULT-VISIBLE link (call/signal - inst is toggled off at boot).
        # A .tscn hub's every link can be inst, which lights a focus with
        # zero budgetable wires; the fn-tier pins need a wired hub.
        tok_info = page.evaluate(
            """() => { const d = window.__dbg;
                 const wd = new Array(d.nodes.length).fill(0);
                 for (const l of d.links)
                   if (l.ty === 'call' || l.ty === 'signal')
                     { wd[l.s]++; wd[l.t]++; }
                 let best = 0;
                 for (let i = 1; i < d.nodes.length; i++)
                   if (wd[i] > wd[best]) best = i;
                 const path = d.nodes[best].path;
                 return { path,
                          tok: path.split('/').pop().replace(/\\.[^.]+$/, '').toLowerCase() }; }"""
        )
        tok, best_path = tok_info["tok"], tok_info["path"]

        def enter_focus_via_row():
            """Issue #33 contract: focus is entered ONLY by a click - typing
            highlights in place, a results-row click is that click."""
            page.fill("#search", tok)
            page.dispatch_event("#search", "input")
            page.wait_for_timeout(600)
            page.evaluate(
                """(p) => { const rows = [...document.querySelectorAll('#searchResults .row')];
                     // file rows carry title=path; rows bind onpointerdown
                     const r = rows.find(x => x.getAttribute('title') === p)
                            || rows[0];
                     r.dispatchEvent(new PointerEvent('pointerdown',
                                                      { bubbles: true })); }""",
                best_path)
            page.wait_for_timeout(1200)
            # depth escalation (#33 contract): the slider is respected, and
            # the fn tier renders the wires that exist within depth N. A
            # depth-1 ball can be wire-free; step the slider 1->2->3 until
            # cross-file fn wires light (bounded, deterministic).
            # converge on a state with both fn wires AND the serve gate
            # on: serveAll needs the camera inside 2.2 ball radii, and only
            # a click re-frames the camera after the ball grows
            for dv in (2, 3):
                st = page.evaluate(
                    """() => { const d = window.__dbg;
                         let n = 0;
                         for (const e of d.fedges)
                           if (d.alphaTgt[e[0]] > 0.5 && d.alphaTgt[e[2]] > 0.5) n++;
                         return { fw: n, serve: !!d.lodServe }; }""")
                if st["fw"] > 0 and st["serve"]:
                    break
                page.evaluate(
                    """(v) => { const el = document.getElementById('depth');
                         el.value = v; el.dispatchEvent(new Event('input')); }""", dv)
                page.wait_for_timeout(1200)
                page.fill("#search", tok)
                page.dispatch_event("#search", "input")
                page.wait_for_timeout(400)
                page.evaluate(
                    """(p) => { const rows = [...document.querySelectorAll('#searchResults .row')];
                         const r = rows.find(x => x.getAttribute('title') === p) || rows[0];
                         r.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })); }""",
                    best_path)
                page.wait_for_timeout(1200)

        # 3z. highlight-only search (issue #33): typing must NOT change the
        # 3D view - no focus, no camera tween, no fn tier from the keyboard.
        lit_pre = page.evaluate(
            "() => window.__dbg.alphaTgt.reduce((s, a) => s + (a > 0.5 ? 1 : 0), 0)")
        page.fill("#search", tok)
        page.dispatch_event("#search", "input")
        page.wait_for_timeout(600)
        inert = page.evaluate(
            """() => { const d = window.__dbg;
                 let hl = 0; const a = d.hlArr || [];
                 for (let i = 0; i < a.length; i++) if (a[i] > 0) hl++;
                 const lit = d.alphaTgt.reduce((s, x) => s + (x > 0.5 ? 1 : 0), 0);
                 const rows = document.querySelectorAll('#searchResults .row').length;
                 const fn = d.fnMesh ? d.fnMesh.count : 0;
                 return { hl, lit, rows, fn, camTween: !!d.camTween,
                          focus: d.focusFileIdx }; }"""
        )
        check("typing is 3D-inert (no focus, no camera tween)",
              inert["focus"] < 0 and not inert["camTween"]
              and inert["lit"] == lit_pre,   # lit set untouched by typing
              f"pre {lit_pre} / post {inert['lit']} (base {lit_base})")
        check("typing highlights matches in place",
              inert["hl"] > 0, str(inert))
        hl_style = page.evaluate(
            """() => { const on = document.querySelector('#hubs .hub.hl'),
                           off = document.querySelector('#hubs .hub:not(.hl)');
                 if (!(on && off)) return { skip: true };
                 const a = getComputedStyle(on), b = getComputedStyle(off);
                 return { skip: false, on: a.color, off: b.color,
                          bg: a.backgroundColor }; }"""
        )
        check(".hl matches visibly lift (CSS rule, not a dead toggle)",
              not hl_style.get("skip") and hl_style["on"] != hl_style["off"],
              str(hl_style))
        check("typing surfaces a capped results list",
              inert["rows"] > 0, str(inert))
        check("typing leaves overview (no fn tier)",
              inert["fn"] == 0, str(inert))

        # 4. focus + functions - entered by a click (results row), which is
        # the ONLY door into the fn tier under the #33 contract.
        enter_focus_via_row()
        check("row click focuses its node",
              page.evaluate("() => window.__dbg.focusFileIdx") >= 0,
              "focusFileIdx after row click")
        check("focus restores the boot fn-layer default (cbFn checked)",
              page.is_checked("#cbFn"), "cbFn after click-focus")


        # 4-pre. LOD law at default-camera focus (the eighth view):
        # click focus + fn layer on, camera NOT moved — the bus tier
        # must serve here regardless of hub shell radius. A distance-
        # only gate passed this on topology luck (close-shelled hubs
        # serve, far-shelled gate); focus-state keying makes it law.
        lodf = page.evaluate(
            """() => { const d = window.__dbg;
                 return d.fnLod ? { b: d.fnLod.bollardsShown,
                                    c: d.fnLod.conduitsShown,
                                    ch: d.fnLod.chevShown,
                                    cnt: d.fnArrows ? d.fnArrows.count : 0,
                                    msb: d.fnLod.minServedBoxPx,
                                    mch: d.fnLod.minChevPx,
                                    od: d.fnLod.oDot, oa: d.fnLod.oArrow,
                                    serve: !!d.lodServe } : null; }"""
        )
        check("focus at default camera serves the bus tier",
              lodf and lodf["b"] > 0 and lodf["c"] > 0 and lodf["msb"] > 0, str(lodf))
        check("default-cam chevrons at size or hidden",
              lodf and (lodf["ch"] == 0 or lodf["mch"] >= 8), str(lodf))
        # issue #6: chevObs = chevShown / carried marks (fnArrows.count
        # after consolidation) must clear 0.75 at a serving view --
        # low-LOD files consolidate to one mark and buried boxes ride
        # their delivery leg, so every mark a view carries is one the
        # user can actually see (or the view hides chevrons entirely)
        check("default-cam chevObs >= 0.75 (carried marks observable)",
              lodf and (lodf["cnt"] == 0
                        or lodf["ch"] / lodf["cnt"] >= 0.75), str(lodf))
        # paint-full law holds when the focus-state serve gate is ON; with
        # the gate off the distance fade IS the design (far-zoom noise
        # control), so only assert it in the gated-on state
        check("served layer at full legibility (opacity floors)",
              lodf and (not lodf["serve"]
                        or (lodf["od"] >= 0.8 and lodf["oa"] >= 0.8)), str(lodf))

        # 4-pre3. 3D vocabulary legend (user r5: the visual language
        # explained itself nowhere): '?' chip toggles one-line legend.
        lg = page.evaluate(
            """() => { const d = window.__dbg;
                 const chip = document.getElementById('lg3d');
                 const x = document.getElementById('lg3dx');
                 if (!chip || !x) return null;
                 chip.click();
                 const open = x.style.display === 'flex' && d.legendOpen === true;
                 const txt = (x.textContent || '').slice(0, 60);
                 chip.click();
                 const closed = x.style.display === 'none' && d.legendOpen === false;
                 return { open, closed, txt }; }"""
        )
        check("legend chip toggles (trunk vocabulary self-explains)",
              lg and lg["open"] and lg["closed"], str(lg))
        vic = page.evaluate(
            """() => { const d = window.__dbg; const nm = d.fnMesh, meta = d.fnMeta;
                 if (!nm || !meta || !meta.length) return { fail: 'no fn layer' };
                 const pos = d.pos, sizes = d.sizes;
                 let maxOff = 0;
                 for (const fm of meta) {
                    const A = fm.file, p = fm.p;
                    const dx = p[0]-pos[A*3], dy = p[1]-pos[A*3+1], dz = p[2]-pos[A*3+2];
                    // owner-vicinity: boxes sit on arc rings of radius
                    // oR + 14 + ring*8 (oR = rendered sphere radius incl
                    // spread + fn-satellite boost via d.sphR) — at most
                    // 24+ past the rendered surface
                    maxOff = Math.max(maxOff, Math.sqrt(dx*dx+dy*dy+dz*dz) - d.sphR(A));
                 }
                 return { count: nm.count, geom: nm.geometry.type,
                          isInstanced: nm.isInstancedMesh, maxOff }; }"""
        )
        check("fn boxes instanced cubes",
              vic.get("isInstanced") and vic.get("geom") == "BoxGeometry", str(vic))
        check("fn boxes orbit owner sphere", vic.get("maxOff", 99) <= 60,
              f"maxOff={vic.get('maxOff')} count={vic.get('count')}"
              f" (rings deepen with tier mass: 14 + 8*ring)")

        # 4b. hub budget: focusing the highest-degree node lights at most
        # HUB_EDGE_BUDGET (12) links; they render as the curved arc overlay
        # (bucket straight segments go black underneath), every other
        # visible link drops to ghost ink (brightness <= 0.12). Hover
        # bypass starts inactive (no pointer).
        hb = page.evaluate(
            """() => { const d = window.__dbg;
                 const lit = d.budgetLit ? [...d.budgetLit] : [];
                 const bright = i => {
                   const ib = d.bucketColIB[d.bucketOf[i]];
                   const s = (d.hwSlot[i] >= 0 ? d.hwSlot[i] : d.slotOf[i] * 6);
                   return Math.max(ib.array[s], ib.array[s+1], ib.array[s+2]);
                 };
                 let leaks = 0, worst = 0;
                 for (let i = 0; i < d.links.length; i++) {
                   if (lit.includes(i)) continue;
                   const b = bright(i);
                   if (b > 0.12) leaks++;
                   worst = Math.max(worst, b);
                 }
                 // budget wires draw as arcs — the overlay must exist and
                 // carry one arc (28 verts) per budget link. The r6 C2.2
                 // affordance (top-8 dim arcs revealing a zero-wire .tscn
                 // hub's connectedness) shares this overlay: surplus arcs
                 // are sanctioned ONLY when the focus node is a .tscn and
                 // are hard-bounded at 10 (9 observed) so runaway arc
                 // spawning still trips the pin.
                 const fa = d.focusArcRef;
                 const arcN = fa && fa.lines.visible
                   ? fa.lines.geometry.attributes.instanceStart.count / 14 : 0;
                 const isTscn = d.fnLod && d.fnLod.serveFi >= 0 &&
                   /\\.tscn$/.test(d.nodes[d.fnLod.serveFi].path || "");
                 return { lit: lit.length, arcN, isTscn, leaks, worst }; }"""
        )
        check("hub budget: <= 12 lit edges on focus",
              hb and hb["lit"] <= 12 and hb["arcN"] >= hb["lit"] and
              (hb["arcN"] == hb["lit"] or
               (hb["isTscn"] and hb["arcN"] - hb["lit"] <= 10)), str(hb))
        check("hub budget: ghost links <= 0.12 brightness",
              hb and hb["leaks"] == 0, str(hb))

        # 4c. fn layer: UNCAPPED between lit files (focus-neighborhood
        # amendment — every fn interconnection between lit files renders;
        # HUB_FN_BUDGET retired). Tier law: wires touching a level-0 focus
        # subject draw bright (fnLines), neighbor↔neighbor wires draw as
        # quiet ink (fnQuiet) — all present, none deleted. 16 verts per
        # wire (8-segment arc, per-wire lift). The focused hub also
        # carries the additive halo ring.
        fnb = page.evaluate(
            """() => { const d = window.__dbg;
                 const wires = d.fnLines ?
                     d.fnLines.geometry.attributes.instanceStart.count / 8 : 0;
                 const quiet = d.fnQuiet ?
                     d.fnQuiet.geometry.attributes.instanceStart.count / 8 : 0;
                 const lit = new Set();
                 let hub = -1;
                 for (let i = 0; i < d.level.length; i++) {
                   if (d.level[i] >= 0) lit.add(i);   // any focused depth
                   if (d.level[i] === 0 && hub < 0) hub = i;
                 }
                 let bright = 0, silent = 0;
                 (d.fedges || []).forEach(e => {
                   if (lit.has(e[0]) && lit.has(e[2]) &&
                       d.alphaTgt[e[0]] > 0.5 && d.alphaTgt[e[2]] > 0.5) {
                     // render rule: either endpoint a focus subject (level 0)
                     if (d.level[e[0]] === 0 || d.level[e[2]] === 0) bright++;
                     else silent++;
                   } });
                  return { wires, quiet, bright, silent, trunks: d.fnTrunkN,
                           trunkW: d.fnTrunkW, jstub: d.fnJstubN,
                           qN: d.fnQuietTrunkN, qW: d.fnQuietTrunkW,
                           litFiles: lit.size, hub,
                           ring: !!(d.hubRing && d.hubRing.visible) }; }"""
        )
        check("fn layer: hub wires bright, neighbors quiet, none dropped",
              fnb["wires"] == fnb["bright"] + fnb["trunkW"] + fnb["trunks"] + fnb["jstub"] and
              fnb["quiet"] == fnb["silent"] + fnb["qW"] + fnb["qN"] and
              fnb["bright"] + fnb["silent"] > 0, str(fnb))
        check("hub ring marks the focused hub", fnb["ring"], str(fnb))

        # 4c-ter. LOD laws at focus (the densest state): the bus tier
        # MUST read here — bollards and conduits shown, every served
        # box resolvable, chevrons at size. Gating is a pure camera-pose
        # function; the focus camera settles before this read.
        lod1 = page.evaluate(
            """() => { const d = window.__dbg;
                 return d.fnLod ? { b: d.fnLod.bollardsShown,
                                    c: d.fnLod.conduitsShown,
                                    ch: d.fnLod.chevShown,
                                    msb: d.fnLod.minServedBoxPx,
                                    mch: d.fnLod.minChevPx } : null; }"""
        )
        check("focus shows the bus tier (no over-gate)",
              lod1 and lod1["b"] > 0 and lod1["c"] > 0, str(lod1))
        check("focus served boxes resolvable (>2 ref-px)",
              lod1 and lod1["msb"] > 2, str(lod1))
        check("focus chevrons at size (>=8 ref-px)",
              lod1 and lod1["ch"] > 0 and lod1["mch"] >= 8, str(lod1))

        # 4c-quater. corridor-complete law (r7): the unit of render is
        # the full path node->leg->station->trunk->station->leg->node.
        # Every served leg anchors at >= ANCHOR_PX (8 ref-px diameter,
        # boost included), every served chain carries both anchor
        # registrations, and no station keeps a fan without its trunk
        # (the cut-bridge regression class: legs>0 && trunks==0). The
        # fi365-class ramp bridges (trunks>0, legs==0) are legitimate
        # no-fan topology, not violations.
        cor = page.evaluate(
            """() => { const d = window.__dbg;
                 const c = d.corridorCensus, px = d.legAnchorPx;
                 if (!c || !px) return null;
                 const served = c.chains.filter(x => x.served);
                 const noanch = served.filter(x => !x.anchorA || !x.anchorB);
                 const cut = c.stations.filter(s => s.legs > 0 && s.trunks < 1);
                 const spx = px.filter(p => p.alpha >= 0.5);
                 return { legs: served.filter(x => x.kind === "leg").length,
                          trunks: served.filter(x => x.kind === "trunk").length,
                          minPx: spx.length ? Math.min(...spx.map(p => p.px)) : null,
                          noAnchor: noanch.length, noAnchorSample:
                            noanch.slice(0, 3).map(x => x.k),
                          cutStations: cut.length }; }"""
        )
        check("corridor law: legs anchored >= 8 ref-px at focus",
              cor and cor["legs"] >= 8 and cor["trunks"] >= 6 and
              cor["minPx"] is not None and cor["minPx"] >= 8, str(cor))
        check("corridor law: served chains carry both anchors",
              cor and cor["noAnchor"] == 0, str(cor))
        check("corridor law: no fan without its trunk",
              cor and cor["cutStations"] == 0, str(cor))

        # 4c-quinquies. zero-gap attachment law (r8): every served chain
        # seam vertex EQUALS its anchor marker position (===, no tolerance
        # — user ruling: near-enough reads as not-touching). Legs land on
        # sub-junction dots + station centers; trunks on station centers.
        zg = page.evaluate(
            """() => { const d = window.__dbg;
          const fa = d.fnBus ? d.fnBus.instanceMatrix.array : [];
          if (!d.busPts || !d.fnStations) return { err: "no-layer" };
          const served = [];
          for (let i = 0; i < d.busPts.length; i++) {
            if (Math.hypot(fa[i*16], fa[i*16+1], fa[i*16+2]) > 0.001) served.push(d.busPts[i]);
          }
          const first = new Map(), last = new Map();
          for (const s of served) {
            if (!first.has(s.k)) first.set(s.k, s);
            last.set(s.k, s);
          }
          const stByFi = new Map();
          for (const S of d.fnStations) {
            if (!stByFi.has(S.fi)) stByFi.set(S.fi, []);
            stByFi.get(S.fi).push(S);
          }
          const eq = (p, q) => p[0] === q[0] && p[1] === q[1] && p[2] === q[2];
          const bad = [];
          let legN = 0, trunkN = 0;
          for (const [k, s0] of first) {
            const s1 = last.get(k);
            if (k.charCodeAt(0) === 76) {
              legN++;
              const pp = k.split("|");
              const S = d.fnStations.find(x => x.id === +pp[2]);
              if (!S) { bad.push(k + ":no-station"); continue; }
              const sp = S.subJ[+pp[3]];
              if (sp && !eq(s0.a, sp)) bad.push(k + ":a!=subJ");
              if (!eq(s1.b, S.p)) bad.push(k + ":b!=station");
            } else {
              trunkN++;
              const pp = k.split(">");
              if (!(stByFi.get(+pp[0]) || []).some(S => eq(S.p, s0.a))) bad.push(k + ":a!=st");
              if (!(stByFi.get(+pp[1]) || []).some(S => eq(S.p, s1.b))) bad.push(k + ":b!=st");
            }
          }
          return { legN, trunkN, bad }; }"""
        )
        check("zero-gap law: chain vertices equal anchor markers exactly",
              zg and zg.get("legN", 0) + zg.get("trunkN", 0) >= 10 and
              not zg.get("bad"), str(zg)[:220])

        # 4c-bis. conduit lane law: bus conduits ALWAYS arc +Y over the
        # fn-box crowd (no -Y dives into the swarm), tiered by trunk
        # emission order so shared corridors separate. Per trunk the
        # apex (max seg.b.y) must clear the straight-line midpoint by
        # >= 0.065 * dist — the r6 fan-terrace lift law (CONDUIT_LIFT_BASE
        # 0.22 -> 0.14) deliberately trades apex height for fan separation;
        # the template FLOOR is lift >= 0.11*dist (viz trunk emission), and
        # a quadratic bezier with control at mid+lift arcs to ~half that:
        # ~0.055*dist apex, +slop for the sampled polyline. Flat or diving
        # conduits remain impossible (ratio < 0).
        cond = page.evaluate(
            """() => { const pts = window.__dbg.busPts;
                 if (!pts || !pts.length) return { trunks: 0, bad: [] };
                 const gates = new Map(window.__dbg.chainGates);
                 const byK = new Map();
                 pts.forEach(s => {
                   if (gates.get(s.k) !== "served") return;   // inventory only
                   if (!byK.has(s.k)) byK.set(s.k, []);
                   byK.get(s.k).push(s); });
                 const bad = [];
                 let n = 0;
                 for (const [k, segs] of byK) {
                   n++;
                   const a0 = segs[0].a, bZ = segs[segs.length - 1].b;
                   const dist = Math.hypot(bZ[0]-a0[0], bZ[1]-a0[1],
                                           bZ[2]-a0[2]) || 1;
                   let apexY = -Infinity;
                   segs.forEach(s => { apexY = Math.max(apexY, s.b[1]); });
                   const need = (a0[1] + bZ[1]) / 2 + 0.05 * dist;
                   if (apexY < need - 1e-6) bad.push({
                     k, apexY: +apexY.toFixed(1), need: +need.toFixed(1),
                     ratio: +((apexY - (a0[1] + bZ[1]) / 2) / dist).toFixed(4),
                   });
                 }
                 return { trunks: n, bad }; }"""
        )
        check("conduits arc over the crowd (+Y, tiered)",
              cond["trunks"] > 0 and len(cond["bad"]) == 0, str(cond))

        # 4d. pin topology: budgeted wires attach at DISTINCT rim points —
        # each budget wire's hub attachment bearing must deviate from the
        # direct center-to-center bearing (Rodrigues fan, ±0.22 rad)
        pins = page.evaluate(
            """() => { const d = window.__dbg;
                 const lit = [...(d.budgetLit || [])];
                 if (lit.length < 3) return { skip: true };
                 const att = i => {
                   const ib = d.bucketPosIB[d.bucketOf[i]];
                   const s = (d.hwSlot[i] >= 0 ? d.hwSlot[i] : d.slotOf[i] * 6);
                   return [ib.array[s], ib.array[s+1], ib.array[s+2]];
                 };
                 const hs = d.links[lit[0]].s;
                 let off = 0, n = 0;
                 for (const i of lit) {
                   const l = d.links[i];
                   if (l.s !== hs && l.t !== hs) continue;
                   n++;
                   const p = att(i);
                   const vx = p[0] - d.pos[hs*3], vy = p[1] - d.pos[hs*3+1],
                         vz = p[2] - d.pos[hs*3+2];
                   const other = l.s === hs ? l.t : l.s;
                   const dx = d.pos[other*3] - d.pos[hs*3],
                         dy = d.pos[other*3+1] - d.pos[hs*3+1],
                         dz = d.pos[other*3+2] - d.pos[hs*3+2];
                   const vl = Math.sqrt(vx*vx + vy*vy + vz*vz) || 1;
                   const dl = Math.sqrt(dx*dx + dy*dy + dz*dz) || 1;
                   const cosA = (vx*dx + vy*dy + vz*dz) / (vl * dl);
                   if (Math.acos(Math.max(-1, Math.min(1, cosA))) > 0.02) off++;
                 }
                 return { n, off }; }"""
        )
        check("pin topology: rim attachments off direct bearing",
              pins.get("skip") or (pins["n"] >= 1 and pins["off"] >= pins["n"] - 2),
              str(pins))

        page.screenshot(path=str(SHOTS / "qa_hubbudget.png"),
                        scale="css", type="png")

        # 5. hover a fn box -> tooltip shows path :: name
        hover = page.evaluate(
            """() => { const d = window.__dbg;
                 // hover a REAL box that projects ON-SCREEN: aggregated
                 // members render scale-0 (no raycast) and a deep focus
                 // ball puts many boxes off-canvas or behind the camera
                 const ok = m => { if (m.agg && !m.count) return false;
                   const v = new d.THREE.Vector3(m.p[0], m.p[1], m.p[2]).project(d.camera);
                   return v.z < 1 && Math.abs(v.x) < 0.95 && Math.abs(v.y) < 0.95; };
                 const fm = d.fnMeta.find(ok) || d.fnMeta[0]; if (!fm) return null;
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
                 // hover a REAL box that projects ON-SCREEN (see 5)
                 const ok = m => { if (m.agg && !m.count) return false;
                   const v = new d.THREE.Vector3(m.p[0], m.p[1], m.p[2]).project(d.camera);
                   return v.z < 1 && Math.abs(v.x) < 0.95 && Math.abs(v.y) < 0.95; };
                 const fm = d.fnMeta.find(ok) || d.fnMeta[0]; if (!fm) return null;
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
        page.screenshot(path=str(SHOTS / "qa_stalk.png"), scale="css", type="png")

        # 5aa. aggregate fn box ('n×') click opens the fn picker over that
        # file's roster (ranked by incident-wire count then name) — NOT the
        # empty-name fn panel. Data-gated: needs a file owning > AGG_MAX (6)
        # wired fns in the focused set.
        pick = page.evaluate(
            """() => { const d = window.__dbg;
                 const onAgg = m => { if (!m.count) return false;
                   const v = new d.THREE.Vector3(m.p[0], m.p[1], m.p[2]).project(d.camera);
                   return v.z < 1 && Math.abs(v.x) < 0.95 && Math.abs(v.y) < 0.95; };
                 const agg = d.fnMeta.find(onAgg);
                 if (!agg) return null;
                 const v = new d.THREE.Vector3(agg.p[0], agg.p[1], agg.p[2]).project(d.camera);
                 const r = d.renderer.domElement.getBoundingClientRect();
                 return { sx: (v.x*0.5+0.5)*r.width + r.left,
                          sy: (-v.y*0.5+0.5)*r.height + r.top,
                          path: d.nodes[agg.file].path, fi: agg.file }; }"""
        )
        if not pick:
            print("SKIP fn picker — no aggregate fn box in this focus")
        else:
            # dispatch on the canvas (harness convention): a real mouse move
            # can be swallowed by DOM labels (.hub pills take pointer events)
            page.evaluate(
                """(s) => { const el = window.__dbg.renderer.domElement;
                     const o = { clientX: s.sx, clientY: s.sy, bubbles: true };
                     el.dispatchEvent(new PointerEvent('pointermove', o));
                     el.dispatchEvent(new PointerEvent('pointerdown', o));
                     el.dispatchEvent(new PointerEvent('pointerup', o));
                     el.dispatchEvent(new MouseEvent('click', o)); }""", pick)
            page.wait_for_timeout(300)
            pk = page.evaluate(
                """(s) => { const d = window.__dbg;
                     const el = document.getElementById('fnPick');
                     if (!el || el.style.display !== 'block') return { fail: 'picker did not open' };
                     const rows = [...el.querySelectorAll('.row')];
                     if (!rows.length) return { fail: 'picker has no rows' };
                     const inc = new Map();
                     (d.mwires || []).forEach(w => {
                       if (w[1] === s.fi) inc.set(w[2], (inc.get(w[2]) || 0) + 1);
                       if (w[3] === s.fi) inc.set(w[4], (inc.get(w[4]) || 0) + 1); });
                     const want = (d.fns[s.path] || [])
                       .map(r => [r[0], r[1], inc.get(r[0]) || 0])
                       .sort((a, b) => (b[2] - a[2]) ||
                         (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
                       .slice(0, 80)
                       .map(r => r[0] + '  :' + r[1]);
                     return { got: rows.map(r => r.textContent), want }; }""", pick)
            check("fn picker opens on aggregate chip click",
                  "fail" not in pk and pk["got"] == pk["want"], str(pk)[:200])
            # ESC while the picker is OPEN: picker-only close, focus survives
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
            check("esc closes fn picker",
                  page.evaluate(
                      "() => document.getElementById('fnPick').style.display") == "none")
            # reopen, then a row click = the same fn panel as that fn box
            # (in-page dispatch: the picker itself must not fight overlays)
            page.evaluate(
                """(s) => { const el = window.__dbg.renderer.domElement;
                     const o = { clientX: s.sx, clientY: s.sy, bubbles: true };
                     el.dispatchEvent(new PointerEvent('pointermove', o));
                     el.dispatchEvent(new PointerEvent('pointerdown', o));
                     el.dispatchEvent(new PointerEvent('pointerup', o));
                     el.dispatchEvent(new MouseEvent('click', o)); }""", pick)
            page.wait_for_timeout(300)
            page.evaluate(
                """() => document.querySelector('#fnPick .row')
                     .dispatchEvent(new MouseEvent('click', { bubbles: true }))""")
            page.wait_for_timeout(300)
            fn_info = page.evaluate(
                """() => ({ open: document.getElementById('info').style.display === 'block',
                     title: document.getElementById('iTitle').textContent })""")
            check("fn picker row opens fn panel",
                  fn_info["open"] and fn_info["title"].endswith("()"), str(fn_info))

        # 5aab. aggregate fn boxes keep sphere clearance like individual
        # boxes: they ride the OUTER arc ring (arcR+12), never tucked against
        # the owner sphere surface.
        sc = page.evaluate(
            """() => { const d = window.__dbg;
                 let aggMin = Infinity, indMin = Infinity;
                 for (const m of d.fnMeta) {
                   const dx = m.p[0]-d.pos[m.file*3], dy = m.p[1]-d.pos[m.file*3+1], dz = m.p[2]-d.pos[m.file*3+2];
                   const clr = Math.sqrt(dx*dx+dy*dy+dz*dz) -
                     d.sizes[m.file] * 1.1 * Math.sqrt(d.spread);
                   if (m.count) aggMin = Math.min(aggMin, clr);
                   else if (!m.agg) indMin = Math.min(indMin, clr);
                 }
                 return { agg: aggMin === Infinity ? -1 : +aggMin.toFixed(1),
                          ind: indMin === Infinity ? -1 : +indMin.toFixed(1) }; }"""
        )
        check("aggregate fn boxes keep sphere clearance",
              sc["agg"] < 0 or (sc["agg"] >= 24 and sc["ind"] >= 13), str(sc))

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
        grey_labs_on = page.evaluate(
            """() => ["hubs", "clabs", "flabs"].map(id =>
                 document.getElementById(id).style.opacity)"""
        )
        page.screenshot(path=str(SHOTS / "qa_grey.png"), scale="css", type="png")
        page.evaluate(
            """() => window.__dbg.renderer.domElement.dispatchEvent(
                 new PointerEvent('pointermove', { clientX: -500, clientY: -500, bubbles: true }))"""
        )
        page.wait_for_timeout(700)
        grey_back = page.evaluate(
            """(s) => { const d = window.__dbg;
                 return { hubTgt: d.alphaTgt[s.hub], farBack: d.alphaTgt[s.far] }; }""",
            grey_setup)
        grey_labs_back = page.evaluate(
            """() => ["hubs", "clabs", "flabs"].map(id =>
                 document.getElementById(id).style.opacity)"""
        )
        check("hover greys non-neighbors",
              bool(grey_setup) and "fail" not in grey_setup
              and grey_on.get("hub", 0) > 0.9
              and grey_on.get("nb", 0) > 0.9
              and grey_on.get("far", 1) <= 0.13
              and grey_back.get("farBack", 0) > 0.9,
              f"on {grey_on} / back {grey_back}")
        check("greyout dims hub/cluster labels and restores",
              grey_labs_on == ["0.25", "0.25", "0.25"]
              and grey_labs_back == ["", "", ""],
              f"on {grey_labs_on} / back {grey_labs_back}")

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
        # 5fb. crosstalk corridor labels must not outlive their arcs: hidden
        # while a collapse is active, back after uncollapse
        xt_state = page.evaluate(
            """() => { const labs = [...document.querySelectorAll('.xtlab')];
                 return { n: labs.length,
                          hidden: labs.every(el => el.style.display === 'none') }; }"""
        )
        if not (sup and "skip" in sup) and sup and "fail" not in sup:
            page.screenshot(path=str(SHOTS / "qa_collapse.png"), scale="css", type="png")
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
        xt_back = page.evaluate(
            """() => { const labs = [...document.querySelectorAll('.xtlab')];
                 return { n: labs.length,
                          shown: labs.filter(el => el.style.display === 'block').length }; }"""
        )
        if sup and "skip" in sup:
            print(f"SKIP supernode collapse — {sup.get('skip')}")
        else:
            check("crosstalk labels hide while collapsed",
                  xt_state["n"] == 0 or xt_state["hidden"], str(xt_state))
            check("crosstalk labels return after uncollapse",
                  xt_back["n"] == 0 or xt_back["shown"] > 0, str(xt_back))
            check("supernode collapse re-targets and restores",
                  bool(sup) and "fail" not in sup
                  and sup.get("collapsed") and not sup2.get("collapsed2")
                  and sup.get("memHidden") == sup.get("mems")
                  and sup.get("dposOk") == sup.get("mems")
                  and sup2.get("restored") == sup2.get("want"),
                  f"{sup} / {sup2}")

        # 5g. wire hover: pointer over an edge names its strongest named wire
        # ('A::sfn -> B::dfn' from DATA.mwires). Data-gated on mwires + an
        # on-screen probeable edge.
        wtip = page.evaluate(
            """() => { const d = window.__dbg;
                 if (!(d.mwires || []).length) return null;
                 const r = d.renderer.domElement.getBoundingClientRect();
                 for (let i = 0; i < d.links.length; i++) {
                   const l = d.links[i];
                   if (d.hwSlot[i] >= 0) continue;   // straight slots only
                   if (d.alphaTgt[l.s] <= 0.5 || d.alphaTgt[l.t] <= 0.5) continue;
                   const arr = d.bucketPosIB[d.bucketOf[i]].array, o = d.slotOf[i] * 6;
                   if (Math.abs(arr[o] - arr[o+3]) + Math.abs(arr[o+1] - arr[o+4]) < 2) continue;
                   const mx = (arr[o] + arr[o+3]) / 2, my = (arr[o+1] + arr[o+4]) / 2, mz = (arr[o+2] + arr[o+5]) / 2;
                   const vm = new d.THREE.Vector3(mx, my, mz).project(d.camera);
                   if (Math.abs(vm.x) > 0.85 || Math.abs(vm.y) > 0.85 || vm.z > 1) continue;
                   const px = p => [(p.x*0.5+0.5)*r.width, (-p.y*0.5+0.5)*r.height];
                   const vs = px(new d.THREE.Vector3(d.pos[l.s*3], d.pos[l.s*3+1], d.pos[l.s*3+2]).project(d.camera));
                   const vt = px(new d.THREE.Vector3(d.pos[l.t*3], d.pos[l.t*3+1], d.pos[l.t*3+2]).project(d.camera));
                    const mp = px(vm);
                    // clear of EVERY lit node (not just endpoints) so a node
                    // tooltip can't win the pick at the wire midpoint
                    let near = false;
                    for (let j = 0; j < d.nodes.length; j++) {
                      if (d.alphaTgt[j] <= 0.5) continue;
                      const pj = px(new d.THREE.Vector3(
                        d.pos[j*3], d.pos[j*3+1], d.pos[j*3+2]).project(d.camera));
                      if (Math.hypot(mp[0]-pj[0], mp[1]-pj[1]) < 40) { near = true; break; }
                    }
                    if (near) continue;
                    // clear of both endpoints so the node tooltip can't win
                    if (Math.hypot(mp[0]-vs[0], mp[1]-vs[1]) < 40 ||
                        Math.hypot(mp[0]-vt[0], mp[1]-vt[1]) < 40) continue;
                   let has = false;
                   (d.mwires || []).forEach(w => {
                     if ((w[1] === l.s && w[3] === l.t) ||
                         (w[1] === l.t && w[3] === l.s)) has = true; });
                   if (!has) continue;
                   // r6: the C2.2 affordance arcs may now legitimately win
                   // picks at former wire sites — only accept candidates
                   // where the product picker resolves THIS link (oracle
                   // stays product semantics, not re-derivation)
                   const pm = d.pickWireMeta({ clientX: mp[0] + r.left,
                                              clientY: mp[1] + r.top });
                   if (!pm || pm.kind !== "link" || pm.li !== i) continue;
                   return { sx: mp[0] + r.left, sy: mp[1] + r.top, s: l.s, t: l.t };
                 }
                 return null; }"""
        )
        if wtip:
            # harness convention: dispatch on the canvas (real mouse moves
            # can be swallowed by DOM labels); park off-canvas first so no
            # node tooltip lingers
            page.evaluate(
                """(s) => { const el = window.__dbg.renderer.domElement;
                     el.dispatchEvent(new PointerEvent('pointermove',
                       { clientX: -500, clientY: -500, bubbles: true }));
                     el.dispatchEvent(new PointerEvent('pointermove',
                       { clientX: s.sx, clientY: s.sy, bubbles: true })); }""",
                wtip)
            page.wait_for_timeout(300)
            # expectation: the product's own picker + strongest-wire sort —
            # the oracle must be the same semantics the handler uses, not a
            # re-derivation (the old raycast-first-hit + deg-only sort could
            # drift from nearest-chord + full tiebreaks)
            wres = page.evaluate(
                """(s) => { const d = window.__dbg, tip = document.getElementById('tip');
                     const m = d.pickWireMeta({ clientX: s.sx, clientY: s.sy });
                     let want = null;
                     if (m && m.kind === "link") {
                       const pair = d.strongPair(d.links[m.li]);
                       if (pair.length)
                         want = d.nodes[pair[0][1]].label + '::' + pair[0][2] +
                           ' \\u2192 ' + d.nodes[pair[0][3]].label + '::' + pair[0][4];
                     }
                     return { shown: tip.style.display === 'block',
                              text: tip.textContent, want }; }""",
                wtip)
            check("wire tooltip on edge hover",
                  wres["shown"] and wres["want"] and wres["text"] == wres["want"],
                  str(wres))
        else:
            print("SKIP wire tooltip — no probeable on-screen wired edge")

        # 6. git-churn channel: DATA.hot normalized 0..1, size boost applied
        # to the hottest file, cold files untouched, caption notes the channel.
        # Overview scale law - leave focus first (#33: Escape returns to the
        # true boot state; the map pane consumes one press when open).
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        check("Escape returns to the overview (focus cleared)",
              page.evaluate("() => window.__dbg.focusFileIdx") < 0
              and page.evaluate("() => window.__dbg.fnMesh") is None,
              "focus/fn layer after Escape")

        # 4t. scene-hub focus ink (issue #39): a .tscn hub whose links are
        # all inst-type draws zero budget ink at the boot toggles — every
        # wire it has is showInst-gated off. Entering focus must seed the
        # tier (transition only); Escape returns BOTH the focus and the
        # boot default.
        hub39 = page.evaluate(
            """() => { const d = window.__dbg;
                 let best = null;
                 for (let i = 0; i < d.nodes.length; i++) {
                   if (!/\\.tscn$/i.test(d.nodes[i].path || "")) continue;
                   let tot = 0, inst = 0;
                   for (const l of d.links) {
                     if (l.s !== i && l.t !== i) continue;
                     tot++;
                     if (l.ty === "inst" || l.ty === "attach") inst++;
                   }
                   if (tot > 3 && inst * 2 > tot && (!best || tot > best.tot))
                     best = { i, tot, inst, path: d.nodes[i].path };
                 }
                 return best; }"""
        )
        if not hub39:
            print("SKIP scene-hub focus ink — no inst-dominant .tscn hub in this index")
        else:
            stem39 = hub39["path"].split("/")[-1].rsplit(".", 1)[0].lower()
            page.fill("#search", stem39)
            page.dispatch_event("#search", "input")
            page.wait_for_timeout(700)
            page.evaluate(
                """(p) => { const rows = [...document.querySelectorAll('#searchResults .row')];
                     const r = rows.find(x => x.getAttribute('title') === p) || rows[0];
                     r.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })); }""",
                hub39["path"])
            page.wait_for_timeout(1500)
            st39 = page.evaluate(
                """() => { const d = window.__dbg;
                     return { fi: d.focusFileIdx, inst: d.showInst,
                              btn: document.getElementById('bInst').classList.contains('on'),
                              budgetN: d.budgetLit ? d.budgetLit.size : null,
                              arcs: d.rfwProbe.fa }; }"""
            )
            check("scene-hub focus seeds the inst tier (transition only)",
                  st39["fi"] >= 0 and st39["inst"] and st39["btn"], str(st39))
            check("scene-hub focus draws budget ink",
                  st39["budgetN"] and st39["budgetN"] > 0 and st39["arcs"], str(st39))
            # the user's own toggle during focus is final for this focus
            # (no re-force): flip the tier off mid-focus, then re-run
            # applyVisibility via a depth change — it must NOT re-seed.
            page.click("#bInst")
            page.wait_for_timeout(400)
            page.evaluate(
                """() => { const el = document.getElementById('depth');
                     el.value = 2; el.dispatchEvent(new Event('input')); }""")
            page.wait_for_timeout(600)
            noRefight = page.evaluate(
                "() => ({ inst: window.__dbg.showInst, fi: window.__dbg.focusFileIdx })")
            check("user toggle wins mid-focus (no re-force)",
                  not noRefight["inst"] and noRefight["fi"] >= 0, str(noRefight))
            page.evaluate(
                """() => { const el = document.getElementById('depth');
                     el.value = 1; el.dispatchEvent(new Event('input')); }""")
            page.wait_for_timeout(400)
            page.keyboard.press("Escape")
            page.keyboard.press("Escape")
            page.wait_for_timeout(900)
            st39b = page.evaluate(
                """() => ({ fi: window.__dbg.focusFileIdx,
                     inst: window.__dbg.showInst,
                     btn: document.getElementById('bInst').classList.contains('on') })"""
            )
            check("Escape restores overview + boot showInst=false",
                  st39b["fi"] < 0 and not st39b["inst"] and not st39b["btn"], str(st39b))
        # The dead-only cycle below zeroes alphaTgt for a moment; alphaArr
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
                  // overview (no dead boost/hover); alpha eases, read it live.
                  // Sub-floor nodes are lifted by the zoomed-out degree floor
                  // (min screen DIAMETER, __dbg.degFloorArr) — expected scale
                  // includes that lift: lift = floor/(2*rpx) capped 4x, where
                  // rpx is the projected RADIUS px of the natural size.
                  const dim = i => 0.45 + 0.55 * d.alpha[i];
                  // satellite allowance (sphR): in fn mode, files owning
                  // >6 fns grow so the box ring keeps spacing — the same
                  // factor the template multiplies into every radius
                  const fnsOf = i => (d.fns[d.nodes[i].path] || []).length;
                  const fnOn = document.getElementById('cbFn').checked;
                  const sat = i => fnOn && fnsOf(i) > 6
                    ? 1 + Math.min(0.8, 0.25 * Math.log2(fnsOf(i) / 6)) : 1;
                  const cam = d.camera;
                  const halfH = d.renderer.domElement.clientHeight / 2;
                  const tHalf = Math.tan(cam.fov * Math.PI / 360);
                  const p3 = new d.THREE.Vector3();
                  const nat = i => base(i) * 1.1 * sat(i) * (1 + 0.35 * h[i]) * dim(i);
                  const lift = i => {
                      p3.set(d.pos[i * 3], d.pos[i * 3 + 1], d.pos[i * 3 + 2]);
                      const rpx = nat(i) * halfH / (tHalf * cam.position.distanceTo(p3));
                      const f = d.degFloorArr[i];
                      return rpx > 0.001 && rpx * 2 < f ? Math.min(4.0, f / (2 * rpx)) : 1;
                  };
                  const expHot = nat(arg) * lift(arg);
                  return { n: h.length, max: h[arg], min: Math.min(...h),
                          // diag
                          argP: d.nodes[arg].path, mxv: +mx(arg).toFixed(3),
                          exp: +expHot.toFixed(3), al: +d.alpha[arg].toFixed(3),
                          aT: +d.alphaTgt[arg].toFixed(3),
                          hl: d.hlArr ? d.hlArr[arg] : -1,
                          df: +(d.degFloorArr[arg] || -1).toFixed(2),
                          hotOk: Math.abs(mx(arg) / expHot - 1) < 0.02,
                          // data-gated: an index where every file has churn
                          // (young repo, all touched recently) has no cold file
                          coldOk: cold < 0 ? true
                                  : Math.abs(mx(cold) / (nat(cold) * lift(cold)) - 1) < 0.02,
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
                 let idx = -1, bestDh = 0, bestJ = -1;
                 // pick a clustered node whose fine vs group hues differ
                 // enough that recoloring is measurable in RGB; fall back to
                 // the max-dh node (index drift can shrink the hue gaps)
                 const hue = c => c < 0 ? 0.08 : (c * 0.61803398875 + 0.55) % 1;
                 for (let j = 0; j < d.nodes.length; j++) {
                   const nd = d.nodes[j];
                   if (nd.cluster < 0 || nd.gid < 0 || d.alphaTgt[j] <= 0.5) continue;
                   if (nd.dead > 0) continue;
                   let dh = Math.abs(hue(nd.gid) - hue(nd.cluster));
                   dh = Math.min(dh, 1 - dh);
                   if (dh > bestDh) { bestDh = dh; bestJ = j; }
                   if (dh > 0.15) { idx = j; break; }
                 }
                 if (idx < 0 && bestDh > 0.02) idx = bestJ;
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

        # 7b. split layout: the 2D map pane is a PERMANENT part of the tool —
        # visible at boot, sharing the window with the 3D canvas; #bMap is a
        # collapse/expand toggle; the divider resizes both regions 280-700px
        # and the choice persists in localStorage
        splits = page.evaluate("""() => {
          const p = document.getElementById('mapPane');
          const d = document.getElementById('divider');
          const c = window.__dbg.renderer.domElement;
          return { open: !p.classList.contains('collapsed'),
                   w: p.clientWidth, cw: c.clientWidth, iw: innerWidth,
                   btnOn: document.getElementById('bMap').classList.contains('on'),
                   divVisible: d.offsetWidth > 0 }; }""")
        check("map pane visible at boot (default open)",
              splits["open"] and splits["btnOn"] and splits["divVisible"],
              str(splits))
        check("3D canvas shares window with pane at boot",
              abs(splits["cw"] + splits["w"] - splits["iw"]) <= 2, str(splits))
        # divider drag: pane widens, 3D canvas shrinks by the same amount
        db = page.evaluate(
            "() => document.getElementById('divider').getBoundingClientRect()")
        cx, cy = db["x"] + db["width"] / 2, db["y"] + 100
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx - 120, cy, steps=8)
        page.mouse.up()
        page.wait_for_timeout(400)   # rAF-throttled resize settles
        after = page.evaluate("""() => ({
          w: document.getElementById('mapPane').clientWidth,
          cw: window.__dbg.renderer.domElement.clientWidth, iw: innerWidth,
          stored: parseInt(localStorage.getItem('neuronav.mapPaneW') || '', 10) })""")
        check("divider drag widens pane + shrinks renderer (180..iw-320 clamp)",
              180 <= after["w"] <= after["iw"] - 320
              and after["w"] >= splits["w"] + 100
              and after["cw"] < splits["cw"]
              and abs(after["cw"] + after["w"] - after["iw"]) <= 2, str(after))
        check("divider drag persists width to localStorage",
              after["stored"] == after["w"], str(after))
        # bMap = collapse/expand: 3D renderer reclaims the full window
        page.evaluate("() => document.getElementById('bMap').click()")
        page.wait_for_timeout(300)
        closed = page.evaluate("""() => {
          const p = document.getElementById('mapPane');
          const d = document.getElementById('divider');
          const c = window.__dbg.renderer.domElement;
          return { collapsed: p.classList.contains('collapsed'),
                   w: p.clientWidth, cw: c.clientWidth, iw: innerWidth,
                   divGone: d.offsetWidth === 0 }; }""")
        check("bMap collapses pane; renderer takes full window",
              closed["collapsed"] and closed["divGone"] and closed["w"] == 0
              and abs(closed["cw"] - closed["iw"]) <= 2, str(closed))
        page.evaluate("() => document.getElementById('bMap').click()")
        page.wait_for_timeout(300)
        reopened = page.evaluate("""() => ({
          w: document.getElementById('mapPane').clientWidth,
          cw: window.__dbg.renderer.domElement.clientWidth,
          iw: innerWidth });""")
        check("bMap re-expands pane at persisted width",
              reopened["w"] == after["w"]
              and abs(reopened["cw"] + reopened["w"] - reopened["iw"]) <= 2,
              str(reopened))
        # width choice survives a reload (localStorage)
        page.reload()
        page.wait_for_timeout(2000)
        reloaded = page.evaluate("""() => ({
          w: document.getElementById('mapPane').clientWidth,
          open: !document.getElementById('mapPane').classList.contains('collapsed') });""")
        check("pane width persists across reload",
              reloaded["open"] and reloaded["w"] == after["w"], str(reloaded))

        # 8. map pane (map-spec-v2): named wires over fn rosters. Data-gated
        # on DATA.mwires — an index without the named-wire exports skips.
        # The pane is already open at the persisted width from section 7b.
        mw = page.evaluate("() => window.__dbg.mwires || []")
        if not mw:
            print("SKIP map pane — no DATA.mwires in this index")
        else:
            enter_focus_via_row()
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
                print("SKIP map bundle chips - no multi-wire pair in this index")
            # quiet edges (addendum rule 5): no wire text fields; wires still flow
            check("map quiet edges: label fields removed",
                  bool(minfo) and "labels" not in minfo
                  and "shownLabels" not in minfo
                  and minfo.get("wires", 0) > 0,
                  str(minfo))
            # routed edges (addendum rule 3): no diagonal center-to-center
            # runs - S-curves keep vertical tangents, ortho allows only the
            # <=8px corner chamfers
            ra = page.evaluate(
                """() => { const L = window.__dbg.mapLayout; if (!L) return null;
                     let bez = 0, vt = 0, diag = 0, maxCh = 0;
                     const aud = arr => (arr || []).forEach(w => {
                       if (w.bez) { bez++;
                         if (w.c1 && w.c1[0] === w.pts[0][0] &&
                             w.c2 && w.c2[0] === w.pts[1][0]) vt++;
                       } else {
                         for (let i = 1; i < w.pts.length; i++) {
                           const dx = Math.abs(w.pts[i][0] - w.pts[i-1][0]);
                           const dy = Math.abs(w.pts[i][1] - w.pts[i-1][1]);
                           if (dx > 0.01 && dy > 0.01) { diag++;
                             maxCh = Math.max(maxCh, Math.min(dx, dy)); } } } });
                     aud(L.wires); aud(L.spines);
                     return { bez, vt, diag, maxCh: +maxCh.toFixed(1) }; }""")
            check("map routed edges: no diagonal runs",
                  bool(ra) and ra["bez"] == ra["vt"]
                  and ra["maxCh"] <= 8.01,
                  str(ra))
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
            # section), full caller list, no "+N more hidden" 24-cap.
            # ONE layout is dense: wire midpoints can sit under a box or
            # chip (they outrank wires in hit priority, and chips anchor at
            # wire midpoints) - scan segment quarter points for one clear
            # of both, on an orthogonal (lane-routed) wire only.
            # collect up to 4 clear wire candidates (plain fn wires first,
            # bus stubs after): pane pose varies with the interaction history,
            # and one candidate can sit under an overlay by the click frame
            wpts = page.evaluate("""() => {
                const L = window.__dbg.mapLayout; if (!L) return null;
                const pane = document.getElementById('mapPane');
                const pw = pane.clientWidth, ph = pane.clientHeight;
                const px = window.__dbg.mapPX, py = window.__dbg.mapPY,
                      z = window.__dbg.mapZ;
                const covered = (sx, sy) =>
                    // rects are x0/x1/y0/y1 world bands (L3567), not x/y/w/h
                    L.rects.some(r => sx > (r.x0 - px) * z && sx < (r.x1 - px) * z
                                  && sy > (r.y0 - py) * z && sy < (r.y1 - py) * z)
                    || (L.chips || []).some(c =>
                        sx > (c.x - px) * z && sx < (c.x + c.w - px) * z
                        && sy > (c.y - py) * z && sy < (c.y + c.h - py) * z);
                const cands = [];
                for (const w of L.wires) {
                    if (w.bez) continue;
                    // var wires open the FILE panel by design (member target
                    // is not a fn) - probe only fn-bearing wires. Stubs are
                    // fn-bearing (bus delivery -> dest fn) but rank second:
                    // plain wires exercise the common path.
                    if (w.ty === 'var') continue;
                    for (let k = 1; k < w.pts.length; k++) {
                        for (const t of [0.5, 0.25, 0.75]) {
                            const wx = w.pts[k-1][0] + (w.pts[k][0] - w.pts[k-1][0]) * t;
                            const wy = w.pts[k-1][1] + (w.pts[k][1] - w.pts[k-1][1]) * t;
                            const sx = (wx - px) * z, sy = (wy - py) * z;
                            if (sx > 4 && sy > 4 && sx < pw - 4 && sy < ph - 4
                                && !covered(sx, sy)) {
                                const rank = w.stub ? 1 : 0;
                                cands.push({ sx: +sx.toFixed(1), sy: +sy.toFixed(1),
                                             rank, dfn: w.dfn });
                                cands.sort((a, b) => a.rank - b.rank);
                                if (cands.length > 4) cands.length = 4;
                            }
                        }
                    }
                }
                return cands.length ? cands : null; }""")
            fn_info = None
            for wpt in (wpts or []):
                page.mouse.click(bb["x"] + wpt["sx"], bb["y"] + wpt["sy"])
                page.wait_for_timeout(350)
                fn_info = page.evaluate(
                    """() => ({ open: document.getElementById('info').style.display === 'block',
                         title: document.getElementById('iTitle').textContent })""")
                if fn_info["open"] and fn_info["title"].endswith("()"):
                    break   # fn panel landed - this pose carries the pin
                fn_info = None
            if fn_info:
                check("map wire click opens fn panel",
                      fn_info["open"] and fn_info["title"].endswith("()"),
                      str(fn_info) + " aims=" + str(wpts))
                check("fn panel lists all callers (no 24-cap)",
                      page.locator("#iUsedBy li.more").count() == 0)
                # [issue #82] sticky wire selection: the click above must
                # latch a paint-tier pin (ONE-layout: wire set unchanged)
                pin0 = page.evaluate("() => window.__dbg.wirePin")
                wires0 = page.evaluate("() => window.__dbg.mapInfo().wires")
                check("wire pin latches on wire click",
                      bool(pin0) and pin0["surface"] == "map"
                      and pin0["kind"] == "wire" and pin0["id"], str(pin0))
                check("wire pin emphasis resolves (pinCover)",
                      page.evaluate(
                          "() => window.__dbg.mapInfo().pinCover") == 1)
                # chip-list item selection: a bundle chip's enumerated rows
                # are wires too - two distinct row clicks must REPLACE the
                # pin (single pin at a time)
                chip_hits = page.evaluate("""() => {
                    const L = window.__dbg.mapLayout;
                    const d = window.__dbg;
                    const pn = document.getElementById("mapPane");
                    const out = [];
                    for (const ch of L.chips) {
                        if (!ch.wires || ch.wires.length < 2) continue;
                        if (ch.wires.filter(w => w.ty !== 'var').length < 2) continue;
                        const sx = (ch.x + ch.w / 2 - d.mapPX) * d.mapZ;
                        const sy = (ch.y + ch.h / 2 - d.mapPY) * d.mapZ;
                        if (sx > 20 && sy > 20 &&
                            sx < pn.clientWidth - 20 &&
                            sy < pn.clientHeight - 20)
                            out.push({ sx: +sx.toFixed(1), sy: +sy.toFixed(1) });
                        if (out.length >= 6) break;
                    }
                    return out; }""")
                chip_hit = None
                for cand in (chip_hits or []):
                    page.mouse.click(bb["x"] + cand["sx"], bb["y"] + cand["sy"])
                    page.wait_for_timeout(300)
                    if page.locator("#mapList .row").count() >= 2:
                        chip_hit = cand
                        break
                if chip_hit:
                    rows = page.locator("#mapList .row")
                    if rows.count() >= 2:
                        rows.nth(0).click()
                        page.wait_for_timeout(250)
                        pinA = page.evaluate("() => window.__dbg.wirePin")
                        rows.nth(1).click()
                        page.wait_for_timeout(250)
                        pinB = page.evaluate("() => window.__dbg.wirePin")
                        check("chip-list item pins the wire",
                              bool(pinA) and pinA["menu"] == "list",
                              str(pinA))
                        check("new selection replaces the pin",
                              bool(pinB) and pinB["id"] != pinA["id"],
                              f"{pinA} -> {pinB}")
                        pin0 = pinB   # persistence rides the live pin
                    else:
                        print("SKIP chip-list pin - list rows < 2")
                else:
                    print(f"SKIP chip-list pin - no chip opened a list ({chip_hits})")
                # persistence: pan, zoom, hover-out - paint-tier state must
                # survive all three without touching the layout
                page.mouse.move(bb["x"] + 120, bb["y"] + 520)
                page.mouse.down()
                page.mouse.move(bb["x"] + 260, bb["y"] + 580, steps=6)
                page.mouse.up()
                page.wait_for_timeout(250)
                page.mouse.move(bb["x"] + 400, bb["y"] + 300)
                page.mouse.wheel(0, -240)
                page.wait_for_timeout(250)
                page.mouse.move(bb["x"] + 60, bb["y"] + 620)
                page.wait_for_timeout(250)
                pin1 = page.evaluate("() => window.__dbg.wirePin")
                wires1 = page.evaluate(
                    "() => window.__dbg.mapInfo().wires")
                check("wire pin survives pan/zoom/hover (paint tier)",
                      pin1 == pin0 and wires1 == wires0,
                      f"{pin0} -> {pin1}, wires {wires0} -> {wires1}")

                # ---- [issue #82] dismissal paths ----
                def latch_pin_via_list():
                    hits = page.evaluate("""() => {
                        const L = window.__dbg.mapLayout;
                        const d = window.__dbg;
                        const pn = document.getElementById("mapPane");
                        const out = [];
                        for (const ch of L.chips) {
                            if (!ch.wires || ch.wires.length < 2) continue;
                            if (ch.wires.filter(w => w.ty !== 'var').length < 2) continue;
                            const sx = (ch.x + ch.w / 2 - d.mapPX) * d.mapZ;
                            const sy = (ch.y + ch.h / 2 - d.mapPY) * d.mapZ;
                            if (sx > 20 && sy > 20 &&
                                sx < pn.clientWidth - 20 &&
                                sy < pn.clientHeight - 20)
                                out.push({ sx: +sx.toFixed(1), sy: +sy.toFixed(1) });
                            if (out.length >= 6) break;
                        }
                        return out; }""")
                    for cand in (hits or []):
                        page.mouse.click(bb["x"] + cand["sx"], bb["y"] + cand["sy"])
                        page.wait_for_timeout(300)
                        # stale rows from a closed list stay in the DOM:
                        # require the list itself to be open before clicking
                        disp = page.evaluate(
                            "() => document.getElementById('mapList').style.display")
                        rows = page.locator("#mapList .row")
                        if disp == "block" and rows.count() >= 2:
                            rows.nth(0).click()
                            page.wait_for_timeout(200)
                            return page.evaluate("() => window.__dbg.wirePin")
                    return None

                # (a) Esc: one press dismisses the pin AND the menu it owns
                # atomically (issue #84 / skeptic #4 - the old two-press
                # chain orphaned the list between presses); the ladder walk
                # past the pin's own surface is pinned by the 3d block
                if pin1 and pin1["menu"] == "list":
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(250)
                    pinE = page.evaluate("() => window.__dbg.wirePin")
                    coverE = page.evaluate("() => window.__dbg.pinCover")
                    listE = page.evaluate(
                        "() => document.getElementById('mapList').style.display")
                    check("esc dismisses the pin and its menu atomically",
                          pinE is None and coverE == 0 and listE != "block",
                          f"{pin1} -> {pinE}, cover {coverE}, list {listE}")
                else:
                    print("SKIP esc chain - live pin is not a list pin")
                # (b) right-click: dismisses the pin and ONLY the pin (the
                # 3D clear-focus contextmenu must not fire on this press)
                pinR = latch_pin_via_list()
                if pinR:
                    page.mouse.click(bb["x"] + 300, bb["y"] + 200, button="right")
                    page.wait_for_timeout(250)
                    pinR2 = page.evaluate("() => window.__dbg.wirePin")
                    infoR = page.evaluate(
                        "() => document.getElementById('info').style.display")
                    check("right-click dismisses the pin (and only the pin)",
                          pinR2 is None and infoR == "block",
                          f"{pinR} -> {pinR2}, info {infoR}")
                else:
                    print("SKIP right-click dismiss - no chip list")
                # (c) closing the wire menu (bundle list) unpins its selection
                pinM = latch_pin_via_list()
                if pinM:
                    void_pt = page.evaluate("""() => {
                        const d = window.__dbg;
                        const pn = document.getElementById("mapPane");
                        const pw = pn.clientWidth, ph = pn.clientHeight;
                        const nearWire = (wx, wy) => {
                            for (const w of d.mapLayout.wires) {
                                for (let k = 1; k < w.pts.length; k++) {
                                    const ax = w.pts[k-1][0], ay = w.pts[k-1][1];
                                    const bx2 = w.pts[k][0], by2 = w.pts[k][1];
                                    const dx = bx2 - ax, dy = by2 - ay;
                                    const t = Math.max(0, Math.min(1,
                                        ((wx-ax)*dx + (wy-ay)*dy) / (dx*dx + dy*dy || 1)));
                                    if (Math.hypot(ax + t*dx - wx, ay + t*dy - wy) < 12 / d.mapZ)
                                        return true;
                                }
                            }
                            return false; };
                        for (let sy = ph - 30; sy > 40; sy -= 24) {
                            for (let sx = 70; sx < pw - 14; sx += 24) {
                                const wx = sx / d.mapZ + d.mapPX;
                                const wy = sy / d.mapZ + d.mapPY;
                                const onRect = d.mapRects.some(r =>
                                    wx >= r.x - 3 && wx <= r.x + r.w + 3 &&
                                    wy >= r.y - 3 && wy <= r.y + r.h + 3);
                                const onChip = d.mapLayout.chips.some(c =>
                                    wx >= c.x - 4 && wx <= c.x + c.w + 4 &&
                                    wy >= c.y - 4 && wy <= c.y + c.h + 4);
                                if (onRect || onChip || nearWire(wx, wy)) continue;
                                return { sx, sy };
                            }
                        }
                        return null; }""")
                    if void_pt:
                        # keep the click clear of HTML overlays (the open list
                        # sits on top of the canvas and swallows canvas clicks)
                        ov_ok = page.evaluate(
                            "() => { const r = document"
                            ".getElementById('mapList').getBoundingClientRect();"
                            " return r; }")
                        sx = void_pt["sx"] + bb["x"]
                        sy = void_pt["sy"] + bb["y"]
                        if (ov_ok and sx > ov_ok["x"] - 8 and
                                sx < ov_ok["x"] + ov_ok["width"] + 8 and
                                sy > ov_ok["y"] - 8 and
                                sy < ov_ok["y"] + ov_ok["height"] + 8):
                            print("SKIP menu close - void under open list")
                        else:
                            page.mouse.click(sx, sy)
                            page.wait_for_timeout(300)
                            pinM2 = page.evaluate("() => window.__dbg.wirePin")
                            listM = page.evaluate(
                                "() => document.getElementById('mapList')"
                                ".style.display")
                            check("closing the wire menu unpins its selection",
                                  pinM2 is None and listM != "block",
                                  f"{pinM} -> {pinM2}, list {listM}")
                    else:
                        print("SKIP menu close - no void point")
                else:
                    print("SKIP menu close - no chip list")

                # ---- [issue #82] 3D surface: corridor/wire click pins ----
                # scan the 3D canvas (left of the map pane) for a point whose
                # pickWireMeta resolves a pickable wire; clicking there runs
                # the capture-phase wire handler, which opens the tip and
                # latches the pin on the ball surface. Section 5bb left the
                # fn layer off (hover tests); a close-up focus then ink-gates
                # every named link below the pick threshold, so turn the fn
                # layer on for this block and restore it after — fn wires
                # pin identically (kind "wire").
                # ---- [issue #82] trunk corridor pins (the enumerated set) ----
                # re-establish focus first: the esc-chain walk above ends in
                # clearFocus when the tip is already hidden (the orbit press
                # hides it), which drops the map layout
                page.fill("#search", tok)
                page.dispatch_event("#search", "input")
                page.wait_for_timeout(500)
                page.evaluate("""tok => { const rows = [...document.querySelectorAll('#searchResults .row')];
                    (rows.find(x => x.title.endsWith(tok)) || rows[0])
                    .dispatchEvent(new PointerEvent('pointerdown', {bubbles: true})); }""", tok)
                page.wait_for_timeout(1200)
                # 2D: click a trunk spine — the pin covers trunk + taps and
                # opens the bus card (map-side trunk-click parity)
                trunk_pt = page.evaluate("""() => {
                    const d = window.__dbg;
                    const L = d.mapLayout;
                    const pn = document.getElementById("mapPane");
                    for (const sp of L.spines) {
                        if (sp.hub !== "trunk" || !sp.pts) continue;
                        for (let k = 1; k < sp.pts.length; k++) {
                            const mx = (sp.pts[k-1][0] + sp.pts[k][0]) / 2;
                            const my = (sp.pts[k-1][1] + sp.pts[k][1]) / 2;
                            const sx = (mx - d.mapPX) * d.mapZ;
                            const sy = (my - d.mapPY) * d.mapZ;
                            if (sx < 14 || sy < 14 ||
                                sx > pn.clientWidth - 14 ||
                                sy > pn.clientHeight - 14) continue;
                            const onRect = d.mapRects.some(r =>
                                mx >= r.x - 8 && mx <= r.x + r.w + 8 &&
                                my >= r.y - 8 && my <= r.y + r.h + 8);
                            if (onRect) continue;
                            return { sx, sy };
                        }
                    }
                    return null; }""")
                if trunk_pt:
                    page.mouse.click(bb["x"] + trunk_pt["sx"],
                                     bb["y"] + trunk_pt["sy"])
                    page.wait_for_timeout(350)
                    pinT = page.evaluate("() => window.__dbg.wirePin")
                    coverT = page.evaluate("() => window.__dbg.pinCover")
                    cardT = page.evaluate(
                        "() => ({ disp: document.getElementById('wireTip')"
                        ".style.display,"
                        " txt: document.getElementById('wireTip')"
                        ".textContent.slice(0, 8) })")
                    check("map trunk click pins the enumerated set",
                          pinT and pinT["surface"] == "map" and
                          pinT["kind"] == "trunk" and pinT["id"],
                          f"{pinT}")
                    check("trunk pin emphasis covers trunk + taps",
                          coverT >= 2, f"pinCover {coverT}")
                    check("trunk pin opens the bus card",
                          cardT["disp"] == "block" and
                          cardT["txt"].startswith("\U0001f68c"),
                          f"{cardT}")
                    # zoom survival (paint tier, no layout rebuild)
                    page.mouse.move(bb["x"] + 400, bb["y"] + 400)
                    page.mouse.wheel(0, -600)
                    page.wait_for_timeout(400)
                    pinT2 = page.evaluate("() => window.__dbg.wirePin")
                    coverT2 = page.evaluate("() => window.__dbg.pinCover")
                    check("trunk pin survives zoom (paint tier)",
                          pinT2 == pinT and coverT2 >= 2,
                          f"{pinT} -> {pinT2}, cover {coverT2}")
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(250)
                else:
                    print("SKIP map trunk pin - no on-screen trunk corridor")
                # 3D: trunk conduit click pins on the ball surface.
                # The esc-chain walk above can end in clearFocus (tip
                # already hidden), which drops the serve gate's focus; a
                # fresh row-click re-frames the camera inside 2.2 ball
                # radii (_lodServe) so conduit picks resolve.
                page.fill("#search", tok)
                page.dispatch_event("#search", "input")
                page.wait_for_timeout(500)
                page.evaluate("""tok => { const rows = [...document.querySelectorAll('#searchResults .row')];
                    (rows.find(x => x.title.endsWith(tok)) || rows[0])
                    .dispatchEvent(new PointerEvent('pointerdown', {bubbles: true})); }""", tok)
                page.wait_for_timeout(1200)
                if not page.evaluate("() => window.__dbg.lodServe"):
                    # dolly in: serve needs the camera inside 2.2 ball
                    # radii — a focused re-click may not move it
                    page.mouse.move(400, 300)
                    for _ in range(3):
                        page.mouse.wheel(0, -600)
                        page.wait_for_timeout(250)
                    page.wait_for_timeout(500)
                trunk3 = None
                tpts = page.evaluate("""() => {
                    const d = window.__dbg;
                    const pn = document.getElementById("mapPane");
                    const xmax = (pn ? pn.getBoundingClientRect().x
                                    : innerWidth) - 14;
                    const out = [];
                    for (let y = 70; y < innerHeight - 40 && out.length < 5;
                         y += 44)
                        for (let x = 24; x < xmax && out.length < 5; x += 44) {
                            const m = d.pickWireMeta({ clientX: x, clientY: y });
                            if (m && m.kind === "trunk") out.push({ x, y });
                        }
                    return out; }""")
                if not tpts:
                    # the 3d-wire block's orbit can leave no conduit in the
                    # strip — one orbit re-frames and the scan retries once
                    page.mouse.move(400, 460)
                    page.mouse.down()
                    for k in range(6):
                        page.mouse.move(400 + 18 * (k + 1), 460 + 6 * (k + 1))
                    page.mouse.up()
                    page.wait_for_timeout(700)
                    tpts = page.evaluate("""() => {
                        const d = window.__dbg;
                        const pn = document.getElementById("mapPane");
                        const xmax = (pn ? pn.getBoundingClientRect().x
                                        : innerWidth) - 14;
                        const out = [];
                        for (let y = 70; y < innerHeight - 40 && out.length < 5;
                             y += 44)
                            for (let x = 24; x < xmax && out.length < 5; x += 44) {
                                const m = d.pickWireMeta({ clientX: x, clientY: y });
                                if (m && m.kind === "trunk") out.push({ x, y });
                            }
                        return out; }""")
                for cand in tpts or []:
                    page.mouse.move(cand["x"], cand["y"])
                    page.wait_for_timeout(150)
                    hov = page.evaluate(
                        "() => ({ hf: window.__dbg.hoveredFn,"
                        " hv: window.__dbg.hovered })")
                    if hov["hf"] is not None and hov["hf"] >= 0:
                        continue
                    if hov["hv"] is not None and hov["hv"] >= 0:
                        continue
                    page.mouse.click(cand["x"], cand["y"])
                    page.wait_for_timeout(300)
                    trunk3 = page.evaluate("() => window.__dbg.wirePin")
                    if trunk3:
                        break
                if trunk3:
                    cover3t = page.evaluate("() => window.__dbg.pinCover")
                    check("3d trunk click pins the conduit",
                          trunk3["surface"] == "ball" and
                          trunk3["kind"] == "trunk" and trunk3["id"],
                          f"{trunk3}, cover {cover3t}")
                    check("3d trunk emphasis resolves (pinCover)",
                          cover3t >= 2, f"cover {cover3t}")
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(250)
                    trunk3b = page.evaluate("() => window.__dbg.wirePin")
                    check("esc dismisses the 3d trunk pin",
                          trunk3b is None, f"{trunk3} -> {trunk3b}")
                else:
                    print("SKIP 3d trunk pin - no pickable conduit in view")

                fn_was_on = page.evaluate(
                    "() => document.getElementById('cbFn').checked")

                link_pts = page.evaluate("""() => {
                    const d = window.__dbg;
                    const pn = document.getElementById("mapPane");
                    const xmax = (pn ? pn.getBoundingClientRect().x
                                    : innerWidth) - 14;
                    const out = [];
                    for (let y = 70; y < innerHeight - 40 && out.length < 5;
                         y += 44) {
                        for (let x = 24; x < xmax && out.length < 5; x += 44) {
                            const m = d.pickWireMeta({ clientX: x, clientY: y });
                            if (m && (m.kind === "link" || m.kind === "wire"))
                                out.push({ x, y });
                        }
                    }
                    return out; }""")
                if not link_pts:
                    # pose-sensitive scan: link ink gates by camera distance
                    # (edgeK); a real orbit drag re-frames the ink tiers and
                    # the scan retries once before giving up
                    page.mouse.move(400, 460)
                    page.mouse.down()
                    for k in range(6):
                        page.mouse.move(400 + 18 * (k + 1), 460 + 6 * (k + 1))
                    page.mouse.up()
                    page.wait_for_timeout(700)
                    link_pts = page.evaluate("""() => {
                        const d = window.__dbg;
                        const pn = document.getElementById("mapPane");
                        const xmax = (pn ? pn.getBoundingClientRect().x
                                        : innerWidth) - 14;
                        const out = [];
                        for (let y = 70; y < innerHeight - 40 && out.length < 5;
                             y += 44) {
                            for (let x = 24; x < xmax && out.length < 5; x += 44) {
                                const m = d.pickWireMeta({ clientX: x, clientY: y });
                                if (m && m.kind === "link")
                                    out.push({ x, y });
                            }
                        }
                        return out; }""")
                pin3d = None
                for cand in link_pts or []:
                    page.mouse.move(cand["x"], cand["y"])
                    page.wait_for_timeout(150)
                    hov = page.evaluate(
                        "() => ({ hf: window.__dbg.hoveredFn,"
                        " hv: window.__dbg.hovered })")
                    if hov["hf"] is not None and hov["hf"] >= 0:
                        continue
                    if hov["hv"] is not None and hov["hv"] >= 0:
                        continue
                    page.mouse.click(cand["x"], cand["y"])
                    page.wait_for_timeout(300)
                    pin3d = page.evaluate("() => window.__dbg.wirePin")
                    if pin3d:
                        break
                if pin3d:
                    cover3d = page.evaluate("() => window.__dbg.pinCover")
                    check("3d wire click pins on the ball surface",
                          pin3d["surface"] == "ball" and
                          pin3d["kind"] in ("link", "wire") and pin3d["id"],
                          f"{pin3d}, cover {cover3d}")
                    check("3d pin emphasis resolves (pinCover)",
                          cover3d >= 1, f"cover {cover3d}")
                    # camera orbit: paint-tier state, overlay re-derived
                    # per frame from the bucket buffers
                    cx0, cy0 = 400, 460
                    page.mouse.move(cx0, cy0)
                    page.mouse.down()
                    for k in range(6):
                        page.mouse.move(cx0 + 18 * (k + 1), cy0 + 6 * (k + 1))
                        page.wait_for_timeout(40)
                    page.mouse.up()
                    page.wait_for_timeout(400)
                    pin3b = page.evaluate("() => window.__dbg.wirePin")
                    cover3b = page.evaluate("() => window.__dbg.pinCover")
                    check("3d pin survives camera orbit (paint tier)",
                          pin3b == pin3d and cover3b >= 1,
                          f"{pin3d} -> {pin3b}, cover {cover3b}")
                    # esc: the pin owns the first press; the tip (transient
                    # overlay) closes on the NEXT press per the existing chain
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(250)
                    pin3c = page.evaluate("() => window.__dbg.wirePin")
                    check("esc dismisses the 3d pin", pin3c is None,
                          f"{pin3b} -> {pin3c}")
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(250)
                    tip3c = page.evaluate(
                        "() => document.getElementById('wireTip')"
                        ".style.display")
                    check("next esc closes the 3d tip (existing binding)",
                          tip3c == "none", f"tip {tip3c}")
                else:
                    print("SKIP 3d wire pin - no pickable link in view")

                if not fn_was_on:
                    page.evaluate(
                        "() => document.getElementById('cbFn').click()")
                    page.wait_for_timeout(500)
                # the esc walk above (tip already hidden by the orbit
                # press) ends in clearFocus, which drops the map layout —
                # re-enter focus (same helper the suite uses: includes the
                # conditional depth escalation a depth-1 re-focus needs)
                enter_focus_via_row()

                # ---- [issue #84] real-hand interaction battery ----
                # jitter: a sub-10px drift press on wire ink is a PICK at
                # the press origin (skeptic #2a: the old >4px pan-guard
                # swallowed real clicks; synthetic zero-drift clicks masked
                # it)
                wpt = page.evaluate("""() => {
                    const d = window.__dbg, L = d.mapLayout;
                    if (!L) return null;
                    const pn = document.getElementById('mapPane');
                    const bb = pn.getBoundingClientRect();
                    for (const w of L.wires) {
                        if (w.bez || w.ty === 'var' || w.pts.length < 2) continue;
                        const a = w.pts[Math.floor(w.pts.length / 2) - 1];
                        const b = w.pts[Math.floor(w.pts.length / 2)];
                        const wx = (a[0] + b[0]) / 2, wy = (a[1] + b[1]) / 2;
                        const sx = (wx - d.mapPX) * d.mapZ + bb.left;
                        const sy = (wy - d.mapPY) * d.mapZ + bb.top;
                        if (sx > bb.left + 8 && sx < bb.right - 8 &&
                            sy > bb.top + 8 && sy < bb.bottom - 8)
                            return { sx: sx, sy: sy };
                    }
                    return null; }""")
                if wpt:
                    page.mouse.move(wpt["sx"] - 14, wpt["sy"] - 10)
                    page.mouse.move(wpt["sx"], wpt["sy"], steps=3)
                    page.mouse.down()
                    page.mouse.move(wpt["sx"] + 6, wpt["sy"] + 4)
                    page.wait_for_timeout(60)
                    page.mouse.up()
                    page.wait_for_timeout(300)
                    pinJ = page.evaluate("() => window.__dbg.wirePin")
                    check("jittered wire click still pins (sub-10px drift latch)",
                          pinJ and pinJ["surface"] == "map" and pinJ["kind"] == "wire",
                          f"{wpt} -> {pinJ}")
                    # void click: uniform dismissal for ANY leftover pin
                    # (skeptic #6: only list-pins died before)
                    if pinJ:
                        vpt = page.evaluate("""() => {
                            const d = window.__dbg, L = d.mapLayout;
                            const pn = document.getElementById('mapPane');
                            const bb = pn.getBoundingClientRect();
                            for (let sy = bb.bottom - 20; sy > bb.top + 20; sy -= 24) {
                                for (let sx = bb.left + 20; sx < bb.right - 20; sx += 24) {
                                    const wx = (sx - bb.left) / d.mapZ + d.mapPX;
                                    const wy = (sy - bb.top) / d.mapZ + d.mapPY;
                                    if (d.mapWireAt(wx, wy) >= 0) continue;
                                    let inRect = false;
                                    for (const rc of L.rects)
                                        if (wx >= rc.x - 3 && wx <= rc.x + rc.w + 3 &&
                                            wy >= rc.y - 3 && wy <= rc.y + rc.h + 3)
                                            { inRect = true; break; }
                                    if (inRect) continue;
                                    return { sx: sx, sy: sy };
                                }
                            }
                            return null; }""")
                        if vpt:
                            page.mouse.click(vpt["sx"], vpt["sy"])
                            page.wait_for_timeout(250)
                            pinV = page.evaluate("() => window.__dbg.wirePin")
                            check("void click unpins any stale pin",
                                  pinV is None, f"{pinJ} -> {pinV} at {vpt}")
                        else:
                            print("SKIP void unpin - no clear pane point")
                    # focus change: a card refocus rebuilds the layout and
                    # must clear the map pin (skeptic #5) - latch a FRESH
                    # pin first (the void check above just cleared it)
                    page.mouse.click(wpt["sx"], wpt["sy"])
                    page.wait_for_timeout(300)
                    pinF0 = page.evaluate("() => window.__dbg.wirePin")
                    if pinF0:
                        card2 = page.evaluate("""() => {
                            const d = window.__dbg;
                            const pn = document.getElementById('mapPane');
                            const bb = pn.getBoundingClientRect();
                            for (const rc of d.mapRects) {
                                if (rc.i === d.focusFileIdx) continue;
                                const sx = (rc.x + rc.w / 2 - d.mapPX) * d.mapZ + bb.left;
                                const sy = (rc.y + 11 - d.mapPY) * d.mapZ + bb.top;
                                if (sx > bb.left + 8 && sx < bb.right - 8 &&
                                    sy > bb.top + 8 && sy < bb.bottom - 8)
                                    return { sx: sx, sy: sy, i: rc.i };
                            }
                            return null; }""")
                        if card2:
                            page.mouse.move(card2["sx"] - 12, card2["sy"] - 8)
                            page.mouse.move(card2["sx"], card2["sy"], steps=3)
                            page.mouse.click(card2["sx"], card2["sy"])
                            page.wait_for_timeout(700)
                            pinF1 = page.evaluate("() => window.__dbg.wirePin")
                            check("focus change clears the map pin",
                                  pinF1 is None, f"{pinF0} -> {pinF1} via card {card2['i']}")
                            # 3d camera must keep orbiting after the card
                            # click (skeptic #3: a label swallowing the
                            # orbit grab used to leave the camera dead)
                            p0 = page.evaluate(
                                "() => window.__dbg.camera.position.toArray()")
                            page.mouse.move(600, 450)
                            page.mouse.down()
                            for k in range(6):
                                page.mouse.move(600 + 22 * (k + 1),
                                                450 + 6 * (k + 1))
                            page.mouse.up()
                            page.wait_for_timeout(400)
                            p1 = page.evaluate(
                                "() => window.__dbg.camera.position.toArray()")
                            moved = sum((a - b) ** 2
                                        for a, b in zip(p0, p1)) ** 0.5
                            check("2d card click keeps the 3d camera orbiting",
                                  moved > 1, f"orbit delta {moved:.2f}")
                        else:
                            print("SKIP focus-change clear - no second card in view")
                    # label drag: drags off a label forward to the orbit
                    # camera and the trailing label click is suppressed
                    lab = page.evaluate("""() => {
                        const pn = document.getElementById('mapPane');
                        const xr = pn ? pn.getBoundingClientRect().x : innerWidth;
                        for (const el of document.querySelectorAll('#hubs .hub, #flabs .flab')) {
                            const r = el.getBoundingClientRect();
                            if (r.left > 4 && r.right < xr - 4 &&
                                r.top > 4 && r.bottom < innerHeight - 4)
                                return { x: r.left + r.width / 2,
                                         y: r.top + r.height / 2 };
                        }
                        return null; }""")
                    if lab:
                        seeds0 = page.evaluate(
                            "() => window.__dbg.focusSeeds.size")
                        p0 = page.evaluate(
                            "() => window.__dbg.camera.position.toArray()")
                        page.mouse.move(lab["x"], lab["y"])
                        page.mouse.down()
                        for k in range(8):
                            page.mouse.move(lab["x"] + 25 * (k + 1),
                                            lab["y"] + 6 * (k + 1))
                            page.wait_for_timeout(30)
                        page.mouse.up()
                        page.wait_for_timeout(400)
                        p1 = page.evaluate(
                            "() => window.__dbg.camera.position.toArray()")
                        movedL = sum((a - b) ** 2
                                     for a, b in zip(p0, p1)) ** 0.5
                        seeds1 = page.evaluate(
                            "() => window.__dbg.focusSeeds.size")
                        check("label drag orbits; its click is suppressed",
                              movedL > 1 and seeds1 == seeds0,
                              f"orbit {movedL:.2f}, seeds {seeds0}->{seeds1}")
                    else:
                        print("SKIP label drag - no label in view")
            elif wpts:
                check("map wire click opens fn panel", False,
                      f"no fn panel from {len(wpts)} clear wire aims: " + str(wpts))
            else:
                print("SKIP map wire click - no clear wire point")
            # corridor trunk consolidation (declutter): corridors spanning
            # the same chunk-row hop share one trunk - distinct stroked
            # corridor polylines must drop below the admitted corridor
            # count. Data-gated: E>12 AND same-hop groups present in this
            # index/focus (fit layout, after the probe above - a wire
            # click opens the info panel without relayout).
            zinfo = page.evaluate("() => window.__dbg.mapInfo()")
            if zinfo and zinfo.get("E", 0) > 12 and zinfo.get("trunkGroups", 0) > 0:
                check("map trunk consolidation reduces drawn polylines",
                      zinfo.get("spinesDrawn", 0) < zinfo.get("spineTotal", 0)
                      and zinfo.get("drawnPolys", 0) <
                          zinfo.get("spineTotal", 0) + zinfo.get("wires", 0),
                      str(zinfo))
            else:
                print(f"SKIP map trunk consolidation - {zinfo}")

            # 8b. ONE layout at every zoom (tier system deleted): the wiring
            # diagram above is THE layout - zooming out must NEVER collapse
            # it into band trunks, zooming in must never re-scope or
            # rearrange it, and drag = pan must hold at every zoom.
            mbb = page.evaluate(
                "() => document.getElementById('mapPane').getBoundingClientRect()")
            mcx, mcy = mbb["x"] + mbb["width"] / 2, mbb["y"] + mbb["height"] / 2

            def mstruct():
                return page.evaluate("""() => { const m = window.__dbg.mapInfo() || {};
                    return { E: m.E, wires: m.wires, chips: m.chips,
                             boxIxs: m.boxIxs, rosterRows: m.rosterRows,
                             probe: m.probeWire }; }""")

            mkeys = ("E", "wires", "chips", "boxIxs", "rosterRows")
            mbase = mstruct()
            # zoom in far: same structure, just scaled
            page.mouse.move(mcx, mcy)
            for _ in range(10):
                page.mouse.wheel(0, -120)
                page.wait_for_timeout(60)
            page.wait_for_timeout(500)
            zin = mstruct()
            check("zoom-in keeps the ONE layout (no re-scope, no relayout)",
                  all(zin[k] == mbase[k] for k in mkeys), f"{mbase} -> {zin}")
            page.screenshot(path=str(SHOTS / "qa_map_zoomin.png"),
                            scale="css", type="png")
            print("artifact: .tmp/shots/qa_map_zoomin.png")
            # drag = pan zoomed in far: content follows the cursor 1:1 and
            # HOLDS (the old doc re-center snap-back is gone)
            page.mouse.move(mcx, mcy)
            page.mouse.down()
            page.mouse.move(mcx - 200, mcy - 150, steps=8)
            page.mouse.up()
            page.wait_for_timeout(120)
            pan_mid = mstruct()
            page.wait_for_timeout(600)
            pan = mstruct()
            pr0, pr1 = zin["probe"], pan["probe"]
            moved = pr0 and pr1 and abs(pr1["sx"] - pr0["sx"] + 200) < 30 \
                and abs(pr1["sy"] - pr0["sy"] + 150) < 30
            check("drag pans content zoomed-in (cursor-following)", moved,
                  f"{pr0} -> {pr1}")
            check("pan holds zoomed-in (no snap-back)",
                  pan_mid["probe"] == pan["probe"],
                  f"{pan_mid['probe']} vs {pan['probe']}")
            check("pan does not change the layout",
                  all(pan[k] == mbase[k] for k in mkeys), "")
            page.screenshot(path=str(SHOTS / "qa_map_panned.png"),
                            scale="css", type="png")
            print("artifact: .tmp/shots/qa_map_panned.png")
            # zoom far out: SAME layout scaled - nothing collapses into
            # band trunks, no doc re-scoping, boxes keep their rosters
            page.mouse.move(mcx, mcy)
            for _ in range(14):
                page.mouse.wheel(0, 120)
                page.wait_for_timeout(60)
            page.wait_for_timeout(500)
            zout = mstruct()
            check("zoom-out keeps the ONE layout (no band-trunk collapse)",
                  all(zout[k] == mbase[k] for k in mkeys), f"{mbase} -> {zout}")
            check("zoom-out moved the view (scaled, not frozen)",
                  zout["probe"] != zin["probe"], "")
            page.screenshot(path=str(SHOTS / "qa_map_zoomout.png"),
                            scale="css", type="png")
            print("artifact: .tmp/shots/qa_map_zoomout.png")
            # 8c. zoom-gated ink tiers (paint-only): the fine layers
            # (underlays, named wires, port dots/arrowheads) hide when
            # zoomed out and return when zoomed back in (hysteresis) -
            # the LAYOUT never changes: mapLayout.wires count identical
            # at both ends (ONE-layout law survives the tier gate).
            wires_pre = page.evaluate("() => window.__dbg.mapLayout.wires.length")
            page.mouse.move(mcx, mcy)
            for _ in range(20):
                page.mouse.wheel(0, 120)
                page.wait_for_timeout(40)
                if page.evaluate("() => window.__dbg.mapZ") < 0.5:
                    break
            page.wait_for_timeout(400)
            ink_off = page.evaluate(
                """() => ({ ink: window.__dbg.mapInkOn,
                            wires: window.__dbg.mapLayout.wires.length })""")
            check("zoom-out hides fine ink (paint-only tier)",
                  ink_off["ink"] is False, str(ink_off))
            check("ink gate never touches the layout (wires unchanged)",
                  ink_off["wires"] == wires_pre, f"{wires_pre} -> {ink_off['wires']}")
            # zoom back in: hysteresis upper bound restores the tier
            page.mouse.move(mcx, mcy)
            for _ in range(20):
                page.mouse.wheel(0, -120)
                page.wait_for_timeout(40)
                if page.evaluate("() => window.__dbg.mapZ") >= 1.0:
                    break
            page.wait_for_timeout(400)
            ink_on = page.evaluate("() => window.__dbg.mapInkOn")
            check("zoom-in restores fine ink (hysteresis)",
                  ink_on is True, str(ink_on))
            # 8d. map-center-on-selection: clicking a 3D file node pans the
            # 2D pane so the node's box lands on pane center (tol 12px,
            # clamp permitting), pulses it, and is a clean no-op while the
            # pane is collapsed.
            def _hub_pick():
                return page.evaluate(
                    """() => { const d = window.__dbg;
                         // aim at the focus SEED itself (level 0): always lit
                         // bright (pickable per the alphaTgt > 0.5 pick law),
                         // guaranteed inside the map's top-40, and camera-
                         // centered. Deep-BFS high-degree nodes can be dimmed
                         // (unpickable by design) or capped out of the map.
                         let hub = -1, best = -1;
                         for (let i = 0; i < d.level.length; i++) {
                           if (d.level[i] !== 0) continue;
                           if ((d.degree[i] || 0) > best) {
                             best = d.degree[i] || 0; hub = i; }
                         }
                         if (hub < 0) return null;
                         const v = new d.THREE.Vector3(
                           d.pos[hub*3], d.pos[hub*3+1],
                           d.pos[hub*3+2]).project(d.camera);
                         if (v.z > 1 || v.z < -1) return null;
                         const r = d.renderer.domElement.getBoundingClientRect();
                         return { hub,
                                  sx: (v.x*0.5+0.5)*r.width + r.left,
                                  sy: (-v.y*0.5+0.5)*r.height + r.top }; }"""
                )
                pick = _hub_pick()
                vp = page.viewport_size
                _diag = page.evaluate(
                    """() => { const d = window.__dbg;
                         return { lay: !!d.mapLayout, req: d.mapCenterReq,
                                  pulse: d.mapPulse ? d.mapPulse.i : null,
                                  rects: d.mapRects ? d.mapRects.length : -1 }; }"""
                )
                if pick is not None and 0 <= pick["sx"] < vp["width"] and 0 <= pick["sy"] < vp["height"]:
                    _pre = page.evaluate(
                        """() => { const d = window.__dbg;
                             return { vis: d.mapPane.canvas.offsetWidth > 0,
                                      req0: d.mapCenterReq }; }"""
                    )
                    page.mouse.click(pick["sx"], pick["sy"])
                    page.wait_for_timeout(400)
                    aim = page.evaluate(
                        """() => { const d = window.__dbg;
                             if (!d.mapLayout) return { hit: false, lay: false };
                             const want = d.mapPulse ? d.mapPulse.i : d.mapCenterReq;
                             const rc = d.mapRects.find(r => r.i === want);
                             if (!rc) return { hit: false, want,
                                               rects: d.mapRects.length,
                                               ixs: d.mapRects.map(r => r.i).slice(0, 45) };
                             const pane = d.mapPane.canvas;
                             const cx = (rc.x + rc.w/2 - d.mapPX) * d.mapZ;
                             const cy = (rc.y + rc.h/2 - d.mapPY) * d.mapZ;
                             return { hit: true, pulse: !!d.mapPulse,
                                      off: Math.hypot(cx - pane.clientWidth/2,
                                                      cy - pane.clientHeight/2) }; }"""
                    )
                    aim["_pre"] = _pre
                    aim["_pick"] = pick
                    check("3d node click centers its map box",
                          aim.get("hit") and aim["off"] <= 18, str(aim))
                    check("center click starts the amber pulse",
                          aim.get("hit") and aim["pulse"] is True, str(aim))
                # pane collapsed -> request refused, nothing deferred
                page.click("#bMap")
                page.wait_for_timeout(150)
                page.mouse.click(pick["sx"], pick["sy"])
                page.wait_for_timeout(150)
                req_closed = page.evaluate("() => window.__dbg.mapCenterReq")
                check("collapsed pane: center request refused",
                      req_closed == -1, str(req_closed))
                page.click("#bMap")   # reopen for later sections
                page.wait_for_timeout(200)
                # pulse lifecycle: gone after MAP_PULSE_MS (900) + margin
                page.wait_for_timeout(1300)
                pulse_gone = page.evaluate("() => window.__dbg.mapPulse")
                check("selection pulse clears after its lifetime",
                      pulse_gone is None, str(pulse_gone))
            # a wild pan must clamp the window inside the world, never
            # strand the layout off-screen
            page.mouse.move(mcx, mcy)
            page.mouse.down()
            page.mouse.move(mcx - 2000, mcy - 2000, steps=8)
            page.mouse.up()
            page.wait_for_timeout(400)
            vw = page.evaluate("""() => ({ px: window.__dbg.mapPX,
                py: window.__dbg.mapPY, z: window.__dbg.mapZ })""")
            check("pan clamps the window inside the world",
                  vw["px"] >= 0 and vw["py"] >= 0, str(vw))
            # restore defaults so later sections see the stock state
            page.evaluate("""() => { const c = [...document.querySelectorAll('#dirs .chip')]
                .find(x => x.textContent.trim() === 'tests'); c.click(); }""")
            page.evaluate("() => { const d = document.getElementById('depth');"
                          " d.value = 2; d.dispatchEvent(new Event('input')); }")
            page.wait_for_timeout(400)
            page.fill("#search", tok)
            page.dispatch_event("#search", "input")
            page.wait_for_timeout(500)
            page.evaluate("() => document.getElementById('bMap').click()")  # collapse pane
            page.keyboard.press("Escape")

        # 4-pre2 (tail). trunk click parity (user r5: colored bus wires
        # were clickable, the white trunk conduits were not): every
        # served bus element must resolve to the SAME rider-card
        # affordance. Stateful probe — runs LAST so its focus/camera
        # perturbations land after every other assertion.
        enter_focus_via_row()
        page.wait_for_timeout(1500)
        trk = page.evaluate(
            """() => { const d = window.__dbg;
                 const el = d.renderer.domElement, r = el.getBoundingClientRect();
                 for (let x = 16; x < r.width; x += 24)
                   for (let y = 16; y < r.height; y += 24) {
                     const m = d.pickWireMeta({ clientX: r.left + x,
                                               clientY: r.top + y });
                     if (m && m.kind === 'trunk')
                       return { sx: r.left + x, sy: r.top + y,
                                desc: d.wireDesc(m) };
                   }
                 return null; }"""
        )
        check("served trunks are pickable at default-cam focus",
              bool(trk), (str(trk)[:80] if trk else "no trunk pick in scan"))
        if trk:
            page.evaluate(
                """(s) => { const el = window.__dbg.renderer.domElement;
                     const o = { clientX: s.sx, clientY: s.sy, bubbles: true };
                     el.dispatchEvent(new PointerEvent('pointermove', o));
                     el.dispatchEvent(new PointerEvent('pointerdown', o));
                     el.dispatchEvent(new PointerEvent('pointerup', o));
                     el.dispatchEvent(new MouseEvent('click', o)); }""", trk)
            page.wait_for_timeout(300)
            card = page.evaluate(
                """() => { const t = document.getElementById('wireTip');
                     return t && t.style.display === 'block' ? t.textContent : ''; }"""
            )
            check("trunk click opens the rider card",
                  card.startswith("\U0001F68C bus ") and "→" in card, card[:80])
        # artifact: screenshot of the focused fn-layer state
        page.screenshot(path=str(SHOTS / "last_run.png"), scale="css", type="png")
        print("artifact: .tmp/shots/last_run.png")
        browser.close()

    print(f"\n{n_files} files indexed · {len(FAILURES)} failure(s)")
    if FAILURES:
        print("FAILED:", ", ".join(FAILURES))
        sys.exit(1)
    print("ALL TESTS PASS")


if __name__ == "__main__":
    main()


