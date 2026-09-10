# AGENTS.md — working ON neuronav

Local code-intelligence tool: vector recall (chroma + Ollama), call/signal
graph, clusters, dead-code tiers, 3D visualizer, stdio MCP server. Python 3.11,
stdlib-first; heavy deps: chromadb/httpx (vector store + embed transport), mcp
(stdio server), numpy/networkx/scipy/scikit-learn (the cluster/graph math the
read tools ride), plus the pinned C++ front-end pair tree-sitter==0.26.0 /
tree-sitter-cpp==0.23.4 (issue #13). Standalone repo — point it
at any project via config; nothing is vendored into target projects.

Per-directory docs: `extractors/AGENTS.md`, `tests/AGENTS.md`, `tools/AGENTS.md`,
`config/AGENTS.md`.

## Non-negotiable invariants

- **No target-repo data in tracked files** — the default config points at a
  private repo; NOTHING derived from it may be committed: no file/class/
  function names, no paths, no screenshots, no measured baselines (qa/ is
  gitignored machine-local state). Tests and probes must be config-agnostic
  (derive targets from the loaded index, see `test_viz`/`probe_scene_placement`/
  `test_server_stdio`); regression canaries live in the gitignored
  `config.json` under `regression_canaries`. Same rule for every future
  target (the engine profile included).
- **Determinism**: same DATA -> same layout byte-for-byte. `_layout` uses a
  seeded rng (1234); the regression suite over the target repo
  (`test_target_regression`) pins range floors — >=630 files, 6500–8100 edges,
  60–130 dead, plus liveness canaries (a known-dead func must stay dead,
  known-alive funcs must stay alive). The target repo drifts on the user side;
  re-pin the floor on user-side refactors, never to mask extractor regressions.
  Never introduce unordered iteration into layout or export paths.
- **Gate before every commit** — all suites, 0 failures:
  `.venv/Scripts/python.exe -X utf8 tests/test_<name>.py`
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
| `nav.py` | config resolution, chroma collection, embedding client (Ollama), rescan/import/export-base, CLI |
| `graph.py` | file/fn symbol graph, per-fn IO extraction, dead-code tiers |
| `extractors/` | per-language parsers behind a registry (`gdscript.py`, `python.py`, `model.py` dataclasses) |
| `clusters.py` | Louvain + labeler + crosstalk (imported lazily) |
| `explore.py` | one-call orientation tool (codegraph-discipline: slices + flow + budget) |
| `server.py` | FastMCP stdio server; read-only tools carry `readOnlyHint`, `rescan` is the write tool; read tools auto-rescan on worktree drift (stat gate, issue #19) |
| `viz.py` | Python `_build_data` + ONE embedded JS template string -> `graph.html` |
| `tools/` | dev gate + viewer: `qa_readability.py` (readability/declutter gate), `serve.py` (no-cache viewer), `wire-project.ps1` (per-project MCP wiring) |
| `config/` | named config profiles; `config.json` (root, gitignored) is the default |
| `vendor/three-0.160.0/` | vendored three.js core + 4 addons, embedded at build (see below) |
| `tests/` | 13 self-contained suites + committed fixtures (see `tests/AGENTS.md`) |
| `docs/map-spec-v2.md` | spec the named-wire map layer implements |

## viz.py template laws

The template is a plain Python string with `__DATA__` and `__IMPORTMAP__`
replaces — edit JS directly, but `graph.html` bakes the template at
`generate()` time: **regen after every template edit** or you test stale JS
(this has bitten us). Serve the bake via `python tools/serve.py`
(no-cache, 127.0.0.1:8791).

- `window.__dbg` is the harness contract: tests read `alphaTgt`, `bucketPosIB`,
  `hubCap`, `fns`, `meta`, `fnLod`, `busPts`, `corridorCensus`, ... — extend it,
  never remove entries tests use.
- Module-scope ordering matters: the template executes top-to-bottom at boot
  (TDZ). Functions called at module scope must only reference symbols declared
  earlier.
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

### Vendored three.js

`vendor/three-0.160.0/` pins the exact bytes (five files: `three.module.js`,
`controls/OrbitControls.js`, `lines/LineSegments2.js`,
`lines/LineSegmentsGeometry.js`, `lines/LineMaterial.js`) — content-addressed
by git, no runtime fetch. `_importmap()` embeds them as base64 `data:`
URIs at build time (CRLF->LF normalized for byte stability); addon relative
imports are rewritten to importmap keys because `data:` modules cannot
resolve relative specifiers. `graph.html` therefore boots offline with zero
network dependencies — keep it that way; never add a CDN reference.

## Test suites (all must stay green)

| suite | covers | needs |
|---|---|---|
| `test_strata` | depth layering, cycles, determinism (AST-extracts real functions) | stdlib + numpy |
| `test_crosslang` | self-index integration: parse + embed + fn search | chroma + Ollama (or `NEURONAV_EMBED_FAKE=1` for plumbing-only runs — what CI uses) |
| `test_pyhard` | python extractor edge cases | numpy + chromadb import only (hermetic fixture config) |
| `test_mwires` | named-wire map exports (`mwires`/`fns`/`meta` contract, map-spec-v2 §0) | chromadb import only (suite self-sets `NEURONAV_EMBED_FAKE=1`, hermetic fixture config) |
| `test_selfindex` | neuronav indexes itself | chromadb import (structural only) |
| `test_target_regression` | byte-stability over the target repo | chromadb import + the target repo configured in `config.json` |
| `test_explore` | explore() behavior incl. degraded mode | mcp + chroma + populated self-index |
| `test_server_stdio` | MCP tool surface end-to-end (JSON-RPC over stdio) | mcp + default-config target repo |
| `test_autorescan` | auto-rescan stat gate: freshness, TTL burst guard, failure cooldown, watcher (issue #19) | mcp + chromadb + numpy/networkx/scipy/scikit-learn (hermetic temp target, fake embeds) |
| `test_repomap` | repo_map budget/determinism/rank ordering on synthetic graphs | stdlib + numpy |
| `test_cpphard` | C++ extractor edge cases: macro surface, pairing, registration, dead tiers, determinism | tree-sitter wheels only (hermetic fixtures) |
| `test_recall` | hybrid recall: BM25F+vector fusion, degraded mode | chromadb import + self-index |
| `test_viz` | 90-check Playwright harness (real Chrome) | playwright + chrome + a fresh bake |

Playwright harness gotchas: launch `channel="chrome"`; it serves `graph.html`
on port 8931 — orphaned python/chrome processes from killed runs hold the
port (`Get-NetTCPConnection -LocalPort 8931` -> kill PID). The harness is
config-agnostic: assertions data-gate on index content (dead files, cycles,
clusters) so self-index AND the target repo both run clean.

`test_strata` extracts functions from `viz.py`'s AST into a synthetic module —
if you add a module-level dependency to `_layout`/`_strata_*`, whitelist it in
the test's `load_viz_funcs`.

Pin-update policy: the regression floors exist to catch extractor/graph
changes. Re-pin ONLY on user-side target drift, saying so explicitly; never
re-pin to make an extractor regression disappear.

Visualizer work also gates through `tools/qa_readability.py` (see
`tools/AGENTS.md`): `--declutter` baseline battery, `--after` ratchet mode,
`--affordance` for sanctioned hub-affordance ink.

## Config profiles

- Default `config.json` (gitignored, machine-local) - the primary target repo.
- Per-project state (issue #15): everything a profile generates (chroma
  store, base shards, `graph.html` bake) lives in `<root>/.neuronav/`;
  explicit `state_dir` overrides. No auto-migration — a root without
  `.neuronav` builds a fresh store on the next rescan (one-time re-embed).
- `config/<name>.json` — alternate profiles, selected via `NEURONAV_CONFIG`
  (absolute path). Relative `"root"` values resolve against the config file's
  directory.
- Consumers wire per-project MCP entries that pass `NEURONAV_CONFIG` in the
  server env (see `tools/wire-project.ps1`) — one install, many projects.
- Scratch/test dirs MUST be in `exclude_dirs` or they pollute the self-index
  dead-code tier (see `config/AGENTS.md`).

## Conventions

Conventional commits, lowercase scope (`feat(viz):`, `fix(config):`,
`test:`, `docs:`). One atomic commit per verified increment. English.
Terse bodies explaining WHY, not WHAT.

- Every change lands via pull request — main is protected with enforce_admins;
  the owner is the sole approving reviewer. Fixes reference their issue
  ("Closes #N") and issues are closed with evidence at merge time, not left
  open. No direct pushes, no admin bypasses.
