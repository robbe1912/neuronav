"""Rust extractor: tree-sitter-rust front-end (issue #244).

Strategy: one parse per file via the pinned grammar pair —
``tree-sitter==0.26.0`` + ``tree-sitter-rust==0.24.2`` (the newest
PyPI release whose ABI loads under the 0.26 core — verified by parse,
same method as the tree-sitter-cpp==0.23.4 pin). Defs come from one
ordered S-expression Query; ``use``/``mod`` declarations get a
structural walk (segment pairing needs field access a query cannot
express).

MACRO POLICY (loud, deliberate): macro invocations are RECORDED at
their call-site — the macro name lands in ``fs.name_literals`` when it
is not a known std/prelude macro — and NEVER expanded. A fn referenced
only from inside a ``macro_rules!`` template has no static call site;
it survives through the corpus-wide mention floor (2 mentions ->
review tier), never as 'likely'-dead. Expansion is out of scope for a
static graph.

Entry model (convention-based, documented): ``fn main`` anywhere is a
root; ``#[test]`` / ``#[tokio::test]`` attributes make their fn an entry
hint; a bare ``#[cfg(test)]`` gate marks nothing (the #[test] fns inside
a cfg'd ``mod`` attribute themselves — uncalled cfg'd helpers stay dead);
lib.rs's bare-``pub`` top-level fns plus the transitive closure over
``pub mod`` declarations are the crate's exported API (roots). Cargo
targets beyond the src/main.rs + src/lib.rs convention ([[bin]] path
overrides) are NOT parsed — Cargo.toml is not a Rust source file. A
file directly under a ``bin`` directory is a cargo bin target walked
as its OWN crate root: ``crate::``/bare module heads anchor at its own
module dir (``src/bin/x.rs`` → ``src/bin/x/``), never at the sibling
lib's ``src/`` (issue #284; dir-shaped ``src/bin/x/main.rs`` bins were
already anchored by the main.rs walk).

pub-visibility analysis: only bare ``pub`` counts as exported;
``pub(crate)`` / ``pub(super)`` / ``pub(in ..)`` are crate-local and
stay dead-eligible. Exported-ness is judged from the lib.rs pub-mod
chain (the only root a static pass can defend); pub fns in modules not
reachable from lib.rs through all-pub mod declarations stay eligible.

Re-exports (the barrel analogue): ``pub use net::send as transmit``
records both the barrel's own binding and a re-export table;
consumers' ``use crate::transmit;`` (crate-root re-exports have no
filesystem spelling, so they resolve in the rebind sweep) and bare
``transmit()`` call-sites rebind to the ORIGIN definer — the ts
barrel law, adapted to path-qualified Rust imports.

NEVER read ``Node.start_point`` / ``end_point`` — py-tree-sitter 0.26.0
has the Point refcount bug (tree-sitter-py issue #472); every line
number is ``bisect`` over newline byte offsets instead.

Determinism law: captures are re-sorted by ``start_byte`` before any
emission; same-name collisions resolve first-in-file-wins (the cpp
overload law — a trait's signature-only declaration owns the line, the
impl's body owns the Func); every sweep iterates ``sorted(...)``. No
rng anywhere.

Leaf parser: reads only the file being parsed (plus sibling-module
existence checks for ``mod``/``use`` resolution — filesystem stat
only, no other source file's contents) and never imports nav or graph
— cross-file work lives in the ctx-driven sweeps below.
"""

from __future__ import annotations

import bisect
import os
import posixpath
import re
from pathlib import Path

from tree_sitter import Language, Parser, Query, QueryCursor

import tree_sitter_rust as _tsr

from extractors.model import FileSym, Func

RUST_EXTS = frozenset({".rs"})
RUST_LANG = Language(_tsr.language())
_PARSER = Parser(RUST_LANG)

_IDENT_TYPES = ("identifier", "type_identifier", "field_identifier")

# def nodes only; names are pulled structurally per node (the grammar's
# `name` field or the first identifier-shaped child).
_QUERY_SRC = """
(function_item) @fn.def
(function_signature_item) @tsig.def
(struct_item) @struct.def
(enum_item) @enum.def
(union_item) @union.def
(trait_item) @trait.def
(type_item) @talias.def
(impl_item) @impl.def
(mod_item) @mod.def
(use_declaration) @use.def
(attribute_item) @attr
(macro_invocation) @macro.call
(macro_definition) @macro.def
(const_item) @const.def
(static_item) @static.def
(field_declaration) @field.def
(enum_variant) @variant
"""
_QUERY = Query(RUST_LANG, _QUERY_SRC)


# ---- node helpers (no Point reads — module header law) --------------------------

def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _line(node, line_starts: list[int]) -> int:
    return bisect.bisect_right(line_starts, node.start_byte)


def _ident_child(node, src: bytes) -> str:
    for ch in node.children:
        if ch.type in _IDENT_TYPES:
            return _text(ch, src)
    return ""


def _last_ident(text_val: str) -> str:
    ids = re.findall(r"[A-Za-z_]\w*", text_val)
    return ids[-1] if ids else ""


def _body_block(node, src: bytes) -> str:
    for ch in node.children:
        if ch.type == "block":
            return _text(ch, src)
    return ""


def _signature(node, src: bytes) -> tuple[list[tuple[str, str]], str]:
    """([(name, type)], ret) from a fn node's parameters + `->` arm."""
    params: list[tuple[str, str]] = []
    ret = ""
    seen_arrow = False
    for ch in node.children:
        if ch.type == "parameters":
            for p in ch.children:
                if p.type == "self_parameter":
                    params.append(("self", _text(p, src)))
                elif p.type == "parameter":
                    pid = pty = ""
                    colon = False
                    for pc in p.children:
                        if pc.type == "identifier" and not colon:
                            pid = pid or _text(pc, src)
                        elif pc.type == ":":
                            colon = True
                        elif colon and not pty:
                            pty = _text(pc, src)
                    if pid:
                        params.append((pid, pty))
        elif ch.type == "->":
            seen_arrow = True
        elif seen_arrow and not ret and ch.type != "block":
            ret = _text(ch, src)
    return params, ret


# ---- module-path resolution (leaf: existence checks only) ------------------------

_ROOT_FILES = ("lib.rs", "main.rs", "mod.rs")


def _crate_root_dir(path: Path) -> Path:
    """Nearest ancestor (self included) holding lib.rs/main.rs — the
    crate-root directory for `crate::` segments. Falls back to the
    file's own directory at the filesystem top."""
    cur = path.parent
    while True:
        if (cur / "lib.rs").is_file() or (cur / "main.rs").is_file():
            return cur
        if cur.parent == cur:
            return path.parent
        cur = cur.parent


def _is_bin_target(path: Path) -> bool:
    """A file directly under a directory named ``bin`` is a cargo bin
    target (src/bin/x.rs): its OWN crate root, never the sibling lib's
    src/ (issue #284)."""
    return path.parent.name == "bin"


def _crate_root_file(path: Path) -> Path | None:
    if _is_bin_target(path):
        return path
    root = _crate_root_dir(path)
    for nm in ("lib.rs", "main.rs"):
        if (root / nm).is_file():
            return root / nm
    return None


def _mod_dir(path: Path) -> Path:
    """Directory a module's child modules live in: mod.rs/lib.rs/main.rs
    are roots (children are siblings); foo.rs declares foo/*.rs."""
    if path.name in _ROOT_FILES:
        return path.parent
    return path.parent / path.stem


def _mod_file_at(directory: Path, name: str) -> Path | None:
    """The file backing module `name` under `directory`, 2018-style."""
    for cand in (directory / f"{name}.rs", directory / name / "mod.rs"):
        if cand.is_file():
            return cand
    return None


def _mod_decl_target(path: Path, name: str) -> Path | None:
    """File backing `mod name;` declared in `path` (root files declare
    siblings; foo.rs declares foo/name.rs; flat layouts fall back to
    sibling form — fixture crates and pre-2018 trees)."""
    for base in (_mod_dir(path), path.parent):
        got = _mod_file_at(base, name)
        if got is not None:
            return got
    return None


def _resolve_chain(path: Path, segments):
    """(module_file, child_dir) after walking a segment chain, or
    (None, None) on the first miss.

    crate::/self::/super:: prefixes anchor at the crate root / the
    file's own module dir / its parent; bare heads try the crate root
    (2018 in-crate paths — external crates miss and record nothing).
    """
    if not segments:
        return (None, None)
    head = segments[0]
    # bin targets anchor at their own module dir, never the lib's src/
    anchor = _mod_dir(path) if _is_bin_target(path) else _crate_root_dir(path)
    if head == "crate":
        directory, segs = anchor, segments[1:]
    elif head == "self":
        directory, segs = _mod_dir(path), segments[1:]
    elif head == "super":
        directory, segs = _mod_dir(path).parent, segments[1:]
    else:
        directory, segs = anchor, segments
    if not segs and head in ("crate", "self", "super"):
        return (None, directory)
    cur = None
    childdir = directory
    for seg in segs:
        got = _mod_file_at(directory, seg)
        if got is None:
            return (None, None)
        cur = got
        directory = got.parent if got.name == "mod.rs" else got.parent / got.stem
        childdir = directory
    return (cur, childdir)


def _rel_of_target(abs_target: Path, path: Path, rel: str) -> str:
    """Repo-rel posix id of an absolute path, derived from the importing
    file's own (abs, rel) pair — parse never learns the walk root."""
    r = os.path.relpath(abs_target, path.parent).replace(os.sep, "/")
    d = posixpath.dirname(rel)
    return posixpath.normpath(posixpath.join(d, r)) if d else posixpath.normpath(r)


# ---- use structural walk ---------------------------------------------------------

def _split_use(node, src: bytes):
    """(is_pub, arg-node) of a use_declaration."""
    is_pub = any(ch.type == "visibility_modifier" for ch in node.children)
    arg = None
    for ch in node.children:
        if ch.type not in ("visibility_modifier", "use", ";"):
            arg = ch
            break
    return is_pub, arg


def _use_segments(node, src: bytes) -> list[str]:
    """Identifier segments of a scoped/identifier use argument."""
    if node is None:
        return []
    return re.findall(r"[A-Za-z_]\w*", _text(node, src))


def _bind_module(fs: FileSym, name: str, trel: str) -> None:
    """A module binding in scope (`use crate::net;`, `mod net {}`):
    net::item() resolves against trel; no whole-module liveness — only
    named imports and glob uses make fns alive."""
    fs.module_vars.setdefault(name, "module:" + trel)


def _take_use(node, src: bytes, path: Path, rel: str, fs: FileSym) -> None:
    """One use_declaration -> from_imports / module_vars / imported_modules.

    `pub use` additionally records the re-export table for the rebind
    sweep; crate-root item imports that have no filesystem spelling
    (`use crate::transmit;` — transmit is a re-export, not a module)
    land in the pending set for sweep-time resolution. External-crate
    paths record nothing.
    """
    is_pub, arg = _split_use(node, src)
    if arg is None:
        return
    reexports: dict = getattr(fs, "_rust_reexports", None) or {}
    pending: set = getattr(fs, "_rust_pending_uses", None) or set()
    aliases: dict = getattr(fs, "_rust_aliases", None) or {}

    def record_import(trel: str, item: str, local: str = "") -> None:
        fs.from_imports.add((trel, item))
        if local and local != item:
            aliases[local] = (trel, item)
        if is_pub:
            reexports[local or item] = (trel, item)

    def bind_path(segs: list[str], local: str = "") -> None:
        """Named import: module-chain + item; module bind when the whole
        path resolves as modules; crate-root pending otherwise."""
        if not segs:
            return
        modfile, _ = _resolve_chain(path, segs)
        if modfile is not None:
            _bind_module(fs, local or segs[-1], _rel_of_target(modfile, path, rel))
            return
        if len(segs) > 1:
            chain, _ = _resolve_chain(path, segs[:-1])
            if chain is not None:
                record_import(_rel_of_target(chain, path, rel), segs[-1], local)
                return
        if len(segs) == 2 and segs[0] == "crate":
            # `use crate::name;` where name is a crate-root re-export:
            # no file to stat — resolve against the root file at sweep
            root = _crate_root_file(path)
            if root is not None:
                pending.add((_rel_of_target(root, path, rel), local or segs[-1]))

    if arg.type == "scoped_identifier":
        bind_path(_use_segments(arg, src))
    elif arg.type == "use_as_clause":
        inner = arg.children[0] if arg.children else None
        ids = [c for c in arg.children if c.type == "identifier"]
        local = _text(ids[-1], src) if ids else ""
        segs = _use_segments(inner, src)
        if local:
            bind_path(segs, local)
    elif arg.type == "scoped_use_list":
        prefix, items = [], None
        for ch in arg.children:
            if ch.type != "use_list":
                prefix = _use_segments(ch, src)
                break
        items = next((ch for ch in arg.children if ch.type == "use_list"), None)
        if items is None:
            return
        chain, childdir = _resolve_chain(path, prefix)
        trel = _rel_of_target(chain, path, rel) if chain is not None else ""
        _take_use_items(items, src, path, rel, fs, trel, childdir, is_pub)
    elif arg.type == "use_list":
        # `use {a, b};` — no prefix, no anchor: not a resolvable form
        pass
    fs._rust_reexports = reexports
    fs._rust_pending_uses = pending
    fs._rust_aliases = aliases


def _take_use_items(items, src: bytes, path: Path, rel: str, fs: FileSym,
                    trel: str, childdir, is_pub: bool) -> None:
    """Items of `use prefix::{a, b as c, d::*}` (one nesting level)."""
    for sp in items.children:
        if sp.type == "use_wildcard":
            if trel:
                fs.imported_modules.add(trel)  # glob: whole-module liveness
        elif sp.type == "identifier":
            nm = _text(sp, src)
            if not nm:
                continue
            modfile = _mod_file_at(childdir, nm) if childdir is not None else None
            if modfile is not None:  # `use crate::{net}` — a module item
                _bind_module(fs, nm, _rel_of_target(modfile, path, rel))
            elif trel:
                fs.from_imports.add((trel, nm))
                if is_pub:
                    rex: dict = getattr(fs, "_rust_reexports", None) or {}
                    rex[nm] = (trel, nm)
                    fs._rust_reexports = rex
        elif sp.type == "scoped_identifier":
            segs = _use_segments(sp, src)
            if trel and len(segs) == 2:  # `use a::{b::c}` — inline chain
                fs.from_imports.add((trel, segs[-1]))
        elif sp.type == "use_as_clause":
            ids = [c for c in sp.children if c.type == "identifier"]
            segs = _use_segments(sp.children[0] if sp.children else None, src)
            if trel and ids and segs:
                fs.from_imports.add((trel, segs[-1]))
                local = _text(ids[-1], src)
                if is_pub and local != segs[-1]:
                    rex = getattr(fs, "_rust_reexports", None) or {}
                    rex[local] = (trel, segs[-1])
                    fs._rust_reexports = rex
                if local != segs[-1]:
                    als: dict = getattr(fs, "_rust_aliases", None) or {}
                    als[local] = (trel, segs[-1])
                    fs._rust_aliases = als


# ---- parse ----------------------------------------------------------------------

def parse(path: Path, rel: str) -> FileSym:
    """Registry entry point: one FileSym per .rs file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    src = text.encode("utf-8")
    fs = FileSym(path=rel, ext=".rs")
    root = _PARSER.parse(src).root_node
    line_starts = [0] + [i + 1 for i, b in enumerate(src) if b == 0x0A]
    caps = QueryCursor(_QUERY).captures(root)

    def bytewise(*keys: str) -> list:
        nodes = []
        for k in keys:
            nodes.extend(caps.get(k, ()))
        return sorted(nodes, key=lambda n: n.start_byte)

    # -- fn collection: signature-only heads first (trait decls own the
    # -- line, impl bodies own the Func — the cpp overload law) ----------
    heads: dict[str, int] = {}
    impls: dict[str, tuple] = {}
    for node in bytewise("tsig.def"):
        nm = _ident_child(node, src)
        if nm:
            heads.setdefault(nm, _line(node, line_starts))
    pub_fns: set[str] = set()
    for node in bytewise("fn.def"):
        nm = _ident_child(node, src)
        if not nm:
            continue
        line = _line(node, line_starts)
        heads.setdefault(nm, line)
        body = _body_block(node, src)
        if body:
            impls.setdefault(nm, (line, body) + _signature(node, src))
        # bare-`pub` fns at file/mod top level (not nested in a fn or
        # impl — associated fns ride their type's reachability): the
        # exported-API surface for the lib.rs entry rule
        parent = node.parent
        if parent is not None and parent.type in ("source_file", "mod_item"):
            for ch in node.children:
                if ch.type == "visibility_modifier" and _text(ch, src).strip() == "pub":
                    pub_fns.add(nm)
                    break
    fs._rust_pub_fns = pub_fns
    for nm in sorted(impls):
        line, body, params, ret = impls[nm]
        fs.funcs.setdefault(nm, Func(path=rel, name=nm, line=heads.get(nm, line),
                                     body=body, params=params, ret=ret))

    # -- named types: first type names the file; all feed class_map and
    # -- aliases (struct/enum/union/trait kinds + `type` aliases) --------
    types: list[tuple[str, str]] = []
    for key, kind in (("struct.def", "struct"), ("enum.def", "enum"),
                      ("union.def", "union"), ("trait.def", "trait")):
        for node in bytewise(key):
            nm = _ident_child(node, src)
            if nm:
                types.append((nm, kind))
                fs.aliases.setdefault(nm, kind)
                if not fs.class_name:
                    fs.class_name = nm
    for node in bytewise("talias.def"):
        nm = _ident_child(node, src)
        if nm:
            fs.aliases.setdefault(nm, "type")
    fs._rust_types = types

    # -- impl blocks: trait impls feed _subclasses (dispatch mirroring);
    # -- each impl'd type also names the impl trait set for the scan ----
    trait_impls: list[tuple[str, str]] = []
    for node in bytewise("impl.def"):
        names: list[str] = []
        for ch in node.children:
            if ch.type == "declaration_list":
                break
            if ch.type in ("type_identifier", "scoped_type_identifier", "generic_type"):
                names.append(_last_ident(_text(ch, src)))
        if len(names) >= 2:
            trait_impls.append((names[0], names[-1]))
    fs._rust_trait_impls = trait_impls

    # -- consts / statics / enum variants / struct fields -----------------
    for node in bytewise("const.def"):
        nm = _ident_child(node, src)
        val = ""
        seen_eq = False
        for ch in node.children:
            if ch.type == "=":
                seen_eq = True
            elif seen_eq and not val:
                val = _text(ch, src)
        if nm:
            fs.consts.setdefault(nm, val)
    for node in bytewise("static.def"):
        nm = _ident_child(node, src)
        typ = ""
        colon = False
        for ch in node.children:
            if ch.type == ":":
                colon = True
            elif colon and not typ:
                typ = _text(ch, src)
        if nm:
            fs.globals.setdefault(nm, typ)
    enum_defs = bytewise("enum.def")
    for node in bytewise("variant"):
        owner = ""
        for enode in enum_defs:
            if enode.start_byte <= node.start_byte < enode.end_byte:
                owner = _ident_child(enode, src)
                break
        nm = _ident_child(node, src)
        if owner and nm:
            fs.consts.setdefault(nm, f"<{owner}>")
    for node in bytewise("field.def"):
        nm = _ident_child(node, src)
        typ = ""
        colon = False
        for ch in node.children:
            if ch.type == ":":
                colon = True
            elif colon and not typ:
                typ = _text(ch, src)
        if nm:
            fs.members.setdefault(nm, typ)

    # -- attributes: sibling runs attributed to the NEXT non-attr item;
    # -- test attrs make their fn an entry hint. Attribution skips the
    # -- rest of the attribute run (`#[tokio::test]` + `#[ignore]`
    # -- stacked on one fn must still root it) ----------------------------
    timeline = sorted(
        (n.start_byte, n.end_byte, k, n)
        for k in ("fn.def", "mod.def", "attr") for n in bytewise(k))
    for i, (_s, end, kind, node) in enumerate(timeline):
        if kind != "attr":
            continue
        attr_txt = _text(node, src).strip().strip("#[]")
        nxt = next((t for t in timeline[i + 1:]
                    if t[0] >= end and t[2] != "attr"), None)
        if nxt is None or nxt[2] != "fn.def":
            continue
        path_txt = attr_txt.split("(", 1)[0].strip()
        is_test = path_txt in ("test", "tokio::test") or (
            path_txt == "cfg" and "test" in attr_txt)
        if not is_test:
            continue
        nm = _ident_child(nxt[3], src)
        if nm:
            fs.entry_hints.add(nm)

    # -- mod declarations: namespaces. Inline mods bind to this file;
    # -- `mod x;` binds (and pub-mods record for the API closure) to the
    # -- sibling file ------------------------------------------------------
    pub_mods: dict[str, str] = {}
    mod_decls: set[str] = set()
    for node in bytewise("mod.def"):
        nm = _ident_child(node, src)
        if not nm:
            continue
        if any(ch.type == "declaration_list" for ch in node.children):
            _bind_module(fs, nm, rel)  # inline: name::f() resolves here
            continue
        tgt = _mod_decl_target(path, nm)
        if tgt is None:
            continue  # generated/external mod: calls fall to name-level liveness
        trel = _rel_of_target(tgt, path, rel)
        _bind_module(fs, nm, trel)
        mod_decls.add(nm)
        if any(ch.type == "visibility_modifier" for ch in node.children):
            pub_mods[nm] = trel  # `pub mod`: exported-API closure edge
    fs._rust_pub_mods = pub_mods
    fs._rust_mod_decls = mod_decls

    # -- macros: RECORD call-sites, never expand (module header law) ------
    fs._rust_macro_defs = {
        _ident_child(node, src) for node in bytewise("macro.def")}
    for node in bytewise("macro.call"):
        nm = _ident_child(node, src)
        if nm and nm not in _STD_MACROS:
            fs.name_literals.add(nm)

    for node in bytewise("use.def"):
        _take_use(node, src, path, rel, fs)
    return fs


# ---- hooks (langsep REQUIRED surface) -------------------------------------------

from extractors.common import entry_keys  # late: package cycle, ts.py precedent

MENTION_FLOOR = 2
DYNAMIC_HINT = re.compile(r"\bdyn\b")  # trait-object dispatch: erased receivers

# std-trait methods invoked without a textual call site: operator
# overloads (infix syntax), `for`-loop desugaring (into_iter/next),
# `{}`-formatting (fmt), scope-end drops, HashMap hashing. The cpp
# CPP_VIRTUALS analogue — dead-scan can never disprove their liveness.
# `.clone()`/`.into()`-style methods DO have textual sites and need no
# shield. Kept minimal; extend only with dispatch evidence.
RUST_STD_TRAIT_METHODS = frozenset({
    "fmt", "drop", "hash", "next", "into_iter", "iter",
    "eq", "ne", "lt", "le", "gt", "ge", "cmp", "partial_cmp",
    "add", "sub", "mul", "div", "rem", "neg", "not",
    "bitand", "bitor", "bitxor", "shl", "shr",
    "index", "index_mut", "deref", "deref_mut",
})

# std/prelude macros whose call-sites are never interesting liveness
# facts (they cannot expand to user-fn calls)
_STD_MACROS = frozenset({
    "println", "print", "eprintln", "eprint", "format", "format_args", "vec",
    "write", "writeln", "panic", "assert", "assert_eq", "assert_ne",
    "debug_assert", "debug_assert_eq", "debug_assert_ne", "todo",
    "unimplemented", "unreachable", "matches", "include", "include_str",
    "include_bytes", "concat", "stringify", "env", "option_env", "cfg",
    "compile_error", "line", "column", "file", "module_path", "thread_local",
    "test", "bench", "rustfmt", "clippy",
})


def is_entry_exempt(name: str) -> bool:
    return name in RUST_STD_TRAIT_METHODS


def unresolved_base_review(name: str) -> bool:
    """Rust has no inheritance; the tail arm is the `_unused` convention
    (leading underscore = deliberately unused — review, never likely)."""
    return name.startswith("_")


def stand_in_review(fs: FileSym, name: str) -> bool:
    return False


def mention_review(name: str, mentions) -> bool:
    return mentions.get(name, 0) >= MENTION_FLOOR


def stat_tags(text: str) -> tuple[str, str]:
    """(class_name, extends) header sniff — Rust has neither header form."""
    return ("", "")


def is_wiring_only(fs: FileSym) -> bool:
    """Barrel analogue: a file with zero funcs that only declares mods
    and/or `pub use` re-exports is wiring, not logic."""
    return (fs.ext == ".rs" and not fs.funcs
            and bool(getattr(fs, "_rust_reexports", None)
                     or getattr(fs, "_rust_mod_decls", None)))


def counts_dead_share(fs: FileSym) -> bool:
    """Judge C1: the dead-share denominator counts .rs files (the .gd/
    .ts precedent — registered structural suffixes flag dead files)."""
    return fs.ext == ".rs"


# ---- entry rules (convention-based; module header documents them) ----------------

def _entry_main(fs: FileSym, ctx=None):
    return entry_keys(fs, ("main",))


def _entry_tests(fs: FileSym, ctx=None):
    return entry_keys(fs, sorted(fs.entry_hints))


def _entry_lib(fs: FileSym, ctx=None):
    """lib.rs's exported API: own bare-pub fns + the transitive closure
    over `pub mod` declarations (pub fns of all-pub-mod-chain modules).
    pub(crate)/private fns stay dead-eligible — crate-local by design."""
    if fs.path.rsplit("/", 1)[-1] != "lib.rs" or ctx is None:
        return ()
    out = list(entry_keys(fs, sorted(getattr(fs, "_rust_pub_fns", ()))))
    seen = {fs.path}
    stack = sorted(getattr(fs, "_rust_pub_mods", {}).values())
    while stack:
        rel = stack.pop(0)
        if rel in seen or rel not in ctx.files:
            continue
        seen.add(rel)
        tfs = ctx.files[rel]
        if getattr(tfs, "ext", "") == ".rs":
            out.extend(entry_keys(tfs, sorted(getattr(tfs, "_rust_pub_fns", ()))))
            stack.extend(t for t in sorted(getattr(tfs, "_rust_pub_mods", {}).values())
                         if t not in seen)
    return out


ENTRY_RULES = (_entry_main, _entry_tests, _entry_lib)


# ---- facts harvest (per-file, ctx-truth surfaces) --------------------------------

def harvest_facts(fs: FileSym, ctx) -> None:
    if fs.ext != ".rs":
        return
    for nm, _kind in getattr(fs, "_rust_types", ()):
        ctx.class_map.setdefault(nm, fs.path)
    for trait, typ in getattr(fs, "_rust_trait_impls", ()):
        # dispatch mirroring: a call landing on the trait's default
        # method mirrors to every file implementing the trait
        ctx._subclasses.setdefault(trait, set()).add(fs.path)
        if not hasattr(ctx, "_rust_impl_traits"):
            ctx._rust_impl_traits = {}
        ctx._rust_impl_traits.setdefault(typ, set()).add(trait)
    for nm in sorted(fs.name_literals):
        ctx.referenced_names.add(nm)


# ---- body scan --------------------------------------------------------------------

RUST_NON_CALLS = frozenset({
    "if", "while", "for", "match", "return", "in", "as", "fn", "let",
    "move", "unsafe", "loop", "break", "continue", "else", "mut", "ref",
    "where", "impl", "struct", "enum", "dyn", "await", "box",
})

# head = dotted/`::`-qualified receiver-or-module path; name = callee.
# Turbofish (`foo::<T>(`, `Foo::<T>::bar(`) tolerated: an optional
# `::<..>` may sit between callee and paren.
RUST_CALL_RE = re.compile(
    r"(?<![\w:.$])((?:[A-Za-z_]\w*(?:\.|::))*)"
    r"([A-Za-z_]\w*)\s*(?:::{0,2}<[^<>]*>)?\s*\(")
RUST_TYPED_LOCAL_RE = re.compile(
    r"\blet\s+(?:mut\s+)?([A-Za-z_]\w*)\s*:\s*([^\n=;]+)")
RUST_NEW_LOCAL_RE = re.compile(
    r"\blet\s+(?:mut\s+)?([A-Za-z_]\w*)\s*=\s*"
    r"([A-Za-z_]\w*)::(?:new|default)\s*(?::<[^<>]*>)?\s*\(")


def _import_target(fs: FileSym, name: str, ctx):
    """(file, fn) an import-bound name resolves to, else None.

    Named pairs first, then the file's own re-export table (`pub use
    x as name` rebinds to the origin through _mod_origin).
    """
    for t, nm in sorted(fs.from_imports):
        if nm == name and t in ctx.files and name in ctx.files[t].funcs:
            return (t, name)
    reexports = getattr(fs, "_rust_reexports", None) or {}
    for local in sorted(reexports):
        if local != name:
            continue
        org, orig_nm = _mod_origin(ctx, *reexports[local])
        if org and orig_nm in ctx.files[org].funcs:
            return (org, orig_nm)
    return None


def _module_dsts(mod_rel: str, name: str, ctx) -> list[tuple[str, str]]:
    """Namespace member call: the module file + its named re-exports."""
    out: list[tuple[str, str]] = []
    tfs = ctx.files.get(mod_rel)
    if tfs is None:
        return out
    if name in tfs.funcs:
        out.append((mod_rel, name))
    for t, nm in sorted(tfs.from_imports):
        if nm == name and t in ctx.files and nm in ctx.files[t].funcs:
            out.append((t, nm))
    return out


def _crate_root_rel(fs: FileSym, ctx) -> str:
    """Rel of the crate root file nearest fs.path (ctx.files truth)."""
    parts = fs.path.split("/")
    if len(parts) > 1 and parts[-2] == "bin":
        return fs.path  # cargo bin target: its own crate root (#284)
    parts = parts[:-1]
    for i in range(len(parts), -1, -1):
        d = "/".join(parts[:i])
        for nm in ("lib.rs", "main.rs"):
            cand = f"{d}/{nm}" if d else nm
            if cand in ctx.files:
                return cand
    return ""


def _ctx_mod_rel(fs: FileSym, ctx, segments: list[str]) -> str:
    """Resolve a `crate::`/`super::` module chain against ctx.files
    keys (scan-time ctx truth — no filesystem)."""
    if not segments:
        return ""
    head, rest = segments[0], segments[1:]
    if head == "crate":
        root = _crate_root_rel(fs, ctx)
        if not root:
            return ""
        if not rest:
            return root
        # root files declare sibling modules; a bin target (its own
        # root) declares <dir>/<stem>/*.rs — src/bin/x.rs -> src/bin/x/
        name = root.rsplit("/", 1)[-1]
        d = root.rsplit("/", 1)[0] if "/" in root else ""
        directory = d if name in _ROOT_FILES else (
            f"{d}/{name[:-3]}" if d else name[:-3])
    elif head == "super":
        d = fs.path.rsplit("/", 1)[0] if "/" in fs.path else ""
        directory = d.rsplit("/", 1)[0] if "/" in d else ""
    else:  # self:: and bare heads resolve through the file's own facts
        return ""
    cur = ""
    for seg in rest:
        hit = ""
        for c in (f"{directory}/{seg}.rs" if directory else f"{seg}.rs",
                  f"{directory}/{seg}/mod.rs" if directory else f"{seg}/mod.rs"):
            if c in ctx.files:
                hit = c
                break
        if not hit:
            return ""
        cur = hit
        directory = hit.rsplit("/", 1)[0] if hit.endswith("/mod.rs") else hit[:-3]
    return cur


def scan_file(fs: FileSym, ctx) -> None:
    if fs.ext != ".rs":
        return
    for _, fn in sorted(fs.funcs.items(), key=lambda kv: (kv[1].line, kv[0])):
        _scan_body_rust(fs, fn, ctx)


def _scan_body_rust(fs: FileSym, fn: Func, ctx) -> None:
    src_key = fn.key
    body = fn.body or ""
    var_types: dict[str, str] = dict(fs.members)
    for p, t in fn.params:
        if t:
            var_types[p] = t
    for m in RUST_TYPED_LOCAL_RE.finditer(body):
        var_types[m.group(1)] = m.group(2).strip()
    for m in RUST_NEW_LOCAL_RE.finditer(body):
        var_types[m.group(1)] = m.group(2)
    aliases: dict = getattr(fs, "_rust_aliases", None) or {}
    for m in RUST_CALL_RE.finditer(body):
        head, name = m.group(1), m.group(2)
        if name in RUST_NON_CALLS:
            continue
        if not head:  # bare call: local -> alias -> import-bound -> drop
            if name in fs.funcs:
                ctx._emit_call(src_key, fs.path, name)
                continue
            if name in aliases and aliases[name][0] in ctx.files \
                    and aliases[name][1] in ctx.files[aliases[name][0]].funcs:
                ctx._emit_call(src_key, aliases[name][0], aliases[name][1])
                continue
            got = _import_target(fs, name, ctx)
            if got:
                ctx._emit_call(src_key, got[0], got[1])
            continue
        if head.endswith("."):  # method call: recv.method(
            recv = head[:-1].rsplit(".", 1)[-1]
            if recv == "self":
                if name in fs.funcs:
                    ctx._emit_call(src_key, fs.path, name)
                else:
                    ctx.referenced_names.add(name)
                continue
            cls = var_types.get(recv, "")
            typ = _last_ident(cls) if cls else ""
            dst = ctx.class_map.get(typ, "") if typ else ""
            tfs = ctx.files.get(dst) if dst else None
            if tfs is not None and name in tfs.funcs:
                ctx._emit_call(src_key, dst, name)  # + override mirror
                continue
            # receiver's own file lacks it: trait-default dispatch — an
            # impl'd trait's file may carry the default method (the
            # _emit_call mirror covers sibling overrides)
            impl_traits = getattr(ctx, "_rust_impl_traits", {})
            dispatched = False
            if typ:
                for trait in sorted(impl_traits.get(typ, ())):
                    trel = ctx.class_map.get(trait, "")
                    ttfs = ctx.files.get(trel) if trel and trel != dst else None
                    if ttfs is not None and name in ttfs.funcs:
                        ctx._emit_call(src_key, trel, name)
                        dispatched = True
                        break
            if not dispatched:
                # untyped receiver or fully-unresolved type: dyn dispatch,
                # generic param, iterator chain — name-level alive
                ctx.referenced_names.add(name)
            continue
        # `::` path call: prefix segments + callee
        segs = head[:-2].split("::")
        head0 = segs[0]
        if head0 in ("crate", "super"):
            path_hit = _ctx_mod_rel(fs, ctx, segs)
            if path_hit:
                tfs = ctx.files.get(path_hit)
                if tfs is not None and name in tfs.funcs:
                    ctx._emit_call(src_key, path_hit, name)
                    continue
                got = _import_target(tfs, name, ctx) if tfs is not None else None
                if got:
                    ctx._emit_call(src_key, got[0], got[1])
                    continue
            ctx.referenced_names.add(name)  # unresolvable in-crate path
            continue
        if len(segs) == 1:
            if head0 in ("Self", "self"):
                if name in fs.funcs:
                    ctx._emit_call(src_key, fs.path, name)
                else:
                    ctx.referenced_names.add(name)
                continue
            mod_var = fs.module_vars.get(head0, "")
            if mod_var.startswith("module:"):
                hit = False
                for dst, fname in _module_dsts(mod_var[len("module:"):], name, ctx):
                    ctx._emit_call(src_key, dst, fname)
                    hit = True
                if not hit:
                    ctx.referenced_names.add(name)
                continue
            if head0 in ctx.class_map:
                dst = ctx.class_map[head0]
                tfs = ctx.files.get(dst)
                if tfs is not None and name in tfs.funcs:
                    ctx._emit_call(src_key, dst, name)
                else:
                    ctx.referenced_names.add(name)
                continue
            if head0[:1].isupper():
                continue  # external type path (Vec::new): no corpus fact
            ctx.referenced_names.add(name)  # unresolvable in-crate module
            continue
        # multi-segment non-crate path: external crate (serde_json::...)
        # or an in-crate spelling this file never declared — name-level
        # alive only when the head is lowercase (module-shaped)
        if head0[:1].islower():
            ctx.referenced_names.add(name)


# ---- sweeps (ctx-truth, post-harvest) ----------------------------------------------

def _mod_origin(ctx, target: str, nm: str, seen: frozenset = frozenset()):
    """Follow `pub use` chains to the origin definer: (rel, name)."""
    if not target or target in seen or target not in ctx.files:
        return ("", nm)
    tfs = ctx.files[target]
    if nm in tfs.funcs:
        return (target, nm)
    for local, (org, orig_nm) in sorted(getattr(tfs, "_rust_reexports", {}).items()):
        if local == nm:
            return _mod_origin(ctx, org, orig_nm, seen | {target})
    return ("", nm)


def rebind_reexports_sweep(ctx) -> None:
    """Rewrite re-exported bindings to the ORIGIN definer (the TS barrel
    analogue) and resolve crate-root pending imports (`use crate::x;`
    where x is a root re-export — unresolvable at parse by design)."""
    for rel in sorted(ctx.files):
        fs = ctx.files[rel]
        if getattr(fs, "ext", "") != ".rs":
            continue
        # 1. pending crate-root imports -> alias table + canonical pair
        for t, nm in sorted(getattr(fs, "_rust_pending_uses", None) or ()):
            tfs = ctx.files.get(t)
            if tfs is None:
                continue
            got = _import_target(tfs, nm, ctx)
            if got:
                als: dict = getattr(fs, "_rust_aliases", None) or {}
                if nm != got[1]:
                    als[nm] = got
                    fs._rust_aliases = als
                fs.from_imports.add(got)
        # 2. re-export chains collapse to the origin definer
        rebound = set()
        for t, nm in sorted(fs.from_imports):
            org, orig_nm = _mod_origin(ctx, t, nm)
            rebound.add((org or t, orig_nm if org else nm))
        fs.from_imports = rebound


def import_liveness_sweep(ctx) -> None:
    """Glob `use foo::*;` imports: the whole target module's funcs enter
    ctx.referenced (python plain-import semantics verbatim)."""
    for rel in sorted(ctx.files):
        fs = ctx.files[rel]
        if getattr(fs, "ext", "") != ".rs":
            continue
        for mod in sorted(fs.imported_modules):
            if mod in ctx.files:
                for other in ctx.files[mod].funcs.values():
                    ctx.referenced.add(other.key)


# registry choreography binds (langsep) — see extractors/python.py's
# _PASS_* block for the rationale (attribute dispatch is invisible to
# the module scan; the value-ref arm roots these binds).
_PASS_REBIND = rebind_reexports_sweep
_PASS_IMPORTS = import_liveness_sweep
_PASS_FACTS = harvest_facts
