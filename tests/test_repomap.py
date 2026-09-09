# repo_map + pagerank: budget bound, byte determinism, rank ordering,
# god-hub saturation. Synthetic graphs only — no index, no embeddings,
# no filesystem beyond the config read at import.
# Run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_repomap.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import graph  # noqa: E402
from extractors.model import FileSym, Func  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail and not cond else ''}")
    if not cond:
        FAILURES.append(name)


def add_file(g, path, names, ext=".py", class_name=""):
    fs = FileSym(path=path, ext=ext, class_name=class_name)
    for i, nm in enumerate(names):
        fs.funcs[nm] = Func(
            path=path, name=nm, line=i + 1,
            body=f"def {nm}(): pass", params=[("x", "int")],
        )
    g.files[path] = fs
    return fs


def wire(g, src_file, src_fn, dst_file, dst_fn):
    g._edge(f"{src_file}::{src_fn}", f"{dst_file}::{dst_fn}")


def build_core():
    """a_first inserted FIRST but low-rank; z_hub inserted later but the
    wiring center; mid carries a class_name signature line."""
    g = graph.Graph()
    add_file(g, "a_first.py", ["f"])
    add_file(g, "z_hub.py", [f"h{i}" for i in range(6)])
    add_file(g, "mid.py", ["tick"], class_name="Widget")
    wire(g, "z_hub.py", "h0", "mid.py", "tick")
    for i in range(10):
        leaf = f"leaf{i:02d}.py"
        add_file(g, leaf, ["work"])
        wire(g, leaf, "work", "z_hub.py", f"h{i % 6}")
    wire(g, "leaf00.py", "work", "a_first.py", "f")
    return g


def build_hubfarm():
    """God-hub: one file with 40 funcs, each wired by its own leaf."""
    g = graph.Graph()
    add_file(g, "hub2.py", [f"m{i}" for i in range(40)])
    for i in range(40):
        leaf = f"l2_{i:02d}.py"
        add_file(g, leaf, ["run"])
        wire(g, leaf, "run", "hub2.py", f"m{i}")
    return g


def sig_line_of(m, fname):
    lines = m.splitlines()
    for i, ln in enumerate(lines):
        if ln.rstrip().endswith(f"{fname}:"):
            for nxt in lines[i + 1:]:
                if nxt.startswith(" "):
                    return nxt
                return ""
    return ""


def main() -> int:
    # -- file_wires: the fold underneath everything ---------------------------
    g = build_core()
    w = g.file_wires()
    check("self-wires dropped", "z_hub.py" not in w["z_hub.py"])
    check("wire count aggregates distinct symbol edges",
          w["leaf01.py"]["z_hub.py"] == 1 and w["z_hub.py"]["mid.py"] == 1)
    check("all files present in adjacency", len(w) == 13)

    # -- pagerank ---------------------------------------------------------------
    r = g.pagerank()
    check("ranks sum to ~1 (dangling spread uniform)",
          abs(sum(r.values()) - 1.0) < 0.02, f"sum={sum(r.values()):.4f}")
    check("hub outranks peripheral file", r["z_hub.py"] > r["a_first.py"])
    check("same instance, same floats", g.pagerank() == r)
    check("same DATA rebuilt, same floats", build_core().pagerank() == r)
    r_it = g.pagerank(iters=3)
    check("iteration cap honored (3 != 30 iters differ)", r_it != r)

    # -- repo_map: budget -------------------------------------------------------
    big = g.repo_map()
    small = g.repo_map(budget_tokens=300)
    check("default budget bounds output", 0 < len(big) and graph._toks(big) <= 2048,
          f"{graph._toks(big)} tokens")
    check("tight budget bounds output", 0 < len(small) and graph._toks(small) <= 300,
          f"{graph._toks(small)} tokens")
    # -- repo_map: determinism --------------------------------------------------
    check("repeat call byte-identical", g.repo_map() == big)
    check("rebuilt graph byte-identical", build_core().repo_map() == big)

    # -- repo_map: ordering follows PageRank, not insertion --------------------
    check("rank order in text (z_hub before a_first despite later insertion)",
          big.index("z_hub.py") < big.index("a_first.py"),
          "insertion had a_first first")
    check("high-rank file leads the map", big.splitlines()[0].startswith("z_hub.py:"))

    # -- repo_map: shape ---------------------------------------------------------
    check("file label + indented signature lines",
          "z_hub.py:" in big and "  h0(x)" in big)
    check("class_name surfaces as signature", "class Widget" in big)
    check("tighter map is a truncation of the full stream (stable order)",
          big.startswith(small))

    # -- god-hubs: appear, don't saturate ---------------------------------------
    gh = build_hubfarm()
    m2 = gh.repo_map(budget_tokens=500)
    check("god-hub present", "hub2.py:" in m2)
    check("god-hub leads the map", m2.splitlines()[0].startswith("hub2.py:"))
    hub_sig = sig_line_of(m2, "hub2.py")
    check("hub signatures capped (<= MAP_MAX_SIGS)",
          0 < hub_sig.count("(") <= graph.MAP_MAX_SIGS, hub_sig[:80])
    leaves_shown = sum(1 for ln in m2.splitlines() if ln.rstrip().endswith(".py:")
                       and ln.lstrip().startswith("l2_"))
    check("budget flows past the hub to other files", leaves_shown >= 8,
          f"{leaves_shown} leaves shown")
    check("hub's 40 funcs not all listed", "m39(" not in hub_sig)

    print(f"\n{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
