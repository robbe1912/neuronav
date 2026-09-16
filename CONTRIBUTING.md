# Contributing to neuronav

Local-first code intelligence: vector recall + call graph + 3D map as an MCP
server. MIT. Python 3.12+, stdlib-first.

## Dev setup

```bash
git clone https://github.com/robbe1912/neuronav
cd neuronav
uv venv && uv pip install -e .   # Python >= 3.12; the committed ==-pin set
uv pip install playwright        # only for local test_viz runs
ollama pull qwen3-embedding:0.6b          # default embedding backend; any OpenAI-compatible /embeddings endpoint also works (config/AGENTS.md)
```

Windows is the primary dev platform. CI is the gate: twenty hermetic
suites (`test_strata`, `test_crosslang`, `test_pyhard`, `test_cpphard`,
`test_autorescan`, `test_server_stdio`, `test_searchtext`,
`test_project_mode`, `test_baseindex`, `test_mwires`, `test_clusterinv`,
`test_recall`, `test_embedprov`, `test_repomap`, `test_selfindex`,
`test_explore`, `test_verifier`, `test_bench`, `test_bakeint`,
`test_portability`) run on ubuntu with `NEURONAV_EMBED_FAKE=1`, plus a
`viz` job that builds the frozen synthetic corpus
(`tests/vizcorpus_build.py`) and runs the full Playwright harness
(`test_viz`) against it. The rest (real embeds, the external target repo)
are local gates; `test_viz` also runs locally on the self-index or any
scratch store via `NEURONAV_CONFIG`.

## Layout

| module | role |
|---|---|
| `nav.py` | config, chroma, embeddings, rescan, base-index import/export |
| `graph.py` | symbol graph, per-fn IO, dead-code tiers |
| `extractors/` | language registry — gdscript, python, C++ (tree-sitter-cpp); add a language = new module + registry entry |
| `clusters.py` | Louvain communities, labels, crosstalk |
| `explore.py` | one-call agent orientation tool |
| `onboard.py` | one-command project onboarding (issue #27): init/wire — install stays read-only, cross-platform |
| `server.py` | FastMCP stdio server (14 tools) |
| `viz.py` | optional add-on: data build + embedded three.js template -> `graph.html` |
| `layout.py` | pure strata/layout math for the viz bake (stdlib + numpy only) |
| `bake/` | pure per-job transforms for the viz DATA pipeline (g/clusters in, DATA rows out) |
| `tools/` | dev gates: `qa_readability.py` (readability/declutter gate) + headless-dev-only no-cache HTTP for the bake (`serve.py`; production opens `.neuronav/graph.html` directly via file:// — issue #133) |
| `bench/` | recall benchmark: golden set + `run_bench.py`, results committed in `bench/RESULTS.md` |

## Ground rules

2. **All suites green before commit.** Minimum bar for any change is the
   CI set (see above). Touching nav/graph/index: add `test_selfindex`,
   `test_target_regression`, `test_explore`, `test_server_stdio`.
   Touching `viz.py`: regenerate + full Playwright harness (`test_viz`)
   on the frozen corpus AND the self-index —
   `NEURONAV_CONFIG=<scratch>/config.json NEURONAV_EMBED_FAKE=1 python
   -X utf8 tests/test_viz.py` (build the corpus first with
   `tests/vizcorpus_build.py --dest <scratch>`).
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
