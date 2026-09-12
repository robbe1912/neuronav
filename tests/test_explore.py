# explore tool: one call returns line-numbered source + call flow + budget
# discipline (codegraph's measured agent-wayfinding discipline, adapted),
# with 100-line windowed slices + continuation anchors (issue #69).
# Run in its own process:
#   NEURONAV_CONFIG=<repo>/config/neuronav.json python -X utf8 tests/test_explore.py
# (the suite self-selects that config via setdefault below; the shell must
# not pre-export NEURONAV_CONFIG)
import os
import re
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
    import explore as xp
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
    check("funnel slices advertise continuation anchors", "pass anchor=" in out, out[-200:])

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

    # Funnel shape: constant repo-map preamble and cluster map precede the
    # query-dependent file shortlist and symbol slices. Preamble must not
    # vary with seed mode (degraded) or seed absence (no-hit path).
    pre = out.split("== clusters ==")[0]
    check("repo map preamble present and map-shaped",
          pre.startswith("== repo map ==") and ".py:" in pre and "(" in pre)
    check("preamble constant across seed modes (vector vs degraded)",
          pre == out2.split("== clusters ==")[0])
    check("no-hit path still carries the orientation preamble",
          out3.startswith("== repo map =="))
    check("cluster map section present", "== clusters ==" in out and "- " in out)
    check("file shortlist section present", "== file shortlist ==" in out)
    check("symbol section header present", "== symbols ==" in out)

    # Windowed slices (issue #69): 100-line cap + deterministic continuation
    # anchors. Hermetic core first: a synthetic 250-line file under .tmp
    # (gitignored, on the index exclude list) drives _slice directly — no
    # index needed for those; run()-level paging targets indexed files only
    # (issue #105 scope guard).
    fixture_rel = ".tmp/test_explore_window.py"
    fixture = Path(xp.nav.ROOT) / fixture_rel
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text(
        "".join(f"line {i:03d} {'x' * 8}\n" for i in range(1, 251)), encoding="utf-8"
    )
    try:
        codelines = lambda sl: [ln for ln in sl.splitlines() if xp._CODE_RE.match(ln)]

        w1 = xp._slice(fixture_rel, 1, 250, 20_000)
        check("window capped at 100 lines", len(codelines(w1)) == 100, f"{len(codelines(w1))}")
        check("window anchor names the next window", w1.rstrip().endswith(
            f'pass anchor="{fixture_rel}:101-200" to continue'))
        check("anchor counts remaining lines", "+150 more lines" in w1)
        check("windowing deterministic (byte-identical rerun)",
              xp._slice(fixture_rel, 1, 250, 20_000) == w1)

        w2 = xp._slice(fixture_rel, 101, 200, 20_000)
        check("continuation window starts at line 101", w2.startswith("101\t"))
        check("continuation ends with next-window anchor",
              "+50 more lines" in w2 and f'anchor="{fixture_rel}:201-250"' in w2)

        w3 = xp._slice(fixture_rel, 201, 250, 20_000)
        check("final window: no anchor past EOF",
              len(codelines(w3)) == 50 and "more lines" not in w3)

        wc = xp._slice(fixture_rel, 1, 250, 600)
        shown = len(codelines(wc))
        m = re.search(r'anchor="[^"]*:(\d+)-(\d+)"', wc)
        check("char cap truncates before line cap", 0 < shown < 100, f"{shown}")
        check("anchor continues from the char-capped cut",
              m is not None and m.group(1) == str(shown + 1))

        # anchor paging through run(): next window without the funnel.
        # Paging is confined to the index (issue #105), so the target is an
        # indexed file derived from the loaded graph, not the excluded
        # .tmp fixture.
        gidx = s.graph.get_graph()
        _nlines = lambda p: len((Path(xp.nav.ROOT) / p).read_text(
            encoding="utf-8", errors="replace").splitlines())
        ipath = max(sorted(gidx.files), key=_nlines)
        check("self-index has a file tall enough to page (non-vacuous)",
              _nlines(ipath) >= 200, f"{ipath} has {_nlines(ipath)} lines")
        page = xp.run("ignored query", anchor=f"{ipath}:101-200")
        check("anchor page serves the requested window",
              page.startswith(f"** {ipath} **") and "\n101\t" in page)
        check("anchor page re-orients nothing (no repo map/clusters)",
              "== repo map ==" not in page and "== clusters ==" not in page)
        check("anchor page still budget-capped", len(page) <= xp.TOTAL_CAP)
        check("bad anchor gets guidance, not error",
              "unreadable anchor" in xp.run("q", anchor="garbage"))
        n = _nlines(ipath)
        check("stale anchor (past EOF) gets guidance, not error",
              "anchor window is empty" in xp.run(
                  "q", anchor=f"{ipath}:{n + 100}-{n + 110}"))

        # Scope guard (issue #105): an existing-but-unindexed file, a ../
        # traversal, and an absolute path all get guidance — never file text.
        esc = xp.run("q", anchor=f"../../{fixture_rel}:1-5")
        abs_ = xp.run("q", anchor=f"{fixture.as_posix()}:1-5")
        unidx = xp.run("q", anchor=f"{fixture_rel}:1-5")
        check("unindexed/escaped anchors get guidance, not file text (issue #105)",
              all("not an indexed file" in r for r in (esc, abs_, unidx))
              and "line 001" not in esc + abs_ + unidx,
              esc[:120])

        # Funnel path on real index data: the longest fn in the self-index
        # must come back as a window (height law) carrying an anchor.
        g = s.graph.get_graph()
        path, fname, fn = max(
            ((p, name, fn) for p, fs in g.files.items() for name, fn in fs.funcs.items()),
            key=lambda t: (len(t[2].body.splitlines()), t[0], t[1]),
        )
        check("self-index has a fn big enough to exercise the window "
              "(non-vacuous; re-point at the new longest fn if refactors split it)",
              len(fn.body.splitlines()) > 95, f"{path}::{fname}")
        seeds = [{"path": path, "func": fname, "line": fn.line, "score": 0.9}]
        sec = xp._symbol_slices(g, seeds, False, xp.TOTAL_CAP, None)
        check("funnel slice obeys the 100-line window",
              len(codelines(sec)) <= 100, f"{len(codelines(sec))}")
        check("funnel slice of an overflowing fn ends with an anchor",
              "more lines - pass anchor=" in sec)

        # Budget math with windows: weak second seed cliffs to a pointer
        # line and the section stays within budget + one block of slack.
        sec2 = xp._symbol_slices(
            g, seeds + [dict(seeds[0], score=0.05)], False, 5_000, None)
        check("cliff: weak hits stay pointer lines", "not shown" in sec2)
        check("budget discipline holds under windowing",
              len(sec2) <= 5_000 + xp.MAX_HIT_CAP + 200, f"{len(sec2)}")
        check("degraded marker survives windowing",
              "degraded" in xp._symbol_slices(g, seeds, True, 5_000, None).lower())
    finally:
        fixture.unlink(missing_ok=True)

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
