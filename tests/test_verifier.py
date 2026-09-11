"""Kythe-style verifier fixtures (issue #66): extractor facts asserted by
structured comments inlined in the fixture sources themselves.

A goal line is `-` right after the language's own comment prefix —
`//- goal` (C++), `#- goal` (GDScript/Python), `;- goal` (tscn) — the
Kythe verifier pattern: reviewers read the assertions, not checker code.
New language extractor = new annotated fixture file.

Grammar (goals are file-scoped; `!` prefix negates any goal):
  @fn defines func | @fn calls @tgt | @fn writes member | @fn ret T
  @fn news T (cpp heap-construction site) | @fn dead
  class N | extends N | tool | signal n | member n [: T] | const n [= v]
  global n | alias n = T | literal n | entry n | init-call n
  include n (cpp quoted include) | connection sig -> method (tscn)
  script p | instance p (tscn, suffix match over res:// paths)

Hermetic by construction: goals assert extractor output only (FileSym +
cpp.scan_calls) — no graph, no config, no chroma, and deliberately no
python import goals (resolving those imports pulls in nav/chromadb; they
are pinned by test_crosslang instead). `@fn dead` is a corpus-local
claim — no other fixture body/literal/connection references the name and
it is not an extractor-level entry root — weaker than the graph tiers,
which stay pinned in test_pyhard/test_cpphard.

  .venv/Scripts/python.exe -X utf8 tests/test_verifier.py
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(HERE))

from extractors import cpp as cpp_x  # noqa: E402
from extractors import gdscript as gd_x  # noqa: E402
from extractors import python as py_x  # noqa: E402
from extractors import registry_for  # noqa: E402

FAILS: list[str] = []

# extractor-level root names for the `dead` goal (per-language virtuals)
GD_ROOTS = set(gd_x.VIRTUALS) | set(gd_x.GUT_ROOTS) | set(gd_x.ENGINE_VIRTUALS.get("", ()))
PY_ROOTS = set(py_x.PY_VIRTUALS)
CPP_ROOTS = set(cpp_x.CPP_VIRTUALS)

# goal line = `-` immediately after the native comment prefix; the text
# after it is the Kythe-shaped goal (`@anchor verb args`)
GOAL_RE = re.compile(r"^\s*(?://|#|;)-\s+(.+)$")


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        FAILS.append(name)


def _extents(fs):
    """cpp only: name -> (def line, next def line) for site attribution."""
    order = sorted(fs.funcs.items(), key=lambda kv: kv[1].line)
    out = {}
    for i, (name, fn) in enumerate(order):
        nxt = order[i + 1][1].line if i + 1 < len(order) else 1 << 30
        out[name] = (fn.line, nxt)
    return out


def _hit(sites, lo, hi, name, kind=None):
    return any(s["name"] == name and lo <= s["line"] < hi
               and (kind is None or s["kind"] == kind) for s in sites)


def eval_goal(g, fs, sites, ext, is_cpp):
    """-> (parsed, ok, detail). Unparsed goals FAIL even under `!`."""
    m = re.fullmatch(r"@(\S+) defines func", g)
    if m:
        return True, m.group(1) in fs.funcs, f"funcs={sorted(fs.funcs)}"
    m = re.fullmatch(r"@(\S+) calls @(\S+)", g)
    if m:
        f, t = m.groups()
        if is_cpp:
            if f not in ext:
                return True, False, f"no func {f}"
            lo, hi = ext[f]
            return True, _hit(sites, lo, hi, t), f"sites={[(s['name'], s['line'], s['kind']) for s in sites]}"
        fn = fs.funcs.get(f)
        if fn is None:
            return True, False, f"no func {f}"
        return True, bool(re.search(rf"(?<!\w){re.escape(t)}\s*\(", fn.body)), "call shape not in body"
    m = re.fullmatch(r"@(\S+) news (\w+)", g)
    if m and is_cpp:
        f, t = m.groups()
        if f not in ext:
            return True, False, f"no func {f}"
        lo, hi = ext[f]
        return True, _hit(sites, lo, hi, t, kind="new"), f"no new-site {t}"
    m = re.fullmatch(r"@(\S+) writes (\w+)", g)
    if m:
        fn = fs.funcs.get(m.group(1))
        if fn is None:
            return True, False, f"no func {m.group(1)}"
        return True, m.group(2) in fn.writes, f"writes={sorted(fn.writes)}"
    m = re.fullmatch(r"@(\S+) ret (\S+)", g)
    if m:
        fn = fs.funcs.get(m.group(1))
        if fn is None:
            return True, False, f"no func {m.group(1)}"
        return True, fn.ret == m.group(2), f"ret={fn.ret!r}"
    simple = [
        (r"tool", lambda: (fs.is_tool, f"is_tool={fs.is_tool}")),
        (r"class (\w+)", lambda: (fs.class_name == m2.group(1), f"class_name={fs.class_name!r}")),
        (r"extends (\w+)", lambda: (fs.extends == m2.group(1), f"extends={fs.extends!r}")),
        (r"signal (\w+)", lambda: (m2.group(1) in fs.signals, f"signals={sorted(fs.signals)}")),
        (r"global (\w+)", lambda: (m2.group(1) in fs.globals, f"globals={sorted(fs.globals)}")),
        (r"literal (\w+)", lambda: (m2.group(1) in fs.name_literals, f"literals={sorted(fs.name_literals)}")),
        (r"entry (\w+)", lambda: (m2.group(1) in fs.entry_hints, f"entry_hints={sorted(fs.entry_hints)}")),
        (r"init-call (\w+)", lambda: (m2.group(1) in fs.init_calls, f"init_calls={sorted(fs.init_calls)}")),
        (r"include (\S+)", lambda: (m2.group(1) in fs.imported_modules, f"imports={sorted(fs.imported_modules)}")),
    ]
    for pat, fn in simple:
        m2 = re.fullmatch(pat, g)
        if m2:
            ok, detail = fn()
            return True, ok, detail
    m = re.fullmatch(r"member (\w+)(?:\s*:\s*(\S+))?", g)
    if m:
        n, t = m.groups()
        ok = n in fs.members and (t is None or fs.members[n] == t)
        return True, ok, f"members={ {k: fs.members[k] for k in sorted(fs.members)} }"
    m = re.fullmatch(r"const (\w+)(?:\s*=\s*(\S+))?", g)
    if m:
        n, v = m.groups()
        ok = n in fs.consts and (v is None or fs.consts[n] == v)
        return True, ok, f"consts={ {k: fs.consts[k] for k in sorted(fs.consts)} }"
    m = re.fullmatch(r"alias (\w+)\s*=\s*(\S+)", g)
    if m:
        return True, fs.aliases.get(m.group(1)) == m.group(2), f"aliases={fs.aliases}"
    m = re.fullmatch(r"connection (\w+)\s*->\s*(\w+)", g)
    if m:
        return True, m.groups() in [tuple(c) for c in fs.connections], f"connections={fs.connections}"
    for pat, field in ((r"script (\S+)", "scripts"), (r"instance (\S+)", "instances")):
        m = re.fullmatch(pat, g)
        if m:
            p = m.group(1)
            vals = getattr(fs, field)
            ok = any(s == p or s.endswith("/" + p) for s in vals)
            return True, ok, f"{field}={vals}"
    return False, False, "unparsed goal (typo?)"


def _dead(name, own_fs, own_fn, corpus, roots):
    if name in own_fs.entry_hints or name in roots:
        return False, "entry hint / virtual root"
    if re.fullmatch(r"_((get)|(set))_\w+", name) and own_fs.ext == ".gd":
        return False, "engine property accessor"
    for rel, fs, _s, _e, _g in corpus:
        for fn_name, fn in fs.funcs.items():
            if fs is own_fs and fn_name == own_fn:
                continue
            if re.search(rf"(?<!\w){name}\b", fn.body):
                return False, f"referenced by {rel}::{fn_name}"
        if name in fs.name_literals or name in fs.init_calls:
            return False, f"referenced by {rel} literal/init-call"
        if name in {meth for _sig, meth in fs.connections}:
            return False, f"wired by {rel} connection"
    return True, ""


# pass 1: parse every fixture file that carries goals (fixed family order,
# sorted files — deterministic)
CORPUS = []  # (rel, fs, sites, extents, goals[(line, text)])
for fam in ("verifier", "pyhard", "cpp", "mwires"):
    for path in sorted((FIX / fam).iterdir()):
        mod = registry_for(path.suffix)
        if mod is None:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        goals = [(no, mt.group(1).strip())
                 for no, ln in enumerate(text.splitlines(), 1)
                 if (mt := GOAL_RE.match(ln))]
        if not goals:
            continue
        rel = f"{fam}/{path.name}"
        fs = mod.parse(path, rel)
        sites, ext = ([], {}) if mod is not cpp_x else (cpp_x.scan_calls(path, rel), _extents(fs))
        CORPUS.append((rel, fs, sites, ext, goals))

# pass 2: evaluate
n_goals = n_neg = n_dead = 0
fam_files = {".py": set(), "gd": set(), "cpp": set()}
for rel, fs, sites, ext, goals in CORPUS:
    is_cpp = fs.ext in cpp_x.CPP_EXTS
    fam_files["gd" if fs.ext in (".gd", ".tscn") else "cpp" if is_cpp else ".py"].add(rel)
    for no, goal in goals:
        n_goals += 1
        neg = goal.startswith("!")
        core = goal[1:].strip() if neg else goal
        if neg:
            n_neg += 1
        label = f"{rel}:{no} {'!' if neg else ''}{core}"
        dm = re.fullmatch(r"@(\S+) dead", core)
        if dm:
            n_dead += 1
            roots = CPP_ROOTS if is_cpp else PY_ROOTS if fs.ext == ".py" else GD_ROOTS
            ok, detail = _dead(dm.group(1), fs, dm.group(1), CORPUS, roots)
            check(label, ok, detail)
            continue
        parsed, ok, detail = eval_goal(core, fs, sites, ext, is_cpp)
        if neg and parsed:
            ok, detail = not ok, f"negated — {detail}"
        check(label, parsed and ok, detail)

# non-vacuity: every extractor family carries goals, both fact directions
check("goals parsed", n_goals >= 80, str(n_goals))
check("annotated files", len(CORPUS) >= 24, str(len(CORPUS)))
check("dead goals present", n_dead >= 8, str(n_dead))
check("negated goals present", n_neg >= 2, str(n_neg))
for fam, floor in ((".py", 4), ("gd", 5), ("cpp", 8)):
    check(f"{fam} fixtures annotated", len(fam_files[fam]) >= floor, str(sorted(fam_files[fam])))

print()
print(f"{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
