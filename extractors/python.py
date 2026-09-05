"""Python extractor: the first cross-language module (contract: README.md).

Parses one .py file into a FileSym:
- funcs: every ``def``/``async def`` (class methods included, flat names);
  bodies are indent-delimited (python indent = 4 spaces)
- members: ``self.x`` assignments in methods (typed or ``= Klass(``)
  plus class-level annotations (``x: T``)
- consts: ``from <repo module> import X`` / ``import <repo module>`` ->
  name -> module rel path, the analog of GDScript's ``const X = preload()``
  receivers (and, via graph.py's import refs, of load-string liveness)
- entry_hints: names called at module level (incl. the ``__main__`` guard),
  @pytest.fixture-decorated funcs and @property/@name.setter accessors
  (attribute-dispatched — GDScript ``set(v):``/``get():`` analog)

Body scanning (call edges) lives in graph._scan_body_py, keyed on fs.ext.
"""

from __future__ import annotations

import re
from pathlib import Path

from extractors.model import FileSym, Func

CLASS_RE = re.compile(r"^(\s*)class\s+([A-Za-z_]\w*)\s*(?:\(([^)]*)\))?\s*:")
DEF_RE = re.compile(r"^(\s*)(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")
DECOR_RE = re.compile(r"^\s*@\S")
FIXTURE_DECOR_RE = re.compile(r"^\s*@(?:pytest\.)?fixture\b")
# attribute-dispatch accessors: @property/@cached_property (getter) and
# @name.setter/@name.deleter fire on attribute access — no call site
PROP_DECOR_RE = re.compile(r"^@(?:[A-Za-z_]\w*\.)*(?:property|cached_property)$")
PROP_ACCESSOR_RE = re.compile(r"^@([A-Za-z_]\w*)\.(?:setter|deleter)$")
SELF_TYPED_RE = re.compile(r"^\s*self\.([A-Za-z_]\w*)\s*:\s*([A-Za-z_]\w*)")
SELF_NEW_RE = re.compile(r"^\s*self\.([A-Za-z_]\w*)\s*=\s*([A-Z]\w*)\s*\(")
CLASS_FIELD_RE = re.compile(r"^([ \t]+)([A-Za-z_]\w*)\s*:\s*([A-Z]\w*)\s*(?:=|$)")
FROM_IMPORT_RE = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+(.+)$")
PLAIN_IMPORT_RE = re.compile(r"^\s*import\s+([\w.,\s]+)$")
MAIN_GUARD_RE = re.compile(r"^(\s*)if\s+__name__\s*==\s*['\"]__main__['\"]\s*:")
MODULE_CALL_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*\(")
MODULE_CALL_SKIP = {
    "if", "for", "while", "elif", "return", "assert", "del", "print",
    "lambda", "not", "await", "with", "except", "raise", "yield",
}


def _module_rel(mod: str, cur: Path) -> str:
    """Rel-posix path of a repo module for an import in `cur`, or ''.

    Handles ``from .nav import x`` (sibling), ``from extractors.gdscript``
    (root-relative package path) and ``import nav`` (top-level module) —
    only resolves when the target file exists in the repo.
    """
    mod = mod.strip()
    if not mod:
        return ""
    parts = mod.lstrip(".").split(".")
    dots = len(mod) - len(mod.lstrip("."))
    if dots:  # relative: sibling (./) or parent packages (../)
        base = cur.parent
        for _ in range(dots - 1):
            base = base.parent
        cand = base.joinpath(*parts)
    else:
        # root-relative package path first (nav.ROOT is imported by the
        # time graph.py drives parsing; fall back to the file's own dir)
        import nav

        cand = nav.ROOT.joinpath(*parts)
        if not cand.with_suffix(".py").is_file() and not (cand / "__init__.py").is_file():
            cand = cur.parent.joinpath(*parts)
    if cand.with_suffix(".py").is_file():
        try:
            return cand.with_suffix(".py").relative_to(nav.ROOT).as_posix()
        except ValueError:
            return ""  # outside the indexed root — not a repo module
    if (cand / "__init__.py").is_file():
        try:
            return (cand / "__init__.py").relative_to(nav.ROOT).as_posix()
        except ValueError:
            return ""
    return ""


def _split_names(spec: str) -> list[str]:
    out = []
    for chunk in spec.split(","):
        chunk = chunk.strip().rstrip(")").strip()
        if not chunk or chunk == "(":
            continue
        # "X as Y" binds the alias; plain "X" binds X
        out.append(chunk.split(" as ")[-1].strip())
    return [nm for nm in out if re.fullmatch(r"[A-Za-z_]\w*", nm)]


def _record_import(line: str, path: Path, fs: FileSym) -> None:
    im = FROM_IMPORT_RE.match(line)
    if im:
        import nav

        relmod = _module_rel(im.group(1), path)
        if relmod:
            # `from pkg import name` may import a SUBMODULE (extractors.
            # gdscript), not just a symbol — prefer name.py when it exists
            if relmod.endswith("/__init__.py"):
                pkg_dir = relmod[: -len("/__init__.py")]
            else:
                pkg_dir = relmod.rsplit("/", 1)[0] if "/" in relmod else ""
            for nm in _split_names(im.group(2)):
                sub = f"{pkg_dir}/{nm}.py" if pkg_dir else f"{nm}.py"
                fs.consts[nm] = sub if (nav.ROOT / sub).is_file() else relmod
        return
    im = PLAIN_IMPORT_RE.match(line)
    if im:
        for mod in im.group(1).split(","):
            seg = mod.split(" as ")
            mod_name = seg[0].strip()
            alias = seg[-1].strip() if len(seg) > 1 else mod_name.split(".")[0]
            relmod = _module_rel(mod_name, path)
            if relmod and re.fullmatch(r"[A-Za-z_]\w*", alias):
                fs.consts[alias] = relmod


def parse(path: Path, rel: str) -> FileSym:
    fs = FileSym(path=rel, ext=".py")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    in_tq = ""  # open triple-quote sentinel
    class_indents: list[int] = []  # open class headers' indents
    fixture_names: set[str] = set()
    pending_fixture = False
    pending_decor_indent = -1
    pending_decors: list[str] = []  # decorator lines awaiting their def
    main_guard_indent = -1  # indent of the `if __name__` header

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if in_tq:
            if line.count(in_tq) % 2 == 1:
                in_tq = ""  # closer seen (odd count may also reopen — rare, accept)
            i += 1
            continue
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        # triple quotes ANYWHERE on the line: template literals like
        # `_TEMPLATE = """<html>...` open mid-line. Everything after the
        # first delimiter is string content; only the prefix stays
        # parseable. Complete (even-count) strings vanish entirely.
        for q in ('"""', "'''"):
            cnt = line.count(q)
            if cnt:
                if cnt % 2 == 1:
                    in_tq = q
                line = line.split(q)[0]
                stripped = line.strip()
                break
        if not stripped:
            i += 1
            continue

        ind = len(line) - len(line.lstrip(" "))

        # dedent closes open classes (a def/class header popping older
        # scopes is handled by their own branches too)
        while class_indents and ind <= class_indents[-1]:
            class_indents.pop()

        # plain string statements (data, not structure) — their text must
        # never leak into the module-level call harvest
        if stripped[0] in "\"'":
            i += 1
            continue

        if stripped.startswith("@"):
            if pending_decor_indent != ind:
                pending_decor_indent = ind
                pending_fixture = False
                pending_decors = []
            if FIXTURE_DECOR_RE.match(stripped):
                pending_fixture = True
            pending_decors.append(stripped)
            i += 1
            continue

        m = CLASS_RE.match(line)
        if m:
            if not fs.class_name:
                fs.class_name = m.group(2)
                if m.group(3):
                    fs.extends = m.group(3).split(",")[0].strip().split(".")[-1]
            class_indents.append(ind)
            pending_decor_indent = -1
            pending_fixture = False
            pending_decors = []
            i += 1
            continue

        m = DEF_RE.match(line)
        if m:
            name = m.group(2)
            if pending_fixture:
                fixture_names.add(name)
            # @property accessors are dispatched on attribute access —
            # entry roots, exactly like GDScript's set(v):/get(): blocks
            for d in pending_decors:
                if PROP_DECOR_RE.match(d) or PROP_ACCESSOR_RE.match(d):
                    fs.entry_hints.add(name)
                    break
            pending_decor_indent = -1
            pending_fixture = False
            pending_decors = []
            j = i + 1
            while j < n:
                nxt = lines[j]
                if nxt.strip() and (len(nxt) - len(nxt.lstrip(" "))) <= ind:
                    break
                j += 1
            body = "\n".join(lines[i:j])
            prev = fs.funcs.get(name)
            if prev is not None:  # same-name merge (overloads/inner defs)
                fs.funcs[name] = Func(
                    path=rel, name=name, line=prev.line, body=prev.body + "\n" + body
                )
            else:
                fs.funcs[name] = Func(path=rel, name=name, line=i + 1, body=body)
            # self.x members live INSIDE method bodies (consumed above) —
            # scan the slice: typed annotations and constructor calls
            if class_indents and class_indents[-1] < ind:
                for bl in lines[i + 1 : j]:
                    sm = SELF_TYPED_RE.match(bl)
                    if sm:
                        fs.members[sm.group(1)] = sm.group(2)
                    else:
                        sn = SELF_NEW_RE.match(bl)
                        if sn:
                            fs.members[sn.group(1)] = sn.group(2)
            i = j
            continue

        # class-body members: annotated fields and self.x assignments
        if class_indents and ind > class_indents[-1]:
            fm = CLASS_FIELD_RE.match(line)
            if fm and len(fm.group(1)) == class_indents[-1] + 4:
                fs.members[fm.group(2)] = fm.group(3)
                i += 1
                continue
            sm = SELF_TYPED_RE.match(line)
            if sm:
                fs.members[sm.group(1)] = sm.group(2)
                i += 1
                continue
            sn = SELF_NEW_RE.match(line)
            if sn:
                fs.members[sn.group(1)] = sn.group(2)
                i += 1
                continue
            _record_import(line, path, fs)
            i += 1
            continue

        # module scope (or the __main__-guard block): statements here run
        # at import/launch — imports bind receivers, calls are entries
        mg = MAIN_GUARD_RE.match(line)
        if mg and main_guard_indent < 0:
            main_guard_indent = ind
            i += 1
            continue
        if main_guard_indent >= 0 and ind <= main_guard_indent:
            main_guard_indent = -1  # guard block ended

        _record_import(line, path, fs)

        # ENTRY_RULES = [rule_a, rule_b, ...]: the documented extractor
        # contract dispatches these callables dynamically (graph._find_roots
        # iterates the list) — list them as parse-declared entry points.
        # Buffer multi-line list literals before harvesting bare idents.
        if stripped.startswith("ENTRY_RULES") and "=" in stripped:
            rhs = stripped.split("=", 1)[1]
            buf = rhs
            j = i + 1
            while buf.count("[") > buf.count("]") and j < n:
                buf += " " + lines[j].strip()
                j += 1
            for em in re.finditer(r"(?<![\w.])([A-Za-z_]\w*)(?!\s*\()", buf):
                fs.entry_hints.add(em.group(1))
            i = j
            continue

        if not class_indents and (ind == 0 or (main_guard_indent >= 0 and ind > main_guard_indent)):
            for cm in MODULE_CALL_RE.finditer(line):
                nm = cm.group(1)
                if nm not in MODULE_CALL_SKIP:
                    fs.entry_hints.add(nm)
        i += 1

    for nm in fixture_names:
        if nm in fs.funcs:
            fs.entry_hints.add(nm)
    return fs


# funcs the runtime may invoke without any static call site
PY_VIRTUALS = {
    "__init__", "__enter__", "__exit__", "__aenter__", "__aexit__",
    "__call__", "__iter__", "__next__", "__len__", "__getitem__",
    "__setitem__", "__delitem__", "__contains__", "__repr__", "__str__",
    "__eq__", "__hash__", "__bool__", "__index__",
}


def _entry_virtuals(fs: FileSym, ctx):
    if fs.ext != ".py":
        return
    for name in fs.funcs:
        if name in PY_VIRTUALS:
            yield fs.funcs[name].key


def _entry_tests(fs: FileSym, ctx):
    # pytest convention: test_* funcs are entry points (GUT analog)
    if fs.ext != ".py":
        return
    for name, fn in fs.funcs.items():
        if name.startswith("test_"):
            yield fn.key


def _entry_module(fs: FileSym, ctx):
    # module-level / __main__-guard calls run at import or launch, and
    # fixture funcs run via the test runner — entry_hints holds both
    if fs.ext != ".py":
        return
    for name in fs.entry_hints:
        fn = fs.funcs.get(name)
        if fn is not None:
            yield fn.key


ENTRY_RULES = [_entry_virtuals, _entry_tests, _entry_module]
