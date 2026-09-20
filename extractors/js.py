"""JavaScript/JSX extractor: tree-sitter front-end (issue #277).

Drives the shared ES-family engine hosted in extractors/ts.py (issue
#361): this module binds the ``ESFamily`` declarative table — the js
grammar's pure-data deltas — and keeps only its structural arms (the
CJS require/exports surface, the jsconfig-first config finder, the
bare-identifier signature grammar). The engine (walker, def
collection, entry rules, scan bodies, sweeps) is ts.py's; js -> ts is
the dependency direction (#293, the alias machinery).

Grammar pair: ``tree-sitter==0.26.0`` +
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

import re
from functools import partial
from pathlib import Path

from tree_sitter import Language, Parser, Query, QueryCursor

import tree_sitter_javascript as _jst
import tree_sitter_typescript as _tst

from extractors.common import (  # leaf module: shared text mechanics (#302)
    body_block,
    ident_child,
    line_starts_of,
    make_import_liveness_sweep,
    node_line as _line,
    node_text as _text,
)
from extractors.model import FileSym, Func
# shared ES-family machinery lives in ts.py (the #361 engine host): the
# declarative ESFamily table, the parameterized walker/sweeper family,
# the JSONC-tolerant alias reader (one extends level, longest-prefix
# paths), the React class lifecycle virtuals, the suffix sets and the
# per-ctx package-walk seen-dict (single-spelled per #293), and the
# thin-forwarder classifier for graph's dup filter (issue #364: js
# stores bodies in ts's bare-brace form, so the shared hook owns both
# ES dialects). ts.py cannot import this module back (js -> ts is the
# dependency direction).
from extractors.ts import ESFamily, JS_EXTS, TS_EXTS
from extractors.ts import TS_BASE_VIRTUALS as _BASE_VIRTUALS
from extractors.ts import _PKG_SEEN, _SPEC_ITEM_RE  # shared single spellings
from extractors.ts import (
    _entry_components_es,
    _entry_file_routes_es,
    _entry_package_es,
    _entry_tests_es,
    _harvest_facts_es,
    _parse_es_family,
    _rebind_reexports_sweep_es,
    _resolve_spec_es,
    _scan_file_es,
)
from extractors.ts import pure_delegate  # graph's dup filter hooks this attr

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
# shared front-end mechanics live in extractors.common.py (#302); the
# per-language knobs are data: identifier node types, block child name,
# '$' allowed in identifiers.
_ident_child = partial(ident_child, ident_types=_IDENT_TYPES)
_body_block = partial(body_block, block_type="statement_block")


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


# ---- module-specifier resolution: js data -----------------------------------------

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


# ---- CJS structural arms (the table's declarator/assignment hooks) -----------------

_REQUIRE_VALUE_RE = re.compile(
    r"""\Arequire\s*\(\s*(["'])([^"'`]+)\1\s*\)(?:\.([A-Za-z_$][\w$]*))?""")


def _take_require(declarator, src: bytes, path: Path, rel: str, fs: FileSym) -> None:
    """CJS require bindings at module scope.

    `const {a, b} = require('./x')` -> named from_imports (the
    `import {a, b}` analogue); `const m = require('./x')` -> default-
    binding consts + module_vars member dispatch (m.fn() resolves
    through the module); `const C = require('./x').Ctor` -> named
    from_imports. Bare require('./x') rides the regex arm in the
    shared walk. Non-literal specifiers add nothing (#116).
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
    got = _resolve_spec_es(_JS_CFG, m.group(2), path, rel)
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
        getattr(fs, _JS_CFG.exported_attr).add(_text(rhs, src))
        return
    if lt == "module.exports" and rhs.type == "object":
        for p in rhs.children:
            if p.type == "pair":
                k = _ident_child(p, src)
                if k:
                    getattr(fs, _JS_CFG.exported_attr).add(k)
            elif p.type == "shorthand_property_identifier":
                getattr(fs, _JS_CFG.exported_attr).add(_text(p, src))
    elif re.fullmatch(r"(?:module\.)?exports\.([A-Za-z_$][\w$]*)", lt):
        nm = lt.rsplit(".", 1)[1]
        getattr(fs, _JS_CFG.exported_attr).add(nm)


_CJS_EXPORTS_OBJ_RE = re.compile(r"""module\.exports\s*=\s*\{""")


def _cjs_barrel_names(text: str, fs: FileSym, names: dict) -> None:
    """CJS barrel surface (the sweep's barrel_names hook):
    module.exports = {a, b} re-exports the file's own require bindings
    under the same (or renamed) keys."""
    body = _CJS_EXPORTS_OBJ_RE.search(text)
    if not body:
        return
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


# ---- js scan data --------------------------------------------------------------------

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


# ---- js entry data ----------------------------------------------------------------------

_TEST_PATH_RE = re.compile(
    r"(?:^|/)__tests__/|\.(?:test|spec|stories)\.(?:js|jsx|mjs|cjs)$")
_CONFIG_NAME_RE = re.compile(
    r"^(?:next\.config\.|vite\.config\.|tailwind\.config\.|app\.config\.)"
    r"[^/]*$|^app\.json$")


# ---- the js table: this grammar's entire contribution to the engine -------------------

_JS_CFG = ESFamily(
    exts=JS_EXTS,
    family_exts=JS_EXTS | TS_EXTS,
    find_config=_find_config,
    resolve_suffixes=_RESOLVE_SUFFIXES,
    index_candidates=("index.js", "index.jsx", "index.ts", "index.tsx"),
    ext_rewrites=_EXT_REWRITES,
    signature=_signature,
    overload_def_keys=(),
    overload_name_keys=(),
    class_def_keys=("cls.def",),
    classes_attr="_js_classes",
    exported_attr="_js_exported",
    structural_arms=None,
    fn_scope_types=("function_declaration", "generator_function_declaration",
                    "function_expression", "arrow_function", "method_definition"),
    field_types=("field_definition", "public_field_definition"),
    namespace_types=(),
    field_name_types=("property_identifier", "private_property_identifier"),
    field_annotation_types=(),
    non_calls=JS_NON_CALLS,
    class_types=("class",),
    anon_default_types=("arrow_function", "function_expression", "class"),
    declarator_hook=_take_require,
    assignment_hook=_take_cjs_export,
    test_path_re=_TEST_PATH_RE,
    config_name_re=_CONFIG_NAME_RE,
    entry_suffixes=(".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
                    "/index.js", "/index.jsx"),
    src_index_fallback=("src/index.js", "src/index.jsx"),
    jsx_ext=".jsx",
    jsx_query=_JSX_QUERY,
    jsx_label="jsx",
    parsers=_PARSERS,
    new_re=JS_NEW_RE,
    new_local_re=JS_NEW_LOCAL_RE,
    call_re=JS_CALL_RE,
    typed_local_re=None,
    member_dispatch=False,
    barrel_attr="_js_barrel",
    sweep_suffixes=("",) + _RESOLVE_SUFFIXES,
    sweep_index=("/index.js", "/index.jsx", "/index.ts", "/index.tsx"),
    barrel_names_hook=_cjs_barrel_names,
)


def parse(path: Path, rel: str) -> FileSym:
    """Registry entry point: one FileSym per .js/.jsx/.mjs/.cjs file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    src = text.encode("utf-8")
    ext = path.suffix.lower()
    fs = FileSym(path=rel, ext=ext)
    root = _PARSERS.get(ext, _PARSERS[".js"]).parse(src).root_node
    line_starts = line_starts_of(src)
    caps = QueryCursor(_JSX_QUERY if ext == ".jsx" else _JS_QUERY).captures(root)
    _parse_es_family(_JS_CFG, root, src, path, rel, fs, line_starts, caps)
    return fs


# ---- hooks (langsep REQUIRED surface) -------------------------------------------

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


# ---- entry rules: js binds the engine on its table ---------------------------------

_entry_tests = partial(_entry_tests_es, _JS_CFG)
_entry_package = partial(_entry_package_es, _JS_CFG)
_entry_components = partial(_entry_components_es, _JS_CFG)
_entry_file_routes = partial(_entry_file_routes_es, _JS_CFG)

ENTRY_RULES = (_entry_tests, _entry_package, _entry_components, _entry_file_routes)


# ---- registry binds: the shared engine on the js table -------------------------------

harvest_facts = partial(_harvest_facts_es, _JS_CFG)
scan_file = partial(_scan_file_es, _JS_CFG)
rebind_reexports_sweep = partial(_rebind_reexports_sweep_es, _JS_CFG)

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
