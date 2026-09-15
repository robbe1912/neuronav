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
`expand` = +bidirectional 1-hop ctx · `both` = the shipped default
(since #228 that includes the graph-neighbor boost λ 0.25 @ rrf_k
30 — see the ceiling section) · `wfused` = `both` with weighted RRF
(vec 1.0 / bm25 0.7) instead of the pinned unweighted k=30 · `gb` =
the boost pinned explicitly (same wire as `both` post-#228) ·
`twopass` = `both` + the deterministic second retrieve (issue #74:
pass-1 lexical top hits donate their identifier surface to the
re-embedded augmented query, 2 embeds/query). Pre-#228 `after`
records measured the unboosted default; the #228 cutover re-ran
the set on the shipped wire.

### After — current main (nl2code query prefix default-on per #217; graph-boost λ 0.25 @ rrf_k 30 default-on per #228; two-pass in `twopass`)

commit `4d9d39b` (dirty tree) · mode **real** · model `qwen3-embedding:0.6b` · 58 indexed files · k=12

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| vec | 0.320 | 0.560 | 0.760 | 0.450 | 0.560 | 0.760 |
| bm25 | 0.640 | 0.960 | 0.960 | 0.747 | 0.960 | 0.960 |
| expand | 0.480 | 0.720 | 0.920 | 0.602 | 0.880 | 0.960 |
| both | 0.640 | 0.960 | 0.960 | 0.747 | 0.960 | 0.960 |
| wfused | 0.640 | 0.960 | 0.960 | 0.739 | 0.960 | 0.960 |
| gb | 0.640 | 0.960 | 0.960 | 0.747 | 0.960 | 0.960 |
| twopass | 0.800 | 0.880 | 0.920 | 0.837 | 0.920 | 0.920 |

by kind (hit@5 / MRR):

| kind | n | vec | bm25 | expand | both | wfused | gb | twopass |
|---|---|---|---|---|---|---|---|---|
| exact | 10 | 0.500 / 0.368 | 1.000 / 0.875 | 0.700 / 0.542 | 1.000 / 0.875 | 1.000 / 0.870 | 1.000 / 0.875 | 1.000 / 0.950 |
| symbol | 4 | 0.250 / 0.348 | 0.750 / 0.396 | 0.250 / 0.175 | 0.750 / 0.396 | 0.750 / 0.375 | 0.750 / 0.396 | 0.500 / 0.500 |
| prose | 9 | 0.667 / 0.463 | 1.000 / 0.704 | 0.889 / 0.772 | 1.000 / 0.704 | 1.000 / 0.698 | 1.000 / 0.704 | 0.889 / 0.824 |
| cross | 2 | 1.000 / 1.000 | 1.000 / 1.000 | 1.000 / 1.000 | 1.000 / 1.000 | 1.000 / 1.000 | 1.000 / 1.000 | 1.000 / 1.000 |

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

## cAST file-doc shaping (issue #229)

Question: do size-aware, signature-first FILE docs (cAST 2025 — merge
micro-fns into their carrier, split monsters at block boundaries, keep
every chunk signature-first) lift recall where the fn-layer chunking
(#141) could not? The fn collection is invisible to `recall.search` —
the file collection is what the vector side queries — so #229 shapes
the docs nav embeds per file instead. Self-index calibration: 12/58
files exceed the 30k embed cap (viz.py 462k → 6.5% visible; server.py,
nav.py, graph.py all >50k) and fn bodies sit at p50=547 chars with 28%
micro / 15% monster — the #76 thresholds (220/2000) already match the
p25/p90 boundaries, so the file layer reuses them. The head carries the
path, class/extends, the full symbol surface (capped, `(+N)` tail), and
the module intro; sections flatten (chunk index, source line) so chunk 1
of every fn embeds before chunk 2 of any fn; the whole doc assembles
under the 30k cap. Store lineage rides the #220 law extended to doc
construction: the `doc_shape` stamp (cast<rev>@<scale>) forces a loud
full re-embed on shape flips — sha-gating alone would serve stale
vectors built from the other shape. Doc count is unchanged (one doc
per file; the shaping rewrites the doc text, not the id grammar) —
the ≤2x index-growth budget holds trivially at 1.0x.

### cast leg — cAST file docs (chunk_file_doc=1.0, the shipped default; default wire, default store)

commit `a8c5c9e` (dirty tree) · mode **real** · model `qwen3-embedding:0.6b` · 58 indexed files · k=12

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| vec | 0.440 | 0.800 | 0.960 | 0.598 | 0.800 | 0.960 |
| bm25 | 0.600 | 0.920 | 0.960 | 0.747 | 0.920 | 0.960 |
| expand | 0.440 | 0.800 | 0.960 | 0.598 | 0.920 | 1.000 |
| both | 0.600 | 0.920 | 0.960 | 0.747 | 0.960 | 0.960 |
| wfused | 0.560 | 0.920 | 0.960 | 0.721 | 0.960 | 0.960 |
| gb | 0.680 | 0.960 | 0.960 | 0.775 | 0.960 | 0.960 |
| twopass | 0.600 | 0.920 | 0.920 | 0.743 | 0.920 | 0.920 |

by kind (hit@5 / MRR):

| kind | n | vec | bm25 | expand | both | wfused | gb | twopass |
|---|---|---|---|---|---|---|---|---|
| exact | 10 | 0.700 / 0.497 | 0.900 / 0.750 | 0.700 / 0.497 | 0.900 / 0.750 | 0.900 / 0.700 | 1.000 / 0.875 | 1.000 / 0.883 |
| symbol | 4 | 0.750 / 0.578 | 0.750 / 0.750 | 0.750 / 0.578 | 0.750 / 0.750 | 0.750 / 0.750 | 0.750 / 0.625 | 0.500 / 0.500 |
| prose | 9 | 0.889 / 0.687 | 1.000 / 0.741 | 0.889 / 0.687 | 1.000 / 0.741 | 1.000 / 0.726 | 1.000 / 0.680 | 1.000 / 0.750 |
| cross | 2 | 1.000 / 0.750 | 1.000 / 0.750 | 1.000 / 0.750 | 1.000 / 0.750 | 1.000 / 0.750 | 1.000 / 1.000 | 1.000 / 0.500 |

### raw leg — raw file docs (chunk_file_doc=0, own .tmp store; the pre-#229 surface at the same commit)

commit `a8c5c9e` (dirty tree) · mode **real** · model `qwen3-embedding:0.6b` · 58 indexed files · k=12

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| vec | 0.280 | 0.560 | 0.760 | 0.417 | 0.560 | 0.760 |
| bm25 | 0.520 | 0.920 | 0.960 | 0.654 | 0.920 | 0.960 |
| expand | 0.280 | 0.560 | 0.760 | 0.417 | 0.880 | 0.960 |
| both | 0.520 | 0.920 | 0.960 | 0.654 | 0.960 | 0.960 |
| wfused | 0.440 | 0.800 | 0.960 | 0.598 | 0.960 | 0.960 |
| gb | 0.640 | 0.960 | 0.960 | 0.760 | 0.960 | 0.960 |
| twopass | 0.640 | 0.840 | 0.920 | 0.742 | 0.920 | 0.920 |

by kind (hit@5 / MRR):

| kind | n | vec | bm25 | expand | both | wfused | gb | twopass |
|---|---|---|---|---|---|---|---|---|
| exact | 10 | 0.500 / 0.318 | 1.000 / 0.648 | 0.500 / 0.318 | 1.000 / 0.648 | 0.800 / 0.622 | 1.000 / 0.883 | 1.000 / 0.875 |
| symbol | 4 | 0.250 / 0.348 | 0.750 / 0.458 | 0.250 / 0.348 | 0.750 / 0.458 | 0.750 / 0.438 | 0.750 / 0.396 | 0.500 / 0.500 |
| prose | 9 | 0.667 / 0.428 | 0.889 / 0.725 | 0.667 / 0.428 | 0.889 / 0.725 | 0.778 / 0.608 | 1.000 / 0.731 | 0.778 / 0.699 |
| cross | 2 | 1.000 / 1.000 | 1.000 / 0.750 | 1.000 / 1.000 | 1.000 / 0.750 | 1.000 / 0.750 | 1.000 / 1.000 | 1.000 / 0.750 |

Δ vs the committed `qprefix` baseline (`both` config, raw file docs
at c0343ab), in points (1 pt = 0.010):

| set | docs | hit@1 | hit@5 | hit@10 | MRR |
|---|---|---|---|---|---|
| qprefix (baseline) | raw | 0.520 | 0.880 | 0.960 | 0.672 |
| castq | cast1@1 | +8.0 | +4.0 | +0.0 | +7.5 |
| rawq | raw | +0.0 | +4.0 | +0.0 | -1.8 |

Verdict (double-run fp-jitter protocol, both runs agree on hit@5
for every config on both legs; jitter only wobbles hit@1 by one
rank-1/2 flip and MRR by ≤0.02): the committed `qprefix` baseline
moves 0.88 → **0.92 hit@5** on `both` (+4.0 pts, MRR 0.672 →
0.727/0.747) and 0.88 → 0.92 on `twopass`; `gb` rides 0.92 → 0.96.
The isolated doc-shape effect is the vector leg: castq `vec` hits
0.80 vs rawq 0.56 (+24 pts) — raw docs at this commit truncate the
enlarged nav.py/graph.py even harder than at c0343ab (qprefix `vec`
was 0.60), while the shaped docs keep every file under the 30k cap
with its symbol surface in the head. The same-commit control also
separates corpus drift from shaping: rawq `both` measures 0.92 too,
and the `bm25` leg reads 0.92 vs 0.88 at c0343ab although its
vector side got no better — the BM25F corpus (graph FileSym
fields, doc-shape-independent by construction) drifted with the
enlarged files, so the +4 vs the
committed baseline conflates the two; at the fused level the shape
contribution lands in MRR (0.727/0.747 vs 0.654) and hit@1
(0.56/0.60 vs 0.52), and on `twopass` hit@5 (+8 pts: 0.92 vs 0.84).
Per-query: the two 30k-truncation victims recover on the vector leg
(`import_base` ∅→6, the agent-protocol query ∅→in), leaving
`sync_functions` (6) and `FileSym` (∅) as the residual `both`
misses. Index growth 1.0x (58 docs, one per file) — inside the ≤2x
cAST budget by construction.

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
monotonically (hub files crowd out precise matches). The tier held
on one corpus, so #73 kept `recall.GRAPH_BOOST` at 0.0 — the
opted-in `gb` config pinned λ 0.25 @ rrf_k 30. AMENDED by issue
#228 (see the ceiling section below): the #228 4λ × 3k ×
4-weights grid re-swept the question on the prefixed wire and
cleared the census bar (hit@5 +0.08, MRR +0.075, double-run
stable) — λ 0.25 @ rrf_k 30 is the shipped default since #228;
the after-table `both` row now carries it. Cross-store deltas
(across commits) carry ±jitter; the in-grid ceiling baseline
gb0-k60-w1-1 vs the boosted cells is the attribution.

| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |
|---|---|---|---|---|---|---|
| both (λ=0) | 0.640 | 0.960 | 0.960 | 0.747 | 0.960 | 0.960 |
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

## Recall ceiling push — census exp 1+2 (issue #228)

Grids from `.team_scratch/paper_census.md` (Hydra arXiv 2602.11671;
RepoBench ICLR 2024; RepoCoder EMNLP 2023). Both ride the shipped
qprefix wire on the qwen3 store; win bar = hit@5 or MRR lift
≥ +0.05 over the committed qprefix `both` row (0.520 / 0.880 / 0.960,
MRR 0.672). Every cell double-run under the fp-jitter protocol.

### Exp 1 — graph-boost fusion grid (λ × RRF-k × vec/bm25 weights)

Set `gbw`, commit 0acdd30 (dirty), real, qwen3-embedding:0.6b, 58 files, k=12.
λ=0 rows are the pure weights×k fusion sweep; `0 / 60 / (1, 1)` is
the shipped `both` wire measured in-grid.

| λ | rrf_k | weights | hit@1 | hit@5 | hit@10 | MRR |
|---|---|---|---|---|---|---|
| 0 | 30 | (0.7, 1) | 0.520 | 0.880 | 0.960 | 0.660 |
| 0 | 30 | (1, 0.5) | 0.520 | 0.800 | 0.880 | 0.625 |
| 0 | 30 | (1, 0.7) | 0.520 | 0.800 | 0.960 | 0.645 |
| 0 | 30 | (1, 1) | 0.520 | 0.920 | 0.960 | 0.657 |
| 0 | 60 | (0.7, 1) | 0.560 | 0.920 | 0.960 | 0.681 |
| 0 | 60 | (1, 0.5) | 0.520 | 0.800 | 0.920 | 0.621 |
| 0 | 60 | (1, 0.7) | 0.520 | 0.800 | 0.960 | 0.642 |
| 0 | 60 | (1, 1) | 0.520 | 0.920 | 0.960 | 0.654 |
| 0 | 120 | (0.7, 1) | 0.560 | 0.880 | 0.960 | 0.681 |
| 0 | 120 | (1, 0.5) | 0.520 | 0.800 | 0.920 | 0.620 |
| 0 | 120 | (1, 0.7) | 0.520 | 0.800 | 0.960 | 0.633 |
| 0 | 120 | (1, 1) | 0.520 | 0.920 | 0.960 | 0.654 |
| 0.25 | 30 | (0.7, 1) | 0.680 | 0.960 | 0.960 | 0.777 |
| 0.25 | 30 | (1, 0.5) | 0.600 | 0.880 | 0.960 | 0.716 |
| 0.25 | 30 | (1, 1) | 0.640 | 0.960 | 0.960 | 0.747 |
| 0.25 | 60 | (0.7, 1) | 0.600 | 0.960 | 0.960 | 0.735 |
| 0.25 | 60 | (1, 0.5) | 0.520 | 0.920 | 0.960 | 0.678 |
| 0.25 | 60 | (1, 0.7) | 0.560 | 0.920 | 0.960 | 0.700 |
| 0.25 | 60 | (1, 1) | 0.640 | 0.960 | 0.960 | 0.743 |
| 0.25 | 120 | (0.7, 1) | 0.480 | 0.840 | 0.960 | 0.656 |
| 0.25 | 120 | (1, 0.5) | 0.440 | 0.880 | 0.960 | 0.620 |
| 0.25 | 120 | (1, 0.7) | 0.520 | 0.920 | 0.960 | 0.677 |
| 0.25 | 120 | (1, 1) | 0.520 | 0.880 | 0.960 | 0.679 |
| 0.5 | 30 | (0.7, 1) | 0.520 | 0.880 | 0.960 | 0.689 |
| 0.5 | 30 | (1, 0.5) | 0.480 | 0.880 | 0.960 | 0.648 |
| 0.5 | 30 | (1, 0.7) | 0.520 | 0.920 | 0.960 | 0.678 |
| 0.5 | 30 | (1, 1) | 0.560 | 0.920 | 0.960 | 0.700 |
| 0.5 | 60 | (0.7, 1) | 0.440 | 0.840 | 0.960 | 0.635 |
| 0.5 | 60 | (1, 0.5) | 0.400 | 0.800 | 0.960 | 0.588 |
| 0.5 | 60 | (1, 0.7) | 0.400 | 0.920 | 0.960 | 0.608 |
| 0.5 | 60 | (1, 1) | 0.480 | 0.880 | 0.960 | 0.655 |
| 0.5 | 120 | (0.7, 1) | 0.400 | 0.800 | 0.960 | 0.590 |
| 0.5 | 120 | (1, 0.5) | 0.400 | 0.800 | 0.960 | 0.574 |
| 0.5 | 120 | (1, 0.7) | 0.400 | 0.840 | 0.960 | 0.587 |
| 0.5 | 120 | (1, 1) | 0.440 | 0.840 | 0.960 | 0.615 |
| 0.75 | 30 | (0.7, 1) | 0.400 | 0.880 | 0.960 | 0.609 |
| 0.75 | 30 | (1, 0.5) | 0.400 | 0.840 | 0.960 | 0.588 |
| 0.75 | 30 | (1, 0.7) | 0.440 | 0.920 | 0.960 | 0.626 |
| 0.75 | 30 | (1, 1) | 0.480 | 0.920 | 0.960 | 0.660 |
| 0.75 | 60 | (0.7, 1) | 0.360 | 0.800 | 0.960 | 0.570 |
| 0.75 | 60 | (1, 0.5) | 0.400 | 0.800 | 0.960 | 0.574 |
| 0.75 | 60 | (1, 0.7) | 0.400 | 0.840 | 0.960 | 0.583 |
| 0.75 | 60 | (1, 1) | 0.400 | 0.840 | 0.960 | 0.594 |
| 0.75 | 120 | (1, 0.5) | 0.360 | 0.800 | 0.920 | 0.542 |
| 0.75 | 120 | (1, 0.7) | 0.360 | 0.840 | 0.920 | 0.543 |
| 0.75 | 120 | (1, 1) | 0.360 | 0.800 | 0.920 | 0.549 |

### Exp 2 — two-pass RepoCoder loop grid (pool × budget × imports × pass-2 weight)

Set `tps`, commit 0acdd30 (dirty), boost off (the two questions stay isolated). Hard split seeded by
the in-grid `both` baseline: hard = pass-1 rank miss or > 5 → 2 of 25 queries.

| pool | budget | imports | w2 | hit@5 | MRR | hard h@5 | hard MRR |
|---|---|---|---|---|---|---|---|
| 3 | 160 | 0 | 0.5 | 0.880 | 0.761 | 0.000 | 0.056 |
| 3 | 160 | 0 | 1 | 0.840 | 0.765 | 0.000 | 0.056 |
| 3 | 160 | 1 | 0.5 | 0.880 | 0.761 | 0.000 | 0.056 |
| 3 | 160 | 1 | 1 | 0.840 | 0.765 | 0.000 | 0.056 |
| 3 | 320 | 0 | 0.5 | 0.880 | 0.731 | 0.000 | 0.062 |
| 3 | 320 | 0 | 1 | 0.880 | 0.743 | 0.000 | 0.062 |
| 3 | 320 | 1 | 0.5 | 0.880 | 0.731 | 0.000 | 0.062 |
| 3 | 320 | 1 | 1 | 0.880 | 0.743 | 0.000 | 0.062 |
| 3 | 640 | 0 | 0.5 | 0.880 | 0.690 | 0.000 | 0.056 |
| 3 | 640 | 0 | 1 | 0.880 | 0.739 | 0.000 | 0.056 |
| 3 | 640 | 1 | 0.5 | 0.880 | 0.692 | 0.000 | 0.056 |
| 3 | 640 | 1 | 1 | 0.880 | 0.739 | 0.000 | 0.056 |
| 5 | 160 | 0 | 0.5 | 0.880 | 0.761 | 0.000 | 0.056 |
| 5 | 160 | 0 | 1 | 0.840 | 0.765 | 0.000 | 0.056 |
| 5 | 160 | 1 | 0.5 | 0.880 | 0.761 | 0.000 | 0.056 |
| 5 | 160 | 1 | 1 | 0.840 | 0.765 | 0.000 | 0.056 |
| 5 | 320 | 0 | 0.5 | 0.880 | 0.731 | 0.000 | 0.062 |
| 5 | 320 | 1 | 0.5 | 0.880 | 0.731 | 0.000 | 0.062 |
| 5 | 320 | 1 | 1 | 0.880 | 0.743 | 0.000 | 0.062 |
| 5 | 640 | 0 | 0.5 | 0.880 | 0.690 | 0.000 | 0.056 |
| 5 | 640 | 1 | 1 | 0.880 | 0.739 | 0.000 | 0.056 |
| 12 | 160 | 0 | 0.5 | 0.880 | 0.761 | 0.000 | 0.056 |
| 12 | 160 | 0 | 1 | 0.840 | 0.765 | 0.000 | 0.056 |
| 12 | 160 | 1 | 0.5 | 0.880 | 0.761 | 0.000 | 0.056 |
| 12 | 160 | 1 | 1 | 0.840 | 0.765 | 0.000 | 0.056 |
| 12 | 320 | 0 | 0.5 | 0.880 | 0.731 | 0.000 | 0.062 |
| 12 | 320 | 1 | 0.5 | 0.880 | 0.731 | 0.000 | 0.062 |
| 12 | 640 | 0 | 0.5 | 0.880 | 0.690 | 0.000 | 0.056 |
| 12 | 640 | 0 | 1 | 0.880 | 0.739 | 0.000 | 0.056 |
| 12 | 640 | 1 | 0.5 | 0.880 | 0.690 | 0.000 | 0.056 |
| 12 | 640 | 1 | 1 | 0.880 | 0.739 | 0.000 | 0.056 |

Baseline `both` on the same hard 2: hit@5 0.000, MRR 0.062 (0 by construction on hit@5 — hard is defined
by that record's own rank > 5; MRR still credits rank 6–12).

### Jitter audit (double-run gate)

76 of 84 cells were byte-identical across the two full runs. 8
cells flipped a ±1-rank near-tie (`_has_exact`, the agent-protocol
prose query, `find_functions`, `sha256_of`, subsystem-names); a
third pass settled tps-p12-b640-i1-w1 (kept, pass2 == pass3) and
left 7 cells still flipping across every re-measure — dropped
from `runs/` and excluded from arbitration, not reported as
false precision: gbw-gb0.25-k30-w1-0.7 (both lines ≥ 0.92 h@5,
0.738–0.741 MRR — would not change any verdict),
gbw-gb0.75-k120-w0.7-1 (a losing λ anyway), and tps-p5/p12-
b320-w1 / p5-b640 (all inside the settled b160/b640 tiers' band,
±0.003 MRR). No winner cell and no baseline was unstable.

### Verdict

**Exp 1 — graph-boost fusion: WIN, shipped.** λ 0.25 @ rrf_k 30,
weights (1, 1) lifts the in-grid baseline 0.520/0.920/0.960/MRR
0.654 to 0.640/0.960/0.960/MRR 0.747 — +0.040 hit@1, +0.040
hit@5, +0.093 MRR, double-run byte-stable, and it recovers one
of the two hard queries. Against the committed qprefix `both` row
(0.520/0.880/0.960/0.672, different store — cross-store deltas
carry ±jitter) the same cell reads +0.120 hit@1 / +0.080 hit@5 /
+0.075 MRR: both bars (hit@5, MRR) clear +0.05. The λ 0.25 tier
from #73 holds at k30 on the prefixed wire; every λ ≥ 0.5 still
loses monotonically (hub files crowd out precise matches).
`recall.GRAPH_BOOST`/`recall.RRF_K` ship 0.25/30.0; `both` and
`gb` are the same wire post-#228. The grid max λ 0.25 @ k30
(0.7, 1) (0.680/0.960/0.960/0.777) exceeds the shipped cell by
+0.030 MRR — a vec-downweight, sub-bar single cell, and in the
jitter-excluded cell's own band; not shipped (same law as #73:
no sub-bar single-cell ships).

**Exp 2 — two-pass RepoCoder loop: NEGATIVE on its census win
condition.** The hard split (2 of 25 queries: pass-1 rank miss or
> 5 in the in-grid baseline) is where RepoCoder promised the
lift; hard hit@5 stays 0.000 for every cell — no tuning of pool
(3/5/12), budget (160/320/640), pass-2 weight (0.5/1.0) or
imports recovers either hard query into the top 5 (hard MRR
0.050–0.062 = ranks 8–12). The loop does lift easy-query ranks
(b160 cells: 0.680 hit@1, MRR 0.765 vs baseline 0.520/0.654)
but drops overall hit@5 to 0.840–0.880 vs 0.920 in-grid — the
augmented query outranks the prose target's competitors on some
easy queries. Budget is monotone-better as it shrinks (640 < 320
< 160 — RepoBench's short-context prior transfers); pool and
weight are flat. The `imports` axis is a measurement of a
near-no-op: resolved from-imports already fold into the importer's
surface via the graph's consts folding, so i0/i1 rows are
byte-identical almost everywhere (5 residual names corpus-wide).
TWO_PASS_* stays at #74 values; `two_pass={pool, budget,
imports, weight}` tuning knobs ship for future sweeps, default
off. Shipped as a negative result per bench law.

<details><summary>per-query first-target rank (· = not in top-12; c = only via hop ctx)</summary>

| query | kind | vec | bm25 | expand | both | wfused | gb | twopass |
|---|---|---|---|---|---|---|---|---|
| `parse_tscn` | exact | 1 | 1 | 2 | 1 | 2 | 1 | 1 |
| `sha256_of` | exact | · | 1 | 6 | 1 | 1 | 1 | 1 |
| `titleize` | exact | · | 1 | · | 1 | 1 | 1 | 1 |
| `registry_for` | exact | 1 | 1 | 1 | 1 | 1 | 1 | 1 |
| `sync_functions` | exact | 10 | 1 | 1 | 1 | 1 | 1 | 1 |
| `fold_continuations` | exact | 3 | 1 | 4 | 1 | 1 | 1 | 1 |
| `_has_exact` | exact | · | 4 | · | 4 | 5 | 4 | 1 |
| `_lexical_fallback` | exact | 4 | 1 | 1 | 1 | 1 | 1 | 1 |
| `import_base` | exact | · | 2 | 2 | 2 | 1 | 2 | 2 |
| `find_functions` | exact | 1 | 1 | 1 | 1 | 1 | 1 | 1 |
| `NoCacheHandler` | symbol | 1 | 1 | 3 | 1 | 1 | 1 | 1 |
| `LabelContext` | symbol | 8 | 3 | 7 | 3 | 4 | 3 | 1 |
| `FileSym` | symbol | 6 | · | 8 | · | · | · | · |
| `Func` | symbol | 10 | 4 | 10 | 4 | 4 | 4 | · |
| `where do godot scene resources get read` | prose | 1 | 1 | 1 | 1 | 1 | 1 | 1 |
| `how do cross-module references become caller edges` | prose | 2 | 1 | 1 | 1 | 1 | 1 | 1 |
| `what stops two simultaneous rescans from corrupting the store` | prose | 6 | 1 | 1 | 1 | 1 | 1 | 1 |
| `how do hermetic suites embed without a live model backend` | prose | · | 1 | 1 | 1 | 1 | 1 | 1 |
| `how are subsystem names chosen from member vocabulary` | prose | 2 | 4 | 2 | 4 | 4 | 4 | 4 |
| `which module hosts the agent protocol on stdin and stdout` | prose | · | 4 | 9 | 4 | 5 | 4 | 6 |
| `single call that shows a newcomer how the codebase is organized` | prose | 2 | 3 | 1 | 3 | 3 | 3 | 1 |
| `how is the embedding index archived inside the repository` | prose | 2 | 1 | 1 | 1 | 1 | 1 | 1 |
| `how is visual clutter of the rendered page measured` | prose | 1 | 2 | 3 | 2 | 2 | 2 | 1 |
| `unreachable deletion candidates and their confidence tiers` | cross | 1 | 1 | 1 | 1 | 1 | 1 | 1 |
| `how are node positions computed reproducibly before baking` | cross | 1 | 1 | 1 | 1 | 1 | 1 | 1 |

</details>

## Rerun

```
git worktree add --detach ../bench-measure <commit>
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set sweep --repo ../bench-measure
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set ceiling --repo ../bench-measure
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set ab --repo ../bench-measure
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set qprefix --repo ../bench-measure
# issue #229 doc-shape legs: castq re-embeds the default store once
# (the doc_shape stamp heals), rawq builds its own .tmp store:
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set castq --repo ../bench-measure
.venv/Scripts/python.exe -X utf8 bench/run_bench.py --set rawq --repo ../bench-measure
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
