# neuronav (swmg-nav) — codebase brain: semantic search + structure graph + 3D map

Use **before grep** for "where is X" questions — finds .gd/.tscn files by meaning, not keywords.

- `semantic_search(query, n=8)`: example `"spell cooldown timer"` → `scripts/magic/magicsystem.gd`. Covers scripts/, scenes/, VFX/, ai/, tests/, tools/ (whole-file granularity, this checkout's branch).
- `find_functions(query)`: same but per-FUNCTION, with line numbers. Use when you need the exact function, not the file.
- Workflow: `semantic_search("concept")` → top hits → Read them. Beats grep when file/symbol names don't match your words.
- `symbol_graph(symbol, depth)`: who calls / what is called — check refactoring safety, trace call chains.
- `clusters(k, min_sim)`: subsystem families from embedding geometry (inventory, dungeon gen, VFX elements…). Survey unfamiliar areas fast.
- `dead_code(n)` / `duplicates(n)`: deletion + dedup candidates. `dead_code` is tiered — "likely" needs one manual check, "review" uses dynamic dispatch. NEVER delete without reading the file and running tests.
- `visualize()`: interactive 3D neuron-map of the repo (clusters/dead-code coloring, search, click-to-inspect). Returns an absolute path — open in browser.
- Freshness: index auto-rescans at session start (<1s when unchanged; imports tracked base shards on first run). After adding/renaming many files mid-session call `rescan`.
- NEVER grep inside `addons/` or `assets/` — third-party/asset noise. When grep IS right, scope to `scripts/`, `scenes/`, `ai/`.
- Index is per-checkout by design — it reflects THIS worktree's branch, not main.
