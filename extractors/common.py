"""Shared scanning mechanics for the language extractors.

Leaf module: pure text mechanics only — nothing here imports nav or
graph, and callers pass text/lines in (the language modules own file
loading). Language semantics (what counts as an entry, how a signature
parses, which names are dynamic) stay in the per-language modules;
their divergence IS the language layer, not duplication to flatten.
"""

from __future__ import annotations

from typing import Callable, Iterable, Iterator

from extractors.model import FileSym, Func

# Python control keywords that look like calls in ``foo(...)`` position —
# the shared base of the module-call skip sets (python.py's harvest and
# graph.py's result-call resolution both filter these first).
PY_CONTROL_KEYWORDS = frozenset({
    "if", "for", "while", "elif", "return", "assert", "del", "print",
    "lambda", "not", "await", "with", "except", "raise", "yield",
})


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
