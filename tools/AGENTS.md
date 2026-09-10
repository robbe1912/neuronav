# AGENTS.md — tools/

## qa_readability.py — readability / declutter gate for the visualizer

Objective ink-clutter gate over the real page: serves the repo root, loads
`graph.html` in headless Chrome (playwright, `channel="chrome"`, port 8951),
drives the same focus state as `test_viz.py` (highest-degree node stem),
and measures clutter metrics from `window.__dbg` in both layers.

```
.venv/Scripts/python.exe -X utf8 tools/qa_readability.py              # base capture -> .tmp/qa/
.venv/Scripts/python.exe -X utf8 tools/qa_readability.py --declutter  # baseline battery
.venv/Scripts/python.exe -X utf8 tools/qa_readability.py --after [--base .tmp/qa/declutter_base.json]
```

- **default**: single-view base run; writes `.tmp/qa/readability_base.json` +
  gate screenshots. Exit 0 = ok.
- **`--declutter`**: captures the multi-angle x multi-hub battery
  (5 camera angles x global + top-8 hubs) into `.tmp/qa/declutter_base.json`
  plus a sha-suffixed snapshot copy and per-subject PNGs.
- **`--after`**: reruns the same battery against the current build, prints a
  per-metric delta table, exits 1 on any clutter regression (ratchet mode).
- everything under `.tmp/qa/` is gitignored machine-local state (baselines +
  screenshots measured against whichever target the local config selects) —
  NEVER commit baselines; they embed private-target measurements.
- **`--affordance SUBJECTS`** (with `--after`): comma list of subjects whose
  sanctioned hub-affordance ink (`labelLabelPairs`/`labelWireLabels`/
  `inkCentral`) is exempt from the gate; `'tscn'` expands to all .tscn hub
  subjects, `'hubs'` to all. Structural clutter (`crossTT`, `chevCrowdHard`,
  `nodeOcclFrac`) is never exempt.

Gate calibration (empirical, do not loosen casually): label collisions flip
+/-1..2 between identical runs; `crossTT` and `nodeOcclFrac` are bit-stable;
GPU drift on `inkCentral` is ~0.006. Hard total tolerances cap the noisy
metrics; `crossTT` additionally carries a hard per-view cap (<=3/view) and totals are capped
ABSOLUTELY against the current anchor (no per-round ratchet).

Operational notes:

- Camera angles are applied to the SAVED subject base pose, never chained.
- `settle()` waits for the tween to stop (3 identical reads); probes require
  two consecutive identical census reads.
- PIL is NOT required — PNG ink analysis decodes a quarter-scale CDP capture
  in pure stdlib.
- The census payloads (`JS_3D`, `JS_2D`, `JS_DECLUT`, `HUBS_JS`, `CAM_*`
  helpers) are embedded as raw JS strings: after editing them, syntax-check
  with `node --check` on the extracted string (or paste into node) before
  running the gate.

## serve.py — no-cache dev viewer

`python tools/serve.py` - binds 127.0.0.1:8791, serves the ACTIVE config's
state dir (where the `graph.html` bake lives). Every
response carries `Cache-Control: no-store, no-cache, must-revalidate` so the
browser always refetches `graph.html` (kills the stale-build bug class when
iterating on the bake). Threaded, quiet logs.

## wire-project.ps1 — per-project MCP wiring

```
powershell -ExecutionPolicy Bypass -File tools\wire-project.ps1 -ProjectPath E:\path\to\proj [-Name x] [-IncludeDirs ...] [-Extensions ...] [-WithBaseShards]
```

One neuronav install serves many projects. Idempotent; BOM-free writes only
(`[System.IO.File]::WriteAllText` — PowerShell 5 utf8 BOM kills
`json.loads` downstream). Steps:

1. Write `config/<Name>.json` in this install (root = absolute project path,
   `state_dir = <project>\.neuronav`, optional include_dirs/extensions).
   `-Name` defaults to the project dir leaf.
2. Append `.neuronav/` to the project's `.gitignore` (idempotent, BOM-free).
3. Optionally seed from the project's own `<project>\.neuronav\base` shards
   (`-WithBaseShards` -> `import-base`), then `rescan` under the profile's
   `NEURONAV_CONFIG` (env set only for the duration). State is project-local —
   no install-side shard copying.
4. Write/update the project's `.mcp.json` `mcpServers.neuronav` entry
   (venv python, `-X utf8 server.py`, env pins the profile via
   `NEURONAV_CONFIG`); update `opencode.json` if present. Restart client
   sessions in the project afterwards.

Requires `.venv` with `chromadb httpx "mcp<2" numpy networkx scipy scikit-learn`.
Agent-side guidance snippet
for wired projects: `templates/agents-snippet.md`.
