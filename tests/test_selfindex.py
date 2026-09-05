# neuronav self-index structural gate (no embeddings/Ollama needed) — run:
#   .venv/Scripts/python.exe -X utf8 tests/test_selfindex.py
# Pins the python-extractor invariants: the tool parses its own repo,
# registry/ENTRY_RULES indirection keeps likely-dead at zero, and the
# framework-dispatch handlers stay in review (not likely).
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["NEURONAV_CONFIG"] = str(Path(__file__).resolve().parents[1] / "config" / "neuronav.json")

import graph  # noqa: E402  (binds neuronav config via NEURONAV_CONFIG)

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


g = graph.get_graph(rebuild=True)

py_files = [f for f in g.files.values() if f.ext == ".py"]
py_funcs = sum(len(f.funcs) for f in py_files)
check("self files indexed", len(py_files) >= 10, f"{len(py_files)} py files")
check("self funcs parsed", py_funcs >= 100, f"{py_funcs} funcs")

# registry + ENTRY_RULES + module receivers resolve: python.py's own
# meta-machinery must be alive (the 9081e03a invariant)
dead = g.dead_code(100000)
likely = [c for c in dead["candidates"] if c["tier"] == "likely"]
check("self likely-dead is zero", dead["by_tier"].get("likely", 0) == 0,
      f"likely={len(likely)}" + (f" e.g. {likely[0]['path']}:{likely[0]['func']}" if likely else ""))

# server.py MCP handlers are framework dispatch: review tier, never likely
# (window covers handler funcs + their private helpers: 15 at bb4b762,
# +1 for the context overview helper)
handlers = [c for c in dead["candidates"] if c["path"] == "server.py"]
check("server handlers stay review", 5 <= len(handlers) <= 20
      and all(c["tier"] == "review" for c in handlers),
      f"{len(handlers)} handler candidates, tiers={sorted({c['tier'] for c in handlers})}")

# determinism: second build yields identical file set + edge count
g2 = graph.get_graph(rebuild=True)
check("self build deterministic",
      set(g.files) == set(g2.files)
      and sum(len(v) for v in g.edges.values()) == sum(len(v) for v in g2.edges.values()),
      f"{len(g.files)} files, {sum(len(v) for v in g.edges.values())} edges")

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
