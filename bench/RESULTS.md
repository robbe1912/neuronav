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
±jitter is the ceiling; all committed records below were double-run. Every
record stamps the golden-set fingerprint it was measured against; render
refuses to mix in records from a different golden set (issue #104).

Configs: `vec` = cosine only · `bm25` = +BM25F reciprocal-rank fusion ·
`expand` = +bidirectional 1-hop ctx · `both` = the shipped default ·
`wfused` = `both` with weighted RRF (vec 1.0 / bm25 0.7) instead of the
pinned unweighted k=60 · `gb` = `both` + the swept graph-neighbor
boost (λ winner, see the λ × RRF-k sweep section) · `twopass` =
`both` + the deterministic second retrieve (issue #74: pass-1
lexical top hits donate their identifier surface to the
re-embedded augmented query, 2 embeds/query).

### After — current main (graph-boost winner in `gb`, two-pass in `twopass`)

commit `2b9cbbe` · mode **real** · model `qwen3-embedding:0.6b` · 44 indexed files · k=12

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| vec | 0.360 | 0.560 | 0.760 | 0.444 | 0.560 | 0.760 |
| bm25 | 0.320 | 0.800 | 0.920 | 0.546 | 0.800 | 0.920 |
| expand | 0.360 | 0.560 | 0.760 | 0.444 | 0.760 | 0.880 |
| both | 0.320 | 0.800 | 0.920 | 0.546 | 0.840 | 0.920 |
| wfused | 0.320 | 0.760 | 0.920 | 0.518 | 0.840 | 0.920 |
| gb | 0.400 | 0.840 | 0.920 | 0.567 | 0.840 | 0.920 |
| twopass | 0.440 | 0.880 | 0.960 | 0.661 | 0.920 | 0.960 |

by kind (hit@5 / MRR):

| kind | n | vec | bm25 | expand | both | wfused | gb | twopass |
|---|---|---|---|---|---|---|---|---|
| exact | 10 | 0.400 / 0.282 | 0.700 / 0.499 | 0.400 / 0.282 | 0.700 / 0.499 | 0.600 / 0.453 | 0.800 / 0.537 | 0.800 / 0.628 |
| symbol | 4 | 0.500 / 0.369 | 0.500 / 0.417 | 0.500 / 0.369 | 0.500 / 0.417 | 0.500 / 0.417 | 0.500 / 0.354 | 0.750 / 0.625 |
| prose | 9 | 0.778 / 0.631 | 1.000 / 0.630 | 0.778 / 0.631 | 1.000 / 0.630 | 1.000 / 0.602 | 1.000 / 0.600 | 1.000 / 0.694 |
| cross | 2 | 0.500 / 0.562 | 1.000 / 0.667 | 0.500 / 0.562 | 1.000 / 0.667 | 1.000 / 0.667 | 1.000 / 1.000 | 1.000 / 0.750 |

### FAKE mode — `NEURONAV_EMBED_FAKE=1` plumbing battery

commit `2b9cbbe` · mode **fake** · model `hash-embed` · 44 indexed files · k=12

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| vec | 0.080 | 0.120 | 0.320 | 0.116 | 0.120 | 0.320 |
| bm25 | 0.120 | 0.600 | 0.840 | 0.309 | 0.600 | 0.840 |
| expand | 0.080 | 0.120 | 0.320 | 0.116 | 0.320 | 0.560 |
| both | 0.120 | 0.600 | 0.840 | 0.309 | 0.720 | 0.920 |
| wfused | 0.080 | 0.480 | 0.760 | 0.269 | 0.680 | 0.880 |
| gb | 0.200 | 0.600 | 0.880 | 0.384 | 0.640 | 0.920 |
| twopass | 0.360 | 0.760 | 0.920 | 0.534 | 0.800 | 0.920 |

by kind (hit@5 / MRR):

| kind | n | vec | bm25 | expand | both | wfused | gb | twopass |
|---|---|---|---|---|---|---|---|---|
| exact | 10 | 0.000 / 0.027 | 0.900 / 0.407 | 0.000 / 0.027 | 0.900 / 0.407 | 0.600 / 0.327 | 0.800 / 0.435 | 0.900 / 0.586 |
| symbol | 4 | 0.250 / 0.078 | 0.250 / 0.188 | 0.250 / 0.078 | 0.250 / 0.188 | 0.250 / 0.186 | 0.250 / 0.192 | 0.750 / 0.383 |
| prose | 9 | 0.222 / 0.245 | 0.444 / 0.255 | 0.222 / 0.245 | 0.444 / 0.255 | 0.444 / 0.264 | 0.444 / 0.414 | 0.556 / 0.439 |
| cross | 2 | 0.000 / 0.062 | 0.500 / 0.300 | 0.000 / 0.062 | 0.500 / 0.300 | 0.500 / 0.167 | 1.000 / 0.375 | 1.000 / 1.000 |

## Retired evidence (issue #104)

The pre-boost `before` set (merge-base 63b6f1f) and the two-pass `tp` set
(`feat/two-pass-recall` head 62ef727) were measured against a golden set
whose seeded-rng query targeted `viz.py`. Issue #86 moved that code to
`layout.py`, so `--verify-only` fails at those commits and the sets cannot
be re-run coherently — their records were dropped rather than kept stale.
The verdicts they justified (boost default-off, two-pass plumbed behind a
flag) are merged; the historical tables live in git history, and the
ablation rows (vec/bm25/expand/both) are re-measured at the current
commit inside the After table below.

### λ × RRF-k sweep — graph-neighbor rank boost (issue #73)

commit `2b9cbbe` · mode **real** · model `qwen3-embedding:0.6b` · 44 indexed files · k=12

Boost: each fused top-k source adds λ/(rrf_k+1)/(source rank) to every
distinct 1-hop file neighbor (accumulated across sources; docs outside
both rank lists enter with src=graph). Baseline row = `both` (λ 0) at the
same commit and store as the winner. Deterministic grid, every cell
double-run — wins inside the documented Ollama ±jitter are treated as
ties.

Verdict (re-swept at 2b9cbbe on the 44-file index, issue #104): λ 0.25 @
rrf_k 30 again tops hit@1 — 0.400 vs 0.320–0.360 across every λ=0 cell,
and the after-table `gb` row pins it — while its MRR 0.567 sits in
near-tie range of gb0-k30 (0.576); the retired 34-file sweep crowned the
same cell cleanly (hit@1 0.520 vs 0.440, MRR 0.651 vs 0.624). Every
λ ≥ 0.5 loses monotonically in both sweeps (hub files crowd out precise
matches). The win is a single cell on one corpus, so `recall.GRAPH_BOOST`
stays 0.0 — plumbing landed default-off — and the `gb` config pins the
winner for opted-in evaluation. Cross-store deltas (across commits)
carry ±jitter; the same-store `gb` vs `both` rows are the attribution.

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| both (λ=0) | 0.320 | 0.800 | 0.920 | 0.546 | 0.840 | 0.920 |
| gb0-k30 | 0.360 | 0.800 | 0.960 | 0.576 | 0.840 | 0.960 |
| gb0-k60 | 0.320 | 0.800 | 0.920 | 0.546 | 0.840 | 0.920 |
| gb0-k120 | 0.320 | 0.800 | 0.920 | 0.546 | 0.840 | 0.920 |
| gb0.25-k30 | 0.400 | 0.840 | 0.920 | 0.567 | 0.840 | 0.920 |
| gb0.25-k60 | 0.360 | 0.840 | 0.920 | 0.534 | 0.840 | 0.920 |
| gb0.25-k120 | 0.320 | 0.800 | 0.920 | 0.497 | 0.800 | 0.920 |
| gb0.5-k30 | 0.360 | 0.840 | 0.920 | 0.529 | 0.840 | 0.920 |
| gb0.5-k60 | 0.320 | 0.800 | 0.920 | 0.503 | 0.800 | 0.920 |
| gb0.5-k120 | 0.280 | 0.680 | 0.920 | 0.456 | 0.680 | 0.920 |
| gb1-k30 | 0.200 | 0.760 | 0.920 | 0.419 | 0.760 | 0.920 |
| gb1-k60 | 0.200 | 0.680 | 0.920 | 0.410 | 0.680 | 0.920 |
| gb1-k120 | 0.200 | 0.640 | 0.920 | 0.405 | 0.640 | 0.920 |
| gb2-k30 | 0.160 | 0.640 | 0.920 | 0.376 | 0.640 | 0.920 |
| gb2-k60 | 0.200 | 0.640 | 0.880 | 0.398 | 0.640 | 0.880 |
| gb2-k120 | 0.200 | 0.600 | 0.840 | 0.384 | 0.600 | 0.840 |

<details><summary>per-query first-target rank (· = not in top-12; c = only via hop ctx)</summary>

| query | kind | vec | bm25 | expand | both | wfused | gb | twopass |
|---|---|---|---|---|---|---|---|---|
| `parse_tscn` | exact | 3 | 2 | 3 | 2 | 2 | 2 | 1 |
| `sha256_of` | exact | 11 | 2 | 11 | 2 | 3 | 2 | 1 |
| `titleize` | exact | 1 | 1 | 1 | 1 | 1 | 1 | 1 |
| `registry_for` | exact | · | · | · | · | · | 11 | 6 |
| `sync_functions` | exact | · | 9 | · | 9 | 10 | 3 | 9 |
| `_fold_continuations` | exact | 10 | 1 | 10 | 1 | 1 | 1 | 1 |
| `_has_exact` | exact | · | 8 | · | 8 | 10 | 9 | 2 |
| `_lexical_fallback` | exact | 5 | 2 | 5 | 2 | 3 | 3 | 2 |
| `import_base` | exact | 11 | 4 | 11 | 4 | 6 | 1 | 2 |
| `find_functions` | exact | 1 | 1 | 1 | 1 | 1 | 2 | 2 |
| `NoCacheHandler` | symbol | 1 | 1 | 1 | 1 | 1 | 1 | 1 |
| `LabelContext` | symbol | 4 | 2 | 4 | 2 | 2 | 4 | 1 |
| `FileSym` | symbol | 7 | · | 7 | · | · | · | · |
| `Func` | symbol | 12 | 6 | 12 | 6 | 6 | 6 | 2 |
| `where do godot scene resources get read` | prose | 1 | 2 | 1 | 2 | 2 | 3 | 2 |
| `how do cross-module references become caller edges` | prose | 10 | 3 | 10 | 3 | 3 | 5 | 2 |
| `what stops two simultaneous rescans from corrupting the store` | prose | 8 | 2 | 8 | 2 | 4 | 1 | 1 |
| `how do hermetic suites embed without a live model backend` | prose | 4 | 2 | 4 | 2 | 2 | 1 | 1 |
| `how are subsystem names chosen from member vocabulary` | prose | 1 | 2 | 1 | 2 | 2 | 5 | 2 |
| `which module hosts the agent protocol on stdin and stdout` | prose | 5 | 3 | 5 | 3 | 3 | 3 | 4 |
| `single call that shows a newcomer how the codebase is organized` | prose | 1 | 1 | 1 | 1 | 1 | 3 | 1 |
| `how is the embedding index archived inside the repository` | prose | 1 | 1 | 1 | 1 | 1 | 1 | 2 |
| `how is visual clutter of the rendered page measured` | prose | 1 | 1 | 1 | 1 | 1 | 1 | 1 |
| `unreachable deletion candidates and their confidence tiers` | cross | 8 | 3 | 8 | 3 | 3 | 1 | 2 |
| `how are node positions computed reproducibly before baking` | cross | 1 | 1 | 1 | 1 | 1 | 1 | 1 |

</details>

## Rerun

```
git worktree add --detach ../bench-measure <commit>
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set after --repo ../bench-measure
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set sweep --repo ../bench-measure
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set fake --fake --repo ../bench-measure
```

All sets are measured in a detached worktree (`git worktree add --detach
<dir> <commit>`), with its own `.neuronav/` state store, so the live shared
index is never touched and attribution is by commit. Ordering: run real sets
first, fake last — fake mode wipes the worktree store for embed-mode
coherence, and a real run after it would embed queries against sha-equal
fake docs. Records carry a golden fingerprint; a golden edit without a
re-run makes `--render-only` fail loudly naming the stale records.
