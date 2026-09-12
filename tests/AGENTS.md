# AGENTS.md — tests/

Seventeen self-contained suites. Each is a standalone script — no pytest — run in
its own process:

```
.venv/Scripts/python.exe -X utf8 tests/test_<name>.py
```

Exit 0 = all pass. Each suite bootstraps `sys.path` to the repo root and
uses a local `check(name, cond)` helper (PASS/FAIL lines + failure count).
CI (`.github/workflows/ci.yml`) runs the seven hermetic suites on ubuntu
with `NEURONAV_EMBED_FAKE=1` (`test_strata`, `test_crosslang`,
`test_pyhard`, `test_cpphard`, `test_autorescan`, `test_searchtext`,
`test_project_mode`);
the rest are local gates.

## Suites

| suite | covers | needs |
|---|---|---|
| `test_strata` | depth layering, cycles, `_layout` determinism | stdlib + numpy |
| `test_crosslang` | self-index integration: parse + embed + fn search over this repo | chromadb + Ollama (or `NEURONAV_EMBED_FAKE=1` — CI mode) |
| `test_pyhard` | python extractor edge cases on `fixtures/pyhard` | numpy + chromadb import only (hermetic fixture config) |
| `test_cpphard` | C++ extractor edge cases on `fixtures/cpp` (issue #13): macro surface, .h/.cpp pairing, registration harvest, dead tiers, determinism | tree-sitter + tree-sitter-cpp import only (hermetic fixture config) |
| `test_selfindex` | self-index structural invariants: likely-dead zero, handlers stay review, deterministic rebuild | chromadb import (structural only) |
| `test_target_regression` | byte-stability over the target repo: floor pins + liveness canaries | chromadb import + the target repo configured in `config.json` |
| `test_explore` | explore() happy/degraded/no-hit paths, windowed slices + anchor paging (issue #69) + MCP tool annotations | mcp + chroma + populated self-index |
| `test_server_stdio` | MCP stdio end-to-end: spawns server.py, drives JSON-RPC, asserts the context tool answers | mcp + default-config target repo |
| `test_autorescan` | auto-rescan stat gate (issue #19): read-tool freshness, TTL burst guard, embed-failure cooldown, `watch_interval_s` watcher — in-process pins + two stdio e2e servers | mcp + chromadb + numpy/networkx/scipy/scikit-learn (hermetic temp target + `NEURONAV_EMBED_FAKE=1`) |
| `test_searchtext` | capped `search_text` tool (issue #68): file:line:row shape, deterministic order, 20-file/3-line caps with markers + totals, `files_only`, glob, graceful regex errors | mcp + chromadb (hermetic temp config, `NEURONAV_EMBED_FAKE=1`) |
| `test_repomap` | repo_map budget bound, byte determinism, rank ordering, god-hub saturation on synthetic graphs | stdlib + numpy (no index, no embeddings) |
| `test_mwires` | named-wire map exports (`mwires`/`fns`/`meta` contract, map-spec-v2 §0) | chromadb import only (self-sets `NEURONAV_EMBED_FAKE=1`) |
| `test_recall` | hybrid recall fusion, ctx hops, degraded mode (real+fake modes) | chromadb import; exact-rank pins need a real-embedded store |
| `test_embedprov` | embed provider contract (issue #17): provider select/auto-detect, ollama+openai wire adapters, env-vs-config key precedence, keyless no-header, 401 loud, 429 retry, batch chunking, no-pad mismatches, fake-mode isolation | stdlib http.server stub on an ephemeral loopback port + chromadb import |
| `test_project_mode` | onboarding (issue #27): discovery precedence env > project-local > checkout, `onboard.init`/`wire` scaffolds incl. `.neuroignore` (issue #36), viz add-on degrade | stdlib + chromadb import (fresh subprocesses, fake embeds) |
| `test_viz` | 103-check Playwright harness over the real baked page | playwright + chrome + a fresh `graph.html` bake |
| `test_verifier` | Kythe-style verifier fixtures (issue #66): `//-`-shaped goal comments inlined in fixture sources, asserted against extractor output (FileSym + cpp scan_calls); `@fn dead` is corpus-local liveness | stdlib + tree-sitter/tree-sitter-cpp for the C++ goals — extractor-level only: no config, no index, no chroma |

`_page_harness.py` (issue #86 strand R9) is the shared Playwright harness
the two browser suites ride: `serve()` (no-cache loopback server, ephemeral
port per issue #132; `reuse=` preserves the suites' historic bind
difference), `launch()` (real Chrome), `open_page()` (settled 1600x900 page
with optional console/pageerror capture), `probe_dbg()` (broken-bake probe),
and the `CheckLog` accumulator with the suites' summary/exit contract.
Consumed by `tests/test_viz.py` (sys.path) and `tools/qa_readability.py`
(`tests._page_harness`) — the suites keep their own assertions; harness
changes may never weaken or drop a check.

`probe_scene_placement.py` is a manual probe script, not a suite.

## Config self-selection (the leakage trap)

Suites pick their own config; the shell must not pre-export one:

- `test_crosslang` / `test_selfindex` hard-set
  `NEURONAV_CONFIG=<repo>/config/neuronav.json` (self-index profile).
- `test_explore` uses `os.environ.setdefault` — an exported var WINS, which
  is exactly why exporting `NEURONAV_CONFIG` in your shell before running
  suites silently points them at the wrong index. Never export it.
- `test_pyhard` / `test_mwires` write a generated temp config under the
  system temp dir pointing at `tests/fixtures/<name>` only — they never touch
  the real index.
- `test_verifier` never touches config at all — it calls extractor
  `parse()` directly, so an exported `NEURONAV_CONFIG` is simply unseen
  (the one suite an exported var cannot leak into).
- `test_autorescan` generates its own temp TARGET TREE + config under the
  system temp dir (separate state dirs for the in-process and e2e-server
  sections) and self-sets `NEURONAV_EMBED_FAKE=1` — never run it against a
  real profile.
- `test_project_mode` builds throwaway project trees under the system
  temp dir and drives init/wire/discovery in fresh subprocesses with
  fake embeds — never touches a real profile.
- `test_server_stdio` strips `NEURONAV_CONFIG` from the child env so the
  server binds the default profile.
- `test_embedprov` writes a generated temp config under the system
  temp dir, points `embed_url` at its own loopback stub (ephemeral
  port), and manages `NEURONAV_EMBED_FAKE`/`NEURONAV_EMBED_KEY`
  itself — it never touches a real profile or model server.
- `test_target_regression` / `test_viz` use the default `config.json` — the
  target repo must exist at its configured path.

## Playwright harness gotchas (`test_viz`)

- Launches real Chrome via `channel="chrome"` (no browser download).
- Serves the repo root on an **ephemeral loopback port** (issue #132): viz
  gates may run concurrently — no fixed-port claims, no orphaned-process
  holds, no TIME_WAIT rerun failures. (`tools/serve.py` keeps its explicit
  port; `test_project_mode` still pins 9081-9090 to exercise serve.py's
  port-refusal contract.)
- Regenerate `graph.html` first — the harness tests the bake, not the
  template.
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
