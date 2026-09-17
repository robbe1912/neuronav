"""JavaScript/JSX extractor: tree-sitter front-end (issue #277).

Mirrors extractors/ts.py (#245) for the JS family: one parse per file
via the pinned grammar pair — ``tree-sitter==0.26.0`` +
``tree-sitter-javascript==0.25.0`` — with the grammar split the wheels
expose: ``.js``/``.mjs``/``.cjs`` parse under
``tree_sitter_javascript.language()`` (NOTE the binding name — the pack
is single-grammar, there is NO ``language_javascript()``; that naming
belongs to the typescript pack), ``.jsx`` under ``language_tsx()`` from
the already-pinned tree-sitter-typescript pack. The two grammars spell
class heritage differently (``class_heritage`` + ``identifier`` names
vs ``extends_clause`` + ``type_identifier``), so the def queries split
per grammar like ts.py's ts/tsx pair.

Module systems, both directions:
- ESM: ``import``/``export`` walks exactly as ts.py.
- CommonJS: ``require('x')`` calls -> import facts (destructured
  ``const {a} = require('./x')`` -> named from_imports; plain
  ``const m = require('./x')`` -> default-binding consts + module_vars
  member dispatch; bare ``require('./x')`` -> whole-module liveness);
  ``module.exports = {a, b}`` -> the export surface (CJS barrels are
  wiring-only like ts barrels); ``module.exports = <anon fn/class>``
  -> Func "default" (the CJS side of the ESM/CJS interop default).
- Mixed .ts+.js repos resolve specifiers both directions: extension-
  less candidates try the js suffixes then the ts suffixes, and
  ``.ts``-suffix specifiers rewrite to ``.js`` candidates (ts.py's
  bare-candidate gate accepts indexed JS files since #277).

NEVER read ``Node.start_point`` / ``end_point`` — py-tree-sitter 0.26.0
has a Point refcount bug (tree-sitter-py issue #472); every line number
is ``bisect`` over newline byte offsets instead (the ts/rust law).

Determinism law: captures are re-sorted by ``start_byte`` before any
emission; same-name collisions resolve first-in-file-wins; every sweep
iterates ``sorted(...)``. No rng anywhere.

Conservatism law (#116): dynamic ``require(var)`` / ``import(expr)``
record nothing unless the argument is a string literal — a miss is
fine, an invention never.

Entry roots (§ entry rules): test/spec/stories files, exported
PascalCase fn/class components (the React convention — a component is
a UI entry, the Godot-virtuals analogue), package.json entry points,
and ``createRoot``/``ReactDOM.render`` JSX targets.

Leaf parser: reads only the file being parsed (plus the project's
jsconfig.json — tsconfig.json fallback — for alias resolution) and
never imports nav or graph; cross-file work lives in the ctx-driven
sweeps below.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import sys
from collections.abc import Iterator
from functools import partial
from pathlib import Path

from tree_sitter import Language, Node, Parser, Query, QueryCursor

import tree_sitter_javascript as _jst
import tree_sitter_typescript as _tst

from extractors.common import (  # leaf module: shared text mechanics (#302)
    body_block,
    ident_child,
    last_ident,
    line_starts_of,
    make_import_liveness_sweep,
    node_line as _line,
    node_text as _text,
    receiver_env,
    rel_of_target as _rel_of_target,
)
from extractors.model import FileSym, Func
# shared ES-family machinery lives in ts.py (the JSONC-tolerant alias
# reader, one extends level, longest-prefix paths; the React class
# lifecycle virtuals; the suffix sets and the per-ctx package-walk
# seen-dict, single-spelled per #293). ts.py cannot import this module
# back (js -> ts is the dependency direction).
from extractors.ts import JS_EXTS, TS_BASE_VIRTUALS as _BASE_VIRTUALS
from extractors.ts import _PKG_SEEN, _alias_expand, _load_tsconfig

JS_LANG = Language(_jst.language())
TSX_LANG = Language(_tst.language_tsx())
_PARSERS = {ext: Parser(TSX_LANG if ext == ".jsx" else JS_LANG) for ext in JS_EXTS}

_IDENT_TYPES = ("identifier", "property_identifier", "type_identifier")

# shared def/decl capture table — valid under both grammars; the
# per-grammar extras below swap the class rows (js: class_heritage +
# identifier names; tsx: extends_clause + type_identifier names) and
# the tsx table adds the JSX rows.
_QUERY_SRC = """
(function_declaration name: (identifier) @fn) @fn.def
(generator_function_declaration name: (identifier) @fn) @gen.def
(method_definition name: (property_identifier) @m) @m.def
(lexical_declaration
  (variable_declarator name: (identifier) @lvar
    value: [(arrow_function) @lval (function_expression) @lval])) @lvdecl.def
(variable_declaration
  (variable_declarator name: (identifier) @lvar
    value: [(arrow_function) @lval (function_expression) @lval])) @vvdecl.def
"""
_JS_EXTRA = """
(class_declaration name: (identifier) @cls) @cls.def
(class_heritage) @ext
"""
_JSX_EXTRA = """
(class_declaration name: (type_identifier) @cls) @cls.def
(extends_clause) @ext
(jsx_opening_element (identifier) @jsxcomp)
(jsx_self_closing_element (identifier) @jsxcomp)
(jsx_attribute (jsx_expression (identifier) @jprop))
(jsx_attribute
  (jsx_expression (member_expression (property_identifier) @jprop)))
"""
_JS_QUERY = Query(JS_LANG, _QUERY_SRC + _JS_EXTRA)
_JSX_QUERY = Query(TSX_LANG, _QUERY_SRC + _JSX_EXTRA)


# ---- node helpers (no Point reads — module header law) --------------------------
# shared front-end mechanics live in extractors/common.py (#302); the
# per-language knobs are data: identifier node types, block child name,
# '$' allowed in identifiers.
_ident_child = partial(ident_child, ident_types=_IDENT_TYPES)
_last_ident = partial(last_ident, dollar=True)


def _find_first_ident(node, src: bytes) -> str:
    if node.type in _IDENT_TYPES:
        return _text(node, src)
    for ch in node.children:
        got = _find_first_ident(ch, src)
        if got:
            return got
    return ""


def _signature(node, src: bytes) -> tuple[list[tuple[str, str]], str]:
    """([(name, "")], "") from a def node's params (js grammar: bare
    identifiers + assignment patterns; no type annotations exist)."""
    params: list[tuple[str, str]] = []
    for ch in node.children:
        if ch.type != "formal_parameters":
            continue
        for p in ch.children:
            if p.type == "identifier":
                params.append((_text(p, src), ""))
            elif p.type == "assignment_pattern":
                pid = _ident_child(p, src)
                if pid:
                    params.append((pid, ""))
    return params, ""


_body_block = partial(body_block, block_type="statement_block")

# ---- jsconfig/tsconfig alias resolution -----------------------------------------

_CONFIG_FIND: dict[Path, Path | None] = {}


def _find_config(start: Path) -> Path | None:
    """Nearest ancestor (self included) carrying jsconfig.json — the js
    home of the paths map — falling back to tsconfig.json (mixed repos
    configure once), cached."""
    key = start.resolve()
    if key in _CONFIG_FIND:
        return _CONFIG_FIND[key]
    cur, found = key, None
    while True:
        for name in ("jsconfig.json", "tsconfig.json"):
            cand = cur / name
            if cand.is_file():
                found = cand
                break
        if found is not None or cur.parent == cur:
            break
        cur = cur.parent
    _CONFIG_FIND[key] = found
    return found


# ---- module-specifier resolution -------------------------------------------------

# extensionless candidates: js suffixes first (js file prefers js),
# then the ts suffixes for mixed repos; the ts preset's tsconfig
# convention (ts sources importing .js specifiers) inverts here:
# .ts-suffix specifiers rewrite to .js candidates.
_EXT_REWRITES = (
    (".ts", (".js", ".jsx")),
    (".tsx", (".jsx",)),
    (".mts", (".mjs",)),
    (".cts", (".cjs",)),
    (".js", (".ts", ".tsx", ".mts")),
)
_RESOLVE_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")


def _abs_candidates(abs_base: Path) -> list[Path]:
    """Filesystem candidates for an absolute base, in resolution order."""
    out = [abs_base]
    s = str(abs_base)
    for e in _RESOLVE_SUFFIXES:
        out.append(Path(s + e))
    for idx in ("index.js", "index.jsx", "index.ts", "index.tsx"):
        out.append(abs_base / idx)
    for src_e, dsts in _EXT_REWRITES:
        if s.endswith(src_e):
            out.extend(Path(s[:-len(src_e)] + d) for d in dsts)
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq



def _is_specifier_relative(spec: str) -> bool:
    return spec.startswith(("./", "../")) or spec in (".", "..")


def _resolve_spec(spec: str, path: Path, rel: str) -> str:
    """Repo-rel path of an import specifier from file `rel`, or ''.

    Relative specifiers resolve against dirname(file); alias-mapped ones
    through the project jsconfig/tsconfig (longest prefix); bare package
    names are external and record nothing. First filesystem-present
    candidate wins.
    """
    if not spec:
        return ""
    abs_bases: list[Path] = []
    if _is_specifier_relative(spec):
        abs_bases.append(Path(os.path.normpath(str(path.parent / spec))))
    else:
        cfg = _find_config(path.parent)
        if cfg is None:
            return ""  # no jsconfig/tsconfig: alias-shaped specifiers are
        loaded = _load_tsconfig(cfg)  # indistinguishable from package names
        if loaded is None:
            return ""  # unreadable: pass-through, degraded (warned once)
        aliases, base_dir = loaded
        repls = _alias_expand(spec, aliases)
        if not repls:
            return ""
        abs_bases.extend(Path(os.path.normpath(str(base_dir / r))) for r in sorted(repls))
    for abs_base in abs_bases:
        for abs_cand in _abs_candidates(abs_base):
            if not abs_cand.is_file():
                continue
            # the bare-specifier candidate only counts when it already
            # names an indexed js/ts file; suffixed/index candidates do
            # by construction
            if abs_cand == abs_base and abs_cand.suffix.lower() not in _RESOLVE_SUFFIXES:
                continue
            return _rel_of_target(abs_cand, path, rel)
    return ""


# ---- import/export structural walk ----------------------------------------------

_DYNAMIC_IMPORT_RE = re.compile(r"""\bimport\s*\(\s*(["'])([^"'`]+)\1""")
_REQUIRE_RE = re.compile(r"""(?<![\w.$])require\s*\(\s*(["'])([^"'`]+)\1""")
_MODULE_INIT_CALL_RE = re.compile(r"\s*([A-Za-z_$][\w$]*)\s*\(")
_REQUIRE_VALUE_RE = re.compile(
    r"""\Arequire\s*\(\s*(["'])([^"'`]+)\1\s*\)(?:\.([A-Za-z_$][\w$]*))?""")


def _string_of(node, src: bytes) -> str:
    t = _text(node, src)
    return t[1:-1] if len(t) >= 2 and t[0] in "\"'`" else t


def _walk_modules(root, src: bytes, path: Path, rel: str, fs: FileSym,
                  line_starts: list[int]) -> None:
    """Import/export walk + module-scope side effects + class fields.

    Function bodies are skipped: their calls are the scan's job, and no
    import/export statement is legal inside one. The CJS surface
    (require bindings, module.exports/exports assignments) is module
    scope by construction, so it rides the same walk.
    """
    stack: list[tuple[Node, bool]] = [(root, True)]
    while stack:
        node, mod = stack.pop()
        if node.type == "import_statement":
            _take_import(node, src, path, rel, fs)
            continue
        if node.type == "export_statement":
            _take_export(node, src, path, rel, fs, line_starts)
            continue
        if node.type in ("function_declaration", "generator_function_declaration",
                         "function_expression", "arrow_function", "method_definition"):
            continue
        if mod and node.type in ("lexical_declaration", "variable_declaration"):
            for ch in node.children:
                if ch.type == "variable_declarator":
                    _take_module_init(ch, src, fs)
                    _take_require(ch, src, path, rel, fs)
        if mod and node.type == "expression_statement":
            m = _MODULE_INIT_CALL_RE.match(_text(node, src))
            if m and m.group(1) not in JS_NON_CALLS:
                fs.init_calls.add(m.group(1))
        if node.type in ("field_definition", "public_field_definition"):
            _take_field(node, src, fs)
        if mod and node.type == "assignment_expression":
            _take_cjs_export(node, src, fs, line_starts)
        for ch in node.children:
            stack.append((ch, mod))
    # dynamic import()/require() literal targets ride imported_modules —
    # the import-liveness sweep turns that into whole-module referenced
    # facts. Non-literal template/variable imports add nothing (#116).
    whole = src.decode("utf-8", "replace")
    for m in _DYNAMIC_IMPORT_RE.finditer(whole):
        spec = m.group(2)
        if "${" in spec:
            continue
        got = _resolve_spec(spec, path, rel)
        if got:
            fs.imported_modules.add(got)
    for m in _REQUIRE_RE.finditer(whole):
        spec = m.group(2)
        if "${" in spec:
            continue
        got = _resolve_spec(spec, path, rel)
        if got:
            fs.imported_modules.add(got)


def _take_import(node, src: bytes, path: Path, rel: str, fs: FileSym) -> None:
    spec, clause = "", None
    for ch in node.children:
        if ch.type == "string":
            spec = _string_of(ch, src)
        elif ch.type == "import_clause":
            clause = ch
    got = _resolve_spec(spec, path, rel) if spec else ""
    if not got:
        return  # external package or unresolvable: recorded nowhere
    if clause is None:
        fs.imported_modules.add(got)  # side-effect import: whole module alive
        return
    for ch in clause.children:
        if ch.type == "identifier":  # default binding: local name -> module
            fs.from_imports.add((got, "default"))
            fs.consts.setdefault(_text(ch, src), got)
        elif ch.type == "named_imports":
            for sp in ch.children:
                if sp.type != "import_specifier":
                    continue
                names = [c for c in sp.children if c.type == "identifier"]
                if names:
                    fs.from_imports.add((got, _text(names[-1], src)))
        elif ch.type == "namespace_import":
            nm = _ident_child(ch, src)
            if nm:
                fs.imported_modules.add(got)
                fs.module_vars.setdefault(nm, "module:" + got)


def _take_export(node, src: bytes, path: Path, rel: str, fs: FileSym,
                 line_starts: list[int]) -> None:
    spec = ""
    for ch in node.children:
        if ch.type == "string":
            spec = _string_of(ch, src)
    if not spec:
        _take_default_export(node, src, fs, line_starts)
        # local export names join the exported surface (component rule)
        for ch in node.children:
            if ch.type in ("function_declaration",
                           "generator_function_declaration"):
                nm = _ident_child(ch, src)
                if nm:
                    fs._js_exported.add(nm)
            elif ch.type in ("lexical_declaration", "variable_declaration"):
                for d in ch.children:
                    if d.type == "variable_declarator":
                        dn = _ident_child(d, src)
                        if dn:
                            fs._js_exported.add(dn)
            elif ch.type == "class":
                nm = _ident_child(ch, src)
                if nm:
                    fs._js_exported.add(nm)
            elif ch.type == "export_clause":
                for sp in ch.children:
                    if sp.type != "export_specifier":
                        continue
                    names = [c for c in sp.children if c.type == "identifier"]
                    if names:
                        fs._js_exported.add(_text(names[-1], src))
        return
    got = _resolve_spec(spec, path, rel) if spec else ""
    if not got:
        return
    for ch in node.children:
        if ch.type == "export_clause":
            for sp in ch.children:
                if sp.type != "export_specifier":
                    continue
                names = [c for c in sp.children if c.type == "identifier"]
                if not names:
                    continue
                imported = _text(names[0], src)
                exported = _text(names[-1], src)
                # both spellings: the barrel's own bare-call binding AND the
                # generic _resolve_definer hop for consumers
                fs.from_imports.add((got, imported))
                if exported != imported:
                    fs.from_imports.add((got, exported))
        elif ch.type == "namespace_export":
            nm = _ident_child(ch, src)
            if nm:
                fs.from_imports.add((got, nm))


def _take_default_export(node, src: bytes, fs: FileSym,
                         line_starts: list[int]) -> None:
    """`export default <anon fn/class/expr>` -> Func "default".

    Named defaults are captured by the def queries (and their names join
    the exported surface for the component entry rule); the anonymous
    forms are only reachable here. Unique per module by construction.
    """
    if "default" in fs.funcs or not any(ch.type == "default" for ch in node.children):
        return
    for ch in node.children:
        if ch.type in ("function_declaration", "generator_function_declaration"):
            if _ident_child(ch, src):
                fs._js_exported.add(_ident_child(ch, src))  # named default
                return  # the def queries own it
            body = _body_block(ch, src) or _text(ch, src)
        elif ch.type in ("arrow_function", "function_expression", "class"):
            body = _body_block(ch, src) or _text(ch, src)
        elif ch.type == "identifier":
            fs._js_exported.add(_text(ch, src))  # export default App;
            return
        else:
            continue
        fs.funcs["default"] = Func(path=fs.path, name="default",
                                   line=_line(node, line_starts), body=body)
        return


def _take_require(declarator, src: bytes, path: Path, rel: str, fs: FileSym) -> None:
    """CJS require bindings at module scope.

    `const {a, b} = require('./x')` -> named from_imports (the
    `import {a, b}` analogue); `const m = require('./x')` -> default-
    binding consts + module_vars member dispatch (m.fn() resolves
    through the module); `const C = require('./x').Ctor` -> named
    from_imports. Bare require('./x') rides the regex arm in
    _walk_modules. Non-literal specifiers add nothing (#116).
    """
    value = None
    for ch in declarator.children:
        if ch.type in ("call_expression", "member_expression"):
            value = ch
    if value is None:
        return
    m = _REQUIRE_VALUE_RE.match(_text(value, src))
    if m is None or "${" in m.group(2):
        return
    got = _resolve_spec(m.group(2), path, rel)
    if not got:
        return
    if m.group(3):  # require('./x').Ctor — named member binding
        fs.from_imports.add((got, m.group(3)))
        return
    name = None
    for ch in declarator.children:
        if ch.type in ("identifier", "object_pattern"):
            name = ch
            break
    if name is None:
        return
    if name.type == "object_pattern":
        # destructured require: {a} binds a; {a: b} binds b (last ident)
        for p in name.children:
            nm = ""
            if p.type == "shorthand_property_identifier_pattern":
                nm = _text(p, src)
            elif p.type == "pair":
                ids = [c for c in p.children if c.type == "identifier"]
                nm = _text(ids[-1], src) if ids else ""
            if nm and re.fullmatch(r"[A-Za-z_$][\w$]*", nm):
                fs.from_imports.add((got, nm))
        return
    nm = _text(name, src)
    fs.consts.setdefault(nm, got)
    fs.module_vars.setdefault(nm, "module:" + got)


def _take_cjs_export(node, src: bytes, fs: FileSym,
                     line_starts: list[int]) -> None:
    """`module.exports = ...` / `exports.foo = ...` export facts.

    An anonymous fn/class/arrow value mints Func "default" (the CJS
    default); any other value's exported names join the exported-name
    surface for the component entry rule and barrel detection.
    """
    lhs = rhs = None
    for ch in node.children:
        if ch.type == "member_expression" and lhs is None:
            lhs = ch
        elif ch.type not in ("member_expression", "="):
            if rhs is None:
                rhs = ch
    if lhs is None or rhs is None:
        return
    lt = _text(lhs, src)
    if lt == "module.exports" and rhs.type in (
            "function_expression", "arrow_function", "class"):
        # anonymous value mints the CJS default (a named value is the
        # def queries' fn; direct children only — params hold idents)
        if rhs.type == "arrow_function" or not _ident_child(rhs, src):
            fs.funcs.setdefault("default", Func(
                path=fs.path, name="default",
                line=_line(node, line_starts),
                body=_body_block(rhs, src) or _text(rhs, src)))
            return
    if lt == "module.exports" and rhs.type == "identifier":
        fs._js_exported.add(_text(rhs, src))
        return
    if lt == "module.exports" and rhs.type == "object":
        for p in rhs.children:
            if p.type == "pair":
                k = _ident_child(p, src)
                if k:
                    fs._js_exported.add(k)
            elif p.type == "shorthand_property_identifier":
                fs._js_exported.add(_text(p, src))
    elif re.fullmatch(r"(?:module\.)?exports\.([A-Za-z_$][\w$]*)", lt):
        nm = lt.rsplit(".", 1)[1]
        fs._js_exported.add(nm)

def _take_module_init(declarator, src: bytes, fs: FileSym) -> None:
    """`const x = makeThing()` at module scope -> callee into init_calls."""
    nm = _ident_child(declarator, src)
    if not nm:
        return
    for ch in declarator.children:
        if ch.type == "call_expression":
            callee = _find_first_ident(ch, src)
            if callee and callee not in JS_NON_CALLS and callee not in ("require", "import"):
                fs.init_calls.add(callee)


def _take_field(node, src: bytes, fs: FileSym) -> None:
    """Class fields: initializer-call facts (js fields carry no types)."""
    nm = ""
    for ch in node.children:
        if ch.type in ("property_identifier", "private_property_identifier") and not nm:
            nm = _text(ch, src)
        elif ch.type == "call_expression" and nm:
            callee = _find_first_ident(ch, src)
            if callee and callee not in JS_NON_CALLS:
                fs.init_calls.add(callee)


# ---- parse ----------------------------------------------------------------------

def _names_in(node, caps, src: bytes, keys: tuple[str, ...]) -> str:
    """Earliest name capture contained in this def node (queries pair the
    def and its name in one match; containment recovers the pairing)."""
    got = _first_cap_in(node, caps, *keys)
    return _text(got, src) if got is not None else ""


def _first_cap_in(node, caps, *keys: str):
    best = None
    for k in keys:
        for n in caps.get(k, ()):
            if node.start_byte <= n.start_byte < node.end_byte:
                if best is None or n.start_byte < best.start_byte:
                    best = n
    return best


def parse(path: Path, rel: str) -> FileSym:
    """Registry entry point: one FileSym per .js/.jsx/.mjs/.cjs file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    src = text.encode("utf-8")
    ext = path.suffix.lower()
    fs = FileSym(path=rel, ext=ext)
    root = _PARSERS.get(ext, _PARSERS[".js"]).parse(src).root_node
    line_starts = line_starts_of(src)
    caps = QueryCursor(_JSX_QUERY if ext == ".jsx" else _JS_QUERY).captures(root)

    def bytewise(*keys: str) -> list:
        nodes = []
        for k in keys:
            nodes.extend(caps.get(k, ()))
        return sorted(nodes, key=lambda n: n.start_byte)

    # -- def collection (first-in-file wins on same-name collisions) ---------------
    heads: dict[str, int] = {}    # name -> first declaration line (any kind)
    impls: dict[str, tuple] = {}  # name -> (line, body, params)
    for node in bytewise("fn.def", "gen.def", "m.def", "lvdecl.def", "vvdecl.def"):
        nm = _names_in(node, caps, src, ("fn", "m", "lvar"))
        if not nm:
            continue
        line = _line(node, line_starts)
        heads.setdefault(nm, line)
        if node.type == "method_definition" and any(
                ch.type in ("get", "set") for ch in node.children):
            # get/set accessors merge by name (first wins); accessor names
            # dispatch on property access -> name-literal liveness
            fs.name_literals.add(nm)
        body = _body_block(node, src)
        sig_node = node
        if not body and node.type in ("lexical_declaration", "variable_declaration"):
            val = _first_cap_in(node, caps, "lval")
            if val is not None:
                body = _body_block(val, src) or _text(val, src)
                sig_node = val
        if body:
            params, ret = _signature(sig_node, src)
            impls.setdefault(nm, (line, body, params, ret))
    for nm in sorted(impls):
        line, body, params, ret = impls[nm]
        fs.funcs.setdefault(nm, Func(path=rel, name=nm, line=heads.get(nm, line),
                                     body=body, params=params, ret=ret))

    # -- classes: first class with a body names the file; every class's
    # -- heritage is recorded for the inheritance fill (class_map) -------
    classes = bytewise("cls.def")
    ext_nodes = bytewise("ext")
    named = False
    js_classes: list[tuple[str, str]] = []
    for node in classes:
        nm = _ident_child(node, src)
        if not nm or not any(ch.type == "class_body" for ch in node.children):
            continue
        ext_nm = ""
        for ext_node in ext_nodes:
            if not (node.start_byte <= ext_node.start_byte < node.end_byte):
                continue
            for ch in ext_node.children:
                if ch.type == "identifier":
                    ext_nm = _text(ch, src)
                elif ch.type == "member_expression":
                    ext_nm = _last_ident(_text(ch, src))
                elif ch.type == "call_expression":
                    ext_nm = _find_first_ident(ch, src)
            break
        js_classes.append((nm, ext_nm))
        if not named:
            fs.class_name = nm
            named = True
        if not fs.extends and ext_nm:
            fs.extends = ext_nm
    fs._js_classes = js_classes  # consumed by _fill_inheritance (class_map)

    # -- exported-name surface: the walk collects ESM + CJS names ---------------
    fs._js_exported = set()
    _walk_modules(root, src, path, rel, fs, line_starts)

    # -- exported PascalCase fn/class components are UI entries (§ entry) ---------
    for nm in sorted(fs._js_exported):
        if nm[:1].isupper() and (nm in fs.funcs or nm == fs.class_name):
            fs.entry_hints.add(nm)

    # -- JSX prop donations (.jsx grammar only) ------------------------------------
    for node in bytewise("jprop"):
        fs.arg_refs.add(_text(node, src))

    return fs


# ---- hooks (langsep REQUIRED surface) -------------------------------------------

from extractors.common import entry_keys  # noqa: E402  (late: package cycle)

MENTION_FLOOR = 2
DYNAMIC_HINT = re.compile(r"\beval\(|\bnew\s+Function\(|\bsetTimeout\(\s*['\"]")

# graph's dup normalizer strips these before hashing (issue #295);
# js has no triple-quote docstrings, so nothing else is declared
COMMENT_PREFIXES = ("//",)


def is_entry_exempt(name: str) -> bool:
    return False


def unresolved_base_review(name: str) -> bool:
    return name.startswith("_") or name in _BASE_VIRTUALS


def stand_in_review(fs: FileSym, name: str) -> bool:
    return False


def mention_review(name: str, mentions: dict) -> bool:
    return mentions.get(name, 0) >= MENTION_FLOOR


def is_wiring_only(fs: FileSym) -> bool:
    """CJS barrels (`module.exports = {a, b}` over require bindings) and
    ESM re-export-only modules are wiring, not logic: zero callable
    surface of their own (the ts-barrel / ambient-.d.ts analogue)."""
    if fs.ext not in JS_EXTS:
        return False
    return bool(getattr(fs, "_js_barrel", False))  # zero own funcs by construction


def counts_dead_share(fs: FileSym) -> bool:
    return fs.ext in {".js", ".jsx"}


def stat_tags(text: str) -> tuple[str, str]:
    return ("", "")


# ---- entry rules ------------------------------------------------------------------

_TEST_PATH_RE = re.compile(
    r"(?:^|/)__tests__/|\.(?:test|spec|stories)\.(?:js|jsx|mjs|cjs)$")
_CONFIG_NAME_RE = re.compile(
    r"^(?:next\.config\.|vite\.config\.|tailwind\.config\.|app\.config\.)"
    r"[^/]*$|^app\.json$")


def _entry_tests(fs: FileSym, ctx) -> Iterator[str]:
    if fs.ext not in JS_EXTS:
        return
    if _TEST_PATH_RE.search(fs.path):
        yield from entry_keys(fs, sorted(fs.funcs))


def _export_leaves(exp: dict, out: list) -> None:
    for k in sorted(exp):
        v = exp[k]
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            _export_leaves(v, out)


def _entry_candidates(spec: str, pkg_dir: str, ctx) -> str:
    """Resolve a package entry specifier against ctx.files (§ order)."""
    if not isinstance(spec, str) or not spec:
        return ""
    base = posixpath.normpath(posixpath.join(pkg_dir, spec))
    for cand in (base, base + ".js", base + ".jsx", base + ".mjs", base + ".cjs",
                 base + ".ts", base + ".tsx",
                 base + "/index.js", base + "/index.jsx"):
        if cand in ctx.files:
            return cand
    for src_e, dsts in _EXT_REWRITES:
        if base.endswith(src_e):
            for d in dsts:
                if base[:-len(src_e)] + d in ctx.files:
                    return base[:-len(src_e)] + d
    return ""


def _entry_package(fs: FileSym, ctx) -> Iterator[str]:
    if fs.ext not in JS_EXTS:
        return
    parts = fs.path.split("/")
    if ((len(parts) == 1 and _CONFIG_NAME_RE.match(parts[0]))
            or (len(parts) == 2 and parts[0] == ".storybook")):
        yield from entry_keys(fs, sorted(fs.funcs))  # framework config = entry
        return
    done = _PKG_SEEN.setdefault(ctx, set())
    if done:
        return
    done.add("")  # sentinel: the root walk itself is one-shot per ctx —
    # trees with no package.json anywhere must not re-walk per
    # ES-family file (the seen-dict is shared with ts.py, #293)
    root_dir = ctx.path_for("")
    for path in sorted(ctx.walk_root_files({".json"})):
        rel = path.relative_to(root_dir).as_posix()
        pp = rel.split("/")
        if not (rel == "package.json"
                or (len(pp) == 3 and pp[0] == "packages" and pp[2] == "package.json")):
            continue
        done.add(rel)
        try:
            pkg = json.loads(ctx.read_file(rel))
        except (OSError, ValueError):
            continue
        if not isinstance(pkg, dict):
            continue
        pkg_dir = "/".join(pp[:-1])
        specs: list[str] = []
        for key in ("main", "module"):
            v = pkg.get(key)
            if isinstance(v, str):
                specs.append(v)
        binv = pkg.get("bin")
        if isinstance(binv, str):
            specs.append(binv)
        elif isinstance(binv, dict):
            specs.extend(binv.values())
        exp = pkg.get("exports")
        if isinstance(exp, dict):
            _export_leaves(exp, specs)
        for spec in sorted(set(specs)):
            entry = _entry_candidates(spec, pkg_dir, ctx)
            if not entry:
                # deterministic build-artifact fallback: dist/stub specifiers
                # that no walked file matches land on src/index.js[x]
                for cand in (posixpath.normpath(pkg_dir + "/src/index.js"),
                             posixpath.normpath(pkg_dir + "/src/index.jsx")):
                    if cand in ctx.files:
                        entry = cand
                        break
            if entry and entry in ctx.files:
                yield from entry_keys(ctx.files[entry], sorted(ctx.files[entry].funcs))


def _entry_components(fs: FileSym, ctx) -> Iterator[str]:
    """Exported PascalCase fn/class components (React convention) and
    the exported `default` of component modules — a component is a UI
    entry, so dead tiers never false-flag one."""
    if fs.ext not in JS_EXTS:
        return
    yield from entry_keys(fs, sorted(fs.entry_hints))


ENTRY_RULES = (_entry_tests, _entry_package, _entry_components)


# ---- facts + call scanning ---------------------------------------------------------

JS_NON_CALLS = frozenset({
    "if", "for", "while", "switch", "catch", "return", "typeof", "new", "await",
    "yield", "function", "import", "export", "default", "void", "delete", "in",
    "of", "do", "else", "try", "finally", "throw", "case", "super", "this",
    "class", "extends", "const", "let", "var", "async", "get", "set",
    "static", "constructor",
})

# head chain + callee before the paren (the ts type-argument bracket arm
# is dropped: js has no generic-call syntax, and `<`/`>` stay comparisons)
JS_NEW_RE = re.compile(r"\bnew\s+([A-Za-z_$][\w$]*)\s*\(")
JS_NEW_LOCAL_RE = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*new\s+([A-Za-z_$][\w$]*)")
JS_CALL_RE = re.compile(
    r"(?<![\w.$])((?:[A-Za-z_$][\w$]*\.)*)"
    r"([A-Za-z_$][\w$]*)\s*\(")


def harvest_facts(fs: FileSym, ctx) -> None:
    if fs.ext not in JS_EXTS:
        return
    for nm in sorted(fs.name_literals):
        ctx.referenced_names.add(nm)
    for nm in sorted(fs.init_calls):
        ctx.referenced_names.add(nm)
    for nm in sorted(fs.arg_refs):
        if nm in fs.funcs:
            ctx.referenced.add(fs.funcs[nm].key)


def _import_target(fs: FileSym, name: str, ctx) -> tuple[str, str] | None:
    """(file, fn) an import/require-bound name resolves to, else None.

    Default bindings (consts) and named pairs both try the binding's
    own name first, then the module default export.
    """
    tgt = fs.consts.get(name, "")
    if tgt and tgt in ctx.files:
        tfs = ctx.files[tgt]
        if name in tfs.funcs:
            return (tgt, name)
        if "default" in tfs.funcs:
            return (tgt, "default")
    for t, nm in sorted(fs.from_imports):
        if nm == name and t in ctx.files:
            tfs = ctx.files[t]
            if nm in tfs.funcs:
                return (t, nm)
            if "default" in tfs.funcs:
                return (t, "default")
    return None


def _module_dsts(mod_rel: str, name: str, ctx) -> list[tuple[str, str]]:
    """Namespace member call: target files defining `name`."""
    out: list[tuple[str, str]] = []
    tfs = ctx.files.get(mod_rel)
    if tfs is None:
        return out
    if name in tfs.funcs:
        out.append((mod_rel, name))
    for tgt, nm in sorted(tfs.from_imports):
        if nm == name and tgt in ctx.files:
            t2 = ctx.files[tgt]
            fn = nm if nm in t2.funcs else ("default" if "default" in t2.funcs else "")
            if fn:
                out.append((tgt, fn))
    return out


_WARNED_JSX_READ = False

def _jsx_sites(path: Path, fs: FileSym) -> list[tuple[str, int]]:
    """Capitalized JSX element names + their lines (.jsx grammar only)."""
    global _WARNED_JSX_READ
    try:
        src = path.read_bytes()
    except OSError:
        # scan_file runs after parse read the same file fine; a failure
        # here means it vanished mid-build — loud once, never per file
        if not _WARNED_JSX_READ:
            _WARNED_JSX_READ = True
            print(f"neuronav: jsx scan re-read failed at {path}: "
                  "sites skipped for this file", file=sys.stderr)
        return []
    caps = QueryCursor(_JSX_QUERY).captures(_PARSERS[".jsx"].parse(src).root_node)
    line_starts = line_starts_of(src)
    sites = [(name, _line(node, line_starts))
             for node in sorted(caps.get("jsxcomp", ()), key=lambda n: n.start_byte)
             for name in (_text(node, src),) if name[:1].isupper()]
    return sites


def scan_file(fs: FileSym, ctx) -> None:
    if fs.ext not in JS_EXTS:
        return
    ordered = sorted(fs.funcs.items(), key=lambda kv: (kv[1].line, kv[0]))

    def container(lineno: int) -> Func | None:
        hit = None
        for _, fn in ordered:
            if fn.line <= lineno:
                hit = fn
            else:
                break
        return hit

    if fs.ext == ".jsx":
        for name, line in _jsx_sites(ctx.path_for(fs.path), fs):
            fn = container(line)
            got = _import_target(fs, name, ctx)
            if fn is None:
                # module scope = a render site (createRoot(...).render(
                # <App/>), ReactDOM.render): the target is an entry root
                # so dead tiers never flag the mounted component
                if name in fs.funcs:
                    ctx.roots.add(fs.funcs[name].key)
                elif got:
                    ctx.roots.add(f"{got[0]}::{got[1]}")
                continue
            if got:
                ctx._emit_call(fn.key, got[0], got[1])
            else:
                ctx.referenced_names.add(name)  # unresolved comp: name-level alive
    for _, fn in ordered:
        _scan_body_js(fs, fn, ctx)


def _scan_body_js(fs: FileSym, fn: Func, ctx) -> None:
    src_key, body, var_types = receiver_env(fs, fn)
    for m in JS_NEW_LOCAL_RE.finditer(body):
        var_types[m.group(1)] = m.group(2)
    new_spans = []
    for m in JS_NEW_RE.finditer(body):
        cls = m.group(1)
        new_spans.append(m.span())
        if cls in ctx.class_map:
            dst = ctx.class_map[cls]
            if "constructor" in ctx.files[dst].funcs:
                ctx._emit_call(src_key, dst, "constructor")
    for m in JS_CALL_RE.finditer(body):
        head, name = m.group(1), m.group(2)
        if not name or name in JS_NON_CALLS:
            continue
        if any(s <= m.start(2) < e for s, e in new_spans):
            continue  # callee of a new-expression: the constructor arm owns it
        if not head:  # bare call: local -> import-bound -> drop
            if name in fs.funcs:
                ctx._emit_call(src_key, fs.path, name)
                continue
            got = _import_target(fs, name, ctx)
            if got:
                ctx._emit_call(src_key, got[0], got[1])
            continue
        parts = head.rstrip(".").split(".")
        base_head = parts[0]
        if base_head == "super":
            base_rel = ctx.class_map.get(fs.extends, "")
            tfs = ctx.files.get(base_rel, None)
            if tfs is not None and name in tfs.funcs:
                ctx._emit_call(src_key, base_rel, name)
            else:
                ctx.referenced_names.add(name)
        elif base_head == "this":
            if name in fs.funcs:
                ctx._emit_call(src_key, fs.path, name)
            else:
                ctx.referenced_names.add(name)
        else:
            cls = var_types.get(base_head, "")
            if cls.startswith("module:"):
                mod_rel = cls[len("module:"):]
                hit = False
                for dst, fname in _module_dsts(mod_rel, name, ctx):
                    ctx._emit_call(src_key, dst, fname)
                    hit = True
                if not hit:
                    ctx.referenced_names.add(name)
            elif cls and _last_ident(cls) in ctx.class_map:
                dst = ctx.class_map[_last_ident(cls)]
                tfs = ctx.files.get(dst, None)
                if tfs is not None and name in tfs.funcs:
                    ctx._emit_call(src_key, dst, name)
                else:
                    ctx.referenced_names.add(name)
            else:
                ctx.referenced_names.add(name)


# ---- sweeps ------------------------------------------------------------------------

_STAR_EXPORT_RE = re.compile(
    r"""export\s*\*\s*(?:as\s+([A-Za-z_$][\w$]*)\s+)?from\s*(["'])([^"']+)\2""")
_REEXPORT_CLAUSE_RE = re.compile(
    r"""export\s*\{([^}]*)\}\s*from\s*(["'])([^"']+)\2""")
_SPEC_ITEM_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*(?:as\s+([A-Za-z_$][\w$]*))?")
_CJS_EXPORTS_OBJ_RE = re.compile(r"""module\.exports\s*=\s*\{""")


def _sweep_resolve(spec: str, rel: str, ctx) -> str:
    """ctx.files-truth resolution for sweep passes (parse-time resolution
    without a live Path: same candidate order, no filesystem)."""
    if not spec or not _is_specifier_relative(spec):
        return ""
    d = posixpath.dirname(rel)
    base = posixpath.normpath(posixpath.join(d, spec) if d else spec)
    for e in ("",) + _RESOLVE_SUFFIXES:
        if base + e in ctx.files:
            return base + e
    for idx in ("/index.js", "/index.jsx", "/index.ts", "/index.tsx"):
        if base + idx in ctx.files:
            return base + idx
    for src_e, dsts in _EXT_REWRITES:
        if base.endswith(src_e):
            for dd in dsts:
                if base[:-len(src_e)] + dd in ctx.files:
                    return base[:-len(src_e)] + dd
    return ""


def _fill_inheritance(ctx) -> None:
    """JS classes feed ctx.class_map and ctx._subclasses so _emit_call
    resolves receivers and mirrors overrides (the graph surfaces)."""
    for rel in sorted(ctx.files):
        fs = ctx.files[rel]
        if fs.ext not in JS_EXTS:
            continue
        for cls_name, ext_nm in getattr(fs, "_js_classes", ()):
            ctx.class_map.setdefault(cls_name, rel)
            if ext_nm:
                ctx._subclasses.setdefault(ext_nm, set()).add(rel)
    changed = True
    while changed:  # transitive closure, deterministic fixed point
        changed = False
        for base in sorted(ctx._subclasses):
            subs = ctx._subclasses[base]
            add = set()
            for sub in sorted(subs):
                sub_cls = ctx.files[sub].class_name
                if sub_cls:
                    add.update(ctx._subclasses.get(sub_cls, ()))
            add -= subs
            if add:
                subs |= add
                changed = True


def rebind_reexports_sweep(ctx) -> None:
    """Rewrite consumers' re-exported bindings to the ORIGIN definer.

    Runs first in the js sweep pair so inheritance mirrors exist before
    scan_file mints edges. Files whose only exports are re-exports —
    ESM barrels or CJS `module.exports = {a, b}` over require
    bindings — are barrels -> is_wiring_only.
    """
    _fill_inheritance(ctx)
    tables: dict[str, tuple[dict[str, tuple[str, str]], list[str]]] = {}
    for rel in sorted(ctx.files):
        fs = ctx.files[rel]
        if fs.ext not in JS_EXTS:
            continue
        try:
            text = ctx.read_file(rel)
        except OSError:
            continue
        names: dict[str, tuple[str, str]] = {}
        stars: list[str] = []
        for m in _REEXPORT_CLAUSE_RE.finditer(text):
            org = _sweep_resolve(m.group(3), rel, ctx)
            for got in _SPEC_ITEM_RE.findall(m.group(1)):
                nm_in = got[0]
                nm_out = got[1] or got[0]
                names[nm_out] = (org, nm_in)
        for m in _STAR_EXPORT_RE.finditer(text):
            org = _sweep_resolve(m.group(3), rel, ctx)
            if m.group(1):
                names[m.group(1)] = (org, "*")
            else:
                stars.append(org)
        body = _CJS_EXPORTS_OBJ_RE.search(text)
        if body:
            # CJS barrel surface: module.exports = {a, b} re-exports the
            # file's own require bindings under the same (or renamed) keys
            req_org = {nm: tgt for tgt, nm in fs.from_imports}
            obj = text[body.end() - 1:]
            depth, end = 0, -1
            for i, ch in enumerate(obj):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            inner = obj[1:end] if end > 0 else ""
            for got in _SPEC_ITEM_RE.findall(inner):
                nm_in, nm_out = got[0], got[1] or got[0]
                if nm_in in req_org:
                    names[nm_out] = (req_org[nm_in], nm_in)
        tables[rel] = (names, stars)
        if not fs.funcs and (names or stars):
            fs._js_barrel = True
    cache: dict[tuple[str, str, frozenset], tuple[str, bool]] = {}

    def origin(target: str, nm: str, seen: frozenset = frozenset()) -> tuple[str, bool]:
        """(definer rel, is_namespace_export) following re-export chains."""
        key = (target, nm, seen)
        if key in cache:
            return cache[key]
        if not target or target in seen or target not in ctx.files:
            return ("", False)
        tfs = ctx.files[target]
        names, stars = tables.get(target, ({}, []))
        if nm != "*" and nm in tfs.funcs:
            return (target, False)
        if nm in names:
            org, nxt = names[nm]
            if nxt == "*":
                return (org, True)
            got = origin(org, nxt, seen | {target})
            if not got[0] and org in ctx.files and nm in ctx.files[org].funcs:
                got = (org, False)
            cache[key] = got
            return got
        if nm != "*":
            hop = ctx._resolve_definer(target, nm, seen | {target})
            if hop and hop != target:
                cache[key] = (hop, False)
                return (hop, False)
        for s in sorted(stars):
            got = origin(s, nm, seen | {target})
            if got[0]:
                cache[key] = got
                return got
        return ("", False)

    for rel in sorted(ctx.files):
        fs = ctx.files[rel]
        if fs.ext not in JS_EXTS:
            continue
        rebound = set()
        for target, nm in sorted(fs.from_imports):
            org, is_ns = origin(target, nm)
            if is_ns and org:
                fs.module_vars.setdefault(nm, "module:" + org)
            else:
                rebound.add((org or target, nm))
        fs.from_imports = rebound
        for nm in sorted(fs.consts):
            tgt = fs.consts[nm]
            if tgt in ctx.files and not tgt.startswith("<"):
                org, _ = origin(tgt, "default")
                if org and org != tgt:
                    fs.consts[nm] = org


import_liveness_sweep = make_import_liveness_sweep(
    JS_EXTS,
    "Star/side-effect/dynamic imports and bare requires: the whole "
    "target module's funcs enter ctx.referenced.")


# registry choreography binds (langsep) — see extractors/python.py's
# _PASS_* block for the rationale (attribute dispatch is invisible to
# the module scan; the value-ref arm roots these binds).
_PASS_REBIND = rebind_reexports_sweep
_PASS_IMPORTS = import_liveness_sweep
_PASS_FACTS = harvest_facts
