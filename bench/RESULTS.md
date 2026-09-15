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

### After — current main (nl2code query prefix default-on per #217; graph-boost winner in `gb`, two-pass in `twopass`)

commit `a97592a` (dirty tree) · mode **real** · model `qwen3-embedding:0.6b` · 58 indexed files · k=12

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| vec | 0.440 | 0.600 | 0.800 | 0.534 | 0.600 | 0.800 |
| bm25 | 0.520 | 0.880 | 0.960 | 0.672 | 0.880 | 0.960 |
| expand | 0.440 | 0.600 | 0.800 | 0.534 | 0.840 | 0.960 |
| both | 0.520 | 0.880 | 0.960 | 0.672 | 0.960 | 0.960 |
| wfused | 0.480 | 0.840 | 0.960 | 0.630 | 0.920 | 0.960 |
| gb | 0.640 | 0.920 | 0.960 | 0.747 | 0.920 | 0.960 |
| twopass | 0.640 | 0.880 | 0.920 | 0.746 | 0.920 | 0.920 |

by kind (hit@5 / MRR):

| kind | n | vec | bm25 | expand | both | wfused | gb | twopass |
|---|---|---|---|---|---|---|---|---|
| exact | 10 | 0.600 / 0.413 | 0.900 / 0.653 | 0.600 / 0.413 | 0.900 / 0.653 | 0.800 / 0.648 | 0.900 / 0.867 | 1.000 / 0.800 |
| symbol | 4 | 0.500 / 0.567 | 0.750 / 0.750 | 0.500 / 0.567 | 0.750 / 0.750 | 0.750 / 0.583 | 0.750 / 0.562 | 0.500 / 0.500 |
| prose | 9 | 0.556 / 0.605 | 0.889 / 0.698 | 0.556 / 0.605 | 0.889 / 0.698 | 0.889 / 0.605 | 1.000 / 0.639 | 0.889 / 0.849 |
| cross | 2 | 1.000 / 0.750 | 1.000 / 0.500 | 1.000 / 0.750 | 1.000 / 0.500 | 1.000 / 0.750 | 1.000 / 1.000 | 1.000 / 0.500 |

### FAKE mode — `NEURONAV_EMBED_FAKE=1` plumbing battery

commit `2a1f231` · mode **fake** · model `hash-embed` · 58 indexed files · k=12

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| vec | 0.040 | 0.200 | 0.240 | 0.097 | 0.200 | 0.240 |
| bm25 | 0.160 | 0.360 | 0.840 | 0.301 | 0.360 | 0.840 |
| expand | 0.040 | 0.200 | 0.240 | 0.097 | 0.440 | 0.640 |
| both | 0.160 | 0.360 | 0.840 | 0.301 | 0.680 | 0.960 |
| wfused | 0.120 | 0.320 | 0.640 | 0.266 | 0.680 | 0.840 |
| gb | 0.240 | 0.680 | 0.920 | 0.450 | 0.680 | 0.960 |
| twopass | 0.240 | 0.560 | 0.800 | 0.371 | 0.800 | 0.920 |

by kind (hit@5 / MRR):

| kind | n | vec | bm25 | expand | both | wfused | gb | twopass |
|---|---|---|---|---|---|---|---|---|
| exact | 10 | 0.200 / 0.129 | 0.400 / 0.417 | 0.200 / 0.129 | 0.400 / 0.417 | 0.400 / 0.350 | 0.900 / 0.584 | 0.700 / 0.477 |
| symbol | 4 | 0.250 / 0.050 | 0.500 / 0.206 | 0.250 / 0.050 | 0.500 / 0.206 | 0.250 / 0.182 | 0.500 / 0.233 | 0.250 / 0.286 |
| prose | 9 | 0.222 / 0.105 | 0.333 / 0.247 | 0.222 / 0.105 | 0.333 / 0.247 | 0.333 / 0.247 | 0.556 / 0.367 | 0.556 / 0.340 |
| cross | 2 | 0.000 / 0.000 | 0.000 / 0.155 | 0.000 / 0.000 | 0.000 / 0.155 | 0.000 / 0.101 | 0.500 / 0.583 | 0.500 / 0.156 |

## Embedding A/B (issue #75)

Question: does jina-code-embeddings-0.5b (JCE, arXiv 2508.21290) beat the
shipped qwen3-embedding:0.6b on this golden set by the ≥ +3-point margin
the paper's 25-task aggregate suggests (78.41 vs 73.49 overall)? JCE Q8_0
(official jinaai GGUF) is served by llama-server with the card's
`--pooling last` contract on the #17 openai wire — Ollama imports the
same GGUF as a completion model (no pooling metadata) and refuses
`/api/embed`, so the A/B needed a sidecar server, not a provider swap.
Every leg is real embeds, double-run, on its own state store: `ab`/
`qprefix` share the qwen3 store (prefixes are query-side only, no
re-index), `jina`/`jinaq` share the JCE store, `jinap` re-indexes with
the passage instruction prepended to embedded docs (stored documents
stay raw — the prefix is an embed-input transform). Records stamp
the effective `query_prefix`/`doc_prefix`. Since #217 the qprefix
wire is the shipped recall default and `ab` pins `query_prefix=''`
as the raw-query baseline; the jina rows stay as measured at
2a1f231 (pre-#217) — re-running them needs the llama-server
sidecar, not the cutover. Same-store legs are
the attribution unit; cross-store deltas ride the double-run floors
below.

### A/B baseline — qwen3-embedding:0.6b, raw query pinned (`query_prefix=''`) at the #217 cutover; same store as `qprefix`

commit `a97592a` (dirty tree) · mode **real** · model `qwen3-embedding:0.6b` · 58 indexed files · k=12

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| vec | 0.320 | 0.520 | 0.640 | 0.418 | 0.520 | 0.640 |
| bm25 | 0.360 | 0.760 | 0.880 | 0.509 | 0.760 | 0.880 |
| expand | 0.320 | 0.520 | 0.640 | 0.418 | 0.760 | 0.760 |
| both | 0.360 | 0.760 | 0.880 | 0.509 | 0.840 | 0.920 |
| wfused | 0.360 | 0.720 | 0.840 | 0.506 | 0.840 | 0.920 |
| gb | 0.560 | 0.880 | 0.920 | 0.692 | 0.920 | 0.960 |
| twopass | 0.440 | 0.800 | 0.920 | 0.625 | 0.880 | 0.920 |

by kind (hit@5 / MRR):

| kind | n | vec | bm25 | expand | both | wfused | gb | twopass |
|---|---|---|---|---|---|---|---|---|
| exact | 10 | 0.300 / 0.284 | 0.600 / 0.410 | 0.300 / 0.284 | 0.600 / 0.410 | 0.600 / 0.409 | 0.800 / 0.719 | 0.800 / 0.595 |
| symbol | 4 | 0.500 / 0.250 | 0.500 / 0.536 | 0.500 / 0.250 | 0.500 / 0.536 | 0.500 / 0.525 | 0.750 / 0.562 | 0.500 / 0.500 |
| prose | 9 | 0.667 / 0.594 | 1.000 / 0.609 | 0.667 / 0.594 | 1.000 / 0.609 | 0.889 / 0.606 | 1.000 / 0.652 | 0.889 / 0.741 |
| cross | 2 | 1.000 / 0.625 | 1.000 / 0.500 | 1.000 / 0.625 | 1.000 / 0.500 | 1.000 / 0.500 | 1.000 / 1.000 | 1.000 / 0.500 |

### A/B leg 1 — qwen3 + the shipped nl2code query-instruction default (#217; these `both` numbers are the shipped baseline)

commit `a97592a` (dirty tree) · mode **real** · model `qwen3-embedding:0.6b` · 58 indexed files · k=12

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| vec | 0.440 | 0.600 | 0.800 | 0.534 | 0.600 | 0.800 |
| bm25 | 0.520 | 0.880 | 0.960 | 0.672 | 0.880 | 0.960 |
| expand | 0.440 | 0.600 | 0.800 | 0.534 | 0.840 | 0.960 |
| both | 0.520 | 0.880 | 0.960 | 0.672 | 0.960 | 0.960 |
| wfused | 0.480 | 0.840 | 0.960 | 0.630 | 0.920 | 0.960 |
| gb | 0.640 | 0.920 | 0.960 | 0.747 | 0.920 | 0.960 |
| twopass | 0.640 | 0.880 | 0.920 | 0.746 | 0.920 | 0.920 |

by kind (hit@5 / MRR):

| kind | n | vec | bm25 | expand | both | wfused | gb | twopass |
|---|---|---|---|---|---|---|---|---|
| exact | 10 | 0.600 / 0.413 | 0.900 / 0.653 | 0.600 / 0.413 | 0.900 / 0.653 | 0.800 / 0.648 | 0.900 / 0.867 | 1.000 / 0.800 |
| symbol | 4 | 0.500 / 0.567 | 0.750 / 0.750 | 0.500 / 0.567 | 0.750 / 0.750 | 0.750 / 0.583 | 0.750 / 0.562 | 0.500 / 0.500 |
| prose | 9 | 0.556 / 0.605 | 0.889 / 0.698 | 0.556 / 0.605 | 0.889 / 0.698 | 0.889 / 0.605 | 1.000 / 0.639 | 0.889 / 0.849 |
| cross | 2 | 1.000 / 0.750 | 1.000 / 0.500 | 1.000 / 0.750 | 1.000 / 0.500 | 1.000 / 0.750 | 1.000 / 1.000 | 1.000 / 0.500 |

### A/B leg 2 — jina-code-embeddings-0.5b Q8_0 (llama-server `--pooling last`, openai wire), raw query (measured at 2a1f231, pre-#217)

commit `2a1f231` · mode **real** · model `jina-code-embeddings-0.5b:Q8_0` · 58 indexed files · k=12

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| vec | 0.280 | 0.440 | 0.600 | 0.364 | 0.440 | 0.600 |
| bm25 | 0.400 | 0.720 | 0.880 | 0.538 | 0.720 | 0.880 |
| expand | 0.280 | 0.440 | 0.600 | 0.364 | 0.800 | 0.880 |
| both | 0.400 | 0.720 | 0.880 | 0.538 | 0.920 | 0.960 |
| wfused | 0.400 | 0.680 | 0.840 | 0.529 | 0.920 | 0.920 |
| gb | 0.640 | 0.920 | 0.960 | 0.744 | 0.920 | 0.960 |
| twopass | 0.600 | 0.800 | 0.840 | 0.695 | 0.920 | 0.920 |

by kind (hit@5 / MRR):

| kind | n | vec | bm25 | expand | both | wfused | gb | twopass |
|---|---|---|---|---|---|---|---|---|
| exact | 10 | 0.400 / 0.208 | 0.700 / 0.552 | 0.400 / 0.208 | 0.700 / 0.552 | 0.700 / 0.533 | 0.900 / 0.910 | 0.800 / 0.759 |
| symbol | 4 | 0.250 / 0.327 | 0.500 / 0.411 | 0.250 / 0.327 | 0.500 / 0.411 | 0.500 / 0.406 | 0.750 / 0.458 | 0.500 / 0.500 |
| prose | 9 | 0.556 / 0.568 | 0.778 / 0.566 | 0.556 / 0.568 | 0.778 / 0.566 | 0.667 / 0.558 | 1.000 / 0.630 | 0.889 / 0.699 |
| cross | 2 | 0.500 / 0.295 | 1.000 / 0.600 | 0.500 / 0.295 | 1.000 / 0.600 | 1.000 / 0.625 | 1.000 / 1.000 | 1.000 / 0.750 |

### A/B leg 2b — jina + nl2code query instruction (same store as `jina`; measured at 2a1f231, pre-#217)

commit `2a1f231` · mode **real** · model `jina-code-embeddings-0.5b:Q8_0` · 58 indexed files · k=12

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| vec | 0.280 | 0.600 | 0.720 | 0.433 | 0.600 | 0.720 |
| bm25 | 0.520 | 0.680 | 0.960 | 0.631 | 0.680 | 0.960 |
| expand | 0.280 | 0.600 | 0.720 | 0.433 | 0.880 | 0.960 |
| both | 0.520 | 0.680 | 0.960 | 0.631 | 0.920 | 0.960 |
| wfused | 0.560 | 0.680 | 0.840 | 0.636 | 0.920 | 0.960 |
| gb | 0.680 | 0.920 | 0.960 | 0.791 | 0.920 | 0.960 |
| twopass | 0.600 | 0.800 | 0.880 | 0.687 | 0.920 | 0.920 |

by kind (hit@5 / MRR):

| kind | n | vec | bm25 | expand | both | wfused | gb | twopass |
|---|---|---|---|---|---|---|---|---|
| exact | 10 | 0.500 / 0.268 | 0.600 / 0.604 | 0.500 / 0.268 | 0.600 / 0.604 | 0.600 / 0.587 | 0.900 / 0.811 | 0.800 / 0.698 |
| symbol | 4 | 1.000 / 0.625 | 0.750 / 0.750 | 1.000 / 0.625 | 0.750 / 0.750 | 0.750 / 0.750 | 0.750 / 0.750 | 0.500 / 0.500 |
| prose | 9 | 0.556 / 0.592 | 0.778 / 0.620 | 0.556 / 0.592 | 0.778 / 0.620 | 0.778 / 0.655 | 1.000 / 0.741 | 0.889 / 0.745 |
| cross | 2 | 0.500 / 0.167 | 0.500 / 0.583 | 0.500 / 0.167 | 0.500 / 0.583 | 0.500 / 0.571 | 1.000 / 1.000 | 1.000 / 0.750 |

### A/B leg 2c — jina paper recipe: query + `Candidate code snippet:` passage instruction at index time (fresh store; measured at 2a1f231, pre-#217)

commit `2a1f231` · mode **real** · model `jina-code-embeddings-0.5b:Q8_0` · 58 indexed files · k=12

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| vec | 0.360 | 0.640 | 0.680 | 0.476 | 0.640 | 0.680 |
| bm25 | 0.560 | 0.800 | 0.920 | 0.651 | 0.800 | 0.920 |
| expand | 0.360 | 0.640 | 0.680 | 0.476 | 0.840 | 0.960 |
| both | 0.560 | 0.800 | 0.920 | 0.651 | 0.960 | 0.960 |
| wfused | 0.600 | 0.800 | 0.880 | 0.670 | 0.960 | 0.960 |
| gb | 0.720 | 0.960 | 0.960 | 0.817 | 0.960 | 0.960 |
| twopass | 0.640 | 0.800 | 0.880 | 0.703 | 0.920 | 0.920 |

by kind (hit@5 / MRR):

| kind | n | vec | bm25 | expand | both | wfused | gb | twopass |
|---|---|---|---|---|---|---|---|---|
| exact | 10 | 0.500 / 0.254 | 0.800 / 0.620 | 0.500 / 0.254 | 0.800 / 0.620 | 0.800 / 0.606 | 1.000 / 0.875 | 0.800 / 0.750 |
| symbol | 4 | 1.000 / 0.875 | 0.750 / 0.750 | 1.000 / 0.875 | 0.750 / 0.750 | 0.750 / 0.750 | 0.750 / 0.750 | 0.500 / 0.500 |
| prose | 9 | 0.667 / 0.596 | 0.889 / 0.662 | 0.667 / 0.596 | 0.889 / 0.662 | 0.889 / 0.731 | 1.000 / 0.815 | 0.889 / 0.750 |
| cross | 2 | 0.500 / 0.250 | 0.500 / 0.562 | 0.500 / 0.250 | 0.500 / 0.562 | 0.500 / 0.550 | 1.000 / 0.667 | 1.000 / 0.667 |

Δ vs the `ab` baseline (`both` config), in points (1 pt = 0.010);
the ab row shows absolutes, leg rows show deltas:

| set | model | hit@1 | hit@5 | hit@10 | MRR |
|---|---|---|---|---|---|
| ab | `qwen3-embedding:0.6b` | 0.360 | 0.760 | 0.880 | 0.509 |
| qprefix | `qwen3-embedding:0.6b` | +16.0 | +12.0 | +8.0 | +16.3 |
| jina | `jina-code-embeddings-0.5b:Q8_0` | +4.0 | -4.0 | +0.0 | +2.9 |
| jinaq | `jina-code-embeddings-0.5b:Q8_0` | +16.0 | -8.0 | +8.0 | +12.2 |
| jinap | `jina-code-embeddings-0.5b:Q8_0` | +20.0 | +4.0 | +4.0 | +14.2 |

Verdict — two rounds, each double-run (every metric line identical
across passes; the Ollama fp-jitter flipped nothing in either round).
Model swap (round 1, measured at 2a1f231): FAILS the ≥ +3-point win
condition on the shipped `both` config — plain jina loses hit@5 by
12.0 pts (0.72 vs 0.84), jinaq by 16.0, and the full paper recipe
jinap still trails hit@5 by 4.0 (0.80 vs 0.84) despite winning hit@1
(+20.0) and MRR (+12.5); the vec-only rows show the same shape
(jina/vec hit@5 0.44 vs ab/vec 0.52), so the paper's aggregate edge
does not transfer to whole-file retrieval on this corpus at Q8_0.
qwen3-embedding:0.6b stays; jinap topping the default-off `gb` column
(0.72/0.96, MRR 0.817) is noted, not shipped. Query prefix (round 2,
measured at the #217 cutover): the free leg wins again, same qwen3
model, fresh store — the raw-query `ab` baseline runs 0.36/0.76/0.88
with MRR 0.509 (hit@5 sits 8 pts under round 1's 0.84 on the same
wire: the corpus moved under the v0.1.2 merge, not the retrieval), and
the shipped prefix lifts `both` to 0.52/0.88/0.96 with MRR 0.672 —
+16.0 hit@1 / +12.0 hit@5 / +8.0 hit@10 / +16.3 MRR — plus `gb` to
0.64/0.92/0.96 (MRR 0.747) and `twopass` to 0.64/0.88/0.92 (MRR
0.746). `after` is byte-identical to `qprefix` on every config: the
shipped default wire IS the measured leg. #217 ships the prefix.

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

commit `2a1f231` · mode **real** · model `qwen3-embedding:0.6b` · 58 indexed files · k=12

Boost: each fused top-k source adds λ/(rrf_k+1)/(source rank) to every
distinct 1-hop file neighbor (accumulated across sources; docs outside
both rank lists enter with src=graph). Baseline row = `both` (λ 0) at the
same commit and store as the winner. Deterministic grid, every cell
double-run — wins inside the documented Ollama ±jitter are treated as
ties.

Grid measured at 2a1f231 with the RAW query (pre-#217 prefix
cutover): the sweep arbitrates λ against its own both-baseline
inside one store, so the cutover does not invalidate the grid,
but sweep cells are not comparable to the prefixed after/gb
rows. Verdict (re-swept at 2a1f231 on the 58-file index after the issue #75
golden re-justify — the retired 44-file sweep at 2b9cbbe crowned the
same cell): λ 0.25 @ rrf_k 30 sits in a three-cell top tier — hit@1
0.560 here vs 0.600 at gb0.25-k60 and gb0.5-k30, a one-query gap well
inside the documented jitter — and it carries the tier's best hit@10
(0.920) with MRR 0.692 vs the k60 cell's 0.702. Every λ ≥ 1 loses
monotonically (hub files crowd out precise matches). The win stays a
single-cell-tier result on one corpus, so `recall.GRAPH_BOOST` stays
0.0 — default-off — and the `gb` config keeps pinning λ 0.25 @
rrf_k 30 for opted-in evaluation; the after-table `gb` row pins it.
Cross-store deltas (across commits) carry ±jitter; the same-store `gb`
vs `both` rows are the attribution.

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| both (λ=0) | 0.520 | 0.880 | 0.960 | 0.672 | 0.960 | 0.960 |
| gb0-k30 | 0.360 | 0.800 | 0.920 | 0.541 | 0.840 | 0.960 |
| gb0-k60 | 0.360 | 0.840 | 0.880 | 0.526 | 0.880 | 0.920 |
| gb0-k120 | 0.360 | 0.840 | 0.880 | 0.519 | 0.920 | 0.920 |
| gb0.25-k30 | 0.560 | 0.880 | 0.920 | 0.692 | 0.920 | 0.960 |
| gb0.25-k60 | 0.600 | 0.880 | 0.880 | 0.702 | 0.920 | 0.920 |
| gb0.25-k120 | 0.520 | 0.800 | 0.880 | 0.646 | 0.840 | 0.920 |
| gb0.5-k30 | 0.600 | 0.880 | 0.920 | 0.696 | 0.920 | 0.920 |
| gb0.5-k60 | 0.520 | 0.840 | 0.920 | 0.648 | 0.880 | 0.920 |
| gb0.5-k120 | 0.280 | 0.760 | 0.960 | 0.500 | 0.800 | 0.960 |
| gb1-k30 | 0.360 | 0.800 | 0.920 | 0.545 | 0.840 | 0.920 |
| gb1-k60 | 0.240 | 0.800 | 0.960 | 0.466 | 0.840 | 0.960 |
| gb1-k120 | 0.240 | 0.720 | 0.920 | 0.453 | 0.760 | 0.920 |
| gb2-k30 | 0.280 | 0.760 | 0.960 | 0.478 | 0.800 | 0.960 |
| gb2-k60 | 0.200 | 0.720 | 0.920 | 0.426 | 0.760 | 0.920 |
| gb2-k120 | 0.160 | 0.720 | 0.880 | 0.389 | 0.760 | 0.880 |

<details><summary>per-query first-target rank (· = not in top-12; c = only via hop ctx)</summary>

| query | kind | vec | bm25 | expand | both | wfused | gb | twopass |
|---|---|---|---|---|---|---|---|---|
| `parse_tscn` | exact | 1 | 1 | 1 | 1 | 1 | 1 | 1 |
| `sha256_of` | exact | 11 | 3 | 11 | 3 | 3 | 1 | 4 |
| `titleize` | exact | 8 | 1 | 8 | 1 | 1 | 1 | 1 |
| `registry_for` | exact | 1 | 1 | 1 | 1 | 1 | 1 | 1 |
| `sync_functions` | exact | 3 | 3 | 3 | 3 | 3 | 1 | 2 |
| `fold_continuations` | exact | 3 | 1 | 3 | 1 | 1 | 1 | 1 |
| `_has_exact` | exact | · | 5 | · | 5 | 6 | 6 | 1 |
| `_lexical_fallback` | exact | 4 | 2 | 4 | 2 | 2 | 1 | 1 |
| `import_base` | exact | · | 6 | · | 6 | 7 | 2 | 4 |
| `find_functions` | exact | 1 | 1 | 1 | 1 | 1 | 1 | 1 |
| `NoCacheHandler` | symbol | 1 | 1 | 1 | 1 | 1 | 1 | 1 |
| `LabelContext` | symbol | 1 | 1 | 1 | 1 | 1 | 1 | 1 |
| `FileSym` | symbol | 6 | · | 6 | · | · | · | · |
| `Func` | symbol | 10 | 1 | 10 | 1 | 3 | 4 | · |
| `where do godot scene resources get read` | prose | 1 | 1 | 1 | 1 | 1 | 1 | 1 |
| `how do cross-module references become caller edges` | prose | 7 | 2 | 7 | 2 | 3 | 3 | 1 |
| `what stops two simultaneous rescans from corrupting the store` | prose | 11 | 1 | 11 | 1 | 3 | 1 | 1 |
| `how do hermetic suites embed without a live model backend` | prose | 8 | 3 | 8 | 3 | 3 | 1 | 1 |
| `how are subsystem names chosen from member vocabulary` | prose | 1 | 3 | 1 | 3 | 3 | 3 | 2 |
| `which module hosts the agent protocol on stdin and stdout` | prose | 12 | 9 | 12 | 9 | 9 | 4 | 7 |
| `single call that shows a newcomer how the codebase is organized` | prose | 1 | 1 | 1 | 1 | 1 | 3 | 1 |
| `how is the embedding index archived inside the repository` | prose | 1 | 1 | 1 | 1 | 1 | 1 | 1 |
| `how is visual clutter of the rendered page measured` | prose | 1 | 1 | 1 | 1 | 1 | 2 | 1 |
| `unreachable deletion candidates and their confidence tiers` | cross | 2 | 2 | 2 | 2 | 1 | 1 | 2 |
| `how are node positions computed reproducibly before baking` | cross | 1 | 2 | 1 | 2 | 2 | 1 | 2 |

</details>

## Rerun

```
git worktree add --detach ../bench-measure <commit>
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set sweep --repo ../bench-measure
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set ab --repo ../bench-measure
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set qprefix --repo ../bench-measure
# JCE legs: serve the official jinaai Q8_0 GGUF first (Ollama imports
# it as a completion model — /api/embed refuses the unpooled GGUF):
llama-server -m jina-code-embeddings-0.5b-Q8_0.gguf --embeddings --pooling last --host 127.0.0.1 --port 18081 -c 32768
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set jina --repo ../bench-measure
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set jinaq --repo ../bench-measure
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set jinap --repo ../bench-measure
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set fake --fake --repo ../bench-measure
```

All sets are measured in a detached worktree (`git worktree add --detach
<dir> <commit>`), with its own `.neuronav/` state store, so the live shared
index is never touched and attribution is by commit. Ordering: run real sets
first, fake last — fake mode wipes the worktree store for embed-mode
coherence, and a real run after it would embed queries against sha-equal
fake docs. A/B legs (issue #75): run `ab` (pins `query_prefix=''`) then
`qprefix` (the #217 shipped default) first, both on the qwen3
store, then the jina legs (their own `.tmp/` stores); `jinap` re-embeds
the corpus with the passage instruction, the others reuse it. Records
carry a golden fingerprint; a golden edit without a
re-run makes `--render-only` fail loudly naming the stale records.


## Agent-level A/B (issue #72)

Instrument: `bench/agent_ab/` (README there). Scripted, model-free agents —
grep-only (shell text tools, no index) vs neuronav-wired (`repo_map` /
`find_functions` / `symbol_graph` / `dead_code`, called direct in-process) —
answer index-derived tasks over the self-index; the metric is cost-to-answer,
not LLM cleverness. Records: `bench/runs/agent_ab-selfindex.json`.

Measured on the PR branch (12 tasks: 3 find-symbol, 3 trace-call-path,
3 locate-refactor-site, 3 dead-code-check; self-index 59 files / 566 fns /
763 edges, real qwen3-embedding:0.6b, double-run deterministic):

| arm | success | tool calls | files read | KB read | KB returned | ms (sum) |
|---|---|---|---|---|---|---|
| grep | 7/12 | 28 | 1,978 | 44,485 | 12.3 | 1,838 |
| neuronav | 12/12 | 28 | 0 | 0.0 | 55.4 | 1,194 |

Success by class (grep / neuronav): find-symbol 3/3 vs 3/3,
trace-call-path 1/3 vs 3/3, locate-refactor-site 3/3 vs 3/3,
dead-code-check 0/3 vs 3/3. Where grep fails it fails structurally:
trace — def-chasing a called name surfaces every same-named def
(`dead_code` in three files) where the resolved graph names one target;
dead-code — entry-rule references (bare idents, test mains) are invisible
to a call-syntax grep, so it calls live fns dead, and the one true-dead pick
is review-tier (dynamic-hint dispatch) where grep's no-tier heuristic answers
`dead:likely`. Tool calls tie because
grep burns them def-chasing while the wired arm pays a fixed `repo_map`
orientation per task.

Limitations, honestly: small sample (12 tasks, first-in-sorted-order — the
picks skew `bake/*`, alphabetical artifact); wall ms is machine-local
(neuronav's includes one Ollama query-embed round-trip per find-symbol
task, ~350 ms warm; grep's excludes process spawn — policies run
in-process); index build is amortized for the wired arm and unaccounted;
the corpus is tests+tools aware (grep legitimately scans what a shell
sees). The dead side thinned 1/2 (second candidate's name not
checkout-unique). Directional, not statistical.
