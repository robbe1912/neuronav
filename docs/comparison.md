# How neuronav differs from other code-graph tools

Factual positioning against the tools that occupy the same space. Claims about
neuronav are backed by artifacts in this repo (`bench/RESULTS.md`,
`tests/`); claims about other tools cite their public docs/readmes as of
2026-09. Corrections welcome — this is a comparison, not an attack.

## The field

| tool | approach | retrieval | languages | human surface |
|---|---|---|---|---|
| **neuronav** | per-project local index: symbol graph + BM25F + vector embeddings, fused | hybrid (exact ∪ semantic ∪ graph) | GDScript, Python, C++ | full 3D map |
| [CodeGraph](https://github.com/colbymchenry/codegraph) | Rust kernel, structural graph only | symbol/graph traversal | 17 | `codegraph ui` (callers/src/callees) |
| [aider repo map](https://aider.chat/docs/repomap.html) | tree-sitter defs/refs + PageRank ranking | token-budgeted map for the LLM | 20+ (tree-sitter) | none (agent-facing text) |
| [Sourcegraph SCIP](https://github.com/sourcegraph/scip) | clangd/LSP-accurate indexers | precise xrefs (hosted or local CLI) | many, per-indexer | web UI |
| GitHub code nav | tree-sitter + stack-graphs, hosted | jump-to-def/xref in PR views | many | PR/blame gutter |
| [SeeRepo](https://arxiv.org/abs/2503.05163) (research) | repo snapshot graph for LLM context | `render_subgraph` on demand | Python | none |

## What neuronav does that the structural-only tools don't

1. **Hybrid recall.** Lexical (BM25F over filename/class/symbols/path/body,
   field-weighted) fused with vector similarity via RRF. Zero-model operation
   is available (`src=bm25`) when no embedding backend is reachable — the
   semantic layer is additive, not a hard dependency. Measured on the
   25-query golden set (`bench/RESULTS.md`): hit@1 .24→.40, exact-name
   hit@5 .50→.80, MRR .470→.595 over vector-only. Concept/prose queries
   ("where is X applied") are the query class pure-symbol tools answer only
   if the agent guesses the right noun.
2. **Engine-native semantics.** GDScript/`.tscn` scene instancing edges,
   signal connections, autoload entry rules; for Godot's C++: `ClassDB::`
   registration binds, `GDVIRTUAL` surface, `ADD_SIGNAL`/`ADD_PROPERTY` —
   engine-to-code edges a generic indexer sees as opaque calls.
3. **Dead-code tiers.** Reachability analysis with language-aware entry
   rules (engine registration, virtuals, tests), not just "unreferenced".
4. **Cluster geometry.** Louvain communities over the *embedding* similarity
   graph + structural crosstalk — subsystem discovery, not only file-tree
   folders.
5. **Human surface.** A 3D map (corridor/trunk/junction visual language,
   perceptual anchoring laws, regression-pinned by Playwright harness) —
   the other local tools are agent-only or offer a flat caller/callee list.
6. **Per-project state with base shards.** `<project>/.neuronav/` holds the
   chroma store + prebaked graph; `WithBaseShards` lets a team share
   embeddings so the model runs once, not per machine.
7. **Determinism as a contract.** Byte-stable bakes (regression-pinned);
   same DATA → same layout byte-for-byte.

## Where neuronav is not ahead (yet)

- **Language breadth** — 3 extractors vs CodeGraph's 17 / aider's tree-sitter
  set. The extractor registry keeps adding a language cheap (one parser +
  dead-tier hints), but each is hand-verified.
- **Install friction** — the semantic half needs an embedding backend
  (default: local Ollama). Structural-only tools install in one command
  with zero services. FAKE/lexical modes keep CI and plumbing model-free.
- **Auto-sync** — CodeGraph watches files and updates the graph on save by
  default; neuronav self-heals at read time (every read tool stat-checks the
  worktree and runs a partial rescan on drift) with an opt-in background
  watcher (`watch_interval_s`). Remaining honest gap: no always-on watcher
  by default.
- **Community/maturity** — 70k★ and an npm distribution vs a young repo.

## The one-line version

> Structural graph tools give the agent a precise map; neuronav additionally
> gives it recall (fuzzy ∪ exact) and a map humans can actually read — at the
> cost of running an embedding model for the semantic half.
