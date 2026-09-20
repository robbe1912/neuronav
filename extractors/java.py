"""Java extractor: tree-sitter-java front-end (issue #335).

Strategy: one parse per file via the pinned grammar pair —
``tree-sitter==0.26.0`` + ``tree-sitter-java==0.23.5`` (the official
org wheel, verified to load and parse under the 0.26 core — same
verification method as the tree-sitter-rust==0.24.2 pin). Defs come
from one ordered S-expression Query; imports/annotations get a
structural walk (modifiers hold annotations as children — a query
cannot express the attribution).

Package/import resolution (v1, documented): NO Maven/Gradle classpath
awareness. ``package a.b;`` declares this file's package; an import
``a.b.C`` resolves INTRA-REPO by package-path-to-directory match — the
nearest ancestor directory of this file whose ``a/b/C.java`` suffix
exists (this also covers the standard ``src/main/java`` and
``src/test/java`` layouts without knowing which root they hang from:
the package path itself IS the suffix). External dependencies (JDK,
jars) miss and record nothing — the loud degrade is the miss itself:
calls through their types fall to name-level liveness, never a silent
wrong-file edge (the SCIP definition-table law: an occurrence binds to
a definition only on a proven path).

Entry model (convention-based, documented): a method named ``main``
whose parameter list carries a ``String[]`` is a root; JUnit
``@Test`` / ``@ParameterizedTest`` annotations (last path segment —
org.junit.Test JUnit4 and org.junit.jupiter.api.Test JUnit5 both hit)
make their method an entry hint (the rusthard ``#[test]`` precedent).

Dispatch model: interface default methods are Funcs of the interface's
own file (bodies present); a call ``recv.m()`` whose receiver type
implements interfaces mirrors to the interface files for defaults and
to sibling implementors through ``ctx._subclasses`` — the same shape
rust.py models for trait defaults. ``@Override`` methods land in
``fs.dispatch_names`` (review tier, never likely — overrides, not
orphans, the gd VIRTUALS law).

NEVER read ``Node.start_point`` / ``end_point`` — py-tree-sitter 0.26
Point bug (tree-sitter-py issue #472); every line number is bisect
over newline byte offsets (the ts/js/rust law).

Determinism law: captures are re-sorted by ``start_byte`` before any
emission; same-name collisions resolve first-in-file-wins (the cpp
overload law — Java overloads share a name, so one Func per name
owns the first line and the FIRST body; the pin documents the
collapse); every sweep iterates ``sorted(...)``. No rng anywhere.

Leaf parser: reads only the file being parsed plus sibling-existence
stat checks for import resolution (no other source file's contents)
and never imports nav or graph — cross-file work lives in the
ctx-driven sweeps below.
"""

from __future__ import annotations

import re
from functools import partial
from pathlib import Path

from tree_sitter import Language, Parser, Query, QueryCursor

import tree_sitter_java as _tsj

from extractors.common import (  # leaf module: shared text mechanics (#302)
    body_block,
    captures_bytewise,  # neutral capture walk (#377)
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

JAVA_EXTS = frozenset({".java"})
JAVA_LANG = Language(_tsj.language())
_PARSER = Parser(JAVA_LANG)

_IDENT_TYPES = ("identifier", "type_identifier")

_QUERY_SRC = """
(class_declaration) @class.def
(interface_declaration) @iface.def
(enum_declaration) @enum.def
(record_declaration) @record.def
(method_declaration) @m.def
(constructor_declaration) @ctor.def
(field_declaration) @field.def
(package_declaration) @pkg
(import_declaration) @import
(method_reference) @mref
"""
_QUERY = Query(JAVA_LANG, _QUERY_SRC)

_ident_child = partial(ident_child, ident_types=_IDENT_TYPES)
_last_ident = partial(last_ident, dollar=False)


def _mods_of(node, src: bytes):
    """(modifiers_text, is_static) of a declaration node — java hangs
    annotations + ``static``/``final`` inside one ``modifiers`` child."""
    for ch in node.children:
        if ch.type == "modifiers":
            txt = _text(ch, src)
            return txt, bool(re.search(r"\bstatic\b", txt))
    return "", False


def _anno_names(mods_txt: str) -> set[str]:
    """Last path segment of every ``@Annotation(...)`` in a modifiers
    blob (JUnit 4 + 5 both land on ``Test``)."""
    return {m.rsplit(".", 1)[-1] for m in re.findall(r"@([\w.]+)", mods_txt)}


def _name_of(node, src: bytes) -> str:
    got = node.child_by_field_name("name")
    if got is not None:
        return _text(got, src)
    return _ident_child(node, src)


def _formal_params(node, src: bytes) -> list[tuple[str, str]]:
    """([(name, type)], ) from a method/constructor's formal_parameters
    (or a record's components — same grammar node)."""
    params: list[tuple[str, str]] = []
    fp = node.child_by_field_name("parameters")
    if fp is None:
        return params
    for p in fp.children:
        if p.type != "formal_parameter":
            continue
        nm = p.child_by_field_name("name")
        if nm is None:
            continue
        pty = ""
        ty = next((c for c in p.children if c.type in
                   ("type_identifier", "array_type", "generic_type",
                    "scoped_type_identifier", "boolean_type", "int")),
                  None)
        if ty is not None:
            pty = _text(ty, src)
        params.append((_text(nm, src), pty))
    return params


def _ret_type(node, src: bytes) -> str:
    ty = node.child_by_field_name("type")
    return _text(ty, src) if ty is not None else ""


def _implements(node, src: bytes) -> list[str]:
    """Interface names off a class/interface declaration's super
    interfaces (``implements``/``extends`` on interfaces)."""
    out: list[str] = []
    sup = node.child_by_field_name("interfaces")
    if sup is None:
        sup = node.child_by_field_name("super_interfaces")
    if sup is None:
        return out
    for ch in sup.children:
        if ch.type in ("type_identifier", "scoped_type_identifier", "generic_type"):
            out.append(_last_ident(_text(ch, src)))
    return out


def _superclass(node, src: bytes) -> str:
    sup = node.child_by_field_name("superclass")
    if sup is None:
        return ""
    got = _last_ident(_text(sup, src))
    return got


# ---- package-path resolution (leaf: existence stats only) -----------------------
# Prior art: the stack-graphs "local path then repo path" two-phase
# resolution — the package suffix IS the path, anchored at the nearest
# ancestor directory that completes it (covers src/main/java and
# src/test/java roots without knowing which one holds the tree).


def _resolve_type(path: Path, pkg_segs: list[str], type_name: str) -> Path | None:
    """File of ``pkg.Type``, walking ancestors of `path` for a dir whose
    ``pkg/Type.java`` suffix exists. None = external dependency."""
    rel = "/".join(pkg_segs + [f"{type_name}.java"])
    cur = path.parent
    while True:
        if (cur / rel).is_file():
            return cur / rel
        if cur.parent == cur:
            return None
        cur = cur.parent


def _pkg_segs(pkg_node, src: bytes) -> list[str]:
    segs = re.findall(r"[A-Za-z_]\w*", _text(pkg_node, src))
    if segs and segs[0] == "package":  # keyword rides the node text
        segs = segs[1:]
    return segs


# ---- parse ----------------------------------------------------------------------

def parse(path: Path, rel: str) -> FileSym:
    """Registry entry point: one FileSym per .java file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    src = text.encode("utf-8")
    fs = FileSym(path=rel, ext=".java")
    root = _PARSER.parse(src).root_node
    line_starts = line_starts_of(src)
    caps = QueryCursor(_QUERY).captures(root)

    bytewise = partial(captures_bytewise, caps)

    pkg: list[str] = []
    for node in bytewise("pkg"):
        pkg = _pkg_segs(node, src)
    fs._java_package = ".".join(pkg)

    # -- named types: first type names the file; all feed class_map ------
    types: list[tuple[str, str]] = []
    implements: list[tuple[str, list[str]]] = []  # (type, interfaces)
    extends_map: list[tuple[str, str]] = []       # (subtype, supertype)
    for key, kind in (("class.def", "class"), ("iface.def", "interface"),
                      ("enum.def", "enum"), ("record.def", "record")):
        for node in bytewise(key):
            nm = _name_of(node, src)
            if not nm:
                continue
            types.append((nm, kind))
            fs.aliases.setdefault(nm, kind)
            if not fs.class_name:
                fs.class_name = nm
            if kind == "class":
                sup = _superclass(node, src)
                if sup:
                    fs.extends = fs.extends or sup
                    extends_map.append((nm, sup))
            ifaces = _implements(node, src)
            if ifaces:
                implements.append((nm, ifaces))
    fs._java_types = types
    fs._java_implements = implements
    fs._java_extends = extends_map

    # -- methods + constructors: one Func per name, first-in-file wins ---
    # (the cpp overload law — Java overloads collapse; the FIRST line and
    # body own the Func; a method's declared line is the method node's)
    bodies: dict[str, tuple] = {}
    heads: dict[str, int] = {}
    statics: set[str] = set()
    entry_hints: set[str] = set()
    dispatch_names: set[str] = set()
    for node in bytewise("m.def", "ctor.def"):
        nm = _name_of(node, src)
        if not nm:
            continue
        line = _line(node, line_starts)
        heads.setdefault(nm, line)
        body = (body_block(node, src, "block")
                or body_block(node, src, "constructor_body"))
        if body and nm not in bodies:
            bodies[nm] = (line, body, _formal_params(node, src),
                          _ret_type(node, src))
        mods, is_static = _mods_of(node, src)
        if is_static:
            statics.add(nm)
        annos = _anno_names(mods)
        if "Test" in annos or "ParameterizedTest" in annos:
            entry_hints.add(nm)
        if "Override" in annos:
            dispatch_names.add(nm)
    fs._java_statics = statics
    fs.entry_hints = entry_hints
    fs.dispatch_names = dispatch_names
    for nm in sorted(bodies):
        line, body, params, ret = bodies[nm]
        fs.funcs.setdefault(nm, Func(path=rel, name=nm,
                                     line=heads.get(nm, line),
                                     body=body, params=params, ret=ret))

    # -- main(String[]) entry hint (rule re-checks the param shape) ------
    for nm in statics | set(fs.funcs):
        if nm == "main":
            fn = fs.funcs.get(nm)
            if fn is not None and any(
                    "String" in t and "[" in t for _, t in fn.params):
                fs.entry_hints.add("main")

    # -- fields: instance fields -> members; static fields -> globals ----
    for node in bytewise("field.def"):
        mods, is_static = _mods_of(node, src)
        ty = _ret_type(node, src)
        for ch in node.children:
            if ch.type != "variable_declarator":
                continue
            nm = ch.child_by_field_name("name")
            if nm is None:
                continue
            name = _text(nm, src)
            if is_static:
                fs.globals.setdefault(name, ty)
            else:
                fs.members.setdefault(name, ty)

    # -- imports: package-path resolution; static imports bind methods ---
    # plain `import a.b.C;`  -> pkg segs[:-1], type tail (info table)
    # static `...C.m;`       -> type is segs[-2], member the tail: binds
    #                           exactly that method (python from-import
    #                           semantics)
    # static `...C.*;`       -> whole-type method liveness (glob law)
    for node in bytewise("import"):
        segs = re.findall(r"[A-Za-z_]\w*|\*", _text(node, src))
        if segs and segs[0] == "import":  # keyword rides the node text
            segs = segs[1:]
        is_static = segs and segs[0] == "static"
        if is_static:
            segs = segs[1:]
        if not segs:
            continue
        if "*" in segs:
            if is_static and len(segs) >= 2:
                got = _resolve_type(path, segs[:-2], segs[-2])
                if got is not None:
                    fs.imported_modules.add(_rel_of_target(got, path, rel))
            continue
        if is_static:
            if len(segs) < 2:
                continue
            got = _resolve_type(path, segs[:-2], segs[-2])
            if got is not None:
                fs.from_imports.add((_rel_of_target(got, path, rel), segs[-1]))
            continue
        got = _resolve_type(path, segs[:-1], segs[-1])
        if got is None:
            continue  # external dependency: loud miss, nothing recorded
        fs._java_type_imports = getattr(fs, "_java_type_imports", set())
        fs._java_type_imports.add(
            (_rel_of_target(got, path, rel), segs[-1]))

    # -- method references (Foo::bar): no call site; name-level alive ----
    for node in bytewise("mref"):
        nm = node.child_by_field_name("method")
        if nm is not None:
            fs.name_literals.add(_text(nm, src))

    return fs


# ---- hooks (langsep REQUIRED surface) -------------------------------------------

from extractors.common import entry_keys  # late: package cycle, rust precedent

MENTION_FLOOR = 2
DYNAMIC_HINT = re.compile(r"\b(?:Override|FunctionalInterface)\b")

# graph's dup normalizer strips these before hashing (issue #295)
COMMENT_PREFIXES = ("//", "/*")

# Object/Comparable machinery invoked without a textual call site:
# string concat + println dispatch toString, hashing dispatches hashCode,
# collections dispatch equals/compareTo. The rust RUST_STD_TRAIT_METHODS
# analogue — dead-scan can never disprove their liveness. Kept minimal;
# extend only with dispatch evidence.
JAVA_VIRTUALS = frozenset({
    "toString", "hashCode", "equals", "clone", "finalize",
    "compareTo", "run", "call",
})


def is_entry_exempt(name: str) -> bool:
    return name in JAVA_VIRTUALS


def unresolved_base_review(name: str) -> bool:
    """Java's tail arm: a method overriding a base this repo does not
    carry (external library) reads review, never likely — the method is
    an override shape by @Override or by name convention."""
    return name in JAVA_VIRTUALS


def stand_in_review(fs: FileSym, name: str) -> bool:
    return False


def mention_review(name: str, mentions) -> bool:
    return mentions.get(name, 0) >= MENTION_FLOOR


def stat_tags(text: str) -> tuple[str, str]:
    """(class_name, extends) header sniff — java has no header form
    (the package line is not a class header)."""
    return ("", "")


def is_wiring_only(fs: FileSym) -> bool:
    """package-info.java and annotation-only files carry no funcs but
    are declarations, not wiring — java has no barrel analogue."""
    return False


def counts_dead_share(fs: FileSym) -> bool:
    """Judge C1: the dead-share denominator counts .java files (the
    .gd/.ts/.rs precedent — registered structural suffixes flag dead
    files)."""
    return fs.ext == ".java"


# ---- entry rules (convention-based; module header documents them) ----------------

def _entry_main(fs: FileSym, ctx=None):
    return entry_keys(fs, ("main",))


def _entry_tests(fs: FileSym, ctx=None):
    return entry_keys(fs, sorted(fs.entry_hints))


ENTRY_RULES = (_entry_main, _entry_tests)


# ---- facts harvest (per-file, ctx-truth surfaces) --------------------------------

def harvest_facts(fs: FileSym, ctx) -> None:
    if fs.ext != ".java":
        return
    for nm, _kind in getattr(fs, "_java_types", ()):
        ctx.class_map.setdefault(nm, fs.path)
    # inheritance + interface dispatch mirroring: supertype -> this file
    # (a call on the supertype's method mirrors to every implementor —
    # the _emit_call override mirror; interface defaults live in the
    # interface's own file via the scan's fallback below)
    for typ, ifaces in getattr(fs, "_java_implements", ()):
        for iface in ifaces:
            ctx._subclasses.setdefault(iface, set()).add(fs.path)
    for sub, sup in getattr(fs, "_java_extends", ()):
        ctx._subclasses.setdefault(sup, set()).add(fs.path)
    # impl map for the default-method fallback: class -> its interfaces
    if not hasattr(ctx, "_java_impl_map"):
        ctx._java_impl_map = {}
    for typ, ifaces in getattr(fs, "_java_implements", ()):
        ctx._java_impl_map.setdefault(typ, set()).update(ifaces)
    for nm in sorted(fs.name_literals):
        ctx.referenced_names.add(nm)


# ---- body scan --------------------------------------------------------------------

JAVA_NON_CALLS = frozenset({
    "if", "while", "for", "switch", "return", "new", "assert", "catch",
    "do", "else", "try", "throw", "throws", "synchronized", "super",
    "this", "case", "default",
})

# head = dotted receiver-or-type path; name = callee.
JAVA_CALL_RE = re.compile(
    r"(?<![\w.$])((?:[A-Za-z_]\w*\.)+)"
    r"([A-Za-z_]\w*)\s*\(")
JAVA_BARE_CALL_RE = re.compile(r"(?<![\w.$])([A-Za-z_]\w*)\s*\(")
JAVA_NEW_RE = re.compile(
    r"\bnew\s+((?:[A-Za-z_]\w*)(?:\.[A-Za-z_]\w*)*)\s*(?:<[^<>]*>)?\s*\(")
# `Foo bar = ...;` / `Foo bar;` — declared locals feed receiver typing
JAVA_LOCAL_RE = re.compile(
    r"(?:^|[;{(\s])([A-Z]\w*(?:<[^;=)\n]*>)?)\s+([a-z]\w*)\s*(?:=[^=]|[;)])")
JAVA_MREF_RE = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*::([A-Za-z_]\w*)")


def _import_target(fs: FileSym, name: str, ctx):
    """(file, fn) a static-import-bound name resolves to, else None."""
    for t, nm in sorted(fs.from_imports):
        if nm == name and t in ctx.files and name in ctx.files[t].funcs:
            return (t, name)
    return None


def scan_file(fs: FileSym, ctx) -> None:
    if fs.ext != ".java":
        return
    for _, fn in sorted(fs.funcs.items(), key=lambda kv: (kv[1].line, kv[0])):
        _scan_body_java(fs, fn, ctx)


def _scan_body_java(fs: FileSym, fn: Func, ctx) -> None:
    src_key, body, var_types = receiver_env(fs, fn, module_vars=False)
    for m in JAVA_LOCAL_RE.finditer(body):
        var_types[m.group(2)] = m.group(1)
    for m in JAVA_NEW_RE.finditer(body):
        cls = m.group(1).rsplit(".", 1)[-1]  # qualified new: class is the tail
        dst = ctx.class_map.get(cls, "")
        # constructor: the ctor Func carries the class's own name
        if dst and cls in ctx.files[dst].funcs:
            ctx._emit_call(src_key, dst, cls)
        elif dst:
            ctx.referenced_names.add(cls)
        else:
            ctx.referenced_names.add(cls)
    for m in JAVA_MREF_RE.finditer(body):
        ctx.referenced_names.add(m.group(1))
    for m in JAVA_CALL_RE.finditer(body):
        head, name = m.group(1), m.group(2)
        if name in JAVA_NON_CALLS:
            continue
        recv = head[:-1].rsplit(".", 1)[-1]
        if recv == "this":
            if name in fs.funcs:
                ctx._emit_call(src_key, fs.path, name)
            else:
                ctx.referenced_names.add(name)
            continue
        if recv == "super":
            # super.m() — the (possibly external) base: name-level alive
            ctx.referenced_names.add(name)
            continue
        if recv[:1].isupper():
            # Type.staticMethod( — class_map truth (imports are names)
            dst = ctx.class_map.get(recv, "")
            tfs = ctx.files.get(dst) if dst else None
            if tfs is not None and name in tfs.funcs:
                ctx._emit_call(src_key, dst, name)
            elif tfs is not None:
                ctx._emit_call(src_key, dst, name)  # unresolved on the
                # class: still an edge — static resolution proved the
                # class, the name rides the mention floor
            else:
                ctx.referenced_names.add(name)  # external type
            continue
        # instance receiver: typed local / field / param -> class_map
        cls = var_types.get(recv, "")
        typ = _last_ident(cls) if cls else ""
        dst = ctx.class_map.get(typ, "") if typ else ""
        tfs = ctx.files.get(dst) if dst else None
        if tfs is not None and name in tfs.funcs:
            ctx._emit_call(src_key, dst, name)  # + override mirror
            continue
        # interface default dispatch: the impl's interfaces may carry a
        # default body (the rust trait-default shape)
        impl_map = getattr(ctx, "_java_impl_map", {})
        dispatched = False
        if typ:
            for iface in sorted(impl_map.get(typ, ())):
                irel = ctx.class_map.get(iface, "")
                itfs = ctx.files.get(irel) if irel and irel != dst else None
                if itfs is not None and name in itfs.funcs:
                    ctx._emit_call(src_key, irel, name)
                    dispatched = True
                    break
        if not dispatched:
            # untyped receiver / stream chain / external lib: name alive
            ctx.referenced_names.add(name)
    # bare calls: static-imports first, then same-file
    for m in JAVA_BARE_CALL_RE.finditer(body):
        name = m.group(1)
        if name in JAVA_NON_CALLS:
            continue
        if name in fs.funcs:
            ctx._emit_call(src_key, fs.path, name)
            continue
        got = _import_target(fs, name, ctx)
        if got:
            ctx._emit_call(src_key, got[0], got[1])
        # else: an unresolved bare name is either a JDK static import or
        # an inherited method — name-level liveness via the OTHER regex
        # arms would double-count; bare JDK calls (println etc.) are
        # never corpus facts, so dropping here is correct


# ---- sweeps (ctx-truth, post-harvest) --------------------------------------------

import_liveness_sweep = make_import_liveness_sweep(
    JAVA_EXTS,
    "`import static a.b.C.*` wildcards: the whole target file's funcs "
    "enter ctx.referenced (glob semantics, the rust `use foo::*` law).",
    from_imports=True,
)


# registry choreography binds (langsep) — see extractors/python.py's
# _PASS_* block for the rationale.
_PASS_IMPORTS = import_liveness_sweep
_PASS_FACTS = harvest_facts
