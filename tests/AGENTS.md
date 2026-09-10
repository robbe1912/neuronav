# AGENTS.md — tests/

Nine self-contained suites. Each is a standalone script — no pytest — run in
its own process:

```
.venv/Scripts/python.exe -X utf8 tests/test_<name>.py
```

Exit 0 = all pass. Each suite bootstraps `sys.path` to the repo root and
uses a local `check(name, cond)` helper (PASS/FAIL lines + failure count).
CI (`.github/workflows/ci.yml`) runs the three hermetic suites on ubuntu
with `NEURONAV_EMBED_FAKE=1`; the rest are local gates.

## Suites

| suite | covers | needs |
|---|---|---|
| `test_strata` | depth layering, cycles, `_layout` determinism | stdlib + numpy |
| `test_crosslang` | self-index integration: parse + embed + fn search over this repo | chromadb + Ollama (or `NEURONAV_EMBED_FAKE=1` — CI mode) |
| `test_pyhard` | python extractor edge cases on `fixtures/pyhard` | numpy + chromadb import only (hermetic fixture config) |
| `test_mwires` | named-wire map exports (`mwires`/`fns`/`meta`, map-spec-v2 §0) on `fixtures/mwires` | chromadb import only (self-sets `NEURONAV_EMBED_FAKE=1`, hermetic fixture config) |
| `test_selfindex` | self-index structural invariants: likely-dead zero, handlers stay review, deterministic rebuild | chromadb import (structural only) |
| `test_target_regression` | byte-stability over the target repo: floor pins + liveness canaries | chromadb import + the target repo configured in `config.json` |
| `test_explore` | explore() happy/degraded/no-hit paths + MCP tool annotations | mcp + chroma + populated self-index |
| `test_server_stdio` | MCP stdio end-to-end: spawns server.py, drives JSON-RPC, asserts the context tool answers | mcp + default-config target repo |
| `test_viz` | 90-check Playwright harness over the real baked page | playwright + chrome + a fresh `graph.html` bake |

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
- `test_server_stdio` strips `NEURONAV_CONFIG` from the child env so the
  server binds the default profile.
- `test_target_regression` / `test_viz` use the default `config.json` — the
  target repo must exist at its configured path.

## Playwright harness gotchas (`test_viz`)

- Launches real Chrome via `channel="chrome"` (no browser download).
- Serves the repo root on port **8931**. Orphaned python/chrome processes
  from killed runs hold the port: `Get-NetTCPConnection -LocalPort 8931`
  -> kill the PID, then rerun.
- Regenerate `graph.html` first — the harness tests the bake, not the
  template.
- Config-agnostic: assertions data-gate on index content, so the self-index
  profile and the default target profile both run clean.
- `test_strata` extracts `_links_adj`/`_tarjan_scc`/`_strata_depths`/
  `_strata_analysis`/`_layout` from `viz.py`'s AST into a synthetic module
  with a whitelisted global scope — new module-level dependencies of those
  functions must be whitelisted in `load_viz_funcs`.

## fixtures/

- `fixtures/pyhard/*.py` — per-feature liveness fixtures for the python
  extractor; each keeps a dead control func so the run is not vacuously
  all-alive.
- `fixtures/mwires/*.gd` + `*.tscn` — named-wire map contract fixtures
  (multi-script scenes discriminate signal resolution; unresolved
  connections counted).
- Suites copy nothing into the real index; generated configs go to the
  system temp dir.

## Pin-update policy

`test_target_regression` pins range floors (files, edges, dead) + canaries.
They exist to catch extractor/graph regressions. The target repo drifts on
the user side — re-pin floors only for that drift, saying so in the commit;
never re-pin to make a regression disappear. Same rule for count windows in
`test_selfindex` (handler window) and shape guards elsewhere.
