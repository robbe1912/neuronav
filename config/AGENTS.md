# AGENTS.md — config/

Named config profiles. One neuronav install indexes many projects; each
project gets a profile here and its MCP entry pins the profile via
`NEURONAV_CONFIG` (see `tools/wire-project.ps1`).

## Selection

- Default: `config.json` at the repo ROOT (gitignored, machine-local) — the
  primary target repo.
- Alternates: `config/<name>.json`, selected by setting `NEURONAV_CONFIG`
  to the profile's ABSOLUTE path. `nav._apply_config` runs once at import
  (and again on `nav.py --config <path>`, which also exports the var so
  sibling modules and subprocesses agree).
- A relative `"root"` resolves against the config file's own directory —
  shipped profiles stay machine-portable (`config/neuronav.json` uses
  `"root": ".."` to index this repo itself).

## Fields (consumed by `nav._apply_config`)

| field | default | meaning |
|---|---|---|
| `root` | parent of the install | target repo root (relative -> resolve against the profile's dir) |
| `state_dir` | `<root>/.neuronav` | ALL generated state for the profile: `chroma/` vectordb, `base/` shards, `graph.html` bake (relative -> resolve against the profile's dir) |
| `collection` | `"main"` | chroma collection name; fn-level index lives at `<collection>-fns` |
| `include_dirs` | `scripts, scenes, VFX, ai, tests, tools` | walked under root |
| `extensions` | `.gd, .tscn` | suffixes kept (must be registered in `extractors/` to parse) |
| `exclude_dirs` | `.git, __pycache__` | pruned from the directory walk |
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

`include_dirs` and `extensions` are additive filters on top; `collection`
namespacing means two profiles never share vectors.
