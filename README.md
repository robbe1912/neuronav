# neuronav

Local code-intelligence for Godot projects: vector search + structural code
graph + interactive 3D map, exposed to AI agents as an MCP server.
Name it anything via config; folder convention `.neuronav/` or whatever the
bootstrap wires.

## What agents get (stdio MCP, 8 tools)

| tool | use |
|---|---|
| `semantic_search(query, n)` | find files by meaning ("spell cooldown timer" → magicsystem.gd) |
| `find_functions(query, n)` | same, per function with line numbers |
| `symbol_graph(symbol, depth)` | callers/callees — refactoring safety |
| `clusters(k, min_sim)` | subsystem families from embedding geometry |
| `dead_code(n)` | unreachable-function candidates, tiered likely/review |
| `duplicates(n)` | exact-clone function bodies (dedup targets) |
| `visualize()` | generate interactive 3D graph.html |
| `rescan()` | incremental re-index (vectors + functions + graph) |

## Install (per checkout)

```powershell
# 1. one-time: Ollama with an embedding model
ollama pull qwen3-embedding:0.6b

# 2. per checkout
powershell -ExecutionPolicy ByPass -File .neuronav\bootstrap.ps1
```

Bootstrap: creates `.venv` (chromadb, httpx, mcp<2), builds/refreshes the
index, wires OpenCode / Claude Code / VS Code / Codex configs, and registers
`agents.md` as agent instructions.

## Config

`config.json` next to the code (gitignored — machine-local):

```json
{ "root": "E:/path/to/project", "include_dirs": ["scripts", "scenes"] }
```

Defaults: root = parent of this folder, include_dirs = scripts/scenes/VFX/ai/tests/tools.
Embedding endpoint/model overridable in `nav.py` constants.

## Notes

- Per-checkout index: each worktree reflects its own branch.
- Dead code is *candidates*, never verdicts — read files before deleting.
- 3D map: click nodes, search by path/class, cluster chips isolate, dead-code toggle.
