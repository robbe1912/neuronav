# Recall benchmark — neuronav self-index

Golden set: 25 queries (`bench/golden.json`) over this repo's `.py` files;
every target justified by a code anchor (verified by `--verify-only`).
hit@k = any golden target in the top-k ranked files; MRR over first-target
rank; reach@k additionally credits a target appearing in the 1-hop ctx of a
top-k hit (0 for configs without expansion). Metrics are rank-derived, so
reruns are byte-stable unless ranking changes.

Configs: `vec` = cosine only · `bm25` = +BM25F reciprocal-rank fusion ·
`expand` = +bidirectional 1-hop ctx · `both` = the shipped default.

### Before — pre-fusion baseline (27c437b era, `nav.search`)

commit `27c437b` · mode **real** · model `qwen3-embedding:0.6b` · 22 indexed files · k=12

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| vec | 0.280 | 0.760 | 0.880 | 0.494 | 0.760 | 0.880 |

<details><summary>per-query first-target rank (· = not in top-12; c = only via hop ctx)</summary>

| query | kind | vec |
|---|---|---|
| `parse_tscn` | exact | 2 |
| `sha256_of` | exact | 2 |
| `titleize` | exact | 2 |
| `registry_for` | exact | · |
| `sync_functions` | exact | 7 |
| `_fold_continuations` | exact | 8 |
| `_has_exact` | exact | · |
| `_lexical_fallback` | exact | 3 |
| `import_base` | exact | 3 |
| `find_functions` | exact | 1 |
| `NoCacheHandler` | symbol | 1 |
| `LabelContext` | symbol | 3 |
| `FileSym` | symbol | 6 |
| `MagicPlayer` | symbol | 11 |
| `where are tscn scene files parsed` | prose | 2 |
| `python imports resolved into graph edges` | prose | 4 |
| `advisory cross-process writer lock` | prose | 3 |
| `deterministic hash embeddings for CI` | prose | 1 |
| `cluster labels via tfidf` | prose | 1 |
| `MCP server tools over stdio JSON-RPC` | prose | 2 |
| `one-call orientation tool for agents` | prose | 1 |
| `export embeddings to tracked gz shards` | prose | 1 |
| `readability declutter battery gate` | prose | 1 |
| `dead code tier logic unreachable functions` | cross | 4 |
| `deterministic offline force layout seeded rng` | cross | 2 |

</details>

## Rerun

```
git worktree add --detach ../bench-before 27c437b
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set before --configs vec --repo ../bench-before
git worktree add --detach ../bench-after <after-commit>
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set after --repo ../bench-after
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set fake --fake --repo ../bench-after
```

Before/after are measured in detached worktrees (`git worktree add --detach
<dir> <commit>`), each with its own `.chroma`, so the live shared index is
never touched and attribution is by commit.
