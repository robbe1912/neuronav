# Recall benchmark — neuronav self-index

Golden set: 25 queries (`bench/golden.json`) over this repo's `.py` files;
every target justified by a code anchor (verified by `--verify-only`).
hit@k = any golden target in the top-k ranked files; MRR over first-target
rank; reach@k additionally credits a target appearing in the 1-hop ctx of a
top-k hit (0 for configs without expansion). Metrics are rank-derived, so
FAKE-mode reruns are byte-identical. Real-mode reruns embed queries fresh
each time: Ollama fp non-determinism can flip a near-tie — observed once
on the baseline (hit@5 0.72 vs 0.76, MRR ±0.005, one query); docs embed
once (sha-incremental store), so document-side ranks stay fixed. Documented
±jitter is the ceiling; all committed records below were double-run.

Configs: `vec` = cosine only · `bm25` = +BM25F reciprocal-rank fusion ·
`expand` = +bidirectional 1-hop ctx · `both` = the shipped default ·
`wfused` = `both` with weighted RRF (vec 1.0 / bm25 0.7) instead of the
pinned unweighted k=60.

### Before — pre-fusion baseline (27c437b era, `nav.search`)

commit `27c437b` · mode **real** · model `qwen3-embedding:0.6b` · 22 indexed files · k=12

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| vec | 0.240 | 0.760 | 0.880 | 0.470 | 0.760 | 0.880 |

by kind (hit@5 / MRR):

| kind | n | vec |
|---|---|---|
| exact | 10 | 0.600 / 0.343 |
| symbol | 4 | 0.500 / 0.398 |
| prose | 9 | 1.000 / 0.670 |
| cross | 2 | 1.000 / 0.350 |

### After — recall-hybrid (`recall.search`)

commit `e582a99` · mode **real** · model `qwen3-embedding:0.6b` · 25 indexed files · k=12

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| vec | 0.240 | 0.680 | 0.880 | 0.455 | 0.680 | 0.880 |
| bm25 | 0.400 | 0.800 | 0.920 | 0.595 | 0.800 | 0.920 |
| expand | 0.240 | 0.680 | 0.880 | 0.455 | 0.800 | 0.920 |
| both | 0.400 | 0.800 | 0.920 | 0.595 | 0.840 | 0.920 |
| wfused | 0.400 | 0.800 | 0.880 | 0.587 | 0.840 | 0.920 |

by kind (hit@5 / MRR):

| kind | n | vec | bm25 | expand | both | wfused |
|---|---|---|---|---|---|---|
| exact | 10 | 0.500 / 0.312 | 0.800 / 0.656 | 0.500 / 0.312 | 0.800 / 0.656 | 0.800 / 0.646 |
| symbol | 4 | 0.500 / 0.396 | 0.500 / 0.396 | 0.500 / 0.396 | 0.500 / 0.396 | 0.500 / 0.396 |
| prose | 9 | 0.889 / 0.664 | 0.889 / 0.599 | 0.889 / 0.664 | 0.889 / 0.599 | 0.889 / 0.599 |
| cross | 2 | 1.000 / 0.350 | 1.000 / 0.667 | 1.000 / 0.350 | 1.000 / 0.667 | 1.000 / 0.625 |

### FAKE mode — `NEURONAV_EMBED_FAKE=1` plumbing battery

commit `e582a99` · mode **fake** · model `hash-embed` · 25 indexed files · k=12

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| vec | 0.120 | 0.280 | 0.440 | 0.195 | 0.280 | 0.440 |
| bm25 | 0.240 | 0.440 | 0.920 | 0.389 | 0.440 | 0.920 |
| expand | 0.120 | 0.280 | 0.440 | 0.195 | 0.520 | 0.680 |
| both | 0.240 | 0.440 | 0.920 | 0.389 | 0.720 | 0.960 |
| wfused | 0.200 | 0.400 | 0.840 | 0.357 | 0.720 | 0.920 |

by kind (hit@5 / MRR):

| kind | n | vec | bm25 | expand | both | wfused |
|---|---|---|---|---|---|---|
| exact | 10 | 0.500 / 0.295 | 0.700 / 0.698 | 0.500 / 0.295 | 0.700 / 0.698 | 0.700 / 0.645 |
| symbol | 4 | 0.250 / 0.271 | 0.250 / 0.150 | 0.250 / 0.271 | 0.250 / 0.150 | 0.250 / 0.139 |
| prose | 9 | 0.111 / 0.065 | 0.222 / 0.165 | 0.111 / 0.065 | 0.222 / 0.165 | 0.111 / 0.156 |
| cross | 2 | 0.000 / 0.125 | 0.500 / 0.333 | 0.000 / 0.125 | 0.500 / 0.333 | 0.500 / 0.250 |

<details><summary>per-query first-target rank (· = not in top-12; c = only via hop ctx)</summary>

| query | kind | vec | bm25 | expand | both | wfused |
|---|---|---|---|---|---|---|
| `parse_tscn` | exact | 3 | 2 | 3 | 2 | 2 |
| `sha256_of` | exact | 2 | 1 | 2 | 1 | 1 |
| `titleize` | exact | 2 | 1 | 2 | 1 | 1 |
| `registry_for` | exact | · | 10 | · | 10 | 12 |
| `sync_functions` | exact | 7 | 3 | 7 | 3 | 4 |
| `_fold_continuations` | exact | 7 | 1 | 7 | 1 | 1 |
| `_has_exact` | exact | · | 8 | · | 8 | 8 |
| `_lexical_fallback` | exact | 6 | 2 | 6 | 2 | 2 |
| `import_base` | exact | 3 | 1 | 3 | 1 | 1 |
| `find_functions` | exact | 1 | 1 | 1 | 1 | 1 |
| `NoCacheHandler` | symbol | 1 | 1 | 1 | 1 | 1 |
| `LabelContext` | symbol | 3 | 2 | 3 | 2 | 2 |
| `FileSym` | symbol | 6 | · | 6 | · | · |
| `MagicPlayer` | symbol | 12 | 12 | 12 | 12 | 12 |
| `where do godot scene resources get read` | prose | 1 | 2 | 1 | 2 | 2 |
| `how do cross-module references become caller edges` | prose | 7 | 7 | 7 | 7 | 7 |
| `what stops two simultaneous rescans from corrupting the store` | prose | 2 | 4 | 2 | 4 | 4 |
| `how do hermetic suites embed without a live model backend` | prose | 2 | 1 | 2 | 1 | 1 |
| `how are subsystem names chosen from member vocabulary` | prose | 2 | 2 | 2 | 2 | 2 |
| `which module hosts the agent protocol on stdin and stdout` | prose | 3 | 2 | 3 | 2 | 2 |
| `single call that shows a newcomer how the codebase is organized` | prose | 1 | 1 | 1 | 1 | 1 |
| `how is the embedding index archived inside the repository` | prose | 1 | 2 | 1 | 2 | 2 |
| `how is visual clutter of the rendered page measured` | prose | 1 | 1 | 1 | 1 | 1 |
| `unreachable deletion candidates and their confidence tiers` | cross | 5 | 3 | 5 | 3 | 4 |
| `how are node positions computed reproducibly before baking` | cross | 2 | 1 | 2 | 1 | 1 |

</details>

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
| `where do godot scene resources get read` | prose | 1 |
| `how do cross-module references become caller edges` | prose | 5 |
| `what stops two simultaneous rescans from corrupting the store` | prose | 2 |
| `how do hermetic suites embed without a live model backend` | prose | 3 |
| `how are subsystem names chosen from member vocabulary` | prose | 2 |
| `which module hosts the agent protocol on stdin and stdout` | prose | 2 |
| `single call that shows a newcomer how the codebase is organized` | prose | 1 |
| `how is the embedding index archived inside the repository` | prose | 1 |
| `how is visual clutter of the rendered page measured` | prose | 1 |
| `unreachable deletion candidates and their confidence tiers` | cross | 5 |
| `how are node positions computed reproducibly before baking` | cross | 2 |

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
never touched and attribution is by commit. Ordering: run real sets first,
fake last — fake mode wipes the worktree store for embed-mode coherence,
and a real run after it would embed queries against sha-equal fake docs.
