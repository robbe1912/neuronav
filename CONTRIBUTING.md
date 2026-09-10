# Contributing to neuronav

Local-first code intelligence: vector recall + call graph + 3D map as an MCP
server. MIT. Python 3.11+, stdlib-first.

## Dev setup

```powershell
git clone https://github.com/robbe1912/neuronav
cd neuronav
python -m venv .venv
.venv\Scripts\python.exe -m pip install chromadb httpx "mcp<2" numpy playwright networkx scipy scikit-learn
ollama pull qwen3-embedding:0.6b          # embedding backend, runs locally
```

Windows is the primary dev platform; `test_strata` + `test_pyhard` are
hermetic (numpy + chromadb import only), `test_crosslang` is a self-index
integration suite (needs Ollama up).

## Layout

| module | role |
|---|---|
| `nav.py` | config, chroma, embeddings, rescan, base-index import/export |
| `graph.py` | symbol graph, per-fn IO, dead-code tiers |
| `extractors/` | language registry (add a language = new module + registry entry) |
| `clusters.py` | Louvain communities, labels, crosstalk |
| `explore.py` | one-call agent orientation tool |
| `onboard.py` | one-command project onboarding (issue #27): init/wire — install stays read-only, cross-platform |
| `server.py` | FastMCP stdio server (12 tools) |
| `viz.py` | optional add-on: data build + embedded three.js template -> `graph.html` |

## Ground rules

1. **Determinism is a contract.** Layout, exports, clusters: seeded and
   ordered. The external-target regression suite pins exact counts; if your change
   legitimately shifts them, say so explicitly in the PR and update the pins
   with justification.
2. **All suites green before commit.** Minimum bar for any change:
   `test_strata`, `test_crosslang`, `test_pyhard`. Touching nav/graph/index:
   add `test_selfindex`, `test_target_regression`, `test_explore`,
   `test_server_stdio`. Touching `viz.py`: regenerate + full Playwright
   harness (`test_viz`) on BOTH the self-index and the external-target profile.
3. **No silent fallbacks.** Missing backend -> loud error or an explicitly
   marked degraded mode (see `explore.py` lexical fallback). Never swallow.
4. **Read-only MCP tools carry `readOnlyHint`**; mutating behavior goes in
   `rescan` and nowhere else.
5. **Extractors are pure**: no chroma, no network, no filesystem beyond the
   file being parsed. Fixture-driven (`tests/fixtures/`).

## Adding a language extractor

1. `extractors/<lang>.py` following `gdscript.py`/`python.py`: parse file ->
   `FileSym`/`Func` records (`extractors/model.py`).
2. Register in `extractors/__init__.py` (extension -> module).
3. Fixtures under `tests/fixtures/<lang>/` + extend `test_crosslang`.
4. If the language has signals/instancing, map them to the `ty` edge kinds
   (`call`/`signal`/`inst`/`attach`/`var`) instead of inventing new ones.

## Base index shards

`nav.py export-base` writes `base/` (gitignored here — shards belong in the
CONSUMING repo, e.g. the consuming repo's `.neuronav/base/`). `import-base` seeds an empty
chroma from shards and guards on embedding model + dim.

## Commit / PR style

Conventional commits (`feat(scope):`, `fix:`, `test:`, `docs:`), one verified
increment per commit, terse bodies that explain why. PRs: what changed, which
suites ran, evidence (output tails or screenshots for visual changes).
