# neuronav

Local code-intelligence for AI coding agents: vector search + structural code
graph + an interactive 3D map, exposed as a stdio MCP server. Standalone repo -
point it at any project via config; nothing is vendored into target projects.

## Tools (stdio MCP, 9)

| tool | use |
|---|---|
| `semantic_search(query, n)` | find files by meaning ("spell cooldown timer" -> magicsystem.gd) |
| `find_functions(query, n)` | same, per function with line numbers |
| `symbol_graph(symbol, depth)` | callers/callees - refactoring safety |
| `explore(query, n)` | one-call orientation: Read-equivalent source slices + callers/callees flow |
| `clusters(k, min_sim)` | subsystem families from embedding geometry |
| `dead_code(n)` | unreachable-function candidates, tiered likely/review - candidates, never verdicts |
| `duplicates(n)` | exact-clone function bodies (dedup targets) |
| `visualize()` | generate interactive 3D graph.html (serve statically, open in browser) |
| `rescan()` | incremental re-index (vectors + functions + graph) |

All read-only tools carry `readOnlyHint`; `rescan` is the one mutating tool.

## Prerequisites

- Python 3.11+ (venv)
- Ollama running locally with an embedding model: `ollama pull qwen3-embedding:0.6b`
- `pip install chromadb httpx "mcp<2"` (into the venv)

Embeddings never leave the machine. Queries need Ollama up; indexing needs it too.

## Install (standalone checkout)

```powershell
git clone <this repo>
cd neuronav
python -m venv .venv
.venv\Scripts\python.exe -m pip install chromadb httpx "mcp<2"
```

## Wire into a project

1. Write `config.json` next to `nav.py` (gitignored, machine-local):

```json
{ "root": "E:/path/to/project", "include_dirs": ["scripts", "scenes"] }
```

Defaults: root = parent of this folder. Extensions per extractors
(`.gd`/`.tscn`/`.py`/...); `NEURONAV_CONFIG` env selects an alternate profile
(e.g. `config/neuronav.json` indexes this repo itself).

2. Project-level MCP wiring (Claude Code `.mcp.json`, OpenCode `opencode.json`):

```json
{ "mcpServers": { "neuronav": {
    "command": "E:\\path\\to\\neuronav\\.venv\\Scripts\\python.exe",
    "args": ["-X", "utf8", "E:\\path\\to\\neuronav\\server.py"] } } }
```

3. Rescan once per checkout; again after big refactors. Per-checkout index
(`.chroma/`, gitignored) - each worktree reflects its own branch.

## Fast onboarding: base shards (skip the re-embed)

Export a project's trained index as gzipped shards (embeddings included,
~6 MB per 600 files) and track them in the project repo:

```powershell
nav.py export-base     # writes base/manifest.json + base/shard-*.jsonl.gz
nav.py import-base     # seeds an empty .chroma from shards; skips deleted files
```

`import-base` guards on model/dim; `rescan` heals to the current worktree.
Example consumer: SWMG's `.neuronav/wire-neuronav.ps1` (copies shards in,
imports, rescans, wires all client configs in one command).

## 3D visualizer

`viz.py generate` bakes a frozen deterministic layout + full graph data into a
single self-contained `graph.html`. Serve the folder with any static server and
open it - hover = 1-hop greyout, focus mode with animated call direction,
strata (height = call depth from entry points), cluster supernodes, cycles and
dead-code lenses, crosstalk corridors.

## Troubleshooting

- `Unexpected UTF-8 BOM` on config load - a tool wrote config.json with a BOM;
  rewrite it as UTF-8 without BOM.
- `base index model mismatch` - the tracked shards were exported with a
  different embedding model; re-export (`export-base`) or `ollama pull` the
  manifest's model.
- Empty results with Ollama down - embeddings backend unreachable; search
  degrades to lexical matching marked "degraded", or fails loudly during
  indexing. Start Ollama and `rescan`.
