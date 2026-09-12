# AGENTS.md — config/

Config resolution. One neuronav install indexes many projects and the
install itself stays read-only (issue #27): the project-local config
(``<project>/.neuronav/config.json``, written by ``onboard.py init``) is
the primary form; named profiles here are the tuned-override form.

## Selection (``nav._discover_config``)

1. ``$NEURONAV_CONFIG`` — explicit, always wins (absolute path). A set-but-
   missing path aborts at load (issue #41): an explicit config is a
   contract, not a hint — silently degrading to walk-all defaults would
   flip the walk identity and the next rescan would purge the previous
   profile's entries.
2. ``<cwd>/.neuronav/config.json`` — project-local config; running any
   command from inside a project just works.
3. ``config.json`` at the repo ROOT (gitignored, machine-local) — the
   legacy default, honored ONLY when cwd is the checkout itself.
4. Nothing found — pure cwd defaults: root = cwd, include ``.``,
   extensions = every registered extractor suffix, excludes = the sane
   set (``.git``, ``__pycache__``, ``.venv``, ``.neuronav``,
   ``node_modules``). This is the npx shape: no config, no edits.

A rescan that matches ZERO files aborts the same way (issue #41) — a
config that walks nothing is a typo, not an empty index.

A config that omits ``state_dir`` aborts at load too (issue #91): the
silent ``<root>/.neuronav`` default is a store INSIDE the scanned root,
so a config whose ``root`` points at a foreign checkout would read and
write that checkout's live store directly — the door that wiped one.
The fix is one key: an explicit ``state_dir`` path, or the exact string
``"default"`` (case-sensitive — any other value is a path) to opt into
``<root>/.neuronav`` (``onboard.py init`` writes the opt-in;
the shipped profiles carry it). Only the no-config pure-defaults leg
(step 4 above) keeps the implicit default — no config, nothing to fix.

``nav._apply_config`` runs once at import (and again on
``nav.py --config <path>``, which also exports the var so sibling
modules and subprocesses agree). A relative ``"root"`` resolves against
the config file's own directory — shipped profiles stay machine-portable
(``config/neuronav.json`` uses ``"root": ".."`` to index this repo
itself).

``.neuroignore`` beside the active config (project-local
``<root>/.neuronav/.neuroignore``, or ``config/.neuroignore`` for a
shipped profile) extends ``exclude_dirs`` with one directory name per
line — ``#`` comments and blank lines ignored, matched at any depth
(issue #36). ``onboard.py init`` scaffolds one pre-seeded with the
scratch conventions (``.tmp``, ``.team_scratch``); users adjust it
without touching the config json.

## Fields (consumed by `nav._apply_config`)

| field | default | meaning |
|---|---|---|
| `root` | parent of the install | target repo root (relative -> resolve against the profile's dir) |
| `state_dir` | required — aborts without it (issue #91) | ALL generated state for the profile: `chroma/` vectordb, `base/` shards, `graph.html` bake (relative -> resolve against the profile's dir); `"default"` = explicit opt-in to `<root>/.neuronav` (`onboard.py init` writes it) — the silent in-root default once wiped a live store |
| `collection` | `"main"` | chroma collection name; fn-level index lives at `<collection>-fns` |
| `include_dirs` | `scripts, scenes, VFX, ai, tests, tools` | walked under root |
| `extensions` | `.gd, .tscn` | suffixes kept (must be registered in `extractors/` to parse) |
| `exclude_dirs` | `.git, __pycache__` | pruned from the directory walk |
| `.neuroignore` | (file beside config) | extra exclude dir names, one per line, merged into `exclude_dirs` at load |
| `recall_two_pass` | `false` | >false: `recall.search` runs the deterministic two-pass retrieve (issue #74) — the pass-1 lexical top hits donate their identifier surface (320-char budget) to a re-embedded augmented query RRF-fused with pass 1; embed budget 2/query, hits marked `two_pass`; skipped entirely in degraded BM25F-only mode. Bench A/B: hit@1 0.40→0.56, hit@5 0.84→0.88, hit@10 0.92→0.96, MRR 0.587→0.706 — default stays OFF because it doubles query-side embeds on the shared `semantic_search` path |
| `embed_url` | `http://127.0.0.1:11434/api/embed` | embedding endpoint (Ollama `/api/embed` or any OpenAI-compatible `/embeddings`) |
| `embed_model` | `qwen3-embedding:0.6b` | model name sent verbatim; also the vector-space fingerprint on the collection and in base-export manifests |
| `embed_dim` | `1024` | explicit per profile — never inferred, mismatch fails loud |
| `embed_provider` | auto-detect from `embed_url` | `"ollama"` or `"openai"` wire protocol; url ending in `/embeddings` -> `openai`, anything else -> `ollama`; unknown explicit value exits loud |
| `embed_api_key` | `""` (none) | sent as `Authorization: Bearer ...` ONLY when non-empty; `$NEURONAV_EMBED_KEY` (evaluated at config load) wins over this field so secrets stay out of tracked profiles |

## Embedding providers (issue #17)

`nav.embed` speaks two wire protocols behind the same config keys:

| provider | request | response rows | auth |
|---|---|---|---|
| `ollama` (default) | `{model, input}` to `/api/embed` | `embeddings[i]` | header only when a key is set (Ollama ignores it) |
| `openai` | `{model, input[]}` to any `/v1/embeddings` | `data[i].embedding`, sorted by `index` (row order is not guaranteed) | Bearer key when set |

The `openai` side covers OpenAI itself and every compatible endpoint —
vLLM, LM Studio, llama.cpp server, and Ollama's own `/v1` layer — so
switching is a config edit, not a code change. Details:

- Provider resolution happens in `nav._apply_config`: explicit
  `embed_provider` wins (case-insensitive, anything but the two names
  exits loud with a fix hint); unset auto-detects from the `embed_url`
  path. Default configs keep the exact Ollama behavior.
- Keys: `$NEURONAV_EMBED_KEY` beats `embed_api_key` in the config json;
  both are read at config load (import or `nav.py --config`). Keyless
  local servers stay keyless — no header is sent when no key is set.
- Robustness: requests chunk at the internal batch of 32 (OpenAI caps
  input array length); `429` responses back off — a parseable
  `Retry-After` is honored capped at 60s, otherwise 1s/2s/4s, then fail.
- Loud failures name the provider: count mismatches raise (never pad or
  truncate), wrong-shape responses raise, missing keys against an
  authed endpoint surface the HTTP error. The collection fingerprint
  records `embed_model` + `embed_provider` (a provider-only change
  never blocks reuse — the model defines the vector space); base-export
  manifests gained a `provider` field, older manifests/collections
  default to `ollama` in messages.
- `NEURONAV_EMBED_FAKE=1` (CI plumbing) short-circuits before any
  network: same deterministic hash vectors as before, provider ignored.

Bench note: before/after recall comparisons must pin ONE
`embed_provider` + `embed_model` pair for both runs — `bench/`
records the model in its result json, and mixing providers (or models)
compares two different vector spaces, not two code states.

## exclude_dirs semantics — read before adding a profile

`exclude_dirs` prunes the traversal itself (`os.walk` in `nav.iter_files`),
so excluded directories cost nothing and — critically — their files never
enter the index. Any directory that holds scratch output, fixtures, or
tooling state MUST be excluded, or it pollutes the index: the dead-code tier
reads their helper functions as unreachable noise and cluster labels drift
toward the junk. The self-index profile (`neuronav.json`) exists precisely
to keep that signal clean — it indexes only `.py` and excludes the venv,
the per-project state store (`.neuronav/`), and fixture trees.

`.tmp/` (repo root) is the sanctioned home for throwaway worktrees and test
screenshots; it is gitignored and — because the self-index walks `.`
(`include_dirs: ["."]`) — it is on `neuronav.json`'s exclude list. Never
park scratch in the repo root itself.

`include_dirs` and `extensions` are additive filters on top; `collection`
namespacing means two profiles never share vectors.


## Auto-rescan stat gate (issue #19)

Read tools never answer from a stale index silently: each call first runs
`nav.stat_fingerprint()` — a stat-only (mtime_ns, size) walk mirroring
`iter_files`' include/exclude rules, TTL-cached for `nav.STAT_TTL_S` (3s)
so bursts of tool calls do not re-stat the tree — and a drifted worktree
triggers the incremental `nav.rescan()` (warm passes skip read+hash via
the stat fingerprint persisted at hash time, issue #42 — the sha stays
the content identity, so unchanged files embed nothing) plus the
graph/fns sync before the tool answers. Embedding failures never crash
the call: one stderr warning, a 60s retry cooldown, and the tool answers
from the current index. `watch_interval_s` > 0 moves the polling into a
daemon thread (~2s quiet debounce before each rescan) so indexing happens
even with no tool traffic; absent or `0` keeps gate-on-read only. Stdlib
`threading`/`time`/`os` throughout — no watchdog dependency.