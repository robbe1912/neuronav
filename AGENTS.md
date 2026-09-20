# AGENTS.md — working ON neuronav

Local code-intelligence tool: vector recall (chroma + a pluggable embed
provider — Ollama by default or any OpenAI-compatible `/embeddings` endpoint,
issue #17), call/signal graph, clusters, dead-code tiers, 3D visualizer, stdio
MCP server. Python 3.12,
stdlib-first; heavy deps: chromadb/httpx (vector store + embed transport), mcp
(stdio server), numpy/networkx/scipy/scikit-learn (the cluster/graph math the
read tools ride), plus the pinned tree-sitter front-ends tree-sitter==0.26.0,
tree-sitter-cpp==0.23.4 (issue #13; also the C front-end, issue #346),
tree-sitter-typescript==0.23.2 (issue #245),
tree-sitter-javascript==0.25.0 (issue #277), tree-sitter-rust==0.24.2
(issue #244), tree-sitter-go==0.25.0 (issue #334), tree-sitter-java==0.23.5
(issue #335), tree-sitter-c-sharp==0.23.5 (issue #336),
tree-sitter-php==0.24.1 (issue #343), and tree-sitter-lua==0.5.0
(issue #342). Standalone repo —
point it
at any project via config; nothing is vendored into target projects.

Per-directory docs: `extractors/AGENTS.md`, `tests/AGENTS.md`, `tools/AGENTS.md`,
`config/AGENTS.md`.

## Non-negotiable invariants

- **No target-repo data in tracked files** — NOTHING derived from any
  target repo may be committed: no file/class/function names, no paths, no
  screenshots, no absolute machine paths (drive letters, home dirs — a
  grep for those shapes over tracked files must stay empty), no measured
  baselines (`.tmp/qa/` is gitignored machine-local state). Tests and
  probes must be config-agnostic (derive targets from the loaded index,
  see `test_viz`/`probe_scene_placement`/`test_server_stdio`); regression
  canaries live in the untracked machine-local config passed per-command
  via `NEURONAV_CONFIG` under `regression_canaries`. Same rule for every
  future target (the engine profile included).
- **Determinism**: same DATA -> same layout byte-for-byte. `_layout` uses a
  seeded rng (1234); the regression suite over the target repo
  (`test_target_regression`) pins range floors — >=630 files, 6500–8100 edges,
  60–130 dead, plus liveness canaries (a known-dead func must stay dead,
  known-alive funcs must stay alive). The target repo drifts on the user side;
  re-pin the floor on user-side refactors, never to mask extractor regressions.
  Never introduce unordered iteration into layout or export paths.
- **Gate before every commit** — all suites, 0 failures:
  `.venv/Scripts/python.exe -X utf8 tests/test_<name>.py`
  (fresh clone: `uv venv && uv pip install -e .` builds that `.venv` from
  the committed ==-pin set; on git-bash/MSYS, if
  exec'ing `.venv/Scripts/python.exe` fails, run
  `uv run --no-project python -X utf8 tests/test_<name>.py`)
- **Config leakage trap**: suites self-select config via internal
  `setdefault`. NEVER export `NEURONAV_CONFIG` in the shell before running
  them — the env var overrides and silently points suites at the wrong index.
- **Loud failures**: a failed offline layout aborts the build (`RuntimeError`);
  the browser re-validates baked DATA. No silent fallbacks (sanctioned
  degraded modes: `explore.py`'s lexical fallback and recall's BM25F-only
  mode, each marked "degraded" in output). Auto-rescan failures (issue #19)
  warn once on stderr, retry-suppress for 60s, and answer from the current
  index — never crash the tool call.

## Architecture map

| module | role |
|---|---|
| `navconfig.py` | config leaf (issue #344): resolution (env > project-local > checkout-local > pure defaults), BOM-tolerant reads, `.neuroignore`/`.gitignore` prune unions, the #91 state_dir contract, `use_config`/`config_scope` — owns every rebindable config global (sibling modules read them as `navconfig.X` attributes, never from-imports) |
| `navstore.py` | store/embed leaf (issue #344): chroma collections (stamps/heals #103, bounded reads #327), the provider-pluggable embed client (ollama/openai wires, issue #17), hybrid recall + clusters queries — owns the process-global store singletons (per-store FileLock/PersistentClient/cluster memo) |
| `navindex.py` | index leaf (issue #344): the walk (walkguard #117, gitignore #296) with one dir-prune + suffix filter leaf shared by all three walk engines (#375), the stat gate (#19), incremental rescan (vanish-tolerant read leg — a file gone between listing and sha/read skips loudly and the purge leg reconciles it same-pass, #374), tracked base import/export (#102), build-observability hooks (#315) |
| `nav.py` | the CLI (issue #344 split): `nav.py <cmd>` orchestration + the #332 trailing-argv rejection — never imports `viz` (bake-free by design, issue #86 R8); every programmatic consumer lives on the leaves above |
| `graph.py` | file/fn symbol graph, per-fn IO extraction, dead-code tiers, facade for the doc shaper (issue #366 carve) — the cAST chunk + file-doc shaping core moved verbatim to `docshape.py` and is re-exported here, so every pinned name (`file_doc`, `FILE_DOC_REV`, the shaper helpers) resolves unchanged |
| `docshape.py` | the cAST doc-shaping subsystem — size-aware chunk helpers (micro merge + monster split) + the per-file doc shaper `file_doc`, moved verbatim from `graph.py` (issue #366; stdlib + extractors + the navconfig/navstore leaves, no graph import — byte-identical generated docs) |
| `recall.py` | hybrid recall leaf: BM25F lexical ranks RRF-fused with vector ranks, post-fusion graph-neighbor boost (issues #73/#228), task-instruction query prefix (#217), two-pass retrieve (#74) — pure-stdlib lexical side, no embed backend import |
| `predicates.py` | derived-predicate cache (issue #71): dead-code tiers, duplicate groups, PageRank, symbol degrees, corpus mentions derived once at build/rescan and persisted under the state dir — query time becomes dict reads; byte-identical cache bytes for the same index data |
| `memories.py` | Serena-style project memories (issue #67): plain markdown files under `<state_dir>/memories/`, atomic same-dir-temp + `os.replace` writes, rides state_dir routing (#131) |
| `extractors/` | 12 per-language parsers behind the suffix registry — gdscript (`.gd`/`.tscn`), python, cpp, ts, js, rust, go, java, c, csharp, php, lua (+ `model.py` dataclasses, `common.py` shared front-end leaf; see extractors/AGENTS.md) |
| `clusters.py` | Louvain engine + labeler + crosstalk, facade for the finalize pipeline (issue #380 carve) — the split/label pass core moved verbatim to `clusterpasses.py` and is re-exported here, so every pinned name (`finalize`, pass constants) resolves unchanged |
| `clusterpasses.py` | the finalize pass pipeline for `navstore.clusters()` — blob split + label passes, moved verbatim from `clusters.py` (issue #380; stdlib + extractors at top level, numpy/sklearn stay function-scoped, labeler seam imported at call time — byte-identical partitions) |
| `archrules.py` | architecture-violation rules over crosstalk (issue #70): reads `<state_dir>/arch-rules.json` at call time (routed per project, the memories.py law); forbid/budget kinds with optional edge-type filter; loud config errors (unknown kind/cluster/type/key, malformed file — never silently skipped); rides `clusters.cross_tallies` so rules see exactly what crosstalk counts (#114 parity) |
| `explore.py` | one-call orientation tool (codegraph-discipline: windowed 100-line slices + continuation anchors, issue #69; one `clusters()` pass feeds both stages, issue #44) |
| `servercore.py` | the FastMCP instance leaf (issue #345): `mcp` + the #207 version pin, #237 instructions, tool annotations, and the shared display caps (`_capped`/`_capped_row`) — one composition truth; server.py and every family module decorate against the same object |
| `server.py` | FastMCP stdio server + the boot/lifecycle core (issue #345 split): fast handshake boot gate (#273), auto-rescan watcher wiring (#19), progress rails (#315), memory/rescan tools, and composition of the query families + the bake leaf below — read-only tools carry `readOnlyHint`, `rescan` is the write tool; the pinning suites reach `_serve`/`_BOOT_*`/`_auto_rescan` through this module's namespace, so those definitions stay here |
| `serverbake.py` | the bake-queue leaf (issue #360): `_BAKE_*` state, `_bake_loop`/`_start_baker`, the visualize tool and the `neuronav://onboarding/status` resource — verbatim moves from server.py; rails bind at `register(...)` per shape: call rails ride late proxies (the servercore._Rail law), object/value rails (`_SCOPE_LOCK`/`_BOOT_READY`/`LOCK_WAIT_S`/boot gate) re-read from server's namespace on every rail call and status read — a module `__getattr__` cannot reach the bodies' LOAD_GLOBAL misses and a frozen copy would hide the pinning suites' rebinds (#359); visualize's `mcp.tool` registration stays in server.py (composition order = tools/list byte-identity) |
| `server_search.py` | query family (issue #345): explore / repo_map / semantic_search / find_functions / search_text — verbatim moves; handlers carry literal `@mcp.tool` decorators against servercore's `mcp`; gate rails bind at `register(...)` as late-binding `_Rail` handles resolved through server's module at call time (importing them from server would cycle; copies would freeze the test seam, issue #359) |
| `server_structure.py` | query family (issue #345): symbol_graph / impact / dead_code / duplicates + the #125 truncation-law renderers |
| `server_clusters.py` | query family (issue #345): clusters / crosstalk / arch_check / context + the context renderers (`_capped`/`_capped_row` live in servercore) |
| `viz.py` | Python `_build_data` orchestrator (pure wiring, #299 B) -> `graph.html`; owns the stage order and threads results through the `bake/` leaves; `ensure_bake()` (issue #86 R8) is the single rescan->bake entry — `server.visualize` and `onboard._index` delegate to it; `generate()` refuses empty/zeroed stores loudly, naming the store + counts + rescan fix (issue #64; tiny-store waiver is `NEURONAV_EMBED_FAKE=1`-only) and the splice is strict-JSON, `</script`/token-refusing, atomic via `os.replace` (issue #108) |
| `vizjs/` | the graph.html JS template as 17 ordered modules (`_HTML_HEAD` + one per `_JS_*` section, #299 A) joined by `vizjs.template()` at `generate()` time — see the template laws below |
| `layout.py` | pure strata/layout math for the bake: adjacency, iterative Tarjan SCC, strata depths, seeded force layout (moved verbatim from `viz.py`, issue #86; stdlib + numpy only, no nav/graph/chroma imports) |
| `bake/` | pure per-job transforms for the viz DATA pipeline (issues #86 phase 2, #299 B): `gitinfo` head/churn stamps, `files_model` J1-J4, `wires` J5-J8, `embeddings` J9 kNN + the ONE chroma fetch (the store-index space derives exactly once there, #299 C), `semantics` J11 cluster matrix + J10 supergroups + #279 semAff rows, `overlays` J13/J14/J17, `fnio` J15-J16, `budget` row-cap keeper — data arrives as arguments; the only nav edge is `bake.embeddings` |
| `onboard.py` | one-command project onboarding (issue #27): `init`/`wire` write `<project>/.neuronav/config.json` + MCP entries; `wire --omp` emits the omp harness mcpServers fragment (issue #130); `global-wire` emits ONE uvx entry (`uvx --from git+…@vX.Y.Z neuronav-mcp`, issue #204) on all four harnesses — the install stays read-only, OS-agnostic pure stdlib |
| `tools/` | dev gates: `qa_readability.py` (readability/declutter gate), `serve.py` (headless-dev no-cache HTTP for the bake only — production is opening `.neuronav/graph.html` directly, file://, issue #133; exclusive bind + per-OS port-owner hint), `run_battery.py` (parallel full-battery driver — every `tests/test_*.py` suite, pool + serial/gated legs, per-suite exit codes, issue #286) |
| `config/` | named config profiles, machine-portable only (relative `root`s); the root `config.json` is deliberately ABSENT (issue #204 — the repo carries no machine values; consumers pass `NEURONAV_CONFIG` per-command or boot pure-defaults on cwd) |
| `vendor/three-0.160.0/` | vendored three.js core + 4 addons, embedded at build (see below) |
| `bench/` | recall benchmark: golden set, `run_bench.py`, committed results (`RESULTS.md`) — the numbers `docs/comparison.md` cites |
| `tests/` | 50 self-contained suites + committed fixtures (see tests/AGENTS.md) |
| `docs/map-spec-v2.md` | spec the named-wire map layer implements |

## vizjs template laws

The template is the `vizjs/` package — `_HTML_HEAD` then one module per
`_JS_*` section (#299 A). `vizjs.template()` joins them in the rung order
pinned in `vizjs/__init__.py` — provably the original text order, so the
bake stays byte-identical to the former single string, one script tag;
the `__DATA__` and `__IMPORTMAP__` replaces are unchanged.
Edit JS directly, but `graph.html` bakes the template at
`generate()` time: **regen after every template edit** or you test stale JS
(this has bitten us). Production opens `<state_dir>/graph.html` directly
(file:// — the bake is self-contained, issue #133); only headless dev
rigs serve it via `python tools/serve.py` (no-cache, 127.0.0.1:8791).

- `window.__dbg` is the harness contract: tests read `alphaTgt`, `bucketPosIB`,
  `hubCap`, `fns`, `meta`, `fnLod`, `busPts`, `corridorCensus`, ... — extend it,
  never remove entries tests use.
- Module-scope ordering matters: the joined template executes top-to-bottom
  at boot (TDZ) — join order IS execution order. Functions called at module
  scope must only reference symbols declared earlier.
- JSON writes from any helper script: UTF-8 WITHOUT BOM
  (`[IO.File]::WriteAllText`), PowerShell 5 `Set-Content -Encoding utf8`
  writes a BOM that kills `json.loads`.

### Corridor architecture (as committed)

- **fn-layer corridors**: calls render as bus/trunk/leg junction corridors —
  file-pair buses bundle into trunk conduits, per-file stations fan legs out
  to fn boxes, junction bollards mark trunk heads, amber chevrons mark
  delivery direction, quiet trunks absorb low-ink wires. Depth-banding tiers
  and the boot LOD law gate the bus tier: bollards/conduits render only when
  their served boxes are resolvable — no droplets, no open-ended buses;
  chevrons meet the 8px floor or hide (`test_viz` pins this at boot).
- **`ANCHOR_PX = 8`** — endpoint anchor floor in reference pixels (a
  DIAMETER). A served corridor endpoint sprite is boosted so its size never
  drops below the floor: an endpoint registers as anchored when
  `pxOf(fi) >= ANCHOR_PX || anchorBoost[fi] > 0`.
- **Corridor-complete law**: the unit of render is the full path
  `node -> leg -> station -> trunk -> station -> leg -> node`. Serving boosts
  BOTH endpoint sprites — size, not brightness (dim files keep their dim
  color). Culled chains park at instance scale 0.0001 and stay in `busPts` as
  inventory: presence is NOT serve state (pick-vs-render parity).
- **`__dbg.corridorCensus`**: getter exposing every busPts chain leg
  (`"L|fi|st|li"`) and trunk (`"sf>tf"`) with endpoint world coords, serve
  state, and anchor registration — the independent-verification hook the
  harnesses read.
- **semantic-affinity wires (#279)**: the bake's J9 mutual-kNN pairs
  (cosine >= 0.45) ride `DATA.semAff` as a second ink species — violet
  dotted strands in their own color family (never confusable with the
  call/signal corridors), globally capped (`SEM_AFF_CAP = 220`, ranked by
  score then endpoint paths), LOD-gated like the corridor tiers (ink only
  while `lodClose` resolves file boxes), ghost law as chords (endpoint
  alpha < 0.05 collapses the row). The `affinity` toggle is UI state only
  — DATA never changes; file cards list the same capped pairs with cosine
  scores (`#iSem`); `__dbg.semAff` is the probe.

### Vendored three.js

`vendor/three-0.160.0/` pins the exact bytes (five files: `three.module.js`,
`controls/OrbitControls.js`, `lines/LineSegments2.js`,
`lines/LineSegmentsGeometry.js`, `lines/LineMaterial.js`) — content-addressed
URIs at build time (CRLF->LF normalized for byte stability); addon relative
imports are rewritten to importmap keys because `data:` modules cannot
resolve relative specifiers. The addon key set itself is DERIVED from the
joined template's `three/addons/...` import specifiers at import time
(#370 — the template is the single truth, so the keys cannot drift from
its import lines; a specifier with no vendored file aborts loudly, never
a silent skip to a blank page). `graph.html` therefore boots offline with zero
network dependencies — keep it that way; never add a CDN reference.

## Test suites (all must stay green)

| suite | covers | needs |
|---|---|---|
| `test_strata` | depth layering, cycles, determinism (imports the real `layout.py`) | stdlib + numpy |
| `test_crosslang` | self-index integration: parse + embed + fn search | chroma + Ollama (or `NEURONAV_EMBED_FAKE=1` for plumbing-only runs — what CI uses) |
| `test_pyhard` | python extractor edge cases | numpy + chromadb import only (hermetic fixture config) |
| `test_mwires` | named-wire map exports (`mwires`/`fns`/`meta` contract, map-spec-v2 §0) | chromadb import only (suite self-sets `NEURONAV_EMBED_FAKE=1`, hermetic fixture config) |
| `test_clusterinv` | cluster partition invariant + crosstalk parity (issue #114): finalize double-assign repaired by weld plurality (identity on healthy input); crosstalk counts only wiring the clusterer's graph sees (tests/ endpoints tallied separately) | numpy + chromadb import only (crafted shapes + stub graph, `NEURONAV_EMBED_FAKE=1`) |
| `test_archrules` | arch-rule engine over crosstalk (issue #70): planted violations per rule kind caught with exact counts + ranked file pairs, typed rules, typo guard (unknown kind/cluster/type — loud, good rules still evaluate), determinism, #114 parity | numpy + chromadb import only (synthetic partitions + stub graphs) |
| `test_selfindex` | neuronav indexes itself | chromadb import (structural only) |
| `test_target_regression` | byte-stability over the target repo | chromadb import + the target repo via per-command `NEURONAV_CONFIG` (untracked machine-local profile) |
| `test_tsregression` | TS target byte-stability + liveness canaries + parse-coverage floors (per-command untracked profile) | chromadb import + the TS target via `NEURONAV_CONFIG` |
| `test_rustregression` | Rust target byte-stability + fn-level liveness canaries + parse-coverage floors (per-command untracked profile) | chromadb import + the Rust target via `NEURONAV_CONFIG` |
| `test_server_stdio` | MCP tool surface end-to-end (JSON-RPC over stdio) | mcp + default-config target repo (CI: self-index FAKE bootstrap, issue #180) |
| `test_autorescan` | auto-rescan stat gate: freshness, TTL burst guard, failure cooldown, watcher (issue #19) | mcp + chromadb + numpy/networkx/scipy/scikit-learn (hermetic temp target, fake embeds) |
| `test_searchtext` | capped regex text search tool (issue #68): rows/order, 20-file + 3-line caps, truncation markers, totals, files_only, glob, graceful paths | mcp + chromadb (hermetic temp target, fake embeds) |
| `test_baseindex` | export/import-base shards (issue #102): second-run idempotence, failed-run non-destruction, byte determinism, stale-shard cleanup, roundtrip | chromadb import (hermetic temp target, fake embeds) |
| `test_repomap` | repo_map budget/determinism/rank ordering on synthetic graphs | stdlib + numpy |
| `test_cpphard` | C++ extractor edge cases (macro surface, pairing, dead tiers, determinism) | tree-sitter wheels (hermetic fixtures) |
| `test_recall` | hybrid recall: BM25F/RRF fusion, ctx hops, degraded mode | chromadb import (hermetic, `NEURONAV_EMBED_FAKE=1`) |
| `test_tshard` | TS extractor edge cases (grammar split, barrels, aliases, overloads, defaults, decorators, JSX, dead tiers, determinism) | tree-sitter + tree-sitter-typescript wheels (hermetic fixtures) |
| `test_jshard` | JS extractor edge cases, issue #277 (js/jsx grammar split, CJS+ESM both directions + interop, React entry rules, mixed .ts+.js, dead tiers, determinism) | tree-sitter + tree-sitter-javascript/-typescript wheels (hermetic fixtures) |
| `test_rusthard` | Rust extractor edge cases (pub-mod API closure, `pub use` re-export rebinding, trait default-method dispatch, test-attribute entry rules, macro call-site recording, wiring-only barrels, dead tiers, determinism) | tree-sitter + tree-sitter-rust wheels (hermetic fixtures) |
| `test_embedprov` | embed provider contract (issue #17) + collection stamp (issue #103): provider select/auto-detect, ollama+openai wire adapters, env-vs-config key precedence, 429 backoff, stamp keeps/heals hnsw:space | stdlib http.server stub + chromadb import |
| `test_project_mode` | onboarding + config discovery precedence + viz-as-add-on (issue #27) | stdlib + chromadb import (hermetic temp trees) |
| `test_viz` | Playwright harness (real Chrome) — 238 `check(...)` sites, executed count corpus-dependent; CI runs it on the frozen corpus from `tests/vizcorpus_build.py` (issue #100) | playwright + chrome + a fresh bake |
| `test_verifier` | Kythe-style verifier fixtures (issue #66): `//-`-shaped goal comments in fixture sources, asserted against extractor output | stdlib + tree-sitter/tree-sitter-cpp (extractor-level only: no config, no index, no chroma) |
| `test_bench` | bench record/golden coherence (issue #104): golden fingerprint determinism, render fails loud naming stale records, coherent sandbox render e2e | stdlib (bench/run_bench.py render path only; no config, no index, no embeds) |
| `test_bakeint` | bake integrity (issues #64/#108): empty/zeroed-store bake refusal, strict-JSON splice, breakout-token refusal, atomic write | chromadb import (hermetic scratch corpus, `NEURONAV_EMBED_FAKE=1`) |
| `test_portability` | BOM-tolerant config reads (issue #119): BOM'd config.json / .neuroignore / base manifest / server foreign config all read via utf-8-sig; git subprocess decode under an ASCII locale; nav CLI stdout reconfigure under an ascii console | stdlib + chromadb import (hermetic temp config, fake embeds, own scratch git repo) |
| `test_walkguard` | rescan walk + write robustness (issue #117): parse isolation + mid-rescan vanish skip with same-pass purge (#374), .tres wiring walk via registry suffixes + pruned traversal, dedupe, write-locked sync, three-engine filter single-truth legs (#375) | chromadb import (hermetic scratch corpus + FAKE embeds) |
| `test_delegates` | pure-delegate duplicate filter (issue #268): thin wrappers drop from exact_duplicates with a counted skip, genuine groups stay, predicate cache stores the filtered list (#71/#116 laws) | chromadb import (hermetic temp fixture, build-only) |
| `test_bootgate` | fast-handshake boot gate (issue #273): with the store's cross-process write lock held by the parent, `initialize` + `tools/list` must still answer; after release the boot thread completes (startup banner) and `repo_map` serves; argv-selectable server-under-test for pre-fix FAIL evidence | mcp + chromadb (hermetic temp fixture + config, `NEURONAV_EMBED_FAKE=1`) |
| `test_impact` | transitive blast-radius tool (issue #280): cycle-safe deterministic BFS closure over fn-level edges — diamond dedupe, honest depth caps with a counted past-the-cap frontier, direction asymmetry, entry-anchored chains, byte-stable rendering, miss suggestions | mcp import (synthetic graphs; no index, no embeddings) |
| `test_gohard` | Go extractor edge cases (issue #334): go.mod module-prefix imports + loud no-go.mod degrade, method receivers, interface-satisfaction mirroring, `_test.go` entry rules, main/init entries, String/Error std-interface shields, dead tiers, sabotage leg, determinism | tree-sitter + tree-sitter-go wheels (hermetic fixtures) |
| `test_javahard` | Java extractor edge cases (issue #335): package-path import resolution (static-member/wildcard + external loud-degrade), main(String[]) + JUnit entry rules, interface default-method dispatch + @Override review shielding, qualified `new` ctor edges + default-ctor name-level alive, dead tiers, determinism | tree-sitter + tree-sitter-java wheels (hermetic fixtures) |
| `test_csharphard` | C# extractor edge cases (issue #336): partial-class merge, Unity MonoBehaviour lifecycle entry roots, `[Test]`/`[TestMethod]`/`[Fact]` hints + static-Main rule, namespace-qualified call resolution, #if-preproc descent, dead tiers, determinism | tree-sitter + tree-sitter-c-sharp wheels (hermetic fixtures) |
| `test_chard` | C extractor edge cases (issue #346): quoted-includes-only law, `main()` root, stem `.c`<->`.h` pairing, TU callback-table liveness, exact dead-tier surface, determinism, registry ownership (.c -> c, .h stays cpp) | tree-sitter + tree-sitter-cpp wheels (hermetic fixtures) |
| `test_phphard` | PHP extractor edge cases (issue #343): PSR-4 `use` resolution + `use function` binding, convention/PHPUnit entries, ctor/static/trait dispatch, magic-method exemption, dead tiers, determinism | tree-sitter + tree-sitter-php wheels (hermetic fixtures) |
| `test_luahard` | Lua extractor edge cases (issue #342): `require()` dotted-path resolution (incl. the paren-less idiom + `init.lua` modules), `T.m`/`t:m` dispatch, metatable `__index` liveness, convention/test entries, dead tiers, determinism | tree-sitter + tree-sitter-lua wheels (hermetic fixtures) |
| `test_explore` | explore() happy/degraded/no-hit paths, windowed slices + anchor paging (issue #69), #297 relevance floor, empty-fn-store wording (#116 law); CI leg self-bootstraps the self-index under FAKE (issue #180) | mcp + chroma (CI: self-populated via FAKE rescan) |
| `test_langsep` | language-separation law: shared modules contain zero language-conditioned behavior — every language fact lives in extractors/ behind the registry (AST scan + registry behavior legs) | tree-sitter wheels (hermetic fixture config) |
| `test_agent_ab` | agent-level A/B coherence harness (issue #72): both scripted arms answer real questions on the synthetic corpus, the ground-truth check has teeth (fabricated answers cannot pass), determinism, records never leak into run_bench | hermetic temp target + config, `NEURONAV_EMBED_FAKE=1` |
| `test_chunking` | cAST-style size-aware doc chunking (issue #76): pure chunking/merging helpers + model-level merge/split primitives over synthetic fns | stdlib (no config, no index) |
| `test_packaging` | uvx contract end-to-end minus the network (issue #204): build the wheel, install into a scratch venv, spawn the installed `neuronav-mcp` console script with zero config, drive JSON-RPC (initialize, tools/list, repo_map) | wheel build + scratch venv (hermetic temp fixture repo) |
| `test_predicates` | derived-predicate cache laws (issue #71): parity (every cached predicate byte-identical to the on-the-fly walk), determinism (same data -> identical cache bytes), loud failures (corrupt/stale cache rederives or errors) | hermetic fixture + self-index parity, build-only (no embeddings) |
| `test_truthful` | truthful failures (issues #115/#116): model/config errors never masquerade as backend-unreachable, BM25F cache never aliases two corpora, non-ASCII identifiers tokenize, misleading server failure shapes answer truthfully | self-index config (self-selected; in-process + subprocess legs) |
| `test_bootrecovery` | boot recovery retry + bounded rescan (issue #292): failed in-session recovery rolls the config re-bind back (nav globals + env + graph singleton + boot identity), lock-timeout SystemExit lands in the degraded prelude, explicit rescan aborts loudly under a held store lock | mcp + chromadb (hermetic temp root, `NEURONAV_EMBED_FAKE=1`; argv-selectable server-under-test) |
| `test_onboardprogress` | onboarding observability (issue #315): `neuronav://onboarding/status` readable mid-build, progressToken'd rescan streams progress, visualize acks immediately with the bake landing via the background baker, stale reads engage only past the grace window | mcp + chromadb (hermetic temp fixture + config, `NEURONAV_EMBED_FAKE=1`) |
| `test_navsplit` | nav split smokes (issue #344): config precedence + rebind-propagation law across leaves, `config_scope` exact-restore, store-lock/clusters memo identity, CLI trailing-argv rejection (#332), atomic-migration law (zero `nav.X` attribute refs in production modules) | hermetic scratch corpus + FAKE embeds |
| `test_chromabounds` | bounded whole-store chroma reads (issue #327): the four whole-store gets page through `navstore.col_get_all`, teeth sized to the real sqlite ceiling (33k-row store rescans green) | chromadb import (hermetic temp fixture, `NEURONAV_EMBED_FAKE=1`) |
| `test_bytelaws` | byte-level output laws (issue #124): `_importmap` pinned against the five vendored files (CRLF->LF embed law), UTF-8-no-BOM asserted on every generated JSON artifact | chromadb + numpy import (hermetic temp config + FAKE store) |
| `test_qa_smoke` | hermetic smoke for the QA gate + bake-only serve.py (issues #120, #124-3): serve surface pins (routes/404s/loopback-Host), `--declutter`/`--after` exit-0 legs + refusal exits (tampered baselines, probe exhaustion, held port) | stdlib leg A; playwright + chrome leg B (machine temp; CI runs the browser leg in the viz job) |

Playwright harness gotchas: launch `channel="chrome"`; it serves `graph.html`
on an ephemeral loopback port (issue #132) — viz gates may run concurrently,
and orphaned-process port holds / TIME_WAIT rerun failures are structurally
gone. (`tools/serve.py`, the headless-dev-only viewer, keeps its explicit
port by design.) The harness is config-agnostic: assertions data-gate on
index content (dead
files, cycles, clusters) so the self-index, the frozen corpus, and the
target repo all run clean (issue #97: interaction checks skip loudly when
the index shape cannot exercise them — no shape is silently assumed).

`layout.py` owns the five pure strata/layout functions (`_links_adj`,
`_tarjan_scc`, `_strata_depths`, `_strata_analysis`, `_layout`) moved
verbatim out of `viz.py` (issue #86); `tests/test_strata.py` imports it
directly. Keep the module free of nav/graph/chroma imports — it must stay
importable with stdlib + numpy only.

Pin-update policy: the regression floors exist to catch extractor/graph
changes. Re-pin ONLY on user-side target drift, saying so explicitly; never
re-pin to make an extractor regression disappear.

Visualizer work also gates through `tools/qa_readability.py` (see
`tools/AGENTS.md`): `--declutter` baseline battery, `--after` ratchet mode,
`--affordance` for sanctioned hub-affordance ink.

## Config profiles

- Root `config.json` — deliberately ABSENT and never restored (issue
  #204: the repo is consumed via `uvx …@tag neuronav-mcp` in any
  directory with zero config; no machine values may live here). Target
  repos are selected per-command via `NEURONAV_CONFIG` pointing at an
  untracked machine-local profile.
- Per-project state (issue #15): everything a profile generates (chroma
  store, base shards, `graph.html` bake) lives in `state_dir` — an
  explicit path, or `"default"` for `<root>/.neuronav/` (`onboard.py
  init` writes the opt-in). No auto-migration — a root without
  `.neuronav` builds a fresh store on the next rescan (one-time re-embed).
- `config/<name>.json` — alternate profiles, selected via `NEURONAV_CONFIG`
  (absolute path). Relative `"root"` values resolve against the config file's
  directory.
- An explicit `NEURONAV_CONFIG` pointing at a missing file aborts at load,
  a rescan matching zero files aborts too (issue #41 — but the server BOOT
  degrades to first-call guidance instead of dying pre-handshake, issue
  #240; only the explicit rescan path stays fatal), and so does a config
  without `state_dir` (issue #91: the silent `<root>/.neuronav` default
  reads and writes a store inside the scanned root — the live-store wipe
  door) — an explicit config is a contract, not a hint; no silent fallback
  that re-points the walk or the store.
- `.neuroignore` beside the active config (one dir name per line, `#`
  comments) extends `exclude_dirs` without touching the json (issue #36);
  `onboard.py init` scaffolds one pre-seeded with `.tmp`/`.team_scratch`.
- Consumers wire per-project MCP entries that pass `NEURONAV_CONFIG` in the
  server env (see `onboard.py wire`) — one install, many projects, zero install-side edits.
  `wire --omp` covers the omp harness class too (issue #130): the same stdio
  entry lands in `~/.omp/agent/mcp.json` under `neuronav-<project>`
  (`--omp-name` overrides; `NEURONAV_OMP_MCP` reroutes the file, kept out of
  tracked docs because the path is machine-local).
- Scratch/test dirs MUST be in `exclude_dirs` or they pollute the self-index
  dead-code tier (see `config/AGENTS.md`).
- Throwaway worktrees and test screenshots live in `.tmp/` (repo root,
  gitignored, on the self-index exclude list) — never the repo root or
  tracked dirs. See `config/AGENTS.md`.

## Conventions

Conventional commits, lowercase scope (`feat(viz):`, `fix(config):`,
`test:`, `docs:`). One atomic commit per verified increment. English.
Terse bodies explaining WHY, not WHAT.

- When starting work on an issue, immediately open a DRAFT PR linked to it ("Closes #N") and push the branch — visible ownership prevents duplicate grabs; flip to ready when the gate is green.
- Every posted artifact and every branch has a PAIR REVIEWER agent that grounds
  the worker: name the reviewer in the draft PR body at open time. The reviewer
  fact-checks issue/PR bodies before `gh` posts them and grounds each increment
  (claims vs artifacts: real runs, real numbers, real files) before push. Read-only
  research stays exempt until it promotes to an issue — then the reviewer gates
  the filing.
  Reviewer charter (owner directive, 2026-09): grounding is necessary,
  never sufficient — every gate also reviews engineering. KISS: needless
  abstraction, speculative generality, or a knob without a concrete
  failure mode is FIX REQUIRED even when every claim is grounded. DRY:
  N inline copies of one pattern demand the shared leaf (the `bake/`
  pure-job pattern is the promotion precedent). Monolith punishment:
  modules accumulating unrelated responsibilities get extracted along the
  existing seams (`layout.py`, `bake/*`, `recall.py` are the models).
  Exhaustive bug hunting: boundaries/off-by-one, empty/None paths,
  error-swallowing try/except and silent fallbacks (violates the
  loud-failures law), unordered iteration in byte-stability paths,
  unicode/encoding, Windows file locks and paths. Race conditions: any
  constructible failing schedule — cross-process `_db_lock`, boot thread
  vs the anyio stdio loop (the C-import deadlock class), watcher vs
  rescan, re-stamp TOCTOU, `os.replace` atomicity — is FIX REQUIRED with
  the schedule named. Grammar, wording, and prose-style nitpicks are out
  of scope: verdicts carry defects, not style.
Every change lands via pull request — main is protected: 1 approval +
  green "suites" CI required, enforce_admins OFF. Agent PRs are authored
  under the owner's token (authors cannot approve their own PRs), so the
  OWNER merges them with `gh pr merge N --admin` (or the UI's "merge
  without waiting"); non-admins can neither push to main nor merge
  without the owner's approval. The review function is the agent
  batteries + post-reviewer verdict comments. Fixes reference their issue
  ("Closes #N") and issues are closed with evidence at merge time, not
  left open. No direct pushes to main.
