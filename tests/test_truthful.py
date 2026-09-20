# truthful failures (issues #115 + #116): model/config errors must not
# masquerade as "backend unreachable", the BM25F cache must never alias
# two corpora through a recycled dict address, non-ASCII identifiers
# must tokenize, and the four misleading server failure shapes (raw
# find_functions error, GDScript-only duplicates, masked _ctx_semantic
# errors, the dead explore budget cliff) must answer truthfully.
# Run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_truthful.py
# (self-selects the self-index config below; the shell must not
# pre-export NEURONAV_CONFIG)
import asyncio  # issue #315: semantic_search is an async shell in-process
import contextlib
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault(
    "NEURONAV_CONFIG", str(Path(__file__).resolve().parents[1] / "config" / "neuronav.json")
)
os.environ.setdefault("NEURONAV_EMBED_FAKE", "1")

from extractors.model import FileSym, Func  # noqa: E402

import graph  # noqa: E402  (binds config via NEURONAV_CONFIG)
import navconfig, navstore
import recall  # noqa: E402
import server  # noqa: E402
import server_clusters  # noqa: E402  # context renderers' home since #345



from harness import finish, styled

check = styled("wide")  # byte pin: two-space tag, fail-only detail

MISMATCH_MSG = (
    "index was built with embed model 'oldm' (provider 'ollama') but "
    "config says 'newm' (provider 'ollama') — run `python nav.py drop` "
    "then rescan"
)


def _reason(exc: Exception) -> str | None:
    try:
        return navstore.embed_failure_reason(exc)
    except AttributeError:
        return None


def _http_exc(code: int, body: str) -> Exception:
    import httpx

    req = httpx.Request("POST", "http://127.0.0.1:11434/api/embed")
    resp = httpx.Response(code, content=body.encode(), request=req)
    return httpx.HTTPStatusError(f"{code}", request=req, response=resp)


def _StubFS(path: str, body: str, cls: str = "", ext: str = ".gd") -> FileSym:
    """Hermetic corpus file on the real parse contract: BM25F reads
    fs.surface since #365, so a hand-rolled field stand-in would
    re-spell the symbol-surface law (and drift from production
    shapes)."""
    fs = FileSym(path=path, ext=ext, class_name=cls)
    fs.funcs["run"] = Func(path=path, name="run", line=1, body=body)
    return fs


def _files(call: str) -> dict:
    """Two-file corpus whose BODIES tokenize differently per `call` —
    the lexical scores must track the dict's actual contents."""

    def mk(path: str, n: str) -> _StubFS:
        return _StubFS(path, f"func run():\n\tcall_{call}_{n}()\n\treturn {n}\n")

    return {"alpha.gd": mk("alpha.gd", "one"), "beta.gd": mk("beta.gd", "two")}


def main() -> int:
    import explore as xp

    # ---- #115a: truthful degradation labels ------------------------------

    r = _reason(RuntimeError(MISMATCH_MSG))
    check("classifier names _check_model aborts as model/config mismatch",
          r is not None and "model/config mismatch" in r and "oldm" in r
          and "newm" in r and "drop" in r, str(r))

    r = _reason(_http_exc(404, '{"error": "model \'bogus\' not found"}'))
    check("classifier names model+provider on 404 model-not-found",
          r is not None and navconfig.EMBED_MODEL in r and navconfig.EMBED_PROVIDER in r
          and "model/config error, not connectivity" in r, str(r))

    r = _reason(_http_exc(500, '{"error": "internal"}'))
    check("classifier: plain 5xx is an endpoint error, NOT unreachable",
          r is not None and "endpoint error" in r and "unreachable" not in r, str(r))

    r = _reason(ConnectionError("connection refused"))
    check("classifier: transport failures stay 'unreachable'",
          r is not None and r == "embedding backend unreachable (ConnectionError)", str(r))

    # recall.search must degrade BM25F-only WITH the true reason, not
    # relabel a config abort as an outage
    orig_vr = recall._vector_ranks

    def _mismatch_vr(query, depth):
        raise RuntimeError(MISMATCH_MSG)

    recall._vector_ranks = _mismatch_vr
    try:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            hits = recall.search("cluster labeling", k=4)
    finally:
        recall._vector_ranks = orig_vr
    check("recall degrades to BM25F-only under model mismatch (#115)",
          bool(hits) and all(h.get("degraded") is True for h in hits),
          str(hits[:1]))
    check("every hit carries the mismatch as degraded_reason (#115)",
          bool(hits) and all(h.get("degraded_reason", "").startswith(
              "embed model/config mismatch") for h in hits),
          str(hits[0].get("degraded_reason")))
    check("stderr names the mismatch, never 'unreachable' (#115)",
          "index was built with embed model" in err.getvalue()
          and "unreachable" not in err.getvalue(), err.getvalue()[:200])

    def _http_vr(query, depth):
        raise _http_exc(404, '{"error": "model \'bogus\' not found"}')

    recall._vector_ranks = _http_vr
    try:
        hits2 = recall.search("cluster labeling", k=4)
    finally:
        recall._vector_ranks = orig_vr
    check("HTTP 404 model error surfaces as model/config, named (#115)",
          bool(hits2) and "model/config error" in hits2[0].get("degraded_reason", "")
          and navconfig.EMBED_MODEL in hits2[0].get("degraded_reason", ""),
          str(hits2[0].get("degraded_reason")))

    # ---- #115b: no stale lexical index through a recycled dict id --------

    f1 = _files("one")
    check("cache still hits for a live corpus (pure perf)",
          recall._cached_index(f1) is recall._cached_index(f1))
    check("cached index scores bit-match a fresh build (A/B)",
          recall._cached_index(f1).scores("call_one_alpha")
          == recall.BM25F(f1).scores("call_one_alpha"))

    aliased = None
    for _ in range(300):
        a = _files("one")
        recall._cached_index(a)
        aid = id(a)
        del a  # CPython refcount free -> dict free list (LIFO)
        cand: dict = {}
        if id(cand) == aid:
            cand.update(_files("two"))
            aliased = cand
            break
    if aliased is not None:
        served = recall._cached_index(aliased)
        fresh = recall.BM25F(aliased)
        check("recycled dict address never serves a stale index (#115)",
              served.scores("call_two_alpha") == fresh.scores("call_two_alpha"),
              f"served={served.scores('call_two_alpha')[:1]} "
              f"fresh={fresh.scores('call_two_alpha')[:1]}")
    else:
        # the fix pins the cached corpus, so the allocator can never
        # hand a live cache entry's address to a second dict — exercise
        # the same-path content swap through a fresh dict instead
        other = dict(_files("two"))
        check("same-path corpus swap never serves the old tokenization (#115)",
              recall._cached_index(other).scores("call_two_alpha")
              == recall.BM25F(other).scores("call_two_alpha"))

    # ---- #115c: Unicode-aware tokenizer -----------------------------------

    toks = recall._tokens("über café 玩家碰撞")
    check("unicode identifiers tokenize (über/café kept, #115)",
          "über" in toks and "café" in toks and "ber" in toks, str(toks))
    check("CJK runs tokenize whole (#115)", "玩家碰撞" in toks, str(toks))
    check("pure CJK no longer tokenizes to [] (#115)",
          recall._tokens("玩家碰撞检测") != [], str(recall._tokens("玩家碰撞检测")))
    check("ASCII tokenization is byte-identical to the old splitter",
          recall._tokens("BM25F parseGd XMLReader foo_bar a_b v2_beta")
          == ["bm", "25", "parse", "gd", "xml", "reader", "foo", "bar", "beta"],
          str(recall._tokens("BM25F parseGd XMLReader foo_bar a_b v2_beta")))

    uni = {"碰撞检测.gd": _StubFS(
        "碰撞检测.gd", "func 玩家碰撞():\n\treturn 玩家\n", cls="玩家")}
    sc = recall.BM25F(uni).scores("玩家碰撞")
    check("CJK identifiers are searchable via BM25F fields (#115)",
          bool(sc) and sc[0][0] == "碰撞检测.gd", str(sc[:1]))

    # ---- #116 case 2: duplicates is not GDScript-only ---------------------

    synth = graph.Graph.__new__(graph.Graph)
    body = "func go():\n\tstep_one()\n\tstep_two()\n\treturn done\n"
    synth.files = {
        "a/player.gd": _StubFS("a/player.gd", body, ext=".gd"),
        "b/player.py": _StubFS("b/player.py", body, ext=".py"),
        "c/player.cpp": _StubFS("c/player.cpp", body, ext=".cpp"),
        "d/other.gd": _StubFS("d/other.gd", "func go():\n\tunrelated()\n", ext=".gd"),
    }
    groups = synth.exact_duplicates(limit=10)
    members = sorted(m for grp in groups for m in grp["members"])
    check("duplicate scan covers .py and .cpp bodies (#116)",
          "b/player.py::run" in members and "c/player.cpp::run" in members,
          str(members))
    check("identical cross-language bodies group together (#116)",
          any(set(grp["members"]) == {"a/player.gd::run", "b/player.py::run",
                                      "c/player.cpp::run"} for grp in groups),
          str(groups))

    # ---- #116 case 4: explore budget cliff is live, output capped ---------

    g = graph.get_graph()
    path, fname, fn = max(
        ((p, name, fn) for p, fs in g.files.items() for name, fn in fs.funcs.items()),
        key=lambda t: (len(t[2].body.splitlines()), t[0], t[1]),
    )
    seeds = [{"path": path, "func": fname, "line": fn.line, "score": 0.9}]
    thin = xp._symbol_slices(g, seeds, False, xp.MIN_HIT_CAP - 1, None)
    check("thin budget demotes to a pointer line (#116)",
          "not shown" in thin and len(thin) <= xp.MIN_HIT_CAP + 200,
          f"{len(thin)} chars, pointer={'not shown' in thin}")
    two = xp._symbol_slices(
        g, seeds + [dict(seeds[0], score=0.89)], False, xp.MIN_HIT_CAP + 100, None)
    check("thin fair-share demotes every seed, budget respected (#116)",
          two.count("not shown") == 2 and len(two) <= xp.MIN_HIT_CAP + 400,
          f"{len(two)} chars, pointers={two.count('not shown')}")

    capped = None
    try:
        capped = xp._cap(["handle line\n" + "x" * 15_000, "y" * 15_000])
    except AttributeError:
        pass
    check("overshoot cuts at a line boundary, marker keeps a handle (#116)",
          capped is not None and len(capped) <= xp.TOTAL_CAP
          and "budget-capped" in capped and "handle line" in capped,
          str(capped)[-120:] if capped else "no _cap")

    # ---- #116 case 1 + case 3: server surfaces ---------------------------

    orig_ar = server._auto_rescan
    rail_hits = {"n": 0}

    def _gated_ar() -> None:  # unit-focus: the failure shapes — and a
        rail_hits["n"] += 1  # #359 seam probe: the family handler must

    # observe this rebind (rails must not bind as copies)
    server._auto_rescan = _gated_ar
    try:
        orig_ff = graph.find_functions

        def _down(*a, **k):
            raise RuntimeError("simulated backend outage")

        graph.find_functions = _down
        try:
            out = server.find_functions("rescan", 6)
        except Exception as exc:  # the base bug: raw MCP error
            out = f"{type(exc).__name__}: {exc}"
        finally:
            graph.find_functions = orig_ff
        check("find_functions degrades to lexical fallback, not an error (#116)",
              isinstance(out, str) and out.startswith("degraded:")
              and "lexical fallback" in out
              and any(".py#" in ln or ".gd#" in ln for ln in out.splitlines()),
              str(out)[:200])

        check("server._auto_rescan rebind reaches the family handler (#359)",
              rail_hits["n"] >= 1,
              f"{rail_hits['n']} rail hits — seam frozen by a rail copy?")
        def _mm(*a, **k):
            raise RuntimeError(MISMATCH_MSG)

        graph.find_functions = _mm
        try:
            out2 = server.find_functions("rescan", 6)
        except Exception as exc:
            out2 = f"{type(exc).__name__}: {exc}"
        finally:
            graph.find_functions = orig_ff
        check("find_functions degraded header names the mismatch (#115/#116)",
              isinstance(out2, str) and out2.startswith("degraded:")
              and "model/config mismatch" in out2 and "oldm" in out2
              and "unreachable" not in out2, str(out2)[:200])

        orig_col = navstore._collection

        def _mm_col():
            raise RuntimeError(MISMATCH_MSG)

        navstore._collection = _mm_col
        try:
            sem = None
            try:
                sem = server_clusters._ctx_semantic("recall.py")
            except Exception as exc:
                sem = f"{type(exc).__name__}: {exc}"
            lines = server_clusters._render_semantic("recall.py")
        finally:
            navstore._collection = orig_col
        check("_ctx_semantic separates empty-miss from backend failure (#116)",
              isinstance(sem, tuple) and sem[1] is not None
              and "model/config mismatch" in sem[1], str(sem)[:160])
        check("context() semantic block names the mismatch, not a bogus "
              "rescan hint (#116)",
              any("degraded" in ln and "model/config mismatch" in ln
                  for ln in lines)
              and not any("file not embedded" in ln for ln in lines),
              str(lines)[:200])

        recall._vector_ranks = _mismatch_vr
        try:
            sm = asyncio.run(server.semantic_search("cluster labeling", 4))
        finally:
            recall._vector_ranks = orig_vr
        check("semantic_search degraded header carries the true reason (#115)",
              "degraded: BM25F-only (embed model/config mismatch" in sm,
              sm[:160])
    finally:
        server._auto_rescan = orig_ar

    finish()


if __name__ == "__main__":
    raise SystemExit(main())
