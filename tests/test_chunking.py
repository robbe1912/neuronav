# cAST-style size-aware doc chunking (issue #76) — hermetic, no config,
# no index: exercises the pure chunking/merging helpers in graph.py and
# the model-level merge/split primitives over synthetic fns. Run:
#   .venv/Scripts/python.exe -X utf8 tests/test_chunking.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from extractors.model import Func, FileSym, add_class_ctx
import graph  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


# ---- model helpers ---------------------------------------------------------

funcs = {
    "carrier": Func(path="m.py", name="carrier", line=1, body="def carrier():\n    x = 1\n"),
    "micro": Func(path="m.py", name="micro", line=5, body="def micro():\n    return 1\n"),
    "micro2": Func(path="m.py", name="micro2", line=9, body="def micro2():\n    return 2\n"),
    "monster": Func(path="m.py", name="monster", line=11, body="def monster():\n    a = 1\n"),
}
add_class_ctx(funcs, "carrier", [funcs["micro"], funcs["micro2"]], siblings=["micro2"])
check("add_class_ctx folds siblings", funcs["carrier"].kind == "class_ctx", str(funcs["carrier"].kind))
check("add_class_ctx excludes brothers", [m[0] for m in funcs["carrier"].members] == ["micro"], str(funcs["carrier"].members))
check("add_class_ctx discards no-member class", "loner" not in funcs)

sig = "monster(a, b)"
big_body = "def monster(a, b):\n" + "\n".join(
    f"    v{i} = " + "x" * 60 for i in range(64)
) + "\n"
mfn = Func(path="m.py", name="monster", line=11, body=big_body)
blocks = [1] + [i for i in range(2, 65) if i % 16 == 0]
parts1 = graph._chunks(mfn, sig, blocks)
parts2 = graph._chunks(mfn, sig, blocks)
check("_chunks is deterministic", parts1 == parts2, str(len(parts1)))
check("_chunks splits a monster, signature on every chunk",
      len(parts1) >= 2 and all(t.startswith(sig) for t, _ in parts1),
      str([len(t) for t, _ in parts1]))
check("_chunks respects the retrieval cap",
      all(len(t) <= int(graph.MONSTER_FN_CHARS) for t, _ in parts1),
      str(max(len(t) for t, _ in parts1)))
check("_chunks lines are body-ordered",
      [ln for _, ln in parts1] == sorted(ln for _, ln in parts1),
      str([ln for _, ln in parts1]))

# ---- statement-block boundary detection ------------------------------------

# body PROPER starts at the first statement (line 0), base = its indent
body = """    a = 1
    b = 2
    if a:
        c = 3
    d = 4
    return d
"""
check("offsets see each top-level stmt", graph._chunk_line_offsets(body) == [1, 2, 4, 5],
      str(graph._chunk_line_offsets(body)))

# sequential statements at the same indent as the first are separate blocks
body2 = """    x = (1 +
         2)
    y = x
    return y
"""
offs = graph._chunk_line_offsets(body2)
check("offsets skip bracket continuation bodies", offs == [2, 3], str(offs))

# string content at column 0 is NOT a boundary (triple-quoted block)
body3 = """    s = \'\'\'
col0 inside string
        deeper inside
\'\'\'
    return s
"""
offs3 = graph._chunk_line_offsets(body3)
check("offsets skip triple-quoted content", offs3 == [4], str(offs3))
# heredoc INSIDE a triple-quoted string stays inert even at lazy indent
body3b = """    s = \'\'\'
col0
    col4 too
\'\'\'
    return s
"""
offs3b = graph._chunk_line_offsets(body3b)
check("offsets keep heredoc contents inert", offs3b == [4], str(offs3b))
# a triple string opened on the FIRST statement line is honored too
body3c = """    s = \'\'\'doc
line
\'\'\'
    return s
"""
offs3c = graph._chunk_line_offsets(body3c)
check("offsets honor triple open on stmt 0", offs3c == [3], str(offs3c))

# brace closure and else are boundaries; deeper-level lines are not
gd_body = """\tdo_thing()
\tif a:
\t\tb()
\telse:
\t\tc()
\tdone()
"""
check("offsets handle gd else", graph._chunk_line_offsets(gd_body) == [1, 3, 5],
      str(graph._chunk_line_offsets(gd_body)))

# ---- monster splitting -----------------------------------------------------

sig = "monster(a, b)"
lines_body = ["def monster(a, b):", '    """Do the big thing."""']
lines_body += ["    stmt_%d = %d" % (i, i) for i in range(200)]
monster = Func(path="m.py", name="monster", line=3, body="\n".join(lines_body) + "\n")
docs = graph._chunked_docs(FileSym(path="m.py", ext=".py", class_name="M"), monster, sig)
check("monster splits", len(docs) >= 2, str(len(docs)))
check("monster chunks signature-led",
      all(d.startswith(sig) for d, _l, _k in docs), str([(k[:20]) for _d, _l, k in docs]))
check("monster chunks within cap",
      all(len(d) <= graph.MONSTER_FN_CHARS for d, _l, _k in docs),
      str([len(d) for d, _l, _k in docs]))
keys = [k for _d, _l, k in docs]
check("monster chunk ids deterministic", keys == sorted(keys) and len(set(keys)) == len(keys), str(keys))
check("monster chunk lines point at first stmt", docs[0][1] == 4, str(docs[0][1]))
check("monster intro carried on later chunks",
      all("# Do the big thing" in d for d, _l, _k in docs[1:]), str([d.splitlines()[1] for d, _l, _k in docs]))

# repeated runs identical (determinism)
docs2 = graph._chunked_docs(FileSym(path="m.py", ext=".py", class_name="M"), monster, sig)
check("monster split deterministic", [(d, l, k) for d, l, k in docs] == [(d, l, k) for d, l, k in docs2])

# small fn stays ONE doc, unchanged key
small = Func(path="m.py", name="small", line=1, body="def small():\n    return 1\n")
one = graph._chunked_docs(FileSym(path="m.py", ext=".py", class_name="M"), small, "small()")
check("small fn keeps single doc + key", bool(one) and one[0][2] == "small", str(one))

# the micro merge must NOT touch kind — _chunk_plan leaves kind alone when
# no carrier exists; only membership growth is allowed
gd0 = FileSym(path="c.gd", ext=".gd", class_name="C0")
gd0.funcs = {
    "only_micro": Func(path="c.gd", name="only_micro", line=3, body="func only_micro():\n\treturn 0\n"),
}
graph._chunk_plan(gd0, gd0.funcs)
check("lone micro keeps raw kind/doc",
      gd0.funcs["only_micro"].kind == "raw" and gd0.funcs["only_micro"].members == [],
      str([(n, fn.kind) for n, fn in gd0.funcs.items()]))

# ---- class-context merge ----------------------------------------------------

gd = FileSym(path="c.gd", ext=".gd", class_name="C")
gd.funcs = {
    "first": Func(path="c.gd", name="first", line=1, body="func first():\n\tvar a = 1\n\treturn a\n"),
    "micro": Func(path="c.gd", name="micro", line=8, body="func micro():\n\treturn 0\n"),
}
# force all micro
for fn in gd.funcs.values():
    fn.body = "func x():\n\tpass\n"
gd.funcs["first"].line = 1
gd.funcs["micro"].line = 8
graph._chunk_plan(gd, gd.funcs)
check("whole-class micro stays standalone (no carrier)",
      all(fn.kind == "raw" for fn in gd.funcs.values()), str([(n, fn.kind) for n, fn in gd.funcs.items()]))

# a non-micro carrier above merges the micro
gd2 = FileSym(path="c.gd", ext=".gd", class_name="C2")
gd2.funcs = {
    "big": Func(path="c.gd", name="big", line=1,
                body="func big():\n" + "\twork()\n" * 40),
    "getter": Func(path="c.gd", name="getter", line=50, body="func getter() -> int:\n\treturn 1\n"),
}
# sanity: micro classification
check("test carrier big is not micro", not graph._is_micro(gd2.funcs["big"]), str(len(gd2.funcs["big"].body)))
check("test fold target is micro", graph._is_micro(gd2.funcs["getter"]), str(len(gd2.funcs["getter"].body)))
graph._chunk_plan(gd2, gd2.funcs)
check("micro merges into nearest non-micro above",
      gd2.funcs["big"].kind == "class_ctx" and [m[0] for m in gd2.funcs["big"].members] == ["getter"],
      str([(n, fn.kind, [m[0] for m in fn.members]) for n, fn in gd2.funcs.items() if fn.kind]))
fs = gd2
doc = graph._fn_doc(fs, gd2.funcs["big"], "big()")
check("class_ctx doc carries member banner",
      "-- getter --" in doc and "return 1" in doc,
      doc.splitlines()[0] + " | " + doc.splitlines()[1] + " | " + doc.splitlines()[2])

# ---- micro detection --------------------------------------------------------

micro_fn = Func(path="m.py", name="m", line=1, body="def m():\n    return 1\n")
check("is_micro true for stub", graph._is_micro(micro_fn))
# a long param list must not mask a stub body: measured past the signature
wide = Func(path="m.py", name="w", line=1,
            body="def w(" + ",".join(f"p{i}: int" for i in range(40)) + "):\n    return 1\n")
check("is_micro measured past signature", graph._is_micro(wide))
big_fn = Func(path="m.py", name="b", line=1, body="def b():\n" + "    code = work()\n" * 20)
check("is_micro false for real body", not graph._is_micro(big_fn))
# a raw GDScript fragment (body proper, no signature) is measured whole
frag = Func(path="c.gd", name="acc", line=9, body="\twork\n" * 60)
check("is_micro measures raw fragment whole", not graph._is_micro(frag))

# ---- knob scale (nav CHUNK_CAST; sync_functions passes it, default off) ----

# scale 0 = OFF: every fn keeps the single historical doc under its own
# key — the byte-identical pre-#141 recall surface
legacy = graph._chunked_docs(FileSym(path="m.py", ext=".py", class_name="M"), monster, "monster(a, b)", 0.0)
check("scale 0 keeps single legacy doc + key",
      len(legacy) == 1 and legacy[0][2] == "monster"
      and legacy[0][0] == "m.py :: func monster(monster(a, b))\n" + monster.body[:6000],
      str(legacy[0][2]))
check("scale 2 defers the split threshold",
      len(graph._chunked_docs(FileSym(path="m.py", ext=".py", class_name="M"), monster, "monster(a, b)", 2.0)) == 1,
      str(len(graph._chunked_docs(FileSym(path="m.py", ext=".py", class_name="M"), monster, "monster(a, b)", 2.0))))

# chunk ids ride the fn-key grammar: fn_key(rel, "name#chunkN") keeps
# split_key() resolving the parent file
check("chunk ids ride the fn-key grammar",
      graph.fn_key("a/b.py", "m#chunk1") == "a/b.py::m#chunk1"
      and graph.split_key("a/b.py::m#chunk1") == "a/b.py",
      graph.fn_key("a/b.py", "m#chunk1"))

# rank-collapse: name#chunkN siblings fold into one (path, parent) slot
rows = [
    {"key": "a.py::m#chunk1", "path": "a.py", "func": "m", "line": 1},
    {"key": "a.py::m#chunk2", "path": "a.py", "func": "m", "line": 40},
    {"key": "b.py::g", "path": "b.py", "func": "g", "line": 3},
    {"key": "c.py::h#chunk1", "path": "c.py", "func": "h", "line": 9},
]
check("fold collapses chunk siblings, keeps rank order",
      [r["key"] for r in graph._fold_parents(rows, 3)] ==
      ["a.py::m#chunk1", "b.py::g", "c.py::h#chunk1"],
      str([r["key"] for r in graph._fold_parents(rows, 3)]))
check("fold is a no-op for unique fns",
      [r["key"] for r in graph._fold_parents([rows[0], rows[2], rows[3]], 4)] ==
      ["a.py::m#chunk1", "b.py::g", "c.py::h#chunk1"],
      str([r["key"] for r in graph._fold_parents([rows[0], rows[2], rows[3]], 4)]))

print()
if FAILS:
    print(f"{len(FAILS)} failure(s): {FAILS}")
    sys.exit(1)
print("0 failure(s)")