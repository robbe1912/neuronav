"""C rider extractor (#346): plain C on the tree-sitter-cpp front-end.

Why a separate module and not ``EXTENSIONS[".c"] = cpp``: cpp.parse()
captures C source cleanly (the grammar is a superset), but its entry and
exemption model is Godot-shaped — ``entry_hints`` stays empty for a
canonical ``main()`` program, ``is_entry_exempt("main")`` is False, and
``CPP_VIRTUALS`` holds engine virtuals only — so ``main()`` would sit in
dead-tier-eligible space and C's address-of/callback idiom (its dominant
implicit-entry shape) has no home in the ClassDB model (verified on the
issue-#346 probes). This module owns the C-shaped model:

- entry: ``main()`` — C's universal entry (programs AND test runners).
  Unity/CMock macro detection is a stated v1 non-goal until evidence
  demands it.
- includes: quoted ``#include "x"`` -> imported_modules; angle-bracket
  system includes are ``system_lib_literal`` grammar nodes the query
  never captures — skipped entirely (system headers never resolve
  within a repo; recording them anywhere feeds mention-floor noise).
- headers: ``.h`` stays cpp's extractor; this module only READS the
  FileSyms cpp.parse produces (cross-module ctx read — the java
  pattern). Pairing: a .c file's quoted include with a matching stem is
  its own header (``widget.c`` -> ``widget.h``); calls to names declared
  in a paired header resolve through to the defining .c impl.
- callbacks: ``&fn`` / ``fn`` in initializers (function-pointer tables
  at TU scope included) keep the target alive via referenced_names —
  the cscope treatment.

Front-end law (py-tree-sitter issue #472): never read
Node.start_point / Node.end_point anywhere in this module — a Point
read corrupts the heap (bare native exit later). Line numbers come
from bisect over newline byte offsets.

Determinism: captures sorted bytewise (start_byte); collection order
is document order; first-in-file-wins for duplicate fn names (the
overload-collapse law — C has no overloads, but macros and conditional
compilation can mint duplicate shapes).
"""

from __future__ import annotations

import re
from bisect import bisect_right
from functools import partial
from pathlib import Path
from typing import Iterable

from tree_sitter import Language, Parser, Query, QueryCursor

from extractors.common import (  # neutral capture/site mechanics (#377)
    captures_bytewise,
    entry_keys,
    fn_key,
    line_starts_of,
    owner_at,
    resolve_include as _resolve_include,
    signature,
    type_text as _type_text,
)
from extractors.model import FileSym, Func

C_EXTS = frozenset({".c"})
# .h/.hpp stay cpp's extractor (issue #346 fix shape): the pairing pass
# only READS the FileSyms cpp.parse produces for them.
C_HEADER_EXTS = frozenset({".h", ".hpp"})

C_LANG = Language(__import__("tree_sitter_cpp").language())
_PARSER = Parser(C_LANG)

# C subset of the cpp capture set (issue #346 fix shape). Free functions
# carry an identifier name (no field/qualified/destructor/operator arms
# — C++-only shapes); declarations without a body never match
# function_definition, so a pure-declaration header parses to funcs={}
# and the .c definition stays the single Func. preproc_include captures
# string_literal paths only — angle-bracket system includes are
# system_lib_literal nodes and stay invisible.
_QUERY_SRC = """
(function_definition declarator: (function_declarator declarator: (identifier) @fn.name)) @fn.def
(function_definition declarator: (pointer_declarator (function_declarator declarator: (identifier) @fn.name))) @fn.def
(field_declaration declarator: (field_identifier) @member.name) @member.decl
(enumerator name: (identifier) @const.name) @const.def
(preproc_include path: (string_literal) @include.path) @include.def
(call_expression function: (identifier) @calli.name) @call.def
(pointer_expression argument: (identifier) @fref.name) @fref.def
(initializer_list (identifier) @init.name)
(declaration declarator: (identifier) @gvar.name) @gvar.def
(declaration declarator: (init_declarator declarator: (identifier) @gvar.name)) @gvar.def
(declaration declarator: (array_declarator (identifier) @gvar.name)) @gvar.def
(declaration declarator: (init_declarator declarator: (array_declarator (identifier) @gvar.name))) @gvar.def
(type_definition declarator: (type_identifier) @td.name) @td.def
"""
_QUERY = Query(C_LANG, _QUERY_SRC)

# node types that can carry a parameter/return/field type spelling
# (C subset of cpp's set)
_TYPE_NODES = frozenset(
    {
        "type_identifier",
        "primitive_type",
        "sized_type_specifier",
    }
)

# dead-tier mention-count corroboration floor (issue #20 law, shared
# with cpp): a C name whose raw-text mentions across the corpus reach
# this count (its own definition plus at least one more) is wired
# somewhere the static pass cannot see, so 'likely' overclaims.
C_MENTION_FLOOR = 2


# the C grammar's ident spellings (the cpp extractor's set adds
# field_identifier) — data for the hoisted find_ident/signature (#377)
_IDENT_TYPES = ("identifier",)


def parse(path: Path, rel: str) -> FileSym:
    """Parse one C file (pure: reads only this file)."""
    fs = FileSym(path=rel, ext=path.suffix.lower())
    src = path.read_bytes()
    tree = _PARSER.parse(src)
    caps = QueryCursor(_QUERY).captures(tree.root_node)
    line_starts = line_starts_of(src)

    bytewise = partial(captures_bytewise, caps)

    fn_names = bytewise("fn.name")
    for fd in bytewise("fn.def"):
        nm = next(
            (n for n in fn_names if fd.start_byte <= n.start_byte < fd.end_byte),
            None,
        )
        if nm is None:
            continue  # def shape with no captured name arm
        name = src[nm.start_byte:nm.end_byte].decode("utf8", "replace")
        if not name or name in fs.funcs:
            continue  # first definition in file order wins
        params, ret = signature(src, fd, _TYPE_NODES, _IDENT_TYPES)
        fs.funcs[name] = Func(
            path=rel,
            name=name,
            line=bisect_right(line_starts, fd.start_byte),
            body=src[fd.start_byte:fd.end_byte].decode("utf8", "replace"),
            params=params,
            ret=ret,
        )

    # struct/union data members: name -> TypeName (C has no access
    # regions — private_members stays empty)
    member_names = bytewise("member.name")
    for md in bytewise("member.decl"):
        nm = next(
            (n for n in member_names if md.start_byte <= n.start_byte < md.end_byte),
            None,
        )
        if nm is None:
            continue
        ty = ""
        for c in md.children:
            if c.type in _TYPE_NODES:
                ty = _type_text(src, c)
                break
        fs.members[src[nm.start_byte:nm.end_byte].decode("utf8", "replace")] = ty

    # enum surface: raw enumerators (BIND_* registration macros are the
    # Godot C++ surface — C has no analog)
    for n in bytewise("const.name"):
        fs.consts.setdefault(src[n.start_byte:n.end_byte].decode("utf8", "replace"), "")

    # quoted includes only — system <...> includes are system_lib_literal
    # nodes the query never captures
    for n in bytewise("include.path"):
        inc = src[n.start_byte:n.end_byte].decode("utf8", "replace").strip('"')
        if inc:
            fs.imported_modules.add(inc)

    # file-scope variables: only declarations whose parent is the
    # translation unit — locals inside function bodies also parse as
    # declarations and must stay out
    gvar_names = bytewise("gvar.name")
    for gd in bytewise("gvar.def"):
        scope = gd.parent.type if gd.parent is not None else ""
        if scope != "translation_unit":
            continue
        nm = next(
            (n for n in gvar_names if gd.start_byte <= n.start_byte < gd.end_byte),
            None,
        )
        if nm is None:
            continue
        ty = ""
        for c in gd.children:
            if c.type in _TYPE_NODES:
                ty = _type_text(src, c)
                break
        fs.globals[src[nm.start_byte:nm.end_byte].decode("utf8", "replace")] = ty

    # typedefs: name -> target type text between keyword and name
    td_names = bytewise("td.name")
    for d in bytewise("td.def"):
        nm = next(
            (n for n in td_names if d.start_byte <= n.start_byte < d.end_byte),
            None,
        )
        if nm is None:
            continue
        name = src[nm.start_byte:nm.end_byte].decode("utf8", "replace")
        decl = src[d.start_byte:d.end_byte].decode("utf8", "replace").strip()
        body = decl[len("typedef "):]
        target = body[: body.rfind(name)].strip() if name in body else ""
        fs.aliases[name] = target

    return fs


def scan_calls(path: Path, rel: str) -> list[dict]:
    """Call/reference sites for graph edge minting: bare identifier calls
    (``call``) and function references (``fref``) — ``&fn`` pointer
    expressions AND bare names in brace-initializer lists (the C
    callback-table idiom: a bare fn name in an initializer decays to a
    function pointer; initializer identifiers are conservative — a var
    name colliding with a fn name only ever over-marks alive)."""
    src = path.read_bytes()
    tree = _PARSER.parse(src)
    caps = QueryCursor(_QUERY).captures(tree.root_node)
    line_starts = line_starts_of(src)
    sites: list[dict] = []
    for key, kind in (
        ("calli.name", "call"),
        ("fref.name", "fref"),
        ("init.name", "fref"),
    ):
        for n in sorted(caps.get(key, ()), key=lambda m: m.start_byte):
            sites.append(
                {
                    "name": src[n.start_byte:n.end_byte].decode("utf8", "replace"),
                    "kind": kind,
                    "line": bisect_right(line_starts, n.start_byte),
                }
            )
    return sites


def _entry_main(fs: FileSym, ctx) -> Iterable[str]:
    """main() — C's universal entry: programs and test runners both enter
    here (Unity/CMock macro detection is a stated v1 non-goal)."""
    if fs.ext not in C_EXTS or "main" not in fs.funcs:
        return
    yield from entry_keys(fs, ("main",))


ENTRY_RULES = [_entry_main]


# ---- uniform shared-surface hooks (langsep) -----------------------------------

# C v1 tracks no dynamic-dispatch macro surface (ClassDB/GDVIRTUAL are
# the Godot C++ shapes): a regex that never matches, so an unreachable
# plain-C fn tiers 'likely' — the honest static verdict.
DYNAMIC_HINT = re.compile(r"(?!)")

# thin C forwarder classification for graph's dup filter (issue #295
# law: signature line, exactly one forwarding call, closing brace —
# anything richer never classifies). Allman bodies fold the brace.
_C_DEL_SIG_RE = re.compile(r"^(?:static\s+)?\w+\s+\w+\s*\([^(){};]*\)\s*\{$")
_C_DEL_FWD_RE = re.compile(r"^(?:return\s+)?\w+\s*\([^(){};]*\)\s*;$")

# graph's dup normalizer strips these before hashing (issue #295): C
# line comments — `#include`/`#pragma` lines are preprocessor, NOT
# comments, and stay whole
COMMENT_PREFIXES = ("//",)

MENTION_FLOOR = C_MENTION_FLOOR


def pure_delegate(norm: str) -> bool:
    lines = [ln.strip() for ln in norm.splitlines()]
    if len(lines) == 4 and lines[1] == "{":
        lines = [lines[0] + " {"] + lines[2:]
    return (
        len(lines) == 3
        and _C_DEL_SIG_RE.match(lines[0]) is not None
        and _C_DEL_FWD_RE.match(lines[1]) is not None
        and lines[2] == "}"
    )


def is_entry_exempt(name: str) -> bool:
    """No C implicit-entry shapes beyond main (an ENTRY_RULES root)."""
    return False


def unresolved_base_review(name: str) -> bool:
    """No underscore-convention tail for C."""
    return False


def stand_in_review(fs: FileSym, name: str) -> bool:
    """Python-only rule (module-scope stand-ins)."""
    return False


def mention_review(name: str, mentions: dict) -> bool:
    """Name keeps appearing across the corpus (comments, dropped
    ambiguous calls): wired somewhere static passes cannot see."""
    return mentions.get(name, 0) >= C_MENTION_FLOOR


# ---- build passes + body scan (langsep) --------------------------------------

def harvest_facts(fs: FileSym, ctx) -> None:
    """Name-literal liveness for C (parse harvests none in v1 — the
    surface stays so future string-dispatch tables route here)."""
    if fs.ext not in C_EXTS:
        return
    for nm in fs.name_literals:
        if len(nm) > 3:
            ctx.referenced_names.add(nm)
    for nm in fs.init_calls:
        ctx.referenced_names.add(nm)


def pair_headers(ctx) -> None:
    """Bind .c impls to their headers (BUILD step — must run before the
    scan loop so call resolution sees the maps).

    STEM-ONLY pairing: an impl's quoted include whose file stem matches
    its own (``widget.c`` -> ``widget.h``) is its header. No fallback:
    a first-resolvable-header fallback let any includer claim a header
    (dead.c sorting before widget.c claimed widget.h and blocked the
    real impl — the smoke fixture caught it), and split-impl corpora
    (several .c behind one .h) simply stay unpaired: their definitions
    keep mention-floor review tiers — conservative, honest. First impl
    wins on a genuinely duplicated stem (deterministic: sorted
    iteration). .h FileSyms are cpp.parse output (cross-module READ —
    .h stays cpp's extractor).
    """
    ctx.c_header_of = {}
    ctx.c_impl_of = {}
    for rel in sorted(ctx.files):
        fs = ctx.files[rel]
        if fs.ext not in C_EXTS:
            continue
        own_stem = rel.rsplit("/", 1)[-1].split(".")[0]
        for inc in sorted(fs.imported_modules):
            resolved = _resolve_include(ctx, rel, inc)
            if (
                resolved
                and ctx.files[resolved].ext in C_HEADER_EXTS
                and resolved.rsplit("/", 1)[-1].split(".")[0] == own_stem
            ):
                ctx.c_header_of[rel] = resolved
                ctx.c_impl_of.setdefault(resolved, rel)
                break


def _include_headers(ctx, rel: str, fs: FileSym) -> list[str]:
    """Resolved .h FileSyms a .c file reads (its own pair first)."""
    hdrs: list[str] = []
    pair = getattr(ctx, "c_header_of", {}).get(rel, "")
    if pair:
        hdrs.append(pair)
    for inc in sorted(fs.imported_modules):
        resolved = _resolve_include(ctx, rel, inc)
        if resolved and resolved not in hdrs and ctx.files[resolved].ext in C_HEADER_EXTS:
            hdrs.append(resolved)
    return hdrs


def scan_file(fs: FileSym, ctx) -> None:
    """C body scan: bare-name call edges + callback liveness.

    Resolution: file-local funcs first, then the headers the file reads
    (pair first) — a name DECLARED in a header resolves to the paired
    impl's DEFINITION when one exists, else the header itself (static
    inline). Unresolved plain calls are DROPPED (the same-name-elsewhere
    ambiguity guard); ``&fn`` references keep the name-alive liveness
    path — a function reachable ONLY through a function pointer is
    alive, including TU-scope callback tables (no container there, the
    edge is skipped, the liveness name lands).
    """
    if fs.ext not in C_EXTS:
        return
    try:
        sites = scan_calls(ctx.path_for(fs.path), fs.path)
    except OSError:
        return
    if not sites or not fs.funcs:
        return
    # body-span aware: a site belongs to a fn only between its def line
    # and its body's last line — TU-scope callback tables sitting after
    # the last fn's closing brace must NOT attribute to that fn (the
    # smoke fixture's TABLE[] after orphan_b minted a bogus edge). The
    # span-aware semantics are the hoisted common.owner_at (#377) — this
    # site fixed them; every container() now spells the same closure.
    order = sorted(fs.funcs.values(), key=lambda f: f.line)
    spans = [(f.line, f.line + f.body.count("\n"), f.key) for f in order]
    hdrs = _include_headers(ctx, fs.path, fs)

    container = partial(owner_at, order, spans=spans)

    for site in sites:
        name = site["name"]
        kind = site["kind"]
        dst = ""
        if name in fs.funcs:
            dst = fn_key(fs.path, name)
        else:
            for hdr in hdrs:
                if name in ctx.files[hdr].funcs:
                    # definition header (static inline): the paired impl
                    # wins when it redefines the name, else the header
                    impl = getattr(ctx, "c_impl_of", {}).get(hdr, "")
                    if impl and name in ctx.files[impl].funcs:
                        dst = fn_key(impl, name)
                    else:
                        dst = fn_key(hdr, name)
                    break
                impl = getattr(ctx, "c_impl_of", {}).get(hdr, "")
                if impl and name in ctx.files[impl].funcs:
                    # pure-declaration header (funcs={} — declarations
                    # never match function_definition): the paired .c
                    # definition IS the call target
                    dst = fn_key(impl, name)
                    break
        if dst:
            src = container(site["line"])
            if src and src != dst:
                ctx._edge(src, dst, ty="call")
            elif not src and kind == "fref":
                # TU-scope callback table entry with no containing fn:
                # the edge has no source, but the reference is real —
                # the target stays alive through referenced_names
                ctx.referenced_names.add(name)
        elif kind == "fref":
            ctx.referenced_names.add(name)


# conservative include-liveness: including a repo header keeps the
# header's own defined surface (static inline) alive — over-marking
# only keeps fns alive, never false-dead (the python `import x`
# semantic; headers-as-pure-declarations parse to funcs={} and mark
# nothing). LOCAL implementation, not the common factory: the factory
# resolves `mod in ctx.files` with the raw include string, which never
# matches the canonical same-dir idiom (`#include "widget.h"` inside
# src/widget.c vs ctx.files key "src/widget.h") — _resolve_include is
# the resolver that already handles it (GK #348 review).
def import_liveness_sweep(ctx) -> None:
    """C include liveness: a quoted include resolving to a repo .h/.hpp
    keeps the header's defined (static inline) funcs referenced."""
    for rel in sorted(ctx.files):
        fs = ctx.files[rel]
        if fs.ext not in C_EXTS:
            continue
        for inc in sorted(fs.imported_modules):
            resolved = _resolve_include(ctx, rel, inc)
            if not resolved or ctx.files[resolved].ext not in C_HEADER_EXTS:
                continue
            for fn in ctx.files[resolved].funcs.values():
                ctx.referenced.add(fn.key)


def is_wiring_only(fs: FileSym) -> bool:
    """C files always carry funcs — never wiring-only."""
    return False


def counts_dead_share(fs: FileSym) -> bool:
    """C files never join the dead-file denominator."""
    return False


def stat_tags(text: str) -> tuple[str, str]:
    """C has no class_name/extends header notion — empty tags."""
    return ("", "")
