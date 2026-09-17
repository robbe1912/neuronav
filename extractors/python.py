"""Python extractor: the first cross-language module (contract: README.md).

Parses one .py file into a FileSym:
- funcs: every ``def``/``async def`` (class methods included, flat names);
  bodies are indent-delimited (python indent = 4 spaces); params/ret come
  from the AST signature (issue #122) — the leading self/cls receiver of
  methods is implicit and excluded, so the signature surface matches the
  gd/cpp display contract
- members: ``self.x`` assignments in methods (typed or ``= Klass(``)
  plus class-level annotations (``x: T``)
- consts: ``from <repo module> import X`` / ``import <repo module>`` ->
  name -> module rel path, the analog of GDScript's ``const X = preload()``
  receivers (and, via graph.py's import refs, of load-string liveness)
- entry_hints: names called at module level (incl. the ``__main__`` guard),
  @pytest.fixture-decorated funcs, @property/@name.setter accessors
  (attribute-dispatched — GDScript ``set(v):``/``get():`` analog) and
  quoted names in ``__all__`` (the declared export surface)
- per-func IO parity with gd: writes (``self.x =``) and mut_params from a
  body scan (issue #122)

Body scanning (call edges) lives in graph._scan_body_py, keyed on fs.ext.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from extractors.common import PY_CONTROL_KEYWORDS, entry_keys, merge_func
from extractors.model import FileSym

CLASS_RE = re.compile(r"^(\s*)class\s+([A-Za-z_]\w*)\s*(?:\(([^)]*)\))?\s*:")
DEF_RE = re.compile(r"^(\s*)(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")
DECOR_RE = re.compile(r"^\s*@\S")
FIXTURE_DECOR_RE = re.compile(r"^\s*@(?:pytest\.)?fixture\b")
# attribute-dispatch accessors: @property/@cached_property (getter) and
# @name.setter/@name.deleter fire on attribute access — no call site
PROP_DECOR_RE = re.compile(r"^@(?:[A-Za-z_]\w*\.)*(?:property|cached_property)$")
PROP_ACCESSOR_RE = re.compile(r"^@([A-Za-z_]\w*)\.(?:setter|deleter)$")
# hints keep a flat generic subscript (self.cache: dict[str, Widget]) —
# graph._scan_body_py resolves the value classes from it
SELF_TYPED_RE = re.compile(r"^\s*self\.([A-Za-z_]\w*)\s*:\s*([A-Za-z_]\w*(?:\[[^\]=]+\])?)")
SELF_NEW_RE = re.compile(r"^\s*self\.([A-Za-z_]\w*)\s*=\s*([A-Z]\w*)\s*\(")
# any-identifier type: dataclass fields are usually lowercase builtins
# (int/str/float) — they are class vars exactly like typed user classes
CLASS_FIELD_RE = re.compile(r"^([ \t]+)([A-Za-z_]\w*)\s*:\s*([A-Za-z_]\w*(?:\[[^\]=]+\])?)\s*(?:=|$)")
FROM_IMPORT_RE = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+(.+)$")
PLAIN_IMPORT_RE = re.compile(r"^\s*import\s+([\w.,\s]+)$")
MAIN_GUARD_RE = re.compile(r"^(\s*)if\s+__name__\s*==\s*['\"]__main__['\"]\s*:")
MODULE_CONST_RE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*(?::[^=]*)?=(?!=)\s*(.*)$")
MODULE_CALL_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*\(")
MODULE_CALL_SKIP = PY_CONTROL_KEYWORDS

# stdlib/framework dispatch hooks: methods http.server-style machinery
# invokes reflectively on a handler subclass (base resolves outside the
# repo, so no static caller exists). Dead-scan classifies these as
# review, mirroring the .gd VIRTUALS rule — they are overrides, not
# orphans. Kept minimal: only names the serving machinery itself calls.
PY_HOOKS = frozenset({
    "end_headers", "log_message", "log_error", "log_request",
    "send_head", "translate_path", "guess_type", "list_directory",
})


def _module_rel(mod: str, cur: Path) -> str:
    """Rel-posix path of a repo module for an import in `cur`, or ''.

    Handles ``from .nav import x`` (sibling), ``from extractors.gdscript``
    (root-relative package path) and ``import nav`` (top-level module) —
    only resolves when the target file exists in the repo.
    """
    mod = mod.strip()
    if not mod:
        return ""
    import nav

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




def _buffer_list_rhs(lines: list[str], i: int, rhs: str) -> tuple[str, int]:
    """Buffer a multi-line bracketed list RHS from ``lines[i]``;
    -> (joined_rhs, next_index)."""
    j = i + 1
    while rhs.count("[") > rhs.count("]") and j < len(lines):
        rhs += " " + lines[j].strip()
        j += 1
    return rhs, j
def _split_names(spec: str) -> list[str]:
    # a `#` comment ends the name list — `from x import a  # noqa` must
    # still bind a (#153: the trailing comment ate the last name)
    spec = spec.split("#", 1)[0]
    out = []
    for chunk in spec.split(","):
        # parenthesized lists leave ( or ) glued to either side of a
        # chunk — strip both, not just the trailing paren (#107)
        chunk = chunk.strip().strip("()").strip()
        if not chunk or chunk == "(":
            continue
        # "X as Y" binds the alias; plain "X" binds X
        out.append(chunk.split(" as ")[-1].strip())
    return [nm for nm in out if re.fullmatch(r"[A-Za-z_]\w*", nm)]


def _has_exact(relpath: str) -> bool:
    """Case-SENSITIVE file check: NTFS/Windows stat is case-insensitive, so
    `is_file()` alone would bless `Vec2.py` when only `vec2.py` exists - the
    graph then records a module id no file has. Compare against the actual
    directory listing instead."""
    import nav

    p = nav.ROOT / relpath
    if not p.is_file():
        return False
    return p.name in {e.name for e in p.parent.iterdir()}


def _buffer_paren_rhs(lines: list[str], i: int, rhs: str) -> tuple[str, int]:
    """Buffer a multi-line parenthesized from-import list from
    ``lines[i]``; -> (joined_rhs, next_index)."""
    j = i + 1
    while rhs.count("(") > rhs.count(")") and j < len(lines):
        rhs += " " + lines[j].strip()
        j += 1
    return rhs, j


def _record_from(module: str, names: list[tuple[str, str]], path: Path, fs: FileSym) -> None:
    """Bind `from module import orig as alias` facts: the receiver const
    (alias) plus the one-name-survives import edge (original name —
    the edge marks the func that exists, the alias may not)."""
    relmod = _module_rel(module, path)
    if not relmod:
        return
    # `from pkg import name` may import a SUBMODULE (extractors.
    # gdscript), not just a symbol — prefer name.py when it exists
    # (exact case) as the receiver target
    if relmod.endswith("/__init__.py"):
        pkg_dir = relmod[: -len("/__init__.py")]
    else:
        pkg_dir = relmod.rsplit("/", 1)[0] if "/" in relmod else ""
    for nm, alias in names:
        sub = f"{pkg_dir}/{nm}.py" if pkg_dir else f"{nm}.py"
        target = sub if _has_exact(sub) else relmod
        fs.consts[alias] = target
        # from-import binds ONE name: that func survives (it is
        # referenced by the import itself); the module's other
        # funcs are NOT kept alive by a name-selecting import
        fs.from_imports.add((target, nm))


def _record_plain(mod_name: str, alias: str, path: Path, fs: FileSym) -> None:
    relmod = _module_rel(mod_name, path)
    if relmod and re.fullmatch(r"[A-Za-z_]\w*", alias):
        fs.consts[alias] = relmod
        # plain import binds the whole namespace: the module may be
        # reached dynamically, keep its funcs alive as a unit
        fs.imported_modules.add(relmod)


def _record_import(line: str, path: Path, fs: FileSym, lines: list[str], i: int) -> int:
    """Line-based import harvest (the AST path in parse() is canonical;
    this one backs the unparseable-file fallback). Handles the
    parenthesized/multi-line from-import shapes of #107. -> number of
    lines consumed."""
    im = FROM_IMPORT_RE.match(line)
    if im:
        rhs, j = _buffer_paren_rhs(lines, i, im.group(2))
        _record_from(im.group(1), [(nm, nm) for nm in _split_names(rhs)], path, fs)
        return j - i
    im = PLAIN_IMPORT_RE.match(line)
    if im:
        for mod in im.group(1).split(","):
            seg = mod.split(" as ")
            mod_name = seg[0].strip()
            alias = seg[-1].strip() if len(seg) > 1 else mod_name.split(".")[0]
            _record_plain(mod_name, alias, path, fs)
    return 1


def _iter_module_scope(body: list[ast.stmt]):
    """Statements that run at import time: module scope proper plus the
    bodies of control-flow wrappers (if/try/with/for/match), but never
    inside a def or class body (those belong to the fn/class scans).
    Decorator expressions of skipped defs are yielded — they execute at
    import (registry-style callbacks)."""
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield from stmt.decorator_list
            continue
        yield stmt
        for sub in (getattr(stmt, "body", ()), getattr(stmt, "orelse", ()), getattr(stmt, "finalbody", ())):
            yield from _iter_module_scope(sub)
        for handler in getattr(stmt, "handlers", ()) or ():
            yield from _iter_module_scope(handler.body)
        for case in getattr(stmt, "cases", ()) or ():
            yield from _iter_module_scope(case.body)


def _harvest_ast(tree: ast.Module, path: Path, fs: FileSym) -> None:
    """AST pass over a parseable file: import facts (anywhere — module,
    class, or function bodies: a lazy `import viz` inside a handler
    still binds the receiver) and module-scope liveness the line scan
    cannot see (indented calls, value refs, receiver binds, dispatch
    surfaces)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = "." * node.level + (node.module or "")
            _record_from(
                mod, [(a.name, a.asname or a.name) for a in node.names], path, fs
            )
        elif isinstance(node, ast.Import):
            for a in node.names:
                _record_plain(a.name, a.asname or a.name.split(".")[0], path, fs)

    # classes -> their method names (dispatch-surface candidates) and the
    # in-file classes their bodies reference (stand-ins wrap each other:
    # FailingClient.create_collection returns FailingAdd, so the wrapped
    # class is as much a dispatch surface as the injected one)
    classes: dict[str, set[str]] = {}
    class_refs: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            methods = set()
            refs: set[str] = set()
            for stmt in node.body:
                for sub in ast.walk(stmt):
                    if isinstance(sub, ast.Name) and sub.id != node.name:
                        refs.add(sub.id)
                    elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods.add(sub.name)
            classes[node.name] = methods
            class_refs[node.name] = refs

    module_refs: set[str] = set()
    for stmt in _iter_module_scope(tree.body):
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name):
                module_refs.add(node.id)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                # module code runs at import: the callee is an entry
                fs.entry_hints.add(node.func.id)
            elif isinstance(node, ast.Assign):
                _bind_module_var(node, fs, classes)

    # classes referenced at module scope travel through an opaque
    # consumer (injected stand-ins, framework singletons): their method
    # names become runtime-dispatch candidates — review tier, not roots.
    # The surface closes over the stand-in object graph (wrappers the
    # dispatch classes themselves reference), breadth-first and sorted
    # for determinism.
    frontier = sorted(set(classes) & module_refs)
    seen: set[str] = set()
    while frontier:
        cls = frontier.pop()
        if cls in seen:
            continue
        seen.add(cls)
        fs.dispatch_names |= classes[cls]
        frontier.extend(sorted((class_refs.get(cls, set()) & set(classes)) - seen))

    # bare-name argument references (#177): a def or class passed by
    # reference as a call argument — keyword value (`parse_constant=f`,
    # `key=rank`) or bare positional (`atexit.register(flush)`,
    # `Server(addr, HandlerCls)`) — has no call site, so liveness must
    # read the argument itself. The AST is the strict guard: only
    # plain Name argument expressions count, so string contents and
    # attribute refs (obj.method) never land here, and **-unpacking
    # (`f(**d)`) is excluded (d is a mapping, not a callable ref).
    # Def-name refs feed the graph's attributed-alive arm; class-name
    # refs ride the #153 dispatch convention (methods review, not
    # likely) — body-level class refs are as much runtime dispatch as
    # module-scope ones.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Name):
                fs.arg_refs.add(arg.id)
                if arg.id in classes:
                    fs.dispatch_names |= classes[arg.id]
        for kw in node.keywords:
            if kw.arg is not None and isinstance(kw.value, ast.Name):
                fs.arg_refs.add(kw.value.id)
                if kw.value.id in classes:
                    fs.dispatch_names |= classes[kw.value.id]


# mutators callable on list/dict/set/pass-by-ref objects (gdscript's
# _MUTATING_METHODS analog — the py member/param parity surface)
_MUTATING_METHODS = {
    "add", "append", "appendleft", "clear", "discard", "extend",
    "insert", "pop", "popleft", "remove", "reverse", "setdefault",
    "sort", "update",
}


def _line_starts(text: str) -> list[int]:
    """Char offsets of each line's start in ``text`` — the fast lookup
    table for _seg (ast.get_source_segment re-splits the whole source per
    call; O(funcs x lines) is too slow for monster files). Universal
    newlines: splitlines(True) keeps the terminator in each span, so the
    running sum lands on the next line's first char for \n, \r\n and \r."""
    pos = [0]
    for ln in text.splitlines(True):
        pos.append(pos[-1] + len(ln))
    return pos


def _seg(node: ast.expr | None, starts: list[int], src: str) -> str:
    """Exact source text of an AST node (any span, multi-line included) —
    the get_source_segment result without the per-call resplit. '' for a
    missing node or a lost-token position."""
    if node is None or getattr(node, "col_offset", -1) < 0:
        return ""
    end_line = getattr(node, "end_lineno", None)
    if end_line is None or getattr(node, "end_col_offset", -1) < 0:
        return ""
    s = starts[max(0, node.lineno - 1)] + node.col_offset
    e = starts[end_line - 1] + node.end_col_offset
    return src[s:e]


def _fn_signature(
    fn: ast.AST | None, src: str | None = None, starts: list[int] | None = None
) -> tuple[list[tuple[str, str]], str]:
    """-> ([(name, type)], ret) for a FunctionDef/AsyncFunctionDef node.

    Source-order params (positional-only, positional-or-keyword, vararg,
    keyword-only, kwarg) with their annotations in signature order — the
    display contract sync_functions/repo_map/fnio all consume. A
    ``None`` node (unparseable-file fallback scan) and a parse-error
    fallback tree with no source (segments unavailable) both degrade:
    names/types empty for the former, declared types '' for the latter.
    """
    if fn is None or src is None:
        return [], ""
    if starts is None:
        starts = _line_starts(src)
    a = fn.args
    t = lambda an: _seg(an, starts, src)  # noqa: E731
    pairs = [(x.arg, t(x.annotation)) for x in a.posonlyargs]
    pairs += [(x.arg, t(x.annotation)) for x in a.args]
    if a.vararg is not None:
        pairs.append((a.vararg.arg, t(a.vararg.annotation)))
    pairs += [(x.arg, t(x.annotation)) for x in a.kwonlyargs]
    if a.kwarg is not None:
        pairs.append((a.kwarg.arg, t(a.kwarg.annotation)))
    return pairs, t(fn.returns)


def _ast_funcs(tree: ast.AST | None) -> tuple[dict[str, ast.AST], set[int]]:
    """(def line -> FunctionDef node, def lines that are CLASS METHODS) —
    the AST signature truth the line scan's keyed merge reads from.
    Column-0 strings no longer desync the scan (issue #50), so the dict
    is exact either way. The method set is precise: a def whose immediate
    parent scope is a class body, recursing through nested classes but
    never into function bodies — closures keep their own receivers."""
    funcs: dict[str, ast.AST] = {}
    methods: set[int] = set()
    if tree is None:
        return funcs, methods
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[node.lineno] = node
    # class-body walk without descending into defs: direct defs (and defs
    # of nested classes) are methods
    stack = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    while stack:
        cls = stack.pop()
        for stmt in cls.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.add(stmt.lineno)
            elif isinstance(stmt, ast.ClassDef):
                stack.append(stmt)
    return funcs, methods


def _scan_io(body: str, params: list) -> tuple:
    """-> (writes, mut_params) member/param mutation sets for a body
    (gdscript._scan_io's python analog — no member_names param: python
    member writes are always ``self.x =``, bare assigns are locals).
    Purely syntactic: member writes = ``self.x =`` (augmented too); param
    mutation = a param name followed by a known mutating method call.
    """
    writes = set(re.findall(r"\bself\.([A-Za-z_]\w*)\s*=(?!=)", body))
    writes |= set(re.findall(r"\bself\.([A-Za-z_]\w*)\s*(?:\+|-|\*|/|%)=(?!=)", body))
    pnames = {p for p, _t in params}
    mut = set()
    for pm in re.finditer(r"\b([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*\(", body):
        if pm.group(1) in pnames and pm.group(2) in _MUTATING_METHODS:
            mut.add(pm.group(1))
    return writes, mut


def _bind_module_var(asg: ast.Assign, fs: FileSym, classes: dict[str, set[str]]) -> None:
    """Module-level assignment facts:
    - value ref: `nav.embed = _counting` / `HOOK = helper` hands a file
      func to another namespace — a genuine use with no call site
    - receiver bind: `LOG = CheckLog()` / `handler = Stub` makes the
      target a typed receiver for every body scan (module vars are
      visible file-wide)"""
    val = asg.value
    if isinstance(val, ast.Name):
        if val.id in fs.funcs:
            fs.entry_hints.add(val.id)
        if val.id in classes:
            for tgt in asg.targets:
                if isinstance(tgt, ast.Name):
                    fs.module_vars[tgt.id] = val.id
    elif isinstance(val, ast.Call) and isinstance(val.func, ast.Name):
        callee = val.func.id
        mod = fs.consts.get(callee, "")
        bound = "module:" + mod if mod else (callee if callee in classes else "")
        if bound:
            for tgt in asg.targets:
                if isinstance(tgt, ast.Name):
                    fs.module_vars[tgt.id] = bound


def parse(path: Path, rel: str) -> FileSym:
    fs = FileSym(path=rel, ext=".py")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    # AST truth for def end lines: a column-0 line inside a triple-quoted
    # string is NOT a dedent, but the indent scan below reads it as one,
    # truncating the body (call edges after it vanish) and desyncing the
    # module scan's string state (the orphaned closer reopens a phantom
    # string that swallows the rest of the file). end_lineno is exact;
    # the indent scan survives only as the fallback for unparseable files.
    def_end: dict[int, int] = {}  # 1-based def line -> 1-based end line
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                def_end[node.lineno] = node.end_lineno
    ast_fns, ast_methods = _ast_funcs(tree)
    line_starts = _line_starts(text)

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
            span_end = def_end.get(i + 1)
            if span_end is not None:
                j = span_end  # 1-based end == 0-based exclusive index
            else:  # unparseable file: legacy indent-terminated scan
                j = i + 1
                while j < n:
                    nxt = lines[j]
                    if nxt.strip() and (len(nxt) - len(nxt.lstrip(" "))) <= ind:
                        break
                    j += 1
            body = "\n".join(lines[i:j])
            io_params, io_ret = _fn_signature(ast_fns.get(i + 1), text, line_starts)
            # method receiver is implicit (gd/cpp parity): the signature
            # surface shows only explicit args, never the leading self/cls
            if i + 1 in ast_methods:
                if io_params and io_params[0][0] in ("self", "cls"):
                    io_params = io_params[1:]
            merge_func(fs.funcs, rel, name, i + 1, body, params=io_params, ret=io_ret)
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
            i += _record_import(line, path, fs, lines, i)
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

        i += _record_import(line, path, fs, lines, i) - 1

        # __all__ = ["a", "b"]: the module's declared export surface —
        # consumers import these without any in-repo call site. Buffer
        # multi-line list literals, then harvest the quoted names.
        if stripped.startswith("__all__") and "=" in stripped:
            buf, j = _buffer_list_rhs(lines, i, stripped.split("=", 1)[1])
            for em in re.finditer(r"[\"']([A-Za-z_]\w*)[\"']", buf):
                fs.entry_hints.add(em.group(1))
            i = j
            continue

        # ENTRY_RULES = [rule_a, rule_b, ...]: the documented extractor
        # contract dispatches these callables dynamically (graph._find_roots
        # iterates the list) — list them as parse-declared entry points.
        # Buffer multi-line list literals before harvesting bare idents.
        if stripped.startswith("ENTRY_RULES") and "=" in stripped:
            buf, j = _buffer_list_rhs(lines, i, stripped.split("=", 1)[1])
            for em in re.finditer(r"(?<![\w.])([A-Za-z_]\w*)(?!\s*\()", buf):
                fs.entry_hints.add(em.group(1))
            i = j
            continue

        # module-level SCREAMING_SNAKE assigns are the module's public
        # constants — harvest into fs.consts so recall hits them as exact
        # identifiers. Values keep the consts contract: a res://-stripped
        # string literal stays a path (mirrors .gd const preload paths);
        # anything else stores '' (graph resolves consts values as file
        # relpaths and safely misses on ''). `==`/augmented ops never
        # match (name-to-= adjacency), class fields were caught above.
        if ind == 0:
            cm = MODULE_CONST_RE.match(line)
            if cm:
                vm = re.match(r"[\"']([^\"']+)[\"']\s*$", cm.group(2))
                fs.consts[cm.group(1)] = (
                    vm.group(1).removeprefix("res://") if vm else ""
                )

        if not class_indents and (ind == 0 or (main_guard_indent >= 0 and ind > main_guard_indent)):
            for cm in MODULE_CALL_RE.finditer(line):
                nm = cm.group(1)
                if nm not in MODULE_CALL_SKIP:
                    fs.entry_hints.add(nm)
        i += 1

    if tree is not None:
        _harvest_ast(tree, path, fs)

    # IO scan runs after the whole file is parsed (gdscript parity).
    for fn in fs.funcs.values():
        fn.writes, fn.mut_params = _scan_io(fn.body, fn.params)

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
    yield from entry_keys(fs, fs.entry_hints)


ENTRY_RULES = [_entry_virtuals, _entry_tests, _entry_module]

# ---- uniform shared-surface hooks (langsep) -----------------------------------
# Language-owned grammar for graph's dup/delegate/chunk/file-doc passes
# (issue #295): the statement shapes graph's packing algorithm matches,
# spelled per language. A py body keeps its signature line (graph skips
# it before classifying); `#` is the only comment prefix; docstrings are
# triple-quoted. Bodies mirror the graph.py expressions they replace
# byte-for-byte (minus the cross-language `fn`/`catch`/`case` branches
# py code never writes).

SIGNATURE_RE = re.compile(r"^(?:async\s+)?def\s+\w+")
GUARD_RE = re.compile(r"^(?:el)?if\s+[^():]+:$")
GUARD_RET_RE = re.compile(r"^return\s+[^()]*$")
ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*(?:\.\w+)* = [^()=]+$")
FORWARD_RE = re.compile(r"^return\s+(?:await\s+)?[A-Za-z_][\w.]*\([\w\s,]*\)$")
COMMENT_PREFIXES = ("#",)
TRIPLE_QUOTES = ('"""', "'''")
DEDENT_RE = re.compile(r"^(\s+)else:|^(\s+)elif\s|^(\s+)except|^(\s+)finally:|^(\s*)@(\w)")
ENCODING_RE = re.compile(r"^#.*?coding[:=]")

# py dynamic-dispatch idioms (issue #295): the reflection/late-binding
# surfaces python actually has — attribute plumbing, dynamic evaluation,
# import machinery. NOT gd's dispatch vocab.
DYNAMIC_HINT = re.compile(
    r"\bgetattr\s*\(|\bsetattr\s*\(|\beval\s*\(|\bexec\s*\("
    r"|\bglobals\s*\(\)|\blocals\s*\(\)|\b__getattr__\b"
)


def is_entry_exempt(name: str) -> bool:
    """Cpp-only rule (implicit entries); py names never exempt."""
    return False


def unresolved_base_review(name: str) -> bool:
    """Underscore-rule tail for py: underscore virtuals, stdlib serving
    machinery (PY_HOOKS) and do_* overrides land in review."""
    return name.startswith("_") or name in PY_HOOKS or name.startswith("do_")


def stand_in_review(fs: FileSym, name: str) -> bool:
    """Module-scope stand-ins (duck-typed stubs, framework singletons) are
    consumed through an opaque caller — honest tier is review."""
    return name in getattr(fs, "dispatch_names", ())


def mention_review(name: str, mentions: dict) -> bool:
    """Cpp-only rule (mention floor)."""
    return False


# ---- python body-scan patterns (companion to the parser above) ----------------
PY_ATTR_CALL_RE = re.compile(r"(?<![\w.$])([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*\(")
PY_CHAIN_CALL_RE = re.compile(
    r"(?<![\w.$])([A-Za-z_]\w*)\s*\.\s*([a-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*\("
)
PY_BARE_CALL_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*\(")
# `name: Type` params and `x = Klass(` locals (capitalized = user
# classes); hints keep a flat generic subscript (dict[str, Widget]) so
# subscript access can resolve the value classes inside
PY_PARAM_TYPED_RE = re.compile(r"[(,]\s*([A-Za-z_]\w*)\s*:\s*([A-Za-z_]\w*(?:\[[^\]=]+\])?)")
PY_LOCAL_NEW_RE = re.compile(r"(?<![\w.!=<>])([A-Za-z_]\w*)\s*=(?!=)\s*([A-Z]\w*)\s*\(")
# with/async-with target bound from a constructor: with Session() as s
PY_WITH_AS_RE = re.compile(
    r"(?<![\w.])(?:async\s+)?with\s+([A-Z]\w*)\s*\([^()]*\)\s+as\s+([A-Za-z_]\w*)"
)
# annotated local: local: Widget = ... / pairs: dict[str, Widget] = ...
PY_ANNOT_ASSIGN_RE = re.compile(
    r"(?<![\w.])([A-Za-z_]\w*)\s*:\s*([A-Za-z_]\w*(?:\[[^\]=]+\])?)\s*=(?!=)"
)
# box[k].method( / self.box[k].method( — subscript access into a hint
PY_SUBSCRIPT_CALL_RE = re.compile(
    r"(?<![\w.$])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\[[^\]]*\]\s*\.\s*([A-Za-z_]\w*)\s*\("
)
# local bound from an imported call: extractor = registry_for(...)
PY_MODULE_ASSIGN_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*=\s*([a-z_]\w*)\s*\(")
# imported_call(args).method( — registry_for(path.suffix).parse(...)
PY_RESULT_CALL_RE = re.compile(r"([A-Za-z_]\w*)\s*\(([^()]*)\)\s*\.\s*([A-Za-z_]\w*)\s*\(")
PY_NON_CALLS = PY_CONTROL_KEYWORDS | {
    "in", "is", "and", "or", "nonlocal", "global", "import", "from",
    "len", "range", "str", "int", "float", "bool", "list", "dict", "set",
    "tuple", "isinstance", "issubclass", "type", "sorted", "reversed",
    "min", "max", "sum", "enumerate", "zip", "open", "getattr", "setattr",
    "hasattr", "repr", "abs", "any", "all", "filter", "map", "dir", "id",
    "hash", "iter", "next", "vars", "format", "bytes", "super", "exit",
    "quit", "help", "input", "round", "divmod", "pow", "chr", "ord", "hex",
    "oct", "bin", "frozenset", "bytearray", "complex", "object",
    "staticmethod", "classmethod", "property", "dataclass", "field",
    "Exception", "ValueError", "TypeError", "RuntimeError", "KeyError",
    "IndexError", "OSError", "IOError", "StopIteration", "FileNotFoundError",
    "NotImplementedError",
}
_HINT_VALUE_RE = re.compile(r"^[A-Za-z_]\w*\[([^\]]*)\]")


def _hint_value_classes(hint: str) -> list[str]:
    """Value classes inside a flat generic hint's outer subscript:
    ``dict[str, Widget]`` -> ``['Widget']`` (Union members included)."""
    m = _HINT_VALUE_RE.match(hint)
    return re.findall(r"\b[A-Z]\w*", m.group(1)) if m else []


# ---- build passes + body scan (langsep: moved from graph.py, ctx=Graph) -------
from extractors.common import FN_KEY_SEP, fold_continuations, fn_key  # noqa: E402


def rebind_reexports_sweep(ctx) -> None:
    """Package re-export rebinding (#153): `from extractors import X`
    binds X to the package __init__ file, but X is DEFINED in a
    submodule the __init__ re-exports — rebind import edge and
    receiver const to the definer. PROSPECTIVE / defense-in-depth
    (GK #164 review, teeth-verified): on the current corpus the
    liveness outcome is already carried by the __init__'s OWN
    from-imports (the pass below runs over every file, and the
    fixed parenthesized harvest binds them straight to definers);
    what this adds is consumer consts that point at definers —
    bare-call edges land on the real implementation, keeping
    caller/reverse-edge data honest — plus coverage for alias and
    chained re-export shapes a consumer may use without the
    __init__ importing the name itself."""
    for fs in ctx.files.values():
        if fs.ext != ".py":
            continue
        rebound = set()
        for target, nm in sorted(fs.from_imports):
            definer = ctx._resolve_definer(target, nm)
            rebound.add((definer or target, nm))
            if definer and definer != target and fs.consts.get(nm) == target:
                fs.consts[nm] = definer
        fs.from_imports = rebound


def import_liveness_sweep(ctx) -> None:
    """A PLAIN `import x` binds the namespace - the module may be
    reached dynamically, so its funcs stay alive as a unit. A
    `from x import y` selects exactly one name: only that func (if it
    is one) survives the import; siblings do not."""
    for fs in ctx.files.values():
        if fs.ext != ".py":
            continue
        for mod in fs.imported_modules:
            if mod in ctx.files:
                for other in ctx.files[mod].funcs.values():
                    ctx.referenced.add(other.key)
        for mod, nm in fs.from_imports:
            if mod in ctx.files and nm in ctx.files[mod].funcs:
                ctx.referenced.add(f"{mod}{FN_KEY_SEP}{nm}")


def arg_refs_sweep(ctx) -> None:
    """Python bare-name argument references (#177): a def passed by
    reference — `json.loads(..., parse_constant=no_constants)`,
    `sorted(rows, key=rank)`, `atexit.register(flush)` — has no
    call site, so the call-regex passes never see it and the dead
    tier flagged it likely. The extractor harvest is AST-guarded
    to plain identifier args (strings and attribute refs never
    land there); same-file defs get an attributed-alive key —
    precise per-def liveness, never the corpus-wide
    referenced_names name match, so same-named funcs elsewhere
    stay honest dead-code material."""
    for fs in ctx.files.values():
        if fs.ext != ".py":
            continue
        for nm in fs.arg_refs:
            if nm in fs.funcs:
                ctx.referenced.add(fs.funcs[nm].key)


def harvest_facts(fs: FileSym, ctx) -> None:
    """Python files contribute no name-literal/init-call facts (the
    sweep stays registry-uniform; nothing to harvest)."""
    return None


def scan_file(fs: FileSym, ctx) -> None:
    if fs.ext != ".py":
        return
    for fn in fs.funcs.values():
        _scan_body_py(fs, fn, ctx)


def _scan_body_py(fs: FileSym, fn: Func, ctx) -> None:
    """Python body scan: call edges via typed receivers, class_map
    classes, and from-import consts (module-file receivers)."""
    src_key = fn.key
    scan_text = fold_continuations(fn.body)
    # receiver types: self-members from the extractor + typed params
    # + constructor locals in this body
    var_types = dict(fs.members)
    var_types.update(fs.module_vars)
    for pm in PY_PARAM_TYPED_RE.finditer(scan_text):
        var_types[pm.group(1)] = pm.group(2)
    for m in PY_ANNOT_ASSIGN_RE.finditer(scan_text):
        var_types[m.group(1)] = m.group(2)
    for m in PY_WITH_AS_RE.finditer(scan_text):
        var_types[m.group(2)] = m.group(1)
    for m in PY_LOCAL_NEW_RE.finditer(scan_text):
        var_types[m.group(1)] = m.group(2)
    # x = imported_name(...): the local becomes a module-object
    # receiver — resolve x.method( against that module (and the
    # modules it re-exports, since registries return submodules)
    for m in PY_MODULE_ASSIGN_RE.finditer(scan_text):
        mod = fs.consts.get(m.group(2), "")
        if mod in ctx.files:
            var_types[m.group(1)] = "module:" + mod
    # obj.method( — head resolves via class_map (repo classes), typed
    # receivers, from-import consts (module-file receivers), or
    # module-object locals bound from an imported call
    for m in PY_ATTR_CALL_RE.finditer(scan_text):
        head, meth = m.group(1), m.group(2)
        if head in ("self", "cls"):
            if meth in fs.funcs:
                ctx._emit_call(src_key, fs.path, meth)
            continue
        vt = var_types.get(head, "")
        if vt.startswith("module:"):
            for dst in _module_method_dsts(vt[len("module:"):], meth, ctx):
                ctx._emit_call(src_key, dst, meth)
            continue
        cls = head if head in ctx.class_map else var_types.get(head, "")
        if cls and cls in ctx.class_map:
            dst = ctx.class_map[cls]
        elif head in fs.consts and fs.consts[head] in ctx.files:
            dst = fs.consts[head]
        else:
            continue
        if meth in ctx.files[dst].funcs:
            ctx._emit_call(src_key, dst, meth)
    # imported_call(args).method( — calling an imported function then
    # a method on the result (registry_for(suffix).parse(...)): the
    # const's module chain supplies the candidate defs
    for m in PY_RESULT_CALL_RE.finditer(scan_text):
        head, meth = m.group(1), m.group(3)
        mod = fs.consts.get(head, "")
        if mod in ctx.files:
            for dst in _module_method_dsts(mod, meth, ctx):
                ctx._emit_call(src_key, dst, meth)
    # two-level chains: self.g.greet( / api.client.run(
    for m in PY_CHAIN_CALL_RE.finditer(scan_text):
        head, mid, tail = m.group(1), m.group(2), m.group(3)
        if head in ("self", "cls"):
            cls = var_types.get(mid, "")
            dst = ctx.class_map.get(cls, "")
        else:
            dst = ctx._chain_dst(var_types, head, mid)
        if dst and tail in ctx.files[dst].funcs:
            ctx._emit_call(src_key, dst, tail)
    # box[k].method( / self.box[k].method( — subscript access into a
    # generic hint (dict[str, Widget]): the capitalized names inside
    # the outer subscript are the receiver candidates
    for m in PY_SUBSCRIPT_CALL_RE.finditer(scan_text):
        head, meth = m.group(1), m.group(2)
        parts = head.split(".")
        if len(parts) > 1 and parts[0] not in ("self", "cls"):
            continue
        hint = var_types.get(parts[-1], "")
        for vc in _hint_value_classes(hint):
            dst = ctx.class_map.get(vc, "")
            if dst and meth in ctx.files[dst].funcs:
                ctx._emit_call(src_key, dst, meth)
    # bare name( — same-file funcs, then from-import module funcs
    for m in PY_BARE_CALL_RE.finditer(scan_text):
        name = m.group(1)
        if name in PY_NON_CALLS:
            continue
        if name in fs.funcs:
            ctx._emit_call(src_key, fs.path, name)
            continue
        dst = fs.consts.get(name, "")
        if dst in ctx.files and name in ctx.files[dst].funcs:
            ctx._emit_call(src_key, dst, name)


def _module_method_dsts(mod_rel: str, meth: str, ctx) -> list[str]:
    """Files that may define `meth` reached through module `mod_rel`:
    the module itself plus the modules it imports (re-export surface —
    registries return submodules listed in their imports)."""
    if mod_rel not in ctx.files:
        return []
    cands = [mod_rel]
    mod_fs = ctx.files[mod_rel]
    for reexport in mod_fs.consts.values():
        if reexport in ctx.files and reexport != mod_rel:
            cands.append(reexport)
    return sorted({c for c in cands if meth in ctx.files[c].funcs})


def is_wiring_only(fs: FileSym) -> bool:
    """Python files always carry funcs — never wiring-only."""
    return False


def counts_dead_share(fs: FileSym) -> bool:
    """Py files never join the dead-file denominator — today's behavior,
    made explicit per-language surface now that bake resolves the hook
    through the registry (judge C1) instead of the gdscript default."""
    return False


def stat_tags(text: str) -> tuple[str, str]:
    """Python has no class_name/extends header notion — empty tags."""
    return ("", "")


# registry choreography binds (langsep) — see extractors/gdscript.py's
# _PASS_* block for the rationale (attribute dispatch is invisible to
# the module scan; the value-ref arm roots these binds).
_PASS_REBIND = rebind_reexports_sweep
_PASS_IMPORTS = import_liveness_sweep
_PASS_ARG_REFS = arg_refs_sweep
_PASS_FACTS = harvest_facts
