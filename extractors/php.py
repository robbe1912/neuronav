r"""PHP extractor: tree-sitter-php front-end (issue #343).

Strategy: one parse per file via the pinned grammar pair —
``tree-sitter==0.26.0`` + ``tree-sitter-php==0.24.1`` (the official
org wheel, verified to load and parse under the 0.26 core; binding is
``language_php()`` — the HTML-embedding grammar, so real-world
``index.php`` files with interleaved markup parse whole). Defs,
namespaces, imports and trait-use come from one ordered Query plus a
small structural walk (attribute groups are SIBLING nodes before a
method — a query cannot express the attribution; the rust
attribute-timeline law applies).

Namespace/import resolution (v1, documented): NO Composer
classmap/PSR-4 prefix-map awareness. ``namespace App\Sub;`` declares
this file's namespace; a ``use App\\Other\\Widget;`` resolves INTRA-REPO
by namespace-path-to-directory match — the nearest ancestor directory
of this file whose ``App/Other/Widget.php`` suffix exists (PSR-4's
"namespace == path relative to the project root" assumption; the
Java #335 shape, which covered src/main/java-style roots the same
way: the namespace path itself IS the suffix). External dependencies
(composer vendors, extensions) miss and record nothing — the loud
degrade is the miss itself: calls through their types fall to
name-level liveness, never a silent wrong-file edge (the SCIP
definition-table law).

Entry model (convention-based, documented): files named ``index.php``
or ``artisan`` are web/tooling entry points — every func in them is a
root (the gd MANUAL_BASES analogue); composer.json ``bin`` scripts
resolve through ctx at entry time (tolerant miss). PHPUnit entries:
``#[Test]`` / ``#[DataProvider]`` attributes and ``@test``
doc-comments attribute to the NEXT method (timeline law).

Dispatch model: ``C::m()`` resolves through class_map (static truth);
``$o->m()`` resolves through typed locals/params/fields → class_map,
then the class's TRAIT files (a trait method body lives in the
trait's own file — the java interface-default fallback shape);
untyped receivers fall to name-level liveness (``referenced_names``,
never a wrong-file edge). ``new C()`` edges the class's file and its
``__construct`` when present.

Magic methods (``__construct``, ``__toString``, ...) are plain
functions parse-wise but engine/runtime-dispatched liveness-wise:
``PHP_VIRTUALS`` exempts them from dead candidacy (the java
``toString`` law). ``__call``/``__callStatic`` additionally mark the
file dynamic (honest review tier for its unreachables).

NEVER read ``Node.start_point`` / ``end_point`` — py-tree-sitter 0.26
Point bug (tree-sitter-py issue #472); every line number is bisect
over newline byte offsets (the ts/js/rust/cpp/c law).

Determinism law: captures are re-sorted by ``start_byte`` before any
emission; same-name collisions resolve first-in-file-wins (the cpp
overload law — PSR-4's one-class-per-file convention keeps method
name collisions rare, and the FIRST line and body own the Func);
every sweep iterates ``sorted(...)``. No rng anywhere.

Leaf parser: reads only the file being parsed plus sibling-existence
stat checks for use-resolution (no other source file's contents) and
never imports nav or graph — cross-file work lives in the ctx-driven
sweeps below (composer.json reads happen in the ENTRY rule, where ctx
is the truth).
"""

from __future__ import annotations

import re
from bisect import bisect_right
from pathlib import Path

from tree_sitter import Language, Parser, Query, QueryCursor

from extractors.common import (
    body_block,
    entry_keys,
    ident_child,
    last_ident,
    line_starts_of,
    make_import_liveness_sweep,
    receiver_env,
    rel_of_target,
)
from extractors.model import FileSym, Func

PHP_EXTS = frozenset({".php"})
_tsp = __import__("tree_sitter_php")
PHP_LANG = Language(_tsp.language_php())
_PARSER = Parser(PHP_LANG)

PHP_CONVENTION_ENTRIES = frozenset({"index.php", "artisan"})

# engine/runtime-dispatched magic methods: liveness cannot be disproven
# (string concat dispatches __toString, isset __isset, ...). Kept
# minimal; extend only with dispatch evidence (the java JAVA_VIRTUALS
# law). __construct is here too: `new C()` edges it when present, but a
# ctor only referenced via `new` in an UNRESOLVED (external) type would
# otherwise read dead.
PHP_VIRTUALS = frozenset(
    {
        "__construct",
        "__destruct",
        "__toString",
        "__debugInfo",
        "__get",
        "__set",
        "__isset",
        "__unset",
        "__call",
        "__callStatic",
        "__invoke",
        "__clone",
    }
)

# PHPUnit attribute names (last segment — the java @Test law): both
# PHP 8 attributes and doc-comment annotations feed entry hints.
PHP_TEST_ATTRS = frozenset({"Test", "DataProvider", "TestWith", "BeforeClass", "AfterClass"})

_QUERY_SRC = """
(namespace_definition name: (namespace_name) @ns.def)
(class_declaration name: (name) @cls.name) @cls.def
(interface_declaration name: (name) @iface.name) @iface.def
(trait_declaration name: (name) @trait.name) @trait.def
(enum_declaration name: (name) @enum.name) @enum.def
(function_definition name: (name) @fn.name) @fn.def
(method_declaration name: (name) @m.name) @m.def
(namespace_use_declaration) @use.def
(use_declaration (name) @tuse.name) @tuse.def
(object_creation_expression (name) @new.name) @new.def
(attribute_group) @attr.def
"""
_QUERY = Query(PHP_LANG, _QUERY_SRC)

_IDENT_TYPES = ("name",)


def _text(node, src: bytes) -> str:
    if node is None:
        return ""
    return src[node.start_byte:node.end_byte].decode("utf8", "replace")


def _name_of(node, src: bytes) -> str:
    nm = node.child_by_field_name("name")
    if nm is not None:
        return _text(nm, src)
    got = ident_child(node, src, ident_types=_IDENT_TYPES)
    return got if got else ""


def _line(node, line_starts) -> int:
    return bisect_right(line_starts, node.start_byte)


# ---- namespace-path resolution (leaf: existence stats only) ----------------------
# Prior art: the stack-graphs "local path then repo path" two-phase
# resolution — the namespace suffix IS the path, anchored at the nearest
# ancestor directory that completes it (PSR-4 assumes namespace == path
# relative to the project root; composer prefix maps are v1-non-goal,
# the no-Maven precedent).


def _ns_segs(ns_node, src: bytes) -> list[str]:
    segs = re.findall(r"[A-Za-z_]\w*", _text(ns_node, src))
    if segs and segs[0] == "namespace":  # keyword rides the node text
        segs = segs[1:]
    return segs


def _resolve_type(path: Path, ns_segs: list[str], type_name: str) -> Path | None:
    """File of ``ns\\Type``, walking ancestors of `path` for a dir whose
    ``ns/segs/Type.php`` suffix exists (PSR-4 default mapping; the
    java #335 anchor-walk, separators swapped)."""
    if not ns_segs or not type_name:
        return None
    rel = "/".join(ns_segs + [f"{type_name}.php"])
    cur = path.parent
    for _ in range(64):  # depth guard, mirrors java
        cand = cur / rel
        if cand.is_file():
            return cand
        nxt = cur.parent
        if nxt == cur:
            return None
        cur = nxt
    return None



# ---- parse ------------------------------------------------------------------------


def parse(path: Path, rel: str) -> FileSym:
    """Registry entry point: one FileSym per .php file."""
    src = path.read_bytes()
    fs = FileSym(path=rel, ext=".php")
    root = _PARSER.parse(src).root_node
    line_starts = line_starts_of(src)
    caps = QueryCursor(_QUERY).captures(root)

    def bytewise(*keys: str) -> list:
        nodes = []
        for k in keys:
            nodes.extend(caps.get(k, ()))
        return sorted(nodes, key=lambda n: n.start_byte)

    # -- namespace -----------------------------------------------------
    ns: list[str] = []
    for node in bytewise("ns.def"):
        ns = _ns_segs(node, src)
    fs._php_namespace = ns

    # -- named types: first type names the file; all feed class_map ----
    types: list[tuple[str, str]] = []
    extends_map: list[tuple[str, str]] = []
    implements: list[tuple[str, list[str]]] = []
    trait_uses: list[tuple[str, str]] = []  # (type, trait)
    type_nodes: list[tuple[str, object]] = []
    for key, kind in (("cls.def", "class"), ("iface.def", "interface"),
                      ("trait.def", "trait"), ("enum.def", "enum")):
        for node in bytewise(key):
            nm = _name_of(node, src)
            if not nm:
                continue
            types.append((nm, kind))
            type_nodes.append((nm, node))
            fs.aliases.setdefault(nm, kind)
            if not fs.class_name:
                fs.class_name = nm
    # extends / implements off the class_interface_clause text
    for nm, node in type_nodes:
        for ch in node.children:
            if ch.type != "class_interface_clause":
                continue
            clause = _text(ch, src)
            ext = re.search(r"extends\s+([A-Za-z_][\w\\\\]*)", clause)
            if ext:
                sup = ext.group(1).rsplit("\\", 1)[-1]
                fs.extends = fs.extends or sup
                extends_map.append((nm, sup))
            for imp in re.findall(r"implements\s+([A-Za-z_][\w\\\\,\\s]*)", clause):
                for one in imp.split(","):
                    one = one.strip().rsplit("\\", 1)[-1]
                    if one:
                        implements.append((nm, one))
    # interface extends: multiple parents ride the same clause shape
    fs._php_types = types
    fs._php_extends = extends_map
    fs._php_implements = implements

    # -- trait use INSIDE a type body: bare use_declaration nodes ------
    # (top-level imports are namespace_use_declaration — distinct node)
    type_spans = [(nm, node.start_byte, node.end_byte) for nm, node in type_nodes]
    for node in bytewise("tuse.def"):
        for nm, lo, hi in type_spans:
            if lo <= node.start_byte < hi:
                tname = ident_child(node, src, ident_types=("name",))
                if tname:
                    trait_uses.append((nm, tname))
                break
    fs._php_trait_uses = trait_uses

    # -- methods + functions: one Func per name, first-in-file wins ----
    # (the cpp overload law; PSR-4 one-class-per-file keeps collisions
    # rare and the FIRST line and body own the Func)
    bodies: dict[str, tuple] = {}
    heads: dict[str, int] = {}
    for node in bytewise("m.def", "fn.def"):
        nm = _name_of(node, src)
        if not nm:
            continue
        line = _line(node, line_starts)
        heads.setdefault(nm, line)
        body = body_block(node, src, "compound_statement")
        if body and nm not in bodies:
            bodies[nm] = (line, body, _params_of(node, src), _ret_of(node, src))
    # attribute timeline: attribute groups + @test doc-comments
    # attribute to the next method (the rust sibling-run law)
    attr_marks: list[tuple[int, bool]] = []  # (line, is_test)
    for node in bytewise("attr.def"):
        txt = _text(node, src)
        is_test = any(a in PHP_TEST_ATTRS for a in re.findall(r"#\[\\?([\w]+)", txt))
        attr_marks.append((_line(node, line_starts), is_test))
    text_str = src.decode("utf8", "replace")
    for m in re.finditer(r"@test\b", text_str):
        attr_marks.append((src.count(b"\n", 0, m.start()) + 1, True))
    entry_hints: set[str] = set()
    # a mark attributes to the NEAREST method at-or-after its line (the
    # rust sibling-run timeline law); only test marks attribute anything
    for mark_line, is_test in attr_marks:
        if not is_test:
            continue
        cands = sorted((ln, onm) for onm, ln in heads.items() if ln >= mark_line)
        if cands:
            entry_hints.add(cands[0][1])
    fs.entry_hints = entry_hints
    for nm in sorted(bodies):
        line, body, params, ret = bodies[nm]
        fs.funcs.setdefault(nm, Func(path=rel, name=nm,
                                     line=heads.get(nm) or line,
                                     body=body, params=params, ret=ret))

    # -- use imports: namespace-path resolution; function imports bind -
    # plain `use App\Other\Widget;`     -> type table (resolved rel path)
    # `use App\Other\Widget as W;`      -> alias table entry
    # `use function App\fns\helper;`    -> from_imports (binds exactly)
    # `use const ...` / group `use X\{Y, Z};` -> leaf-walk below
    for node in bytewise("use.def"):
        txt = _text(node, src)
        is_fn = re.search(r"\buse\s+function\b", txt) is not None
        segs = re.findall(r"[A-Za-z_]\w*", txt)
        if segs and segs[0] == "use":
            segs = segs[1:]
        if is_fn and segs and segs[0] == "function":
            segs = segs[1:]
        if not segs:
            continue
        alias = None
        if "as" in segs:
            i = segs.index("as")
            alias = segs[i + 1] if i + 1 < len(segs) else None
            segs = segs[:i]
        if "{" in txt:
            # group use `App\{Other\Widget, More}` — v1 non-goal; a
            # garbage-path resolution is worse than a miss, so the loud
            # degrade is recording nothing
            continue
        if len(segs) < 2:
            continue
        if is_fn:
            got = _resolve_type(path, segs[:-2], segs[-2]) if len(segs) >= 2 else None
            if got is not None:
                fs.from_imports.add((rel_of_target(got, path, rel), segs[-1]))
            continue
        got = _resolve_type(path, segs[:-1], segs[-1])
        if got is None:
            continue  # external dependency: loud miss, nothing recorded
        got_rel = rel_of_target(got, path, rel)
        fs.imported_modules.add(got_rel)
        fs._php_type_imports = getattr(fs, "_php_type_imports", set())
        fs._php_type_imports.add((got_rel, segs[-1]))
        if alias:
            fs._php_aliases = getattr(fs, "_php_aliases", {})
            fs._php_aliases[alias] = segs[-1]

    # -- `Foo::class` constants: class-level liveness, no call site ----
    for m in re.finditer(rb"\b([A-Za-z_]\w*)::class\b", src):
        fs.name_literals.add(m.group(1).decode("utf8", "replace"))

    return fs


def _params_of(node, src: bytes) -> list[tuple[str, str]]:
    """([(name, type)]) from formal_parameters (simple_parameter +
    promoted-property shapes both carry type + variable_name)."""
    params: list[tuple[str, str]] = []
    fp = node.child_by_field_name("parameters")
    if fp is None:
        for ch in node.children:
            if ch.type == "formal_parameters":
                fp = ch
                break
    if fp is None:
        return params
    for pd in fp.children:
        if pd.type not in ("simple_parameter", "variadic_parameter"):
            continue
        nm = pd.child_by_field_name("name")
        ty_node = pd.child_by_field_name("type")
        ty = _text(ty_node, src) if ty_node is not None else ""
        params.append((
            _text(nm, src).lstrip("$") if nm is not None else "",
            ty.rsplit("\\", 1)[-1],
        ))
    return params


def _ret_of(node, src: bytes) -> str:
    for ch in node.children:
        if ch.type in ("named_type", "primitive_type", "nullable_type"):
            return _text(ch, src).rsplit("\\", 1)[-1]
    return ""


# ---- hooks (langsep REQUIRED surface) ---------------------------------------------

MENTION_FLOOR = 2
# __call/__callStatic bodies dispatch names the static pass cannot see
DYNAMIC_HINT = re.compile(r"function\s+__(?:call|callStatic)\s*\(")

# graph's dup normalizer strips these before hashing (issue #295).
# `#` is deliberately ABSENT: PHP attributes are `#[...]` and a
# `#`-strip would corrupt them (the java preprocessor-note analogue).
COMMENT_PREFIXES = ("//", "/*")


def is_entry_exempt(name: str) -> bool:
    return name in PHP_VIRTUALS


def unresolved_base_review(name: str) -> bool:
    """Magic methods on bases this repo does not resolve: overrides,
    not orphans (the JAVA_VIRTUALS tail law)."""
    return name in PHP_VIRTUALS


def stand_in_review(fs: FileSym, name: str) -> bool:
    return False


def mention_review(name: str, mentions) -> bool:
    return mentions.get(name, 0) >= MENTION_FLOOR


def stat_tags(text: str) -> tuple[str, str]:
    """(class_name, extends) header sniff — PHP has no header form."""
    return ("", "")


def is_wiring_only(fs: FileSym) -> bool:
    return False


def counts_dead_share(fs: FileSym) -> bool:
    return fs.ext == ".php"


# ---- entry rules (convention-based; module header documents them) -----------------


def _entry_conventions(fs: FileSym, ctx=None):
    """index.php / artisan are web/tooling entry points: every func in
    them roots (the gd MANUAL_BASES analogue). composer.json `bin`
    scripts root the same way when the manifest is readable in ctx."""
    if fs.ext not in PHP_EXTS:
        return
    base = fs.path.rsplit("/", 1)[-1]
    if base in PHP_CONVENTION_ENTRIES:
        yield from entry_keys(fs, sorted(fs.funcs))
        return
    bins = getattr(ctx, "_php_bin_scripts", None)
    if bins is None and ctx is not None:
        bins = set()
        try:
            manifest = ctx.read_file("composer.json")
        except (OSError, AttributeError):
            manifest = ""
        if manifest:
            for m in re.finditer(r'"bin"\s*:\s*\[(.*?)\]', manifest, re.S):
                for q in re.findall(r'"([^"]+\.php)"', m.group(1)):
                    bins.add(q)
        ctx._php_bin_scripts = bins
    if bins and (fs.path in bins or base in {b.rsplit("/", 1)[-1] for b in bins}):
        yield from entry_keys(fs, sorted(fs.funcs))


def _entry_tests(fs: FileSym, ctx=None):
    if fs.ext not in PHP_EXTS:
        return
    yield from entry_keys(fs, sorted(fs.entry_hints))


ENTRY_RULES = (_entry_conventions, _entry_tests)


# ---- facts harvest (per-file, ctx-truth surfaces) ---------------------------------


def harvest_facts(fs: FileSym, ctx) -> None:
    if fs.ext != ".php":
        return
    for nm, _kind in getattr(fs, "_php_types", ()):
        ctx.class_map.setdefault(nm, fs.path)
    for typ, iface in getattr(fs, "_php_implements", ()):
        ctx._subclasses.setdefault(iface, set()).add(fs.path)
    for sub, sup in getattr(fs, "_php_extends", ()):
        ctx._subclasses.setdefault(sup, set()).add(fs.path)
    # trait consumers: class -> trait files (the scan's fallback truth)
    if not hasattr(ctx, "_php_trait_map"):
        ctx._php_trait_map = {}
    for typ, trait in getattr(fs, "_php_trait_uses", ()):
        trel = ctx.class_map.get(trait, "")
        if trel:
            ctx._php_trait_map.setdefault(fs.path, set()).add(trel)
    for nm in sorted(fs.name_literals):
        ctx.referenced_names.add(nm)


# ---- body scan ---------------------------------------------------------------------

PHP_NON_CALLS = frozenset({
    "if", "while", "for", "foreach", "switch", "return", "new", "print",
    "echo", "match", "throw", "try", "catch", "finally", "do", "else",
    "elseif", "case", "default", "function", "fn", "clone", "yield",
    "list", "array", "isset", "unset", "empty", "include", "require",
    "include_once", "require_once",
})

PHP_NEW_RE = re.compile(r"\bnew\s+(?:\\\\)?([A-Za-z_]\w*)\s*\(")
PHP_STATIC_RE = re.compile(
    r"(?<![\w$>])(?:\\\\)?([A-Za-z_]\w*)(?:\\\\[A-Za-z_]\w*)*::([A-Za-z_]\w*)\s*\(")
PHP_DYN_RE = re.compile(r"(\$\w+)\s*->\s*([A-Za-z_]\w*)\s*\(")
PHP_BARE_RE = re.compile(r"(?<![\w$>])([A-Za-z_]\w*)\s*\(")
# typed locals/params: `Widget $w` (param types also ride _params_of)
PHP_LOCAL_RE = re.compile(
    r"(?:^|[;,({=\s])(?:\\\\)?([A-Z]\w*)\s+(\$\w+)")
PHP_FIELD_RE = re.compile(
    r"(?:public|protected|private)\s+(?:static\s+)?(?:\\\\)?([A-Z]\w*)\s+(\$\w+)")
# static property fetch: self::$x / C::$x — type-truth, no call
PHP_SELF_RE = re.compile(r"\b(?:self|static)\s*::\s*\$")


def _import_target(fs: FileSym, name: str, ctx):
    """(file, fn) a `use function` import resolves to, else None."""
    for t, nm in sorted(fs.from_imports):
        if nm == name and t in ctx.files and name in ctx.files[t].funcs:
            return (t, name)
    return None


def scan_file(fs: FileSym, ctx) -> None:
    if fs.ext != ".php":
        return
    for _, fn in sorted(fs.funcs.items(), key=lambda kv: (kv[1].line, kv[0])):
        _scan_body_php(fs, fn, ctx)


def _scan_body_php(fs: FileSym, fn: Func, ctx) -> None:
    src_key, body, var_types = receiver_env(fs, fn, module_vars=False)
    for m in PHP_LOCAL_RE.finditer(body):
        var_types[m.group(2)] = m.group(1)
    for m in PHP_FIELD_RE.finditer(body):
        var_types[m.group(2)] = m.group(1)
    for m in re.finditer(r"(\$\w+)\s*=\s*new\s+(?:\\\\)?([A-Za-z_]\w*)\s*\(", body):
        var_types[m.group(1)] = m.group(2)  # assignment-new binds the var
    for m in PHP_NEW_RE.finditer(body):
        cls = m.group(1)
        dst = ctx.class_map.get(cls, "")
        if dst:
            if "__construct" in ctx.files[dst].funcs:
                ctx._emit_call(src_key, dst, "__construct")
            else:
                # no ctor: the class's own surface is the reference
                ctx.referenced_names.add(cls)
        else:
            ctx.referenced_names.add(cls)  # external class: name-level
    for m in PHP_STATIC_RE.finditer(body):
        cls, name = m.group(1), m.group(2)
        if name in PHP_NON_CALLS:
            continue
        dst = ctx.class_map.get(cls, "")
        if dst and (name in ctx.files[dst].funcs or name in PHP_VIRTUALS):
            ctx._emit_call(src_key, dst, name)
        elif dst:
            ctx._emit_call(src_key, dst, name)  # class proved, name rides
        elif cls not in ("self", "static", "parent"):
            ctx.referenced_names.add(name)
        else:
            ctx.referenced_names.add(name)  # self::m on an unresolved base
    for m in PHP_DYN_RE.finditer(body):
        recv, name = m.group(1), m.group(2)
        if name in PHP_NON_CALLS:
            continue
        if recv == "$this":
            # own class first, then its trait files (consumed surface)
            if name in fs.funcs:
                ctx._emit_call(src_key, fs.path, name)
                continue
            dispatched_this = False
            for trel in sorted(getattr(ctx, "_php_trait_map", {}).get(fs.path, ())):
                if name in ctx.files[trel].funcs:
                    ctx._emit_call(src_key, trel, name)
                    dispatched_this = True
                    break
            if dispatched_this:
                continue
            ctx.referenced_names.add(name)  # inherited/external method
            continue
        cls = var_types.get(recv, "")
        typ = last_ident(cls.replace("\\", "/")) if cls else ""
        dst = ctx.class_map.get(typ, "") if typ else ""
        dispatched = False
        if dst and name in ctx.files[dst].funcs:
            ctx._emit_call(src_key, dst, name)
            dispatched = True
        if not dispatched and dst:
            # trait fallback: the class's traits may define the method
            for trel in sorted(getattr(ctx, "_php_trait_map", {}).get(dst, ())):
                if name in ctx.files[trel].funcs:
                    ctx._emit_call(src_key, trel, name)
                    dispatched = True
                    break
        if not dispatched:
            ctx.referenced_names.add(name)  # untyped receiver: name alive
    for m in PHP_BARE_RE.finditer(body):
        name = m.group(1)
        if name in PHP_NON_CALLS:
            continue
        if name in fs.funcs:
            ctx._emit_call(src_key, fs.path, name)
            continue
        got = _import_target(fs, name, ctx)
        if got:
            ctx._emit_call(src_key, got[0], got[1])
        # else: an unresolved bare name is a PHP builtin or an external
        # composer fn — never corpus facts, dropping is correct (the
        # java bare-call law)


# ---- sweeps (ctx-truth, post-harvest) ----------------------------------------------

import_liveness_sweep = make_import_liveness_sweep(
    PHP_EXTS,
    "`use` type imports keep the target file's whole surface referenced "
    "(glob semantics, the rust `use foo::*` law); `use function` binds "
    "exactly the named fn (python from-import semantics).",
    from_imports=True,
)


# registry choreography binds (langsep) — see extractors/python.py's
# _PASS_* block for the rationale.
_PASS_IMPORTS = import_liveness_sweep
_PASS_FACTS = harvest_facts
