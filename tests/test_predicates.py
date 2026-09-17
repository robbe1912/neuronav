# derived-predicate cache gates (issue #71) — hermetic fixture + self-index
# parity. No embeddings, no Ollama: build-only paths. Run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_predicates.py
#
# Pins the three cache laws:
# - parity: every cached predicate (and every tool reading one) is
#   byte-identical to the on-the-fly walk on the same graph
# - determinism: same data -> identical cache bytes across rescans
# - loud failures: corrupt/stale/missing cache rederives or errors,
#   never serves wrong answers; persist failure degrades, marked
import contextlib
import copy
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WORK = Path(tempfile.mkdtemp(prefix="pred71-"))
PROJ = WORK / "proj"
(PROJ / "config").mkdir(parents=True)
(PROJ / "config" / "neuronav.json").write_text(
    json.dumps(
        {
            "root": "..",
            "collection": "predfix",
            "state_dir": "default",
            "include_dirs": ["."],
            "extensions": [".py"],
            "exclude_dirs": [".git", "__pycache__", ".venv", ".neuronav"],
        }
    ),
    encoding="utf-8",
)

# fixture wiring:
#   app.main -> mid.mid -> core.leaf        (transitive caller chain)
#   app.main -> cyc_a.ping <-> cyc_b.pong   (2-cycle pair)
#   core.orphan, d1.twin, d2.twin           (dead; twins share a body)
#   dyn.dispatch                            (dead in a getattr file -> review)
(PROJ / "core.py").write_text(
    '''def leaf():
    total = 1
    total += 2
    return total


def orphan():
    left = "never"
    left += " called"
    return left
''',
    encoding="utf-8",
)
(PROJ / "mid.py").write_text(
    '''from core import leaf


def mid():
    value = leaf()
    return value
''',
    encoding="utf-8",
)
(PROJ / "app.py").write_text(
    '''from mid import mid

from cyc_a import ping


def main():
    one = mid()
    two = ping()
    return one + two


main()
''',
    encoding="utf-8",
)
(PROJ / "cyc_a.py").write_text(
    '''from cyc_b import pong


def ping():
    answer = pong()
    return answer
''',
    encoding="utf-8",
)
(PROJ / "cyc_b.py").write_text(
    '''from cyc_a import ping


def pong():
    echo = ping()
    return echo
''',
    encoding="utf-8",
)
TWIN_BODY = '''def twin():
    alpha = 10
    beta = 20
    return alpha + beta
'''
(PROJ / "d1.py").write_text(TWIN_BODY, encoding="utf-8")
(PROJ / "d2.py").write_text(TWIN_BODY, encoding="utf-8")
(PROJ / "dyn.py").write_text(
    '''def dispatch(target):
    target = getattr(target, "ready")
    return target
''',
    encoding="utf-8",
)

os.environ["NEURONAV_CONFIG"] = str(PROJ / "config" / "neuronav.json")

import graph  # noqa: E402  (binds the fixture config)
import nav  # noqa: E402
import predicates  # noqa: E402



from harness import finish, styled

check = styled("wide")  # byte pin: two-space tag, fail-only detail

CACHE = predicates._state_dir() / predicates.NAME


def tool_outputs(g) -> tuple:
    """Every public tool output that reads a cached predicate."""
    return (
        g.dead_code(),
        g.exact_duplicates(),
        g.repo_map(),
        g.pagerank(),
        g.file_wires(),
        g._symbol_degrees(),
        g.symbol_graph("ping", depth=3),
    )


try:
    # -- build + cache lands -----------------------------------------------------
    g = graph.get_graph(rebuild=True)
    check("fixture indexed", len(g.files) == 8, f"{sorted(g.files)}")
    check("cache written under .neuronav", CACHE.is_file(), str(CACHE))
    pred = g._pred
    check("cache families complete", pred is not None and predicates.FAMILIES <= set(pred),
          f"{sorted(predicates.FAMILIES - set(pred or {}))}")
    check("mentions skipped for non-cpp corpus", pred["mentions"] is None)

    # -- fixture semantics: the derived facts are the hand-derived truth --------
    dead_keys = {f"{r['path']}::{r['func']}": r["tier"] for r in pred["dead"]}
    check("dead tiers: orphan/twins likely, dyn file review",
          dead_keys.get("core.py::orphan") == "likely"
          and dead_keys.get("d1.py::twin") == "likely"
          and dead_keys.get("dyn.py::dispatch") == "review",
          str(dead_keys))
    twin_groups = [d for d in pred["dups"] if set(d["members"]) == {"d1.py::twin", "d2.py::twin"}]
    check("duplicates: planted twin pair grouped", len(twin_groups) == 1, str(pred["dups"]))

    # -- parity: cached vs on-the-fly on the same graph -------------------------
    cached_pred = copy.deepcopy(pred)
    with_cache = tool_outputs(g)
    g._pred = None  # force every query down the original walk
    raw_pred = predicates.derive(g)
    without_cache = tool_outputs(g)
    check("payload parity: every family identical", raw_pred == cached_pred,
          str([k for k in raw_pred if raw_pred[k] != cached_pred[k]]))
    check("tool outputs byte-identical cached vs raw", with_cache == without_cache)
    g._pred = cached_pred  # restore

    # -- determinism: two rescans -> identical bytes ----------------------------
    b1 = CACHE.read_bytes()
    mtime1 = CACHE.stat().st_mtime_ns
    g = graph.get_graph(rebuild=True)  # hit: must not rewrite
    check("cache hit leaves file untouched",
          CACHE.read_bytes() == b1 and CACHE.stat().st_mtime_ns == mtime1)
    CACHE.unlink()
    graph.get_graph(rebuild=True)
    b2 = CACHE.read_bytes()
    CACHE.unlink()
    graph.get_graph(rebuild=True)
    b3 = CACHE.read_bytes()
    check("two rescans byte-identical", b1 == b2 == b3, f"{len(b1)}/{len(b2)}/{len(b3)}")

    # -- loud failures: corruption never serves wrong answers --------------------
    doc = json.loads(CACHE.read_bytes())
    good_line = doc["pred"]["dead"][0]["line"]
    doc["pred"]["dead"][0]["line"] = good_line + 1000  # valid json, wrong value
    doc.pop("seal")
    doc["seal"] = "0" * 64  # forged seal: bytes no longer verify
    CACHE.write_bytes(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode())
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        g = graph.get_graph(rebuild=True)
    lines = [r["line"] for r in g.dead_code()["candidates"] if r["path"] == doc["pred"]["dead"][0]["path"]
             and r["func"] == doc["pred"]["dead"][0]["func"]]
    check("forged seal: loud rederive, never the tampered value",
          "rederiv" in err.getvalue() and lines == [good_line],
          f"stderr={err.getvalue().strip()[:80]!r} lines={lines}")
    check("tampered cache healed on disk",
          json.loads(CACHE.read_bytes())["pred"]["dead"][0]["line"] == good_line)

    CACHE.write_bytes(b'{"schema": 1, "fingerprint": "x", "sea')  # truncated json
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        g = graph.get_graph(rebuild=True)
    check("garbage bytes: loud rederive, tools correct",
          "rederiv" in err.getvalue() and g.dead_code() == without_cache[0])

    for bad in (b"42", b"[1, 2]", b'"nice try"'):  # valid JSON, non-object
        CACHE.write_bytes(bad)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            g = graph.get_graph(rebuild=True)
        check(f"non-object json root {bad!r}: loud rederive, tools correct",
              "rederiv" in err.getvalue() and g.dead_code() == without_cache[0])

    # 'pred' shape hole: a LIST of family names passes set.issubset, so
    # the load must type-check 'pred' itself (real fingerprint + real
    # seal over the wrong shape — only the type guard stands in the way)
    fp = json.loads(CACHE.read_bytes())["fingerprint"]
    core_doc = {"schema": 1, "fingerprint": fp, "pred": sorted(predicates.FAMILIES)}
    forged = {**core_doc, "seal": predicates._seal(core_doc)}
    CACHE.write_bytes(json.dumps(forged, sort_keys=True, separators=(",", ":")).encode())
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        g = graph.get_graph(rebuild=True)
    check("non-object 'pred' with valid seal: loud rederive, tools correct",
          "rederiv" in err.getvalue() and g.dead_code() == without_cache[0])

    # -- staleness: input change invalidates -------------------------------------
    with open(PROJ / "core.py", "a", encoding="utf-8") as fh:
        fh.write('\n\ndef stale_orphan():\n    gone = 1\n    gone += 2\n    return gone\n')
    g = graph.get_graph(rebuild=True)
    keys = {f"{r['path']}::{r['func']}" for r in g._pred["dead"]}
    check("stale fingerprint: rederive reflects the edit",
          "core.py::stale_orphan" in keys and g.dead_code()["total"] == 5,
          f"total={g.dead_code()['total']}")
    check("edited cache bytes differ", CACHE.read_bytes() != b1)

    # -- missing cache: tools still work ------------------------------------------
    CACHE.unlink()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        g = graph.get_graph(rebuild=True)
    check("missing cache: silent first-run build, tools fine",
          g.dead_code()["total"] == 5 and "rederiv" not in err.getvalue())

    # -- persist failure: marked fallback, tools correct --------------------------
    CACHE.unlink()
    if CACHE.parent.is_dir():
        shutil.rmtree(CACHE.parent)
    CACHE.parent.write_text("not a dir", encoding="utf-8")  # .neuronav as a file
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        g = graph.get_graph(rebuild=True)
    check("unwritable state dir: loud note, tools correct",
          "not persisted" in err.getvalue() and g.dead_code()["total"] == 5,
          f"stderr={err.getvalue().strip()[:90]!r}")
    os.remove(CACHE.parent)

    # -- self-index parity (real mixed corpus) ------------------------------------
    with nav.config_scope(ROOT / "config" / "neuronav.json"):
        gs = graph.get_graph(rebuild=True)
        check("self-index cache bound", gs._pred is not None)
        outs = tool_outputs(gs)
        spred = copy.deepcopy(gs._pred)
        gs._pred = None
        check("self-index payload parity", predicates.derive(gs) == spred)
        check("self-index tool outputs parity", tool_outputs(gs) == outs)
finally:
    shutil.rmtree(WORK, ignore_errors=True)



finish()
