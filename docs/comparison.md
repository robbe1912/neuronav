# How neuronav differs from other code-graph tools

Factual positioning against the tools that occupy the same space. Claims about
neuronav are backed by artifacts in this repo (`bench/RESULTS.md`,
`tests/`); claims about other tools cite their public docs/readmes as of
2026-09, plus the independent field surveys ([wal.sh 2026 code-graph MCP
survey](https://wal.sh/research/2026-code-graph-mcp-survey/)). Corrections
welcome — this is a comparison, not an attack.

## The field

| tool | approach | retrieval | languages | human surface |
|---|---|---|---|---|
| **neuronav** | per-project local index: symbol graph + BM25F + vector embeddings, fused | hybrid (exact ∪ semantic ∪ graph) | GDScript, Python, C++, TypeScript, Rust (JS in flight, #277) | full 3D map |
| [CodeGraph](https://github.com/colbymchenry/codegraph) | Rust kernel, structural graph only (no vectors — SQLite FTS5 + graph resolution) | symbol/graph traversal | ~34 (Rust kernel parses ~20 natively) | `codegraph ui` (callers/src/callees) |
| [aider repo map](https://aider.chat/docs/repomap.html) | tree-sitter defs/refs + PageRank ranking | token-budgeted map for the LLM | 20+ (tree-sitter) | none (agent-facing text) |
| [Sourcegraph SCIP](https://github.com/sourcegraph/scip) | clangd/LSP-accurate indexers | precise xrefs (hosted or local CLI) | many, per-indexer | web UI |
| GitHub code nav | tree-sitter + stack-graphs, hosted | jump-to-def/xref in PR views | many | PR/blame gutter |
| [GitNexus](https://github.com/abhigyanpatwari/GitNexus) | Node graph + communities + flows, browser UI + CLI/MCP (PolyForm-NC license) | graph traversal + BM25 | 15 vendored tree-sitter grammars | browser 2D graph |
| [Serena](https://github.com/oraios/serena) | LSP-based MCP toolkit (symbols, references, project memories) | LSP xrefs (per-language servers) | whatever the LSP server speaks | none |
| [probe](https://github.com/probelabs/probe) | Rust structural search: ripgrep speed + tree-sitter AST chunking | lexical ∪ AST chunk match | many (tree-sitter) | none |
| [SeeRepo](https://arxiv.org/abs/2503.05163) (research) | repo snapshot graph for LLM context | `render_subgraph` on demand | Python | none |

## What neuronav does that the structural-only tools don't

1. **Hybrid recall.** Lexical (BM25F over filename/class/symbols/path/body,
   field-weighted) fused with vector similarity via RRF. Zero-model operation
   is available (`src=bm25`) when no embedding backend is reachable — the
   semantic layer is additive, not a hard dependency. Queries are embedded
   behind the nl2code task instruction (JCE model card, shipped default
   since #217): prefixing the embedded query only — store-compatible, no
   re-index, lexical side raw — lifted the same qwen3 store from
   .36/.76/.88 to .52/.88/.96 hit@1/5/10 with MRR .51→.67. Measured on the
   25-query golden set (`bench/RESULTS.md`), every attribution against the
   `both` (shipped-default) row of the current table, baseline = the
   vector-only `vec` row: hit@5 .60→.88, exact-name hit@5 .60→.90,
   MRR .53→.67; the graph-boosted and two-pass retrieves top the table at
   hit@5 .92 / MRR .75.
   Concept/prose queries
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
   chroma store + prebaked graph; `export_base`/`import_base` (the `nav.py
   export-base` / `import-base` CLI) let a team share embeddings so the
   model runs once, not per machine.
7. **Determinism as a contract.** Byte-stable bakes (regression-pinned);
   same DATA → same layout byte-for-byte.

## Where neuronav is not ahead (yet)
- **Language breadth** — 5 extractors (GDScript, Python, C++, TypeScript,
  Rust; JS in flight as #277) vs CodeGraph's ~34 / aider's tree-sitter
  set. The extractor registry keeps adding a language cheap (one parser +
  dead-tier hints), but each is hand-verified — the 2026 field surveys
  (wal.sh) show the failure mode of breadth without semantics: a grammar
  gap reads as confidently-ranked wrong answers, not low confidence.
- **Install friction** — the semantic half needs an embedding backend
  (default: local Ollama). Structural-only tools install in one command
  with zero services. FAKE/lexical modes keep CI and plumbing model-free.
- **Auto-sync** — CodeGraph watches files and updates the graph on save by
  default; neuronav self-heals at read time (every read tool stat-checks the
  worktree and runs a partial rescan on drift) with an opt-in background
  watcher (`watch_interval_s`). Remaining honest gap: no always-on watcher
  by default.
- **Scale evidence** — CodeGraph publishes huge-repo numbers (Linux kernel
  70k files <12 min; Swift 27k files ~100 s fresh). neuronav is validated
  in the 1k–13k-file range (self-index, SWMG 1.6k, the TS 4-repo matrix's
  12.8k-file leg) and has no 70k-file benchmark; the embed step is also
  ours alone — cold-index time on very large repos is unproven, and the
  map bake at that scale is untested.
- **Community/maturity** — ~9.6k★ (CodeGraph, 2026-09) and an npm
  distribution vs a young repo.

## The one-line version

> Structural graph tools give the agent a precise map; neuronav additionally
> gives it recall (fuzzy ∪ exact) and a map humans can actually read — at the
> cost of running an embedding model for the semantic half.
