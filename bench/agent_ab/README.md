# agent_ab — agent-level A/B harness (issue #72)

Measures **cost-to-answer** of two agent wirings on real task classes over a
live index: a **grep-only** agent (shell text tools, no index) vs a
**neuronav-wired** agent (the MCP read tools). This is the production-proof
instrument the recall benchmark is not: bench measures rank quality of one
tool; this measures whole-agent behavior — tool calls, bytes pulled, files
touched, success — on tasks an agent actually receives.

```
.venv/Scripts/python.exe -X utf8 bench/agent_ab/run.py            # self-index
.venv/Scripts/python.exe -X utf8 tests/test_agent_ab.py           # coherence
```

## Task classes

Every task is a prompt plus a **verifiable expected answer derived from the
actual index** (never hardcoded), so the corpus tracks the repo it measures:

| class | question | ground truth from |
|---|---|---|
| `find-symbol` | which file defines fn X? | `g.files` |
| `trace-call-path` | which cross-file fns does X call? | `g.edges` |
| `locate-refactor-site` | a signature change to X — which other files break? | `g.reverse` |
| `dead-code-check` | is X reachable? (`alive` / `dead:likely` / `dead:review`) | `g.reachable`, `g.dead_code` |

Selection is deterministic (sorted candidates, first K per class). Two
gates keep questions well-posed: a symbol must have exactly one `def` across
the whole **checkout** (an ambiguous question measures nothing), and answers
must fit what the tools can return (e.g. `symbol_graph` shows 8 callers —
tasks needing more are not eligible). Classes the index shape cannot
exercise are **skipped loudly** (`SKIP …` printed, recorded in `notes`) —
the test_viz issue #97 pattern.

## Model-free, scripted policies

The "agent" in each arm is a **scripted policy**: a fixed playbook per task
class (grep arm = fixed regex patterns; neuronav arm = fixed tool-call
sequence), parameterized only by the task's target symbols — never its
expected answer. Why: with an LLM in the loop, cost-to-answer measures the
model's cleverness and every re-run drifts; with a script, it measures the
**tools**, and runs are reproducible. Tokens are N/A by design — the
context-cost proxy is `ret_bytes` (what the tools return to the consumer).

- **grep playbook**: `grep -rn <pattern> --include='*.py' .` per symbol,
  `cat` to slice a function body, then per-callee def-chasing greps. Its
  fixed prior knowledge (Python keywords/builtins/common stdlib method
  names it must not chase) is pinned in `PY_NOCHASE`. Dead checks use the
  honest textual heuristic — a call-site mention means alive; tier is
  structurally unknowable without reachability, so it always answers
  `dead:likely` (a review-tier dead fn is a capability gap it measurably
  fails, not a bug).
- **neuronav playbook**: one `repo_map` orientation preamble (what
  production agents do), then per class: `find_functions` (exact-name
  filter over hits) → `symbol_graph` escalation for find-symbol;
  `symbol_graph` for trace/refactor; `symbol_graph` then `dead_code` for
  dead checks.

## Direct API, not a stdio spawn

The neuronav arm calls `graph.repo_map / find_functions / symbol_graph /
dead_code` **in-process** rather than spawning the stdio MCP server. Why
direct is the deterministic choice: a subprocess adds boot and pipe timing
to exactly the wall-time metric being measured, and JSON-RPC framing makes
byte accounting approximate; server.py's tools are thin wrappers over these
same functions, so tool semantics are identical, while call/byte counts are
exact and the double-run comparison is byte-stable. `semantic_search` and
`explore` are intentionally unused by the playbooks: their outputs are
ranked-file lists and LLM-facing prose that cannot answer "which file
*defines* X" without the exact-match filtering `find_functions` already
provides — a scripted policy must be able to parse its tools' answers
deterministically.

## Metrics, determinism, records

Per-task rows: `success` (ground-truth check, exact on every dimension —
wrong file, partial/padded set, or wrong tier all fail), `calls`, `files`,
`read_bytes`, `ret_bytes`, `wall_ms`. Aggregates per arm and per class.

Every invocation runs the battery **twice** and demands identical rows on
all stable fields (`task/cls/arm/success/answer/calls/files/read_bytes/
ret_bytes`); on mismatch it exits 5 and writes no record. `wall_ms` is
machine-local and advisory, excluded from that contract (recorded twice as
`wall_ms`/`wall_ms2`). Real-mode semantic jitter is absorbed by deriving
answers from exact-name/structural resolution, never raw rank order.

Records: `bench/runs/agent_ab-<label>.json` (default label `selfindex`).
`run_bench._records()` skips that filename prefix — these are not
golden-set evidence and must not trip the issue #104 stale-record gate.

## Fairness and limitations (read before citing numbers)

- **Index build is amortized**: the neuronav arm assumes a built store
  (same as production); the grep arm pays a full-checkout scan per query.
  That is the production question — per-query cost given a live index —
  but it is not "grep loses forever": on a cold repo the index must be
  built first. The record carries index size (files/funcs/edges) as
  context.
- **The grep arm sees the whole checkout** (tests/, bench/ included — that
  is what a shell greps), while the index excludes `bench/` by config; the
  task-uniqueness gate is computed over the same whole checkout, so both
  arms face one well-posed question.
- **Small sample**: ~3 tasks per class, first-in-sorted-order, on one repo
  (self-index). Numbers are directional, not statistical; per-task rows
  are in the record for inspection.
- **Scripted ceiling**: a smarter (or dumber) LLM agent would spend
  differently; this harness prices the tool wirings, not models.
- **grep successes are honest**: textual false positives (a call-shape
  mention in a comment/docstring) and misses count as failures.
