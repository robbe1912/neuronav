"""Go extractor: tree-sitter-go front-end (issue #334).

Strategy: one parse per file via the pinned grammar pair —
``tree-sitter==0.26.0`` + ``tree-sitter-go==0.25.0`` (official
tree-sitter org wheel, verified by parse; ``core`` extra NOT installed,
same pattern as the other front-ends). Defs come from one ordered
S-expression Query; imports resolve through ``go.mod`` (the tsconfig
precedent: the module file's contents are read at parse time — it is
project metadata, not another source file).

IMPORT MODEL: a Go import names a PACKAGE (a directory), not a file —
``import "example.com/m/lib"`` where ``example.com/m`` is the module
path in go.mod resolves to the repo dir ``lib/`` by prefix match. The
leaf records the package DIR rel in ``fs.imported_modules`` plus a
``module:`` binding per import alias; the go-specific liveness sweep
(downstream) keeps every exported func of that dir's non-test files
alive. External and vendored imports are not in the corpus — name-level
liveness at most, the rust external-crate law. A missing go.mod above
the file degrades loudly (one note per process, the tsconfig
``alias resolution degraded`` precedent).

ENTRY MODEL (convention-based, documented): ``func main`` in a ``package
main`` file is a root; ``_test.go`` files make ``Test*``/``Benchmark*``/
``Fuzz*``/``Example*``/``TestMain`` fns entry hints (the rusthard
``#[test]`` law — helpers only tests call revive through their call
edges); ``func init()`` runs implicitly at program start (no caller can
exist) and is always a root. Std-interface dispatch with no textual call
site — ``String()`` (fmt.Stringer) and ``Error()`` (error) — gets the
RUST_STD_TRAIT_METHODS shield, kept minimal per that law.

INTERFACE SATISFACTION (the rust trait-dispatch class): Go satisfaction
is structural and implicit. The name-based heuristic — type T satisfies
interface I when T's method-name set covers I's — mirrors what
gopls/SCIP-class indexers do without full type checking (stack-graphs
solves resolution declaratively; we borrow only the convention that
structural name coverage is the defensible static floor). Interface
method specs do NOT mint Funcs (no bodies — a spec Func would pollute
duplicate hashing); a call on an interface-typed receiver mirrors to
every satisfying type's defining file directly, and falls back to
name-level liveness when no satisfier carries the method.

Package-spanning definitions: Go packages spread across files — a bare
``helper()`` call resolves against the whole own-package dir, not just
the calling file; methods resolve through the corpus-wide class_map
(every type_declaration feeds it via harvest_facts).

NEVER read ``Node.start_point`` / ``end_point`` — py-tree-sitter 0.26.0
has the Point refcount bug (tree-sitter-py issue #472); every line
number is ``bisect`` over newline byte offsets instead.

Determinism law: captures are re-sorted by ``start_byte`` before any
emission; same-name collisions resolve first-in-file-wins (the cpp
overload law — a method name shared by two receivers keeps the first
line); every sweep iterates ``sorted(...)``. No rng anywhere.

Leaf parser: reads only the file being parsed plus ``go.mod`` contents
and directory existence checks for import resolution — never another
source file's contents, never nav or graph; cross-file work lives in
the ctx-driven sweeps below.
"""

from __future__ import annotations

import os
import re
import sys
from functools import partial
from pathlib import Path

from tree_sitter import Language, Parser, Query, QueryCursor

import tree_sitter_go as _tsg

from extractors.common import (  # leaf module: shared text mechanics (#302)
    ident_child,
    last_ident,
    line_starts_of,
    node_line as _line,
    node_text as _text,
    receiver_env,
    rel_of_target as _rel_of_target,
)
from extractors.model import FileSym, Func

GO_EXTS = frozenset({".go"})
GO_LANG = Language(_tsg.language())
_PARSER = Parser(GO_LANG)

_IDENT_TYPES = ("identifier", "type_identifier", "field_identifier",
                "package_identifier")

# def nodes only; names are pulled structurally per node (the grammar's
# name field or the first identifier-shaped child).
_QUERY_SRC = """
(function_declaration) @fn.def
(method_declaration) @method.def
(type_spec) @type.def
(method_elem) @imethod.def
(struct_type) @struct.def
(field_declaration) @field.def
(import_spec) @import.def
(package_clause) @pkg
(var_declaration) @var.def
"""
_QUERY = Query(GO_LANG, _QUERY_SRC)


# ---- node helpers (no Point reads — module header law) --------------------------
# shared front-end mechanics live in extractors/common.py (#302); the
# per-language knobs are data: identifier node types, '$' allowed in
# identifiers (go: no).
_ident_child = partial(ident_child, ident_types=_IDENT_TYPES)
_last_ident = partial(last_ident, dollar=False)


def _params_of(node, src: bytes, skip: int = 0) -> tuple[list[tuple[str, str]], str]:
    """([(name, type)], ret) from a declaration's parameter/result lists.

    ``skip`` leading parameter_list children are ignored (the receiver of
    a method_declaration). Go's `x, y int` shares one type across idents
    — first ident names, last ident types. Unnamed params keep "".
    """
    params: list[tuple[str, str]] = []
    ret = ""
    lists = [ch for ch in node.children if ch.type == "parameter_list"]
    if len(lists) > skip:
        for p in lists[skip].children:
            if p.type != "parameter_declaration":
                continue
            ids = [c for c in p.children if c.type in _IDENT_TYPES]
            # a composite type node (slice/map/chan/func) beside the
            # ident means the ident NAMES a typed param, not types a
            named = any(c.type not in ("identifier", "type_identifier",
                                       "field_identifier",
                                       "package_identifier",
                                       ",", "comment")
                        for c in p.children)
            if len(ids) >= 2:
                params.append((_text(ids[0], src), _text(ids[-1], src)))
            elif len(ids) == 1 and named:
                nm = _text(ids[0], src)
                params.append(
                    (nm, _text(p, src).replace(nm, "", 1).strip()))
            elif len(ids) == 1:
                params.append(("", _text(ids[0], src)))
    if len(lists) == skip + 2:
        # parenthesized (possibly named) result list — keep the text
        ret = _text(lists[-1], src)
    elif lists:
        # bare result arm: children after the params list, before body
        after = False
        for ch in node.children:
            if ch is lists[-1]:
                after = True
                continue
            if after and ch.type not in ("block", "comment", "{", "}"):
                ret = ret or _text(ch, src)
    return params, ret.strip()


# ---- go.mod module resolution (leaf: this file + go.mod only) --------------------

_GOMOD_CACHE: dict[str, "str | None"] = {}
_WARNED_NO_GOMOD = False
_MODULE_RE = re.compile(r"(?m)^\s*module\s+(\S+)")


def _module_of(path: Path) -> tuple[str, Path]:
    """(module path, module root dir) from the nearest go.mod above
    ``path``; ("", root-dir) when absent — intra-module resolution then
    degrades loudly (once per process) and every import stays external."""
    global _WARNED_NO_GOMOD
    cur = path.parent.resolve()
    while True:
        gomod = cur / "go.mod"
        if gomod.is_file():
            key = str(gomod)
            if key not in _GOMOD_CACHE:
                try:
                    mod = _MODULE_RE.search(gomod.read_text(
                        encoding="utf-8", errors="replace"))
                except OSError:
                    mod = None
                _GOMOD_CACHE[key] = mod.group(1) if mod else None
            got = _GOMOD_CACHE[key]
            return (got or "", cur)
        if cur == cur.parent:
            break
        cur = cur.parent
    if not _WARNED_NO_GOMOD:
        _WARNED_NO_GOMOD = True
        print(f"neuronav: no go.mod above {path} — intra-module import "
              f"resolution degraded (every import treated as external)",
              file=sys.stderr)
    return ("", path.parent)


def _pkg_name_of(import_path: str) -> str:
    """Default binding name of an import: its last path segment."""
    return import_path.rsplit("/", 1)[-1]


# ---- parse ----------------------------------------------------------------------

def parse(path: Path, rel: str) -> FileSym:
    """Registry entry point: one FileSym per .go file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    src = text.encode("utf-8")
    fs = FileSym(path=rel, ext=".go")
    root = _PARSER.parse(src).root_node
    line_starts = line_starts_of(src)
    caps = QueryCursor(_QUERY).captures(root)

    def bytewise(*keys: str) -> list:
        nodes = []
        for k in keys:
            nodes.extend(caps.get(k, ()))
        return sorted(nodes, key=lambda n: n.start_byte)

    # -- package clause: main-package flag + _test.go detection ---------
    package = ""
    for node in bytewise("pkg"):
        for ch in node.children:
            if ch.type == "package_identifier":
                package = _text(ch, src)
                break
        break
    fs._go_package = package
    fs._go_is_main = package == "main"
    fs._go_is_test = path.name.endswith("_test.go")

    # -- imports: module-prefix resolution (the tsconfig precedent) -----
    module, modroot = _module_of(path)
    fs._go_module = module
    for node in bytewise("import.def"):
        alias = ""
        ipath = ""
        for ch in node.children:
            if ch.type == "package_identifier":
                alias = _text(ch, src)
            elif ch.type == "interpreted_string_literal":
                ipath = _text(ch, src).strip("\"'")
        if not ipath:
            continue
        if module and (ipath == module or ipath.startswith(module + "/")):
            sub = ipath[len(module):].strip("/")
            target = modroot / sub if sub else modroot
            if target.is_dir():
                trel = _rel_of_target(target, path, rel)
                fs.imported_modules.add(trel)
                fs.module_vars.setdefault(alias or _pkg_name_of(ipath),
                                          "module:" + trel)
                continue
        # external / vendored / unresolvable: no corpus fact — package
        # calls through this binding stay name-level at most (scan law)
        fs.module_vars.setdefault(alias or _pkg_name_of(ipath), "external")

    # -- funcs: package fns + methods (first-in-file-wins on collision) -
    method_types: dict[str, set[str]] = {}
    for node in bytewise("fn.def", "method.def"):
        nm = _ident_child(node, src)
        if not nm:
            continue
        line = _line(node, line_starts)
        params, ret = _params_of(node, src, skip=1 if node.type ==
                                 "method_declaration" else 0)
        if node.type == "method_declaration":
            recv_name, recv_type = "", ""
            for ch in node.children:
                if ch.type == "parameter_list":
                    for p in ch.children:
                        if p.type == "parameter_declaration":
                            ids = [c for c in p.children
                                   if c.type in _IDENT_TYPES]
                            recv_type = _last_ident(_text(p, src))
                            recv_name = _text(ids[0], src) if ids else ""
                    break
            if recv_type:
                method_types.setdefault(recv_type, set()).add(nm)
            # receiver name visible in every body of this file (the
            # gdscript per-class env approximation; documented above)
            if recv_name and recv_type:
                fs.members.setdefault(recv_name, recv_type)
        body = src[node.start_byte:node.end_byte].decode("utf-8", "replace")
        if nm not in fs.funcs:
            fs.funcs[nm] = Func(path=rel, name=nm, line=line, body=body,
                                params=params, ret=ret)
        if fs._go_is_test and re.match(
                r"(Test|Benchmark|Fuzz|Example)\w*", nm):
            fs.entry_hints.add(nm)
    fs._go_method_types = method_types

    # -- named types: first type names the file; struct/interface facts --
    types: list[tuple[str, str]] = []
    interfaces: dict[str, list[str]] = {}
    for node in bytewise("type.def"):
        nm = _ident_child(node, src)
        if not nm:
            continue
        kind = "type"
        iface_names: list[str] = []
        for ch in node.children:
            if ch.type == "interface_type":
                kind = "interface"
                for mc in ch.children:
                    if mc.type == "method_elem":
                        mn = _ident_child(mc, src)
                        if mn:
                            iface_names.append(mn)
            elif ch.type == "struct_type":
                kind = "struct"
            elif ch.type == "=":
                kind = "alias"
        types.append((nm, kind))
        fs.aliases.setdefault(nm, kind)
        if not fs.class_name:
            fs.class_name = nm
        if kind == "interface" and iface_names:
            interfaces[nm] = sorted(set(iface_names))
    fs._go_types = types
    fs._go_interfaces = interfaces

    # -- struct fields -> members (gates var edges + receiver typing) ---
    for node in bytewise("field.def"):
        nm = _ident_child(node, src)
        if not nm:
            continue
        fty = ""
        for ch in node.children:
            if ch.type not in ("field_identifier", ",", "comment"):
                fty = fty or _text(ch, src)
        fs.members.setdefault(nm, fty)

    # -- package-level vars: honest dead-code material (globals) --------
    for node in bytewise("var.def"):
        for ch in node.children:
            if ch.type == "var_spec":
                for vc in ch.children:
                    if vc.type == "identifier":
                        fs.globals.setdefault(_text(vc, src), "")
                    elif vc.type == "type_identifier" and fs.globals:
                        last = next(reversed(fs.globals))
                        fs.globals[last] = _text(vc, src)

    return fs


# ---- hooks (langsep REQUIRED surface) -------------------------------------------

from extractors.common import entry_keys  # late: package cycle, ts precedent

MENTION_FLOOR = 2
# reflection erases receivers — Value.Method / MethodByName dispatch has
# no static callee (the rust `dyn` analogue)
DYNAMIC_HINT = re.compile(r"\breflect\.")

# graph's dup normalizer strips these before hashing (issue #295); go
# `//` line comments and `///`-style doc comments share the prefix
COMMENT_PREFIXES = ("//",)

# implicit dispatch with no textual call site: `func init()` runs at
# package init; String/Error satisfy fmt.Stringer / error — consumed by
# every fmt/print path, none of which the corpus can see. The
# RUST_STD_TRAIT_METHODS law: kept minimal, extend only with dispatch
# evidence.
GO_INIT_VIRTUALS = frozenset({"init"})
GO_STD_IFACE_METHODS = frozenset({"String", "Error"})

# thin go forwarder classification for graph's dup filter (issue #295;
# brace-language shape, the ts/cpp law): `func f() { g() }` /
# `func (r T) f() { return g(r.x) }` — signature line, exactly one
# forwarding call or return, closing brace; anything richer never
# classifies.
_GO_DEL_FWD_RE = re.compile(
    r"^(?:return\s+)?[A-Za-z_][\w.]*\([^(){};]*\)$")


def pure_delegate(norm: str) -> bool:
    lines = [ln.strip() for ln in norm.splitlines()]
    return (
        len(lines) == 3
        and lines[0].startswith("func")
        and lines[0].endswith("{")
        and _GO_DEL_FWD_RE.match(lines[1]) is not None
        and lines[2] == "}"
    )


def is_entry_exempt(name: str) -> bool:
    return name in GO_INIT_VIRTUALS or name in GO_STD_IFACE_METHODS


def unresolved_base_review(name: str) -> bool:
    """Go has no inheritance and no deliberate-unused fn convention —
    the tail arm stays honest (no exemption)."""
    return False


def stand_in_review(fs: FileSym, name: str) -> bool:
    return False


def mention_review(name: str, mentions) -> bool:
    return mentions.get(name, 0) >= MENTION_FLOOR


def stat_tags(text: str) -> tuple[str, str]:
    """(class_name, extends) header sniff — Go has neither header form."""
    return ("", "")


def is_wiring_only(fs: FileSym) -> bool:
    """Go files are never pure wiring: a types-only file still declares
    corpus facts (structs/interfaces feed class_map + satisfaction)."""
    return False


def counts_dead_share(fs: FileSym) -> bool:
    """Judge C1: the dead-share denominator counts .go files (the .gd/
    .ts precedent — registered structural suffixes flag dead files)."""
    return fs.ext == ".go"


# ---- entry rules (convention-based; module header documents them) ----------------

def _entry_main(fs: FileSym, ctx=None):
    if getattr(fs, "_go_is_main", False):
        return entry_keys(fs, ("main",))
    return ()


def _entry_tests(fs: FileSym, ctx=None):
    return entry_keys(fs, sorted(fs.entry_hints))


def _entry_init(fs: FileSym, ctx=None):
    return entry_keys(fs, sorted(GO_INIT_VIRTUALS & set(fs.funcs)))


ENTRY_RULES = (_entry_main, _entry_tests, _entry_init)


# ---- facts harvest (per-file, ctx-truth surfaces) --------------------------------

def harvest_facts(fs: FileSym, ctx) -> None:
    if fs.ext != ".go":
        return
    if not hasattr(ctx, "_go_iface_methods"):
        ctx._go_iface_methods = {}
        ctx._go_type_methods = {}
        ctx._go_satisfies = {}
    for nm, _kind in getattr(fs, "_go_types", ()):
        ctx.class_map.setdefault(nm, fs.path)
    for nm, methods in sorted(getattr(fs, "_go_interfaces", {}).items()):
        ctx._go_iface_methods.setdefault(nm, set()).update(methods)
    for typ, methods in sorted(getattr(fs, "_go_method_types", {}).items()):
        ctx._go_type_methods.setdefault(typ, set()).update(methods)
    for nm in sorted(fs.name_literals):
        ctx.referenced_names.add(nm)


def interface_satisfaction_sweep(ctx) -> None:
    """Corpus-wide name-based satisfaction: T satisfies I when T's
    method-name set covers I's (the gopls/SCIP static floor — no type
    checking, signatures ignored on purpose: a name match is the only
    defensible static fact at this layer). Runs after the facts sweep
    has collected every interface/method table."""
    if not hasattr(ctx, "_go_iface_methods"):
        return
    for iface in sorted(ctx._go_iface_methods):
        need = ctx._go_iface_methods[iface]
        if not need:
            continue
        for typ in sorted(ctx._go_type_methods):
            if need <= ctx._go_type_methods[typ]:
                ctx._go_satisfies.setdefault(iface, set()).add(typ)


# ---- body scan --------------------------------------------------------------------

GO_NON_CALLS = frozenset({
    "if", "for", "range", "switch", "return", "go", "defer", "select",
    "case", "break", "continue", "fallthrough", "goto", "func", "var",
    "const", "type", "struct", "interface", "map", "chan", "package",
    "import",
})

# head = dotted receiver-or-package path; name = callee. No turbofish,
# no `::` — go qualification is a single dot.
GO_CALL_RE = re.compile(
    r"(?<![\w.$])((?:[A-Za-z_]\w*\.)*)"
    r"([A-Za-z_]\w*)\s*\(")
# typed locals: composite literals (`x := T{...}` / `x := &T{...}`),
# var declarations, make/new constructions
GO_COMPOSITE_LOCAL_RE = re.compile(
    r"\b([a-z_]\w*)\s*:=\s*&?([A-Z]\w*)\{")
GO_VAR_LOCAL_RE = re.compile(
    r"\bvar\s+([a-z_]\w*)\s+([*]?[\w.\[\]]+)")
GO_NEW_LOCAL_RE = re.compile(
    r"\b([a-z_]\w*)\s*:=\s*(?:new|&)\s*\(?([A-Z]\w*)")


def _own_pkg_dir(fs: FileSym) -> str:
    return os.path.dirname(fs.path)


def _pkg_dsts(pkg_dir: str, name: str, ctx) -> list[tuple[str, str]]:
    """(file, fn) pairs for ``name`` across a package dir's files — a Go
    package spans files; resolution is dir-wide, never file-local."""
    out: list[tuple[str, str]] = []
    for rel in sorted(ctx.files):
        if os.path.dirname(rel) != pkg_dir:
            continue
        tfs = ctx.files[rel]
        if tfs.ext != ".go":
            continue
        if name in tfs.funcs:
            out.append((rel, name))
    return out




def scan_file(fs: FileSym, ctx) -> None:
    if fs.ext != ".go":
        return
    for _, fn in sorted(fs.funcs.items(), key=lambda kv: (kv[1].line, kv[0])):
        _scan_body_go(fs, fn, ctx)


def _scan_body_go(fs: FileSym, fn: Func, ctx) -> None:
    src_key, body, var_types = receiver_env(fs, fn, module_vars=False)
    own_dir = _own_pkg_dir(fs)
    for m in GO_COMPOSITE_LOCAL_RE.finditer(body):
        var_types[m.group(1)] = m.group(2)
    for m in GO_VAR_LOCAL_RE.finditer(body):
        var_types[m.group(1)] = m.group(2)
    for m in GO_NEW_LOCAL_RE.finditer(body):
        var_types[m.group(1)] = m.group(2)
    for m in GO_CALL_RE.finditer(body):
        head, name = m.group(1), m.group(2)
        if name in GO_NON_CALLS:
            continue
        if not head:  # bare call: own file -> own package dir -> drop
            if name in fs.funcs:
                ctx._emit_call(src_key, fs.path, name)
                continue
            hits = _pkg_dsts(own_dir, name, ctx)
            if hits:
                for dst, fname in hits:
                    ctx._emit_call(src_key, dst, fname)
                continue
            ctx.referenced_names.add(name)
            continue
        # single-dot qualification: package alias or typed receiver
        seg = head[:-1]
        mod_var = fs.module_vars.get(seg, "")
        if mod_var.startswith("module:"):
            hits = _pkg_dsts(mod_var[len("module:"):], name, ctx)
            hit = False
            for dst, fname in hits:
                if fname[:1].isupper() or dst.startswith(own_dir):
                    ctx._emit_call(src_key, dst, fname)
                    hit = True
            if not hit:
                ctx.referenced_names.add(name)
            continue
        if mod_var == "external":
            # external package member: name-level alive only when the
            # callee is exported-shaped (the rust module-shaped law)
            if name[:1].islower():
                ctx.referenced_names.add(name)
            continue
        # typed receiver: concrete type -> its file; interface -> mirror
        typ = _last_ident(var_types.get(seg, "") or "")
        if not typ and seg[:1].isupper() and seg in ctx.class_map:
            typ = seg  # T.M(...) — package-free type selector
        if typ:
            dst = ctx.class_map.get(typ, "")
            tfs = ctx.files.get(dst) if dst else None
            if tfs is not None and name in tfs.funcs:
                ctx._emit_call(src_key, dst, name)
                continue
            if typ in getattr(ctx, "_go_iface_methods", {}):
                # interface-typed receiver: mirror to every satisfying
                # type's defining file (the _emit_call subclass-mirror
                # analogue, direct — spec files carry no Func)
                dispatched = False
                for sat in sorted(getattr(ctx, "_go_satisfies", {})
                                  .get(typ, ())):
                    srel = ctx.class_map.get(sat, "")
                    stfs = ctx.files.get(srel) if srel else None
                    if stfs is not None and name in stfs.funcs:
                        ctx._emit_call(src_key, srel, name)
                        dispatched = True
                if dispatched:
                    continue
            ctx.referenced_names.add(name)  # unresolved receiver
            continue
        if seg[:1].isupper():
            continue  # external type path (url.URL{...}): no corpus fact
        ctx.referenced_names.add(name)  # unresolvable module-shaped head


# ---- sweeps (ctx-truth, post-harvest) ----------------------------------------------

def import_liveness_sweep(ctx) -> None:
    """An imported Go package's exported API is alive: every non-test
    file of the dir contributes its exported funcs (python plain-import
    semantics, adapted to dir-packages). Unexported funcs stay
    dead-eligible — the honest Go dead-code story."""
    for rel in sorted(ctx.files):
        fs = ctx.files[rel]
        if fs.ext != ".go":
            continue
        for pkg_dir in sorted(fs.imported_modules):
            for other_rel in sorted(ctx.files):
                tfs = ctx.files[other_rel]
                if (tfs.ext != ".go"
                        or os.path.dirname(other_rel) != pkg_dir
                        or other_rel.endswith("_test.go")):
                    continue
                for nm, other in sorted(tfs.funcs.items()):
                    if nm[:1].isupper():
                        ctx.referenced.add(other.key)


# registry choreography binds (langsep) — see extractors/python.py's
# _PASS_* block for the rationale. Go has no re-exports: no rebind pass.
_PASS_IFACE = interface_satisfaction_sweep
_PASS_IMPORTS = import_liveness_sweep
_PASS_FACTS = harvest_facts
