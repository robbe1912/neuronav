# cross-language verification (neuronav self-index config) — run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_crosslang.py
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

# 0. function vector index: upsert every parsed function explicitly so the
# check is deterministic regardless of collection state (bootstrap vs
# already-populated exercise the same parse+embed+upsert path)
_all = sorted(rel for rel, fs in graph._all_filesyms().items() if fs.funcs)
sync = graph.sync_functions(_all, [])
# warm-store proof: with the content-hash cache, re-syncing unchanged
# fns embeds nothing (cached counts them) — cold and warm runs both
# cover every fn
check("py fns synced", sync["fns_upserted"] + sync["fns_cached"] >= 80, str(sync))

# 0b. re-sync of the unchanged corpus must be embed-free
resync = graph.sync_functions(_all, [])
check("py fns re-sync all cached",
      resync["fns_upserted"] == 0
      and resync["fns_cached"] == sync["fns_upserted"] + sync["fns_cached"],
      f"{sync} -> {resync}")

# 0c. per-fn cache edges (.team_scratch is excluded from the file walk,
# so the scratch file never enters any suite's corpus)
import nav  # noqa: E402

def _fn_cache_edges() -> None:
    # nested helpers stay invisible to the self-index dead-scan (its
    # zero-likely-dead pin covers this file's corpus too)
    scratch_rel = ".team_scratch/xlang_fn_cache.py"
    scratch = Path.cwd() / scratch_rel

    def _write(body: str) -> None:
        scratch.parent.mkdir(exist_ok=True)
        scratch.write_text(body, encoding="utf-8")

    try:
        _write("def cache_alpha(x):\n"
               "    return x + 1\n"
               "\n"
               "\n"
               "def cache_beta(y):\n"
               "    return y * 2\n")
        r1 = graph.sync_functions([scratch_rel], [])
        check("cache cold scratch embeds both",
              r1["fns_upserted"] == 2 and r1["fns_cached"] == 0, str(r1))
        _write("# header shifts alpha's line; beta's body changes; gamma is new\n"
               "\n"
               "def cache_alpha(x):\n"
               "    return x + 1\n"
               "\n"
               "\n"
               "def cache_beta(y):\n"
               "    return y * 3\n"
               "\n"
               "\n"
               "def cache_gamma(z):\n"
               "    return z - 1\n")
        r2 = graph.sync_functions([scratch_rel], [])
        check("cache re-sync handles moved+changed+new",
              r2["fns_upserted"] == 3 and r2["fns_cached"] == 0, str(r2))
        r3 = graph.sync_functions([scratch_rel], [])
        check("cache warm scratch all cached",
              r3["fns_upserted"] == 0 and r3["fns_cached"] == 3, str(r3))
        # pure line shift must not call the embedder at all (vector reuse)
        real_embed = nav.embed

        def _boom(texts):
            raise AssertionError(
                "embedder called during pure line-move sync")

        nav.embed = _boom
        try:
            _write("# one more shift line\n"
                   "# header shifts alpha's line; beta's body changes; gamma is new\n"
                   "\n"
                   "def cache_alpha(x):\n"
                   "    return x + 1\n"
                   "\n"
                   "\n"
                   "def cache_beta(y):\n"
                   "    return y * 3\n"
                   "\n"
                   "\n"
                   "def cache_gamma(z):\n"
                   "    return z - 1\n")
            r4 = graph.sync_functions([scratch_rel], [])
            check("line-shift-only sync embeds nothing",
                  r4["fns_upserted"] == 3 and r4["fns_cached"] == 0, str(r4))
        finally:
            nav.embed = real_embed
        scratch.unlink()
        r5 = graph.sync_functions([], [scratch_rel])
        check("cache deleted file purges fns",
              r5["purged_paths"] == 1 and r5["purged_fns"] == 3, str(r5))
    finally:
        if scratch.exists():
            scratch.unlink()
        graph.sync_functions([], [scratch_rel])


_fn_cache_edges()

# 1. graph layer parsed python
py_files = [f for f in g.files.values() if f.ext == ".py"]
py_funcs = sum(len(f.funcs) for f in py_files)
check("py graph parsed", len(py_files) >= 10 and py_funcs >= 80,
      f"{len(py_files)} files, {py_funcs} funcs")

# 2. function-level semantic search stays inside this config's collection
hits = graph.find_functions("cluster label", n=3)
check("py find_functions scoped", bool(hits) and all(h["path"].endswith(".py") for h in hits),
      str([(h["path"], h["func"]) for h in hits]))

# 3. symbol graph on a python function
res = g.symbol_graph("label_cluster", 1)
check("py symbol_graph", "label_cluster" in str(res)[:400], str(res)[:160])

# 4. dead-code run is sane on python
dead = g.dead_code(100000)
check("py dead_code runs", 0 < dead["total"] < len(g.edges),
      f"total={dead['total']} edges={len(g.edges)}")

# 5. cross-language integration: a mixed .py/.h/.cpp toy tree builds one
# graph through the same registry (issue #13). Runs last: rebuilding the
# singleton graph against the temp config would retire the self-index one.
import json  # noqa: E402
import tempfile  # noqa: E402

import nav  # noqa: E402

_toy = Path(tempfile.mkdtemp(prefix="neuronav_crosslang_"))
(_toy / "widget.h").write_text(
    "class Widget : public Object {\n"
    "	GDCLASS(Widget, Object)\n"
    "public:\n"
    "	int scale_value(int p_v);\n"
    "};\n"
)
(_toy / "widget.cpp").write_text(
    '#include "widget.h"\n'
    "int Widget::scale_value(int p_v) { return p_v * 2; }\n"
    "static int unused_cpp_helper(int p_v) { return p_v; }\n"
    "void Widget::_bind_methods() {\n"
    '	ClassDB::bind_method(D_METHOD("scale_value", "v"), &Widget::scale_value);\n'
    "}\n"
)
(_toy / "helper.py").write_text("def python_side_tool():\n    return 3\n")
_cfg = Path(tempfile.gettempdir()) / "neuronav_crosslang_config.json"
_cfg.write_text(json.dumps({
    "root": _toy.as_posix(),
    "collection": "crosslang",
    "state_dir": "default",
    "include_dirs": ["."],
    "extensions": [".py", ".h", ".cpp"],
    "exclude_dirs": [],
}))
nav._apply_config(_cfg)
g2 = graph.get_graph(rebuild=True)
check("cpp registry integration", g2.files["widget.cpp"].class_name == "Widget"
      and g2.class_map.get("Widget") == "widget.h",
      f"class={g2.files['widget.cpp'].class_name}")
check("cpp qualified defs + bound alive", "scale_value" in g2.files["widget.cpp"].funcs,
      sorted(g2.files["widget.cpp"].funcs))
_dead2 = {(c["path"], c["func"]) for c in g2.dead_code(100000)["candidates"]}
check("cpp dead tier in mixed tree", ("widget.cpp", "unused_cpp_helper") in _dead2
      and ("widget.cpp", "scale_value") not in _dead2, str(sorted(_dead2)))
check("py coexists with cpp", any(f.ext == ".py" for f in g2.files.values())
      and any(f.ext in (".h", ".cpp") for f in g2.files.values()),
      str(sorted(g2.files)))

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
