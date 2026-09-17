"""TypeScript extractor: tree-sitter-typescript front-end (issue #245).

Strategy: one parse per file via the pinned grammar pair —
``tree-sitter==0.26.0`` + ``tree-sitter-typescript==0.23.2`` — with the
grammar split the wheels expose: `.ts`/`.mts`/`.cts` parse under
``language_typescript()`` (the TS grammar rejects JSX with ERROR nodes),
`.tsx` under ``language_tsx()``. Defs come from one ordered S-expression
Query per grammar; import/export statements get a structural walk
(specifier pairing needs field access a query cannot express).

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

import bisect
import json
import os
import posixpath
import re
import sys
import weakref
from collections.abc import Iterator
from pathlib import Path

from tree_sitter import Language, Node, Parser, Query, QueryCursor

import tree_sitter_typescript as _tst

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
    ids = re.findall(r"[A-Za-z_$][\w$]*", text_val)
    return ids[-1] if ids else ""


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


def _body_block(node, src: bytes) -> str:
    for ch in node.children:
        if ch.type == "statement_block":
            return _text(ch, src)
    return ""


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


def _load_tsconfig(cfg: Path) -> tuple[dict[str, list[str]], Path] | None:
    """(alias map find->replacements, base dir) or None when unreadable.

    One `extends` level, child wins; baseUrl/paths resolve against the
    tsconfig's own directory. An unreadable extends target (package
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
                merged = dict(parent)
                merged.update(data)
                base = merged
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


# ---- module-specifier resolution (§1.4) -----------------------------------------

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


def _abs_candidates(abs_base: Path) -> list[Path]:
    """Filesystem candidates for an absolute base, in §1.4 order."""
    out = [abs_base]
    s = str(abs_base)
    for e in _RESOLVE_SUFFIXES:
        out.append(Path(s + e))
    for idx in ("index.ts", "index.tsx", "index.js", "index.jsx"):
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


def _rel_of_target(abs_target: Path, path: Path, rel: str) -> str:
    """Repo-rel posix id of an absolute path, derived from the importing
    file's own (abs, rel) pair — parse never learns the walk root."""
    r = os.path.relpath(abs_target, path.parent).replace(os.sep, "/")
    d = posixpath.dirname(rel)
    return posixpath.normpath(posixpath.join(d, r)) if d else posixpath.normpath(r)


def _is_specifier_relative(spec: str) -> bool:
    return spec.startswith(("./", "../")) or spec in (".", "..")


def _resolve_spec(spec: str, path: Path, rel: str) -> str:
    """Repo-rel path of an import specifier from file `rel`, or ''.

    Relative specifiers resolve against dirname(file); alias-mapped ones
    through the project tsconfig (longest prefix); bare package names are
    external and record nothing. First filesystem-present candidate wins.
    """
    if not spec:
        return ""
    abs_bases: list[Path] = []
    if _is_specifier_relative(spec):
        abs_bases.append(Path(os.path.normpath(str(path.parent / spec))))
    else:
        cfg = _find_tsconfig(path.parent)
        if cfg is None:
            return ""  # no tsconfig: alias-shaped specifiers are indistinguishable
        loaded = _load_tsconfig(cfg)   # from package names — external, silent
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
            # names an indexed ES-family file — ts or js (#277 opened
            # the js direction; JS_EXTS ships here for both modules,
            # #293, so the family suffix sets keep one spelling)
            if (abs_cand == abs_base
                    and abs_cand.suffix.lower() not in TS_EXTS | JS_EXTS):
                continue
            return _rel_of_target(abs_cand, path, rel)
    return ""


# ---- import/export structural walk ----------------------------------------------

_DYNAMIC_IMPORT_RE = re.compile(r"""\bimport\s*\(\s*(["'])([^"'`]+)\1""")
_REQUIRE_RE = re.compile(r"""(?<![\w.$])require\s*\(\s*(["'])([^"'`]+)\1""")
_MODULE_INIT_CALL_RE = re.compile(r"\s*([A-Za-z_$][\w$]*)\s*\(")


def _string_of(node, src: bytes) -> str:
    t = _text(node, src)
    return t[1:-1] if len(t) >= 2 and t[0] in "\"'`" else t


def _walk_modules(root, src: bytes, path: Path, rel: str, fs: FileSym,
                  line_starts: list[int]) -> None:
    """Import/export walk + module-scope side effects + class fields.

    Function bodies are skipped: their calls are the scan's job, and no
    import/export statement is legal inside one.
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
                         "function_expression", "arrow_function", "method_definition",
                         "function_signature", "method_signature"):
            continue
        if mod and node.type in ("lexical_declaration", "variable_declaration"):
            for ch in node.children:
                if ch.type == "variable_declarator":
                    _take_module_init(ch, src, fs)
        if mod and node.type == "expression_statement":
            m = _MODULE_INIT_CALL_RE.match(_text(node, src))
            if m and m.group(1) not in TS_NON_CALLS:
                fs.init_calls.add(m.group(1))
        if node.type == "public_field_definition":
            _take_field(node, src, fs)
        if node.type in ("internal_module", "module"):
            nm = _ident_child(node, src)
            if nm:
                fs.module_vars.setdefault(nm, "module:" + rel)
        for ch in node.children:
            stack.append((ch, mod))
    # dynamic import()/require() literal targets ride imported_modules —
    # the import-liveness sweep turns that into whole-module referenced
    # facts. Non-literal template/variable imports add nothing.
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
                    fs._ts_exported.add(nm)
            elif ch.type in ("lexical_declaration", "variable_declaration"):
                for d in ch.children:
                    if d.type == "variable_declarator":
                        dn = _ident_child(d, src)
                        if dn:
                            fs._ts_exported.add(dn)
            elif ch.type in ("class", "abstract_class"):
                nm = _ident_child(ch, src)
                if nm:
                    fs._ts_exported.add(nm)
            elif ch.type == "export_clause":
                for sp in ch.children:
                    if sp.type != "export_specifier":
                        continue
                    names = [c for c in sp.children if c.type == "identifier"]
                    if names:
                        fs._ts_exported.add(_text(names[-1], src))
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
                fs._ts_exported.add(_ident_child(ch, src))  # named default
                return  # the def queries own it
            body = _body_block(ch, src) or _text(ch, src)
        elif ch.type in ("arrow_function", "function_expression",
                         "class", "abstract_class"):
            body = _body_block(ch, src) or _text(ch, src)
        elif ch.type == "identifier":
            fs._ts_exported.add(_text(ch, src))  # export default App;
            return
        else:
            continue
        fs.funcs["default"] = Func(path=fs.path, name="default",
                                   line=_line(node, line_starts), body=body)
        return


def _take_module_init(declarator, src: bytes, fs: FileSym) -> None:
    """`const x = makeThing()` at module scope -> callee into init_calls."""
    nm = _ident_child(declarator, src)
    if not nm:
        return
    for ch in declarator.children:
        if ch.type == "call_expression":
            callee = _find_first_ident(ch, src)
            if callee and callee not in TS_NON_CALLS and callee not in ("require", "import"):
                fs.init_calls.add(callee)


def _take_field(node, src: bytes, fs: FileSym) -> None:
    """Class fields: typed members + initializer-call facts."""
    nm = ""
    for ch in node.children:
        if ch.type == "property_identifier" and not nm:
            nm = _text(ch, src)
        elif ch.type == "type_annotation" and nm:
            fs.members.setdefault(nm, _type_text(ch, src))
        elif ch.type == "call_expression" and nm:
            callee = _find_first_ident(ch, src)
            if callee and callee not in TS_NON_CALLS:
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
    """Registry entry point: one FileSym per .ts/.tsx/.mts/.cts file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    src = text.encode("utf-8")
    ext = path.suffix.lower()
    fs = FileSym(path=rel, ext=ext)
    root = _PARSERS.get(ext, _PARSERS[".ts"]).parse(src).root_node
    line_starts = [0] + [i + 1 for i, b in enumerate(src) if b == 0x0A]
    caps = QueryCursor(_TSX_QUERY if ext == ".tsx" else _TS_QUERY).captures(root)

    def bytewise(*keys: str) -> list:
        nodes = []
        for k in keys:
            nodes.extend(caps.get(k, ()))
        return sorted(nodes, key=lambda n: n.start_byte)

    # -- overload-aware def collection (cpp law: first-in-file wins) -------------
    heads: dict[str, int] = {}    # name -> first declaration line (any kind)
    impls: dict[str, tuple] = {}  # name -> (line, body, params, ret)
    for node in bytewise("sig.def", "msig.def"):
        # signature-only heads: overload fronts and ambient `declare` bodies.
        # They run BEFORE the def loop so a body-less front at an earlier
        # line owns the collapsed Func's line (overload law).
        nm = _names_in(node, caps, src, ("sig", "msig"))
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
            params, ret = _signature(sig_node, src)
            impls.setdefault(nm, (line, body, params, ret))
    for nm in sorted(impls):
        line, body, params, ret = impls[nm]
        fs.funcs.setdefault(nm, Func(path=rel, name=nm, line=heads.get(nm, line),
                                     body=body, params=params, ret=ret))

    # -- classes: first class with a body names the file; every class's
    # -- extends is recorded for the inheritance fill (class_map) -------
    classes = bytewise("cls.def", "acls.def")
    ext_nodes = bytewise("ext")
    named = False
    ts_classes: list[tuple[str, str]] = []
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
        ts_classes.append((nm, ext_nm))
        if not named:
            fs.class_name = nm
            named = True
        if not fs.extends and ext_nm:
            fs.extends = ext_nm
    fs._ts_classes = ts_classes  # consumed by _fill_inheritance (class_map)

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

    # -- JSX prop donations (tsx grammar only) ------------------------------------
    for node in bytewise("jprop"):
        fs.arg_refs.add(_text(node, src))

    fs._ts_exported = set()
    _walk_modules(root, src, path, rel, fs, line_starts)

    # -- exported PascalCase fn/class components are UI entries (§1.7) ----------
    for nm in sorted(fs._ts_exported):
        if nm[:1].isupper() and (nm in fs.funcs or nm == fs.class_name):
            fs.entry_hints.add(nm)
    return fs


# ---- hooks (langsep REQUIRED surface) -------------------------------------------

from extractors.common import entry_keys  # noqa: E402  (late: package cycle)


MENTION_FLOOR = 2
DYNAMIC_HINT = re.compile(r"\bReflect\.|\bProxy\(|\beval\(")

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


# ---- entry rules (§1.7) ------------------------------------------------------------

_TEST_PATH_RE = re.compile(r"(?:^|/)__tests__/|\.(?:test|spec)\.(?:ts|tsx|mts|cts)$")
_CONFIG_NAME_RE = re.compile(
    r"^(?:next\.config\.|vite\.config\.|tailwind\.config\.|app\.config\.)"
    r"[^/]*$|^app\.json$")
_PKG_SEEN: "weakref.WeakKeyDictionary[object, set]" = weakref.WeakKeyDictionary()


def _entry_tests(fs: FileSym, ctx) -> Iterator[str]:
    if fs.ext not in TS_EXTS:
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
    """Resolve a package entry specifier against ctx.files (§1.4 order)."""
    if not isinstance(spec, str) or not spec:
        return ""
    base = posixpath.normpath(posixpath.join(pkg_dir, spec))
    for cand in (base, base + ".ts", base + ".tsx", base + ".mts", base + ".cts",
                 base + "/index.ts", base + "/index.tsx"):
        if cand in ctx.files:
            return cand
    for src_e, dsts in _EXT_REWRITES:
        if base.endswith(src_e):
            for d in dsts:
                if base[:-len(src_e)] + d in ctx.files:
                    return base[:-len(src_e)] + d
    return ""


def _entry_package(fs: FileSym, ctx) -> Iterator[str]:
    if fs.ext not in TS_EXTS:
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
    # trees with no package.json anywhere must not re-walk per ES-family
    # file (the seen-dict is shared with js.py, #293)
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
                # that no walked file matches land on src/index.ts[x]
                for cand in (posixpath.normpath(pkg_dir + "/src/index.ts"),
                             posixpath.normpath(pkg_dir + "/src/index.tsx")):
                    if cand in ctx.files:
                        entry = cand
                        break
            if entry and entry in ctx.files:
                yield from entry_keys(ctx.files[entry], sorted(ctx.files[entry].funcs))


def _entry_components(fs: FileSym, ctx) -> Iterator[str]:
    """Decorated methods and exported PascalCase fn/class components
    (React convention) — a component is a UI entry, so dead tiers
    never false-flag one."""
    if fs.ext not in TS_EXTS:
        return
    yield from entry_keys(fs, sorted(fs.entry_hints))


ENTRY_RULES = (_entry_tests, _entry_package, _entry_components)


# ---- facts + call scanning (§1.5) ---------------------------------------------------

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


def harvest_facts(fs: FileSym, ctx) -> None:
    if fs.ext not in TS_EXTS:
        return
    for nm in sorted(fs.name_literals):
        ctx.referenced_names.add(nm)
    for nm in sorted(fs.init_calls):
        ctx.referenced_names.add(nm)
    for nm in sorted(fs.arg_refs):
        if nm in fs.funcs:
            ctx.referenced.add(fs.funcs[nm].key)


def _import_target(fs: FileSym, name: str, ctx) -> tuple[str, str] | None:
    """(file, fn) an import-bound name resolves to, else None.

    Default bindings (consts) and named pairs both try the binding's own
    name first, then the module default export.
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


_WARNED_TSX_READ = False


def _jsx_sites(path: Path, fs: FileSym) -> list[tuple[str, int]]:
    """Capitalized JSX element names + their lines (tsx grammar only)."""
    global _WARNED_TSX_READ
    try:
        src = path.read_bytes()
    except OSError:
        # scan_file runs after parse read the same file fine; a failure
        # here means it vanished mid-build — loud once, never per file
        if not _WARNED_TSX_READ:
            _WARNED_TSX_READ = True
            print(f"neuronav: tsx scan re-read failed at {path}: "
                  "sites skipped for this file", file=sys.stderr)
        return []
    caps = QueryCursor(_TSX_QUERY).captures(_PARSERS[".tsx"].parse(src).root_node)
    line_starts = [0] + [i + 1 for i, b in enumerate(src) if b == 0x0A]
    sites = [(name, _line(node, line_starts))
             for node in sorted(caps.get("jsxcomp", ()), key=lambda n: n.start_byte)
             for name in (_text(node, src),) if name[:1].isupper()]
    return sites


def scan_file(fs: FileSym, ctx) -> None:
    if fs.ext not in TS_EXTS:
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

    if fs.ext == ".tsx":
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
        _scan_body_ts(fs, fn, ctx)


def _scan_body_ts(fs: FileSym, fn: Func, ctx) -> None:
    src_key = fn.key
    body = fn.body or ""
    var_types: dict[str, str] = dict(fs.members)
    var_types.update(fs.module_vars)
    for p, t in fn.params:
        if t:
            var_types[p] = t
    for m in TS_TYPED_LOCAL_RE.finditer(body):
        var_types[m.group(1)] = m.group(2)
    for m in TS_NEW_LOCAL_RE.finditer(body):
        var_types[m.group(1)] = m.group(2)
    new_spans = []
    for m in TS_NEW_RE.finditer(body):
        cls = m.group(1)
        new_spans.append(m.span())
        if cls in ctx.class_map:
            dst = ctx.class_map[cls]
            if "constructor" in ctx.files[dst].funcs:
                ctx._emit_call(src_key, dst, "constructor")
    for m in TS_CALL_RE.finditer(body):
        head, name = m.group(1), m.group(2)
        if not name or name in TS_NON_CALLS:
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
            elif len(parts) == 2 and fs.members.get(parts[1]):
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


# ---- sweeps (§1.8) -------------------------------------------------------------------

_STAR_EXPORT_RE = re.compile(
    r"""export\s*\*\s*(?:as\s+([A-Za-z_$][\w$]*)\s+)?from\s*(["'])([^"']+)\2""")
_REEXPORT_CLAUSE_RE = re.compile(
    r"""export\s*\{([^}]*)\}\s*from\s*(["'])([^"']+)\2""")
_SPEC_ITEM_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*(?:as\s+([A-Za-z_$][\w$]*))?")


def _sweep_resolve(spec: str, rel: str, ctx) -> str:
    """ctx.files-truth resolution for sweep passes (parse-time resolution
    without a live Path: same §1.4 candidate order, no filesystem)."""
    if not spec or not _is_specifier_relative(spec):
        return ""
    d = posixpath.dirname(rel)
    base = posixpath.normpath(posixpath.join(d, spec) if d else spec)
    for cand in (base, base + ".ts", base + ".tsx", base + ".mts", base + ".cts",
                 base + "/index.ts", base + "/index.tsx"):
        if cand in ctx.files:
            return cand
    for src_e, dsts in _EXT_REWRITES:
        if base.endswith(src_e):
            for dd in dsts:
                if base[:-len(src_e)] + dd in ctx.files:
                    return base[:-len(src_e)] + dd
    return ""


def _fill_inheritance(ctx) -> None:
    """TS classes feed ctx.class_map and ctx._subclasses so _emit_call
    resolves receivers and mirrors overrides (the graph surfaces)."""
    for rel in sorted(ctx.files):
        fs = ctx.files[rel]
        if fs.ext not in TS_EXTS:
            continue
        for cls_name, ext_nm in getattr(fs, "_ts_classes", ()):
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

    Runs first in the TS sweep pair so inheritance mirrors exist before
    scan_file mints edges. Files whose only exports are re-exports are
    barrels -> is_wiring_only.
    """
    _fill_inheritance(ctx)
    tables: dict[str, tuple[dict[str, tuple[str, str]], list[str]]] = {}
    for rel in sorted(ctx.files):
        fs = ctx.files[rel]
        if fs.ext not in TS_EXTS:
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
        tables[rel] = (names, stars)
        if not fs.funcs and (names or stars):
            fs._ts_barrel = True
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
        if fs.ext not in TS_EXTS:
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


def import_liveness_sweep(ctx) -> None:
    """Star/side-effect/dynamic imports: the whole target module's funcs
    enter ctx.referenced (python plain-import semantics verbatim)."""
    for rel in sorted(ctx.files):
        fs = ctx.files[rel]
        if fs.ext not in TS_EXTS:
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
