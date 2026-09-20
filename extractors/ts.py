"""TypeScript extractor: tree-sitter-typescript front-end (issue #245).

Strategy: one parse per file via the pinned grammar pair —
``tree-sitter==0.26.0`` + ``tree-sitter-typescript==0.23.2`` — with the
grammar split the wheels expose: `.ts`/`.mts`/`.cts` parse under
``language_typescript()`` (the TS grammar rejects JSX with ERROR nodes),
`.tsx` under ``language_tsx()``. Defs come from one ordered S-expression
Query per grammar; import/export statements get a structural walk
(specifier pairing needs field access a query cannot express).

Since issue #361 this module also HOSTS the shared ES-family walker/
sweeper engine: the ``ESFamily`` declarative table plus the
parameterized engine functions below. js.py binds its own table — the
js grammar's pure-data deltas (suffix orders, NON_CALLS vocabulary,
node-type tuples, CJS hooks) — and drives the same engine, so the old
~800-line mirror is one engine now. Structural arms stay per-module
(langsep law): this module keeps the tsconfig loader, typed
signatures, the overload pre-pass and the enum/interface/decorator
parse arms; js.py keeps its CJS require/exports surface.

NEVER read ``Node.start_point`` / ``end_point`` — py-tree-sitter 0.26.0
has a Point refcount bug (tree-sitter-py issue #472); every line number
is ``bisect`` over newline byte offsets instead.

Determinism law: captures are re-sorted by ``start_byte`` before any
emission; same-name collisions resolve first-in-file-wins (the cpp
overload law — overload runs collapse to the FIRST declaration's line
with the implementation's body, ambient-only runs vanish); every sweep
iterates ``sorted(...)``. No rng anywhere.

Leaf parser: reads only the file being parsed (plus the project's
tsconfig.json for alias resolution) and never imports nav or graph —
cross-file work lives in the ctx-driven sweeps below.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import sys
import weakref
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from tree_sitter import Language, Node, Parser, Query, QueryCursor

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

TS_EXTS = frozenset({".ts", ".tsx", ".mts", ".cts"})
# js-family suffix set ships here beside its ts twin (single spelling,
# #293): js.py imports it — js -> ts is the dependency direction (the
# alias machinery), so a ts -> js import would cycle.
JS_EXTS = frozenset({".js", ".jsx", ".mjs", ".cjs"})
TS_LANG = Language(_tst.language_typescript())
TSX_LANG = Language(_tst.language_tsx())
_PARSERS = {ext: Parser(TSX_LANG if ext == ".tsx" else TS_LANG) for ext in TS_EXTS}

_IDENT_TYPES = ("identifier", "property_identifier", "type_identifier")

# shared def/decl capture table (the tsx query adds the JSX rows)
_QUERY_SRC = """
(function_declaration name: (identifier) @fn) @fn.def
(generator_function_declaration name: (identifier) @fn) @gen.def
(function_signature name: (identifier) @sig) @sig.def
(method_definition name: (property_identifier) @m) @m.def
(method_signature name: (property_identifier) @msig) @msig.def
(lexical_declaration
  (variable_declarator name: (identifier) @lvar
    value: [(arrow_function) @lval (function_expression) @lval])) @lvdecl.def
(variable_declaration
  (variable_declarator name: (identifier) @lvar
    value: [(arrow_function) @lval (function_expression) @lval])) @vvdecl.def
(class_declaration name: (type_identifier) @cls) @cls.def
(abstract_class_declaration name: (type_identifier) @cls) @acls.def
(extends_clause) @ext
(implements_clause (type_identifier) @impl)
(enum_declaration name: (identifier) @enum) @enum.def
(enum_body (property_identifier) @enbare)
(enum_assignment name: (property_identifier) @enasg)
(type_alias_declaration name: (type_identifier) @ta) @ta.def
(interface_declaration name: (type_identifier) @iface) @iface.def
(decorator) @dec
"""
_TSX_EXTRA = """
(jsx_opening_element (identifier) @jsxcomp)
(jsx_self_closing_element (identifier) @jsxcomp)
(jsx_attribute (jsx_expression (identifier) @jprop))
(jsx_attribute
  (jsx_expression (member_expression (property_identifier) @jprop)))
"""
_TS_QUERY = Query(TS_LANG, _QUERY_SRC)
_TSX_QUERY = Query(TSX_LANG, _QUERY_SRC + _TSX_EXTRA)


# ---- node helpers (no Point reads — module header law) --------------------------
# shared front-end mechanics live in extractors.common.py (#302); the
# per-language knobs are data: identifier node types, block child name,
# '$' allowed in identifiers.
_ident_child = partial(ident_child, ident_types=_IDENT_TYPES)
_last_ident = partial(last_ident, dollar=True)
_body_block = partial(body_block, block_type="statement_block")


def _find_first_ident(node, src: bytes) -> str:
    if node.type in _IDENT_TYPES:
        return _text(node, src)
    for ch in node.children:
        got = _find_first_ident(ch, src)
        if got:
            return got
    return ""


def _type_text(node, src: bytes) -> str:
    """`: Widget` annotation text without the leading colon."""
    return _text(node, src).lstrip()[1:].strip()


def _signature(node, src: bytes) -> tuple[list[tuple[str, str]], str]:
    """([(name, type)], ret) from a def node's params + return annotation."""
    params: list[tuple[str, str]] = []
    ret = ""
    for ch in node.children:
        if ch.type == "formal_parameters":
            for p in ch.children:
                if p.type not in ("required_parameter", "optional_parameter"):
                    continue
                pid = pty = ""
                for pc in p.children:
                    if pc.type == "identifier" and not pid:
                        pid = _text(pc, src)
                    elif pc.type == "type_annotation" and not pty:
                        pty = _type_text(pc, src)
                    elif pc.type == "new_expression" and not pty:
                        pty = _find_first_ident(pc, src)
                if pid:
                    params.append((pid, pty))
        elif ch.type == "type_annotation" and not ret:
            ret = _type_text(ch, src)
    return params, ret


# ---- ES-family shared engine (issue #361) -----------------------------------------
# ts.py and js.py were a ~800-line near-verbatim mirror whose every
# behavioral difference is DATA. The engine below is parameterized by
# one declarative table per dialect; js.py imports this section and
# binds its own ESFamily. Host = ts.py because js -> ts was already the
# dependency direction (#293, the alias machinery); NOT common.py —
# langsep law keeps language facts at language modules, and every field
# below IS a language fact spelled where its grammar lives.


@dataclass
class ESFamily:
    """Declarative per-language table driving the shared ES-family
    walker/sweeper engine (issue #361).

    Each field is one axis the two grammars differ on; the engine never
    branches on language names. Structural arms (ts: tsconfig loader,
    typed signatures, overload pre-pass, enum/interface/decorator parse
    arms; js: CJS require/exports surface, jsconfig fallback, bare
    signatures) stay in their language modules and enter only as the
    callable hook fields. Deliberately not frozen: the tshard/jshard
    sabotage legs flip one knob in place — the partial binds all share
    the table object."""

    # -- identity + suffix sets ------------------------------------------------
    exts: frozenset                 # the dialect's registered suffixes
    family_exts: frozenset          # bare-candidate gate (the ts|js union)
    # -- module-specifier resolution (§1.4) ------------------------------------
    find_config: Callable           # tsconfig / jsconfig-first finder
    resolve_suffixes: tuple         # extensionless candidate order
    index_candidates: tuple         # directory-index candidate order
    ext_rewrites: tuple             # suffix rewrites (.js->.ts / .ts->.js)
    # -- def/class collection --------------------------------------------------
    signature: Callable             # typed (ts) / bare-identifier (js)
    overload_def_keys: tuple        # signature-only pre-pass (ts) / empty
    overload_name_keys: tuple       # name captures pairing the pre-pass
    class_def_keys: tuple           # cls.def (+ acls.def under ts)
    classes_attr: str               # FileSym attr for (name, extends) pairs
    exported_attr: str              # FileSym attr for the exported-name set

    # -- module walk -----------------------------------------------------------
    fn_scope_types: tuple           # node types whose bodies the walk skips
    field_types: tuple              # class-field node types
    namespace_types: tuple          # ts ambient module/namespace arms
    field_name_types: tuple         # field-name identifier types
    field_annotation_types: tuple   # ts type annotations on fields
    non_calls: frozenset            # keyword-looking-callee vocabulary
    class_types: tuple              # class node types (ts adds abstract)
    anon_default_types: tuple       # anon `export default` value types

    # -- entry rules -----------------------------------------------------------
    test_path_re: re.Pattern
    config_name_re: re.Pattern
    entry_suffixes: tuple           # package-entry candidate suffix order
    src_index_fallback: tuple       # dist/stub fallback index spellings
    # -- call scanning ---------------------------------------------------------
    jsx_ext: str                    # the JSX-grammar suffix
    jsx_query: Query
    jsx_label: str                  # warn-once cell key ("tsx"/"jsx")
    parsers: dict
    new_re: re.Pattern
    new_local_re: re.Pattern
    call_re: re.Pattern

    # -- sweeps (§1.8) ----------------------------------------------------------
    barrel_attr: str                # FileSym attr marking barrels
    sweep_suffixes: tuple
    sweep_index: tuple


    # -- structural + dialect hooks (default off; the tables spell them) --------
    structural_arms: Callable | None = None   # ts enums/interfaces/decorators
    declarator_hook: Callable | None = None    # js CJS require bindings
    assignment_hook: Callable | None = None    # js module.exports/exports.foo
    typed_local_re: re.Pattern | None = None  # ts `const x: T` receiver typing
    member_dispatch: bool = False   # ts this.member typed-receiver dispatch
    barrel_names_hook: Callable | None = None  # js CJS barrel surface


# ---- shared engine: pure helpers (no table needed) -------------------------------

def _is_specifier_relative(spec: str) -> bool:
    return spec.startswith(("./", "../")) or spec in (".", "..")


def _string_of(node, src: bytes) -> str:
    t = _text(node, src)
    return t[1:-1] if len(t) >= 2 and t[0] in "\"'`" else t


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


def _export_leaves(exp: dict, out: list) -> None:
    for k in sorted(exp):
        v = exp[k]
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            _export_leaves(v, out)


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


# ---- shared engine: specifier resolution ------------------------------------------


def _abs_candidates_es(cfg, abs_base: Path) -> list[Path]:
    """Filesystem candidates for an absolute base, in the family's §1.4
    order (the table's suffix/index orders spell the dialect)."""
    out = [abs_base]
    s = str(abs_base)
    for e in cfg.resolve_suffixes:
        out.append(Path(s + e))
    for idx in cfg.index_candidates:
        out.append(abs_base / idx)
    for src_e, dsts in cfg.ext_rewrites:
        if s.endswith(src_e):
            out.extend(Path(s[:-len(src_e)] + d) for d in dsts)
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _resolve_spec_es(cfg, spec: str, path: Path, rel: str) -> str:
    """Repo-rel path of an import specifier from file `rel`, or ''.

    Relative specifiers resolve against dirname(file); alias-mapped ones
    through the project config (the shared tsconfig loader; the js
    table's finder prefers jsconfig.json), longest prefix; bare package
    names are external and record nothing. First filesystem-present
    candidate wins.
    """
    if not spec:
        return ""
    abs_bases: list[Path] = []
    if _is_specifier_relative(spec):
        abs_bases.append(Path(os.path.normpath(str(path.parent / spec))))
    else:
        config_path = cfg.find_config(path.parent)
        if config_path is None:
            return ""  # no config: alias-shaped specifiers are
        loaded = _load_tsconfig(config_path)  # indistinguishable from pkg names
        if loaded is None:
            return ""  # unreadable: pass-through, degraded (warned once)
        aliases, base_dir = loaded
        repls = _alias_expand(spec, aliases)
        if not repls:
            return ""
        abs_bases.extend(Path(os.path.normpath(str(base_dir / r))) for r in sorted(repls))
    for abs_base in abs_bases:
        for abs_cand in _abs_candidates_es(cfg, abs_base):
            if not abs_cand.is_file():
                continue
            # the bare-specifier candidate only counts when it already
            # names an indexed ES-family file — ts or js (#277 opened
            # the js direction; the suffix sets keep one spelling, #293)
            if (abs_cand == abs_base
                    and abs_cand.suffix.lower() not in cfg.family_exts):
                continue
            return _rel_of_target(abs_cand, path, rel)
    return ""


# ---- shared engine: import/export structural walk ----------------------------------

_DYNAMIC_IMPORT_RE = re.compile(r"""\bimport\s*\(\s*(["'])([^"'`]+)\1""")
_REQUIRE_RE = re.compile(r"""(?<![\w.$])require\s*\(\s*(["'])([^"'`]+)\1""")
_MODULE_INIT_CALL_RE = re.compile(r"\s*([A-Za-z_$][\w$]*)\s*\(")


def _take_import_es(cfg, node, src: bytes, path: Path, rel: str,
                    fs: FileSym) -> None:
    spec, clause = "", None
    for ch in node.children:
        if ch.type == "string":
            spec = _string_of(ch, src)
        elif ch.type == "import_clause":
            clause = ch
    got = _resolve_spec_es(cfg, spec, path, rel) if spec else ""
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


def _take_export_es(cfg, node, src: bytes, path: Path, rel: str, fs: FileSym,
                    line_starts: list[int]) -> None:
    spec = ""
    for ch in node.children:
        if ch.type == "string":
            spec = _string_of(ch, src)
    if not spec:
        _take_default_export_es(cfg, node, src, fs, line_starts)
        # local export names join the exported surface (component rule)
        for ch in node.children:
            if ch.type in ("function_declaration",
                           "generator_function_declaration"):
                nm = _ident_child(ch, src)
                if nm:
                    getattr(fs, cfg.exported_attr).add(nm)
            elif ch.type in ("lexical_declaration", "variable_declaration"):
                for d in ch.children:
                    if d.type == "variable_declarator":
                        dn = _ident_child(d, src)
                        if dn:
                            getattr(fs, cfg.exported_attr).add(dn)
            elif ch.type in cfg.class_types:
                nm = _ident_child(ch, src)
                if nm:
                    getattr(fs, cfg.exported_attr).add(nm)
            elif ch.type == "export_clause":
                for sp in ch.children:
                    if sp.type != "export_specifier":
                        continue
                    names = [c for c in sp.children if c.type == "identifier"]
                    if names:
                        getattr(fs, cfg.exported_attr).add(_text(names[-1], src))
        return
    got = _resolve_spec_es(cfg, spec, path, rel) if spec else ""
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


def _take_default_export_es(cfg, node, src: bytes, fs: FileSym,
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
                getattr(fs, cfg.exported_attr).add(_ident_child(ch, src))  # named default
                return  # the def queries own it
            body = _body_block(ch, src) or _text(ch, src)
        elif ch.type in cfg.anon_default_types:
            body = _body_block(ch, src) or _text(ch, src)
        elif ch.type == "identifier":
            getattr(fs, cfg.exported_attr).add(_text(ch, src))  # export default App;
            return
        else:
            continue
        fs.funcs["default"] = Func(path=fs.path, name="default",
                                   line=_line(node, line_starts), body=body)
        return


def _take_module_init_es(cfg, declarator, src: bytes, fs: FileSym) -> None:
    """`const x = makeThing()` at module scope -> callee into init_calls."""
    nm = _ident_child(declarator, src)
    if not nm:
        return
    for ch in declarator.children:
        if ch.type == "call_expression":
            callee = _find_first_ident(ch, src)
            if callee and callee not in cfg.non_calls and callee not in ("require", "import"):
                fs.init_calls.add(callee)


def _take_field_es(cfg, node, src: bytes, fs: FileSym) -> None:
    """Class fields: typed members (ts table: type annotations) +
    initializer-call facts."""
    nm = ""
    for ch in node.children:
        if ch.type in cfg.field_name_types and not nm:
            nm = _text(ch, src)
        elif ch.type in cfg.field_annotation_types and nm:
            fs.members.setdefault(nm, _type_text(ch, src))
        elif ch.type == "call_expression" and nm:
            callee = _find_first_ident(ch, src)
            if callee and callee not in cfg.non_calls:
                fs.init_calls.add(callee)


def _walk_modules_es(cfg, root, src: bytes, path: Path, rel: str, fs: FileSym,
                     line_starts: list[int]) -> None:
    """Import/export walk + module-scope side effects + class fields.

    Function bodies are skipped: their calls are the scan's job, and no
    import/export statement is legal inside one. The js table hooks the
    CJS surface (require bindings, module.exports/exports assignments)
    onto the same walk — module scope by construction.
    """
    stack: list[tuple[Node, bool]] = [(root, True)]
    while stack:
        node, mod = stack.pop()
        if node.type == "import_statement":
            _take_import_es(cfg, node, src, path, rel, fs)
            continue
        if node.type == "export_statement":
            _take_export_es(cfg, node, src, path, rel, fs, line_starts)
            continue
        if node.type in cfg.fn_scope_types:
            continue
        if mod and node.type in ("lexical_declaration", "variable_declaration"):
            for ch in node.children:
                if ch.type == "variable_declarator":
                    _take_module_init_es(cfg, ch, src, fs)
                    if cfg.declarator_hook is not None:
                        cfg.declarator_hook(ch, src, path, rel, fs)
        if mod and node.type == "expression_statement":
            m = _MODULE_INIT_CALL_RE.match(_text(node, src))
            if m and m.group(1) not in cfg.non_calls:
                fs.init_calls.add(m.group(1))
        if node.type in cfg.field_types:
            _take_field_es(cfg, node, src, fs)
        if cfg.assignment_hook is not None and mod and node.type == "assignment_expression":
            cfg.assignment_hook(node, src, fs, line_starts)
        if node.type in cfg.namespace_types:
            nm = _ident_child(node, src)
            if nm:
                fs.module_vars.setdefault(nm, "module:" + rel)
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
        got = _resolve_spec_es(cfg, spec, path, rel)
        if got:
            fs.imported_modules.add(got)
    for m in _REQUIRE_RE.finditer(whole):
        spec = m.group(2)
        if "${" in spec:
            continue
        got = _resolve_spec_es(cfg, spec, path, rel)
        if got:
            fs.imported_modules.add(got)


# ---- shared engine: parse core -----------------------------------------------------


def _parse_es_family(cfg, root, src: bytes, path: Path, rel: str, fs: FileSym,
                     line_starts: list[int], caps) -> None:
    """Shared ES-family parse core (issue #361): def collection (the cpp
    first-in-file law; the ts table adds the signature-only overload
    pre-pass), class/heritage collection, the import/export module walk,
    the exported-PascalCase component hints, and the JSX prop donations.
    The dialect's grammar front-end (parsers + query tables) builds
    `caps` before the call; structural arms arrive via the table."""
    def bytewise(*keys: str) -> list:
        nodes = []
        for k in keys:
            nodes.extend(caps.get(k, ()))
        return sorted(nodes, key=lambda n: n.start_byte)

    # -- def collection (first-in-file wins on same-name collisions) -------------
    heads: dict[str, int] = {}    # name -> first declaration line (any kind)
    impls: dict[str, tuple] = {}  # name -> (line, body, params, ret)
    for node in bytewise(*cfg.overload_def_keys):
        # signature-only heads (ts table): overload fronts and ambient
        # `declare` bodies. They run BEFORE the def loop so a body-less
        # front at an earlier line owns the collapsed Func's line
        # (overload law).
        nm = _names_in(node, caps, src, cfg.overload_name_keys)
        if nm:
            heads.setdefault(nm, _line(node, line_starts))
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
            params, ret = cfg.signature(sig_node, src)
            impls.setdefault(nm, (line, body, params, ret))
    for nm in sorted(impls):
        line, body, params, ret = impls[nm]
        fs.funcs.setdefault(nm, Func(path=rel, name=nm, line=heads.get(nm, line),
                                     body=body, params=params, ret=ret))

    # -- classes: first class with a body names the file; every class's
    # -- extends is recorded for the inheritance fill (class_map) -------
    classes = bytewise(*cfg.class_def_keys)
    ext_nodes = bytewise("ext")
    named = False
    es_classes: list[tuple[str, str]] = []
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
        es_classes.append((nm, ext_nm))
        if not named:
            fs.class_name = nm
            named = True
        if not fs.extends and ext_nm:
            fs.extends = ext_nm
    setattr(fs, cfg.classes_attr, es_classes)  # consumed by _fill_inheritance

    # -- the dialect's structural arms (ts: enums/interfaces/types/decorators)
    if cfg.structural_arms is not None:
        cfg.structural_arms(caps, fs, classes, bytewise, src)

    # -- exported-name surface: the walk collects ESM (+ CJS via the js
    # -- table's hooks) names ----------------------------------------------
    setattr(fs, cfg.exported_attr, set())
    _walk_modules_es(cfg, root, src, path, rel, fs, line_starts)

    # -- exported PascalCase fn/class components are UI entries (§1.7) ----------
    for nm in sorted(getattr(fs, cfg.exported_attr)):
        if nm[:1].isupper() and (nm in fs.funcs or nm == fs.class_name):
            fs.entry_hints.add(nm)

    # -- JSX prop donations (the jsx grammars only) ------------------------------
    for node in bytewise("jprop"):
        fs.arg_refs.add(_text(node, src))


# ---- shared engine: entry rules -----------------------------------------------------


def _entry_tests_es(cfg, fs: FileSym, ctx) -> Iterator[str]:
    if fs.ext not in cfg.exts:
        return
    if cfg.test_path_re.search(fs.path):
        yield from entry_keys(fs, sorted(fs.funcs))


def _entry_candidates_es(cfg, spec: str, pkg_dir: str, ctx) -> str:
    """Resolve a package entry specifier against ctx.files (§1.4 order)."""
    if not isinstance(spec, str) or not spec:
        return ""
    base = posixpath.normpath(posixpath.join(pkg_dir, spec))
    for cand in [base + suf for suf in (("",) + cfg.entry_suffixes)]:
        if cand in ctx.files:
            return cand
    for src_e, dsts in cfg.ext_rewrites:
        if base.endswith(src_e):
            for d in dsts:
                if base[:-len(src_e)] + d in ctx.files:
                    return base[:-len(src_e)] + d
    return ""


def _entry_package_es(cfg, fs: FileSym, ctx) -> Iterator[str]:
    if fs.ext not in cfg.exts:
        return
    parts = fs.path.split("/")
    if ((len(parts) == 1 and cfg.config_name_re.match(parts[0]))
            or (len(parts) == 2 and parts[0] == ".storybook")):
        yield from entry_keys(fs, sorted(fs.funcs))  # framework config = entry
        return
    done = _PKG_SEEN.setdefault(ctx, set())
    if done:
        return
    done.add("")  # sentinel: the root walk itself is one-shot per ctx —
    # trees with no package.json anywhere must not re-walk per ES-family
    # file (the seen-dict is shared across the family, #293)
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
            entry = _entry_candidates_es(cfg, spec, pkg_dir, ctx)
            if not entry:
                # deterministic build-artifact fallback: dist/stub specifiers
                # that no walked file matches land on the table's src/index
                for cand in (posixpath.normpath(pkg_dir + "/" + s)
                             for s in cfg.src_index_fallback):
                    if cand in ctx.files:
                        entry = cand
                        break
            if entry and entry in ctx.files:
                yield from entry_keys(ctx.files[entry], sorted(ctx.files[entry].funcs))


def _entry_components_es(cfg, fs: FileSym, ctx) -> Iterator[str]:
    """Decorated methods (ts table) and exported PascalCase fn/class
    components (the React convention) — a component is a UI entry, so
    dead tiers never false-flag one."""
    if fs.ext not in cfg.exts:
        return
    yield from entry_keys(fs, sorted(fs.entry_hints))


def _entry_file_routes_es(cfg, fs: FileSym, ctx) -> Iterator[str]:
    """File-based routing (#328, deepened #340): expo-router `app/**` and
    Next `pages/**` mount route files by convention at ANY depth — no
    import points at them, so an anonymous-default route trends dead
    alongside its exclusive deps (a named/PascalCase default is already
    alive via the component rule). Every ES-family file under a
    top-level routing root is an entry; imports revive via the
    reachability walk. Deliberately over-approximates toward ALIVE for
    colocation files inside the subtree — the conservative direction
    for liveness tiers. Top-level roots only; no config knob."""
    if fs.ext not in cfg.exts:
        return
    parts = fs.path.split("/")
    if len(parts) >= 2 and parts[0] in ("app", "pages"):
        yield from entry_keys(fs, sorted(fs.funcs))


# ---- shared engine: facts + call scanning --------------------------------------------


def _harvest_facts_es(cfg, fs: FileSym, ctx) -> None:
    if fs.ext not in cfg.exts:
        return
    for nm in sorted(fs.name_literals):
        ctx.referenced_names.add(nm)
    for nm in sorted(fs.init_calls):
        ctx.referenced_names.add(nm)
    for nm in sorted(fs.arg_refs):
        if nm in fs.funcs:
            ctx.referenced.add(fs.funcs[nm].key)


_WARNED_JSX_READS: dict[str, bool] = {}  # per-dialect warn-once cells (#361)


def _jsx_sites_es(cfg, path: Path, fs: FileSym) -> list[tuple[str, int]]:
    """Capitalized JSX element names + their lines (jsx-family grammars)."""
    label = cfg.jsx_label
    try:
        src = path.read_bytes()
    except OSError:
        # scan_file runs after parse read the same file fine; a failure
        # here means it vanished mid-build — loud once, never per file
        if not _WARNED_JSX_READS.get(label):
            _WARNED_JSX_READS[label] = True
            print(f"neuronav: {label} scan re-read failed at {path}: "
                  "sites skipped for this file", file=sys.stderr)
        return []
    caps = QueryCursor(cfg.jsx_query).captures(
        cfg.parsers[cfg.jsx_ext].parse(src).root_node)
    line_starts = line_starts_of(src)
    sites = [(name, _line(node, line_starts))
             for node in sorted(caps.get("jsxcomp", ()), key=lambda n: n.start_byte)
             for name in (_text(node, src),) if name[:1].isupper()]
    return sites


def _scan_file_es(cfg, fs: FileSym, ctx) -> None:
    if fs.ext not in cfg.exts:
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

    if fs.ext == cfg.jsx_ext:
        for name, line in _jsx_sites_es(cfg, ctx.path_for(fs.path), fs):
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
        _scan_body_es(cfg, fs, fn, ctx)


def _scan_body_es(cfg, fs: FileSym, fn: Func, ctx) -> None:
    src_key, body, var_types = receiver_env(fs, fn)
    if cfg.typed_local_re is not None:
        for m in cfg.typed_local_re.finditer(body):
            var_types[m.group(1)] = m.group(2)
    for m in cfg.new_local_re.finditer(body):
        var_types[m.group(1)] = m.group(2)
    new_spans = []
    for m in cfg.new_re.finditer(body):
        cls = m.group(1)
        new_spans.append(m.span())
        if cls in ctx.class_map:
            dst = ctx.class_map[cls]
            if "constructor" in ctx.files[dst].funcs:
                ctx._emit_call(src_key, dst, "constructor")
    for m in cfg.call_re.finditer(body):
        head, name = m.group(1), m.group(2)
        if not name or name in cfg.non_calls:
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
            elif cfg.member_dispatch and len(parts) == 2 and fs.members.get(parts[1]):
                cls = fs.members[parts[1]]
                dst = ctx.class_map.get(_last_ident(cls), "")
                tfs = ctx.files.get(dst, None)
                if tfs is not None and name in tfs.funcs:
                    ctx._emit_call(src_key, dst, name)
                else:
                    ctx.referenced_names.add(name)
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


# ---- shared engine: sweeps -----------------------------------------------------------


def _sweep_resolve_es(cfg, spec: str, rel: str, ctx) -> str:
    """ctx.files-truth resolution for sweep passes (parse-time resolution
    without a live Path: same candidate order, no filesystem)."""
    if not spec or not _is_specifier_relative(spec):
        return ""
    d = posixpath.dirname(rel)
    base = posixpath.normpath(posixpath.join(d, spec) if d else spec)
    for e in cfg.sweep_suffixes:
        if base + e in ctx.files:
            return base + e
    for idx in cfg.sweep_index:
        if base + idx in ctx.files:
            return base + idx
    for src_e, dsts in cfg.ext_rewrites:
        if base.endswith(src_e):
            for dd in dsts:
                if base[:-len(src_e)] + dd in ctx.files:
                    return base[:-len(src_e)] + dd
    return ""


def _fill_inheritance_es(cfg, ctx) -> None:
    """ES-family classes (this table's dialect) feed ctx.class_map and
    ctx._subclasses so _emit_call resolves receivers and mirrors
    overrides (the graph surfaces)."""
    for rel in sorted(ctx.files):
        fs = ctx.files[rel]
        if fs.ext not in cfg.exts:
            continue
        for cls_name, ext_nm in getattr(fs, cfg.classes_attr, ()):
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


_STAR_EXPORT_RE = re.compile(
    r"""export\s*\*\s*(?:as\s+([A-Za-z_$][\w$]*)\s+)?from\s*(["'])([^"']+)\2""")
_REEXPORT_CLAUSE_RE = re.compile(
    r"""export\s*\{([^}]*)\}\s*from\s*(["'])([^"']+)\2""")
_SPEC_ITEM_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*(?:as\s+([A-Za-z_$][\w$]*))?")


def _rebind_reexports_sweep_es(cfg, ctx) -> None:
    """Rewrite consumers' re-exported bindings to the ORIGIN definer.

    Runs first in the sweep pair so inheritance mirrors exist before
    scan_file mints edges. Files whose only exports are re-exports —
    ESM barrels, or CJS `module.exports = {a, b}` over require bindings
    (the js table's barrel hook) — are barrels -> is_wiring_only.
    """
    _fill_inheritance_es(cfg, ctx)
    tables: dict[str, tuple[dict[str, tuple[str, str]], list[str]]] = {}
    for rel in sorted(ctx.files):
        fs = ctx.files[rel]
        if fs.ext not in cfg.exts:
            continue
        try:
            text = ctx.read_file(rel)
        except OSError:
            continue
        names: dict[str, tuple[str, str]] = {}
        stars: list[str] = []
        for m in _REEXPORT_CLAUSE_RE.finditer(text):
            org = _sweep_resolve_es(cfg, m.group(3), rel, ctx)
            for got in _SPEC_ITEM_RE.findall(m.group(1)):
                nm_in = got[0]
                nm_out = got[1] or got[0]
                names[nm_out] = (org, nm_in)
        for m in _STAR_EXPORT_RE.finditer(text):
            org = _sweep_resolve_es(cfg, m.group(3), rel, ctx)
            if m.group(1):
                names[m.group(1)] = (org, "*")
            else:
                stars.append(org)
        if cfg.barrel_names_hook is not None:
            cfg.barrel_names_hook(text, fs, names)
        tables[rel] = (names, stars)
        if not fs.funcs and (names or stars):
            setattr(fs, cfg.barrel_attr, True)
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
        if fs.ext not in cfg.exts:
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


# ---- tsconfig alias resolution (§1.3) -------------------------------------------

_WARNED_TSCONFIG = False
_TSCONFIG_FIND: dict[Path, Path | None] = {}
_TSCONFIG_LOAD: dict[Path, tuple[dict[str, list[str]], Path] | None] = {}



def _strip_jsonc(text: str) -> str:
    """String-aware // + block-comment strip (JSONC tolerance)."""
    out, i, n = [], 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _find_tsconfig(start: Path) -> Path | None:
    """Nearest ancestor (self included) carrying tsconfig.json, cached."""
    key = start.resolve()
    if key in _TSCONFIG_FIND:
        return _TSCONFIG_FIND[key]
    cur, found = key, None
    while True:
        cand = cur / "tsconfig.json"
        if cand.is_file():
            found = cand
            break
        if cur.parent == cur:
            break
        cur = cur.parent
    _TSCONFIG_FIND[key] = found
    return found


def _merge_extends(parent: dict, child: dict) -> dict:
    """Extends-chain deep-merge (#378): dict-valued keys (compilerOptions,
    and paths within it) union instead of wholesale replace, child leaf
    winning on conflict; every other value stays child-wins. The old
    shallow dict(parent)+update dropped the base alias map the moment a
    child carried its own compilerOptions without paths -> false-dead."""
    merged = dict(parent)
    for k, v in child.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _merge_extends(merged[k], v)
        else:
            merged[k] = v
    return merged


def _load_tsconfig(cfg: Path) -> tuple[dict[str, list[str]], Path] | None:
    """(alias map find->replacements, base dir) or None when unreadable.

    One `extends` level; dict-valued keys deep-merge with the child leaf
    winning (#378), scalars child-wins; baseUrl/paths resolve against
    the tsconfig's own directory. An unreadable extends target (package
    specifier like "expo/tsconfig.base", or a missing relative path)
    keeps the config's OWN compilerOptions — only the own file being
    unparseable degrades the whole tsconfig. Either way: exactly one
    stderr line (test-visible via the module flags).
    """
    global _WARNED_TSCONFIG
    if cfg in _TSCONFIG_LOAD:
        return _TSCONFIG_LOAD[cfg]
    loaded: tuple[dict[str, list[str]], Path] | None = None
    try:
        data = json.loads(_strip_jsonc(cfg.read_text(encoding="utf-8", errors="replace")))
        base = data
        ext = data.get("extends")
        if isinstance(ext, str):
            try:
                ext_path = Path(os.path.normpath(str(cfg.parent / ext)))
                parent = json.loads(_strip_jsonc(
                    ext_path.read_text(encoding="utf-8", errors="replace")))
                base = _merge_extends(parent, data)
            except (OSError, ValueError):
                if not _WARNED_TSCONFIG:
                    _WARNED_TSCONFIG = True
                    print(f"neuronav: tsconfig extends unreadable at "
                          f"{cfg.parent} ({ext}): alias resolution "
                          f"degraded to own paths", file=sys.stderr)
        opts = base.get("compilerOptions") or {}
        base_dir = Path(os.path.normpath(str(cfg.parent / str(opts.get("baseUrl", ".")))))
        paths = opts.get("paths") or {}
        aliases: dict[str, list[str]] = {}
        if isinstance(paths, dict):
            for find, repls in paths.items():
                if isinstance(find, str) and isinstance(repls, list):
                    aliases[find] = [str(r) for r in repls if isinstance(r, str)]
        loaded = (aliases, base_dir)
    except (OSError, ValueError):
        if not _WARNED_TSCONFIG:
            _WARNED_TSCONFIG = True
            print(f"neuronav: tsconfig unreadable at {cfg.parent}: "
                  "alias resolution degraded", file=sys.stderr)
    _TSCONFIG_LOAD[cfg] = loaded
    return loaded


def _alias_expand(spec: str, aliases: dict[str, list[str]]) -> list[str] | None:
    """Replacement specifiers for `spec`, longest-prefix match first."""
    for find in sorted(aliases, key=lambda k: (-len(k), k)):
        repls = aliases[find]
        if find == "*":
            return [r.replace("*", spec) for r in repls]
        if find.endswith("/*"):
            if spec.startswith(find[:-1]):
                rest = spec[len(find) - 1:]
                return [r.replace("*", rest) for r in repls]
        elif spec == find:
            return list(repls)
    return None


# ---- module-specifier resolution: ts data (§1.4) ----------------------------------

_EXT_REWRITES = (
    (".js", (".ts", ".tsx", ".mts")),
    (".jsx", (".tsx",)),
    (".mjs", (".mts",)),
    (".cjs", (".cts",)),
)

# extensionless candidates: ts suffixes first (a ts importer prefers
# ts files), then the js suffixes — mixed .ts+.js repos resolve both
# directions (#293: `from './helper'` where only helper.js exists)
_RESOLVE_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


# ---- ts scan data --------------------------------------------------------------------

TS_NON_CALLS = frozenset({
    "if", "for", "while", "switch", "catch", "return", "typeof", "new", "await",
    "yield", "function", "import", "export", "default", "void", "delete", "in",
    "of", "do", "else", "try", "finally", "throw", "case", "super", "this",
    "class", "extends", "const", "let", "var", "declare", "async", "interface",
    "type", "enum", "implements", "namespace", "module", "abstract", "get",
    "set", "static", "public", "private", "protected", "readonly", "satisfies",
    "as", "keyof", "infer", "is", "asserts", "key", "constructor",
})

# head chain + callee + optional type-argument bracket run before the paren;
# the `<...>` must touch the callee (comparison chains keep their spaces)
TS_NEW_RE = re.compile(r"\bnew\s+([A-Za-z_$][\w$]*)\s*(?:<[^()]*>)?\s*\(")
TS_TYPED_LOCAL_RE = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*:\s*([A-Z][\w$.]*)")
TS_NEW_LOCAL_RE = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*new\s+([A-Za-z_$][\w$]*)")
TS_CALL_RE = re.compile(
    r"(?<![\w.$])((?:[A-Za-z_$][\w$]*\.)*)"
    r"([A-Za-z_$][\w$]*)\s*(?:<[^()<>()]*>)?\s*\(")


# ---- ts structural parse arms (the engine's structural_arms hook) --------------------


def _ts_structural_arms(caps, fs, classes, bytewise, src) -> None:
    """ts-only parse arms (issue #361: structural, per-module): enums,
    implements, type aliases/interfaces, decorators."""
    # -- enums: members -> consts under the owning enum name ---------------------
    enum_defs = bytewise("enum.def")
    for node in bytewise("enbare", "enasg"):
        owner = ""
        for enode in enum_defs:
            if enode.start_byte <= node.start_byte < enode.end_byte:
                owner = _ident_child(enode, src)
                break
        nm = _text(node, src) if node.type in _IDENT_TYPES else _ident_child(node, src)
        if owner and nm:
            fs.consts.setdefault(nm, f"<{owner}>")

    # -- implements interfaces -> aliases ----------------------------------------
    for node in classes:
        for impl_node in bytewise("impl"):
            if node.start_byte <= impl_node.start_byte < node.end_byte:
                fs.aliases.setdefault(_text(impl_node, src), "interface")

    # -- type aliases / interfaces: surface facts, never funcs -------------------
    for node in bytewise("ta.def"):
        nm = _ident_child(node, src)
        if nm:
            fs.aliases.setdefault(nm, "type")
    for node in bytewise("iface.def"):
        nm = _ident_child(node, src)
        if nm:
            fs.aliases.setdefault(nm, "interface")

    # -- decorators: class decorator -> every method; method decorator -> it ----
    def _decl_start(node) -> int:
        # decorators are children of the declaration: the node's own
        # start_byte sits ON the `@`. Attribution needs the keyword start.
        for ch in node.children:
            if ch.type != "decorator":
                return ch.start_byte
        return node.start_byte

    decls: list[tuple[int, int, str, str]] = [
        (_decl_start(n), n.end_byte, "cls", _ident_child(n, src)) for n in classes]
    decls += [(_decl_start(n), n.end_byte, "m", _names_in(n, caps, src, ("m",)))
              for n in bytewise("m.def")]
    decls.sort()
    for dec in bytewise("dec"):
        target = next((d for d in decls if d[0] > dec.end_byte and d[3]), None)
        if not target:
            continue
        s, e, kind, _nm = target
        if kind == "cls":
            for ms, me, mk, mnm in decls:
                if mk == "m" and mnm and s <= ms < e:
                    fs.entry_hints.add(mnm)
        else:
            fs.entry_hints.add(_nm)


# ---- ts entry data ---------------------------------------------------------------------

_TEST_PATH_RE = re.compile(r"(?:^|/)__tests__/|\.(?:test|spec)\.(?:ts|tsx|mts|cts)$")
_CONFIG_NAME_RE = re.compile(
    r"^(?:next\.config\.|vite\.config\.|tailwind\.config\.|app\.config\.)"
    r"[^/]*$|^app\.json$")


# ---- the ts table: this grammar's entire contribution to the engine -------------------

_TS_CFG = ESFamily(
    exts=TS_EXTS,
    family_exts=TS_EXTS | JS_EXTS,
    find_config=_find_tsconfig,
    resolve_suffixes=_RESOLVE_SUFFIXES,
    index_candidates=("index.ts", "index.tsx", "index.js", "index.jsx"),
    ext_rewrites=_EXT_REWRITES,
    signature=_signature,
    overload_def_keys=("sig.def", "msig.def"),
    overload_name_keys=("sig", "msig"),
    class_def_keys=("cls.def", "acls.def"),
    classes_attr="_ts_classes",
    exported_attr="_ts_exported",
    structural_arms=_ts_structural_arms,
    fn_scope_types=("function_declaration", "generator_function_declaration",
                    "function_expression", "arrow_function", "method_definition",
                    "function_signature", "method_signature"),
    field_types=("public_field_definition",),
    namespace_types=("internal_module", "module"),
    field_name_types=("property_identifier",),
    field_annotation_types=("type_annotation",),
    non_calls=TS_NON_CALLS,
    class_types=("class", "abstract_class"),
    anon_default_types=("arrow_function", "function_expression", "class",
                        "abstract_class"),
    test_path_re=_TEST_PATH_RE,
    config_name_re=_CONFIG_NAME_RE,
    entry_suffixes=(".ts", ".tsx", ".mts", ".cts", "/index.ts", "/index.tsx"),
    src_index_fallback=("src/index.ts", "src/index.tsx"),
    jsx_ext=".tsx",
    jsx_query=_TSX_QUERY,
    jsx_label="tsx",
    parsers=_PARSERS,
    new_re=TS_NEW_RE,
    new_local_re=TS_NEW_LOCAL_RE,
    call_re=TS_CALL_RE,
    typed_local_re=TS_TYPED_LOCAL_RE,
    member_dispatch=True,
    barrel_attr="_ts_barrel",
    sweep_suffixes=("", ".ts", ".tsx", ".mts", ".cts"),
    sweep_index=("/index.ts", "/index.tsx"),
)


def parse(path: Path, rel: str) -> FileSym:
    """Registry entry point: one FileSym per .ts/.tsx/.mts/.cts file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    src = text.encode("utf-8")
    ext = path.suffix.lower()
    fs = FileSym(path=rel, ext=ext)
    root = _PARSERS.get(ext, _PARSERS[".ts"]).parse(src).root_node
    line_starts = line_starts_of(src)
    caps = QueryCursor(_TSX_QUERY if ext == ".tsx" else _TS_QUERY).captures(root)
    _parse_es_family(_TS_CFG, root, src, path, rel, fs, line_starts, caps)
    return fs


# ---- hooks (langsep REQUIRED surface) -------------------------------------------

from extractors.common import entry_keys  # noqa: E402  (late: package cycle)

MENTION_FLOOR = 2
DYNAMIC_HINT = re.compile(r"\bReflect\.|\bProxy\(|\beval\(")

# graph's dup normalizer strips these before hashing (issue #295)
COMMENT_PREFIXES = ("//",)

# Thin TS/JS forwarder classification for graph's dup filter (issue #295:
# language-owned; #116 conservatism — signature line, exactly one
# forwarding return, closing brace; anything richer never classifies).
# Takes the body graph already normalized (comment-stripped, uniform
# indent).
_TS_DEL_SIG_RE = re.compile(
    r"^(?:export\s+)?(?:async\s+)?function\s+\w+\s*\([^(){};]*\)"
    r"(?:\s*:\s*[\w<>\[\]|, ]+)?\s*\{$"
)
_TS_DEL_FWD_RE = re.compile(r"^return\s+[\w.]+\([^(){};]*\)\s*;$")


def pure_delegate(norm: str) -> bool:
    lines = [ln.strip() for ln in norm.splitlines()]
    # the ts extractor stores fn.body WITHOUT the signature line (it starts
    # at the bare opening brace), so both shapes must classify: the
    # signature form and the bare-brace form
    if len(lines) == 3 and lines[0] == "{":
        return (
            _TS_DEL_FWD_RE.match(lines[1]) is not None
            and lines[2] == "}"
        )
    return (
        len(lines) == 3
        and _TS_DEL_SIG_RE.match(lines[0]) is not None
        and _TS_DEL_FWD_RE.match(lines[1]) is not None
        and lines[2] == "}"
    )

TS_BASE_VIRTUALS = frozenset({
    "render", "componentDidMount", "componentDidUpdate", "componentDidCatch",
    "componentWillUnmount", "shouldComponentUpdate", "getDerivedStateFromProps",
    "getSnapshotBeforeUpdate",
})


def is_entry_exempt(name: str) -> bool:
    return False


def unresolved_base_review(name: str) -> bool:
    return name.startswith("_") or name in TS_BASE_VIRTUALS


def stand_in_review(fs: FileSym, name: str) -> bool:
    return False


def mention_review(name: str, mentions: dict) -> bool:
    return mentions.get(name, 0) >= MENTION_FLOOR


def is_wiring_only(fs: FileSym) -> bool:
    """Barrels (re-export-only modules) and ambient `.d.ts` declarations are
    wiring, not logic: they carry zero callable surface of their own. A
    `.d.ts` that DOES declare callables is logic — the dead-share
    denominator counts it (judge C1), and wiring∩dead-file is a
    contradiction the bake and the tsreg/tshard gates forbid; the
    registry resolution made that combo reachable on real corpora
    (compiler baseline dumps are declare-function files)."""
    if fs.ext not in TS_EXTS:
        return False
    if bool(getattr(fs, "_ts_barrel", False)):  # zero own funcs by construction
        return True
    return fs.path.endswith((".d.ts", ".d.mts", ".d.cts", ".d.tsx")) and not fs.funcs


def counts_dead_share(fs: FileSym) -> bool:
    return fs.ext in {".ts", ".tsx"}


def stat_tags(text: str) -> tuple[str, str]:
    return ("", "")


# ---- entry rules (§1.7): ts binds the engine on its table --------------------------

_PKG_SEEN: "weakref.WeakKeyDictionary[object, set]" = weakref.WeakKeyDictionary()

_entry_tests = partial(_entry_tests_es, _TS_CFG)
_entry_package = partial(_entry_package_es, _TS_CFG)
_entry_components = partial(_entry_components_es, _TS_CFG)
_entry_file_routes = partial(_entry_file_routes_es, _TS_CFG)

ENTRY_RULES = (_entry_tests, _entry_package, _entry_components, _entry_file_routes)


# ---- registry binds: the shared engine on the ts table -------------------------------

harvest_facts = partial(_harvest_facts_es, _TS_CFG)
scan_file = partial(_scan_file_es, _TS_CFG)
_jsx_sites = partial(_jsx_sites_es, _TS_CFG)
rebind_reexports_sweep = partial(_rebind_reexports_sweep_es, _TS_CFG)

import_liveness_sweep = make_import_liveness_sweep(
    TS_EXTS,
    "Star/side-effect/dynamic imports: the whole target module's funcs "
    "enter ctx.referenced (python plain-import semantics verbatim).")


# registry choreography binds (langsep) — see extractors/python.py's
# _PASS_* block for the rationale (attribute dispatch is invisible to
# the module scan; the value-ref arm roots these binds).
_PASS_REBIND = rebind_reexports_sweep
_PASS_IMPORTS = import_liveness_sweep
_PASS_FACTS = harvest_facts
