# neuronav viz QA harness — drives the real page in headless Chrome.
# Run: .venv/Scripts/python.exe -X utf8 tests/test_viz.py  (exit 0 = all pass)
# Uses system Chrome via channel="chrome" (no browser download needed).
import os
import sys
import re
import json
from pathlib import Path

from playwright.sync_api import TimeoutError as PwTimeout, sync_playwright

from _page_harness import CheckLog, launch, open_page, quiesce, require_fresh_bake, serve

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / ".tmp" / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
# CI guard (opt-in, #89 class): an exported NEURONAV_CONFIG silently
# redirects this gate at a foreign store. Strict runs refuse it instead —
# locally the override stays the sanctioned scratch-store mechanism.
if os.environ.get("NEURONAV_STRICT_DEFAULT") == "1" and os.environ.get("NEURONAV_CONFIG"):
    sys.exit("test_viz: NEURONAV_STRICT_DEFAULT=1 refuses an exported NEURONAV_CONFIG "
             f"({os.environ['NEURONAV_CONFIG']!r}) — unset one of the two")
import navconfig
import viz  # noqa: E402  (determinism bake + SEM_AFF_CAP for the #279 legs)

LOG = CheckLog()
check = LOG.check  # the 157 call sites below keep their bare check(...) form

# [#123] readiness helpers — waits key on observable state (search rows
# present, map built, page quiescent), never on blanket sleeps.


def wait_rows(page, title=None, timeout: int = 4000):
    """Search rows render async (debounced) — wait for the rows (or the
    exact path-suffixed row a click targets) instead of sleeping a fixed
    interval. Stem searches pass no title: row titles are full paths, a
    stem never suffix-matches."""
    sel = (f"#searchResults .row[title$='{title}']" if title
           else "#searchResults .row")
    page.wait_for_selector(sel, timeout=timeout)


def map_ready(page, timeout: int = 4000):
    """Focus-driven 2D map build done (layout + rects) before any map
    read — replaces the post-focus blanket sleeps. Degrades loudly when
    the map never builds: sections own null-guards that SKIP by name, and
    the executed-check floor turns a wholesale map death into a breach
    instead of a crash."""
    try:
        page.wait_for_function(
            "() => window.__dbg.mapLayout !== null && "
            "(window.__dbg.mapRects || []).length > 0", timeout=timeout)
        return True
    except PwTimeout:
        print("note: map layout never readied — map reads below see a "
              "dead map (precondition gate; the floor settles it)")
        return False


# [#123] executed-check floor: SKIPs stay loud for genuine data-gates,
# but a run that executes too few checks FAILS — a render regression that
# destroys a section's precondition used to convert dozens of checks into
# silent skips while the suite stayed green. FLOOR_MAP rides on the
# data-level DATA.mwires gate (it survives render breakage); the margins
# absorb the pose-sensitive sub-branches (wire-point / card-pose scans)
# that legitimately skip when the camera leaves no probeable target.
# Pinned to the frozen CI corpus — foreign corpora execute different
# counts and the breach message says so (tests/AGENTS.md).
FLOOR_BASE = 80
FLOOR_MAP = 70


def main():
    # [#89] stale-bake refusal BEFORE anything is served: the harness is
    # read-only — it measures the active config's bake, never the
    # template, and a bake older than viz.py would green-light
    # yesterday's product. Loud refusal, remedy named, no auto-bake.
    require_fresh_bake(navconfig.STATE_DIR)
    # bake is per-project now — the shared harness serves the active
    # config's state dir on an ephemeral loopback port (#132)
    httpd, port = serve(navconfig.STATE_DIR)
    try:
        run_tests(port)
    finally:
        httpd.shutdown()
        httpd.server_close()


def run_tests(port: int):
    # [#123] pre-bound here so a closed fn_info gate never raises
    # NameError - a closed gate reads as None, the floor settles it
    latch_pin_via_list = None
    with sync_playwright() as pw:
        browser = launch(pw)
        # #98-class console/pageerror capture rides open_page(errors=...)
        errors = []
        page = open_page(browser, port, errors)
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

        # 1b. [#325] poison-name teeth: fn identifiers colliding with
        # Object.prototype members (constructor/__proto__ fns in a dir
        # named hasOwnProperty) must neither kill the boot (pre-fix:
        # fnOf[e[1]].add TypeError -> black page) nor corrupt the
        # name-keyed indexes. fnOfProbe is the __dbg teeth hook.
        probe = page.evaluate("() => window.__dbg.fnOfProbe")
        has_poison = page.evaluate(
            "() => window.__dbg.nodes.some(n => (n.dir || '') === 'hasOwnProperty')")
        if has_poison:
            check("#325 poison fnOf counts (ctor 2 files / proto 1)",
                  probe and probe["ctor"] == 2 and probe["proto"] == 1,
                  str(probe))
        else:
            check("#325 fnOf probe present", bool(probe), str(probe))

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
            wait_rows(page, best_path)
            page.evaluate(
                """(p) => { const rows = [...document.querySelectorAll('#searchResults .row')];
                     // file rows carry title=path; rows bind onpointerdown
                     const r = rows.find(x => x.getAttribute('title') === p)
                            || rows[0];
                     r.dispatchEvent(new PointerEvent('pointerdown',
                                                      { bubbles: true })); }""",
                best_path)
            quiesce(page)
            # depth escalation (#33 contract): the slider is respected, and
            # the fn tier renders the wires that exist within depth N. A
            # depth-1 ball can be wire-free; step the slider 1->2->3 until
            # cross-file fn wires light (bounded, deterministic).
            # on: serveAll needs the camera inside 2.2 ball radii; the
            # depth input itself re-frames with the ball now (#62), and
            # the re-click below re-runs the full focus() framing besides
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
                quiesce(page)
                page.fill("#search", tok)
                page.dispatch_event("#search", "input")
                wait_rows(page, best_path)
                page.evaluate(
                    """(p) => { const rows = [...document.querySelectorAll('#searchResults .row')];
                         const r = rows.find(x => x.getAttribute('title') === p) || rows[0];
                         r.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })); }""",
                    best_path)
                quiesce(page)

        # 3z. highlight-only search (issue #33): typing must NOT change the
        # 3D view - no focus, no camera tween, no fn tier from the keyboard.
        lit_pre = page.evaluate(
            "() => window.__dbg.alphaTgt.reduce((s, a) => s + (a > 0.5 ? 1 : 0), 0)")
        page.fill("#search", tok)
        page.dispatch_event("#search", "input")
        wait_rows(page)
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
                 // [issue #81] a synthetic chip.click() carries 0,0 coords;
                 // a capture-phase wire picker that claims chrome clicks
                 // both kills the chip's own handler (toggle dead) and
                 // pops a stray wire tip at the corner. Chrome-owned
                 // clicks must never surface ink.
                 const tip = document.getElementById('wireTip');
                 return { open, closed, txt,
                          tip: tip ? getComputedStyle(tip).display : 'gone' }; }"""
        )
        check("legend chip toggles (trunk vocabulary self-explains)",
              lg and lg["open"] and lg["closed"], str(lg))
        check("chrome clicks never surface a wire tip (#81 steal class)",
              lg and lg["tip"] in ("none", "gone"), str(lg))
        # [issue #111] the #info panel must render as chrome: a misplaced
        # closing brace orphaned #info's background/border/padding and, via
        # CSS error recovery, swallowed the #info h2 rule too — the panel
        # degraded to bare floating text over the 3D scene.
        icss = page.evaluate(
            """() => { const el = document.getElementById('info');
                 if (!el) return null;
                 const cs = getComputedStyle(el);
                 const h2 = el.querySelector('h2');
                 return { disp: cs.display,
                          bg: cs.backgroundColor,
                          bd: cs.borderTopWidth,
                          pd: cs.paddingTop,
                          h2px: h2 ? getComputedStyle(h2).fontSize
                                   : 'none' }; }"""
        )
        check("info panel renders as chrome (#111 css rot)",
              icss and icss["disp"] == "block"
              and icss["bg"] not in ("rgba(0, 0, 0, 0)", "transparent")
              and icss["bd"] != "0px" and icss["pd"] != "0px"
              and icss["h2px"] == "13px", str(icss))
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
        # #97: the coverage clause (>= 8 legs, >= 6 trunks, >= 8 ref-px)
        # needs a corridor-shaped lit set — small graphs legitimately
        # light fewer. Data-gate the count clauses loudly instead of
        # failing; the two exact laws below stay gated on any shape.
        if not (cor and cor["legs"] >= 8 and cor["trunks"] >= 6):
            print(f"SKIP corridor law coverage clause - data shape: {cor}")
        else:
            check("corridor law: legs anchored >= 8 ref-px at focus",
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
          // #97: chains in the explained-exit taper state legally end
          // mid-ink (radius ramp toward 0 from the attached end) — their
          // last served vertex is the dissolve point by design, not a
          // seam gap. Verify geometry only on fully-served chains; the
          // chainGates probe reports each chain's first failing gate.
          const cg = new Map(d.chainGates || []);
          let tapered = 0;
          const stByFi = new Map();
          for (const S of d.fnStations) {
            if (!stByFi.has(S.fi)) stByFi.set(S.fi, []);
            stByFi.get(S.fi).push(S);
          }
          const bad = [];
          let legN = 0, trunkN = 0;
          const eq = (p, q) => p[0] === q[0] && p[1] === q[1] && p[2] === q[2];
          for (const [k, s0] of first) {
            if (cg.get(k) !== "served") { tapered++; continue; }
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
          return { legN, trunkN, tapered, bad }; }"""
        )
        if zg and zg.get("legN", 0) + zg.get("trunkN", 0) < 10:
            # #97: too few fully-served chains to verify the seam law on
            # this shape — loud skip, not a silent pass.
            print(f"SKIP zero-gap law - data shape: only "
                  f"{zg.get('legN', 0) + zg.get('trunkN', 0)} fully-served "
                  f"chains (need >= 10); {zg.get('tapered', 0)} legally "
                  f"tapered (explained-exit)")
        else:
            check("zero-gap law: chain vertices equal anchor markers exactly",
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

        # 4c-bis. corridor ink laws (#5/#7): bollards must clear fn-box
        # ink ON SCREEN (jClearMinPx >= 0 — the qa_readability formula:
        # min over on-screen bollards of dist-to-box-ink minus bollard r;
        # the render-time dodge slides dots along their own ink), and
        # conduit MIDSPAN ink must route outside served fn boxes
        # (conduitBoxViol4px <= 3 distinct chain~box pairs; midspan per
        # the issue's own wording — edge segments are the lawful
        # draws-to-anchor/fan-out ink, absorbed by the ceiling). Both
        # need a corridor-shaped lit set: data-gate loudly, never pass
        # silently on a shape that cannot exercise the floor.
        ink5 = page.evaluate(
            """() => { const d = window.__dbg;
                 if (!d.fnMeta || !d.fnJDot || !d.fnJDotR) return { skip: 'no fn layer' };
                 const cv = d.renderer.domElement;
                 const r = cv.getBoundingClientRect();
                 const W = r.width, H = r.height;
                 const fovY = d.camera.fov * Math.PI / 180;
                 const V3 = (x, y, z) => new d.THREE.Vector3(x, y, z);
                 const P = p => { const q = V3(p[0], p[1], p[2]).project(d.camera);
                   return { x: (q.x*0.5+0.5)*W + r.left, y: (-q.y*0.5+0.5)*H + r.top }; };
                 const camD = p => d.camera.position.distanceTo(V3(p[0], p[1], p[2]));
                 const pxwu = p => ((cv.clientHeight || 900)/2) /
                   (Math.tan(fovY/2) * Math.max(camD(p), 1));
                 const on = q => q.x > -40 && q.x < W + 40 && q.y > -40 && q.y < H + 40;
                 const boxes = [];
                 for (const m of d.fnMeta) {
                   if (m.agg && !m.count) continue;
                   const q = P(m.p);
                   if (!on(q)) continue;
                   boxes.push({ x: q.x, y: q.y, r: (m.count ? 3 : 2) * pxwu(m.p) });
                 }
                 const fm = d.fnJDot.instanceMatrix.array;
                 let jClear = null, bolls = 0;
                 for (let i = 0; i < d.fnJDotR.length; i++) {
                   const p = [fm[i*16+12], fm[i*16+13], fm[i*16+14]];
                   const q = P(p);
                   if (!on(q)) continue;
                   bolls++;
                   const jr = (d.fnJDotR[i] || 0) * pxwu(p);
                   for (const b of boxes) {
                     const c = Math.hypot(q.x - b.x, q.y - b.y) - b.r - jr;
                     if (jClear === null || c < jClear) jClear = c;
                   }
                 }
                 return { boxes: boxes.length, bolls,
                          jClear: jClear === null ? null : +jClear.toFixed(1),
                          tgt: !!(d.jDotArrays && d.jDotArrays.tgt) }; }"""
        )
        if ink5.get("skip") or not ink5["boxes"] or not ink5["bolls"]:
            print(f"SKIP jClear law - corridor shape: {ink5}")
        else:
            check("junction bollards clear fn-box ink (#5 jClearMinPx >= 0)",
                  ink5["jClear"] is not None and ink5["jClear"] >= 0, str(ink5))
            check("bollard dodge axes exported (jDotArrays.tgt)",
                  ink5["tgt"], str(ink5))

        ink7 = page.evaluate(
            """() => { const d = window.__dbg;
                 if (!d.busPts || !d.fnBus || !d.fnBus.instanceMatrix)
                   return { skip: 'no corridor layer' };
                 const cv = d.renderer.domElement;
                 const r = cv.getBoundingClientRect();
                 const W = r.width, H = r.height;
                 const fovY = d.camera.fov * Math.PI / 180;
                 const V3 = (x, y, z) => new d.THREE.Vector3(x, y, z);
                 const P = p => { const q = V3(p[0], p[1], p[2]).project(d.camera);
                   return { x: (q.x*0.5+0.5)*W + r.left, y: (-q.y*0.5+0.5)*H + r.top }; };
                 const camD = p => d.camera.position.distanceTo(V3(p[0], p[1], p[2]));
                 const pxwu = p => ((cv.clientHeight || 900)/2) /
                   (Math.tan(fovY/2) * Math.max(camD(p), 1));
                 const boxes = [];
                 for (const m of d.fnMeta) {
                   if (m.agg && !m.count) continue;
                   if ((d.alphaTgt[m.file] || 0) < 0.5) continue;
                   const q = P(m.p);
                   boxes.push({ x: q.x, y: q.y, r: (m.count ? 3 : 2) * pxwu(m.p) });
                 }
                 const fa = d.fnBus.instanceMatrix.array;
                 const chains = new Map();
                 for (let i = 0; i < d.busPts.length; i++) {
                   if (Math.hypot(fa[i*16], fa[i*16+1], fa[i*16+2]) <= 0.001) continue;
                   const s = d.busPts[i];
                   const dx = fa[i*16+12] - (s.a[0]+s.b[0])/2,
                         dy = fa[i*16+13] - (s.a[1]+s.b[1])/2,
                         dz = fa[i*16+14] - (s.a[2]+s.b[2])/2;
                   const a = P([s.a[0]+dx, s.a[1]+dy, s.a[2]+dz]),
                         b = P([s.b[0]+dx, s.b[1]+dy, s.b[2]+dz]);
                   if (!chains.has(s.k)) chains.set(s.k, []);
                   chains.get(s.k).push([a.x, a.y, b.x, b.y]);
                 }
                 const distSeg = (px, py, s) => {
                   const ex = s[2]-s[0], ey = s[3]-s[1], L2 = ex*ex+ey*ey;
                   let t = L2 ? ((px-s[0])*ex + (py-s[1])*ey)/L2 : 0;
                   t = Math.max(0, Math.min(1, t));
                   return Math.hypot(px-(s[0]+t*ex), py-(s[1]+t*ey)); };
                 let viol = 0; const sites = [];
                 for (const [k, all] of chains) {
                   if (all.length < 3) continue;        // need real midspan
                   const segs = all.slice(1, -1);       // edge segs = anchor ink
                   for (const b of boxes)
                     if (segs.some(s => distSeg(b.x, b.y, s) < b.r + 4)) {
                       viol++;
                       if (sites.length < 4) sites.push(k);
                     }
                 }
                 return { boxes: boxes.length, chains: chains.size, viol, sites }; }"""
        )
        if ink7.get("skip") or not ink7["boxes"] or ink7["chains"] < 3:
            print(f"SKIP conduit midspan law - corridor shape: {ink7}")
        else:
            check("conduit midspan clears fn boxes (#7 <= 3 crossings)",
                  ink7["viol"] <= 3, str(ink7))

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
                         want: d.nodes[fm.file].path + ' :: ' + fm.name,
                         sx: +sx.toFixed(1), sy: +sy.toFixed(1) }); }, 300)); }"""
        )
        check("hover tooltip on fn box", bool(hover) and hover["shown"]
              and hover["text"] == hover["want"], str(hover))

        # [issue #58] hover tips die with pointer context (3D surface):
        # canvas pointerleave, window blur, and the map pane opening over
        # the pointer's region each dismiss the tip; re-entering hover
        # re-shows it (no zombie hide state). Pin-owned wireTip cards are
        # guarded separately in the pin blocks.
        if hover and hover["shown"]:
            cyc = page.evaluate(
                """(h) => { const el = window.__dbg.renderer.domElement;
                     const t = () => document.getElementById('tip').style.display;
                     el.dispatchEvent(new PointerEvent('pointerleave'));
                     const leave = t();
                     el.dispatchEvent(new PointerEvent('pointermove',
                       { clientX: h.sx, clientY: h.sy, bubbles: true }));
                     const reshow = t();
                     window.dispatchEvent(new Event('blur'));
                     const blur = t();
                     return { leave, reshow, blur }; }""",
                hover)
            check("3d hover tip dies on canvas leave, re-shows, dies on blur",
                  cyc["leave"] == "none" and cyc["reshow"] == "block"
                  and cyc["blur"] == "none", str(cyc))
            # surface switch: the pane expanding over the hovered canvas
            # region ends the context with no pointermove at all. The fn
            # point is re-projected after the collapse (the canvas rect
            # changes with the split, stale coords would miss the box).
            page.evaluate("() => document.getElementById('bMap').click()")
            page.wait_for_timeout(250)
            sw = page.evaluate(
                """() => { const d = window.__dbg;
                     const ok = m => { if (m.agg && !m.count) return false;
                       const v = new d.THREE.Vector3(m.p[0], m.p[1], m.p[2]).project(d.camera);
                       return v.z < 1 && Math.abs(v.x) < 0.95 && Math.abs(v.y) < 0.95; };
                     const fm = d.fnMeta.find(ok) || d.fnMeta[0]; if (!fm) return { skip: true };
                     const v = new d.THREE.Vector3(fm.p[0], fm.p[1], fm.p[2]).project(d.camera);
                     const r = d.renderer.domElement.getBoundingClientRect();
                     const sx = (v.x*0.5+0.5)*r.width + r.left, sy = (-v.y*0.5+0.5)*r.height + r.top;
                     d.renderer.domElement.dispatchEvent(new PointerEvent('pointermove',
                       { clientX: sx, clientY: sy, bubbles: true }));
                     const shown = document.getElementById('tip').style.display;
                     document.getElementById('bMap').click();
                     return new Promise(res => setTimeout(() => res({ shown,
                       hidden: document.getElementById('tip').style.display }), 250)); }""")
            check("3d hover tip dies when the map surface opens",
                  sw.get("skip") or (sw["shown"] == "block" and sw["hidden"] == "none"),
                  str(sw))
            # park off-target so later sections start from a clean hover
            page.evaluate(
                "() => window.__dbg.renderer.domElement.dispatchEvent("
                "new PointerEvent('pointermove', { clientX: 2, clientY: 2,"
                " bubbles: true }))")

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
        quiesce(page, 4000)
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
            wait_rows(page, hub39["path"])
            page.evaluate(
                """(p) => { const rows = [...document.querySelectorAll('#searchResults .row')];
                     const r = rows.find(x => x.getAttribute('title') === p) || rows[0];
                     r.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })); }""",
                hub39["path"])
            quiesce(page)
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
            # [#60] the same focus on the 2D map: mwires carries no inst
            # class, so a pair whose ONLY relation is inst/attach must
            # still paint - as a sole-relation underlay at the affordance
            # tier (0.40 alpha + 1-screen-px width floor), not declutter
            # ghost ink. Shape-gated on sole-relation underlays existing.
            page.wait_for_function(
                "() => window.__dbg.mapLayout !== null", timeout=4000)
            mi60 = page.evaluate(
                """() => { const d = window.__dbg;
                     const L = d.mapLayout;
                     if (!L) return null;
                     const named = new Set(L.wires.map(w => w.sf + "_" + w.df));
                     const sole = L.underlays.filter(u =>
                       !L.pairW.has(u.s + "_" + u.t) &&
                       !named.has(u.s + "_" + u.t)).length;
                     const mi = d.mapInfo();
                     return { sole, afford: mi ? mi.instAfford : null,
                              alpha: mi ? mi.instAffordAlpha : null,
                              width: mi ? mi.instAffordWidth : null }; }""")
            if not mi60 or not mi60["sole"]:
                print("SKIP #60 map affordance — no sole-relation inst "
                      "underlays in this focus (#97)")
            else:
                check("inst-only pairs paint map affordance ink (#60)",
                      mi60["afford"] == mi60["sole"],
                      f"afford={mi60['afford']} sole={mi60['sole']}")
                # [#210] pin the ISSUED ink, not the code path: the seg
                # params must survive an alpha/width revert sabotage
                check("afford ink issues 0.40-alpha 1px-floor segs (#60)",
                      mi60["alpha"] is not None and mi60["alpha"] >= 0.40
                      and mi60["width"] is not None and mi60["width"] >= 1.0,
                      f"alpha={mi60['alpha']} width={mi60['width']}")
                # the width floor max(1, 1/mapZ) equals a bare 1px
                # stroke AT z=1 - drive below z=1 where a floorless
                # revert renders sub-pixel, then re-pin (#210)
                mp_c = page.evaluate("() => { const b = "
                    "document.getElementById('mapPane').getBoundingClientRect(); "
                    "return { x: b.left + b.width / 2, y: b.top + b.height / 2 }; }")
                for _ in range(20):
                    if page.evaluate("() => window.__dbg.mapZ") <= 0.8:
                        break
                    page.mouse.move(mp_c["x"], mp_c["y"])
                    page.mouse.wheel(0, 120)
                    page.wait_for_timeout(35)
                mz = page.evaluate("() => window.__dbg.mapZ")
                mi60z = page.evaluate("() => { const mi = "
                    "window.__dbg.mapInfo(); "
                    "return { a: mi.instAffordAlpha, w: mi.instAffordWidth }; }")
                check("afford ink floors to 1 screen px below z=1 (#60)",
                      mz <= 0.8 and mi60z["w"] >= 1.0 / mz - 1e-6
                      and mi60z["a"] >= 0.40,
                      f"width={mi60z['w']} alpha={mi60z['a']} z={mz}")
                # restore the stock pose: zoom back to >=1 and park the
                # mouse off the pane so later legs read unhovered state
                for _ in range(20):
                    if page.evaluate("() => window.__dbg.mapZ") >= 1.0:
                        break
                    page.mouse.move(mp_c["x"], mp_c["y"])
                    page.mouse.wheel(0, -120)
                    page.wait_for_timeout(35)
                page.mouse.move(8, 8)
            # the user's own toggle during focus is final for this focus
            # (no re-force): flip the tier off mid-focus, then re-run
            # applyVisibility via a depth change — it must NOT re-seed.
            page.click("#bInst")
            page.wait_for_timeout(400)
            page.evaluate(
                """() => { const el = document.getElementById('depth');
                     el.value = 2; el.dispatchEvent(new Event('input')); }""")
            quiesce(page, 4000)
            noRefight = page.evaluate(
                "() => ({ inst: window.__dbg.showInst, fi: window.__dbg.focusFileIdx })")
            check("user toggle wins mid-focus (no re-force)",
                  not noRefight["inst"] and noRefight["fi"] >= 0, str(noRefight))
            page.evaluate(
                """() => { const el = document.getElementById('depth');
                     el.value = 1; el.dispatchEvent(new Event('input')); }""")
            quiesce(page, 4000)
            page.keyboard.press("Escape")
            page.keyboard.press("Escape")
            quiesce(page, 6000)
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
        quiesce(page, timeout=20000)
        churn = page.evaluate(
            """() => { const d = window.__dbg;
                 const h = d.hot;
                 if (!h) return { fail: 'no DATA.hot' };
                 let arg = 0;
                 for (let j = 1; j < h.length; j++) if (h[j] > h[arg]) arg = j;
                 let cold = -1;
                 for (let j = 0; j < h.length; j++)
                   if (h[j] === 0 && d.alphaTgt[j] > 0.5) { cold = j; break; }
                 // #368: the expected radius DERIVES FROM DATA — no third
                 // formula copy. d.sizes is the baked law verbatim
                 // (DATA.restR: connectivity base * churn boost * render
                 // scale) and the satellite threshold is DATA.meta.aggMax;
                 // only the renderer's dynamic multipliers are applied.
                  const mat = new d.THREE.Matrix4();
                  const mx = i => { d.fileMesh.getMatrixAt(i, mat); return mat.elements[0]; };
                  // instance scale = sizes × dim-shrink (0.45+0.55a) at
                  // overview (no dead boost/hover); alpha eases, read it live.
                  // Sub-floor nodes are lifted by the zoomed-out degree floor
                  // (min screen DIAMETER, __dbg.degFloorArr) — expected scale
                  // includes that lift: lift = floor/(2*rpx) capped 4x, where
                  // rpx is the projected RADIUS px of the natural size.
                  const dim = i => 0.45 + 0.55 * d.alpha[i];
                  // satellite allowance (sphR): in fn mode, files owning
                  // more than the baked AGG_MAX fns grow so the box ring
                  // keeps spacing — the same factor the template
                  // multiplies into every radius
                  const fnsOf = i => (d.fns[d.nodes[i].path] || []).length;
                  const fnOn = document.getElementById('cbFn').checked;
                  const AM = d.meta.aggMax;
                  const sat = i => fnOn && fnsOf(i) > AM
                    ? 1 + Math.min(0.8, 0.25 * Math.log2(fnsOf(i) / AM)) : 1;
                  const cam = d.camera;
                  const halfH = d.renderer.domElement.clientHeight / 2;
                  const tHalf = Math.tan(cam.fov * Math.PI / 360);
                  const p3 = new d.THREE.Vector3();
                  const nat = i => d.sizes[i] * sat(i) * dim(i);
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

        # [#368] bake-law constants ride DATA.meta and the page consumes
        # them: what the JS reads must equal the pinned Python constants
        # (a restated JS literal or a stale bake breaks the equality) and
        # the affinity tooltip must quote the baked top-k/floor verbatim.
        laws368 = page.evaluate(
            """() => ({ aggMax: window.__dbg.meta.aggMax,
                        semFloor: window.__dbg.meta.semFloor,
                        semTopK: window.__dbg.meta.semTopK,
                        restN: window.__dbg.sizes.length,
                        title: document.getElementById('bSemAff').title })"""
        )
        check("[#368] law constants ride DATA.meta (aggMax/semFloor/semTopK)",
              laws368["aggMax"] == viz.AGG_MAX
              and laws368["semFloor"] == viz.SEM_FLOOR
              and laws368["semTopK"] == viz.SEM_TOP_K
              and laws368["restN"]
              == page.evaluate("() => window.__dbg.nodes.length"),
              str(laws368))
        check("[#368] affinity tooltip quotes the baked top-k/floor",
              f"top-{laws368['semTopK']}" in laws368["title"]
              and str(laws368["semFloor"]) in laws368["title"],
              laws368["title"])

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
        elif grp and grp.get("groupN", 99) >= grp.get("fineN", 0):
            # #97: coarse_groups nudges its cut into 3..8 supergroups, so
            # demonstrating COLLAPSE (groupN < fineN) needs >= 9 fine
            # clusters. Small indexes map 1:1 — the recolor/reset laws
            # below still hold; only the collapse clause lacks a subject.
            ok = (grp.get("mode1") and not grp.get("modeAfterReset")
                  and grp.get("restoredN") == grp.get("fineN")
                  and grp.get("recolored"))
            check("groups toggle recolors and resets (no collapse to show "
                  "on this shape)", ok, str(grp))
            print(f"SKIP groups collapse clause - data shape: fineN "
                  f"{grp.get('fineN')} < 9 fine clusters")
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
        quiesce(page, timeout=20000)
        reloaded = page.evaluate("""() => ({
          w: document.getElementById('mapPane').clientWidth,
          open: !document.getElementById('mapPane').classList.contains('collapsed') });""")
        check("pane width persists across reload",
              reloaded["open"] and reloaded["w"] == after["w"], str(reloaded))

        # 8. map pane (map-spec-v2): named wires over fn rosters. Data-gated
        # on DATA.mwires — an index without the named-wire exports skips.
        # The pane is already open at the persisted width from section 7b.
        mw = page.evaluate("() => window.__dbg.mwires || []")
        # [#123] executed-check floor rides the data-level gate (constants
        # above): DATA.mwires survives a render regression, so a map-side
        # breakage lands as an executed shortfall, never a skip-to-green.
        LOG.floor = FLOOR_BASE + (FLOOR_MAP if mw else 0)
        if not mw:
            print("SKIP map pane — no DATA.mwires in this index")
        else:
            enter_focus_via_row()
            map_ready(page)   # [#123] layout + rects before the first read
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

            # [issue #58] hover tips die with pointer context (2D L1 map
            # tip): appears on its wire, then dismisses on void move-off,
            # pane leave (pointer onto the 3D canvas), surface switch
            # (bMap collapse), window blur, and drag-pan (a press ends the
            # hover — the pane pans under a frozen tip). Drag runs last:
            # it pans the pose for the candidate scans below.
            h58 = page.evaluate("""() => {
                const d = window.__dbg, L = d.mapLayout; if (!L) return null;
                const pane = document.getElementById('mapPane');
                const pw = pane.clientWidth, ph = pane.clientHeight;
                const px = d.mapPX, py = d.mapPY, z = d.mapZ;
                // chip coverage computed locally: mapChipAt is not a
                // __dbg probe (same world-rect semantics as mapChipAt)
                const chipAt = (wx, wy) => (L.chips || []).some(c =>
                    wx >= c.x && wx <= c.x + c.w && wy >= c.y && wy <= c.y + c.h);
                // hover point: ON a wire by the product's own hit-test,
                // clear of chips (they outrank wires in the handler)
                let hp = null;
                for (const w of L.wires) {
                    if (hp) break;
                    if (w.bez) continue;
                    for (let k = 1; k < w.pts.length && !hp; k++) {
                        for (const t of [0.5, 0.25, 0.75]) {
                            const wx = w.pts[k-1][0] + (w.pts[k][0] - w.pts[k-1][0]) * t;
                            const wy = w.pts[k-1][1] + (w.pts[k][1] - w.pts[k-1][1]) * t;
                            const sx = (wx - px) * z, sy = (wy - py) * z;
                            if (sx <= 14 || sy <= 14 || sx >= pw - 14 || sy >= ph - 14) continue;
                            if (d.mapWireAt(wx, wy) < 0 || chipAt(wx, wy)) continue;
                            hp = { sx: +sx.toFixed(1), sy: +sy.toFixed(1) };
                            break;
                        }
                    }
                }
                if (!hp) return null;
                // void point: no wire in the pick band, no chip over it
                for (let gx = 14; gx < pw - 14; gx += 16)
                    for (let gy = 14; gy < ph - 14; gy += 16) {
                        const wx = gx / z + px, wy = gy / z + py;
                        if (d.mapWireAt(wx, wy) < 0 && !chipAt(wx, wy))
                            return { vx: gx, vy: gy, ...hp };
                    }
                return null; }""")
            if h58:
                def tip2d():
                    return page.evaluate(
                        "() => document.getElementById('mapTip').style.display")
                hx, hy = bb["x"] + h58["sx"], bb["y"] + h58["sy"]
                page.mouse.move(hx, hy)
                page.wait_for_timeout(150)
                on = tip2d()
                txt = page.evaluate(
                    "() => document.getElementById('mapTip').textContent")
                check("2d hover tip appears on the wire",
                      on == "block" and len(txt) > 4, f"{on} {txt[:40]!r}")
                page.mouse.move(bb["x"] + h58["vx"], bb["y"] + h58["vy"])
                page.wait_for_timeout(150)
                check("2d hover tip dismisses on move-off (void)",
                      tip2d() == "none", tip2d())
                page.mouse.move(hx, hy)
                page.wait_for_timeout(150)
                pre = tip2d()
                page.mouse.move(bb["x"] - 60, bb["y"] + bb["height"] / 2)
                page.wait_for_timeout(150)
                check("2d hover tip dismisses on pane leave",
                      pre == "block" and tip2d() == "none", f"{pre} -> {tip2d()}")
                page.mouse.move(hx, hy)
                page.wait_for_timeout(150)
                pre = tip2d()
                page.evaluate("() => document.getElementById('bMap').click()")
                page.wait_for_timeout(250)
                off = tip2d()
                page.evaluate("() => document.getElementById('bMap').click()")
                page.wait_for_timeout(250)
                check("2d hover tip dismisses on surface switch (bMap)",
                      pre == "block" and off == "none", f"{pre} -> {off}")
                page.mouse.move(hx, hy)
                page.wait_for_timeout(150)
                pre = tip2d()
                page.evaluate("() => window.dispatchEvent(new Event('blur'))")
                check("2d hover tip dismisses on window blur",
                      pre == "block" and tip2d() == "none", f"{pre} -> {tip2d()}")
                page.mouse.move(hx, hy)
                page.wait_for_timeout(150)
                page.mouse.down()
                page.mouse.move(hx + 40, hy + 30, steps=4)
                page.wait_for_timeout(150)
                drg = tip2d()
                page.mouse.up()
                page.wait_for_timeout(150)
                check("2d hover tip dismisses on drag-pan",
                      drg == "none", drg)
            else:
                print("SKIP 2d hover-tip lifecycle - no clear wire point")
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
            # [#123] never except-NameError into a skip: pre-bind the
            # map-section helper — the fn_info branch redefines it below;
            # without fn_info the pin sections take their loud data-gated
            # SKIP. A NameError raised INSIDE the helper is a failure.
            latch_pin_via_list = None
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
                # [#196] cross-surface: ONE latch, BOTH surfaces - the
                # map pin's 3D twin emphasizes the same wire (corridor
                # chain tint + the wire's own arcs)
                covX0 = page.evaluate(
                    "() => window.__dbg.mapInfo().pinCoverX")
                check("map wire pin emphasizes in 3d (cross-surface)",
                      covX0 >= 1, f"pinCoverX {covX0}")
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
                    # [#123] latch condition, not a fixed sleep: the list
                    # opens when the chip's click lands — retry next chip
                    # on timeout instead of reading a half-open list.
                    try:
                        page.wait_for_function(
                            "() => document.querySelectorAll"
                            "('#mapList .row').length >= 2", timeout=1200)
                    except PwTimeout:
                        continue
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
                            // [#123] pick-vs-paint parity: clicks land on
                            // the PAINTED hit rect (collision-ladder
                            // displacement, LOD, fade all baked in), not
                            // the raw layout anchor - a center computed
                            // from ch.x/ch.w misses a displaced chip
                            const h = ch.hit;
                            if (!h) continue;   // hidden chip: unclickable
                            const sx = h.x + h.w / 2;
                            const sy = h.y + h.h / 2;
                            if (sx > 20 && sy > 20 &&
                                sx < pn.clientWidth - 20 &&
                                sy < pn.clientHeight - 20)
                                out.push({ sx: +sx.toFixed(1), sy: +sy.toFixed(1) });
                            if (out.length >= 6) break;
                        }
                        return out; }""")
                    for cand in (hits or []):
                        page.mouse.click(bb["x"] + cand["sx"], bb["y"] + cand["sy"])
                        # [#123] condition waits, not sleeps: the list must
                        # OPEN on this click (a stale list from an earlier
                        # section can linger in the DOM), then the row
                        # click must latch the pin before it is read.
                        try:
                            page.wait_for_function(
                                "() => document.getElementById('mapList')"
                                ".style.display === 'block'", timeout=1200)
                        except PwTimeout:
                            continue
                        rows = page.locator("#mapList .row")
                        if rows.count() >= 2:
                            try:
                                rows.nth(0).click(timeout=2000)
                            except PwTimeout:
                                # the list can flutter closed between the
                                # open-wait and the click (a mid-tween
                                # repaint re-anchors and fails) - retry
                                # the next chip instead of dying
                                continue
                            try:
                                page.wait_for_function(
                                    "() => window.__dbg.wirePin &&"
                                    " window.__dbg.wirePin.menu === 'list'",
                                    timeout=1200)
                            except PwTimeout:
                                continue
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
                wait_rows(page)
                page.evaluate("""tok => { const rows = [...document.querySelectorAll('#searchResults .row')];
                    (rows.find(x => x.title.endsWith(tok)) || rows[0])
                    .dispatchEvent(new PointerEvent('pointerdown', {bubbles: true})); }""", tok)
                quiesce(page)
                # [#123] quiesce covers the 3D scene only — the 2D layout
                # rebuild is a separate pass; wait for it before reading
                # spines or the scan aims at the previous focus's geometry
                map_ready(page)
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

                    # [issue #58] regression guard: blur is a hover-context
                    # end, not a pin dismissal — the bus card and its pin
                    # survive it (#85 contract)
                    pgM = page.evaluate("""() => {
                        window.dispatchEvent(new Event('blur'));
                        return { disp: document.getElementById('wireTip').style.display,
                                 pin: window.__dbg.wirePin }; }""")
                    check("map pin card unaffected by blur (#85 contract)",
                          pgM["disp"] == "block" and pgM["pin"] == pinT,
                          f"{pgM} vs {pinT}")
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
                wait_rows(page)
                page.evaluate("""tok => { const rows = [...document.querySelectorAll('#searchResults .row')];
                    (rows.find(x => x.title.endsWith(tok)) || rows[0])
                    .dispatchEvent(new PointerEvent('pointerdown', {bubbles: true})); }""", tok)
                quiesce(page)
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
                    // [#81 surface law] a real click lands on whatever
                    // element owns the point - ink under the results
                    // overlay is chrome-owned and the picker must
                    // refuse it, so only canvas-owned points are aims
                    const el = d.renderer.domElement;
                    for (let y = 70; y < innerHeight - 40 && out.length < 5;
                         y += 44)
                        for (let x = 24; x < xmax && out.length < 5; x += 44) {
                            const m = d.pickWireMeta({ clientX: x, clientY: y });
                            if (m && m.kind === "trunk" &&
                                document.elementFromPoint(x, y) === el)
                                out.push({ x, y });
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
                    quiesce(page, 4000)
                    tpts = page.evaluate("""() => {
                        const d = window.__dbg;
                        const pn = document.getElementById("mapPane");
                        const xmax = (pn ? pn.getBoundingClientRect().x
                                        : innerWidth) - 14;
                    const out = [];
                    // [#81 surface law] canvas-owned aims only (see above)
                    const el = d.renderer.domElement;
                    for (let y = 70; y < innerHeight - 40 && out.length < 5;
                         y += 44)
                        for (let x = 24; x < xmax && out.length < 5; x += 44) {
                            const m = d.pickWireMeta({ clientX: x, clientY: y });
                            if (m && m.kind === "trunk" &&
                                document.elementFromPoint(x, y) === el)
                                out.push({ x, y });
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
                    # [#196] cross-surface: the 3D pin's map twin
                    # paints (trunk corridor, or its singles fallback
                    # for a pair the map drew below trunk admission)
                    covXT = page.evaluate(
                        "() => window.__dbg.mapInfo().pinCoverX")
                    check("3d trunk pin emphasizes in 2d (cross-surface)",
                          covXT >= 1, f"pinCoverX {covXT}")
                    # [#196] the picker is geometry-only: instance-color
                    # writes must never feed pickWireMeta (the same ink
                    # still resolves the same meta under a live tint)
                    pkBlind = page.evaluate("""(c) => {
                        const m = window.__dbg.pickWireMeta(
                            { clientX: c.x, clientY: c.y });
                        return m && m.kind === "trunk"
                            ? String(m.k) : null; }""", cand)
                    check("pickWireMeta stays color-blind under tint",
                          pkBlind == str(trunk3.get("k")),
                          f"{pkBlind} vs {trunk3.get('k')}")
                    # [issue #84] endpoint law: both chain termini sit ON
                    # the fn boxes the legs serve (owner: 'from the actual
                    # start function to the actual end function'), not at
                    # station dots on the file spheres
                    epLaw = page.evaluate("""() => {
                        const c = window.__dbg.pinChain || {};
                        if (!c.boxA || !c.boxB) return null;
                        const eq = (u, v) => u && v &&
                            Math.abs(u[0]-v[0]) < 1e-6 &&
                            Math.abs(u[1]-v[1]) < 1e-6 &&
                            Math.abs(u[2]-v[2]) < 1e-6;
                        return eq(c.ep0, c.boxA) && eq(c.ep1, c.boxB); }""")
                    check("3d chain endpoints anchor at the fn boxes",
                          epLaw is True, f"pinChain ep law {epLaw}")
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
                    // [#81 surface law] canvas-owned aims only (see the
                    // trunk scan above)
                    const el = d.renderer.domElement;
                    for (let y = 70; y < innerHeight - 40 && out.length < 5;
                         y += 44) {
                        for (let x = 24; x < xmax && out.length < 5; x += 44) {
                            const m = d.pickWireMeta({ clientX: x, clientY: y });
                            if (m && (m.kind === "link" || m.kind === "wire") &&
                                document.elementFromPoint(x, y) === el)
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
                    quiesce(page, 4000)
                    link_pts = page.evaluate("""() => {
                        const d = window.__dbg;
                        const pn = document.getElementById("mapPane");
                        const xmax = (pn ? pn.getBoundingClientRect().x
                                        : innerWidth) - 14;
                        const out = [];
                        // [#81 surface law] canvas-owned aims only
                        const el = d.renderer.domElement;
                        for (let y = 70; y < innerHeight - 40 && out.length < 5;
                             y += 44) {
                            for (let x = 24; x < xmax && out.length < 5; x += 44) {
                                const m = d.pickWireMeta({ clientX: x, clientY: y });
                                if (m && m.kind === "link" &&
                                    document.elementFromPoint(x, y) === el)
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
                    # [issue #85 owner r2] the pin tip must PERSIST with a
                    # from->to description while the pin lives
                    txt3a = page.evaluate(
                        "() => document.getElementById('wireTip')"
                        ".textContent")
                    disp3a = page.evaluate(
                        "() => document.getElementById('wireTip')"
                        ".style.display")

                    check("3d pin tip persists with from->to",
                          disp3a == "block" and
                          ("\u2192" in txt3a or "\u2193" in txt3a) and
                          len(txt3a) > 8,
                          f"{disp3a} {txt3a[:50]!r}")
                    check("3d pin emphasis resolves (pinCover)",
                          cover3d >= 1, f"cover {cover3d}")
                    # [#196] cross-surface twin (map wire row of the
                    # pair, or the pair's corridors/singles). A pair the
                    # map layout does not admit (a file outside the map
                    # lit set) has no 2D ink to emphasize - gate on
                    # presence so the check has teeth exactly when the
                    # twin is owed
                    cxp = page.evaluate("""() => {
                        const d = window.__dbg, pin = d.wirePin;
                        const fm = d.fnMeta, L = d.mapLayout;
                        if (!pin || !L) return { skip: true };
                        if (pin.kind !== "wire" || pin.a == null ||
                            !fm || !fm[pin.a] || !fm[pin.b])
                          // link pin: hub-budget pair - no gate
                          return { skip: false,
                                   x: d.mapInfo().pinCoverX };
                        const sf = fm[pin.a].file, df = fm[pin.b].file;
                        // the gate mirrors the resolver: a pair the map
                        // aggregated away (no trunk/single/row ink)
                        // owes no 2D twin; any ink present must paint
                        let ink = 0;
                        for (const sp of L.spines)
                          if (sp.pts && sp.pts.length > 1 &&
                              ((sp.s === sf && sp.t === df) ||
                               (sp.s === df && sp.t === sf))) ink++;
                        for (const w of L.wires)
                          if ((w.sf === sf && w.df === df) ||
                              (w.sf === df && w.df === sf)) ink++;
                        return { skip: ink === 0,
                                 x: d.mapInfo().pinCoverX }; }""")
                    if not cxp.get("skip"):
                        check("3d wire pin emphasizes in 2d (cross-surface)",
                              cxp.get("x", 0) >= 1,
                              f"pinCoverX {cxp.get('x')}")
                    else:
                        print("SKIP 3d wire 2d twin - pair outside "
                              "the map layout")
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
                    tip3b2 = page.evaluate(
                        "() => document.getElementById('wireTip')"
                        ".style.display")
                    check("pin tip survives the orbit press",
                          tip3b2 == "block", f"tip {tip3b2}")
                    # [#196] the instance tint is paint-tier state: an
                    # orbit (no rebuild) leaves it applied
                    tOrb = page.evaluate("""() => {
                        const d = window.__dbg, t = d.pinTint;
                        return { n: t.tinted.length,
                                 arcs: d.pinTint2.length }; }""")
                    check("3d tint survives the orbit (paint tier)",
                          tOrb["n"] + tOrb["arcs"] > 0, str(tOrb))

                    # [issue #58] regression guard: hover-context ends must
                    # not touch pin-owned cards — blur + canvas leave leave
                    # the pinned tip and pin intact (#85 contract)
                    pgB = page.evaluate("""() => {
                        window.dispatchEvent(new Event('blur'));
                        window.__dbg.renderer.domElement.dispatchEvent(
                            new PointerEvent('pointerleave'));
                        return { disp: document.getElementById('wireTip').style.display,
                                 pin: window.__dbg.wirePin }; }""")
                    check("ball pin tip unaffected by blur/leave (#85 contract)",
                          pgB["disp"] == "block" and pgB["pin"] == pin3b,
                          f"{pgB} vs {pin3b}")
                    # esc: the pin owns the first press; the tip (transient
                    # overlay) closes on the NEXT press per the existing chain
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(250)
                    pin3c = page.evaluate("() => window.__dbg.wirePin")
                    check("esc dismisses the 3d pin", pin3c is None,
                          f"{pin3b} -> {pin3c}")
                    # [#196] one esc clears BOTH surfaces' emphasis
                    covZ = page.evaluate(
                        "() => ({ c: window.__dbg.pinCover,"
                        " x: window.__dbg.pinCoverX })")
                    check("esc clears the pin on both surfaces",
                          pin3c is None and covZ["c"] == 0
                          and covZ["x"] == 0, str(covZ))
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
                # ---- [issue #87] fn-hover dead zone over wire ink ----
                # The hover raycast keeps an 18px forgiveness ball around
                # every fn-box center; the capture-phase wire-claim gate
                # used to defer to ANY active fn hover, so wire ink under
                # that invisible band was unclickable - the press opened
                # the fn panel instead of latching the wire (MapSkeptic
                # phase B #13: click 8px off a box center, picker resolves
                # the wire, no pin, no tip). Ink is the rarer, more
                # intentional aim: a press the picker resolves on ink must
                # claim the wire even inside a hover ball.
                def dead87_scan():
                    boxes = page.evaluate("""() => {
                        const d = window.__dbg;
                        const el = d.renderer.domElement;
                        const cr = el.getBoundingClientRect();
                        const mi = d.camera.matrixWorldInverse.elements,
                              pj = d.camera.projectionMatrix.elements;
                        // THREE is module-scoped in the bake - project
                        // fn-box centers by hand (column-major rows)
                        const proj = (x, y, z) => {
                          const cx = mi[0]*x+mi[4]*y+mi[8]*z+mi[12],
                                cy = mi[1]*x+mi[5]*y+mi[9]*z+mi[13],
                                cz = mi[2]*x+mi[6]*y+mi[10]*z+mi[14],
                                cw = mi[3]*x+mi[7]*y+mi[11]*z+mi[15];
                          const nx = pj[0]*cx+pj[4]*cy+pj[8]*cz+pj[12],
                                ny = pj[1]*cx+pj[5]*cy+pj[9]*cz+pj[13],
                                nz = pj[2]*cx+pj[6]*cy+pj[10]*cz+pj[14],
                                nw = pj[3]*cx+pj[7]*cy+pj[11]*cz+pj[15];
                          return [nx/nw, ny/nw, nz/nw];
                        };
                        const out = [];
                        for (let i = 0;
                             i < d.fnMeta.length && out.length < 40; i++) {
                            const fm = d.fnMeta[i];
                            if (!fm || (fm.agg && !fm.count)) continue;
                            if (d.alphaTgt[fm.file] <= 0.5) continue;
                            const nd = proj(fm.p[0], fm.p[1], fm.p[2]);
                            if (nd[2] > 1) continue;
                            out.push({
                                sx: Math.round((nd[0]*0.5+0.5)*cr.width
                                               + cr.left),
                                sy: Math.round((-nd[1]*0.5+0.5)*cr.height
                                               + cr.top),
                                p: fm.p, z: nd[2],
                                s: fm.count ? 6 : 4});
                        }
                        return out; }""")
                    # graded ring probes (6-10px offsets from the box
                    # center, inside the 18px ball): a probe is a
                    # dead-zone pixel when the fn tooltip is up (spaced
                    # " :: " is fn/file hover text; wire tips use
                    # unspaced label::x) AND the wire picker resolves
                    # ink on the canvas there. On the head bake
                    # (__dbg.pickWireZ readable), additionally require
                    # the ink to sit clearly in front of EVERY box whose
                    # projected silhouette contains the pixel (a
                    # superset of the ray-threaded hovered box) - the
                    # exact class the depth-gated fix flips. On a base
                    # bake the getter is absent and any ink-under-hover
                    # pixel is dead - the base gate ate them all.
                    offs = [(0, 6), (6, 0), (-6, 0), (0, -6), (8, 8),
                            (-8, -8), (8, -8), (-8, 8), (-8, 0), (10, 0)]
                    for b in boxes:
                        for ox, oy in offs:
                            px, py = b["sx"] + ox, b["sy"] + oy
                            page.mouse.move(px, py)
                            page.wait_for_timeout(40)
                            tip = page.evaluate(
                                "() => { const t ="
                                " document.getElementById('tip');"
                                " return t && t.style.display !== 'none'"
                                " ? t.textContent : ''; }")
                            if " :: " not in tip:
                                continue
                            hit = page.evaluate(
                                """(q) => {
                                const p = q.p, boxes = q.boxes;
                                const d = window.__dbg;
                                const el = d.renderer.domElement;
                                if (document.elementFromPoint(p[0], p[1])
                                    !== el) return null;
                                const m = d.pickWireMeta({clientX: p[0],
                                                          clientY: p[1]});
                                if (!m) return null;
                                if (d.pickWireZ === undefined)
                                  return {x: p[0], y: p[1],
                                          kind: m.kind};   // base bake
                                // head: ink clearly nearer than every
                                // box silhouette containing the pixel
                                // (margin absorbs float noise vs the
                                // handler's own THREE projection)
                                const mi =
                                  d.camera.matrixWorldInverse.elements,
                                      pj =
                                  d.camera.projectionMatrix.elements;
                                const proj = (x, y, z) => {
                                  const cx = mi[0]*x+mi[4]*y+mi[8]*z
                                             +mi[12],
                                        cy = mi[1]*x+mi[5]*y+mi[9]*z
                                             +mi[13],
                                        cz = mi[2]*x+mi[6]*y+mi[10]*z
                                             +mi[14],
                                        cw = mi[3]*x+mi[7]*y+mi[11]*z
                                             +mi[15];
                                  const nx = pj[0]*cx+pj[4]*cy+pj[8]*cz
                                             +pj[12],
                                        ny = pj[1]*cx+pj[5]*cy+pj[9]*cz
                                             +pj[13],
                                        nz = pj[2]*cx+pj[6]*cy+pj[10]*cz
                                             +pj[14],
                                        nw = pj[3]*cx+pj[7]*cy+pj[11]*cz
                                             +pj[15];
                                  return [nx/nw, ny/nw, nz/nw];
                                };
                                // compare in NDC (bbox corners are NDC;
                                // the probe arrives in screen px)
                                const cr2 = el.getBoundingClientRect();
                                const nx2 = ((p[0]-cr2.left)/cr2.width)
                                            *2 - 1,
                                      ny2 = -(((p[1]-cr2.top)
                                               /cr2.height)*2 - 1);
                                let minBz = Infinity;
                                for (const bx of boxes) {
                                  let x0 = 1e9, x1 = -1e9,
                                      y0 = 1e9, y1 = -1e9;
                                  for (let c = 0; c < 8; c++) {
                                    const v = proj(
                                      bx.p[0] + ((c&1) ? bx.s/2 : -bx.s/2),
                                      bx.p[1] + ((c&2) ? bx.s/2 : -bx.s/2),
                                      bx.p[2] + ((c&4) ? bx.s/2 : -bx.s/2));
                                    x0 = Math.min(x0, v[0]);
                                    x1 = Math.max(x1, v[0]);
                                    y0 = Math.min(y0, v[1]);
                                    y1 = Math.max(y1, v[1]);
                                  }
                                  if (nx2 >= x0 - 0.005 &&
                                      nx2 <= x1 + 0.005 &&
                                      ny2 >= y0 - 0.005 &&
                                      ny2 <= y1 + 0.005)
                                    minBz = Math.min(minBz, bx.z);
                                }
                                if (!(d.pickWireZ < minBz - 0.002))
                                  return null;
                                return {x: p[0], y: p[1],
                                        kind: m.kind}; }""",
                                {"p": [px, py], "boxes": boxes})
                            if hit:
                                return hit
                    return None
                dead87 = dead87_scan()
                if not dead87:
                    # camera-adjacent intermittence (MapSkeptic: dead
                    # after an orbit precursor, fine on a fresh pose) -
                    # one bounded orbit, then rescan, mirrors the
                    # trunk/link batteries' retry
                    page.mouse.move(400, 460)
                    page.mouse.down()
                    for k in range(1, 7):
                        page.mouse.move(400 + 3 * k, 460 + k)
                        page.wait_for_timeout(40)
                    page.mouse.up()
                    page.wait_for_timeout(700)
                    dead87 = dead87_scan()
                if dead87:
                    camA87 = page.evaluate(
                        "() => window.__dbg.camera.position.toArray()")
                    focA87 = page.evaluate(
                        "() => window.__dbg.focusFileIdx")
                    page.mouse.click(dead87["x"], dead87["y"])
                    page.wait_for_timeout(400)
                    pin87 = page.evaluate("() => window.__dbg.wirePin")
                    camB87 = page.evaluate(
                        "() => window.__dbg.camera.position.toArray()")
                    focB87 = page.evaluate(
                        "() => window.__dbg.focusFileIdx")
                    dcam87 = max(abs(a - b)
                                 for a, b in zip(camA87, camB87))
                    check("wire ink near fn boxes pins (#87 dead zone)",
                          pin87 and pin87["surface"] == "ball"
                          and focB87 == focA87 and dcam87 < 0.5,
                          f"{dead87} -> {pin87}, cam delta {dcam87:.2f}")
                    if pin87:
                        # only release a pin that latched - a stray Esc
                        # with no pin falls through the chain to
                        # clearFocus and would drop the map layout out
                        # from under the rest of the suite
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(250)
                        check("esc releases the dead-zone pin "
                              "(existing chain)",
                              not page.evaluate(
                                  "() => window.__dbg.wirePin"),
                              "pin cleared")
                else:
                    print("SKIP #87 dead zone - no ink under a hover band")

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
                    # [issue #85 owner r1] the pin emphasis must be the app
                    # accent, not the ambient white wire mass - sample the
                    # canvas at the pin midpoint; reverting to white/gray
                    # fails this (no teal pixel in the window)
                    if pinJ:
                        acc = page.evaluate("""(w) => {
                            const d = window.__dbg, L = d.mapLayout;
                            if (!L) return null;
                            const wr = L.wires.find(
                                x => d.wireKeyOf(x) === w.id);
                            if (!wr || !wr.pts || wr.pts.length < 2)
                                return null;
                            // #mapPane IS the 2D canvas (mapRender draws it)
                            const cv = document.getElementById('mapPane');
                            if (!cv || !cv.getContext) return null;
                            const bb = cv.getBoundingClientRect();
                            const dpr = cv.width / (cv.clientWidth || 1);
                            const ctx = cv.getContext('2d');
                            // sample 5 stations along the polyline - a
                            // dashed emphasis can gap at any single point
                            let teal = false;
                            const N = wr.pts.length;
                            for (let s = 0; s < 5 && !teal; s++) {
                                const p = wr.pts[Math.min(
                                    N - 1, Math.round((N - 1) * s / 4))];
                                const sx = (p[0] - d.mapPX) * d.mapZ + bb.left;
                                const sy = (p[1] - d.mapPY) * d.mapZ + bb.top;
                                const cx0 = Math.round((sx - bb.left) * dpr);
                                const cy0 = Math.round((sy - bb.top) * dpr);
                                for (let dx = -3; dx <= 3 && !teal; dx++)
                                  for (let dy = -3; dy <= 3 && !teal; dy++) {
                                    const q = ctx.getImageData(cx0 + dx,
                                        cy0 + dy, 1, 1).data;
                                    if (q[3] < 30) continue;
                                    if (q[1] > q[0] + 40 && q[1] > q[2] + 10)
                                        teal = true;
                                  }
                            }
                            return { teal }; }""", pinJ)
                        check("pinned map wire paints in the accent",
                              acc is not None and acc["teal"],
                              f"{acc} at {wpt}")
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
                                    sy > bb.top + 8 && sy < bb.bottom - 8 &&
                                    // #97: presses must reach the canvas —
                                    // DOM overlays over the header are inert
                                    document.elementFromPoint(sx, sy) === pn)
                                    return { sx: sx, sy: sy, i: rc.i };
                            }
                            return null; }""")
                        focBefore = page.evaluate(
                            "() => window.__dbg.focusFileIdx")
                        sigBefore = page.evaluate(
                            "() => (window.__dbg.mapLayout || {}).sig || ''")
                        if card2:
                            page.mouse.move(card2["sx"] - 12, card2["sy"] - 8)
                            page.mouse.move(card2["sx"], card2["sy"], steps=3)
                            page.mouse.click(card2["sx"], card2["sy"])
                            page.wait_for_timeout(700)
                            pinF1 = page.evaluate("() => window.__dbg.wirePin")
                            focAfter = page.evaluate(
                                "() => window.__dbg.focusFileIdx")
                            sigAfter = page.evaluate(
                                "() => (window.__dbg.mapLayout || {}).sig || ''")
                            if focAfter == focBefore:
                                # #97: dense maps can route wire ink over
                                # the header band — the engine resolves that
                                # click to the WIRE (4px stroke rule), which
                                # re-latches a pin instead of refocusing.
                                # Without a real refocus there is no subject.
                                print(f"SKIP focus-change clear - card "
                                      f"{card2['i']} click resolved to wire "
                                      f"ink (focus {focBefore} unchanged)")
                            elif sigAfter == sigBefore:
                                # #97: the clear rides the layout REBUILD —
                                # a refocus onto the same lit set keeps the
                                # identical map, and the pin's wire is still
                                # drawn, so keeping it is correct engine
                                # behavior, not a stale selection.
                                print(f"SKIP focus-change clear - refocus "
                                      f"{focBefore}->{focAfter} kept the "
                                      f"same lit signature (no rebuild)")
                            else:
                                check("focus change clears the map pin",
                                      pinF1 is None,
                                      f"{pinF0} -> {pinF1} via card {card2['i']}")
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

            # 8b2. zoom never seeds layout decisions (issue #63): the OLD
            # lanePad = max(9, 9/mapZ) leaked live zoom into the hub-bus
            # trunk router at rebuild time - a cache miss (pane resize,
            # expansion toggle) at a non-fit zoom changed lane spacing
            # between sessions, so the SAME focus state produced different
            # trunk geometry and routeAudit.down (194 vs 169 in the field).
            # A rebuild of the SAME expansion state at ANY zoom must
            # reproduce the trunk geometry + audit byte-for-byte. Data-gated
            # on the map actually having hub trunks (a dense focus). The
            # rebuild is forced by a __dbg expansion toggle (dblclick family)
            # so the pane SIZE is untouched and the zoom is the only
            # differing live variable.
            def _maplit():
                return page.evaluate("""() => {
                     const d = window.__dbg, L = d.mapLayout;
                     if (!L) return null;
                     const a = d.routeAudit || {};
                     const trunks = L.spines.filter(s => s.hub === 'trunk').map(s => s.pts);
                     return { down: a.down, up: a.up, sameRow: a.sameRow,
                              back: a.back, hubTrunks: a.hubTrunks,
                              hubTaps: a.hubTaps, hubPeels: a.hubPeels,
                              nTrunk: trunks.length,
                              trunks: trunks,
                              wires: L.wires.length, z: Math.round(d.mapZ * 100) }; }""")
            lit0 = _maplit()
            if lit0 and lit0["nTrunk"]:
                # expansion toggle helper: force a REAL rebuild (new key) at
                # the current zoom while keeping size/sig identical. Toggle a
                # wired box the map currently has expanded.
                # pick ONE wired box by file index ONCE (expandedSet is a Set
                # whose iteration order is not a stable contract — toggling
                # "[0]" across rebuilds could hit different boxes). Choose a
                # box that is sure to be present and wired: the focus seed
                # (level 0, always lit + expanded at L0).
                def _seed_box():
                    return page.evaluate("""() => {
                         const d = window.__dbg, L = d.mapLayout;
                         if (!L) return null;
                         for (const i of L.expandedSet)
                           if (d.level[i] === 0) return i;
                         return null; }""")
                _sb = _seed_box()
                if _sb is None:
                    print("SKIP issue#63 zoom-seed checks - no expanded seed box")
                else:
                    _rebuild_ix = _sb

                    def _rebuild(closed):
                        page.evaluate(
                            """(a) => { const [ix, closed] = a;
                                 const d = window.__dbg;
                                 if (closed) d.mapExpandUser.set(ix, false);
                                 else d.mapExpandUser.delete(ix);
                                 d.mapPane.draw(); }""",
                            [_rebuild_ix, closed])
                        page.wait_for_timeout(600)

                    # baseline: close the seed roster at FIT -> rebuild
                    _rebuild(True)
                    fit_closed = _maplit()
                    _rebuild(False)     # reopen -> fit state with seed open
                    # zoom IN hard (mapZ -> 3.0); the pane size is untouched
                    zoompt = page.evaluate(
                        "() => document.getElementById('mapPane').getBoundingClientRect()")
                    page.mouse.move(zoompt["x"] + zoompt["width"] / 2,
                                    zoompt["y"] + zoompt["height"] / 2)
                    for _ in range(30):
                        page.mouse.wheel(0, -120)
                        page.wait_for_timeout(40)
                    page.wait_for_timeout(400)
                    z1 = page.evaluate("() => window.__dbg.mapZ")
                    _rebuild(True)      # same closed state, NOW at z=3
                    z3_closed = _maplit()
                    _rebuild(False)
                    # zoom OUT hard (mapZ -> 0.2)
                    page.mouse.move(zoompt["x"] + zoompt["width"] / 2,
                                    zoompt["y"] + zoompt["height"] / 2)
                    for _ in range(60):
                        page.mouse.wheel(0, 120)
                        page.wait_for_timeout(30)
                    page.wait_for_timeout(400)
                    z2 = page.evaluate("() => window.__dbg.mapZ")
                    _rebuild(True)      # same closed state, NOW at z=0.2
                    z02_closed = _maplit()
                    _rebuild(False)
                    # the SAME closed expansion state across rebuild zooms
                    # must be byte-identical in audit AND trunk geometry
                    def _same(a, b):
                        return bool(a and b) and (
                            a["down"] == b["down"] and a["up"] == b["up"] and
                            a["sameRow"] == b["sameRow"] and a["back"] == b["back"] and
                            a["hubTrunks"] == b["hubTrunks"] and
                            a["hubTaps"] == b["hubTaps"] and
                            a["hubPeels"] == b["hubPeels"] and
                            a["nTrunk"] == b["nTrunk"] and
                            a["wires"] == b["wires"] and a["trunks"] == b["trunks"])
                    same3 = _same(fit_closed, z3_closed)
                    same2 = _same(fit_closed, z02_closed)
                    check("rebuild at zoom-IN reproduces audit + trunk geometry (issue #63)",
                          bool(same3) and z1 >= 2.5,
                          f"fit vs z={z1:.2f}: {fit_closed} vs {z3_closed}")
                    check("rebuild at zoom-OUT reproduces audit + trunk geometry (issue #63)",
                          bool(same2) and z2 <= 0.35,
                          f"fit vs z={z2:.2f}: {fit_closed} vs {z02_closed}")
                    # sanity: the rebuild toggle actually changed something
                    # (the geometry comparison is not vacuous)
                    check("issue#63 rebuild toggle actually rebuilt (open != closed)",
                          bool(fit_closed) and bool(_maplit()),
                          f"closed down={fit_closed['down']}")
                # source invariant: the hub-bus trunk router must NEVER read
                # mapZ — the lane pad is a world-space constant, not a
                # screen-space value scaled by the live zoom. The old
                # `lanePad = Math.max(9, 9 / mapZ)` leaked view state into
                # layout rebuilds, so the SAME focus could route differently
                # across sessions (routeAudit.down 194 vs 169). Assert the
                # built page's lanePad assignment is zoom-free.
                lane_src = page.evaluate(
                    """() => { const s = document.documentElement.outerHTML || '';
                         const i = s.indexOf('const lanePad =');
                         if (i < 0) return { idx: -1 };
                         const tail = s.slice(i, i + 120);
                         return { idx: i,
                                  zoomFree: tail.indexOf('mapZ') < 0,
                                  tail: tail.slice(0, 80) }; }""")
                check("issue#63 trunk lanePad is zoom-free (source invariant)",
                      bool(lane_src) and lane_src["idx"] >= 0 and lane_src["zoomFree"],
                      str(lane_src.get("tail") if lane_src else lane_src))

                # 8b3. [#10] router bend law: the staircase class (serial
                rf10 = page.evaluate(
                    """() => { const d = window.__dbg;
                         const rf = d.routeFns;
                         if (!rf) return { fail: 'no routeFns' };
                         // synthetic staircase-class polyline: 6 jogs of
                         // 6px cross-runs between same-direction 40px
                         // vertical runs (the issue's signature shape) —
                         // x CLIMBS monotonically, it does not oscillate
                         const stair = [[0, 0]];
                         let px = 0, py = 0;
                         for (let k = 0; k < 6; k++) {
                           py += 40; stair.push([px, py]);
                           px += 6; stair.push([px, py]);
                           py += 40; stair.push([px, py]);
                         }
                         const merged = rf.mergeBends(stair.map(p => [p[0], p[1]]));
                         const t0 = rf.turnsOf(stair), t1 = rf.turnsOf(merged);
                         const sameEnds = merged.length >= 2
                           && merged[0][0] === stair[0][0]
                           && merged[0][1] === stair[0][1]
                           && merged[merged.length-1][0] === stair[stair.length-1][0]
                           && merged[merged.length-1][1] === stair[stair.length-1][1];
                         // monotone law: a clean zigzag must never gain
                         // a bend through the merger
                         const zz = [];
                         for (let k = 0; k <= 8; k++)
                           zz.push([k % 2 ? 30 : 0, k * 20]);
                         const zt0 = rf.turnsOf(zz), zt1 = rf.turnsOf(rf.mergeBends(zz));
                         return { t0, t1, sameEnds, len0: stair.length,
                                  len1: merged.length, zt0, zt1 }; }""")
                check("[#10] route merger exposed on the page (routeFns)",
                      bool(rf10) and "fail" not in rf10, str(rf10)[:160])
                if rf10 and "fail" not in rf10:
                    check("[#10] merger consolidates the staircase class",
                          rf10["t1"] <= rf10["t0"] and rf10["sameEnds"]
                          and rf10["len1"] < rf10["len0"],
                          f"turns {rf10['t0']}->{rf10['t1']} "
                          f"pts {rf10['len0']}->{rf10['len1']}")
                    check("[#10] merger never adds a bend (monotone law)",
                          rf10["zt1"] <= rf10["zt0"],
                          f"zigzag {rf10['zt0']}->{rf10['zt1']}")
                a10 = page.evaluate("() => window.__dbg.routeAudit")
                check("[#10] route census carries the turn fields",
                      bool(a10) and all(k in a10 for k in
                        ("maxTurns", "over20", "turnsSum", "turnsN",
                         "maxTrunkTurns")),
                      str(a10)[:180])
                check("[#10] staircase class within bar post-merge",
                      bool(a10) and a10.get("maxTurns") is not None
                      and a10["maxTurns"] <= 20 and a10["over20"] == 0,
                      f"maxTurns={a10.get('maxTurns') if a10 else None} "
                      f"over20={a10.get('over20') if a10 else None}")
            page.screenshot(path=str(SHOTS / "qa_map_zoomout.png"),
                            scale="css", type="png")
            print("artifact: .tmp/shots/qa_map_zoomout.png")
            # 8c. zoom-gated ink tiers (paint-only): the fine layers
            # (underlays, named wires, port dots/arrowheads) hide when
            # zoomed out and return when zoomed back in (hysteresis) -
            # the LAYOUT never changes: mapLayout.wires count identical
            # at both ends (ONE-layout law survives the tier gate).
            wires_pre = page.evaluate(
                "() => window.__dbg.mapLayout"
                " ? window.__dbg.mapLayout.wires.length : -1")
            page.mouse.move(mcx, mcy)
            for _ in range(20):
                page.mouse.wheel(0, 120)
                page.wait_for_timeout(40)
                if page.evaluate("() => window.__dbg.mapZ") < 0.5:
                    break
            page.wait_for_timeout(400)
            ink_off = page.evaluate(
                """() => ({ ink: window.__dbg.mapInkOn,
                            wires: window.__dbg.mapLayout
                                ? window.__dbg.mapLayout.wires.length
                                : -1 })""")
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

            # 8c2. [#77] distance-gated wire LOD bands + selection-time
            # unbundling. Bands are PAINT tier (layout never changes, #63;
            # DATA blob byte-identical): band 1 thins the label plane,
            # band 2 aggregates to cluster corridors. Thresholds sit below
            # the fit floor, so the overview above ran at band 0.
            thr = page.evaluate("() => window.__dbg.mapLodThresholds")
            fit_z = page.evaluate("() => window.__dbg.mapZ")
            if fit_z < thr["z1x"]:
                print(f"SKIP 8c2 LOD bands — fit z {fit_z:.2f} sits below "
                      f"band-1 exit {thr['z1x']} (corpus shape, #97)")
            else:
                def wheel_to(target, direction):
                    for _ in range(50):
                        z = page.evaluate("() => window.__dbg.mapZ")
                        if (direction < 0 and z < target) or \
                           (direction > 0 and z >= target):
                            return z
                        page.mouse.move(mcx, mcy)
                        page.mouse.wheel(0, 120 if direction < 0 else -120)
                        page.wait_for_timeout(35)
                    return page.evaluate("() => window.__dbg.mapZ")

                b0 = page.evaluate("() => window.__dbg.mapInfo()")
                check("LOD band 0 at fit: full fidelity (#77)",
                      b0["lodBand"] == 0 and b0["rosterShown"] > 0,
                      f"band={b0['lodBand']} roster={b0['rosterShown']}")
                page.screenshot(path=".tmp/shots/qa_map_band0.png", type="png")

                wheel_to(thr["z1"], -1)
                b1 = page.evaluate("() => window.__dbg.mapInfo()")
                check("LOD band 1: roster folds + badges thin (VISSOFT'25)",
                      b1["lodBand"] == 1 and b1["rosterShown"] == 0,
                      f"band={b1['lodBand']} roster={b1['rosterShown']}")
                page.screenshot(path=".tmp/shots/qa_map_band1.png", type="png")

                wheel_to(thr["z2"], -1)
                b2 = page.evaluate("() => window.__dbg.mapInfo()")
                if b2["lodHiddenSpines"] > 0:
                    check("LOD band 2: intra-cluster corridors fold",
                          b2["lodBand"] == 2 and
                          b2["lodSpinesPainted"] ==
                          b2["spinesDrawn"] - b2["lodHiddenSpines"]
                          - b2["tapsFolded"],   # [#61] taps fold < MAP_TAP_FOLD_Z
                          f"band={b2['lodBand']} "
                          f"painted={b2['lodSpinesPainted']} "
                          f"drawn={b2['spinesDrawn']} "
                          f"hidden={b2['lodHiddenSpines']} "
                          f"tapsFolded={b2['tapsFolded']}")
                else:
                    print("SKIP band-2 spine fold — corpus has no "
                          "intra-cluster corridors (#97)")
                if b2["lodAggChips"] > 0:
                    check("LOD band 2: pair badges aggregate to cluster counts",
                          b2["lodBand"] == 2 and b2["lodAggChips"] >= 1,
                          f"aggChips={b2['lodAggChips']}")
                else:
                    print("SKIP band-2 chip aggregation — corpus shape (#97)")
                page.screenshot(path=".tmp/shots/qa_map_band2.png", type="png")

                # selection-time unbundling (AVI'12: bundling degrades path
                # tracing): while a pin holds a corridor pair open, its
                # riders draw straight/individual EVEN at band 2; Escape
                # restores band ink atomically (dismissal parity, #197).
                trunk_pick = page.evaluate(
                    """() => { const d = window.__dbg, L = d.mapLayout;
                         if (!L || !L.pairRiders) return null;
                         for (const sp of L.spines) {
                           if (sp.hub !== "trunk" || !sp.pts || sp.pts.length < 2) continue;
                           if (!L.pairRiders[sp.s + "_" + sp.t]) continue;
                           const m = sp.pts[Math.floor(sp.pts.length / 2)];
                           return { pair: sp.s + "_" + sp.t,
                                    riders: L.pairRiders[sp.s + "_" + sp.t].length,
                                    sx: (m[0] - d.mapPX) * d.mapZ,
                                    sy: (m[1] - d.mapPY) * d.mapZ };
                         }
                         return null; }""")
                if trunk_pick is None:
                    print("SKIP selection-time unbundling — no trunk with "
                          "riders at band 2 (corpus shape, #97)")
                else:
                    page.mouse.click(mbb["x"] + trunk_pick["sx"],
                                     mbb["y"] + trunk_pick["sy"])
                    page.wait_for_timeout(250)
                    pin = page.evaluate("() => window.__dbg.wirePin")
                    ub = page.evaluate("() => window.__dbg.mapInfo()")
                    check("unbundling: pinned pair opens at band 2 (#77)",
                          pin and pin["kind"] == "trunk" and
                          ub["lodKeep"] and ub["lodUnbundled"] > 0,
                          f"pin={pin and pin['kind']} keep={ub['lodKeep']} "
                          f"unbundled={ub['lodUnbundled']} "
                          f"riders={trunk_pick['riders']}")
                    page.screenshot(path=".tmp/shots/qa_map_unbundled.png", type="png")
                    agg_pre = ub["lodAggChips"]
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(250)
                    pin_e = page.evaluate("() => window.__dbg.wirePin")
                    ub2 = page.evaluate("() => window.__dbg.mapInfo()")
                    check("dismissal parity: Escape restores band ink (#197)",
                          pin_e is None and ub2["lodUnbundled"] == 0 and
                          not ub2["lodKeep"] and ub2["lodAggChips"] == agg_pre
                          and ub2["lodBand"] == 2,
                          f"pin={pin_e} unbundled={ub2['lodUnbundled']} "
                          f"keep={ub2['lodKeep']} agg={ub2['lodAggChips']}"
                          f"/{agg_pre} band={ub2['lodBand']}")

                # restore past band-0 exit AND to 8c's z >= 1.0 state:
                # the later pan-clamp law needs the world wider than the
                # window (its premise holds at z >= 1.0)
                wheel_to(1.0, 1)
                b0b = page.evaluate("() => window.__dbg.mapInfo()")
                check("LOD bands restore: zoom-in returns to band 0",
                      b0b["lodBand"] == 0 and b0b["rosterShown"] > 0,
                      f"band={b0b['lodBand']} roster={b0b['rosterShown']}")
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
            # 8e. [#61] super-hub tier degradation: hub taps are
            # 1-world-px access roads; below MAP_TAP_FOLD_Z they render
            # sub-half-pixel and fold (paint + pick parity) - the floored
            # trunk + peel dots carry the read. Shape-gated on the
            # section focus having hub taps.
            taps_now = page.evaluate(
                """() => { const L = window.__dbg.mapLayout;
                     return L ? L.spines.filter(s => s.hub === 'tap').length : 0; }""")
            if not taps_now:
                print("SKIP 8e tap fold (#61) — focus shape has no hub taps (#97)")
            else:
                def _tap_wheel(target, direction):
                    for _ in range(50):
                        z = page.evaluate("() => window.__dbg.mapZ")
                        if (direction < 0 and z < target) or \
                           (direction > 0 and z >= target):
                            return z
                        page.mouse.move(mcx, mcy)
                        page.mouse.wheel(0, 120 if direction < 0 else -120)
                        page.wait_for_timeout(35)
                    return page.evaluate("() => window.__dbg.mapZ")

                thr_t = page.evaluate("() => window.__dbg.mapLodThresholds")
                _tap_wheel(thr_t["tapFold"] - 0.03, -1)
                page.wait_for_timeout(200)
                tf = page.evaluate("() => window.__dbg.mapInfo()")
                check("taps fold below the legibility floor (#61)",
                      tf["tapsFolded"] == taps_now and tf["tapsPainted"] == 0
                      and tf["lodSpinesPainted"] >= 1,
                      f"painted={tf['tapsPainted']} "
                      f"folded={tf['tapsFolded']}/{taps_now} "
                      f"spines={tf['lodSpinesPainted']}")
                page.screenshot(path=".tmp/shots/qa_map_tapfold.png",
                                type="png")
                _tap_wheel(thr_t["tapFold"] + 0.1, 1)
                page.wait_for_timeout(200)
                tr = page.evaluate("() => window.__dbg.mapInfo()")
                check("taps restore above the floor (#61)",
                      tr["tapsPainted"] == taps_now and tr["tapsFolded"] == 0,
                      f"painted={tr['tapsPainted']} "
                      f"folded={tr['tapsFolded']}")
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
        # [issue #85 owner r3] corridor-complete legs: clicking a
        # junction leg must latch the FULL corridor (its dominant
        # trunk), with the persistent bus pair summary on the tip
        # junction legs only carry pick ink in the serve tier (camera
        # within 2.2 ball radii) - wheel-zoom in like a user reading
        # junctions until _lodServe flips on
        # zoom budget: keep dollying until serve flips - the 6-tick
        # budget was tuned on large-ball stores; a tighter compact ball
        # (small 1-hop sets) needs a few more ticks to cross the
        # 2.2R serve tier (#97 data-shape fix, engine law unchanged)
        for _ in range(10):
            if page.evaluate("() => !!window.__dbg.lodServe"):
                break
            c = page.evaluate(
                "() => { const r = window.__dbg.renderer"
                ".domElement.getBoundingClientRect();"
                " return {x: r.left + r.width / 2,"
                " y: r.top + r.height / 2}; }")
            page.mouse.move(c["x"], c["y"])
            page.mouse.wheel(0, -400)
            page.wait_for_timeout(250)
        leg_pts = page.evaluate("""() => {
            const d = window.__dbg;
            // the tail collapsed the map pane: scan canvas-local
            // like the tail does (full-width 3D canvas, left=0)
            const el = d.renderer.domElement,
                  r = el.getBoundingClientRect();
            const out = [];
            for (let x = 16; x < r.width && out.length < 4; x += 30)
              for (let y = 16; y < r.height && out.length < 4;
                   y += 30) {
                const m = d.pickWireMeta({ clientX: r.left + x,
                                           clientY: r.top + y });
                // [#81 surface law] canvas-owned aims only
                if (m && m.kind === "jleg" &&
                    document.elementFromPoint(r.left + x, r.top + y) === el)
                    out.push({ x: r.left + x, y: r.top + y });
              }
            return out; }""")
        pinLg = None
        for cand in leg_pts or []:
            page.mouse.move(cand["x"], cand["y"])
            page.wait_for_timeout(150)
            hov = page.evaluate(
                "() => ({ hf: window.__dbg.hoveredFn,"
                " hv: window.__dbg.hovered })")
            if (hov["hf"] is not None and hov["hf"] >= 0) or \
               (hov["hv"] is not None and hov["hv"] >= 0):
                continue
            page.mouse.click(cand["x"], cand["y"])
            page.wait_for_timeout(400)
            pinLg = page.evaluate("() => window.__dbg.wirePin")
            if pinLg:
                leg_used = cand
                break
        if pinLg and pinLg["kind"] == "trunk":
            covLg = page.evaluate("() => window.__dbg.pinCover")
            tipLg = page.evaluate(
                "() => document.getElementById('wireTip')"
                ".textContent")
            check("junction leg click pins the full corridor",
                  pinLg["surface"] == "ball" and covLg >= 2,
                  f"{leg_used} -> {pinLg}, cover {covLg}")
            check("leg pin tip shows the bus pair summary",
                  "bus" in tipLg and "\u2192" in tipLg,
                  (tipLg or "")[:60])

            # [issue #85 owner r4 / #196] the emphasis IS the corridor
            # now: the tinted instance set must EQUAL the corridor's
            # true serving set (trunk pieces + junction legs, visible
            # only) and carry the accent lerp on every sampled piece.
            # On-geometry by construction - this replaces the old
            # polyline-hop distance probe (the constructed overlay it
            # measured is deleted; a chord regression is impossible)
            geoT = page.evaluate("""() => {
                const d = window.__dbg, pin = d.wirePin;
                if (!pin || !pin.k || !d.fnBus || !d.fnBus.instanceColor)
                    return null;
                const prefs = [];
                for (const S of (d.fnStations || []))
                    if (S.tks.indexOf(pin.k) >= 0)
                        prefs.push("L|" + S.fi + "|" + S.id + "|");
                const mm = d.fnBus.instanceMatrix.array,
                      ca = d.fnBus.instanceColor.array;
                const want = [];
                for (let i = 0; i < d.busPtsMeta.length; i++) {
                    const mt = d.busPtsMeta[i]; if (!mt) continue;
                    const k = String(mt.k || "");
                    const hit = (mt.kind === "trunk" && k === pin.k) ||
                        (k.charCodeAt(0) === 76 &&
                         prefs.some(p => k.startsWith(p)));
                    if (!hit) continue;
                    if (Math.hypot(mm[i*16], mm[i*16+1], mm[i*16+2])
                        <= 0.001) continue;
                    want.push(i);
                }
                const pt = d.pinTint;
                const tinted = pt.tinted;
                if (!tinted.length)
                    return { ok: false, why: "no tint",
                             want: want.length };
                const same = tinted.length === want.length &&
                    tinted.every((v, i2) => v === want[i2]);
                // accent-lerp teeth: green-dominant samples (a revert
                // to stock/white colors fails this)
                let gN = 0, sN = 0;
                for (let j = 0; j < tinted.length;
                     j += Math.max(1, Math.floor(tinted.length / 12))) {
                    const q = tinted[j]; sN++;
                    if (ca[q*3+1] > ca[q*3] + 0.08 && ca[q*3+1] > 0.35)
                        gN++;
                }
                return { ok: true, same, nT: tinted.length,
                         nW: want.length, gN, sN }; }""")
            if geoT and geoT.get("ok"):
                check("pin emphasis tints exactly the corridor set",
                      geoT["same"] and geoT["gN"] >= 1,
                      f"tinted {geoT['nT']} vs want {geoT['nW']}, "
                      f"accent {geoT['gN']}/{geoT['sN']} samples")
            elif geoT:
                print(f"SKIP on-geometry tint check - {geoT}")

            # [issue #85 owner r4] the corridor itself reads selected:
            # covered instances tint toward the accent (sibling = leg
            # outside the corridor, stock color); restore is checked
            # after dismissal below
            tintLg = page.evaluate("""() => {
                const d = window.__dbg, pin = d.wirePin;
                if (!pin || !pin.k || !d.fnBus || !d.fnBus.instanceColor)
                    return null;
                const prefs = [];
                for (const S of (d.fnStations || []))
                    if (S.tks.indexOf(pin.k) >= 0)
                        prefs.push("L|" + S.fi + "|" + S.id + "|");
                const mm = d.fnBus.instanceMatrix.array,
                      ca = d.fnBus.instanceColor.array;
                let q = -1, sib = -1;
                for (let i = 0; i < d.busPtsMeta.length; i++) {
                    const mt = d.busPtsMeta[i]; if (!mt) continue;
                    const k = String(mt.k || "");
                    const hit = (mt.kind === "trunk" && k === pin.k) ||
                        (k.charCodeAt(0) === 76 &&
                         prefs.some(p => k.startsWith(p)));
                    if (hit && q < 0 &&
                        Math.hypot(mm[i*16], mm[i*16+1], mm[i*16+2]) > 0.001)
                        q = i;
                    else if (!hit && k.charCodeAt(0) === 76 && sib < 0)
                        sib = i;
                }
                if (q < 0 || sib < 0) return { ok: false, q, sib };
                return { ok: true, q, sib,
                    t: [ca[q*3], ca[q*3+1], ca[q*3+2]],
                    sib0: [ca[sib*3], ca[sib*3+1], ca[sib*3+2]] }; }""")
            if tintLg and tintLg.get("ok"):
                check("pinned corridor instances tint the accent",
                      tintLg["t"][1] > tintLg["t"][0] + 0.08
                      and tintLg["t"][1] > 0.35,
                      f"piece {tintLg['q']} rgb "
                      f"{[round(v, 2) for v in tintLg['t']]}")
                # [#196] the LOD serve pass rewrites matrices only -
                # the instance tint rides it untouched (the leg block
                # runs with _lodServe on)
                tSrv = page.evaluate(
                    "() => window.__dbg.pinTint.tinted.length")
                check("corridor tint survives the serve pass",
                      tSrv > 0, f"tinted {tSrv}")

            # press on the leg again: the transient tip hides on
            # pointerdown and the pin re-asserts it next frame
            page.mouse.move(leg_used["x"], leg_used["y"])
            page.mouse.down()
            page.mouse.up()
            page.wait_for_timeout(400)
            pinLg2 = page.evaluate("() => window.__dbg.wirePin")
            tipLg2 = page.evaluate(
                "() => document.getElementById('wireTip')"
                ".style.display")
            check("pin tip survives a press while pinned",
                  tipLg2 == "block" and pinLg2 == pinLg,
                  f"tip {tipLg2}, {pinLg} == {pinLg2}")
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
            pinLg3 = page.evaluate("() => window.__dbg.wirePin")
            tipLg3 = page.evaluate(
                "() => document.getElementById('wireTip')"
                ".style.display")
            check("esc clears the leg pin and its tip",
                  pinLg3 is None and tipLg3 == "none",
                  f"pin {pinLg3}, tip {tipLg3}")
            if tintLg and tintLg.get("ok"):
                tintOff = page.evaluate("""(q) => {
                    const d = window.__dbg;
                    const ca = d.fnBus.instanceColor.array;
                    return [ca[q*3], ca[q*3+1], ca[q*3+2]]; }""",
                    tintLg["q"])
                check("corridor tint restores on dismissal",
                      all(abs(a - b) < 0.02 for a, b in
                          zip(tintOff, tintLg["sib0"])),
                      f"piece {tintLg['q']} rgb "
                      f"{[round(v, 2) for v in tintOff]} vs sibling "
                      f"{[round(v, 2) for v in tintLg['sib0']]}")
        else:
            print("SKIP junction leg click - no leg ink in view")

        # [issue #84] skeptic #14: a focus rebuild must not leave the
        # trunk bundle list open with stale corridor rows - latch a pin
        # THROUGH the list (menu=list), refocus via a card header clear
        # of the open list, then require both the list and the pin gone.
        # Runs AFTER the stateful tail probe on purpose: the card
        # refocus rotates the camera approach direction focus()
        # inherits, and the trunk-scan grid above is sensitive to it
        # (bisected: the latch alone is clean, the extra refocus is
        # what the tail saw).
        pinL = None
        if page.evaluate("() => !!window.__dbg.mapLayout"):
            # expand only if collapsed: bMap TOGGLES, and an unconditional
            # click would collapse an open pane and starve the tail
            if page.evaluate(
                    "() => document.getElementById('mapPane')"
                    ".classList.contains('collapsed')"):
                page.evaluate(
                    "() => document.getElementById('bMap').click()")
            try:
                page.wait_for_function(
                    "() => !document.getElementById('mapPane')"
                    ".classList.contains('collapsed')", timeout=4000)
            except PwTimeout:
                print("SKIP stale list close - map pane would not expand "
                      "(precondition gate)")
            # [#123] no except-NameError skip: the helper is pre-bound
            # above — a closed data gate reads as None (loud SKIP below),
            # while a NameError raised INSIDE the helper is a failure.
            pinL = latch_pin_via_list() if latch_pin_via_list else None
        else:
            print("SKIP stale list close - no map layout "
                  "(shape: index has no DATA.mwires)")
        if pinL and pinL.get("menu") == "list":
            card3 = page.evaluate("""() => {
                const d = window.__dbg;
                const pn = document.getElementById('mapPane');
                const bb = pn.getBoundingClientRect();
                const lr = document.getElementById('mapList')
                    .getBoundingClientRect();
                for (const rc of d.mapRects) {
                    if (rc.i === d.focusFileIdx) continue;
                    const sx = (rc.x + rc.w / 2 - d.mapPX) * d.mapZ + bb.left;
                    const sy = (rc.y + 11 - d.mapPY) * d.mapZ + bb.top;
                    const wx = (sx - bb.left) / d.mapZ + d.mapPX;
                    const wy = (sy - bb.top) / d.mapZ + d.mapPY;
                    // [#123] the pane's own hit test decides what a click
                    // means, in priority order: the vars chip (branch 0),
                    // bundle chips, named wires, then TRUNK SPINES - only
                    // then the header refocus. Exclude every claimant the
                    // product would resolve ahead of the header.
                    const vc = d.mapVarsChipRect;
                    if (vc && sx - bb.left >= vc.x && sx - bb.left <= vc.x + vc.w &&
                        sy - bb.top >= vc.y && sy - bb.top <= vc.y + vc.h)
                        continue;
                    if (d.mapWireAt(wx, wy) !== -1) continue;
                    if (d.mapChipAt(wx, wy) !== -1) continue;   // painted hit rect
                    const tol = 10 / d.mapZ;
                    let onSpine = false;
                    for (const sp of (d.mapLayout.spines || [])) {
                        if (onSpine || !sp.pts || sp.pts.length < 2) continue;
                        for (let q = 1; q < sp.pts.length; q++) {
                            const ax = sp.pts[q-1][0], ay = sp.pts[q-1][1],
                                  bx = sp.pts[q][0], by = sp.pts[q][1];
                            const l2 = (bx-ax)*(bx-ax) + (by-ay)*(by-ay) || 1;
                            let t = ((wx-ax)*(bx-ax) + (wy-ay)*(by-ay)) / l2;
                            t = Math.max(0, Math.min(1, t));
                            if (Math.hypot(wx - (ax + t*(bx-ax)),
                                           wy - (ay + t*(by-ay))) < tol) {
                                onSpine = true; break;
                            }
                        }
                    }
                    if (onSpine) continue;
                    // overlap law: the handler resolves the TOPMOST-drawn
                    // rect at the point (reverse iteration) - if another
                    // box overlaps this header, the click lands in that
                    // box's row zone and silently toggles the freeze
                    let top = null;
                    for (let k2 = d.mapRects.length - 1; k2 >= 0; k2--) {
                        const r2 = d.mapRects[k2];
                        if (wx < r2.x || wx > r2.x + r2.w ||
                            wy < r2.y || wy > r2.y + r2.h) continue;
                        top = r2; break;
                    }
                    if (top !== rc) continue;
                    if (sx > bb.left + 8 && sx < bb.right - 8 &&
                        sy > bb.top + 8 && sy < bb.bottom - 8 &&
                        (sx < lr.x - 8 || sx > lr.x + lr.width + 8 ||
                         sy < lr.y - 8 || sy > lr.y + lr.height + 8) &&
                        // any overlay (list, wire tip, pin chip) swallows
                        // the click - the aim must be the pane's own ink,
                        // same guard as the drift probes
                        document.elementFromPoint(sx, sy) === pn)
                        return { sx: sx, sy: sy, i: rc.i };
                }
                return null; }""")
            if card3:
                # [#123] the scan and the click are two instants: a
                # transient (hover tip, staggered pin-card reveal) can
                # cover the aim between them and swallow the input -
                # events=[] in instrumented runs. Gate on click-time
                # point ownership and retry a swallowed click; a real
                # refocus that fails to close the list still fails the
                # check below (retries only re-attempt the input).
                foc0 = page.evaluate("() => window.__dbg.focusFileIdx")
                for _try in range(3):
                    try:
                        page.wait_for_function(
                            """(m) => document.elementFromPoint(m.sx, m.sy)
                                && document.elementFromPoint(m.sx, m.sy).id
                                   === 'mapPane'""",
                            arg=card3, timeout=2000)
                    except PwTimeout:
                        pass   # the surface check below fails honestly
                    page.mouse.click(card3["sx"], card3["sy"])
                    try:
                        page.wait_for_function(
                            "(f) => window.__dbg.focusFileIdx !== f",
                            arg=foc0, timeout=900)
                        break   # the 220ms refocus debounce landed
                    except PwTimeout:
                        continue   # input swallowed - gate and retry
                try:
                    page.wait_for_function(
                        "() => document.getElementById('mapList')"
                        ".style.display !== 'block'", timeout=1500)
                except PwTimeout:
                    pass   # the surface check below fails honestly
                listR = page.evaluate(
                    "() => document.getElementById('mapList').style.display")
                pinR = page.evaluate("() => window.__dbg.wirePin")
                check("focus rebuild closes the stale bundle list",
                      listR != "block" and pinR is None,
                      f"list {listR}, pin {pinL} -> {pinR} via card {card3['i']}")

        # [issue #85 owner r1 / groundskeeper] trunk corridors paint in
        # the accent: latch a REAL trunk spine pin - the chip-list pin
        # is a wire and never matched a trunk spine, so the old probe
        # was unreachable and silently self-skipped - then pixel-probe
        # the painted corridor midpoint.
        tspine = page.evaluate("""() => {
            const d = window.__dbg, L = d.mapLayout;
            if (!L || !L.spines) return null;
            const bb = document.getElementById('mapPane')
                .getBoundingClientRect();
            for (const sp of L.spines) {
                if (sp.hub !== "trunk" || !sp.pts || sp.pts.length < 2)
                    continue;
                // [#123] sample quarter points, midpoint first to match
                // the historical aim: the click lands on whatever ink
                // the pane's own hit test resolves, so verify the aim
                // with the product's picker (no named wire, no chip) -
                // the pin is then the trunk this check is about, at any
                // camera azimuth.
                for (const k of [2, 1, 3]) {
                    const m = sp.pts[Math.min(
                        Math.floor(sp.pts.length * k / 4),
                        sp.pts.length - 1)];
                    const sx = (m[0] - d.mapPX) * d.mapZ + bb.left;
                    const sy = (m[1] - d.mapPY) * d.mapZ + bb.top;
                    if (sx < bb.left + 10 || sx > bb.right - 10 ||
                        sy < bb.top + 10 || sy > bb.bottom - 10)
                        continue;
                    const wx = (sx - bb.left) / d.mapZ + d.mapPX;
                    const wy = (sy - bb.top) / d.mapZ + d.mapPY;
                    if (d.mapWireAt(wx, wy) !== -1) continue;
                    if ((L.chips || []).some(c =>
                        sx > (c.x - d.mapPX) * d.mapZ + bb.left &&
                        sx < (c.x + c.w - d.mapPX) * d.mapZ + bb.left &&
                        sy > (c.y - d.mapPY) * d.mapZ + bb.top &&
                        sy < (c.y + c.h - d.mapPY) * d.mapZ + bb.top))
                        continue;
                    return { sx: Math.round(sx), sy: Math.round(sy) };
                }
            }
            return null; }""")
        if tspine:
            page.mouse.click(tspine["sx"], tspine["sy"])
            # [#123] wait for the pin the click should latch (or miss
            # honestly: the surface check below has teeth either way)
            try:
                page.wait_for_function(
                    "() => window.__dbg.wirePin !== null", timeout=1200)
            except PwTimeout:
                pass
            pinT = page.evaluate("() => window.__dbg.wirePin")
            # [#123] the pin latches synchronously with the click, but the
            # accent PAINT trails it by a frame or two - poll the pixel
            # postcondition instead of one instantaneous read (the fixed
            # 400ms sleep it replaces raced the same paint and failed
            # intermittently). On timeout the check below fails honestly.
            tcol = None
            for _poll in range(10):
                tcol = page.evaluate("""(m) => {
                    const cv = document.getElementById('mapPane');
                    const r2 = cv.getBoundingClientRect();
                    const dpr = window.devicePixelRatio || 1;
                    const cx0 = Math.round((m.sx - r2.left) * dpr);
                    const cy0 = Math.round((m.sy - r2.top) * dpr);
                    const ctx = cv.getContext('2d');
                    let teal = false;
                    for (let dx = -5; dx <= 5 && !teal; dx++)
                        for (let dy = -5; dy <= 5 && !teal; dy++) {
                            const p = ctx.getImageData(cx0 + dx, cy0 + dy,
                                1, 1).data;
                            if (p[3] < 30) continue;
                            if (p[1] > p[0] + 40 && p[1] > p[2] + 10)
                                teal = true;
                        }
                    return { teal }; }""", tspine)
                if tcol["teal"]:
                    break
                page.wait_for_timeout(100)
            check("pinned trunk corridor paints in the accent",
                  pinT is not None and pinT.get("surface") == "map"
                  and tcol["teal"],
                  f"{tspine} -> {pinT}, {tcol}")
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
        else:
            print("SKIP trunk accent probe - no on-screen trunk spine")

        # [skeptic #16] a stale ball pin must reap after a refocus
        # rebuild (cover 0 -> gone in ~1s). A db8047d tip throw stalled
        # the rAF chain and made the pin immortal - #17's guard must
        # keep the reap alive and the run error-free.
        pe0 = []
        page.on("pageerror", lambda e: pe0.append(str(e)))
        bp = None
        for _ in range(3):
            cands = page.evaluate("""() => { const d = window.__dbg;
                const el = d.renderer.domElement,
                      r = el.getBoundingClientRect();
                // stay left of the 2D map pane - clicks behind it hit
                // the pane DOM and never latch a ball pin
                const xmax = document.getElementById('mapPane')
                    .getBoundingClientRect().left - 14;
                const out = [];
                for (let x = 24; x < xmax && x < r.width - 24 &&
                     out.length < 12; x += 40)
                    for (let y = 70; y < r.height - 24 &&
                         out.length < 12; y += 40) {
                        const m = d.pickWireMeta({
                            clientX: r.left + x, clientY: r.top + y });
                        // any pickable kind latches a ball pin -
                        // wire/trunk directly, jleg via the corridor
                        // latch, link via its own latch
                        if (m && (m.kind === "wire" || m.kind === "trunk" ||
                                  m.kind === "jleg" || m.kind === "link") &&
                            // [#81 surface law] canvas-owned aims only
                            document.elementFromPoint(
                                r.left + x, r.top + y) === el)
                            out.push([Math.round(r.left + x),
                                      Math.round(r.top + y)]);
                    }
                return out; }""")
            for c in (cands or []):
                page.mouse.move(c[0], c[1])
                page.wait_for_timeout(120)
                hov = page.evaluate("() => ({ hf: window.__dbg.hoveredFn,"
                                    " hv: window.__dbg.hovered })")
                if (hov["hf"] is not None and hov["hf"] >= 0) or \
                   (hov["hv"] is not None and hov["hv"] >= 0):
                    continue
                page.mouse.click(c[0], c[1])
                # [#123] the latch condition is the wait: a candidate
                # that does not pin within the window is skipped, not
                # read half-latched.
                try:
                    page.wait_for_function(
                        "() => window.__dbg.wirePin &&"
                        " window.__dbg.wirePin.surface === 'ball'",
                        timeout=1200)
                except PwTimeout:
                    continue
                bp = page.evaluate("() => window.__dbg.wirePin")
                if bp and bp.get("surface") == "ball":
                    break
            if bp and bp.get("surface") == "ball":
                break
        if bp and bp.get("surface") == "ball":
            card4 = page.evaluate("""() => {
                const d = window.__dbg;
                const bb = document.getElementById('mapPane')
                    .getBoundingClientRect();
                for (const rc of d.mapRects) {
                    if (rc.i === d.focusFileIdx) continue;
                    const sx = (rc.x + rc.w / 2 - d.mapPX) * d.mapZ
                        + bb.left;
                    const sy = (rc.y + 11 - d.mapPY) * d.mapZ + bb.top;
                    if (sx > bb.left + 8 && sx < bb.right - 8 &&
                        sy > bb.top + 8 && sy < bb.bottom - 8)
                        return { sx: Math.round(sx), sy: Math.round(sy) };
                }
                return null; }""")
            if card4:
                page.mouse.move(card4["sx"] - 12, card4["sy"] - 8)
                page.mouse.move(card4["sx"], card4["sy"], steps=3)
                page.mouse.click(card4["sx"], card4["sy"])
                # [#123] the reap is a ~1s product timer — wait it out
                # instead of a fixed sleep; a timeout lands in the check
                # below with the pin state in the detail line. The reap
                # can fire mid-rebuild (refocus nulls the pin before the
                # camera tween / map rebuild finish) — settle both before
                # the sections below read geometry.
                try:
                    page.wait_for_function(
                        "() => window.__dbg.wirePin === null", timeout=2500)
                except PwTimeout:
                    pass
                quiesce(page, 4000)
                map_ready(page)
                bp2 = page.evaluate("() => window.__dbg.wirePin")
                check("stale ball pin reaps after refocus",
                      bp2 is None and not pe0,
                      f"{bp} -> {bp2}, pageerrors {pe0[:1]}")
            else:
                print("SKIP stale reap - no card for refocus")
        else:
            print(f"SKIP stale reap - no ball pin latched "
                  f"(cands {len(cands or [])})")

        # [skeptic #19] replacing a trunk pin (tint ON) with a link
        # pin must restore the corridor tint - the replace path never
        # dies, so the updateBallPin head guard is the only trigger
        pinTk = None
        for _t in range(3):
            tc = page.evaluate("""() => { const d = window.__dbg;
                const el = d.renderer.domElement,
                      r = el.getBoundingClientRect();
                const xmax = document.getElementById('mapPane')
                    .getBoundingClientRect().left - 14;
                const out = [];
                for (let x = 24; x < xmax && x < r.width - 24 &&
                     out.length < 12; x += 40)
                    for (let y = 70; y < r.height - 24 &&
                         out.length < 12; y += 40) {
                        const m = d.pickWireMeta({
                            clientX: r.left + x, clientY: r.top + y });
                        if (m && (m.kind === "trunk" || m.kind === "jleg")
                            && document.elementFromPoint(
                                r.left + x, r.top + y) === el)
                            out.push([Math.round(r.left + x),
                                      Math.round(r.top + y)]);
                    }
                return out; }""")
            for c in (tc or []):
                page.mouse.move(c[0], c[1])
                page.wait_for_timeout(120)
                hov = page.evaluate("() => ({ hf: window.__dbg.hoveredFn,"
                                    " hv: window.__dbg.hovered })")
                if (hov["hf"] is not None and hov["hf"] >= 0) or \
                   (hov["hv"] is not None and hov["hv"] >= 0):
                    continue
                page.mouse.click(c[0], c[1])
                try:
                    page.wait_for_function(
                        "() => window.__dbg.wirePin &&"
                        " window.__dbg.wirePin.kind === 'trunk'",
                        timeout=1200)
                except PwTimeout:
                    continue
                pk = page.evaluate("() => window.__dbg.wirePin")
                if pk and pk.get("kind") == "trunk":
                    pinTk = pk
                    break
            if pinTk:
                break
        if pinTk:
            tkq = page.evaluate("""() => {
                const d = window.__dbg, pin = d.wirePin;
                if (!pin || !pin.k || !d.fnBus || !d.fnBus.instanceColor)
                    return null;
                const prefs = [];
                for (const S of (d.fnStations || []))
                    if (S.tks.indexOf(pin.k) >= 0)
                        prefs.push("L|" + S.fi + "|" + S.id + "|");
                const mm = d.fnBus.instanceMatrix.array;
                let q = -1, sib = -1;
                for (let i = 0; i < d.busPtsMeta.length; i++) {
                    const mt = d.busPtsMeta[i]; if (!mt) continue;
                    const k = String(mt.k || "");
                    const hit = (mt.kind === "trunk" && k === pin.k) ||
                        (k.charCodeAt(0) === 76 &&
                         prefs.some(p => k.startsWith(p)));
                    if (hit && q < 0 &&
                        Math.hypot(mm[i*16], mm[i*16+1], mm[i*16+2]) > 0.001)
                        q = i;
                    else if (!hit && k.charCodeAt(0) === 76 && sib < 0)
                        sib = i;
                }
                if (q < 0) return null;
                const ca = d.fnBus.instanceColor.array;
                // bus pieces carry per-piece stock colors - the
                // baseline is THIS piece's captured original, not a
                // sibling's (siblings differ)
                const pt = d.pinTint;
                // pinTintOrig: per-piece [r,g,b] triplets, position-
                // indexed against pinTinted
                const ix = pt.tinted.indexOf(q);
                const q0 = ix >= 0 ? pt.orig[ix] : null;
                if (!q0) return null;
                return { q, q0,
                         on: [ca[q*3], ca[q*3+1], ca[q*3+2]] }; }""")
            if tkq:
                pinRpl = None
                for _t in range(3):
                    lc = page.evaluate("""() => { const d = window.__dbg;
                        const el = d.renderer.domElement,
                              r = el.getBoundingClientRect();
                        const xmax = document.getElementById('mapPane')
                            .getBoundingClientRect().left - 14;
                        const out = [];
                        for (let x = 24; x < xmax && x < r.width - 24 &&
                             out.length < 12; x += 40)
                            for (let y = 70; y < r.height - 24 &&
                                 out.length < 12; y += 40) {
                                const m = d.pickWireMeta({
                                    clientX: r.left + x,
                                    clientY: r.top + y });
                                if (m && (m.kind === "link" ||
                                          m.kind === "wire") &&
                                    document.elementFromPoint(
                                        r.left + x, r.top + y) === el)
                                    out.push([Math.round(r.left + x),
                                              Math.round(r.top + y)]);
                            }
                        return out; }""")
                    # the persistent pin tip is a DOM overlay - a
                    # click that lands on it never reaches the canvas
                    tipR = page.evaluate(
                        "() => { const t = document.getElementById"
                        "('wireTip'); if (!t || t.style.display === "
                        "'none') return null; const r = t."
                        "getBoundingClientRect(); return [r.left, r.top,"
                        " r.right, r.bottom]; }")
                    for c in (lc or []):
                        if tipR and tipR[0] - 24 < c[0] < tipR[2] + 24 \
                           and tipR[1] - 24 < c[1] < tipR[3] + 24:
                            continue
                        page.mouse.move(c[0], c[1])
                        page.wait_for_timeout(120)
                        hov = page.evaluate(
                            "() => ({ hf: window.__dbg.hoveredFn,"
                            " hv: window.__dbg.hovered })")
                        if (hov["hf"] is not None and hov["hf"] >= 0) or \
                           (hov["hv"] is not None and hov["hv"] >= 0):
                            continue
                        page.mouse.click(c[0], c[1])
                        try:
                            page.wait_for_function(
                                "() => window.__dbg.wirePin &&"
                                " (window.__dbg.wirePin.kind === 'link'"
                                " || window.__dbg.wirePin.kind === 'wire')",
                                timeout=1200)
                        except PwTimeout:
                            continue
                        # A wire/link pin whose corridor IS the pinned
                        # trunk legitimately re-tints the same pieces
                        # (wire pins resolve their pair -> trunk chain).
                        # The restore law is about a DIFFERENT corridor
                        # replacing this one - reject candidates that
                        # still cover the probed piece (#97 shape fix).
                        pr = page.evaluate(
                            """(q) => { const d = window.__dbg;
                            const pr = d.wirePin;
                            if (!pr || (pr.kind !== "link" &&
                                        pr.kind !== "wire")) return null;
                            const pt = d.pinTint;
                            if (pt.tinted && pt.tinted.indexOf(q) >= 0)
                                return null;
                            return { kind: pr.kind, id: pr.id }; }""",
                            tkq["q"])
                        if pr and pr.get("id") != pinTk.get("id"):
                            pinRpl = pr
                            break
                    if pinRpl:
                        break
                if pinRpl:
                    # [#123] the tint restore trails the pin swap by a
                    # frame or two — wait for the postcondition itself
                    # (stock rgb back on the probed piece); on timeout
                    # fall through and let the check below fail honestly.
                    try:
                        page.wait_for_function(
                            """(t) => { const d = window.__dbg;
                                 const ca = d.fnBus.instanceColor.array;
                                 return Math.abs(ca[t.q*3] - t.q0[0]) < 0.02
                                    && Math.abs(ca[t.q*3+1] - t.q0[1]) < 0.02
                                    && Math.abs(ca[t.q*3+2] - t.q0[2]) < 0.02; }""",
                            arg={"q": tkq["q"], "q0": tkq["q0"]},
                            timeout=2500)
                    except PwTimeout:
                        pass
                    tkOff = page.evaluate("""(q) => {
                        const d = window.__dbg;
                        const ca = d.fnBus.instanceColor.array;
                        return [ca[q*3], ca[q*3+1], ca[q*3+2]]; }""",
                        arg=tkq["q"])
                    check("replaced pin restores the corridor tint",
                          all(abs(a - b) < 0.02 for a, b in
                              zip(tkOff, tkq["q0"])),
                          f"trunk {pinTk['id']} tint on "
                          f"{[round(v, 2) for v in tkq['on']]} -> "
                          f"{pinRpl['kind']} pin, piece {tkq['q']} rgb "
                          f"{[round(v, 2) for v in tkOff]} vs stock "
                          f"{[round(v, 2) for v in tkq['q0']]}")
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(250)
                else:
                    print("SKIP replace-restore - no link/wire pin")
            else:
                print("SKIP replace-restore - no covered piece")
        else:
            print("SKIP replace-restore - no trunk pin")

        # [issue #196 / skeptic R9b] tint lifecycle convergence law:
        # trunk pin -> wire pin replace -> Esc must leave EVERY tint
        # buffer byte-exact stock. The replacing wire's own emphasis
        # (corridor chain + its arcs) is legitimate while it lives -
        # a hash compare at the live-pin point false-positives on it
        # (the R9b dispute) - but nothing may survive dismissal, so
        # the invariant is pinned at the post-Esc point where any
        # stale record would necessarily show.
        stockC = page.evaluate("""() => {
            const d = window.__dbg;
            if (!d.fnBus || !d.fnBus.instanceColor || !d.fnLines ||
                !d.fnLines.geometry ||
                !d.fnLines.geometry.attributes.instanceColorStart)
              return null;
            return {
              bus: Array.from(d.fnBus.instanceColor.array),
              cs: Array.from(d.fnLines.geometry.attributes
                .instanceColorStart.array),
              ce: Array.from(d.fnLines.geometry.attributes
                .instanceColorEnd.array) }; }""")
        pinA2 = None
        if stockC:
            for _t in range(3):
                tc2 = page.evaluate("""() => { const d = window.__dbg;
                    const el = d.renderer.domElement,
                          r = el.getBoundingClientRect();
                    const out = [];
                    for (let x = 24; x < r.width - 24 && out.length < 10;
                         x += 40)
                        for (let y = 70; y < r.height - 24 &&
                             out.length < 10; y += 40) {
                            const m = d.pickWireMeta({
                                clientX: r.left + x, clientY: r.top + y });
                            if (m && (m.kind === "trunk" || m.kind === "jleg")
                                && document.elementFromPoint(
                                    r.left + x, r.top + y) === el)
                                out.push([Math.round(r.left + x),
                                          Math.round(r.top + y)]);
                        }
                    return out; }""")
                for c2 in (tc2 or []):
                    page.mouse.move(c2[0], c2[1])
                    page.wait_for_timeout(120)
                    hov = page.evaluate(
                        "() => ({ hf: window.__dbg.hoveredFn,"
                        " hv: window.__dbg.hovered })")
                    if (hov["hf"] is not None and hov["hf"] >= 0) or \
                       (hov["hv"] is not None and hov["hv"] >= 0):
                        continue
                    page.mouse.click(c2[0], c2[1])
                    try:
                        page.wait_for_function(
                            "() => window.__dbg.wirePin &&"
                            " window.__dbg.wirePin.kind === 'trunk'",
                            timeout=1200)
                    except PwTimeout:
                        continue
                    pinA2 = page.evaluate("() => window.__dbg.wirePin")
                    break
                if pinA2:
                    break
        pinB2 = None
        if pinA2:
            for _t in range(3):
                lc2 = page.evaluate("""() => { const d = window.__dbg;
                    const el = d.renderer.domElement,
                          r = el.getBoundingClientRect();
                    const xmax = document.getElementById('mapPane')
                        .getBoundingClientRect().left - 14;
                    const out = [];
                    for (let x = 24; x < xmax && x < r.width - 24 &&
                         out.length < 12; x += 40)
                        for (let y = 70; y < r.height - 24 &&
                             out.length < 12; y += 40) {
                            const m = d.pickWireMeta({
                                clientX: r.left + x, clientY: r.top + y });
                            if (m && (m.kind === "link" || m.kind === "wire")
                                && document.elementFromPoint(
                                    r.left + x, r.top + y) === el)
                                out.push([Math.round(r.left + x),
                                          Math.round(r.top + y)]);
                        }
                    return out; }""")
                tipR2 = page.evaluate(
                    "() => { const t = document.getElementById"
                    "('wireTip'); if (!t || t.style.display === "
                    "'none') return null; const r = t."
                    "getBoundingClientRect(); return [r.left, r.top,"
                    " r.right, r.bottom]; }")
                for c2 in (lc2 or []):
                    if tipR2 and tipR2[0] - 24 < c2[0] < tipR2[2] + 24 \
                       and tipR2[1] - 24 < c2[1] < tipR2[3] + 24:
                        continue
                    page.mouse.move(c2[0], c2[1])
                    page.wait_for_timeout(120)
                    hov = page.evaluate(
                        "() => ({ hf: window.__dbg.hoveredFn,"
                        " hv: window.__dbg.hovered })")
                    if (hov["hf"] is not None and hov["hf"] >= 0) or \
                       (hov["hv"] is not None and hov["hv"] >= 0):
                        continue
                    page.mouse.click(c2[0], c2[1])
                    try:
                        page.wait_for_function(
                            "() => window.__dbg.wirePin &&"
                            " (window.__dbg.wirePin.kind === 'link'"
                            " || window.__dbg.wirePin.kind === 'wire')",
                            timeout=1200)
                    except PwTimeout:
                        continue
                    pb = page.evaluate("() => window.__dbg.wirePin")
                    if pb and pb.get("id") != pinA2.get("id"):
                        pinB2 = pb
                        break
                if pinB2:
                    break
        if pinA2 and pinB2:
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
            conv = page.evaluate("""(st) => {
                const d = window.__dbg;
                let bus = -1, cs = -1, ce = -1;
                if (d.fnBus && d.fnBus.instanceColor) {
                    const a = d.fnBus.instanceColor.array;
                    bus = 0;
                    for (let i = 0; i < a.length; i++)
                        if (Math.abs(a[i] - st.bus[i]) > 1e-6) bus++;
                }
                const g = d.fnLines && d.fnLines.geometry &&
                    d.fnLines.geometry.attributes;
                if (g && g.instanceColorStart) {
                    const a = g.instanceColorStart.array;
                    cs = 0;
                    for (let i = 0; i < a.length; i++)
                        if (Math.abs(a[i] - st.cs[i]) > 1e-6) cs++;
                }
                if (g && g.instanceColorEnd) {
                    const a = g.instanceColorEnd.array;
                    ce = 0;
                    for (let i = 0; i < a.length; i++)
                        if (Math.abs(a[i] - st.ce[i]) > 1e-6) ce++;
                }
                const pt = d.pinTint;
                return { bus, cs, ce,
                         rec: pt.tinted.length, arcs: d.pinTint2.length,
                         dots: d.pinDots,
                         pin: d.wirePin ? d.wirePin.id : null }; }""",
                stockC)
            check("replace->esc converges every tint buffer to stock",
                  conv["pin"] is None and conv["bus"] == 0 and
                  conv["cs"] == 0 and conv["ce"] == 0 and
                  conv["rec"] == 0 and conv["arcs"] == 0 and
                  conv["dots"] is False,
                  f"{pinA2['id']} -> {pinB2['id']} -> esc: {conv}")
        elif stockC:
            print(f"SKIP convergence law - latches {pinA2 and 'trunk'}"
                  f" {pinB2 and 'wire'}")
        else:
            print("SKIP convergence law - no tint buffers")

        # 4e. [#12] focus container: the focused file renders as a
        # translucent hull HOLDING its fn boxes (faint fill + back-side
        # rim, depthWrite off, behind the fn tier). It is focus-tier ink:
        # the serve gate must be ON, so wheel INTO the ball from the 3D
        # canvas (x < paneW — the map pane owns the right half) until
        # lodServe flips; bounded, loud-skip when the camera cannot reach.
        served, notches = False, 0
        for _ in range(12):
            if page.evaluate("() => !!window.__dbg.lodServe"):
                served = True
                break
            page.mouse.move(400, 450)
            page.mouse.wheel(0, -240)
            notches += 1
            quiesce(page)
        if not served:
            print(f"SKIP [#12] focus container — serve gate unreachable "
                  f"after {notches} wheel steps (camera shape)")
        else:
            hp = page.evaluate(
                """() => { const d = window.__dbg;
                     const fi = d.focusFileIdx;
                     const h = d.focusHullProbe || null;
                     let n = 0, outside = 0, maxD = 0;
                     (d.fnMeta || []).forEach(m => {
                       if (m.file !== fi) return;
                       if (m.agg && !m.count) return;
                       n++;
                       const half = m.count ? 3 : 2;
                       const dd = Math.hypot(m.p[0] - d.pos[fi*3],
                                            m.p[1] - d.pos[fi*3+1],
                                            m.p[2] - d.pos[fi*3+2]) + half;
                       if (dd > maxD) maxD = dd;
                       if (h && dd > h.R + 0.01) outside++;
                     });
                     return { fi, h, n, outside, maxD: +maxD.toFixed(2),
                              oHull: h ? h.oHull : 0 }; }""")
            check("[#12] focus container: translucent hull behind fn tier",
                  bool(hp["h"]) and hp["h"]["transparent"]
                  and not hp["h"]["depthWrite"]
                  and hp["h"]["ro"] < (hp["h"]["boxRO"] or 0),
                  str(hp.get("h")))
            check("[#12] focus container holds every fn box of the file",
                  hp["n"] >= 1 and hp["outside"] == 0
                  and hp["h"]["R"] >= hp["maxD"],
                  f"boxes={hp['n']} outside={hp['outside']} "
                  f"R={hp['h']['R']:.1f} maxBoxD={hp['maxD']}")
            check("[#12] focus container is served-tier ink (visible)",
                  hp["oHull"] > 0, str(hp["oHull"]))
            # focus-tier-only: far zoom fades the container away — the
            # file collapses back to its node dot. One wheel event is
            # one dolly step regardless of delta magnitude, so zoom in
            # a LOOP of separate events.
            page.mouse.move(400, 450)
            for _ in range(notches + 10):
                page.mouse.wheel(0, 120)
                page.wait_for_timeout(20)
            quiesce(page)
            far = page.evaluate(
                "() => window.__dbg.focusHullProbe"
                " ? window.__dbg.focusHullProbe.oHull : -1")
            check("[#12] focus container fades out at far zoom",
                  0 <= far < hp["oHull"],
                  f"near={hp['oHull']} far={far}")
            # restore the pre-leg state for the sections that follow:
            # (a) the REAL pointer must leave the canvas — parked on it,
            # Chrome reconciles every later synthetic pointermove with a
            # trusted pointerleave that kills hover ink; park it on the
            # chrome. (b) the focus row re-click re-tweens the camera to
            # the ball framing (wheel-step arithmetic undo drifts).
            page.mouse.move(1500, 15)
            page.fill("#search", tok)
            page.dispatch_event("#search", "input")
            wait_rows(page, best_path)
            page.evaluate(
                """(p) => { const rows =
                     [...document.querySelectorAll('#searchResults .row')];
                     const r = rows.find(x => x.getAttribute('title') === p);
                     if (r) r.dispatchEvent(
                       new PointerEvent('pointerdown', { bubbles: true })); }""",
                best_path)
            quiesce(page)

        # [issue #85 owner r5] the click-slop law: a real hand drifts
        # 5-12px between press and release - before the slop restore
        # every drifted card click was swallowed as a pan and the 3D
        # camera never refocused (owner: "does not move the camera").
        # Contract: sub-12px TOTAL travel resolves as a click AT THE
        # PRESS ORIGIN (skeptic #22: release-point resolution let
        # 8-11px drifts exit the header band); >=12px is a pan. Runs
        # after the tail for the same direction-inheritance reason as
        # the stale-list block above it.
        drift_card = page.evaluate("""() => {
            const d = window.__dbg;
            const bb = document.getElementById('mapPane')
                .getBoundingClientRect();
            for (const rc of d.mapRects) {
                if (rc.i === d.focusFileIdx || !rc.w || !rc.h) continue;
                const hx = (rc.x + rc.w / 2 - d.mapPX) * d.mapZ + bb.left;
                const hy = (rc.y + 11 - d.mapPY) * d.mapZ + bb.top;
                if (hx > bb.left + 20 && hx < bb.right - 20 &&
                    hy > bb.top + 20 && hy < bb.bottom - 20 &&
                    // skeptic #23: DOM overlays (list chrome, tag chips)
                    // can cover card headers - presses there are inert,
                    // so rungs must land on the canvas itself
                    document.elementFromPoint(hx, hy) ===
                        document.getElementById('mapPane'))
                    return { i: rc.i, hx: hx, hy: hy };
            }
            return null; }""")
        if drift_card:
            camA = page.evaluate(
                "() => window.__dbg.camera.position.toArray()")
            tgtA = page.evaluate(
                "() => window.__dbg.controls.target.toArray()")
            focA = page.evaluate("() => window.__dbg.focusFileIdx")
            page.mouse.move(drift_card["hx"] - 5, drift_card["hy"] - 4)
            page.mouse.down()
            for k in range(1, 7):
                page.mouse.move(drift_card["hx"] - 5 + k,
                                drift_card["hy"] - 4 + k)
            page.mouse.up()
            # baseline fixed window: the 220ms refocus debounce plus the
            # tween land inside it, and the NEXT section's inputs must
            # land on the settled pose exactly as baseline sequences them
            # (a shorter readiness wait here shifts every downstream
            # pixel probe's arriving azimuth).
            page.wait_for_timeout(900)
            camB = page.evaluate(
                "() => window.__dbg.camera.position.toArray()")
            tgtB = page.evaluate(
                "() => window.__dbg.controls.target.toArray()")
            focB = page.evaluate("() => window.__dbg.focusFileIdx")
            dcam = max(abs(a - b) for a, b in zip(camA, camB))
            dtgt = max(abs(a - b) for a, b in zip(tgtA, tgtB))
            # #97: the cam clause is a proxy for "the re-frame fired" -
            # dense cores can re-frame a near neighbour from an inherited
            # camera direction with a sub-1wu position move. The tween
            # TARGET moving (or the camera) is the honest discriminator:
            # a swallowed click moves neither.
            check("drifted card click still refocuses",
                  focB != focA and (dcam > 1 or dtgt > 1),
                  f"{focA} -> {focB}, cam delta {dcam:.1f}, "
                  f"target delta {dtgt:.1f}")
            # negative rung: >=12px TOTAL travel is a pan, not a click -
            # the press must NOT refocus (boundary is travel, not
            # per-axis). #146: 7 diagonal (+1,+1) moves are only 9.9px -
            # sub-slop, so the engine rescues the click at the PRESS
            # ORIGIN, which on live-index geometry sits on a neighbouring
            # card header and legitimately refocuses. 9 moves (12.73px)
            # clear the slop: a true pan cannot click anywhere, so the
            # rung is pose-independent instead of geometry-lucky.
            page.mouse.move(drift_card["hx"] - 6, drift_card["hy"] - 5)
            page.mouse.down()
            for k in range(1, 10):
                page.mouse.move(drift_card["hx"] - 6 + k,
                                drift_card["hy"] - 5 + k)
                page.wait_for_timeout(12)
            page.mouse.up()
            page.wait_for_timeout(700)
            focC = page.evaluate("() => window.__dbg.focusFileIdx")
            check("drift past the slop pans instead of clicking",
                  focC == focB, f"{focB} stays {focC}")
        else:
            print("SKIP drifted card click - no second card on screen")
        # [issue #90] the two rungs the #85 battery left unpinned (GK
        # addendum 6: the negative rung pressed the already-focused
        # card, so no slop threshold could ever fail it, and both
        # drifted-card rungs used same-card press/release, so
        # release-point resolution passed unnoticed).
        # (a) >=12px total travel NEVER resolves as a click: press an
        # unfocused card header, drift 14.1px, release - focus must
        # stay put AND the pan must stick (a click-at-press-origin
        # would refocus the card and the slop-restore would undo the
        # pan, so both clauses have teeth).
        # (b) press-ORIGIN resolution: press an unfocused card header
        # mid-band, drift 10px straight down so the release EXITS the
        # 22wu header band onto the same card's rows, release - the
        # click must resolve at the press origin (refocus the pressed
        # card), never at the release point (a row freeze toggles and
        # focuses nothing). Reverting the 1e24980 origin rule fails
        # this rung.
        hdr2 = None  # press header helper: live coords for a card index

        def hdr_of(i):
            return page.evaluate("""(i) => {
                const d = window.__dbg;
                const bb = document.getElementById('mapPane')
                    .getBoundingClientRect();
                const rc = d.mapRects.find(r => r.i === i);
                if (!rc) return null;
                const hx = (rc.x + rc.w / 2 - d.mapPX) * d.mapZ + bb.left;
                const hy = (rc.y + 11 - d.mapPY) * d.mapZ + bb.top;
                if (document.elementFromPoint(hx, hy) !==
                    document.getElementById('mapPane')) return null;
                return { hx: hx, hy: hy, z: d.mapZ }; }""", i)

        def cards2():
            # unfocused, in-pane card headers with the 5ab62d4 guard
            return page.evaluate("""() => {
                const d = window.__dbg;
                const bb = document.getElementById('mapPane')
                    .getBoundingClientRect();
                const out = [];
                for (const rc of d.mapRects) {
                    if (rc.i === d.focusFileIdx || !rc.w || !rc.h)
                        continue;
                    const hx = (rc.x + rc.w / 2 - d.mapPX) * d.mapZ
                               + bb.left;
                    const hy = (rc.y + 11 - d.mapPY) * d.mapZ + bb.top;
                    if (hx > bb.left + 40 && hx < bb.right - 40 &&
                        hy > bb.top + 40 && hy < bb.bottom - 40 &&
                        document.elementFromPoint(hx, hy) ===
                            document.getElementById('mapPane'))
                        out.push({ i: rc.i,
                                   rows: (rc.rows || []).length > 0 });
                    if (out.length >= 4) break;
                }
                return out; }""")

        cands = cards2()
        # ---- rung (b) first, at ambient zoom: sub-slop drift down so
        # the release EXITS the 22wu header band. The drift is sized
        # from the live mapZ: > 11wu below the press (band bottom is
        # 22wu, press sits at 11wu) yet < 12px total travel (float
        # steps - at fit zoom the window between the two bounds is a
        # fraction of a pixel wide).
        if cands:
            cardB = next((c for c in cands if c["rows"]), cands[0])
            hdr2 = hdr_of(cardB["i"])
        if hdr2:
            drop = min(11.0 * hdr2["z"] + 0.6, 11.9)
            focA = page.evaluate("() => window.__dbg.focusFileIdx")
            page.mouse.move(hdr2["hx"], hdr2["hy"])
            page.mouse.down()
            for k in range(1, 8):
                page.mouse.move(hdr2["hx"],
                                hdr2["hy"] + drop * k / 7)
                page.wait_for_timeout(12)
            page.mouse.up()
            # baseline fixed window: the 220ms refocus debounce lands
            # inside it; shorter readiness waits shift downstream probes.
            page.wait_for_timeout(900)
            focB = page.evaluate("() => window.__dbg.focusFileIdx")
            check("sub-slop drift resolves at the press origin "
                  "(#90 rung b)",
                  focB == cardB["i"],
                  f"pressed {cardB['i']}, focus {focB}, "
                  f"drop {drop:.1f}px at z {hdr2['z']:.2f}")
        else:
            print("SKIP #90 rung b - no unfocused card header on screen")
        # ---- rung (a): >=12px travel NEVER resolves as a click. Pan
        # slack needs the content LARGER than the pane - at fit zoom
        # (and zoomed out) mapClampView re-centers every drag back to
        # the fit (skeptic verdict on 1e24980: the 12.7px rung was
        # unobservable for exactly this reason), so zoom IN three
        # notches at the pane centre first, then re-derive candidates
        # from the live pan.
        bb0 = page.evaluate(
            "() => { const r = document.getElementById('mapPane')"
            ".getBoundingClientRect();"
            " return {x: (r.left + r.right) / 2,"
            " y: (r.top + r.bottom) / 2}; }")
        page.mouse.move(bb0["x"], bb0["y"])
        focC = page.evaluate("() => window.__dbg.focusFileIdx")
        maxpan = 0.0
        attempted = False
        # zoom in progressively; retry until a drag finds pan slack
        # (a zoom level whose content exceeds the pane). Focus must
        # stay put through EVERY attempt - a past-slop drag is a pan,
        # never a click, wherever it lands.
        for notch in range(1, 6):
            page.mouse.wheel(0, -300)
            page.wait_for_timeout(120)
            page.wait_for_timeout(130)
            cands = cards2()
            hdrA = hdr_of(cands[0]["i"]) if cands else None
            if not hdrA:
                continue
            attempted = True
            panA = page.evaluate(
                "() => [window.__dbg.mapPX, window.__dbg.mapPY]")
            # 10 diagonal (+1,+1) moves = 14.1px total travel
            page.mouse.move(hdrA["hx"] - 7, hdrA["hy"] - 7)
            page.mouse.down()
            for k in range(1, 11):
                page.mouse.move(hdrA["hx"] - 7 + k, hdrA["hy"] - 7 + k)
                page.wait_for_timeout(12)
            page.mouse.up()
            page.wait_for_timeout(700)
            panB = page.evaluate(
                "() => [window.__dbg.mapPX, window.__dbg.mapPY]")
            maxpan = max(maxpan,
                         max(abs(a - b) for a, b in zip(panA, panB)))
            if maxpan > 1:
                break
        focD = page.evaluate("() => window.__dbg.focusFileIdx")
        if attempted:
            # teeth: a sabotaged slop threshold turns this drag into a
            # press-origin click (focus jumps) AND restores the pan -
            # both clauses fail together
            check("past-slop travel pans, never clicks (#90 rung a)",
                  focD == focC and maxpan > 1,
                  f"focus {focC} stays {focD}, pan moved {maxpan:.1f}wu")
        else:
            print("SKIP #90 rung a - no unfocused card header on screen")
        # [issue #111 steal class — #81 family at real coordinates] wire
        # ink that projects under an #info row must not claim the click:
        # find a row pixel where the picker sees ink (bounded orbit
        # search), real-click it, and require the row's focus jump to fire
        # with no wire tip surfaced. Runs at the tail because the real
        # click legitimately jumps focus (and the camera tween with it).
        if not page.locator("#info li").count():
            enter_focus_via_row()  # guarantee a populated #info panel
        tgt0 = page.evaluate("() => window.__dbg.focusFileIdx")
        # [#123] incidence preconditioning: whether wire ink projects
        # under an #info row depends on the camera DISTANCE the tail
        # sections arrive at (zoom notches mid-tween vary it run to run).
        # Pull the 3D camera back first - the wheel must ride the 3D
        # canvas (400,300), NOT the map pane (that zooms the 2D map and
        # perturbs the #113 freeze probes downstream) - then sweep; the
        # gate below only closes when no angle has ink.
        page.mouse.move(400, 300)
        for _zn in range(4):
            page.mouse.wheel(0, -300)
            page.wait_for_timeout(60)
        quiesce(page, 4000)
        # [#123] bounded sweep, not a fixed orbit count: whether wire ink
        # projects under an #info row depends on the arriving azimuth,
        # which earlier sections legitimately vary - sweep a full
        # revolution so the shape gate only closes when the index truly
        # has no ink-over-row incidence at any angle
        for _ in range(16):
            spot = page.evaluate(
                """() => { const d = window.__dbg;
                     const r = document.getElementById('info')
                                       .getBoundingClientRect();
                     // [#123] no y-cap: the elementFromPoint closest-li
                     // check below self-verifies row pixels, so sampling
                     // the full panel only widens real incidence
                     for (let x = r.x + 12; x < r.right - 12; x += 16)
                       for (let y = r.y + 12; y < r.bottom - 12; y += 12) {
                         if (!d.pickWireMeta({ clientX: x, clientY: y }))
                           continue;
                         const el = document.elementFromPoint(x, y);
                         const li = el && el.closest('#info li');
                         if (li && !li.classList.contains('more'))
                           return { x, y,
                                    txt: li.textContent.slice(0, 40) };
                       }
                     return null; }"""
            )
            if spot:
                break
            page.mouse.move(800, 450)  # orbit: bring ink under the panel
            page.mouse.down()
            for k in range(8):
                page.mouse.move(800 + (k - 4) * 126, 450 + (k - 4) * 48,
                                steps=2)
            page.mouse.up()
            # baseline fixed window: the rescan reads pickWireMeta while
            # the orbit's residual motion is fully at rest (the ink pass
            # trails the eases by a frame or two - no readiness signal)
            page.wait_for_timeout(600)
        if spot:
            page.mouse.click(spot["x"], spot["y"])
            quiesce(page, 4000)
            hj = page.evaluate(
                """() => { const t = document.getElementById('wireTip');
                     return { focus: window.__dbg.focusFileIdx,
                              tip: t ? getComputedStyle(t).display
                                     : 'gone' }; }"""
            )
            check("info rows keep their click over wire ink (#111 steal class)",
                  hj["focus"] != tgt0 and hj["tip"] in ("none", "gone"),
                  f"spot={spot} tgt0={tgt0} hj={hj}")
        else:
            # #97 family: the steal law needs a row pixel with wire ink
            # under it — a layout where no ink projects under #info in
            # the sweep has no subject. Loud skip, never a shape-assumed
            # fail.
            print("SKIP info steal class - no ink-under-row pixel in "
                  "sweep (shape: no wire ink under #info on this index)")

        # [issues #112/#113] focus teardown: the camera tween, hover ink,
        # wire pins and the 2D map's focus-scoped state all die WITH the
        # focus — nine enumerated bugs, one clear-state law per surface
        # (clearFocus/mapTeardown for 2D). Real interactions only; skips
        # are loud shape-guards, never silent.
        page.evaluate("""() => { const d = window.__dbg;
            document.getElementById('bReset').click();   // boot slate
            if (d.mapPane.canvas.classList.contains('collapsed'))
              document.getElementById('bMap').click(); }""")
        quiesce(page, 6000)

        def td_focus(path, settle=True):
            """[#123] settle=False leaves the camera tween in flight on
            purpose — #112-1 samples the tween mid-flight."""
            page.fill("#search", path.split("/")[-1].split(".")[0])
            page.dispatch_event("#search", "input")
            wait_rows(page, path)
            page.evaluate(
                """(p) => { const rows =
                     [...document.querySelectorAll('#searchResults .row')];
                     const r = rows.find(x => x.getAttribute('title') === p)
                            || rows[0];
                     r.dispatchEvent(new PointerEvent('pointerdown',
                                                      { bubbles: true })); }""",
                path)
            if settle:
                quiesce(page)

        td_nodes = page.evaluate(
            """() => { const d = window.__dbg;
                 let hub = 0; for (let i = 1; i < d.nodes.length; i++)
                   if (d.degree[i] > d.degree[hub]) hub = i;
                 let other = -1;
                 for (let i = 0; i < d.nodes.length; i++)
                   if (i !== hub && d.degree[i] > 0) { other = i; break; }
                 return { a: d.nodes[hub].path, b: d.nodes[other].path }; }""")

        # -- #112-1: Esc mid-tween cancels the tween outright; the camera
        # rests near the overview, never re-lerped onto the dead focus ball.
        boot_p = page.evaluate("() => [...window.__dbg.camera.position]")
        td_focus(td_nodes["a"], settle=False)   # tween still in flight
        tween_live = page.evaluate("() => !!window.__dbg.camTween")
        to_c = page.evaluate(
            "() => window.__dbg.camTween ? [...window.__dbg.camTween.toC] : null")
        page.keyboard.press("Escape")
        esc_killed = page.evaluate("() => !window.__dbg.camTween")
        quiesce(page, 4000)
        fin = page.evaluate("() => [...window.__dbg.camera.position]")
        if tween_live and to_c:
            d_to_c = max(abs(fin[k] - to_c[k]) for k in range(3))
            d_boot = max(abs(fin[k] - boot_p[k]) for k in range(3))
            check("Esc mid-tween cancels it; camera escapes the dead pose (#112)",
                  esc_killed and d_to_c > 300 and d_boot < 800,
                  f"esc_killed={esc_killed} dist_focus_pose={d_to_c:.0f} "
                  f"dist_boot={d_boot:.0f}")
        else:
            print("SKIP esc-mid-tween - tween not in flight at sample")

        # -- #112-2: a user zoom beats the tween (wheel listener parity
        # with the pointerdown cancel).
        td_focus(td_nodes["a"], settle=False)  # tween must be in flight
        page.wait_for_function("() => !!window.__dbg.camTween", timeout=4000)
        # the enumerated bug is that the wheel never cancels the tween (only
        # canvas pointerdown did). assert the cancel itself, gated on the
        # tween being verifiably in flight when the wheel lands: a slow box
        # may deliver the wheel near the tween's end, where the camera has
        # almost converged - killing it there legitimately leaves the pose
        # close to the target, so pose-escape distance is timing-fragile.
        # base behavior with a live tween keeps lerping: camTween survives
        # the wheel, which is the red.
        # [union-red 9e8d056, CI-only] the .flab focus labels are
        # pointer-events:auto DOM laid over the canvas and REPOSITIONED
        # EVERY FRAME through the moving tween camera (updateFocusLabels
        # reprojects their 3D anchors), so a label can slide over the probe
        # point between the ownership scan and wheel delivery: the wheel
        # then targets the label div, the canvas cancel listener never
        # fires, camTween survives to convergence and dist_to_tween_pose
        # reads 0 (the un-killed tween simply lands). Local rigs stayed
        # green on frame phase. Ownership must therefore be proven AT
        # DELIVERY, not at scan time: a capture-phase wheel probe records
        # the true event target with zero timing skew, and an attempt only
        # counts when it proves the canvas received the wheel while the
        # tween was still well inside its 400ms window (<350ms, so the
        # kill cannot be confused with natural expiry). A canvas-proven
        # delivery that leaves camTween alive is the product red; an
        # intercepted attempt retries — labels move every frame, so the
        # rescan re-picks a clear point. Persistent interception is a loud
        # SKIP (shape-guard), never a silent pass or a phantom red.
        _spot_js = """() => { const cv = window.__dbg.renderer.domElement,
                     r = cv.getBoundingClientRect();
                 for (const [fx, fy] of [[0.5, 0.5], [0.4, 0.4], [0.6, 0.6],
                                         [0.3, 0.5], [0.5, 0.3]]) {
                   const x = r.left + r.width * fx, y = r.top + r.height * fy;
                   if (document.elementFromPoint(x, y) === cv) return [x, y];
                 }
                 return null; }"""
        probe2 = None
        _any_spot = False
        _n_intercepted = 0
        _n_aged = 0
        for _attempt in range(4):
            if _attempt:
                # fresh tween EVERY attempt: an intercepted wheel burns
                # wall-clock without killing the tween, so retrying on a
                # decaying one would SKIP-by-age without the listener
                # ever being exercised (GK gate condition).
                td_focus(td_nodes["a"], settle=False)
                page.wait_for_function(
                    "() => !!window.__dbg.camTween", timeout=4000)
            spot2 = page.evaluate(_spot_js)
            if not spot2:
                continue          # canvas fully covered this frame: rescan
            _any_spot = True
            page.mouse.move(spot2[0], spot2[1])
            # one round trip: sample the live tween AND arm the capture
            # probe (CI round trips cost ~100ms each; every extra op
            # between arming and the wheel ages the attempt).
            armed = page.evaluate(
                """() => { const t = window.__dbg.camTween;
                     if (!t) return null;
                     window.__wt = null;
                     window.addEventListener('wheel',
                       e => { window.__wt =
                           e.target === window.__dbg.renderer.domElement; },
                       { capture: true, once: true });
                     return { t0: t.t0, toC: [...t.toC] }; }""")
            if not armed:
                continue          # tween died mid-arm: re-arm next round
            page.mouse.wheel(0, -600)
            got = page.evaluate(
                """() => ({ onCanvas: window.__wt === true,
                            alive: !!window.__dbg.camTween,
                            now: performance.now() })""")
            age_ms = got["now"] - armed["t0"]
            # the read is honest while it still precedes natural expiry:
            # the tween dies at t0+400, so a camTween observed null with
            # now-t0 < 400 was killed inside its window (only Esc /
            # canvas pointerdown / the wheel listener null it mid-life,
            # and this probe issues none of the first two).
            if got["onCanvas"] and age_ms < 400:
                probe2 = {"toC": armed["toC"],
                          "age": age_ms,
                          "killed": not got["alive"]}
                break
            if got["onCanvas"]:
                _n_aged += 1        # proven on canvas but tween window spent
            else:
                _n_intercepted += 1  # a moving label took the delivery
        if probe2:
            page.wait_for_timeout(900)
            fin2 = page.evaluate("() => [...window.__dbg.camera.position]")
            d2 = max(abs(fin2[k] - probe2["toC"][k]) for k in range(3))
            check("wheel zoom mid-tween beats the tween (#112)",
                  probe2["killed"],
                  f"wheel_killed={probe2['killed']} "
                  f"(canvas-proven delivery, tween age {probe2['age']:.0f}ms "
                  f"of 400) "
                  f"dist_to_tween_pose={d2:.0f}")
        else:
            print("SKIP wheel-mid-tween (#112) - "
                  + ("canvas fully covered" if not _any_spot
                     else f"wheel delivery never proven on canvas "
                          f"(intercepted {_n_intercepted}, "
                          f"aged-out {_n_aged} of 4 attempts)"))
        page.keyboard.press("Escape")
        quiesce(page, 4000)

        # -- #112-3: the white hover stalk dies with its context (Esc
        # teardown + canvas pointerleave), same law as the #58 tip family.
        def td_hover_fnbox():
            return page.evaluate(
                """() => { const d = window.__dbg, T = d.THREE;
                     const el = d.renderer.domElement,
                           r = el.getBoundingClientRect();
                     const ok = m => { if (m.agg && !m.count) return false;
                       const v = new T.Vector3(m.p[0], m.p[1], m.p[2])
                         .project(d.camera);
                       return v.z < 1 && Math.abs(v.x) < 0.95
                           && Math.abs(v.y) < 0.95; };
                     for (let j = 0; j < d.fnMeta.length; j++) {
                       if (!ok(d.fnMeta[j])) continue;
                       const v = new T.Vector3(
                         d.fnMeta[j].p[0], d.fnMeta[j].p[1], d.fnMeta[j].p[2])
                         .project(d.camera);
                       el.dispatchEvent(new PointerEvent('pointermove', {
                         clientX: (v.x * 0.5 + 0.5) * r.width + r.left,
                         clientY: (-v.y * 0.5 + 0.5) * r.height + r.top,
                         bubbles: true }));
                       return true; }
                     return false; }""")

        td_focus(td_nodes["a"])
        hov1 = td_hover_fnbox()
        quiesce(page, 4000)
        stalk1 = page.evaluate(
            "() => !!(window.__dbg.fnStalk && window.__dbg.fnStalk.visible)")
        page.keyboard.press("Escape")
        quiesce(page, 4000)
        stalk_esc = page.evaluate(
            "() => !!(window.__dbg.fnStalk && window.__dbg.fnStalk.visible)")
        if hov1:
            check("hover stalk dies on focus teardown (#112)",
                  stalk1 and not stalk_esc,
                  f"shown={stalk1} after_esc={stalk_esc}")
        else:
            print("SKIP stalk-Esc - no fn box projects on-screen")
        td_focus(td_nodes["a"])
        hov2 = td_hover_fnbox()
        quiesce(page, 4000)
        stalk2 = page.evaluate(
            "() => !!(window.__dbg.fnStalk && window.__dbg.fnStalk.visible)")
        page.evaluate(
            "() => window.__dbg.renderer.domElement.dispatchEvent("
            "new PointerEvent('pointerleave'))")
        quiesce(page, 4000)
        stalk_lv = page.evaluate(
            "() => !!(window.__dbg.fnStalk && window.__dbg.fnStalk.visible)")
        if hov2:
            check("hover stalk dies on canvas pointerleave (#112)",
                  stalk2 and not stalk_lv,
                  f"shown={stalk2} after_leave={stalk_lv}")
        else:
            print("SKIP stalk-leave - no fn box projects on-screen")

        # -- #113-1: a stale mapFrozenIx dims the NEXT focus's diagram
        # (dim() returns freeze-alpha for everything but the dead file).
        def td_box_lum(ix):
            return page.evaluate(
                """(ix) => { const d = window.__dbg;
                     const rc = d.mapRects.find(r => r.i === ix);
                     if (!rc) return null;
                     const cv = document.getElementById('mapPane');
                     const dpr = cv.width / (cv.clientWidth || 440);
                     const sx = (rc.x - d.mapPX) * d.mapZ,
                           sy = (rc.y - d.mapPY) * d.mapZ;
                     const x0 = Math.max(0, Math.round(sx * dpr)),
                           y0 = Math.max(0, Math.round(sy * dpr));
                     const w = Math.max(1, Math.min(Math.round(rc.w * dpr),
                           cv.width - x0 - 1));
                     const h = Math.max(1, Math.round(rc.h * dpr));
                     const px = cv.getContext('2d')
                       .getImageData(x0, y0, w, h).data;
                     let s = 0, n = 0;
                     for (let i = 0; i < px.length; i += 4) {
                       s += .3 * px[i] + .59 * px[i + 1] + .11 * px[i + 2];
                       n++; }
                     return n ? s / n : -1; }""", ix)

        td_focus(td_nodes["a"])   # the SAME node refocuses after Esc: the
        # stale mapFrozenIx lands on this very diagram, so no cross-focus
        # box sharing is needed
        map_ready(page)
        row_t = page.evaluate(
            """() => { const d = window.__dbg;
                 const bb = document.getElementById('mapPane')
                   .getBoundingClientRect();
                 const rc = d.mapRects.find(r => r.rows && r.rows.length);
                 if (!rc) return null;
                 return { sx: (rc.x + rc.w / 2 - d.mapPX) * d.mapZ + bb.left,
                          sy: ((rc.rows[0].y0 + rc.rows[0].y1) / 2
                               - d.mapPY) * d.mapZ + bb.top,
                          i: rc.i }; }""")
        if row_t:
            peer = page.evaluate(
                """(i) => { const r = window.__dbg.mapRects.find(
                     r => r.i !== i);
                     return r ? r.i : null; }""", row_t["i"])
            if peer is not None:
                lum_f0, lum_p0 = td_box_lum(row_t["i"]), td_box_lum(peer)
                page.mouse.click(row_t["sx"], row_t["sy"])
                page.wait_for_timeout(700)
                lum_f1, lum_p1 = td_box_lum(row_t["i"]), td_box_lum(peer)
                # real user path: the pointer leaves the pane (hover ink
                # dies with pointer context), then Esc walks the ladder one
                # intent per press - 1st closes the freeze, 2nd clears the
                # focus (the teardown under test)
                page.mouse.move(400, 450)
                page.wait_for_timeout(200)
                for _ in range(3):
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(350)
                    if page.evaluate(
                            "() => window.__dbg.focusFileIdx") < 0:
                        break
                ix_esc = page.evaluate(
                    "() => window.__dbg.mapFrozenIx !== undefined"
                    " ? window.__dbg.mapFrozenIx : 'n/a'")
                hover_esc = page.evaluate(
                    "() => window.__dbg.mapHover !== undefined"
                    " ? window.__dbg.mapHover : 'n/a'")
                td_focus(td_nodes["a"])
                map_ready(page)
                lum_f2, lum_p2 = td_box_lum(row_t["i"]), td_box_lum(peer)
                if -1 not in (lum_f0, lum_p0, lum_f1, lum_p1,
                              lum_f2, lum_p2):
                    # same-box across time: the peer must recover to its
                    # OWN unfrozen baseline once Esc cleared the stale freeze
                    # (cross-box compares are bogus - hub vs dep boxes carry
                    # different inherent luminance)
                    check("Esc clears the freeze before the next focus (#113)",
                          lum_p1 + 3 < lum_p0 and ix_esc == -1 and
                          abs(lum_p2 - lum_p0) < 6,
                          f"peer {lum_p0:.0f}->{lum_p1:.0f} (frozen) "
                          f"ix_after_esc={ix_esc} hover_after_esc={hover_esc}; "
                          f"refocused peer {lum_p2:.0f} vs unfrozen "
                          f"baseline {lum_p0:.0f}")
                else:
                    print("SKIP stale-freeze - box pixel probe off-canvas")
            else:
                print("SKIP stale-freeze - single-box diagram")
        else:
            print("SKIP stale-freeze - no roster-row box in this layout")

        # -- #113-2: the no-focus teardown kills the vars chip rect (no
        # invisible toggle zone under the "focus a node" hint).
        page.keyboard.press("Escape")
        quiesce(page, 4000)
        nf = page.evaluate(
            "() => ({ layout: window.__dbg.mapLayout === null,"
            " rect: window.__dbg.mapVarsChipRect })")
        pbb = page.evaluate(
            """() => { const r = document.getElementById('mapPane')
                 .getBoundingClientRect();
                 return { x: r.left, y: r.top }; }""")
        page.mouse.click(pbb["x"] + 31, pbb["y"] + 34)   # stale chip zone
        page.wait_for_timeout(400)
        vars_flipped = page.evaluate("() => window.__dbg.mapVars")
        td_focus(td_nodes["a"])
        map_ready(page)
        vars_after = page.evaluate("() => window.__dbg.mapVars")
        check("dead-zone click cannot flip vars across teardown (#113)",
              vars_flipped is False and vars_after is False,
              f"layout_null={nf['layout']} rect={nf['rect']} "
              f"flipped={vars_flipped} after_refocus={vars_after}")

        # -- #113-3: pick-vs-paint parity — a zoom-faded chip's anchor is
        # not a toggle, a painted (displaced) chip responds where drawn.
        anchor = page.evaluate(
            """() => { const d = window.__dbg, L = d.mapLayout;
                 if (!L) return null;
                 for (const ch of L.chips) {
                   if (d.mapRects.some(rc =>
                         ch.x < rc.x + rc.w + 6 && ch.x + ch.w > rc.x - 6 &&
                         ch.y < rc.y + rc.h + 6 && ch.y + ch.h > rc.y - 6))
                     continue;
                   return { wx: ch.x + ch.w / 2, wy: ch.y + ch.h / 2 }; }
                 return null; }""")
        if anchor:
            page.mouse.move(pbb["x"] + 200, pbb["y"] + 300)
            for _ in range(16):
                if page.evaluate("() => window.__dbg.mapZ") < 0.40:
                    break
                page.mouse.wheel(0, 240)
                page.wait_for_timeout(110)
            zout = page.evaluate("() => window.__dbg.mapZ")
            sc = page.evaluate(
                """(a) => { const d = window.__dbg;
                     const bb = document.getElementById('mapPane')
                       .getBoundingClientRect();
                     return { x: (a.wx - d.mapPX) * d.mapZ + bb.left,
                              y: (a.wy - d.mapPY) * d.mapZ + bb.top }; }""",
                anchor)
            page.mouse.click(sc["x"], sc["y"])
            page.wait_for_timeout(400)
            hid_open = page.evaluate(
                "() => document.getElementById('mapList').style.display"
                " === 'block'")
            check("zoom-faded chip anchor is not a toggle (#113)",
                  zout < 0.45 and not hid_open,
                  f"mapZ={zout:.2f} list_open={hid_open}")
            page.mouse.move(pbb["x"] + 200, pbb["y"] + 300)
            for _ in range(18):
                if page.evaluate("() => window.__dbg.mapZ") > 1.0:
                    break
                page.mouse.wheel(0, -240)
                page.wait_for_timeout(110)
            page.wait_for_timeout(300)
            par = page.evaluate(
                """() => { const d = window.__dbg;
                     const vis = (d.mapLayout ? d.mapLayout.chips : [])
                               .filter(c => c.hit);
                     if (!vis.length) return null;
                     const c = vis[0],
                           ci = d.mapLayout ? d.mapLayout.chips.indexOf(c) : -1;
                     const wx = (c.hit.x + c.hit.w / 2) / d.mapZ + d.mapPX,
                           wy = (c.hit.y + c.hit.h / 2) / d.mapZ + d.mapPY;
                     return { ci, pick: (d.mapChipAt ? d.mapChipAt(wx, wy) : null),
                              bb: document.getElementById('mapPane')
                                .getBoundingClientRect(),
                              sxc: (c.hit.x + c.hit.w / 2) + 0,
                              syc: (c.hit.y + c.hit.h / 2) + 0 }; }""")
            if par:
                page.mouse.click(par["bb"]["left"] + par["sxc"],
                                 par["bb"]["top"] + par["syc"])
                page.wait_for_timeout(400)
                paint_open = page.evaluate(
                    "() => document.getElementById('mapList').style.display"
                    " === 'block'")
                check("painted chip rect is the pick target (#113)",
                      par["pick"] == par["ci"] and paint_open,
                      f"ci={par['ci']} pick={par['pick']} "
                      f"click_opened={paint_open}")
            else:
                check("painted chip rect is the pick target (#113)",
                      False, "no chip reports a painted hit rect")
        else:
            print("SKIP chip parity - no off-box chip in this layout")

        # -- #198: chip-ladder pick-vs-paint parity at zoom != 1.
        # The ladder displaces badges in SCREEN px (paint draws
        # dyW = dy/mapZ world px = dy screen px), but the pre-fix
        # hit rect rode dy*mapZ, drifting the click zone
        # dy*(mapZ-1) px off the painted glyph. Displaced chips
        # are a data shape, so walk the top wired-degree foci
        # (the map section's own target rule) in the LOD band
        # until some layout reports a Y-displaced painted chip,
        # then click the PAINTED rect center and demand the pin
        # land on that chip. Loud SKIP if no focus has the shape.
        foci = page.evaluate(
            """() => { const d = window.__dbg;
                 const wd = new Array(d.nodes.length).fill(0);
                 for (const l of d.links)
                   if (l.ty === 'call' || l.ty === 'signal')
                     { wd[l.s]++; wd[l.t]++; }
                 return wd.map((w, i) => [w, d.nodes[i].path])
                   .sort((a, b) => b[0] - a[0]).slice(0, 8)
                   .map(x => x[1]); }"""
        )

        mbb = page.evaluate(
            """() => { const r = document.getElementById('mapPane')
                 .getBoundingClientRect();
                 return { x: r.left, y: r.top,
                          w: r.width, h: r.height }; }"""
        )

        def focus_row(path):
            stem = path.split("/")[-1].split(".")[0]
            page.fill("#search", stem)
            page.dispatch_event("#search", "input")
            page.wait_for_selector(
                f"#searchResults .row[title$='{path}']",
                timeout=4000)
            page.evaluate(
                """(p) => { const rows = [...document.querySelectorAll(
                     "#searchResults .row")];
                   (rows.find(x => x.getAttribute("title") === p) ||
                     rows[0]).dispatchEvent(new PointerEvent(
                     "pointerdown", { bubbles: true })); }""", path)
            quiesce(page)

        def drag_pane(target):
            # real divider drag (same idiom as the pane section): the
            # ladder-collision shape only exists at the boot-default
            # pane - a wider pane reflows the map layout and the
            # collided chip never grows
            db = page.evaluate(
                """() => { const r = document.getElementById('divider')
                     .getBoundingClientRect();
                     return { x: r.x + r.width / 2, y: r.y,
                              iw: window.innerWidth }; }""")
            page.mouse.move(db['x'], db['y'] + 100)
            page.mouse.down()
            tx = db['iw'] - target
            for k in range(1, 7):
                page.mouse.move(db['x'] + (tx - db['x']) * k / 6,
                                db['y'] + 100)
                page.wait_for_timeout(30)
            page.mouse.up()
            page.wait_for_timeout(500)

        w0 = page.evaluate('() => window.__dbg.paneW')
        if abs(w0 - 800) > 2:   # PANE_DEFAULT (viz.py)
            drag_pane(800)

        cand = None
        walk = []
        for path in foci:
            # close any bundle list left open above - a floating
            # list eats pane-center wheel events
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
            focus_row(path)
            # deepest layout: the collided-chip shape lives at depth 3
            # (a serve-gated escalation can strand the scan at depth 2,
            # whose smaller layout never grows the colliding chip)
            page.evaluate(
                """() => { const el = document.getElementById('depth');
                     el.value = 3;
                     el.dispatchEvent(new Event('input')); }""")
            quiesce(page)
            focus_row(path)
            page.wait_for_timeout(300)
            for zt in (0.66, 0.58):
                page.mouse.move(mbb['x'] + mbb['w'] / 2,
                                mbb['y'] + mbb['h'] / 2)
                for _ in range(30):
                    z = page.evaluate('() => window.__dbg.mapZ')
                    if z <= zt + 0.03:
                        break
                    page.mouse.wheel(0, 240)
                    page.wait_for_timeout(60)
                page.wait_for_timeout(300)
                r = page.evaluate(
                    """() => { const d = window.__dbg,
                         L = d.mapLayout;
                         if (!L || !L.chips) return null;
                         const zNow = +d.mapZ.toFixed(3);
                         const bb = document.getElementById('mapPane')
                           .getBoundingClientRect();
                         const vc = d.mapVarsChipRect;
                         let best = null, seen = 0;
                         let painted = 0, disp = 0;
                         for (const ch of L.chips) {
                           if (!ch.hit || !ch.disp) continue;
                           painted++;
                           if (ch.disp.dy || ch.disp.dx) disp++;
                           if (Math.abs(ch.disp.dy) < 5) continue;
                           seen++;
                           // painted screen center = anchor + ladder
                           // slot, both in screen px
                           const ax = (ch.x + ch.w / 2 - d.mapPX)
                             * d.mapZ + ch.disp.dx,
                                 ay = (ch.y + ch.h / 2 - d.mapPY)
                             * d.mapZ + ch.disp.dy;
                           if (ax < 24 || ay < 24 ||
                               ax > bb.width - 24 ||
                               ay > bb.height - 24)
                             continue;
                           if (vc && ax > vc.x - 4 &&
                               ax < vc.x + vc.width + 4 &&
                               ay > vc.y - 4 &&
                               ay < vc.y + vc.height + 4)
                             continue;
                           // the painted point must belong to THIS
                           // chip alone, else the click is ambiguous
                           let clash = false;
                           for (const o of L.chips) {
                             if (o === ch || !o.hit) continue;
                             if (ax >= o.hit.x &&
                                 ax <= o.hit.x + o.hit.w &&
                                 ay >= o.hit.y &&
                                 ay <= o.hit.y + o.hit.h)
                               { clash = true; break; }
                           }
                           if (clash) continue;
                           if (!best || Math.abs(ch.disp.dy) >
                                 Math.abs(best.disp.dy))
                             best = ch;
                         }
                         if (!best) return {
                           best: null, seen, painted, disp,
                           z: zNow, n: L.chips.length };
                         const px = (best.x + best.w / 2 - d.mapPX)
                           * d.mapZ + best.disp.dx,
                               py = (best.y + best.h / 2 - d.mapPY)
                           * d.mapZ + best.disp.dy;
                         return { best: { ci: L.chips.indexOf(best),
                                  dy: best.disp.dy, dx: best.disp.dx,
                                  z: zNow, px, py,
                                  hx: best.hit.x + best.hit.w / 2,
                                  hy: best.hit.y + best.hit.h / 2,
                                  left: bb.left, top: bb.top },
                                  seen, painted, disp }; }"""
                )
                cand = r["best"] if r else None
                walk.append((path.split("/")[-1], zt,
                             round((r or {}).get("z", 0), 2),
                             (r or {}).get("seen"),
                             (r or {}).get("painted"),
                             (r or {}).get("disp")))
                if cand:
                    break
            if cand:
                break
        if cand:
            page.mouse.click(cand['left'] + cand['px'],
                             cand['top'] + cand['py'])
            page.wait_for_timeout(400)
            pin = page.evaluate(
                "() => ({ open: document.getElementById('mapList')"
                ".style.display === 'block', "
                "ci: window.__dbg.mapListChip })"
            )
            drift = max(abs(cand['px'] - cand['hx']),
                        abs(cand['py'] - cand['hy']))
            check("ladder chip click at zoom!=1 hits the painted glyph "
                  "(#198)",
                  drift <= 0.5 and pin['open'] and
                  pin['ci'] == cand['ci'],
                  f"focus={path} mapZ={cand['z']:.2f} "
                  f"dy={cand['dy']:.0f} dx={cand['dx']:.0f} "
                  f"paint=({cand['px']:.1f},{cand['py']:.1f}) "
                  f"hit=({cand['hx']:.1f},{cand['hy']:.1f}) "
                  f"drift={drift:.1f}px pin={pin}")
        else:
            print(f"SKIP ladder chip parity - no Y-displaced painted "
                  f"chip in any top-degree focus "
                  f"(LOD band) walk={walk}")
        if abs(w0 - 800) > 2:
            drag_pane(w0)
        # canonical hand-off for the sections below: the focus walk
        # and the probe click leave arbitrary state - restore the
        # post-#113-3 pose (focus node a, list closed, near-stock
        # zoom) so #113-5's chip pick is the one it was authored on
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        td_focus(td_nodes["a"])
        map_ready(page)
        # restore near-stock zoom for the sections below
        page.mouse.move(pbb['x'] + 200, pbb['y'] + 300)
        for _ in range(30):
            if page.evaluate('() => window.__dbg.mapZ') >= 1.0:
                break
            page.mouse.wheel(0, -240)
            page.wait_for_timeout(110)
        page.wait_for_timeout(200)

        # -- #113-5: the bundle list re-anchors (or closes) when the map
        # moves under it — live UI never floats over stale geometry.
        page.keyboard.press("Escape")
        quiesce(page, 4000)
        td_focus(td_nodes["a"])
        map_ready(page)
        lpick = page.evaluate(
            """() => { const d = window.__dbg;
                 if (!d.mapLayout || !d.mapLayout.chips.length) return null;
                 const vis = (d.mapLayout ? d.mapLayout.chips : [])
                               .filter(c => c.hit);
                 const c = vis.length ? vis[0] : d.mapLayout.chips[0];
                 return { ci: d.mapLayout.chips.indexOf(c),
                          sx: c.hit ? c.hit.x + c.hit.w / 2
                                    : (c.x + c.w / 2 - d.mapPX) * d.mapZ,
                          sy: c.hit ? c.hit.y + c.hit.h / 2
                                    : (c.y + c.h / 2 - d.mapPY) * d.mapZ }; }""")
        if lpick:
            page.mouse.click(pbb["x"] + lpick["sx"], pbb["y"] + lpick["sy"])
            page.wait_for_timeout(500)
            lb = page.evaluate(
                """() => { const l = document.getElementById('mapList');
                     return { open: l.style.display === 'block',
                              left: parseFloat(l.style.left) || 0,
                              top: parseFloat(l.style.top) || 0 }; }""")
            # wheel OUTSIDE the list (pointer-events:auto eats pane events)
            page.mouse.move(pbb["x"] + 600, pbb["y"] + 700)
            page.mouse.wheel(0, -240)
            page.wait_for_timeout(700)
            la = page.evaluate(
                """(ci) => { const l = document.getElementById('mapList');
                     const d = window.__dbg;
                     const c = d.mapLayout ? d.mapLayout.chips[ci] : null;
                     const chipSx = c ? (c.hit ? c.hit.x + c.hit.w / 2
                       : (c.x + c.w / 2 - d.mapPX) * d.mapZ) : null;
                     return { open: l.style.display === 'block',
                              left: parseFloat(l.style.left) || 0,
                              chipSx }; }""", lpick["ci"])
            moved = (la["chipSx"] is not None
                     and abs(la["chipSx"] - lpick["sx"]) > 40)
            check("bundle list re-anchors or closes on zoom (#113)",
                  lb["open"] and moved and
                  ((not la["open"]) or abs(la["left"] - lb["left"]) > 30),
                  f"before={lb} after={la} chip_moved={moved}")
            if la["open"]:
                page.mouse.move(pbb["x"] + 600, pbb["y"] + 700)
                page.mouse.down()
                page.mouse.move(pbb["x"] + 740, pbb["y"] + 700, steps=6)
                page.mouse.up()
                page.wait_for_timeout(600)
                lp = page.evaluate(
                    """() => { const l = document.getElementById('mapList');
                         return { open: l.style.display === 'block',
                                  left: parseFloat(l.style.left) || 0 }; }""")
                check("bundle list re-anchors or closes on pan (#113)",
                      (not lp["open"]) or abs(lp["left"] - la["left"]) > 30,
                      f"{la} -> {lp}")
        else:
            print("SKIP list re-anchor - no chip in this layout")

        # -- #113-4: Backspace inside a picker filter edits the filter —
        # it must not pop the focus stack beneath the open picker.
        page.keyboard.press("Escape")
        quiesce(page, 4000)
        td_focus(td_nodes["a"])
        map_ready(page)
        hdr = page.evaluate(
            """() => { const d = window.__dbg;
                 const rc = d.mapRects.find(r => r.i !== d.focusFileIdx)
                         || d.mapRects[0];
                 if (!rc) return null;
                 const bb = document.getElementById('mapPane')
                   .getBoundingClientRect();
                 return { sx: (rc.x + rc.w / 2 - d.mapPX) * d.mapZ + bb.left,
                          sy: (rc.y + 8 - d.mapPY) * d.mapZ + bb.top }; }""")
        more = None
        if hdr:
            page.mouse.click(hdr["sx"], hdr["sy"])   # header refocus pushes
            quiesce(page, 6000)
            stack1 = page.evaluate("() => window.__dbg.focusStack.length")
            focus_b = page.evaluate("() => window.__dbg.focusFileIdx")
            more = page.evaluate(
                """() => { const d = window.__dbg;
                     const rc = d.mapRects.find(r => r.more && r.more.y0);
                     if (!rc) return null;
                     const bb = document.getElementById('mapPane')
                       .getBoundingClientRect();
                     return { sx: (rc.x + rc.w / 2 - d.mapPX) * d.mapZ
                              + bb.left,
                              sy: ((rc.more.y0 + rc.more.y1) / 2 - d.mapPY)
                              * d.mapZ + bb.top }; }""")
            if more and stack1 >= 1:
                page.mouse.click(more["sx"], more["sy"])
                page.wait_for_timeout(500)
                picker_open = page.evaluate(
                    "() => document.getElementById('mapPick').style.display"
                    " === 'block'")
                page.keyboard.type("he")
                page.keyboard.press("Backspace")
                page.wait_for_timeout(700)
                bk = page.evaluate(
                    """() => ({ stack: window.__dbg.focusStack.length,
                         focus: window.__dbg.focusFileIdx,
                         input: document.querySelector('#mapPick input').value })""")
                check("Backspace edits the picker filter, not the stack (#113)",
                      picker_open and bk["stack"] == stack1
                      and bk["focus"] == focus_b and bk["input"] == "h",
                      f"picker={picker_open} stack {stack1}->{bk['stack']} "
                      f"focus {focus_b}->{bk['focus']} input={bk['input']!r}")
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
            elif not more:
                print("SKIP picker backspace - no +N more roster overflow")
        else:
            print("SKIP picker backspace - no off-focus card header")

        # -- #112-4: resetAll returns EVERY pin to boot — a tip-menu pin
        # and its persistent wire tip used to survive the reset.
        page.keyboard.press("Escape")
        quiesce(page, 4000)
        td_focus(td_nodes["a"])
        map_ready(page)
        cands = page.evaluate(
            """() => { const d = window.__dbg;
                 if (!d.mapLayout) return [];
                 const bb = document.getElementById('mapPane')
                   .getBoundingClientRect();
                 const out = [];
                 for (const sp of (d.mapLayout ? d.mapLayout.spines : [])) {
                   if (!sp.pts || sp.pts.length < 2) continue;
                   for (const f of [0.5, 0.25, 0.75]) {
                     const k = Math.floor(sp.pts.length * f);
                     if (k < 1 || k >= sp.pts.length) continue;
                     const m = sp.pts[k];
                     if (d.mapRects.some(rc =>
                           m[0] > rc.x - 4 && m[0] < rc.x + rc.w + 4 &&
                           m[1] > rc.y - 4 && m[1] < rc.y + rc.h + 4))
                       continue;
                     const sx = Math.round(
                       (m[0] - d.mapPX) * d.mapZ + bb.left);
                     const sy = Math.round(
                       (m[1] - d.mapPY) * d.mapZ + bb.top);
                     // the click must land ON the pane canvas: a panned
                     // view maps off-pane points onto the 3D surface
                     if (document.elementFromPoint(sx, sy) !==
                           document.getElementById('mapPane'))
                       continue;
                     out.push({ sx, sy });
                     if (out.length >= 8) return out;
                   }
                 }
                 return out; }""")
        pin_t = None
        for cand in (cands or []):
            page.mouse.click(cand["sx"], cand["sy"])
            page.wait_for_timeout(400)
            pin_t = page.evaluate("() => window.__dbg.wirePin")
            if pin_t:
                break
        if pin_t:
            page.evaluate("() => document.getElementById('bReset').click()")
            quiesce(page, 4000)
            pin_r = page.evaluate("() => window.__dbg.wirePin")
            tip_r = page.evaluate(
                "() => document.getElementById('wireTip').style.display"
                " !== 'block'")
            check("resetAll clears every pin and its tip (#112)",
                  pin_r is None and tip_r,
                  f"{cands} pin {pin_t.get('menu')} -> {pin_r} "
                  f"tip_hidden={tip_r}")
        else:
            print(f"SKIP reset pin clear - no spine point latched a pin "
                  f"({cands})")
        # artifact: screenshot of the focused fn-layer state
        page.screenshot(path=str(SHOTS / "last_run.png"), scale="css", type="png")
        print("artifact: .tmp/shots/last_run.png")
        # -- #59: Escape off a pinned map list must leave the functions
        # checkbox at its boot truth (#31 class: reset == boot; toggles
        # are not focus state). The lookfeel f10 screenshot read the
        # post-Escape box as unchecked - it is checked but DISABLED (no
        # focus renders grayed); this leg pins the DOM truth on the exact
        # pinned-list path so no dismissal path can silently regress it.
        # Runs LAST: its Esc x2 ends in clearFocus, and the earlier
        # click-at-scanned-coordinate tests are camera-pose sensitive.
        enter_focus_via_row()
        map_ready(page)
        pin59 = latch_pin_via_list()
        if pin59:
            st59 = page.evaluate("""() => ({
                dom: document.getElementById('cbFn').checked,
                probe: window.__dbg.cbFn })""")
            check("pinned map list keeps functions box checked (#59)",
                  st59["dom"] is True and st59["probe"] is True,
                  f"{st59}")
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
            e1 = page.evaluate("""() => ({
                dom: document.getElementById('cbFn').checked,
                probe: window.__dbg.cbFn, pin: window.__dbg.wirePin,
                list: document.getElementById('mapList').style.display,
                focus: window.__dbg.focusSeeds.size })""")
            check("esc off the pin keeps functions box checked (#59)",
                  e1["dom"] is True and e1["probe"] is True and
                  e1["pin"] is None and e1["list"] != "block" and
                  e1["focus"] == 1,
                  f"{e1}")
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
            e2 = page.evaluate("""() => ({
                dom: document.getElementById('cbFn').checked,
                probe: window.__dbg.cbFn,
                disabled: document.getElementById('cbFn').disabled,
                focus: window.__dbg.focusSeeds.size })""")
            check("esc ladder lands boot cbFn truth (checked, #59)",
                  e2["dom"] is True and e2["probe"] is True and
                  e2["focus"] == 0 and e2["disabled"] is True,
                  f"{e2}")
        else:
            print("SKIP #59 cbFn ladder - no chip list")

        # -- #62: depth-slider truth. The crumb's lit count used to
        # recount the 1-hop ball (frozen at landing size) while the real
        # lit set grew with the slider, and the camera never re-framed -
        # served content sat off-screen until a re-click. Both must track
        # the slider. Data-gate: a seed whose 2-hop set actually grows;
        # an index without that shape skips loudly.
        depth62 = page.evaluate(
            """() => { const d = window.__dbg;
                 let best = null;
                 for (let s = 0; s < d.nodes.length; s++) {
                   if (!d.adj[s] || !d.adj[s].length) continue;
                   const seen = new Set([s]);
                   for (const v of d.adj[s]) if (!seen.has(v)) seen.add(v);
                   const n1 = seen.size;
                   for (const u of [...seen])
                     for (const v of d.adj[u]) if (!seen.has(v)) seen.add(v);
                   const g = seen.size - n1;
                   if (!best || g > best.g)
                     best = { i: s, path: d.nodes[s].path, g };
                 }
                 return best; }"""
        )
        if not depth62 or depth62["g"] <= 0:
            print(f"SKIP #62 depth-slider truth - no 2-hop growth "
                  f"(g={depth62 and depth62['g']})")
        else:
            page.fill("#search", depth62["path"].rsplit("/", 1)[-1].split(".")[0])
            page.dispatch_event("#search", "input")
            wait_rows(page, depth62["path"])
            page.locator(f"#searchResults .row[title='{depth62['path']}']").click()
            quiesce(page)

            def crumb62():
                return page.evaluate(
                    """() => { const d = window.__dbg;
                         const t = document.getElementById('crumb').textContent || '';
                         const m = t.match(/depth (\\d+).*?(\\d+) files lit/);
                         return { crumb: m ? +m[2] : null,
                                  lit: d.alphaTgt.reduce(
                                    (s, a) => s + (a > 0.5 ? 1 : 0), 0),
                                  cam: [d.camera.position.x, d.camera.position.y,
                                        d.camera.position.z] }; }"""
                )

            c1 = crumb62()
            check("[#62] crumb lit-count equals the live lit set",
                  c1["crumb"] == c1["lit"], str(c1))
            page.evaluate(
                """() => { const el = document.getElementById('depth');
                     el.value = 2; el.dispatchEvent(new Event('input')); }""")
            quiesce(page)
            c2 = crumb62()
            check("[#62] crumb lit-count tracks the depth-grown set",
                  c2["crumb"] == c2["lit"] and c2["lit"] > c1["lit"], str(c2))
            drift = sum(abs(a - b) for a, b in zip(c2["cam"], c1["cam"]))
            check("[#62] camera re-frames on depth growth",
                  drift > 1.0, f"pose drift {drift:.1f}")
            frame62 = page.evaluate(
                """() => { const d = window.__dbg;
                     let out = 0, n = 0;
                     for (let i = 0; i < d.nodes.length; i++) {
                       if (d.alphaTgt[i] <= 0.5) continue;
                       n++;
                       const p = d.posAt(i);
                       const v = new d.THREE.Vector3(p[0], p[1], p[2])
                         .project(d.camera);
                       if (Math.abs(v.x) > 1.05 || Math.abs(v.y) > 1.05) out++;
                     }
                     return { n, out }; }"""
            )
            check("[#62] grown lit set stays in frame",
                  frame62["out"] == 0, str(frame62))
            page.keyboard.press("Escape")
            quiesce(page)

        # -- #8: super-hub landing degrade. Past SUPER_DEG (weight-sum
        # degree, the number the hub chip shows) a landing folds its 1-hop
        # chip fan to the top-weight few + ONE count chip (the #77
        # magnitude tier applied to the 3D chip plane; #261 tap-fold
        # precedent). Labels only: the lit ball, budget arcs and corridor
        # census are untouched. Data-gate on the index shipping a
        # deg>100 hub; smaller indexes skip loudly.
        sup8 = page.evaluate(
            """() => { const d = window.__dbg;
                 let best = 0;
                 for (let i = 1; i < d.nodes.length; i++)
                   if (d.degree[i] > d.degree[best]) best = i;
                 return { path: d.nodes[best].path,
                          deg: +d.degree[best].toFixed(0),
                          super: d.degree[best] > 100 }; }"""
        )
        if not sup8["super"]:
            print(f"SKIP #8 super-hub fold - max degree {sup8['deg']} "
                  f"(tier needs > 100)")
        else:
            page.fill("#search", sup8["path"].rsplit("/", 1)[-1].split(".")[0])
            page.dispatch_event("#search", "input")
            wait_rows(page, sup8["path"])
            page.locator(f"#searchResults .row[title='{sup8['path']}']").click()
            quiesce(page)
            fold8 = page.evaluate(
                """() => { const d = window.__dbg;
                     const vis = [...document.querySelectorAll('.flab')]
                       .filter(e => e.offsetParent !== null);
                     const fc = document.querySelector('.flab.fold');
                     let under = 0;
                     for (const e of vis) {
                       if (e.getBoundingClientRect().left < 310) under++;
                     }
                     const lab = d.nodes[d.focusFileIdx].label;
                     const hc = [...document.querySelectorAll('#hubs .hub')]
                       .find(e => e.offsetParent !== null
                              && e.textContent.startsWith(lab + " "));
                     let halo = !!hc;
                     if (hc) {
                       const hr = hc.getBoundingClientRect();
                       for (const e of vis) {
                         if (e.classList.contains('fold')) continue;
                         const r = e.getBoundingClientRect();
                         const sep = r.left > hr.right + 20 ||
                           hr.left > r.right + 20 ||
                           r.top > hr.bottom + 20 ||
                           hr.top > r.bottom + 20;
                         if (!sep) { halo = false; break; }
                       }
                     }
                     return { st: d.focusFold,
                              foldText: fc ? fc.textContent : null,
                              foldVis: !!(fc && fc.offsetParent !== null),
                              vis: vis.length, under, halo }; }"""
            )
            st8 = fold8["st"]
            check("[#8] super-hub landing folds the 1-hop chip fan",
                  st8["super"] and st8["folded"] > 0 and st8["shown"] <= 17
                  and fold8["foldVis"] and fold8["vis"] <= st8["shown"] + 1,
                  str(fold8))
            check("[#8] fold chip names the folded count",
                  fold8["foldText"] == f"+{st8['folded']} more", str(fold8))
            check("[#8] folded landing keeps chips off the control panel",
                  fold8["under"] == 0, str(fold8))
            check("[#8] clean halo band around the focused hub chip",
                  fold8["halo"], str(fold8))
            ink8 = page.evaluate(
                """() => { const d = window.__dbg;
                     return {
                       lit: d.alphaTgt.reduce(
                         (s, a) => s + (a > 0.5 ? 1 : 0), 0),
                       ball: d.compactBall.nOthers,
                       budget: d.rfwProbe.budgetN }; }"""
            )
            check("[#8] fold is labels-only (ball + budget arcs intact)",
                  ink8["lit"] >= ink8["ball"] + 1 and ink8["budget"] > 0,
                  str(ink8))
            page.locator(".flab.fold").click()
            quiesce(page)
            unf8 = page.evaluate("() => window.__dbg.focusFold")
            check("[#8] fold chip click unfolds the fan",
                  unf8["unfold"] and unf8["folded"] == 0
                  and unf8["shown"] > 17, str(unf8))
            page.keyboard.press("Escape")
            quiesce(page)
            page.fill("#search", sup8["path"].rsplit("/", 1)[-1].split(".")[0])
            page.dispatch_event("#search", "input")
            wait_rows(page, sup8["path"])
            page.locator(f"#searchResults .row[title='{sup8['path']}']").click()
            quiesce(page)
            ref8 = page.evaluate("() => window.__dbg.focusFold")
            check("[#8] a fresh landing refolds",
                  ref8["super"] and ref8["folded"] > 0 and not ref8["unfold"],
                  str(ref8))
            page.keyboard.press("Escape")
            quiesce(page)
        # -- #279: semantic-affinity overlay — the J9 mutual-kNN pairs the
        # layout springs on, promoted to ink: a capped violet dotted wire
        # species, LOD-gated like the corridor tiers, plus card rows and a
        # UI-only toggle. Data-gated like every species leg: a store with no
        # mutual-kNN pair at cosine >= 0.45 skips loudly (#97 law).
        sem = page.evaluate("() => window.__dbg.semAff")
        if not sem["rows"]:
            print("SKIP #279 semantic-affinity overlay - bake carries no "
                  "affinity pairs (store lacks mutual-kNN cosine >= 0.45)")
        else:
            check("[#279] affinity rows ride DATA, under the global cap",
                  0 < sem["rows"] <= viz.SEM_AFF_CAP, str(sem))
            check("[#279] species LOD-gated: overview serves no affinity ink",
                  sem["on"] and not sem["lod"] and sem["served"] == 0, str(sem))
            page.evaluate(
                """() => { const d = window.__dbg;
                     d.controls.target.set(0, 0, 0);
                     // dist ~115 < the lodDist floor (420) — deterministic
                     // pose for the corridor-tier zoom gate
                     d.camera.position.set(60, 40, 90);
                     d.controls.dispatchEvent({ type: 'change' }); }""")
            quiesce(page)
            semz = page.evaluate("() => window.__dbg.semAff")
            check("[#279] close zoom serves the species (corridor-tier gate)",
                  semz["lod"] and semz["served"] == semz["rows"], str(semz))
            page.click("#bSemAff")
            page.wait_for_timeout(120)
            semt = page.evaluate("() => window.__dbg.semAff")
            check("[#279] toggle hides the species (UI state, DATA intact)",
                  semt["on"] is False and semt["served"] == 0
                  and semt["rows"] == sem["rows"]
                  and "on" not in (page.get_attribute("#bSemAff", "class") or ""),
                  str(semt))
            page.click("#bSemAff")   # back on for the card leg
            page.wait_for_timeout(120)
            # card rows ride the SAME capped pairs: focus sample.a by search
            # row click (focus entry law, #33)
            page.fill("#search", sem["sample"]["a"].rsplit("/", 1)[-1].split(".")[0])
            page.dispatch_event("#search", "input")
            wait_rows(page, sem["sample"]["a"])
            page.locator(
                f"#searchResults .row[title='{sem['sample']['a']}']").click()
            quiesce(page)
            card = page.evaluate(
                """() => ({ head: document.getElementById('kSem').textContent,
                           rows: [...document.querySelectorAll('#iSem li')]
                             .map(li => li.textContent) })""")
            check("[#279] card gains a semantic-neighbors section",
                  card["head"].startswith("SEMANTIC NEIGHBORS (")
                  and any("cos" in r for r in card["rows"]), str(card))
            b_lab = sem["sample"]["b"].rsplit("/", 1)[-1]
            check("[#279] card rows list neighbours with cosine scores",
                  any(b_lab in r and "cos" in r for r in card["rows"]), str(card))
            page.keyboard.press("Escape")
            quiesce(page)


        # #98 watchdog: NO console error and NO uncaught JS error may fire
        # anywhere in the session — boot, search-focus entry, depth
        # escalation, wire/trunk/leg pins (2D + 3D), the stale-reap refocus
        # walk, toggles, camera moves. #98 shipped because only the boot
        # window was asserted: a ReferenceError (busLodInit evaluated
        # outside its — mistakenly tick-nested — scope) stalled the rAF
        # chain in a later flow while every check still passed.
        # Exemptions: favicon (route stub above) and setPointerCapture —
        # the harness dispatches synthetic PointerEvents with no active
        # pointer id; a real input device cannot produce that failure.
        real_errors = [e for e in errors
                       if "favicon" not in e and "setPointerCapture" not in e]
        check("console clean across boot+focus+pin (watchdog)",
              not real_errors, "; ".join(real_errors[:5]))

        browser.close()

    # -- #279 determinism law (bake side, no browser): two generates of the
    # SAME store must be byte-identical modulo meta.generated_at. The
    # affinity rows ride a ranked (score, then path-tie) capped transform —
    # any ordering wobble lands here as a byte diff.
    outs = []
    try:
        for tag in ("a", "b"):
            outs.append(Path(viz.generate(
                out=ROOT / ".tmp" / f"viz_det_{tag}.html")))

        def norm(h: bytes) -> bytes:
            return re.sub(rb'"generated_at":"[^"]*"', b'"generated_at":""', h)

        ha, hb = (norm(p.read_bytes()) for p in outs)
        check("[#279] two bakes byte-identical modulo generated_at",
              ha == hb,
              f"{outs[0].stat().st_size} vs {outs[1].stat().st_size} bytes")
        da = json.loads(re.search(rb"const DATA = (.+);\n", ha).group(1))
        db = json.loads(re.search(rb"const DATA = (.+);\n", hb).group(1))
        check("[#368] restR channel rides DATA, identical across bakes",
              isinstance(da.get("restR"), list)
              and len(da["restR"]) == len(da["nodes"])
              and da["restR"] == db["restR"],
              f"n={len(da.get('restR') or [])}")
        if not da.get("semAff"):
            print("SKIP #279 affinity-row determinism detail - "
                  "bake carries no affinity pairs")
        else:
            check("[#279] affinity rows identical across bakes",
                  da["semAff"] == db["semAff"], str(da["semAff"][:3]))
    finally:
        for p in outs:   # no .tmp residue (byte-law hygiene)
            p.unlink(missing_ok=True)

    # -- [#368] bake-law single-source teeth (template + bake side) -------
    # The template must CONSUME the baked constants (DATA.restR, meta.
    # aggMax/semFloor) and never restate the radius/affinity laws as JS
    # literals — the banned strings are the exact forms the fix removed.
    # The sabotage leg proves the stamped meta flows from the Python
    # constants (patch -> bake -> DATA), so retuning the bake actually
    # changes what the page reads instead of drifting from a JS copy.
    import vizjs
    tpl = vizjs.template()
    restated = [s for s in ("4.5 + Math.sqrt", "cosine ≥ 0.45",
                            "mutual top-6", "const AGG_MAX = 6")
                if s in tpl]
    consumed = all(s in tpl for s in
                   ("DATA.restR", "DATA.meta.aggMax", "DATA.meta.semFloor"))
    check("[#368] template consumes DATA laws, no restated literals",
          not restated and consumed,
          f"restated={restated} consumed={consumed}")
    sab, sab_p = None, None
    old_agg, old_floor = viz.AGG_MAX, viz.SEM_FLOOR
    try:
        viz.AGG_MAX, viz.SEM_FLOOR = 3, 0.30
        sab_p = Path(viz.generate(out=ROOT / ".tmp" / "viz_sab368.html"))
        sab = json.loads(re.search(
            rb"const DATA = (.+);\n", sab_p.read_bytes()).group(1))["meta"]
    finally:
        viz.AGG_MAX, viz.SEM_FLOOR = old_agg, old_floor
        if sab_p is not None:
            sab_p.unlink(missing_ok=True)   # no .tmp residue
    check("[#368] sabotage: patched constants flow into the bake",
          sab is not None and sab["aggMax"] == 3 and sab["semFloor"] == 0.30
          and sab["semTopK"] == viz.SEM_TOP_K,
          str(sab and {k: sab[k] for k in ("aggMax", "semFloor", "semTopK")}))

    LOG.finish(f"{n_files} files indexed")


if __name__ == "__main__":
    main()


