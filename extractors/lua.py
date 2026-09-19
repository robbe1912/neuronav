"""Lua extractor: tree-sitter-lua front-end (issue #342).

Strategy: one parse per file via the pinned grammar pair —
``tree-sitter==0.26.0`` + ``tree-sitter-lua==0.5.0`` (official
tree-sitter-grammars org wheel, verified by parse; ``core`` extra NOT
installed, same pattern as the other front-ends). Defs come from one
ordered S-expression Query plus a structural walk of the
variable-declaration / assignment shapes that carry anonymous
``function()`` values.

IMPORT MODEL: ``require("mod")`` / ``require("mod.sub")`` names a module
by dotted path resolved against the REPO ROOT (the Java package-path
shape): ``mod.sub`` → ``mod/sub.lua`` or ``mod/sub/init.lua``; the
dominant Neovim plugin layout keeps its sources under ``lua/``, so the
same four candidates are tried under ``lua/`` after the direct join
(convention coverage, not package.path classpath awareness — no
LuaRocks/env awareness in v1, per the issue). A require that resolves
records the target rel in ``fs.imported_modules`` (the module's funcs
stay alive as a unit — the js whole-module law) and a
``local m = require(...)`` binding lands in ``fs.module_vars`` so body
scans resolve ``m.fn()`` through it. A require whose spec is not a
string literal (``require("mod_" .. name)``, ``require(var)``) is
dynamic: no static link, no emission — the module's funcs ride the
generic mention-count floor (the js dynamic-import discipline). A
string-literal require that resolves to NO corpus candidate degrades
loudly: one stderr note per process naming the spec and the requiring
file (the go missing-go.mod law), never a silent drop.

ENTRY MODEL (convention-based, documented): ``init.lua`` and
``main.lua`` are the executable conventions (Neovim plugin/runtime
roots; LÖVE main.lua). A convention file that NO other corpus file
requires is a root — its funcs are entries. A required module is never
a root by convention: its liveness rides the require graph (the
dead-module class stays honestly dead — an unreferenced ``mod.lua``
that is neither convention-named nor required leaves its funcs as dead
candidates). Circular requires (a↔b) are file-level edges, not
recursion: each side lands in the other's ``imported_modules`` and the
liveness sweep settles deterministically. Spec/test files —
``_spec.lua`` / ``_test.lua`` suffixes or a ``spec/``/``test/`` path
segment — root all their funcs (the rusthard ``#[test]`` law: helpers
only tests call revive through their call edges).

DISPATCH MODEL: lua method calls split by punctuation. ``T.m(...)`` is
a static lookup: through a require binding it resolves to the target
module file; through an own-file ``local T = {}`` table to the
same-file def; a head that names a GLOBAL table (``function T.m``
without a local decl) resolves name-based across the corpus — global
tables are lua's shared namespace. ``t:m(...)`` (colon form) passes an
implicit self — receiver type is runtime-only, so the method name goes
to the mention floor (``referenced_names``), with a same-file edge when
the receiver's table defines the method (cheap precision, no false
silence). Metatable ``__index = Base`` chains make Base's table methods
reachable through the child: the index sweep marks the base identifier
referenced and keeps the base table's method defs alive in the corpus
(name-based — the go interface-satisfaction floor).

NEVER read ``Node.start_point`` / ``end_point`` — py-tree-sitter
0.26.0 has the Point refcount bug (tree-sitter-py issue #472); every
line number is ``bisect`` over newline byte offsets instead.

Determinism law: captures are re-sorted by ``start_byte`` before any
emission; same-name collisions resolve first-in-file-wins (the cpp
overload law); every sweep iterates ``sorted(...)``. No rng anywhere.

Leaf parser: reads only the file being parsed plus directory-existence
checks for require resolution — never another source file's contents,
never nav or graph; cross-file work lives in the ctx-driven sweeps
below.
"""

from __future__ import annotations

import posixpath
import re
import sys
from pathlib import Path

from tree_sitter import Language, Parser, Query, QueryCursor

import tree_sitter_lua as _tslua

from extractors.common import (  # leaf module: shared text mechanics (#302)
    line_starts_of,
    make_import_liveness_sweep,
    node_line as _line,
    node_text as _text,
    receiver_env,
)
from extractors.model import FileSym, Func

LUA_EXTS = frozenset({".lua"})
LUA_LANG = Language(_tslua.language())
_PARSER = Parser(LUA_LANG)

# def nodes only; names come from the declaration's first named child
# (identifier | dot_index_expression | method_index_expression).
_QUERY_SRC = """
(function_declaration) @fn.def
(variable_declaration) @vdecl
(assignment_statement) @assign
"""
_QUERY = Query(LUA_LANG, _QUERY_SRC)

_IDENT_RE = re.compile(r"\A[A-Za-z_]\w*\Z")

# ---- module-scope regex facts (byte-deterministic, module-walk laws) -------------

_LUA_REQUIRE_RE = re.compile(
    r"""(?<![\w.])require\s*\(\s*(["'])([^"'`\n]+)\1\s*\)""")
_LUA_REQ_BIND_RE = re.compile(
    r"""(?m)^[^\S\n]*local\s+([A-Za-z_]\w*)\s*=\s*"""
    r"""require\s*\(\s*(["'])([^"'`\n]+)\2\s*\)""")
_LUA_TABLE_RE = re.compile(
    r"""(?m)^[^\S\n]*local\s+([A-Za-z_]\w*)\s*=\s*(?:setmetatable\s*\(\s*)?\{""")
_LUA_INDEX_RE = re.compile(r"""__index\s*=\s*([A-Za-z_]\w*)\b""")
_LUA_INIT_CALL_RE = re.compile(
    r"""(?m)^[^\S\n]*local\s+[A-Za-z_]\w*\s*=\s*([A-Za-z_]\w*)\s*\(""")

_WARNED_UNRESOLVED = False


# ---- require resolution (repo-root dotted path + the lua/ convention) ------------

def _repo_root(path: Path, rel: str) -> Path:
    """Absolute repo root derived from the parsed file's own rel depth."""
    return path.parents[len(rel.split("/")) - 1]


def _resolve_spec(spec: str, root: Path) -> str:
    """Repo-rel candidate for a dotted require spec, or ''.

    Order: direct join, then the Neovim ``lua/`` layout; file module
    before directory module (the require() search order shape).
    """
    d = spec.replace(".", "/")
    for prefix in ("", "lua/"):
        base = posixpath.join(prefix, d) if prefix else d
        for cand in (base + ".lua", posixpath.join(base, "init.lua")):
            if (root / cand).is_file():
                return posixpath.normpath(cand)
    return ""


def _note_unresolved(spec: str, rel: str) -> None:
    """One stderr line per process (the go missing-go.mod law)."""
    global _WARNED_UNRESOLVED
    if _WARNED_UNRESOLVED:
        return
    _WARNED_UNRESOLVED = True
    print(f"lua: unresolved require {spec!r} (from {rel}) — outside the "
          f"corpus or a package.path module; no LuaRocks awareness in v1 "
          f"(issue #342); further notes suppressed",
          file=sys.stderr)


# ---- helpers -----------------------------------------------------------------------

def _name_segments(text_val: str) -> list[str]:
    """['T', 'method'] from 'T.method' / 'a.b:c' — [] when not
    identifier-chain shaped."""
    parts = re.split(r"[.:]", text_val)
    return parts if all(_IDENT_RE.match(p) for p in parts) else []


def _params_of(node, src: bytes) -> list[tuple[str, str]]:
    """[(name, "")] from a parameters node — lua carries no types."""
    out: list[tuple[str, str]] = []
    for ch in node.named_children:
        t = _text(ch, src)
        if _IDENT_RE.match(t):
            out.append((t, ""))
    return out


def _fn_from_decl(node, src: bytes, line_starts: list[int]):
    """(func-name, receiver, line, params, body) from a
    function_declaration node; name '' skips."""
    named = [ch for ch in node.named_children
             if ch.type in ("identifier", "dot_index_expression",
                            "method_index_expression")]
    if not named:
        return ("", "", 0, [], "")
    full = _text(named[0], src)
    segs = _name_segments(full)
    if not segs:
        return ("", "", 0, [], "")
    params: list[tuple[str, str]] = []
    body = ""
    for ch in node.named_children:
        if ch.type == "parameters":
            params = _params_of(ch, src)
        elif ch.type == "block":
            body = _text(ch, src)
    if len(segs) == 1:
        return (segs[0], "", _line(node, line_starts), params, body)
    return (segs[-1], ".".join(segs[:-1]), _line(node, line_starts),
            params, body)


def _anon_fn(node, src: bytes, line_starts: list[int]):
    """Same tuple from an assignment-shaped decl whose value is an
    anonymous function_definition (``local f = function()`` /
    ``T.m = function()``)."""
    assign = node
    if node.type == "variable_declaration":
        assign = next((c for c in node.children
                       if c.type == "assignment_statement"), None)
    if assign is None:
        return ("", "", 0, [], "")
    vlist = next((c for c in assign.named_children
                  if c.type == "variable_list"), None)
    exprs = next((c for c in assign.named_children
                  if c.type == "expression_list"), None)
    if vlist is None or exprs is None:
        return ("", "", 0, [], "")
    fdef = next((c for c in exprs.named_children
                 if c.type == "function_definition"), None)
    if fdef is None:
        return ("", "", 0, [], "")
    target = next((c for c in vlist.named_children
                   if c.type in ("identifier", "dot_index_expression")), None)
    if target is None:
        return ("", "", 0, [], "")
    segs = _name_segments(_text(target, src))
    if not segs:
        return ("", "", 0, [], "")
    params: list[tuple[str, str]] = []
    body = ""
    for ch in fdef.named_children:
        if ch.type == "parameters":
            params = _params_of(ch, src)
        elif ch.type == "block":
            body = _text(ch, src)
    line = _line(node, line_starts)
    if len(segs) == 1:
        return (segs[0], "", line, params, body)
    return (segs[-1], ".".join(segs[:-1]), line, params, body)


# ---- parse ----------------------------------------------------------------------

def parse(path: Path, rel: str) -> FileSym:
    """Registry entry point: one FileSym per .lua file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    src = text.encode("utf-8")
    fs = FileSym(path=rel, ext=".lua")
    root = _PARSER.parse(src).root_node
    line_starts = line_starts_of(src)
    caps = QueryCursor(_QUERY).captures(root)

    nodes = []
    for key in ("fn.def", "vdecl", "assign"):
        nodes.extend(caps.get(key, ()))
    nodes.sort(key=lambda n: n.start_byte)

    # -- def collection (first-in-file wins on same-name collisions) ---------------
    heads: dict[str, int] = {}
    impls: dict[str, tuple] = {}
    fs._lua_methods: dict[str, set[str]] = {}
    for node in nodes:
        if node.type == "function_declaration":
            nm, recv, line, params, body = _fn_from_decl(
                node, src, line_starts)
        else:
            nm, recv, line, params, body = _anon_fn(
                node, src, line_starts)
        if not nm or not body:
            continue
        heads.setdefault(nm, line)
        if recv:
            fs._lua_methods.setdefault(recv, set()).add(nm)
        impls.setdefault(nm, (line, body, params))
    for nm in sorted(impls):
        line, body, params = impls[nm]
        fs.funcs.setdefault(nm, Func(path=rel, name=nm,
                                     line=heads.get(nm, line),
                                     body=body, params=params))

    # -- module-scope regex facts (requires, tables, __index, init calls) ----------
    fs._lua_tables: set[str] = set()
    fs._lua_index: set[str] = set()
    root_dir = _repo_root(path, rel)
    for m in _LUA_TABLE_RE.finditer(text):
        fs._lua_tables.add(m.group(1))
    for m in _LUA_INDEX_RE.finditer(text):
        fs._lua_index.add(m.group(1))
    for m in _LUA_INIT_CALL_RE.finditer(text):
        fs.init_calls.add(m.group(1))
    resolved_specs: set[str] = set()
    for m in _LUA_REQ_BIND_RE.finditer(text):
        got = _resolve_spec(m.group(3), root_dir)
        if got:
            fs.imported_modules.add(got)
            resolved_specs.add(m.group(3))
            fs.module_vars.setdefault(m.group(1), "module:" + got)
    for m in _LUA_REQUIRE_RE.finditer(text):
        spec = m.group(2)
        got = _resolve_spec(spec, root_dir)
        if got:
            fs.imported_modules.add(got)
            resolved_specs.add(spec)
        elif spec not in resolved_specs:
            _note_unresolved(spec, rel)

    return fs


# ---- hooks (langsep REQUIRED surface) -------------------------------------------

from extractors.common import entry_keys  # noqa: E402  (late: package cycle)

MENTION_FLOOR = 2
# runtime-fabricated code / global-table dispatch has no static callee
DYNAMIC_HINT = re.compile(
    r"\bload\s*\(|\bloadstring\s*\(|\bdofile\s*|\b_G\.")

# graph's dup normalizer strips these before hashing (issue #295); lua
# line comments (``--``) also open long bracket comments — the per-line
# strip is conservative-correct on both
COMMENT_PREFIXES = ("--",)


def is_entry_exempt(name: str) -> bool:
    return False


def unresolved_base_review(name: str) -> bool:
    """Lua's privacy convention is the underscore prefix — the js law."""
    return name.startswith("_")


def stand_in_review(fs: FileSym, name: str) -> bool:
    return False


def mention_review(name: str, mentions) -> bool:
    return mentions.get(name, 0) >= MENTION_FLOOR


def stat_tags(text: str) -> tuple[str, str]:
    return ("", "")


def is_wiring_only(fs: FileSym) -> bool:
    """Lua files are never pure wiring: a tables-only module still
    declares corpus facts (tables feed method resolution)."""
    return False


def counts_dead_share(fs: FileSym) -> bool:
    """Judge C1: the dead-share denominator counts .lua files (the
    registered-structural-suffix law)."""
    return fs.ext == ".lua"


# ---- entry rules (convention-based; module header documents them) ----------------

_TEST_PATH_RE = re.compile(r"(?:^|/)(?:tests?|specs?)/|_(?:spec|test)\.lua$")
_CONVENTION_NAMES = frozenset({"init.lua", "main.lua"})


def _entry_tests(fs: FileSym, ctx=None):
    if _TEST_PATH_RE.search(fs.path.replace("\\", "/")):
        yield from entry_keys(fs, sorted(fs.funcs))


def _entry_convention(fs: FileSym, ctx=None):
    """init.lua/main.lua that NO other corpus file requires — the
    executable conventions (Neovim plugin roots, LÖVE main)."""
    if fs.ext != ".lua":
        return
    base = fs.path.rsplit("/", 1)[-1]
    if base not in _CONVENTION_NAMES:
        return
    required = getattr(ctx, "_lua_required", None)
    if required is not None and fs.path in required:
        return
    yield from entry_keys(fs, sorted(fs.funcs))


ENTRY_RULES = (_entry_tests, _entry_convention)


# ---- facts harvest (per-file, ctx-truth surfaces) --------------------------------

def harvest_facts(fs: FileSym, ctx) -> None:
    if fs.ext != ".lua":
        return


# ---- body scan --------------------------------------------------------------------

LUA_NON_CALLS = frozenset({
    "require", "pcall", "xpcall", "print", "error", "assert",
    "setmetatable", "getmetatable", "rawget", "rawset", "rawequal",
    "tonumber", "tostring", "type", "select", "next", "unpack",
    "pairs", "ipairs",
})

# head = dotted (or colon) receiver chain; name = callee. ``?:`` in the
# chain marks the implicit-self form (dispatch = runtime-typed).
LUA_CALL_RE = re.compile(
    r"(?<![\w.])(?:([A-Za-z_]\w*(?:[.:][A-Za-z_]\w*)*)[.:])?"
    r"([A-Za-z_]\w*)\s*\(")


def _own_table(fs: FileSym, head: str) -> bool:
    return head in fs._lua_tables or head in fs._lua_methods


def _global_table_dsts(name: str, ctx) -> list[tuple[str, str]]:
    """(file, fn) pairs for methods defined on a GLOBAL table `name`
    (no local decl in the defining file) — lua's shared namespace."""
    out: list[tuple[str, str]] = []
    for rel in sorted(ctx.files):
        other = ctx.files[rel]
        if other.ext != ".lua":
            continue
        methods = getattr(other, "_lua_methods", {})
        if name in methods and name not in getattr(other, "_lua_tables", set()):
            for m in sorted(methods[name]):
                if m in other.funcs:
                    out.append((rel, m))
    return out


def scan_file(fs: FileSym, ctx) -> None:
    if fs.ext != ".lua":
        return
    for fn in [fs.funcs[k] for k in sorted(fs.funcs)]:
        _scan_body_lua(fs, fn, ctx)


def _scan_body_lua(fs: FileSym, fn: Func, ctx) -> None:
    src_key, body, var_types = receiver_env(fs, fn)
    for m in LUA_CALL_RE.finditer(body):
        head, name = m.group(1), m.group(2)
        if not name or name in LUA_NON_CALLS:
            continue
        if not head:
            # bare call: own-file def or drop (bare locals are noise in
            # lua — every file-local helper would match otherwise)
            if name in fs.funcs:
                ctx._emit_call(src_key, fs.path, name)
            continue
        colon = ":" in head
        base = head.split(":")[0].split(".")[0]
        if not colon and base in var_types and \
                var_types[base].startswith("module:"):
            mod_rel = var_types[base][len("module:"):]
            tfs = ctx.files.get(mod_rel, None)
            if tfs is not None and name in tfs.funcs:
                ctx._emit_call(src_key, mod_rel, name)
            else:
                ctx.referenced_names.add(name)
        elif not colon and _own_table(fs, base):
            # own-file table (local T = {} or T.m defined here): methods
            # are file-local — a same-file def is the only honest target
            if name in fs.funcs:
                ctx._emit_call(src_key, fs.path, name)
            else:
                ctx.referenced_names.add(name)
        elif not colon:
            # global table head: shared namespace — name-based across
            # the corpus, mention floor as the fallback
            hit = False
            for dst, fname in _global_table_dsts(base, ctx):
                if fname == name:
                    ctx._emit_call(src_key, dst, fname)
                    hit = True
            if not hit:
                ctx.referenced_names.add(name)
        else:
            # colon form: implicit self — receiver is runtime-typed;
            # same-file edge when the table defines the method, else the
            # mention floor carries the name (never silent)
            if name in fs.funcs:
                ctx._emit_call(src_key, fs.path, name)
            ctx.referenced_names.add(name)


# ---- sweeps (ctx-truth, post-harvest) ----------------------------------------------

def required_sweep(ctx) -> None:
    """Corpus-wide required-file set for the convention entry rule
    (self-requires never mark their own file required)."""
    ctx._lua_required = set()
    for rel in sorted(ctx.files):
        fs = ctx.files[rel]
        if fs.ext != ".lua":
            continue
        ctx._lua_required |= {r for r in fs.imported_modules if r != rel}


def index_sweep(ctx) -> None:
    """Metatable ``__index = Base`` chains: the base identifier is
    referenced and the base table's method defs stay alive — the go
    interface-satisfaction floor (name-based, corpus-wide)."""
    for rel in sorted(ctx.files):
        fs = ctx.files[rel]
        if fs.ext != ".lua":
            continue
        for base in sorted(fs._lua_index):
            ctx.referenced_names.add(base)
            for rel2 in sorted(ctx.files):
                other = ctx.files[rel2]
                if other.ext != ".lua":
                    continue
                methods = getattr(other, "_lua_methods", {})
                if base not in methods:
                    continue
                for m in sorted(methods[base]):
                    if m in other.funcs:
                        ctx.referenced.add(other.funcs[m].key)


import_liveness_sweep = make_import_liveness_sweep(
    LUA_EXTS,
    "lua require() target module's funcs enter ctx.referenced.")


# registry choreography binds (langsep) — see extractors/python.py's
# _PASS_* block for the rationale (attribute dispatch is invisible to
# the module scan; the value-ref arm roots these binds).
_PASS_REQUIRED = required_sweep
_PASS_INDEX = index_sweep
_PASS_IMPORTS = import_liveness_sweep
