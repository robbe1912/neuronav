# AGENTS.md — working ON neuronav

Local code-intelligence tool: vector recall (chroma + Ollama), call/signal
graph, clusters, dead-code tiers, 3D visualizer, stdio MCP server. Python 3.11,
stdlib-first; chromadb/httpx are the only heavy deps.

## Non-negotiable invariants

- **Determinism**: same DATA -> same layout byte-for-byte. `_layout` uses a
  seeded rng (1234); SWMG regression (`test_swmg_regression`) pins
  ~1606 files / ~6735 edges / dead ~90 (range floors — the SWMG target
  repo drifts; re-pin the floor on user-side refactors, never to mask
  extractor regressions). Never introduce unordered
  iteration into layout or export paths.
- **Gate before every commit** — all suites, 0 failures:
  `.venv/Scripts/python.exe -X utf8 tests/test_<name>.py`
- **Config leakage trap**: suites self-select config via internal
  `setdefault`. NEVER export `NEURONAV_CONFIG` in the shell before running
  them — the env var overrides and silently points suites at the wrong index.
- **Loud failures**: a failed offline layout aborts the build (`RuntimeError`);
  the browser re-validates baked DATA. No silent fallbacks.

## Architecture map

| module | role |
|---|---|
| `nav.py` | config resolution, chroma collection, embedding client (Ollama), rescan/import/export-base, CLI |
| `graph.py` | file/fn symbol graph, per-fn IO extraction, dead-code tiers |
| `extractors/` | per-language parsers behind a registry (`gdscript.py`, `python.py`, `model.py` dataclasses) |
| `clusters.py` | Louvain + labeler + crosstalk |
| `explore.py` | one-call orientation tool (codegraph-discipline: slices + flow + budget) |
| `server.py` | FastMCP stdio server; read-only tools carry `readOnlyHint`, `rescan` is the write tool |
| `viz.py` | Python `_build_data` + ONE embedded JS template string -> `graph.html` |

## viz.py working rules

- The template is a plain Python string with `__DATA__` replace — edit JS
  directly, but `graph.html` bakes the template at `generate()` time: **regen
  after every template edit** or you test stale JS (this has bitten us).
- `window.__dbg` is the harness contract: tests read `alphaTgt`, `bucketPosIB`,
  `hubCap`, `fnMeta`, `meta`, ... — extend it, never remove entries tests use.
- Module-scope ordering matters: the template executes top-to-bottom at boot
  (TDZ). Functions called at module scope must only reference symbols declared
  earlier.
- JSON writes from any helper script: UTF-8 WITHOUT BOM
  (`[IO.File]::WriteAllText`), PowerShell 5 `Set-Content -Encoding utf8`
  writes a BOM that kills `json.loads`.

## Test suites (all must stay green)

| suite | covers | needs |
|---|---|---|
| `test_strata` | depth layering, cycles, determinism (AST-extracts real functions) | stdlib + numpy |
| `test_crosslang` | self-index integration: parse + embed + fn search | chroma + Ollama (or `NEURONAV_EMBED_FAKE=1` for plumbing-only runs — what CI uses) |
| `test_pyhard` | python extractor edge cases | stdlib |
| `test_selfindex` | neuronav indexes itself | chroma (+ index) |
| `test_swmg_regression` | SWMG byte-stability | chroma, SWMG checkout, default config |
| `test_explore` | explore() behavior incl. degraded mode | chroma |
| `test_server_stdio` | MCP tool surface | mcp |
| `test_viz` | 20-assertion Playwright harness (real Chrome) | playwright + chrome |

Playwright harness gotchas: launch `channel="chrome"`; it serves `graph.html`
on port 8931 — orphaned python/chrome processes from killed runs hold the
port (`Get-NetTCPConnection -LocalPort 8931` -> kill PID). The harness is
config-agnostic: assertions data-gate on index content (dead files, cycles,
clusters) so self-index AND SWMG both run clean.

`test_strata` extracts functions from `viz.py`'s AST into a synthetic module —
if you add a module-level dependency to `_layout`/`_strata_*`, whitelist it in
the test's `load_viz_funcs`.

## Config profiles

- Default `config.json` (gitignored, machine-local) — the primary target repo.
- `config/<name>.json` — alternate profiles, selected via `NEURONAV_CONFIG`
  (absolute path). Relative `"root"` values resolve against the config file's
  directory.
- Consumers wire per-project MCP entries that pass `NEURONAV_CONFIG` in the
  server env (see `tools/wire-project.ps1`) — one install, many projects.

## Conventions

Conventional commits, lowercase scope (`feat(viz):`, `fix(config):`,
`test:`, `docs:`). One atomic commit per verified increment. English.
Terse bodies explaining WHY, not WHAT.
