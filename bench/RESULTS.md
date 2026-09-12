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
pinned unweighted k=60 · `gb` = `both` + the swept graph-neighbor
boost (λ winner, see the λ × RRF-k sweep section) · `twopass` =
`both` + the deterministic second retrieve (issue #74: pass-1
lexical top hits donate their identifier surface to the
re-embedded augmented query, 2 embeds/query).

### Before — merge-base 63b6f1f (pre-boost, pre-two-pass, `recall.search` defaults)

commit `63b6f1f` · mode **real** · model `qwen3-embedding:0.6b` · 34 indexed files · k=12

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| vec | 0.280 | 0.680 | 0.880 | 0.430 | 0.680 | 0.880 |
| bm25 | 0.400 | 0.840 | 0.920 | 0.585 | 0.840 | 0.920 |
| expand | 0.280 | 0.680 | 0.880 | 0.430 | 0.800 | 0.920 |
| both | 0.400 | 0.840 | 0.920 | 0.585 | 0.880 | 0.920 |
| wfused | 0.360 | 0.800 | 0.920 | 0.569 | 0.840 | 0.920 |

by kind (hit@5 / MRR):

| kind | n | vec | bm25 | expand | both | wfused |
|---|---|---|---|---|---|---|
| exact | 10 | 0.500 / 0.267 | 0.700 / 0.559 | 0.500 / 0.267 | 0.700 / 0.559 | 0.700 / 0.556 |
| symbol | 4 | 0.750 / 0.415 | 0.750 / 0.425 | 0.750 / 0.415 | 0.750 / 0.425 | 0.500 / 0.417 |
| prose | 9 | 0.778 / 0.582 | 1.000 / 0.667 | 0.778 / 0.582 | 1.000 / 0.667 | 1.000 / 0.611 |
| cross | 2 | 1.000 / 0.600 | 1.000 / 0.667 | 1.000 / 0.600 | 1.000 / 0.667 | 1.000 / 0.750 |

### After — recall branch (graph-boost winner in `gb`, two-pass in `twopass`)

commit `9157e86` (dirty tree) · mode **real** · model `qwen3-embedding:0.6b` · 34 indexed files · k=12

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| vec | 0.280 | 0.600 | 0.880 | 0.427 | 0.600 | 0.880 |
| bm25 | 0.440 | 0.840 | 0.920 | 0.624 | 0.840 | 0.920 |
| expand | 0.280 | 0.600 | 0.880 | 0.427 | 0.800 | 0.920 |
| both | 0.440 | 0.840 | 0.920 | 0.624 | 0.880 | 0.920 |
| wfused | 0.360 | 0.840 | 0.920 | 0.569 | 0.880 | 0.920 |
| gb | 0.520 | 0.880 | 0.960 | 0.651 | 0.880 | 0.960 |

by kind (hit@5 / MRR):

| kind | n | vec | bm25 | expand | both | wfused | gb |
|---|---|---|---|---|---|---|---|
| exact | 10 | 0.400 / 0.257 | 0.700 / 0.561 | 0.400 / 0.257 | 0.700 / 0.561 | 0.700 / 0.539 | 0.800 / 0.724 |
| symbol | 4 | 0.750 / 0.425 | 0.750 / 0.625 | 0.750 / 0.425 | 0.750 / 0.625 | 0.750 / 0.458 | 0.750 / 0.375 |
| prose | 9 | 0.778 / 0.582 | 1.000 / 0.685 | 0.778 / 0.582 | 1.000 / 0.685 | 1.000 / 0.611 | 1.000 / 0.615 |
| cross | 2 | 0.500 / 0.583 | 1.000 / 0.667 | 0.500 / 0.583 | 1.000 / 0.667 | 1.000 / 0.750 | 1.000 / 1.000 |

### FAKE mode — `NEURONAV_EMBED_FAKE=1` plumbing battery

commit `9157e86` · mode **fake** · model `hash-embed` · 34 indexed files · k=12

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| vec | 0.040 | 0.120 | 0.400 | 0.104 | 0.120 | 0.400 |
| bm25 | 0.240 | 0.600 | 0.920 | 0.379 | 0.600 | 0.920 |
| expand | 0.040 | 0.120 | 0.400 | 0.104 | 0.280 | 0.720 |
| both | 0.240 | 0.600 | 0.920 | 0.379 | 0.840 | 0.960 |
| wfused | 0.160 | 0.440 | 0.880 | 0.316 | 0.760 | 0.960 |
| gb | 0.280 | 0.680 | 0.880 | 0.452 | 0.800 | 0.920 |

by kind (hit@5 / MRR):

| kind | n | vec | bm25 | expand | both | wfused | gb |
|---|---|---|---|---|---|---|---|
| exact | 10 | 0.000 / 0.042 | 0.700 / 0.457 | 0.000 / 0.042 | 0.700 / 0.457 | 0.500 / 0.377 | 0.900 / 0.514 |
| symbol | 4 | 0.250 / 0.098 | 0.500 / 0.165 | 0.250 / 0.098 | 0.500 / 0.165 | 0.250 / 0.153 | 0.250 / 0.134 |
| prose | 9 | 0.111 / 0.176 | 0.444 / 0.337 | 0.111 / 0.176 | 0.444 / 0.337 | 0.444 / 0.269 | 0.556 / 0.458 |
| cross | 2 | 0.500 / 0.100 | 1.000 / 0.600 | 0.500 / 0.100 | 1.000 / 0.600 | 0.500 / 0.545 | 1.000 / 0.750 |

### Two-pass A/B — `feat/two-pass-recall` head 62ef727 (pre-boost baselines + `twopass`)

commit `62ef727` (dirty tree) · mode **real** · model `qwen3-embedding:0.6b` · 34 indexed files · k=12

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| vec | 0.280 | 0.640 | 0.880 | 0.433 | 0.640 | 0.880 |
| bm25 | 0.400 | 0.840 | 0.920 | 0.587 | 0.840 | 0.920 |
| expand | 0.280 | 0.640 | 0.880 | 0.433 | 0.760 | 0.920 |
| both | 0.400 | 0.840 | 0.920 | 0.587 | 0.880 | 0.920 |
| wfused | 0.400 | 0.840 | 0.920 | 0.598 | 0.880 | 0.920 |
| twopass | 0.560 | 0.880 | 0.960 | 0.706 | 0.880 | 0.960 |

by kind (hit@5 / MRR):

| kind | n | vec | bm25 | expand | both | wfused | twopass |
|---|---|---|---|---|---|---|---|
| exact | 10 | 0.500 / 0.268 | 0.700 / 0.559 | 0.500 / 0.268 | 0.700 / 0.559 | 0.700 / 0.558 | 0.900 / 0.800 |
| symbol | 4 | 0.500 / 0.411 | 0.750 / 0.438 | 0.500 / 0.411 | 0.750 / 0.438 | 0.750 / 0.425 | 0.500 / 0.531 |
| prose | 9 | 0.778 / 0.588 | 1.000 / 0.667 | 0.778 / 0.588 | 1.000 / 0.667 | 1.000 / 0.685 | 1.000 / 0.670 |
| cross | 2 | 1.000 / 0.600 | 1.000 / 0.667 | 1.000 / 0.600 | 1.000 / 0.667 | 1.000 / 0.750 | 1.000 / 0.750 |

### λ × RRF-k sweep — graph-neighbor rank boost (issue #73)

commit `6c54708` (dirty tree) · mode **real** · model `qwen3-embedding:0.6b` · 34 indexed files · k=12

Boost: each fused top-k source adds λ/(rrf_k+1)/(source rank) to every
distinct 1-hop file neighbor (accumulated across sources; docs outside
both rank lists enter with src=graph). Baseline row = `both` (λ 0) at the
same commit and store as the winner. Deterministic grid, every cell
double-run — wins inside the documented Ollama ±jitter are treated as
ties.

Verdict: λ 0.25 @ rrf_k 30 is the only cell beating λ 0 (hit@1 0.520 vs
0.440, MRR 0.651 vs 0.624, back-to-back on one store); every λ ≥ 0.5
loses monotonically (hub files crowd out precise matches). The win is a
single cell on one corpus, so `recall.GRAPH_BOOST` stays 0.0 — plumbing
landed default-off — and the `gb` config pins the winner for opted-in
evaluation. Cross-store deltas (before vs after tables) carry ±jitter;
the same-store `gb` vs `both` rows are the boost's attribution.

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| both (λ=0) | 0.440 | 0.840 | 0.920 | 0.624 | 0.880 | 0.920 |
| gb0-k30 | 0.440 | 0.840 | 0.920 | 0.629 | 0.880 | 0.920 |
| gb0-k60 | 0.440 | 0.840 | 0.920 | 0.624 | 0.880 | 0.920 |
| gb0-k120 | 0.440 | 0.840 | 0.920 | 0.624 | 0.880 | 0.920 |
| gb0.25-k30 | 0.520 | 0.880 | 0.960 | 0.651 | 0.880 | 0.960 |
| gb0.25-k60 | 0.400 | 0.880 | 0.960 | 0.590 | 0.880 | 0.960 |
| gb0.25-k120 | 0.280 | 0.840 | 0.920 | 0.518 | 0.840 | 0.920 |
| gb0.5-k30 | 0.360 | 0.880 | 0.960 | 0.564 | 0.880 | 0.960 |
| gb0.5-k60 | 0.240 | 0.840 | 0.960 | 0.499 | 0.840 | 0.960 |
| gb0.5-k120 | 0.240 | 0.800 | 0.960 | 0.473 | 0.800 | 0.960 |
| gb1-k30 | 0.160 | 0.840 | 0.960 | 0.440 | 0.840 | 0.960 |
| gb1-k60 | 0.160 | 0.800 | 0.960 | 0.438 | 0.800 | 0.960 |
| gb1-k120 | 0.160 | 0.840 | 0.960 | 0.434 | 0.840 | 0.960 |
| gb2-k30 | 0.160 | 0.840 | 0.960 | 0.429 | 0.840 | 0.960 |
| gb2-k60 | 0.160 | 0.720 | 0.960 | 0.409 | 0.720 | 0.960 |
| gb2-k120 | 0.160 | 0.640 | 0.920 | 0.396 | 0.640 | 0.920 |

<details><summary>per-query first-target rank (· = not in top-12; c = only via hop ctx)</summary>

| query | kind | vec | bm25 | expand | both | wfused | gb |
|---|---|---|---|---|---|---|---|
| `parse_tscn` | exact | 3 | 2 | 3 | 2 | 2 | 1 |
| `sha256_of` | exact | 5 | 1 | 5 | 1 | 1 | 1 |
| `titleize` | exact | 2 | 1 | 2 | 1 | 1 | 1 |
| `registry_for` | exact | · | · | · | · | · | 9 |
| `sync_functions` | exact | 12 | 6 | 12 | 6 | 8 | 2 |
| `_fold_continuations` | exact | 7 | 1 | 7 | 1 | 1 | 1 |
| `_has_exact` | exact | · | 9 | · | 9 | 10 | 8 |
| `_lexical_fallback` | exact | 6 | 3 | 6 | 3 | 3 | 1 |
| `import_base` | exact | 7 | 2 | 7 | 2 | 3 | 1 |
| `find_functions` | exact | 1 | 1 | 1 | 1 | 1 | 2 |
| `NoCacheHandler` | symbol | 1 | 1 | 1 | 1 | 1 | 1 |
| `LabelContext` | symbol | 3 | 2 | 3 | 2 | 2 | 4 |
| `FileSym` | symbol | 5 | · | 5 | · | · | · |
| `Func` | symbol | 6 | 1 | 6 | 1 | 3 | 4 |
| `where do godot scene resources get read` | prose | 1 | 2 | 1 | 2 | 2 | 2 |
| `how do cross-module references become caller edges` | prose | 9 | 3 | 9 | 3 | 3 | 4 |
| `what stops two simultaneous rescans from corrupting the store` | prose | 4 | 1 | 4 | 1 | 2 | 1 |
| `how do hermetic suites embed without a live model backend` | prose | 8 | 2 | 8 | 2 | 3 | 1 |
| `how are subsystem names chosen from member vocabulary` | prose | 2 | 2 | 2 | 2 | 2 | 5 |
| `which module hosts the agent protocol on stdin and stdout` | prose | 4 | 3 | 4 | 3 | 3 | 4 |
| `single call that shows a newcomer how the codebase is organized` | prose | 1 | 1 | 1 | 1 | 1 | 3 |
| `how is the embedding index archived inside the repository` | prose | 1 | 1 | 1 | 1 | 1 | 1 |
| `how is visual clutter of the rendered page measured` | prose | 1 | 1 | 1 | 1 | 1 | 1 |
| `unreachable deletion candidates and their confidence tiers` | cross | 6 | 3 | 6 | 3 | 2 | 1 |
| `how are node positions computed reproducibly before baking` | cross | 1 | 1 | 1 | 1 | 1 | 1 |

</details>

<details><summary>per-query first-target rank (· = not in top-12; c = only via hop ctx)</summary>

| query | kind | vec | bm25 | expand | both | wfused |
|---|---|---|---|---|---|---|
| `parse_tscn` | exact | 3 | 2 | 3 | 2 | 2 |
| `sha256_of` | exact | 4 | 1 | 4 | 1 | 1 |
| `titleize` | exact | 2 | 1 | 2 | 1 | 1 |
| `registry_for` | exact | · | · | · | · | · |
| `sync_functions` | exact | 11 | 7 | 11 | 7 | 8 |
| `_fold_continuations` | exact | 8 | 1 | 8 | 1 | 1 |
| `_has_exact` | exact | · | 9 | · | 9 | 10 |
| `_lexical_fallback` | exact | 6 | 3 | 6 | 3 | 3 |
| `import_base` | exact | 5 | 2 | 5 | 2 | 2 |
| `find_functions` | exact | 1 | 1 | 1 | 1 | 1 |
| `NoCacheHandler` | symbol | 1 | 1 | 1 | 1 | 1 |
| `LabelContext` | symbol | 3 | 2 | 3 | 2 | 2 |
| `FileSym` | symbol | 5 | · | 5 | · | · |
| `Func` | symbol | 8 | 5 | 8 | 5 | 6 |
| `where do godot scene resources get read` | prose | 1 | 2 | 1 | 2 | 2 |
| `how do cross-module references become caller edges` | prose | 9 | 3 | 9 | 3 | 3 |
| `what stops two simultaneous rescans from corrupting the store` | prose | 4 | 1 | 4 | 1 | 2 |
| `how do hermetic suites embed without a live model backend` | prose | 8 | 2 | 8 | 2 | 3 |
| `how are subsystem names chosen from member vocabulary` | prose | 2 | 3 | 2 | 3 | 2 |
| `which module hosts the agent protocol on stdin and stdout` | prose | 4 | 3 | 4 | 3 | 3 |
| `single call that shows a newcomer how the codebase is organized` | prose | 1 | 1 | 1 | 1 | 1 |
| `how is the embedding index archived inside the repository` | prose | 1 | 1 | 1 | 1 | 1 |
| `how is visual clutter of the rendered page measured` | prose | 1 | 1 | 1 | 1 | 1 |
| `unreachable deletion candidates and their confidence tiers` | cross | 5 | 3 | 5 | 3 | 2 |
| `how are node positions computed reproducibly before baking` | cross | 1 | 1 | 1 | 1 | 1 |

</details>

## Rerun

```
git worktree add --detach ../bench-before 63b6f1f
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set before --configs vec,bm25,expand,both,wfused --repo ../bench-before
git worktree add --detach ../bench-after <after-commit>
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set after --repo ../bench-after
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set sweep --repo ../bench-after
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set fake --fake --repo ../bench-after
```

Before/after are measured in detached worktrees (`git worktree add --detach
<dir> <commit>`), each with its own `.neuronav/` state store, so the live shared index is
never touched and attribution is by commit. Ordering: run real sets first,
fake last — fake mode wipes the worktree store for embed-mode coherence,
and a real run after it would embed queries against sha-equal fake docs.
