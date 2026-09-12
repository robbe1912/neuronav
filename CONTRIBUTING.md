# Contributing to neuronav

Local-first code intelligence: vector recall + call graph + 3D map as an MCP
server. MIT. Python 3.11+, stdlib-first.

## Dev setup

```powershell
git clone https://github.com/robbe1912/neuronav
cd neuronav
python -m venv .venv
.venv\Scripts\python.exe -m pip install chromadb httpx "mcp<2" numpy playwright networkx scipy scikit-learn "tree-sitter==0.26.0" "tree-sitter-cpp==0.23.4"
ollama pull qwen3-embedding:0.6b          # default embedding backend; any OpenAI-compatible /embeddings endpoint also works (config/AGENTS.md)
```

Windows is the primary dev platform. CI is the gate: the six hermetic
suites (`test_strata`, `test_crosslang`, `test_pyhard`, `test_cpphard`,
`test_autorescan`, `test_project_mode`) run on ubuntu with
`NEURONAV_EMBED_FAKE=1`; the rest (real embeds, Playwright, the external
target repo) are local gates.

## Layout

| module | role |
|---|---|
| `nav.py` | config, chroma, embeddings, rescan, base-index import/export |
| `graph.py` | symbol graph, per-fn IO, dead-code tiers |
| `extractors/` | language registry — gdscript, python, C++ (tree-sitter-cpp); add a language = new module + registry entry |
| `clusters.py` | Louvain communities, labels, crosstalk |
| `explore.py` | one-call agent orientation tool |
| `onboard.py` | one-command project onboarding (issue #27): init/wire — install stays read-only, cross-platform |
| `server.py` | FastMCP stdio server (12 tools) |
| `viz.py` | optional add-on: data build + embedded three.js template -> `graph.html` |
| `layout.py` | pure strata/layout math for the viz bake (stdlib + numpy only) |
| `tools/` | dev gate (`qa_readability.py`) + no-cache bake viewer (`serve.py`) |
| `bench/` | recall benchmark: golden set + `run_bench.py`, results committed in `bench/RESULTS.md` |

## Ground rules

1. **Determinism is a contract.** Layout, exports, clusters: seeded and
   ordered. The external-target regression suite pins exact counts; if your change
   legitimately shifts them, say so explicitly in the PR and update the pins
   with justification.
2. **All suites green before commit.** Minimum bar for any change is the
   CI set: `test_strata`, `test_crosslang`, `test_pyhard`, `test_cpphard`,
   `test_autorescan`, `test_project_mode`. Touching nav/graph/index:
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

1. `extractors/<lang>.py` — follow `python.py` (stdlib AST) or `cpp.py`
   (tree-sitter front-end + regex macro pass): parse file ->
   `FileSym`/`Func` records (`extractors/model.py`).
2. Register in `extractors/__init__.py` (extension -> module).
3. Fixtures under `tests/fixtures/<lang>/` + a hardening suite
   (`test_pyhard`/`test_cpphard` pattern) + extend `test_crosslang`.
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
