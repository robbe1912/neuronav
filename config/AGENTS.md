# AGENTS.md — config/

Config resolution. One neuronav install indexes many projects and the
install itself stays read-only (issue #27): the project-local config
(``<project>/.neuronav/config.json``, written by ``onboard.py init``) is
the primary form; named profiles here are the tuned-override form.

## Selection (``nav._discover_config``)

1. ``$NEURONAV_CONFIG`` — explicit, always wins (absolute path).
2. ``<cwd>/.neuronav/config.json`` — project-local config; running any
   command from inside a project just works.
3. ``config.json`` at the repo ROOT (gitignored, machine-local) — the
   legacy default, honored ONLY when cwd is the checkout itself.
4. Nothing found — pure cwd defaults: root = cwd, include ``.``,
   extensions = every registered extractor suffix, excludes = the sane
   set (``.git``, ``__pycache__``, ``.venv``, ``.neuronav``,
   ``node_modules``). This is the npx shape: no config, no edits.

``nav._apply_config`` runs once at import (and again on
``nav.py --config <path>``, which also exports the var so sibling
modules and subprocesses agree). A relative ``"root"`` resolves against
the config file's own directory — shipped profiles stay machine-portable
(``config/neuronav.json`` uses ``"root": ".."`` to index this repo
itself).

## Fields (consumed by `nav._apply_config`)

| field | default | meaning |
|---|---|---|
| `root` | parent of the install | target repo root (relative -> resolve against the profile's dir) |
| `state_dir` | `<root>/.neuronav` | ALL generated state for the profile: `chroma/` vectordb, `base/` shards, `graph.html` bake (relative -> resolve against the profile's dir) |
| `collection` | `"main"` | chroma collection name; fn-level index lives at `<collection>-fns` |
| `include_dirs` | `scripts, scenes, VFX, ai, tests, tools` | walked under root |
| `extensions` | `.gd, .tscn` | suffixes kept (must be registered in `extractors/` to parse) |
| `exclude_dirs` | `.git, __pycache__` | pruned from the directory walk |
| `watch_interval_s` | `0` (off) | >0: the MCP server polls the stat gate every N seconds and auto-rescans without waiting for a tool call (issue #19) |
| `embed_url` / `embed_model` / `embed_dim` | local Ollama `/api/embed`, `qwen3-embedding:0.6b`, 1024 | embedding backend |

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
triggers the sha-gated `nav.rescan()` (unchanged files embed nothing) plus
the graph/fns sync before the tool answers. Embedding failures never crash
the call: one stderr warning, a 60s retry cooldown, and the tool answers
from the current index. `watch_interval_s` > 0 moves the polling into a
daemon thread (~2s quiet debounce before each rescan) so indexing happens
even with no tool traffic; absent or `0` keeps gate-on-read only. Stdlib
`threading`/`time`/`os` throughout — no watchdog dependency.