"""Shared scanning mechanics for the language extractors.

Leaf module: pure text mechanics only — nothing here imports nav or
graph, and callers pass text/lines in (the language modules own file
loading). Language semantics (what counts as an entry, how a signature
parses, which names are dynamic) stay in the per-language modules;
their divergence IS the language layer, not duplication to flatten.
"""

from __future__ import annotations

import bisect
import os
import posixpath
import re

from typing import Callable, Iterable, Iterator

from extractors.model import FileSym, Func

# Python control keywords that look like calls in ``foo(...)`` position —
# the shared base of the module-call skip sets (python.py's harvest and
# graph.py's result-call resolution both filter these first).
PY_CONTROL_KEYWORDS = frozenset({
    "if", "for", "while", "elif", "return", "assert", "del", "print",
    "lambda", "not", "await", "with", "except", "raise", "yield",
})

# File-level dynamic-dispatch hints enabling the quoted-ident harvest and
# the dead-tier "review" gate. This is the GODOT dispatch surface — the
# gd scanner's own vocab (language-owned, issue #295). python.py spells
# its own py-idiom pattern; re-binding this one there made `.connect(`/`Callable(`
# flip py files dynamic for dispatch vocab python never uses.
DYNAMIC_HINT_RE = re.compile(
    r'\.call\(|\.call_deferred|Callable\(|has_method\(|\.connect\(|\.rpc\(|\.emit\('
)


def scan_indented_block(
    lines: list[str], start: int, base: int, indent_of: Callable[[str], int]
) -> tuple[str, int]:
    """Consume an indented block starting at ``lines[start]``.

    Blank lines, deeper-indented lines, and anything inside an open
    triple-quoted string belong to the block (a triple-quoted string can
    carry column-0 content that only LOOKS like a dedent). -> (body, end)
    where ``end`` indexes the first line NOT consumed.
    """
    body: list[str] = []
    in_tq = False
    j = start
    while j < len(lines):
        nxt = lines[j]
        if in_tq:
            body.append(nxt)
            if nxt.count('"""') % 2 == 1 or nxt.count("'''") % 2 == 1:
                in_tq = False
            j += 1
            continue
        if nxt.strip() == "":
            body.append(nxt)
            j += 1
            continue
        if indent_of(nxt) > base:
            body.append(nxt)
            if nxt.count('"""') % 2 == 1 or nxt.count("'''") % 2 == 1:
                in_tq = True
            j += 1
            continue
        break
    return "\n".join(body), j


def balanced_span(text: str, open_idx: int) -> int:
    """Index just past the balanced ``)`` for the ``(`` at open_idx.

    Registration-style macro calls carry nested parens and no trailing
    ``;`` — statement-scoped regex runs past the call, so scan depth.
    """
    depth = 0
    for i in range(open_idx, len(text)):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(text)


def merge_func(
    funcs: dict, path: str, name: str, line: int, body: str,
    params: list | None = None, ret: str | None = None,
) -> None:
    """Insert or same-name merge one Func into ``funcs``.

    A same-name redeclaration (inner classes legally re-declare a func;
    GDScript accessor blocks re-run) keeps the earliest line and
    concatenates bodies so call edges from BOTH survive; params/ret
    take the first non-empty statement of the two.
    """
    prev = funcs.get(name)
    if prev is None:
        funcs[name] = Func(
            path=path, name=name, line=line, body=body,
            params=[] if params is None else params,
            ret="" if ret is None else ret,
        )
    else:
        funcs[name] = Func(
            path=path, name=name, line=prev.line,
            body=prev.body + "\n" + body,
            params=prev.params if params is None else (prev.params or params),
            ret=prev.ret if ret is None else (prev.ret or ret),
        )


def entry_keys(fs: FileSym, names: Iterable[str]) -> Iterator[str]:
    """Entry-key shell: keys for the declared names that exist in fs.

    Callers own the language guard and the iteration order (sorting a
    set when they need a deterministic one).
    """
    for nm in names:
        fn = fs.funcs.get(nm)
        if fn is not None:
            yield fn.key


# ---- fn-key grammar (frozen contract) -----------------------------------------
# Node keys in Graph.edges/reverse/roots/referenced are "path::func" plus
# three pseudo-node spellings: "path::tscn" (scene file node),
# "path::SIGNAL:name" (signal node), "path::VAR:member" (member-write
# node); a bare "*::name" marks name-only references. The grammar is
# FROZEN — the viz template's fnKey/keyFile logic mirrors it, so any
# change is a both-sides contract (never one-sided). split_key's "first
# :: wins" is safe because extractor captures are identifier-shaped
# (never contain "::"). Home: common.py (language-neutral) since the
# langsep cutover; graph.py and bake consume via the package surface.
FN_KEY_SEP = "::"
TSCN_SUFFIX = "::tscn"
SIGNAL_PREFIX = "::SIGNAL:"
VAR_PREFIX = "::VAR:"

# ---- cross-language text mechanics --------------------------------------------
# Call/member shapes shared by more than one extractor's body scanner.
QUALIFIED_CALL_RE = re.compile(r"(?<![\w.$])([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*\(")
# receiver.member access that is NOT a call: member name lowercase-initial
# (vars), negative lookahead rejects optional-whitespace-then-paren
MEMBER_ACCESS_RE = re.compile(
    r"(?<![\w.$])([A-Za-z_]\w*)\s*\.\s*([a-z_]\w*)\b(?!\s*\()"
)
BARE_CALL_RE = re.compile(r"(?<![\w.$])([A-Za-z_]\w*)\s*\(")
# identifier-shaped token anywhere in raw corpus text (issue #20): the
# dead-tier mention-count pass counts these per file once, comments and
# string literals included — never a rescan per dead candidate
MENTION_TOKEN_RE = re.compile(r"[A-Za-z_]\w*")


def fn_key(rel: str, name: str) -> str:
    """Function-node key: repo-relative path + function name."""
    return f"{rel}{FN_KEY_SEP}{name}"


def fold_continuations(body: str) -> str:
    """Join physical lines whose parens/brackets are still open so a call
    split across lines becomes one logical line for regex scanning."""
    out: list[str] = []
    buf = ""
    depth = 0
    for line in body.splitlines():
        buf = line if not buf else f"{buf} {line.strip()}"
        depth += (
            line.count("(") - line.count(")")
            + line.count("[") - line.count("]")
            + line.count("{") - line.count("}")
        )
        if depth <= 0:
            out.append(buf)
            buf = ""
            depth = 0
    if buf:
        out.append(buf)
    return "\n".join(out)

# ---- tree-sitter front-end mechanics (shared by ts/js/rust, #302) ---------------
# Byte/child lookups the grammar front-ends share verbatim; the language
# knobs are DATA passed in (identifier node types, block child name,
# '$' in identifiers) — language semantics stay in the language modules.


def node_text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def node_line(node, line_starts: list[int]) -> int:
    return bisect.bisect_right(line_starts, node.start_byte)


def line_starts_of(src: bytes) -> list[int]:
    return [0] + [i + 1 for i, b in enumerate(src) if b == 0x0A]


def ident_child(node, src: bytes, ident_types) -> str:
    for ch in node.children:
        if ch.type in ident_types:
            return node_text(ch, src)
    return ""


def last_ident(text_val: str, dollar: bool = False) -> str:
    ids = re.findall(r"[A-Za-z_$][\w$]*" if dollar else r"[A-Za-z_]\w*", text_val)
    return ids[-1] if ids else ""


def body_block(node, src: bytes, block_type: str) -> str:
    for ch in node.children:
        if ch.type == block_type:
            return node_text(ch, src)
    return ""


def rel_of_target(abs_target, path, rel: str) -> str:
    """Repo-rel posix id of an absolute path, derived from the importing
    file's own (abs, rel) pair — parse never learns the walk root."""
    r = os.path.relpath(abs_target, path.parent).replace(os.sep, "/")
    d = posixpath.dirname(rel)
    return posixpath.normpath(posixpath.join(d, r)) if d else posixpath.normpath(r)


def receiver_env(fs: FileSym, fn: Func, *, module_vars: bool = True,
                 params: bool = True) -> tuple[str, str, dict[str, str]]:
    """Scan-body prologue shared by the body scanners: (src_key, body,
    receiver type env) — members (+ module vars per language), seeded
    with typed params where the grammar carries them."""
    var_types: dict[str, str] = dict(fs.members)
    if module_vars:
        var_types.update(fs.module_vars)
    if params:
        for p, t in fn.params:
            if t:
                var_types[p] = t
    return fn.key, fn.body or "", var_types


def make_import_liveness_sweep(exts, doc: str, *, from_imports: bool = False):
    """Build the registry's import_liveness_sweep(ctx) hook: every whole-
    module import (python `import x`, ts/js `import * as x`/side-effect,
    rust `use x::*`) keeps the target module's entire func surface alive
    — dynamic reachability is presumed. from_imports=True adds the python
    `from x import y` arm (only the imported name survives there). The
    per-language gate set and docstring stay at the call site."""

    def import_liveness_sweep(ctx) -> None:
        for rel in sorted(ctx.files):
            fs = ctx.files[rel]
            if fs.ext not in exts:
                continue
            for mod in sorted(fs.imported_modules):
                if mod in ctx.files:
                    for other in ctx.files[mod].funcs.values():
                        ctx.referenced.add(other.key)
            if from_imports:
                for mod, nm in sorted(fs.from_imports):
                    if mod in ctx.files and nm in ctx.files[mod].funcs:
                        ctx.referenced.add(f"{mod}{FN_KEY_SEP}{nm}")

    import_liveness_sweep.__doc__ = doc
    return import_liveness_sweep
