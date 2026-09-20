"""cAST doc shaping — the size-aware embed-document shaper.

The doc-shaping subsystem carved verbatim out of graph.py (issue #366):
the #76 statement-block chunk helpers (micro merge + monster split —
what sync_functions shapes on the fn layer) and the #229 per-file
shaper — file_doc, what navindex._rescan_locked embeds in place of the
raw file text. Pure doc construction: stdlib + the extractors package
+ the navconfig/navstore leaves, no graph import (graph re-exports the
pinned names as a facade); generated docs stay byte-identical across
the carve.
"""

from __future__ import annotations

import re
from pathlib import Path

import navconfig, navstore
from extractors import (
    FUNC_KEYWORD,
    add_class_ctx,
    registry_for,
)

# -- cAST-style size-aware doc chunking (issue #76) ---------------------------

# size thresholds, calibrated on the self-index corpus (docs/comparison.md):
# the median fn body is a handful of lines — getters/stubs and one-liners
# give no recall surface of their own, and monster fns (>2k chars, the
# model-context scale) drown their own signature under unrelated body
# tokens. The whole pass is gated by the nav config knob CHUNK_CAST
# (``chunk_cast``, default 0.0 = OFF: the fresh-store #141 bench A/B
# showed no lift — recall reads the file layer, the fn layer is
# invisible to it — so flipping the default needs an A/B that shows
# one): off, every fn keeps the single
# historical doc under fn_key(rel, name); on (1.0 = calibrated, other
# positives scale the thresholds), monsters split into statement-block
# chunk docs under fn_key(rel, "name#chunkN") — chunk ids ride the
# fn-key grammar, so split_key() still resolves the parent file.
MICRO_FN_CHARS = 220    # bodies at or below this merge into class context
MONSTER_FN_CHARS = 2000  # bodies above this split at statement block boundaries
CHUNK_DOC_CAP = 8000     # per-doc ceiling for class-merged and chunk docs


def _cast_scale() -> float:
    """The cAST chunking knob: nav's CHUNK_CAST (config ``chunk_cast``),
    read at call time so config_scope rebinding is honored. 0.0 = OFF —
    sync_functions emits the pre-#141 single doc per fn, byte-identical
    ids/docs/metadata; 1.0 = calibrated thresholds; other positive
    values scale MICRO_FN_CHARS/MONSTER_FN_CHARS. Negative clamps to
    0.0 (treated as off)."""
    return max(0.0, float(getattr(navconfig, "CHUNK_CAST", 0.0)))

_OPEN_RE = re.compile(r"[(\[{]$")



def _fn_body_start(fn: Func) -> int:
    """Line index of the first INDENTED statement inside a fn body — the
    body proper (past the signature, which may span continuation lines).
    Signature lines rest at or above the def line's indent; the first body
    statement is strictly deeper. Reports len(lines) when the body is
    empty, so callers slice harmlessly."""
    lines = fn.body.splitlines()
    base = len(lines[0]) - len(lines[0].lstrip(" \t")) if lines else 0
    for i in range(1, len(lines)):
        nxt = lines[i]
        if nxt.strip() and (len(nxt) - len(nxt.lstrip(" \t"))) > base:
            return i
    return len(lines)


def _chunk_line_offsets(body: str, mod=None) -> list[int]:
    """Line indices of top-level statement-block starts within a fn body
    PROPER (line 0 = the first statement, past the signature) — the cAST
    AST-boundary split points. base is the first statement's indent, so a
    later statement at that same indent (sequential or following a dedent)
    starts a new block, as do the language's dedent boundaries — brace
    closures, decorators, else/elif/catch — per the extractor's DEDENT_RE
    (issue #295: language-owned; none declared = only same-indent starts).
    Bracket continuations never count: the opener line ends with an open
    bracket, continuation lines sit deeper than the statement indent, and
    closing-bracket lines start with the closer. Triple-quoted string
    content is skipped when the language declares TRIPLE_QUOTES (a
    heredoc can mine column-0 lines that merely LOOK like dedents).
    Deterministic — a pure function of the body text + grammar."""
    dedent_re = getattr(mod, "DEDENT_RE", None)
    marks = tuple(getattr(mod, "TRIPLE_QUOTES", ()))
    lines = body.splitlines()
    if not lines:
        return []
    base = len(lines[0]) - len(lines[0].lstrip(" \t"))
    out: list[int] = []
    prev_end_open = False
    in_triple: str | None = None

    def _find_triple(ln: str) -> tuple[str | None, str | None]:
        """(opener, rest) — the first triple-quote mark on the line, if any."""
        for mark in marks:
            pos = ln.find(mark)
            if pos >= 0:
                return mark, ln[pos + len(mark) :]
        return None, None

    # the first statement may open a triple-quoted docstring itself
    t0_open, rest = _find_triple(lines[0])
    if t0_open and rest.count(t0_open) % 2 == 0:
        t0_close = rest.find(t0_open)
        if t0_close < 0:
            in_triple = t0_open
    for idx in range(1, len(lines)):
        ln = lines[idx]
        if in_triple:
            pos = ln.find(in_triple)
            if pos >= 0:
                in_triple = None
            continue
        stripped = ln.strip()
        if not stripped:
            prev_end_open = False
            continue
        if _OPEN_RE.search(ln.rstrip()):
            prev_end_open = True
            continue
        tm, rest = _find_triple(ln)
        if tm:
            if rest.count(tm) % 2 == 0:
                in_triple = None
            else:
                in_triple = tm
            prev_end_open = False
            continue
        if stripped.endswith((")", "]", "}")):
            prev_end_open = False
        ind = len(ln) - len(ln.lstrip(" \t"))
        if (
            (ind <= base or (dedent_re.match(ln) if dedent_re is not None else False))
            and not prev_end_open
            and not stripped.startswith((")", "]", "}"))
        ):
            out.append(idx)
        prev_end_open = False
    return out


def _first_stmt(text: str) -> int:
    """1-based line of the first non-blank line in ``text``."""
    for i, ln in enumerate(text.splitlines(), 1):
        if ln.strip():
            return i
    return 1


def _chunks(fn: Func, sig: str, blocks: list[int], scale: float = 1.0) -> list[tuple[str, int]]:
    """Statement-block chunk slices of a monster fn: (chunk_text, abs_line)
    per block, in body order. ``blocks`` are ABSOLUTE line indices of
    statement-block starts inside fn.body (including the body's first
    statement — callers compute them via _chunk_line_offsets on the body
    proper and re-base). The body's statement blocks are packed greedily
    into chunks sized to the retrieval cap — consecutive small blocks share
    a chunk (doc count stays near the pre-split value), a single oversized
    block bisects at its statement lines, hard-bisecting at the half-cap
    when the grammar sees no inner boundary (a giant literal). The
    signature rides EVERY chunk so each is a self-contained retrieval unit.
    Deterministic — a pure function of (fn, sig, blocks, scale)."""
    lines = fn.body.splitlines()
    n_lines = len(lines)
    points = sorted({b for b in blocks if 0 < b < n_lines} | {n_lines})
    cap = int(MONSTER_FN_CHARS * scale)  # scaled retrieval ceiling
    body_cap = cap - len(sig) - 2
    out: list[tuple[str, int]] = []

    def emit(start: int, end: int) -> None:
        """One chunk over body-line indices [start, end). An oversized
        segment (a single giant statement block, e.g. a 700-line loop body)
        cuts a PREFIX that fits under the cap — aligned back to the nearest
        statement boundary when one sits inside the safe prefix — then
        recurses on the remainder (cAST: oversized node with children ->
        split there instead). Prefix-fit keeps every chunk near-full, so
        the doc count stays ~chars/cap, not 2x that."""
        text = "\n".join(lines[start:end]).strip()
        if not text:
            return
        chunk = sig + "\n" + text
        first_abs = next((i for i in range(start, end) if lines[i].strip()), start)
        abs_line = fn.line + first_abs  # 1-based def line + 0-based body offset
        if len(chunk) <= cap:
            out.append((chunk, abs_line))
            return
        acc = len(sig) + 2
        cut = end
        for i in range(start, end):
            acc += len(lines[i]) + 1
            if acc >= cap:
                cut = i + 1
                break
        aligned = [b for b in blocks if start < b < cut]
        if aligned:
            cut = aligned[-1]
        if cut <= start or cut >= end:
            out.append((chunk, abs_line))  # unsplittable single line
            return
        emit(start, cut)
        emit(cut, end)

    prev = points[0]  # end of the last consumed statement block
    cut = points[0]   # start of the accumulating chunk segment
    acc = 0
    for i in range(1, len(points)):
        end = points[i]
        piece_chars = sum(len(ln) + 1 for ln in lines[prev:end])
        if acc and acc + piece_chars > body_cap:
            emit(cut, prev)  # flush the accumulated segment at a block boundary
            cut = prev
            acc = 0
        acc += piece_chars
        if end >= n_lines:
            emit(cut, end)
        prev = end
    return out


def _chunk_intro(fn: Func, mod=None) -> str:
    """The fn's big-picture title line, if one opens the body: the first
    line of a docstring or a leading comment — per the language's grammar
    (issue #295): TRIPLE_QUOTES docstrings, COMMENT_PREFIXES comments
    (`#` for py/gd, `//` — and `///` — for brace languages). Kept short;
    anything else (a real first statement) is not an intro."""
    prefixes = tuple(getattr(mod, "COMMENT_PREFIXES", ()))
    triples = tuple(getattr(mod, "TRIPLE_QUOTES", ()))
    lines = fn.body.splitlines()
    if len(lines) < 2:
        return ""
    first = lines[1].strip()
    if triples and first.startswith(triples):
        return first.strip("'\" ")[:48]
    if prefixes and first.startswith(prefixes) and len(first) <= 96:
        return first.lstrip("".join(sorted(set("".join(prefixes))))).strip()[:48]
    return ""


def _chunk_docs(fn: Func, sig: str, blocks: list[int], scale: float = 1.0,
                mod=None) -> list[tuple[str, int]]:
    """(doc, line) pairs for a monster fn's statement-block chunks. A body
    that opens with a docstring/title comment keeps that intro on EVERY
    later chunk (`# <first line>`), so prose retrieval does not lose the
    big-picture orientation (cAST keeps signature-first docs; a leading
    intro is part of the signature surface)."""
    intro = _chunk_intro(fn, mod)
    if not intro:
        return _chunks(fn, sig, blocks, scale)
    chunks = _chunks(fn, sig, blocks, scale)
    out = []
    for i, (text, line_no) in enumerate(chunks, 1):
        if i > 1:
            text = sig + "\n# " + intro + "\n" + text[len(sig) :].lstrip("\n")
        out.append((text, line_no))
    return out


def _fn_doc(fs: FileSym, fn: Func, sig: str) -> str:
    """The cAST size-aware fn document (issue #76) for one (already-shaped)
    fn: ``class_ctx`` folks fold in their merged member bodies, ``chunk``
    entries are pre-built split docs, everything else keeps the historical
    signature-first raw doc. The 6000-char raw ceiling and the class/chunk
    caps preserve the pre-chunking recall surface (docs/comparison.md
    pinned the fps/recall wins; the bench golden set targets these fns)."""
    head = f"{fs.path} :: func {fn.name}({sig}){(' -> ' + fn.ret) if fn.ret else ''}"
    if fn.kind == "class_ctx":
        parts = [head]
        for mname, _line, mbody in fn.members:
            parts.append("-- " + mname + " --")
            parts.append(mbody)
        return "\n".join(parts)[:CHUNK_DOC_CAP]
    if fn.kind == "chunk":
        return fn.body[:CHUNK_DOC_CAP]
    return head + "\n" + fn.body[:6000]


def _is_micro(fn: Func, scale: float = 1.0) -> bool:
    """cAST micro-fn test (issue #76): trivial getters/stubs/one-liners —
    body past the signature at or under MICRO_FN_CHARS. When the first
    line is NOT a signature (a raw GDScript body fragment already past
    the header), measure the whole fragment — long-param masking only
    applies to bodies that actually carry their signature line."""
    lines = fn.body.splitlines()
    if not lines:
        return False
    nb = _fn_body_start(fn)
    if not lines[0].strip().endswith(":") and not lines[0].lstrip().startswith(FUNC_KEYWORD):
        body = fn.body  # raw fragment: no signature line to strip
    else:
        body = "\n".join(lines[nb:]) if nb < len(lines) else ""
    return 0 < len(body) <= int(MICRO_FN_CHARS * scale)


def _micro_groups(funcs: dict[str, Func], scale: float = 1.0) -> dict[str, list[Func]]:
    """Pure micro-fn grouping shared by the fn layer (#76) and the file
    layer (#229): each micro fn (see _is_micro) folds into the nearest
    NON-micro fn above it in source order — the class body document
    that owns its neighborhood. A micro fn above no non-micro fn stays
    standalone (nothing to merge into). Deterministic — a pure
    function of the parsed funcs."""
    order = sorted(funcs.items(), key=lambda kv: (kv[1].line, kv[0]))
    groups: dict[str, list[Func]] = {}
    for name, fn in order:
        if not _is_micro(fn, scale):
            continue
        above = [
            cn for cn, cfn in order
            if not _is_micro(cfn, scale) and cfn.line < fn.line
        ]
        if not above:
            continue  # no fn above: nothing to merge into
        groups.setdefault(above[-1], []).append(fn)
    return groups


def _overlay_class_context(funcs: dict[str, Func], fs: FileSym, scale: float = 1.0) -> None:
    """cAST micro-fn merge (issue #76): fold a class file's micro-functions
    (getters/stubs/one-liners, incl. GDScript property accessors —
    ``_set_x``/``_get_x``) into the class method doc that owns their
    neighborhood: the nearest NON-micro method above them (the class body
    document in source order). The merge is carried on the carrier fn's
    Func (kind='class_ctx'), so the fn index still emits one entry per fn —
    retrieval, dead-code, explore and the mwires roster are untouched; the
    extra context only widens the vector surface. When the whole class is
    micro (no non-micro method exists to carry the fold), fns stay
    standalone — there is nothing to merge into. Modules without a
    class_name (python modules, tool scripts) are never merged: a module
    has no class document."""
    if not fs.class_name:
        return
    groups = _micro_groups(funcs, scale)
    for carrier in sorted(groups, key=lambda c: (funcs[c].line, c)):
        add_class_ctx(funcs, carrier, groups[carrier])


def _chunked_docs(fs: FileSym, fn: Func, sig: str, scale: float = 1.0) -> list[tuple[str, int, str]]:
    """The cAST size-aware docs for ONE fn: (doc, line, key). Regression —
    unchanged fns keep a single signature-first doc under ``name``; a
    monster fn (>MONSTER_FN_CHARS * scale) splits into statement-block
    chunk docs under ``name#chunkN`` (docs grow < 1/fn — the win
    condition); a class_ctx fn (pre-folded by _overlay_class_context)
    emits one wider doc. Never mutates fs.funcs — chroma ids are the only
    surface that grows. scale <= 0 (knob off) keeps the single historical
    doc — the byte-identical pre-#141 recall surface."""
    if scale <= 0.0:
        return [(_fn_doc(fs, fn, sig), fn.line, fn.name)]
    if fn.kind == "class_ctx":
        return [(_fn_doc(fs, fn, sig), fn.line, fn.name)]
    if fn.kind == "chunk":
        return [(_fn_doc(fs, fn, sig), fn.line, fn.name)]
    body = fn.body
    if len(body) <= int(MONSTER_FN_CHARS * scale):
        return [(_fn_doc(fs, fn, sig), fn.line, fn.name)]
    # monster: split at statement blocks, driving doc ids
    lines = body.splitlines()
    nb = _fn_body_start(fn)
    body_proper = "\n".join(lines[nb:]) if nb < len(lines) else ""
    if not body_proper:
        return [(_fn_doc(fs, fn, sig), fn.line, fn.name)]
    mod = registry_for(fs.ext)
    offs = _chunk_line_offsets(body_proper, mod)
    blocks = [nb] + [nb + i for i in offs]  # absolute (first stmt included)
    if len(blocks) < 2:
        # monster with a single giant statement: hard-bisect the body
        blocks = []
        nlines = len(lines)
        seg = nb
        step = max(1, (nlines - nb) // max(2, len(body) // int(MONSTER_FN_CHARS * scale)))
        while seg < nlines - 1:
            seg = min(nlines - 1, seg + step)
            blocks.append(seg)
    docs: list[tuple[str, int, str]] = []
    for i, (chunk, aline) in enumerate(_chunk_docs(fn, sig, blocks, scale, mod), 1):
        docs.append((chunk, aline, f"{fn.name}#chunk{i}"))
    return docs


def _chunk_plan(fs: FileSym, funcs: dict[str, Func], scale: float = 1.0) -> None:
    """Shape the fn docs for one file BEFORE the sync loop: fold micro fns
    into class context (mutating Func.kind/members), so the loop's
    per-fn _chunked_docs sees stable shapes. Deterministic — pure function
    of the parsed FileSym + funcs."""
    _overlay_class_context(funcs, fs, scale)


# -- cAST file-doc shaping (issue #229) ---------------------------------------

# The #76 chunking lifted to the file layer — where recall actually reads
# it. The fn collection ("-fns") is invisible to recall.search (the #141
# fresh-store A/B showed no lift), so the size-aware shape earns its keep
# on the docs nav embeds per FILE: recall's vector side queries exactly
# that collection. Self-index distribution that calibrates the reuse:
# 12/58 files exceed navstore.MAX_EMBED_CHARS (30k) — their bytes past the
# truncation are invisible to the vector side today (viz.py 462k = 6.5%
# visible; graph.py/nav.py/server.py all >50k); fn bodies p50=547 chars,
# micro (<=220) = 28%, monster (>2000) = 15% — the #76 thresholds already
FILE_DOC_REV = 3        # shaper semantics version — bump whenever the
                        # shaper changes docs for the same input bytes;
                        # nav's doc_shape stamp rides it so shape-lineaged
                        # stores re-embed loudly instead of serving stale
                        # vectors under sha-gating (#220 law, doc side).
                        # rev 2: the "# imports:" head line (#229 extension)
                        # rev 3: language-owned intros (#295) — `//`/`///`
                        # comment blocks become real intros for brace
                        # languages, `#` lines in those files stop
                        # misreading as intros, per-language dedent
                        # boundaries refine monster splits
FILE_SYMBOLS_CAP = 1200  # symbol-surface line budget (chars)
FILE_IMPORTS_CAP = 400   # import-surface line budget (chars) — the file's
                         # resolved imports ride the doc head (cAST's
                         # contextual-awareness gap; RepoCoder context
                         # augmentation), ~1.3% of the 30k embed budget
FILE_INTRO_CAP = 400     # module docstring / leading-comment budget

_ENC_RE = re.compile(r"^#.*?coding[:=]")  # PEP 263 coding line: py-owned


def _file_intro(text: str, mod=None) -> str:
    """The file's big-picture opener (cAST keeps intros on chunks; the
    file analog): the leading module docstring or comment block, past
    shebang/encoding lines. Grammar is language-owned (issue #295):
    TRIPLE_QUOTES docstrings, COMMENT_PREFIXES comment blocks (`#` for
    py/gd, `//` — and `///` — for brace languages; a `#include` line in
    a cpp file is preprocessor, not prose). The shebang skip stays
    universal; the PEP 263 coding-line skip applies where the language
    declares ENCODING_RE. '' when the file opens with code. A language
    declaring no grammar yields no intro. Deterministic."""
    prefixes = tuple(getattr(mod, "COMMENT_PREFIXES", ()))
    triples = tuple(getattr(mod, "TRIPLE_QUOTES", ()))
    enc_re = getattr(mod, "ENCODING_RE", None)
    lines = text.splitlines()
    i = 0
    while i < len(lines) and (
        not lines[i].strip()
        or lines[i].startswith("#!")
        or (enc_re is not None and enc_re.match(lines[i]))
    ):
        i += 1
    if i >= len(lines):
        return ""
    out: list[str] = []
    first = lines[i].lstrip()
    if triples and first.startswith(triples):
        mark = next(m for m in triples if first.startswith(m))
        rest = first[len(mark):]
        close = rest.find(mark)
        if close >= 0:
            out.append(rest[:close])
        else:
            out.append(rest)
            i += 1
            while i < len(lines):
                ln = lines[i]
                pos = ln.find(mark)
                if pos >= 0:
                    out.append(ln[:pos])
                    break
                out.append(ln)
                i += 1
    elif prefixes and first.startswith(prefixes):
        strip_chars = "".join(sorted(set("".join(prefixes))))
        while i < len(lines) and lines[i].lstrip().startswith(prefixes):
            stripped = lines[i].lstrip().lstrip(strip_chars).strip()
            if stripped:
                out.append(stripped)
            i += 1
    return "\n".join(ln for ln in out if ln.strip())[:FILE_INTRO_CAP]


def _fn_sections(fs: FileSym, fn: Func, scale: float = 1.0) -> list[str]:
    """Signature-first doc sections for ONE fn at file scale (#229): a
    normal fn is its verbatim body (bodies open at the def line —
    signature-first by construction); a monster (> MONSTER_FN_CHARS *
    scale) splits at statement-block boundaries with the FULL signature
    lines on every chunk plus the body intro — the #76 `_chunked_docs`
    mechanics reused verbatim, only the signature surface differs (the
    fn layer's chunks carry the params-only sig; file sections carry
    the whole def header). Deterministic — pure function of (fn, scale)."""
    body = fn.body
    if len(body) <= int(MONSTER_FN_CHARS * scale):
        return [body]
    lines = body.splitlines()
    nb = _fn_body_start(fn)
    if nb >= len(lines):
        return [body]  # no body proper past the signature: nothing to split
    sig = "\n".join(lines[:nb])
    return [doc for doc, _line, _key in _chunked_docs(fs, fn, sig, scale)]


def file_doc(path: Path, rel: str, text: str, scale: float = 1.0) -> str:
    """The cAST-shaped embed document for one file (issue #229) — what
    navindex._rescan_locked embeds and stores in place of the raw file text.
    Size-aware, signature-first (cAST 2025; RepoBench):
    - head: path, class/extends, the full symbol surface (every fn name
      rides the doc, capped), the resolved import surface (`# imports:`,
      capped — cAST's contextual-awareness gap), and the module intro;
    - micro fns merge into the nearest non-micro fn above them — the
      class-context fold (`_micro_groups`), members as `-- name --`
      banners under their carrier;
    - monster fns split at statement-block boundaries, every chunk
      signature-first (`_fn_sections`);
    - sections flatten by (chunk index, source line): chunk 1 of EVERY
      fn embeds before chunk 2 of ANY fn, so a 460k file no longer
      buries its later fns under the 30k embed truncation;
    - assembly stays under navstore.MAX_EMBED_CHARS — the embed-side
      truncation never clips shaped docs blind.
    Fallbacks keep the raw text verbatim: scale <= 0 (knob off — the
    byte-identical pre-#229 surface), no parser for the suffix, or a
    parse with no fns (nothing structural to shape). Deterministic —
    a pure function of (file bytes, rel, scale); never mutates the
    parsed FileSym, so BM25F's graph view is untouched."""
    if scale <= 0.0:
        return text
    mod = registry_for(path.suffix)
    if mod is None:
        return text
    fs = mod.parse(path, rel)
    if not fs.funcs:
        return text

    head = [f"# {rel}"]
    if fs.class_name:
        head.append(f"# class {fs.class_name}"
                    + (f" extends {fs.extends}" if fs.extends else ""))
    syms = fs.surface
    line = "# symbols: " + " ".join(syms)
    if len(line) > FILE_SYMBOLS_CAP:
        keep: list[str] = []
        used = len("# symbols: ")
        room = FILE_SYMBOLS_CAP - 8  # headroom for the (+N) tail
        for name in syms:
            if used + len(name) + 1 > room:
                break
            keep.append(name)
            used += len(name) + 1
        line = "# symbols: " + " ".join(keep) + f" (+{len(syms) - len(keep)})"
    head.append(line)
    # whole-module imports (side-effect/namespace/dynamic) plus the module
    # side of named imports — the doc head mirrors what the file pulls in,
    # not the liveness contract (imported_modules alone stays whole-module-
    # alive facts; from_imports carry the named bindings)
    imps = sorted(fs.imported_modules | {m for m, _ in fs.from_imports})
    if imps:
        line = "# imports: " + " ".join(imps)
        if len(line) > FILE_IMPORTS_CAP:
            keep = []
            used = len("# imports: ")
            room = FILE_IMPORTS_CAP - 8  # headroom for the (+N) tail
            for imp in imps:  # NOT `mod` — that name is the extractor
                # module used by _file_intro below (issue #356)
                if used + len(imp) + 1 > room:
                    break
                keep.append(imp)
                used += len(imp) + 1
            line = "# imports: " + " ".join(keep) + f" (+{len(imps) - len(keep)})"
        head.append(line)
    intro = _file_intro(text, mod)
    if intro:
        head.append(intro)
    doc_head = "\n".join(head)

    groups = _micro_groups(fs.funcs, scale)
    folded = {fn.name for micros in groups.values() for fn in micros}
    per_fn: list[list[str]] = []
    for name, fn in sorted(fs.funcs.items(), key=lambda kv: (kv[1].line, kv[0])):
        if name in folded:
            continue
        sections = _fn_sections(fs, fn, scale)
        micros = groups.get(name)
        if micros:
            sections[-1] += "\n" + "\n".join(
                f"-- {m.name} --\n{m.body}" for m in micros
            )
        per_fn.append(sections)
    budget = int(navstore.MAX_EMBED_CHARS)
    parts: list[str] = [doc_head]
    used = len(doc_head)
    for idx in range(max((len(s) for s in per_fn), default=0)):
        for sections in per_fn:
            if idx >= len(sections):
                continue
            part = sections[idx]
            if used + len(part) + 1 > budget:
                continue
            parts.append(part)
            used += len(part) + 1
    return "\n".join(parts)
