# neuronav
[![CI](https://github.com/robbe1912/neuronav/actions/workflows/ci.yml/badge.svg)](https://github.com/robbe1912/neuronav/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Local code-intelligence for AI coding agents: vector search + structural code
graph + an interactive 3D map, exposed as a stdio MCP server. Standalone repo -
point it at any project via config; nothing is vendored into target projects.

See [docs/comparison.md](docs/comparison.md) for how neuronav differs from
other code-graph tools (CodeGraph, aider repo map, SCIP).

## Tools (stdio MCP, 13)

| tool | use |
|---|---|
| `repo_map(budget_tokens)` | token-budget repo map, PageRank-ranked — the cheap orientation preamble |
| `semantic_search(query, n)` | find files by meaning ("rescan and index the repo" -> nav.py), RRF-fused with BM25F |
| `find_functions(query, n)` | same, per function with line numbers |
| `search_text(pattern, glob, files_only)` | regex text search — grep-class exact-string/literal queries; capped `file:line:text` rows (20 files / 3 lines) with truncation markers + totals |
| `symbol_graph(symbol, depth)` | callers/callees - refactoring safety |
| `explore(query, n, anchor)` | one-call orientation: Read-equivalent source slices + callers/callees flow; slices cap at a 100-line window ending in `pass anchor="path:start-end" to continue` — pass that anchor back to page the next window with zero re-orientation |
| `context(path, depth)` | per-file dossier: cluster, structural+semantic neighbors, hub rank, edge types |
| `clusters(k, min_sim)` | subsystem families from embedding geometry |
| `crosstalk()` | which subsystem clusters are wired together (cross-cluster coupling report) |
| `dead_code(n)` | unreachable-function candidates, tiered likely/review - candidates, never verdicts |
| `duplicates(n)` | exact-clone function bodies (dedup targets) |
| `visualize()` | generate interactive 3D graph.html (serve statically, open in browser) |
| `rescan()` | incremental re-index (vectors + functions + graph) — the explicit always-sync variant; read tools already auto-rescan on worktree drift |

All read-only tools carry `readOnlyHint`; `rescan` is the one mutating tool.

Every tool also takes an optional trailing `dir` (issue #131): empty
serves the boot config's repo; any other path routes that one call to
that checkout's index — one server entry covers every repo on the
machine. See [One entry, any repo](#one-entry-any-repo-universal-mount).

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

## Two-pass retrieval (optional, issue #74)

`semantic_search` normally retrieves once. Setting `"recall_two_pass": true`
in the config adds a deterministic second pass (RepoCoder-style iterative
retrieval, no LLM): the first pass's top hits donate their identifiers and
fn bodies — char-budgeted — to an augmented query that is re-embedded once
and RRF-fused with the pass-1 ranks. Hard embed budget: 2 calls per query.
Engaged hits carry `two_pass: true` (same marker convention as `degraded`);
when the vector side is down the feature stays out of the way and the
BM25F-only degraded contract is served unchanged. Default `false` — the
opt-in reflects the bench A/B (see the issue-74 PR; `bench/RESULTS.md`
`twopass` column).

## Prerequisites

- Python 3.11+ (venv)
- An embedding backend. Default: Ollama running locally —
  `ollama pull qwen3-embedding:0.6b`. Any OpenAI-compatible
  `/embeddings` endpoint works too (vLLM, LM Studio, llama.cpp server,
  Ollama's own `/v1` layer): point `embed_url` at it and, if it needs a
  key, set `NEURONAV_EMBED_KEY` (env beats the config's `embed_api_key`,
  so secrets stay out of tracked files). See `config/AGENTS.md`.
- `pip install chromadb httpx "mcp<2" numpy networkx scipy scikit-learn` (into the venv)

The default setup keeps embeddings on the machine; queries and indexing
both need the backend reachable.

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
(walk-everything defaults, extensions = every registered extractor suffix,
and a `"state_dir": "default"` opt-in — see below)
plus a `.neuroignore` beside it (one dir-name-per-line excludes, pre-seeded
with the `.tmp`/`.team_scratch` scratch conventions — edit freely, no json
surgery), appends `.neuronav/` to the project's `.gitignore`, indexes the
tree, bakes the map, and wires MCP entries (`.mcp.json` for Claude Code,
`opencode.json` when present) with `NEURONAV_CONFIG` pinned to the
project-local config.
**The neuronav install stays read-only** — nothing about a project is stored
inside it, so one install serves any number of projects and the package is
`npx`-shaped (run the tool against a repo, never edit the package).

Config discovery when you run `nav.py`/`server.py` yourself:
`NEURONAV_CONFIG` env → `<cwd>/.neuronav/config.json` (the project-local
one `onboard.py init` writes) → `config.json` next to `nav.py` *only when
cwd is the checkout* (machine-local default) → pure defaults (root = cwd,
walk everything). An explicit `NEURONAV_CONFIG` that points at a missing
file aborts at load, a rescan that matches zero files aborts too, and a
config without `state_dir` aborts the same way (issue #91: the silent
`<root>/.neuronav` default is a store inside the scanned root;
`"default"` is the opt-in, `onboard.py init` writes it) — no silent
fallback that quietly re-points the walk or the store, or purges the
previous profile's entries. Read tools auto-rescan on worktree drift, so an explicit
`rescan()` is only needed after big refactors. Upgrading from an install
whose state sat machine-local? Nothing moves automatically: the next rescan
builds a fresh `.neuronav/` store (one-time re-embed), or set `state_dir`
explicitly (`"default"` = `<root>/.neuronav`) to keep the old location.

Agent-facing guidance for consuming repos: `templates/agents-snippet.md`.

### Without MCP wiring

`python onboard.py init --index` writes the config + `.gitignore` entry and
indexes without touching MCP files. `--project <path>` targets another
directory from anywhere.

## One entry, any repo (universal mount)

Per-project wiring (above) stays the zero-config path, but a harness
that spans many repos can mount **one** server entry and pass the repo
per call — this replaces the per-project `mcpServers.neuronav` entries:

```json
{
  "mcpServers": {
    "neuronav": {
      "command": "E:/path/to/neuronav/.venv/Scripts/python.exe",
      "args": ["-X", "utf8", "E:/path/to/neuronav/server.py"],
      "env": {"NEURONAV_CONFIG": "E:/path/to/main-project/.neuronav/config.json"}
    }
  }
}
```

```jsonc
// then, per call — no per-project entries, no restart:
repo_map({"budget_tokens": 1024, "dir": "D:/work/game-a"})
semantic_search({"query": "save system", "dir": "D:/work/game-b"})
```

The boot repo (no `dir` passed) follows normal config discovery — the
`env` pin above just gives the mount a default; without one, a boot that
finds no repo aborts loudly at startup (zero-files contract).

Routing is stateless by design (issue #131: no activate/switch round
trip to forget): one call answers from exactly one repo and the next
call is unaffected. First contact with a fresh dir **onboards** it — the
same scaffold `onboard.py init` writes (`"state_dir": "default"`, the
#91 opt-in), then the full build (base shards first when tracked,
vectors + functions + graph) — and the call answers with the build
summary in `rescan()`'s format; a long build shows up as one long call,
exactly like a first `rescan()`. Indexes never cross: chroma clients,
write locks, cluster memos and the graph cache are all keyed per store.
Naming a dir is explicit consent to build there — it is not the #91
silent-store class (config-file-driven runs keep the loud abort). A
missing/unreadable dir is a loud MCP error naming the dir; a foreign
config without `state_dir` aborts the call the same way.

Auto-rescan and the watcher stay on the boot project only (the stat
fingerprint and failure cooldown are process-global); a routed repo
refreshes via an explicit `rescan({"dir": ...})`.

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
Hover = 1-hop greyout, focus mode with animated call direction, live
search with highlighted matches + click-to-focus on hubs and function
tiers, strata (height = call depth from entry points), cluster supernodes,
cycles and dead-code lenses, crosstalk corridors.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — conventional commits, the suite
gate, extractor rules. Every change lands via pull request.

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

- `NEURONAV_CONFIG points at '<path>', which does not exist` — deliberate
  abort, not a fallback: the explicit var is a contract. Unset it or point
  it at a real config json (`onboard.py init` writes one).
- `config '<path>' sets no "state_dir"` — deliberate abort (issue #91):
  the default would be `<root>/.neuronav`, a store inside the scanned
  root. Set `state_dir` to a path of your own, or `"default"` to opt
  into `<root>/.neuronav` (`onboard.py init` writes the opt-in).
- `rescan found 0 files under root=...` — the config matches nothing
  (typo'd `include_dirs`/`extensions`); fix the config instead of
  accepting an empty index.
