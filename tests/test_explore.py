# explore tool: one call returns line-numbered source + call flow + budget
# discipline (codegraph's measured agent-wayfinding discipline, adapted).
# Run in its own process:
#   NEURONAV_CONFIG=<repo>/config/neuronav.json python -X utf8 tests/test_explore.py
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault(
    "NEURONAV_CONFIG", str(Path(__file__).resolve().parents[1] / "config" / "neuronav.json")
)

import server  # noqa: E402  (binds neuronav config via NEURONAV_CONFIG)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail and not cond else ''}")
    if not cond:
        FAILURES.append(name)


def main() -> int:
    import server as s

    # Happy path: real query against the self-index.
    out = s.explore("cluster labeling", n=3)
    check("returns source slices", ("func " in out or "def " in out) and "\t" in out, out[:120])
    check("line numbers are cat -n style", any(
        ln.split("\t", 1)[0].strip().isdigit() for ln in out.splitlines() if "\t" in ln
    ))
    check("file header marks each block", "**" in out and ".py" in out)
    check("call flow header present", "callers:" in out or "callees:" in out or "no callers" in out)
    check("budget respected", len(out) <= 22000, f"{len(out)} chars")

    # Degraded path: embedding backend down must NOT error — deterministic
    # fallback keeps the tool useful (success-shaped, never isError).
    orig = s.graph.find_functions

    def _boom(*a, **k):
        raise ConnectionError("ollama down")

    try:
        s.graph.find_functions = _boom
        out2 = s.explore("cluster labeling", n=3)
        check("ollama-down still returns guidance+hits, not error",
              ("func " in out2 or "def " in out2) and "degraded" in out2.lower(), out2[:200])
    finally:
        s.graph.find_functions = orig

    # Unknown concept: guidance, not failure.
    out3 = s.explore("zzz_no_such_concept_qq", n=3)
    check("no-hit returns next-step guidance", "rescan" in out3 or "grep" in out3.lower(), out3[:150])

    # Tool annotations: readOnlyHint must reach the MCP surface (client
    # permission gates read it).
    import asyncio

    async def _tools():
        return await s.mcp.list_tools()

    tools = asyncio.run(_tools())
    ann = {t.name: t.annotations for t in tools}
    ro_bad = {k: v for k, v in ann.items()
              if k != "rescan" and not (v and v.readOnlyHint)}
    check("all read-only tools carry readOnlyHint", not ro_bad, str(ro_bad))
    check("rescan is the only unannotated (write) tool",
          ann.get("rescan") is None and len(ann) >= 10)
    check("explore tool registered", "explore" in ann)

    print(f"\n{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
