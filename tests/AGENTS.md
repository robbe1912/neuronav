# AGENTS.md — tests/

Forty self-contained suites. Each is a standalone script — no pytest — run in
its own process:

```
.venv/Scripts/python.exe -X utf8 tests/test_<name>.py
```

Exit 0 = all pass. Each suite bootstraps `sys.path` to the repo root and
uses a local `check(name, cond)` helper (PASS/FAIL lines + failure count).
CI (`.github/workflows/ci.yml`) runs thirty hermetic suites on ubuntu
with `NEURONAV_EMBED_FAKE=1` (`test_strata`, `test_crosslang`,
`test_pyhard`, `test_cpphard`, `test_jshard`, `test_autorescan`, `test_server_stdio`,
`test_searchtext`, `test_project_mode`, `test_baseindex`, `test_mwires`,
`test_clusterinv`, `test_archrules`, `test_recall`, `test_embedprov`,
`test_repomap`, `test_selfindex`, `test_explore`, `test_verifier`,
`test_bench`, `test_bakeint`, `test_portability`, `test_bytelaws`,
`test_walkguard`, `test_langsep`, `test_delegates`, `test_qa_smoke`,
`test_bootgate`, `test_impact`, `test_packaging`) plus a `viz` job that builds
frozen synthetic corpus (`tests/vizcorpus_build.py`) and runs `test_viz`
against its hermetic store in a real browser (issue #100), then re-runs
`test_qa_smoke` there so its playwright battery leg executes (the suites
job runs the same suite with that leg skipped via
`NEURONAV_QA_SMOKE_NO_BROWSER=1`). The two e2e
suites need NO committed store in CI (issue #180): on a fresh checkout
(no `.neuronav/`, no checkout-local `config.json`) each self-bootstraps
the self-index via one FAKE-embed rescan when `nav.count() == 0` — the
#166 F1 pattern from `test_recall` — and drives its drift/stat-gate/
routed-freshness/recall-knob/degraded scenarios on hermetic scratch
trees. On an owner checkout (config present) both run their owner legs
unchanged: the stores are never written by the CI bootstrap. The rest
are local gates outside the matrix: `test_target_regression` (a
populated target repo in the default config), plus the hermetic
owner-side `test_chunking` and `test_truthful`.

## Suites

| suite | covers | needs |
|---|---|---|
| `test_strata` | depth layering, cycles, `_layout` determinism | stdlib + numpy |
| `test_crosslang` | self-index integration: parse + embed + fn search over this repo | chromadb + Ollama (or `NEURONAV_EMBED_FAKE=1` — CI mode) |
| `test_pyhard` | python extractor edge cases on `fixtures/pyhard` | numpy + chromadb import only (hermetic fixture config) |
| `test_cpphard` | C++ extractor edge cases on `fixtures/cpp` (issue #13): macro surface, .h/.cpp pairing, registration harvest, dead tiers, determinism | tree-sitter + tree-sitter-cpp import only (hermetic fixture config) |
| `test_tshard` | TS extractor edge cases (grammar split, barrels, aliases, overloads, defaults, decorators, JSX, dead tiers, determinism) | tree-sitter + tree-sitter-typescript wheels (hermetic fixtures) |
| `test_jshard` | JavaScript extractor edge cases (issue #277: grammar split js/jsx + binding-name pin, CJS require/module.exports beside ESM + interop defaults, ESM/CJS barrels wiring-only with origin rebind, jsconfig aliases (good + malformed), React entry rules, mixed .ts+.js resolution both directions, dead tiers + mention floor, registry/RAW_TEXT_EXTS/preset pins, determinism) | tree-sitter + tree-sitter-javascript/-typescript wheels (hermetic fixtures) |
| `test_rusthard` | Rust extractor edge cases (pub-mod API closure, `pub use` rebinding, trait dispatch, test attrs, macros, dead tiers, sabotage leg, determinism) | tree-sitter + tree-sitter-rust wheels (hermetic fixtures) |
| `test_selfindex` | self-index structural invariants: likely-dead zero, handlers stay review, deterministic rebuild | chromadb import (structural only) |
| `test_target_regression` | byte-stability over the target repo: floor pins + liveness canaries | chromadb import + the target repo configured in `config.json` |
| `test_tsregression` | TS target byte-stability + liveness canaries + parse-coverage floors (per-command untracked profile); hermetic section pins the judge-C1 dead-file registry resolution (`.ts` flags like the `.gd` control) + the `# imports:` doc header | chromadb import + the TS target via `NEURONAV_CONFIG` |
| `test_rustregression` | Rust target byte-stability + fn-level liveness canaries + parse-coverage floors (per-command untracked profile, issue #284); hermetic section pins the crate shape — lib.rs pub-mod closure, wiring-only barrel, bin target as its OWN crate root, integration-test name-level wiring (no cross-crate static edges, pinned as a non-goal) — plus dead-file flags and the `# imports:` doc header | chromadb import + the Rust target via `NEURONAV_CONFIG` |
| `test_explore` | explore() happy/degraded/no-hit paths, windowed slices + anchor paging (issue #69) + MCP tool annotations; CI leg self-bootstraps the self-index under FAKE (issue #180) | mcp + chroma + populated self-index (CI: self-populated via FAKE rescan) |
| `test_server_stdio` | MCP stdio end-to-end: spawns server.py, drives JSON-RPC, asserts the context tool answers; drift/stat-gate, routed-freshness, recall-knobs (graph_boost/two_pass), degraded-shape scenarios (issue #180); dead_code truncation footer + duplicates pure-delegate skip footer on hermetic corpora (issues #266/#268) | mcp + default-config target repo (CI: self-index FAKE bootstrap) |
| `test_autorescan` | auto-rescan stat gate (issue #19): read-tool freshness, TTL burst guard, embed-failure cooldown, `watch_interval_s` watcher — in-process pins + two stdio e2e servers + the #239 chroma hnsw-settle retry pin (constructed interleaving, no real race needed) | mcp + chromadb + numpy/networkx/scipy/scikit-learn (hermetic temp target + `NEURONAV_EMBED_FAKE=1`) |
| `test_delegates` | pure-delegate duplicate filter (issue #268): thin wrappers drop from exact_duplicates with a counted skip, genuine groups stay, predicate cache stores the filtered list (#71/#116 laws) | chromadb import (hermetic temp fixture, build-only) |
| `test_bootgate` | fast-handshake boot gate (issue #273): while the parent holds the store's cross-process write lock, `initialize` + `tools/list` must still answer; after release the boot thread completes (startup banner) and serves `repo_map`; server-under-test selectable via argv for pre-fix FAIL evidence | mcp + chromadb (hermetic temp fixture + config, `NEURONAV_EMBED_FAKE=1`) |
| `test_impact` | impact() transitive blast radius (issue #280): cycle termination, diamond single-count, honest depth caps with a counted past-the-cap frontier, direction asymmetry, entry-boundary annotation, byte-stable rendering, miss suggestions | mcp import (synthetic graphs; no index, no embeddings) |
| `test_searchtext` | capped `search_text` tool (issue #68): file:line:row shape, deterministic order, 20-file/3-line caps with markers + totals, `files_only`, glob, graceful regex errors | mcp + chromadb (hermetic temp config, `NEURONAV_EMBED_FAKE=1`) |
| `test_baseindex` | export/import-base shards (issue #102): second-run idempotence (WinError 183), per-phase non-destruction (mid-write debris outside base, commit rollback, cleanup self-heal), byte determinism, stale-shard cleanup, fresh-store roundtrip, stale-id skip (sources deleted since export), manifest model/dim-mismatch refusal (issue #124) | chromadb import (hermetic temp target, `NEURONAV_EMBED_FAKE=1`) |
| `test_repomap` | repo_map budget bound, byte determinism, rank ordering, god-hub saturation on synthetic graphs | stdlib + numpy (no index, no embeddings) |
| `test_mwires` | named-wire map exports (`mwires`/`fns`/`meta` contract, map-spec-v2 §0) | chromadb import only (self-sets `NEURONAV_EMBED_FAKE=1`) |
| `test_clusterinv` | cluster partition invariant + crosstalk parity (issue #114): finalize's family moves vs full-weld regroups can double-assign a file — repaired by weld plurality (last pass, identity on healthy input); crosstalk counts only wiring the clusterer's structural graph sees (tests/ endpoints tallied separately, no cluster number) | numpy + chromadb import only (crafted shapes + stub graph, self-sets `NEURONAV_EMBED_FAKE=1`) |
| `test_archrules` | arch-rule engine over crosstalk (issue #70): planted per-kind violations caught (exact wire counts + ranked offending file pairs — the sabotage teeth), typed rules, cluster ref forms (label / case-fold / cN id), typo guard (unknown kind/cluster/type/key, malformed JSON, duplicate ids — named errors, never silently skipped), absent-rules answer, determinism, #114 parity vs `crosstalk()` (tests/ + unclustered wiring feeds no rule number), rules follow the routed state dir | numpy + chromadb import only (synthetic partitions + stub graphs, self-sets `NEURONAV_EMBED_FAKE=1`) |
| `test_recall` | hybrid recall fusion, ctx hops, degraded mode (real+fake modes), query-prefix construction pin (#217) | chromadb import; exact-rank pins need a real-embedded store |
| `test_embedprov` | embed provider contract (issue #17): provider select/auto-detect, ollama+openai wire adapters, env-vs-config key precedence, keyless no-header, 401 loud, 429 retry, batch chunking, no-pad mismatches, fake-mode isolation, pre-#17 store heal vs provider-drift refusal + raw-provider messages (#159) | stdlib http.server stub on an ephemeral loopback port + chromadb import |
| `test_project_mode` | onboarding (issue #27): discovery precedence env > project-local > checkout, `onboard.init`/`wire` scaffolds incl. `.neuroignore` (issue #36), init re-run NEVER clobbers a customized config (issue #121), wire BOM-tolerant + loud on malformed MCP jsons + atomic writes (issue #121), `wire --omp` mcpServers fragment shape (issue #130: default/`--omp-name` server names, merge-preserving writes, two-project no-collision, hermetic `NEURONAV_OMP_MCP` reroute), viz add-on degrade | stdlib + chromadb import (fresh subprocesses, fake embeds) |
| `test_viz` | Playwright harness over the real baked page (200+ checks, corpus-dependent); CI mode = frozen corpus (issue #100), local mode = the active config's store (default `config.json` or the self-index); [#89] refuses a bake older than `viz.py` (no auto-bake — regenerate first); [#123] executed-check floor pinned to the CI corpus (`FLOOR_BASE`/`FLOOR_MAP`, data-gated on `DATA.mwires`) + quiescence waits (`__dbg.settled`) instead of blanket sleeps; [#279] affinity legs (species cap/LOD/toggle/card + two-bake byte identity) data-gated on `DATA.semAff` | playwright + chrome + a fresh `graph.html` bake |
| `test_verifier` | Kythe-style verifier fixtures (issue #66): `//-`-shaped goal comments inlined in fixture sources, asserted against extractor output (FileSym + cpp scan_calls); `@fn dead` is corpus-local liveness | stdlib + tree-sitter/tree-sitter-cpp for the C++ goals — extractor-level only: no config, no index, no chroma |
| `test_bench` | bench record/golden coherence (issue #104): fingerprint determinism + order-insensitivity, render() refuses mismatched/missing fingerprints naming every stale record, coherent sandbox render e2e | stdlib only — imports bench/run_bench.py's render path against a temp bench dir; no config, no index, no embeds |
| `test_bakeint` | bake integrity (issues #64/#108): empty/zeroed-store bake refusal naming the store + vector counts + the rescan fix, FAKE-only tiny-store waiver, strict-JSON splice (NaN/Infinity refused with paths), `</script`/`__DATA__`/`__IMPORTMAP__` breakout-token refusal, atomic `os.replace` bake write | chromadb import (self-sets `NEURONAV_EMBED_FAKE=1`; the real-provider refusal legs run in FAKE-scrubbed child processes) |
| `test_portability` | BOM-tolerant config reads (issue #119): BOM'd config.json / .neuroignore / base manifest / server `_validate_foreign_config` all read via `utf-8-sig`; git subprocess decode (`bake.gitinfo` head/churn) stays UTF-8 under an ASCII locale; nav CLI reconfigures stdout under an ascii console | stdlib + chromadb import (hermetic temp config, fake embeds, own scratch git repo) |
| `test_bytelaws` | byte-level output laws (issue #124): `_importmap` pinned against the five vendored files (CRLF→LF embed law, relative-specifier rewrite, determinism), UTF-8-no-BOM law asserted on every generated JSON artifact (onboard config scaffold, export_base manifest, baked graph.html DATA/importmap splices) | chromadb + numpy import (hermetic temp config + FAKE store, self-sets `NEURONAV_EMBED_FAKE=1`) |
| `test_walkguard` | rescan walk + write robustness (issues #117, #296): vanishing-file parse isolation (stderr note, never a crash), pruned root-wide .tres wiring walk honoring exclude_dirs + the cache floor (now derived from `nav.WALK_DEFAULTS` — one canonical set, divergence-pinned), include-overlap dedupe on index key, fn-store purge/upsert inside the write lock, #296 legs: include_dirs opt-in walk-everything fallback (no more Godot-layout default), root `.gitignore` dir-entry pruning with `.neuroignore` precedence over `!negations`, one-shot gitignore-prune stderr note (FAKE-silent), canonical-set equality | chromadb import (hermetic scratch corpus + FAKE embeds) |
| `test_qa_smoke` | hermetic smoke for the QA gate + bake-only serve.py (issues #120, #124-3): leg A pins serve.py's surface over a dummy bake (sole route `/graph.html`, `/chroma`+`/base`+traversal 404s, loopback-Host allow/403, no-store, bytes-fresh); leg B (playwright) builds a tiny 5-file corpus + FAKE store + bake, runs `--declutter` (exit 0 + baseline shape pinned: identity block + every subject x angle x GATE_KEYS cell), `--after` (exit 0), and the refusals — tampered/schema/identity-less baselines exit 2 naming both identities, probe exhaustion exits 2, orphan-held port bind fails loudly with the owner hint | stdlib for leg A; playwright + chrome for leg B (everything under machine temp; `NEURONAV_QA_DIR` reroutes battery outputs) |

`_page_harness.py` (issue #86 strand R9) is the shared Playwright harness
the two browser suites ride: `serve()` (no-cache loopback server, ephemeral
port per issue #132; the pre-#120 `reuse` double-bind option is gone —
an explicit `port=` arms the serve.py issue #40 exclusive-bind
refusal — a taken port exits 1 with the per-OS `port_owner_hint()` remedy,
never a raw traceback, #123), `require_fresh_bake()` (#89 stale-bake
refusal — test_viz calls it before serving), `launch()` (real Chrome),
`open_page()` (settled 1600x900 page with optional console/pageerror
capture; quiesces on `__dbg.settled` when the bake exposes the getter,
else the historic fixed settle), `quiesce()` (#123 readiness wait),
`probe_dbg()` (broken-bake probe), and the `CheckLog` accumulator with the
suites' summary/exit contract plus the #123 executed-check floor
(`finish()` fails an executed shortfall — suites set `LOG.floor` from the
data-shape). Consumed by `tests/test_viz.py` (sys.path) and
`tools/qa_readability.py` (`tests._page_harness`) — the suites keep their
own assertions; harness changes may never weaken or drop a check.

`probe_scene_placement.py` is a manual probe script, not a suite.

## Config self-selection (the leakage trap)
Suites pick their own config; the shell must not pre-export one:

- `test_crosslang` / `test_selfindex` hard-set
  `NEURONAV_CONFIG=<repo>/config/neuronav.json` (self-index profile).
- `test_explore` uses `os.environ.setdefault` — an exported var WINS, which
  is exactly why exporting `NEURONAV_CONFIG` in your shell before running
  suites silently points them at the wrong index. Never export it. Under
  `NEURONAV_EMBED_FAKE=1` it self-populates an empty self-index store
  (issue #180: one FAKE rescan + fn sync when count == 0, the #166
  pattern) — a populated real store is never touched.
- `test_explore`'s no-hit legs are deterministic by construction
  (issue #249 decode): `find_functions` has no relevance floor — on any
  populated fn store it returns top-n cosine neighbors for EVERY query,
  so a nonsense string's score is embed-space-dependent (0.08 under
  FAKE hash vectors, 0.549 against real embeds on the recreated venv)
  and "no hits" is unreachable through query choice alone. The legs
  force the empty-index contract (`find_functions` -> `[]`, the
  `count == 0` branch) and assert the scoring internals first —
  absent-token query -> `_lexical_fallback == []` and
  `_seed_hits == ([], True, None)` — then pin the guidance marker
  `no hits for` (the old `rescan` substring also matched repo-map
  signatures like `rescan(timeout)`: vacuous passes).
- `test_pyhard` / `test_mwires` write a generated temp config under the
  system temp dir pointing at `tests/fixtures/<name>` only — they never touch
  the real index.
- `test_bakeint` builds a scratch corpus + FAKE store under the system
  temp dir (the corpus-builder shape, miniature); its real-provider
  refusal legs run in child processes with `NEURONAV_EMBED_FAKE`
  scrubbed from the environment — never a live store.
- `test_verifier` never touches config at all — it calls extractor
  `parse()` directly, so an exported `NEURONAV_CONFIG` is simply unseen
  (the one suite an exported var cannot leak into).
- `test_bench` is in the same boat: it renders in a scratch bench dir
  (patched `BENCH_DIR`/`DEFAULT_REPO`) and never imports nav — config is
  unseen.
- `test_autorescan` generates its own temp TARGET TREE + config under the
  system temp dir (separate state dirs for the in-process and e2e-server
  sections) and self-sets `NEURONAV_EMBED_FAKE=1` — never run it against a
  real profile.
- `test_baseindex` generates its own temp TARGET TREE + config under the
  system temp dir (a second state dir for the import roundtrip) and
  self-sets `NEURONAV_EMBED_FAKE=1` — never run it against a real
  profile.
- `test_project_mode` builds throwaway project trees under the system
  temp dir and drives init/wire/discovery in fresh subprocesses with
  fake embeds — never touches a real profile.
- `test_server_stdio` strips `NEURONAV_CONFIG` from the child env so the
  server binds the default profile; on a fresh checkout with FAKE embeds
  available (CI) it instead binds the self-index profile and bootstraps
  its store (issue #180) — its drift/degraded scenarios always run on
  hermetic scratch trees under `.team_scratch/`.
- `test_embedprov` writes a generated temp config under the system
  temp dir, points `embed_url` at its own loopback stub (ephemeral
  port), and manages `NEURONAV_EMBED_FAKE`/`NEURONAV_EMBED_KEY`
  itself — it never touches a real profile or model server.
- `test_viz` serves the ACTIVE config's state dir — point
  `NEURONAV_CONFIG` at your scratch store config before running (issue
  #89 trap; the boot banner shows what you serve; a bake older than
  `viz.py` is refused outright — regenerate, never auto-bake). CI runs it
  against the frozen corpus store built by `tests/vizcorpus_build.py`
  (`--dest <scratch>` → repo-shaped GDScript corpus with real git
  history, fake embeds, own config + `.neuronav`; hermetic — never a
  live store).
- `test_qa_smoke` builds everything it needs under the system temp dir
  (tiny corpus, FAKE store, bake, QA output via `NEURONAV_QA_DIR`) — it
  never reads or writes a live store or real baselines.
- `test_target_regression` uses the default `config.json` — the target
  repo must exist at its configured path.
- `test_tsregression` hermetic section writes a generated temp config
  under the system temp dir rooted at `tests/fixtures/tsreg`; its profile
  legs rebind via `nav._apply_config` to whatever `NEURONAV_CONFIG` names
  (skip loudly without one, issue #97) — never export the var. The named
  profile owns the numbers: the parse-coverage floor is read from its
  `ts_regression.parse_floor` key and liveness canaries from its
  `regression_canaries` block (dead `[path, func]` pairs, alive name
  tokens) — both machine-local, never tracked.
- `test_rustregression` mirrors that shape for Rust (issue #284): hermetic
  leg over `tests/fixtures/rustreg` (crate-shaped); profile legs bind the
  #284 audit target (anubis daemon-rs) via an untracked scratch profile —
  `.tmp/anubis-profile.json` on this machine, root at
  `E:/GitRepos/anubis-public/packages/daemon-rs`, `state_dir` outside the
  target per #91. Floors come from `rust_regression.parse_floor`/`rs_floor`
  and canaries from `regression_canaries` (dead `[path, func]` pairs;
  alive pairs are fn-level — the Rust refinement of tsregression's
  file-level tokens). Skip loudly without the env (#97); never export it.

- Launches real Chrome via `channel="chrome"` (no browser download).
- Serves the repo root on an **ephemeral loopback port** (issue #132): viz
  gates may run concurrently — no fixed-port claims, no orphaned-process
  holds, no TIME_WAIT rerun failures. (`tools/serve.py` keeps its explicit
  port; `test_project_mode` still pins 9081-9090 to exercise serve.py's
  port-refusal contract.)
- Regenerate `graph.html` first — the harness tests the bake, not the
  template. Enforced since #89: the gate refuses a bake whose mtime
  predates `viz.py` (the refusal names the remedy; never an auto-bake).
- Config-agnostic: assertions data-gate on index content, so the self-index
  profile and the default target profile both run clean.
- `test_strata` imports `layout.py` directly — the five pure fns
  (`_links_adj`/`_tarjan_scc`/`_strata_depths`/`_strata_analysis`/
  `_layout`, moved out of `viz.py` in issue #86 phase 2). Keep
  `layout.py` importable with stdlib + numpy only (no nav/graph/chroma
  edges).

## fixtures/

- `fixtures/pyhard/*.py` — per-feature liveness fixtures for the python
  extractor; each keeps a dead control func so the run is not vacuously
  all-alive.
- `fixtures/mwires/*.gd` + `*.tscn` — named-wire map contract fixtures
  (multi-script scenes discriminate signal resolution; unresolved
  connections counted).
- `fixtures/cpp/*.h` + `*.cpp` — C++ extractor fixtures: macro/
  registration surface, `.h`/`.cpp` pairing, dead tiers, and the
  mention-rescue pair (`mention_rescue.cpp` + its caller).
- `fixtures/ts/*` — TS extractor fixtures (issue #245): grammar-split JSX,
  barrel re-export chains, tsconfig aliasing (incl. a malformed-tsconfig
  degraded-mode sibling), overload collapse, default exports, decorators,
  super calls, ambient `.d.ts`, and the dead-tier pair (`dead_helpers.ts`
  + mention partners in `barrel_view.tsx`).
- `fixtures/js/*` — JS extractor fixtures (issue #277): grammar-split JSX
  composition, ESM + CJS barrels, interop defaults, jsconfig aliasing
  (incl. a malformed-jsconfig degraded sibling), React entry rules
  (exported components, render target, stories), super calls, mixed
  .ts+.js resolution, and the dead-tier pair (`dead_helpers.js` +
  `mention_sink.js`).
- `fixtures/tsreg/*` — TS dead-share + import-header fixtures: the `.ts`
  pair (dead-share denominator, resolved import line), a `.gd` control
  for the judge-C1 registry-resolution pin, and a `.py` pair for the
  language-neutral `# imports:` doc head.
- `fixtures/rust/*` — Rust extractor fixtures (issue #244): pub-mod API
  closure, re-export rebinding, trait dispatch, entry attributes, macro
  call-sites, wiring-only barrel, orphan controls, and the cargo-bin
  own-crate-root pair (`bin/cli.rs` + `bin/cli/helper.rs`, issue #284).
- `fixtures/rustreg/*` — crate-shaped rust regression fixture (issue
  #284): lib.rs pub-mod barrel + re-export, in-crate alias rebind,
  dead-share pair, a bin target with its own module tree, and an
  integration test wired through the crate name.
- `fixtures/verifier/*` — synthetic annotated fixtures for extractor
  facts with no committed coverage (gd declared surface, tscn PackedScene
  instancing). Goal-comment grammar: `tests/test_verifier.py` header;
  pyhard/cpp/mwires fixtures carry the same `//-`/`#-`/`;-` goals
  inlined above the constructs they assert.
- Suites copy nothing into the real index; generated configs go to the
  system temp dir.

## Pin-update policy

`test_target_regression` pins range floors (files, edges, dead) + canaries.
They exist to catch extractor/graph regressions. The target repo drifts on
the user side — re-pin floors only for that drift, saying so in the commit;
never re-pin to make a regression disappear. Same rule for count windows in
`test_selfindex` (handler window) and shape guards elsewhere.
