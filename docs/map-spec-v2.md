# Named-Wire Map — Spec v2 (implementation-ready)

Adversarial cycle: designer spec v1 → 16-finding attack → merged v2 ([F#] = finding). Decisions FINAL: var wires OFF + `vars` chip; read/write split v1.5; attach/inst anonymous + demoted.

## 0. Data contract (viz.py _build_data; graph.py untouched in v2)
NEW exports (fedges/fio untouched — 3D layer, search, showFnInfo unaffected):
- `DATA.mwires: [[ty, sf, sfn, df, dfn, line, extra], …]`
  - call ← fedges-identical filters: `["call", sf, sfn, df, dfn, line, null]`
  - var ← g.edges dsts endswith `::VAR:` (graph.py member-write emission sites; grammar constants `FN_KEY_SEP`/`VAR_PREFIX` in graph.py's fn-key block): `["var", sf, sfn, df, member, line, null]`
  - signal ← tscn fs.connections resolved via script_rels mirroring graph.py `_wire_tscn`: `["signal", tscn_f, SIGNAL_NAME, script_f, handler, 0, null]` [F5: sfn = signal name]
- `DATA.fns: {path: [[fn_name, line], …]}` from g.files[p].funcs (complete roster)
- `DATA.meta: {sig_resolved: n, sig_unresolved: m}` [F13]

## 1. Wire identity
- call wire = (src_fn@A → dst_fn@B); NAME = callee (dst fn).
- var = member name (dst owns). DEFAULT OFF + map-local `vars` chip [F10].
- signal = signal name; entry lands on handler row; tooltip `scene > sig > B::handler` [F5]. Unresolved → anonymous amber FILE corridor [F13].
- attach/inst = anonymous, DEMOTED: 1px stroke, alpha 0.40, T-junction, drawn FIRST beneath named wires.

## 2. Admission — TWO-TIER [F1]
Tier 1 pairs: file skeleton unchanged (w>=2 || i<120 || top8-touch; 160 cap). Tier 2: rank fn wires per pair (callee in-degree desc, dfn, sf, sfn, line) [F14]; TOP-1/pair individual + labeled; rest ride corridor. E := admitted corridors in view.

## 3. Type glyphs
call: #d9e2eb solid / solid triangle. signal: #ffb347 dash 6-4 / hollow triangle / italic label. var: #73e68c dot 2-3 / filled circle. inst/attach: #3dccf2 dash 1-3 / no arrow. One font constant for ALL map text (ui-monospace/Menlo/Consolas) [F14].

## 4. File boxes — rosters
Collapsed = 22px box. Expanded = header (click refocus, dblclick toggle) + fn rows @14px, 10px name, ✎N badge (fio.w). Wire terminates ON its fn row. FN_PORT_MAX=12 with PINNED SET FIRST: fns touched by seed-incident wires / query fn [F4]; rest by rank. "+N more…" → edge-anchored DOM picker (searchable, closes on canvas input + ESC) [F11]. Hysteresis: expand >=1.5, collapse <1.2 [F7]. E<=12 → wire>=1 boxes expand at any zoom [F9].

## 5. Layout [F6/F7]
Expansion resolved BEFORE row wrap. rowH = max(64, tallest-expanded-in-row + 40). freeX radius scales with box widths; exhausted → translucent bezier overlays (alpha 0.35, no lane claims) — honest degradation. Layout cache keyed (focusVersion, expansionSet, paneSize); pan/zoom = pure transform. rAF dirty-flag single draw [F7]. Focus change resets zoom/pan to fit both dims, floor 1.0 [F15,F9].

## 6. Labels [F2]
Entry micro-label at arrowhead: 10px, type-colored, +4px offset. Greedy dy-nudge ladder (0,-9,-18,+9,+18,-27…), first-fit, HIDE-IF-NO-FIT (hub-label pattern viz.py:2383). Per-target budget 2; truncate 9 chars + "…" when narrow. Zoom tiers only when E>12: z<0.7 → top-8, z>=1.5 → all in-viewport; E<=12 → ALL.

## 7. Bundles [F3]
Corridor spine carries typed micro-chips ("×18" call / "×5" sig / "×2" var). Chip click → pinned DOM LIST panel: every wire enumerated "A::sfn → B::dfn :line", scrollable. Pinned until ESC/void-click.

## 8. Disclosure
L0: seed roster open; top-1/pair labeled; bundles chipped. L1 hover wire: dim non-incident 0.15; map-local tooltip `A::sfn() → B::dfn()` + line + fio sig→ret + ✎w/⇄mp chips; signal = `scene > sig > B::handler`. L2 click wire: showFnInfo(target) CALLED BY [F8]; hit test distance-to-segment, 6px SCREEN-space. BUGFIX: showFnInfo 24-cap "+N more" → max-height+scroll. L3 roster-row click: local dim only (0.08), rows frozen, NO re-seed [F12].

## 9. Render order
attach/inst underlays → corridor spines (gradient) → individual typed wires → boxes + rosters → labels/chips/terminators.

## 10. Survives untouched
barycenter, row wrap, BFS levels, claimLane/nextY/crossedBands/chamfers, wheel-zoom/drag-pan, mapToWorld, rect hit-test, click-refocus, MAP_MAX=40, skeleton tier-1.

## 11. v1.5 parking lot
var read/write split; signal extractor 4-tuple; in-code cross-file connect(); intra-file wires.

## 12. Effort
Medium+: python exports mechanical; JS = box pass, two-tier edge pass, label pass, 3 DOM overlays (tooltip/list/picker), layout cache + rAF. Highest risk: F6 expansion-in-layout, F7 cache keys.
