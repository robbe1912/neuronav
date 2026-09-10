# neuronav

Local code-intelligence for AI coding agents: vector search + structural code
graph + an interactive 3D map, exposed as a stdio MCP server. Standalone repo -
point it at any project via config; nothing is vendored into target projects.

See [docs/comparison.md](docs/comparison.md) for how neuronav differs from
other code-graph tools (CodeGraph, aider repo map, SCIP).

## Tools (stdio MCP, 12)

| tool | use |
|---|---|
| `repo_map(budget_tokens)` | token-budget repo map, PageRank-ranked — the cheap orientation preamble |
| `semantic_search(query, n)` | find files by meaning ("rescan and index the repo" -> nav.py), RRF-fused with BM25F |
| `find_functions(query, n)` | same, per function with line numbers |
| `symbol_graph(symbol, depth)` | callers/callees - refactoring safety |
| `explore(query, n)` | one-call orientation: Read-equivalent source slices + callers/callees flow |
| `context(path, depth)` | per-file dossier: cluster, structural+semantic neighbors, hub rank, edge types |
| `clusters(k, min_sim)` | subsystem families from embedding geometry |
| `crosstalk()` | which subsystem clusters are wired together (cross-cluster coupling report) |
| `dead_code(n)` | unreachable-function candidates, tiered likely/review - candidates, never verdicts |
| `duplicates(n)` | exact-clone function bodies (dedup targets) |
| `visualize()` | generate interactive 3D graph.html (serve statically, open in browser) |
| `rescan()` | incremental re-index (vectors + functions + graph) — the explicit always-sync variant; read tools already auto-rescan on worktree drift |

All read-only tools carry `readOnlyHint`; `rescan` is the one mutating tool.

## Automatic freshness (auto-rescan)

Every read tool first stats the worktree (mtime/size walk over the configured
includes, TTL-cached ~3s so bursts don't re-walk) and, when it drifted from
the last synced state, runs the sha-gated incremental rescan before
answering — external edits show up in the next tool call with no manual
`rescan()`. Embedding failures degrade loudly: one stderr warning, a 60s
retry cooldown, and the tool answers from the current index. To index even
without tool traffic, set `"watch_interval_s": 0.5` (seconds; absent/0 = off)
in the config: a stdlib daemon thread then polls the same stat gate and
rescans after a ~2s quiet debounce.

## Prerequisites

- Python 3.11+ (venv)
- Ollama running locally with an embedding model: `ollama pull qwen3-embedding:0.6b`
- `pip install chromadb httpx "mcp<2" numpy networkx scipy scikit-learn` (into the venv)

Embeddings never leave the machine. Queries need Ollama up; indexing needs it too.

## Install (standalone checkout)

```powershell
git clone <this repo>
cd neuronav
python -m venv .venv
.venv\Scripts\python.exe -m pip install chromadb httpx "mcp<2" numpy networkx scipy scikit-learn
```

## Wire into a project (one command, any OS)

From inside the target project:

```bash
python /path/to/neuronav/onboard.py wire --index
```

That's the whole setup: it writes `<project>/.neuronav/config.json`
(walk-everything defaults, extensions = every registered extractor suffix),
appends `.neuronav/` to the project's `.gitignore`, indexes the tree, bakes
the map, and wires MCP entries (`.mcp.json` for Claude Code, `opencode.json`
when present) with `NEURONAV_CONFIG` pinned to the project-local config.
**The neuronav install stays read-only** — nothing about a project is stored
inside it, so one install serves any number of projects and the package is
`npx`-shaped (run the tool against a repo, never edit the package).

Config discovery when you run `nav.py`/`server.py` yourself:
`NEURONAV_CONFIG` env → `<cwd>/.neuronav/config.json` (the project-local
one `onboard.py init` writes) → `config.json` next to `nav.py` *only when
cwd is the checkout* (machine-local default) → pure defaults (root = cwd,
walk everything). Read tools auto-rescan on worktree drift, so an explicit
`rescan()` is only needed after big refactors. Upgrading from an install
whose state sat machine-local? Nothing moves automatically: the next rescan
builds a fresh `.neuronav/` store (one-time re-embed), or set `state_dir`
explicitly to keep the old location.

Agent-facing guidance for consuming repos: `templates/agents-snippet.md`.

### Without MCP wiring

`python onboard.py init --index` writes the config + `.gitignore` entry and
indexes without touching MCP files. `--project <path>` targets another
directory from anywhere.

## Fast onboarding: base shards (skip the re-embed)

Export a project's trained index as gzipped shards (embeddings included,
~6 MB per 600 files) and track them in the project repo:

```bash
nav.py export-base     # writes <state_dir>/base/{manifest.json,shard-*.jsonl.gz}
nav.py import-base     # seeds the project's empty store from shards; skips deleted files
```

`import-base` guards on model/dim; `rescan` heals to the current worktree.
Shards travel with the project: export from one checkout's `.neuronav/base/`,
track them in the project repo, and teammates seed straight from there
(state is project-local — no install-side copies).

## 3D visualizer (optional add-on)

The **core** is the index (chroma) + hybrid recall (BM25F + vector + RRF)
+ graph + MCP server. The visualizer is an add-on that ships enabled:
`viz.py` bakes a frozen deterministic layout + full graph data into a single
self-contained `graph.html` (`tools/serve.py` serves it, `tools/qa_readability.py`
gates it). Removing `viz.py` + `vendor/` strips it cleanly — `onboard.py --index`
skips the bake with a note, the MCP `visualize()` tool answers with a
pointer instead of a bake, and every other tool keeps working.
Hover = 1-hop greyout, focus mode with animated call direction, strata
(height = call depth from entry points), cluster supernodes, cycles and
dead-code lenses, crosstalk corridors.

## Troubleshooting

- `Unexpected UTF-8 BOM` on config load - a tool wrote config.json with a BOM;
  rewrite it as UTF-8 without BOM.
- `base index model mismatch` - the tracked shards were exported with a
  different embedding model; re-export (`export-base`) or `ollama pull` the
  manifest's model.
- `NEURONAV_EMBED_FAKE=1` — CI/plumbing mode: deterministic hash embeddings,
  no Ollama needed. Exercises upsert/query/scoping for real; NOT semantic.
  Never mix with a real collection you care about (same collection gets
  fake vectors upserted).
- Empty results with Ollama down - embeddings backend unreachable; search
  degrades to lexical matching marked "degraded", or fails loudly during
  indexing. Start Ollama and `rescan`.
- `neuronav: auto-rescan FAILED (...)` on stderr - the background
  freshness rescan could not run (embedding backend down); read tools keep
  answering from the current index and retry is suppressed for 60s. Start
  Ollama; the gate recovers by itself or via an explicit `rescan()`.
