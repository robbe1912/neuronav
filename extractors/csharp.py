"""C# extractor: tree-sitter-c-sharp front-end (issue #336).

Grammar wheel pinned at ``tree-sitter-c-sharp==0.23.5`` (official org
binding, Roslyn-derived grammar, C# 1-13; ``core`` extra NOT installed).
Line numbers are byte-offset derived — never ``start_point`` (the
py-tree-sitter #472 law shared by ts/js/rust).

Prior-art pass (owner directive: borrowed ideas NAMED):
- GitHub stack-graphs / tree-sitter-graph — path-based name resolution;
  the namespace-to-file index here is a deliberately simplified cousin
  (one namespace segment match, no binding lattice).
- Sourcegraph SCIP — symbols+occurrences per document feeding a global
  definition table; mirrored as flat Func keys resolved through
  class_map plus the namespace index.
- semgrep — language rules as named data beside the parser (the
  registry ENTRY_RULES discipline this package already follows).
- joern / codequery — code-property-graph style type merging; the
  partial-class merge collapses same-name declarations to one surface
  (first declaration wins the line, mirroring the overload law).
- codegraph (tree-sitter registry projects) — per-language front-end
  modules behind a suffix registry: this file's shape.

v1 scope, with the loud degradations named in the issue:
- NO .sln/.csproj awareness: intra-repo resolution is namespace-to-path
  only (a using directive keeps every file declaring that namespace
  alive as a unit — python plain-import semantics). External namespaces
  (System, UnityEngine.*, ...) simply match nothing and resolve outside
  the repo, which is the correct answer for engine/stdlib surface.
- ``using static X.Y;`` keeps the file(s) declaring namespace X alive
  (member-level static binding is v2; documented simplification).
- Properties ride ``members`` (name -> type) — they are field-like for
  liveness; accessors are not separate funcs. Constructors are Funcs
  named after the type; ``new X(...)`` calls reference that name.
- ``this.X = ...`` member writes fill ``writes``; unqualified field
  writes stay out (local/field ambiguity is not worth the false edge).

Symbols: class/struct/interface/record/enum declarations (nested
included), methods, constructors; fields + properties as members;
namespace + using directives; [Test]/[TestMethod]/[Fact]/[Theory]
attributes as entry hints (fs.entry_hints); Unity MonoBehaviour /
ScriptableObject event functions as convention entries, mirroring
gdscript.py's Godot ``_ready``-lifecycle rooting.
"""

from __future__ import annotations

import re
from pathlib import Path

from extractors.common import (
    FN_KEY_SEP,
    body_block,
    entry_keys,
    ident_child,
    last_ident,
    line_starts_of,
    node_line,
    node_text,
)
from extractors.model import FileSym, Func

try:
    from tree_sitter import Language, Parser
    import tree_sitter_c_sharp as _tscs
except ImportError as _e:  # pragma: no cover - dependency pin in pyproject
    raise ImportError(
        "neuronav: the C# extractor needs tree-sitter-c-sharp==0.23.5 "
        "(pyproject pin); install the project dependencies"
    ) from _e

CSHARP_EXTS = frozenset({".cs"})
CSHARP_LANG = Language(_tscs.language())
_PARSER = Parser(CSHARP_LANG)

_IDENT_TYPES = ("identifier", "type_identifier")

_TYPE_DECLS = frozenset({
    "class_declaration", "struct_declaration", "interface_declaration",
    "record_declaration", "enum_declaration",
})
_TYPE_KW = {
    "class_declaration": "class", "struct_declaration": "struct",
    "interface_declaration": "interface", "record_declaration": "record",
    "enum_declaration": "enum",
}

# Unity engine event functions (the MonoBehaviour/ScriptableObject
# lifecycle): dispatched by the engine with no textual call site — the
# gdscript VIRTUALS analogue. Main-table names from the Unity manual;
# kept minimal, extend only with dispatch evidence.
UNITY_EVENT_FUNCTIONS = frozenset({
    "Awake", "Start", "Update", "FixedUpdate", "LateUpdate",
    "OnEnable", "OnDisable", "OnDestroy", "OnValidate", "Reset",
    "OnGUI", "OnDrawGizmos", "OnDrawGizmosSelected",
    "OnTriggerEnter", "OnTriggerExit", "OnTriggerStay",
    "OnCollisionEnter", "OnCollisionExit", "OnCollisionStay",
    "OnControllerColliderHit", "OnJointBreak",
    "OnMouseDown", "OnMouseUp", "OnMouseOver",
    "OnMouseEnter", "OnMouseExit", "OnMouseDrag", "OnMouseUpAsButton",
    "OnBecameVisible", "OnBecameInvisible",
    "OnApplicationQuit", "OnApplicationFocus", "OnApplicationPause",
    "OnTransformChildrenChanged", "OnTransformParentChanged",
    "OnParticleSystemStopped", "OnParticleCollision",
    "OnAnimatorIK", "OnAnimatorMove",
})

# Unity base types: engine-dispatched, never resolvable in a source
# graph — their files' event functions are entry roots (the gdscript
# MANUAL_BASES / engine-base family).
UNITY_BASES = frozenset({"MonoBehaviour", "ScriptableObject"})

# test framework attributes -> entry roots (the rust #[test] analogue)
TEST_ATTRIBUTES = frozenset({"Test", "TestMethod", "Fact", "Theory"})

_last_ident = lambda text: last_ident(text, dollar=False)  # noqa: E731
_body_block = lambda node, src: body_block(node, src, "block")  # noqa: E731


def _modifiers(node, src: bytes) -> set[str]:
    return {
        node_text(ch, src)
        for ch in node.children
        if ch.type == "modifier"
    }


def _attributes(node, src: bytes) -> set[str]:
    """Attribute names on a declaration: every attribute_list's
    identifier children (``[Test]``, ``[TestCase(3)]``...)."""
    out: set[str] = set()
    for ch in node.children:
        if ch.type != "attribute_list":
            continue
        for sub in ch.children:
            if sub.type == "attribute":
                out.add(ident_child(sub, src, _IDENT_TYPES))
    return out


def _base_names(node, src: bytes) -> list[str]:
    """Base-list type names in declaration order (identifier children of
    base_list — interfaces included; generic args are inside the ident
    text and last_ident() trims to the final segment)."""
    for ch in node.children:
        if ch.type == "base_list":
            return [
                _last_ident(node_text(c, src))
                for c in ch.children
                if c.type in ("identifier", "qualified_name", "generic_name")
            ]
    return []


def _param_list(node, src: bytes) -> list[tuple[str, str]]:
    """[(name, type)] from a parameter_list: each parameter's identifier
    child is the name; the remaining type-ish text (minus modifiers and
    attributes) is the declared type, trimmed and on one line."""
    out: list[tuple[str, str]] = []
    for ch in node.children:
        if ch.type != "parameter":
            continue
        name = ident_child(ch, src, _IDENT_TYPES)
        parts = [
            node_text(c, src)
            for c in ch.children
            if c.type not in ("identifier", "modifier", "attribute_list",
                              ",", "parameter_modifier")
            and not node_text(c, src).strip() in ("ref", "out", "in",
                                                  "this", "params")
        ]
        typ = " ".join(p.strip() for p in parts if p.strip())
        out.append((name or "_", re.sub(r"\s+", " ", typ)))
    return out


def _return_type(node, src: bytes) -> str:
    """The declared return type: the child immediately before the name
    identifier that is neither modifier nor attribute_list (void/int/
    qualified/generic types); blank for constructors."""
    name_seen = False
    prev = ""
    for ch in node.children:
        if ch.type in ("identifier", "parameter_list"):
            name_seen = True
            break
        t = ch.type
        if t not in ("modifier", "attribute_list"):
            prev = node_text(ch, src)
    if name_seen and prev and prev.strip() not in ("(", ")"):
        return re.sub(r"\s+", " ", prev.strip())
    return ""


# ---- parse ----------------------------------------------------------------------


def parse(path: Path, rel: str) -> FileSym:
    """Registry entry point: one FileSym per .cs file."""
    src = path.read_bytes()
    line_starts = line_starts_of(src)
    fs = FileSym(path=rel, ext=".cs")
    # dynamic per-language facts (rust's _rust_* precedent): declared
    # namespaces incl. nested, type table (name, primary base, kind)
    fs._csharp_namespaces = set()
    fs._csharp_types = []
    _walk(fs, _PARSER.parse(src).root_node, src, line_starts, ())
    return fs


def _walk(fs: FileSym, node, src: bytes, line_starts, ns: tuple[str, ...]) -> None:
    for ch in node.children:
        t = ch.type
        if t == "using_directive":
            _take_using(fs, ch, src)
        elif t == "namespace_declaration":
            dotted = _dotted_name(ch, src)
            if dotted:
                full = ".".join(ns + dotted)
                fs._csharp_namespaces.add(full)
                _walk(fs, _decl_list(ch), src, line_starts, ns + dotted)
        elif t in _TYPE_DECLS:
            _take_type(fs, ch, src, line_starts)
        elif t.startswith("preproc_"):
            # file-level #if blocks can wrap usings/types/namespaces —
            # descend (the member-level twin lives in _take_type)
            _walk(fs, ch, src, line_starts, ns)
        # scoped usings inside namespaces ride the recursion above;
        # anything else (statements at odd spots) is not a declaration


def _decl_list(node):
    for ch in node.children:
        if ch.type == "declaration_list":
            return ch
    return node


def _dotted_name(ns_node, src: bytes) -> tuple[str, ...]:
    for ch in ns_node.children:
        if ch.type in ("qualified_name", "identifier"):
            return tuple(
                m.group(0) for m in
                re.finditer(r"[A-Za-z_]\w*", node_text(ch, src))
            )
    return ()


def _take_using(fs: FileSym, node, src: bytes) -> None:
    """using-directive facts: plain ``using X.Y;``/``using static X.Y;``
    record the namespace X.Y — resolution against the repo's declared
    namespaces happens in the liveness sweep (namespace-to-path match,
    python plain-import unit semantics; the issue's documented v1)."""
    dotted: list[str] = []
    for ch in node.children:
        if ch.type in ("qualified_name", "identifier"):
            dotted = [m.group(0) for m in
                      re.finditer(r"[A-Za-z_]\w*", node_text(ch, src))]
    if dotted:
        fs.imported_modules.add(".".join(dotted))


def _take_type(fs: FileSym, node, src: bytes, line_starts) -> None:
    name = ident_child(node, src, _IDENT_TYPES)
    if not name:
        return
    bases = _base_names(node, src)
    kind = _TYPE_KW[node.type]
    mods = _modifiers(node, src)
    partial = "partial" in mods
    fs._csharp_types.append((name, bases[0] if bases else "", kind))
    if not fs.class_name:
        # file header mirrors rust's first-impl-able-type convention:
        # class_map gets the first top-level type + its primary base
        fs.class_name = name
        fs.extends = bases[0] if bases else ""
        fs.is_tool = "static" in mods  # a static class is a utility
    for b in bases:
        if b in UNITY_BASES:
            fs._csharp_unity = True
    body = _decl_list(node)

    def _member(mch) -> None:
        t = mch.type
        if t in _TYPE_DECLS:  # nested types: flat model, same surface
            _take_type(fs, mch, src, line_starts)
        elif t == "method_declaration":
            _take_method(fs, mch, src, line_starts, name, partial)
        elif t == "constructor_declaration":
            _take_method(fs, mch, src, line_starts, name, partial)
        elif t == "field_declaration":
            _take_field(fs, mch, src)
        elif t == "property_declaration":
            _take_property(fs, mch, src)
        elif t.startswith("preproc_"):
            # #if/#elif/#else bodies carry real members (Unity's
            # UNITY_EDITOR blocks are the canonical case) — descend or
            # whole platform-conditional methods vanish from the surface
            for pch in mch.children:
                _member(pch)

    for ch in body.children:
        _member(ch)


def _take_method(fs: FileSym, node, src: bytes, line_starts,
                 cls: str, partial: bool) -> None:
    name = ident_child(node, src, _IDENT_TYPES) or cls  # ctor: class name
    attrs = _attributes(node, src)
    block = _body_block(node, src)
    if not block:  # expression-bodied member: the arrow clause is the body
        for ch in node.children:
            if ch.type == "arrow_expression_clause":
                block = node_text(ch, src)
                break
    fn = Func(
        path=fs.path,
        name=name,
        line=node_line(node, line_starts),
        body=block,
        params=_param_list(_first(node, "parameter_list"), src),
        ret=_return_type(node, src),
    )
    # member writes: `this.X = ...` assignments (qualified-only; bare
    # field writes are local/field ambiguous and stay out — header law)
    fn.writes = set(re.findall(r"\bthis\s*\.\s*([A-Za-z_]\w*)\s*(?:[-+*/]?=)", block))
    if attrs & TEST_ATTRIBUTES:
        fs.entry_hints.add(name)
    _merge(fs, fn)


def _merge(fs: FileSym, fn: Func) -> None:
    """Same-name merge, common.merge_func's law with the IO surface
    kept: first declaration wins the line; bodies concatenate so call
    edges from BOTH partial declarations survive; params/ret take the
    first non-empty; writes union (merge_func rebuilds the Func and
    would drop them)."""
    prev = fs.funcs.get(fn.name)
    if prev is None:
        fs.funcs[fn.name] = fn
        return
    prev.body = prev.body + "\n" + fn.body
    prev.params = prev.params or fn.params
    prev.ret = prev.ret or fn.ret
    prev.writes |= fn.writes


def _first(node, ty):
    for ch in node.children:
        if ch.type == ty:
            return ch
    return node


def _take_field(fs: FileSym, node, src: bytes) -> None:
    for ch in node.children:
        if ch.type != "variable_declaration":
            continue
        typ = ""
        for c in ch.children:
            if c.type in ("predefined_type", "identifier", "qualified_name",
                          "generic_name", "array_type", "nullable_type"):
                typ = re.sub(r"\s+", " ", node_text(c, src).strip())
                break
        for c in ch.children:
            if c.type == "variable_declarator":
                nm = ident_child(c, src, _IDENT_TYPES)
                if nm:
                    fs.members[nm] = typ


def _take_property(fs: FileSym, node, src: bytes) -> None:
    """Properties ride ``members`` (name -> declared type): they are
    field-like for liveness; accessors are not separate funcs."""
    nm = ident_child(node, src, _IDENT_TYPES)
    if not nm:
        return
    typ = _return_type(node, src)
    fs.members[nm] = typ


# ---- hooks (langsep REQUIRED surface) -------------------------------------------

MENTION_FLOOR = 2

# reflection/delegate dispatch surface: names resolved at runtime have
# no static call site — files using them classify their unreachable
# funcs "review" (the .gd call()/Callable() analogue)
DYNAMIC_HINT = re.compile(
    r"\.GetType\s*\(\s*\)|\.GetMethod\s*\(|\.Invoke\s*\("
    r"|Activator\.CreateInstance|Delegate\.CreateDelegate"
    r"|\btypeof\s*\("
)

# graph's dup normalizer strips these before hashing (issue #295): C#
# `//` line and `///` doc comments share the prefix. `/* */` block
# comments are multi-line and stay out of the line-prefix mechanism
# (same simplification cpp.py carries).
COMMENT_PREFIXES = ("//",)


def is_entry_exempt(name: str) -> bool:
    return name in UNITY_EVENT_FUNCTIONS


def unresolved_base_review(name: str) -> bool:
    """Interface implementations resolve through the interface's
    contract, not a call site: I-prefixed bases are the C# convention
    spelling of that (mirrors the .gd unresolved-base tail)."""
    return name.startswith("I") and len(name) > 1 and name[1].isupper()


def stand_in_review(fs: FileSym, name: str) -> bool:
    return False


def mention_review(name: str, mentions) -> bool:
    return mentions.get(name, 0) >= MENTION_FLOOR


_FIRST_TYPE_RE = re.compile(
    rb"\b(class|struct|interface|record|enum)\s+([A-Za-z_]\w*)"
)
_FIRST_BASE_RE = re.compile(
    rb"\b(class|struct|interface|record)\s+[A-Za-z_]\w*(?:<[^<>]*>)?"
    rb"\s*(?::\s*([A-Za-z_]\w*))"
)


def stat_tags(text: str) -> tuple[str, str]:
    """(class_name, extends) header sniff for the rescan stat stamp —
    first type declaration and its first base."""
    b = text.encode("utf-8", "replace")[:4096]
    m = _FIRST_TYPE_RE.search(b)
    if not m:
        return ("", "")
    name = m.group(2).decode()
    m2 = _FIRST_BASE_RE.search(b)
    base = m2.group(2).decode() if m2 else ""
    return (name, base)


def is_wiring_only(fs: FileSym) -> bool:
    """A file with no funcs AND no members declares only types (enum /
    interface contracts) — wiring for other files' calls, never dead on
    its own count."""
    return not fs.funcs and not fs.members


def counts_dead_share(fs: FileSym) -> bool:
    """Judge C1: the dead-share denominator counts .cs files (the .gd/
    .ts/.rs precedent — registered structural suffixes flag dead files)."""
    return fs.ext == ".cs"


# ---- entry rules (convention-based; the gdscript VIRTUALS mirror) ----------------


def _entry_main(fs: FileSym, ctx=None):
    """A static Main is the executable entry (the rust `fn main` rule)."""
    for name, fn in fs.funcs.items():
        if name == "Main":
            yield fn.key
            return


def _entry_tests(fs: FileSym, ctx=None):
    """[Test]/[TestMethod]/[Fact]/[Theory]-annotated methods (the rust
    #[test] rule; entry_hints carries the attribute harvest)."""
    return entry_keys(fs, sorted(fs.entry_hints))


def _entry_unity(fs: FileSym, ctx=None):
    """Unity engine event functions on MonoBehaviour/ScriptableObject
    bases: engine-dispatched with no textual call site — the gdscript
    _ready-lifecycle rooting, C# spelling."""
    if not getattr(fs, "_csharp_unity", False):
        return
    for name, fn in fs.funcs.items():
        if name in UNITY_EVENT_FUNCTIONS:
            yield fn.key


ENTRY_RULES = (_entry_main, _entry_tests, _entry_unity)


# ---- facts harvest (per-file, ctx-truth surfaces) --------------------------------


def harvest_facts(fs: FileSym, ctx) -> None:
    if fs.ext != ".cs":
        return
    # base-list names are type references: a base resolves in class_map
    # or mentions keep its methods in the review tier (mention floor)
    for _name, base, _kind in getattr(fs, "_csharp_types", ()):
        if base:
            ctx.referenced_names.add(base)


# ---- body scan --------------------------------------------------------------------

# non-call keyword heads shared with the ts/js scanner family
CSHARP_NON_CALLS = frozenset({
    "if", "while", "for", "foreach", "switch", "return", "throw", "new",
    "using", "lock", "catch", "else", "do", "in", "out", "ref", "base",
})

CSHARP_CALL_RE = re.compile(
    r"(?<![\w.$])((?:[A-Za-z_]\w*(?:\.|::))*)"
    r"([A-Za-z_]\w*)\s*(?:::{0,2}<[^<>]*>)?\s*\("
)


def _namespace_files(ctx) -> dict[str, list[str]]:
    """namespace -> rels of files declaring it (built lazily per ctx;
    partial declarations all land here — the partial merge surface)."""
    idx: dict[str, list[str]] = {}
    for rel in sorted(ctx.files):
        other = ctx.files[rel]
        for ns in sorted(getattr(other, "_csharp_namespaces", ())):
            idx.setdefault(ns, []).append(rel)
    return idx


def _type_files(ctx) -> dict[str, list[str]]:
    """type name -> rels declaring it (lazily per ctx). Wider than
    ctx.class_map on purpose: class_map carries only each file's HEADER
    type (fs.class_name), so `Helper.Tick()` inside a file whose header
    type is Player would otherwise miss Helper entirely — the e2e smoke
    caught exactly that leak into referenced_names."""
    idx: dict[str, list[str]] = {}
    for rel in sorted(ctx.files):
        other = ctx.files[rel]
        for tname, _base, _kind in sorted(getattr(other, "_csharp_types", ())):
            idx.setdefault(tname, []).append(rel)
    return idx


def scan_file(fs: FileSym, ctx) -> None:
    if fs.ext != ".cs":
        return
    if getattr(ctx, "_csharp_ns_index", None) is None:
        ctx._csharp_ns_index = _namespace_files(ctx)
        ctx._csharp_type_index = _type_files(ctx)
    for fn in fs.funcs.values():
        _scan_body_csharp(fs, fn, ctx)


def _scan_body_csharp(fs: FileSym, fn: Func, ctx) -> None:
    """Call wiring via ctx._emit_call (real edges + override mirroring —
    the rust/gdscript idiom): bare calls resolve same-file then type
    definers; qualified calls by receiver root — member/this/base ->
    same-file, type head -> definer file, namespace head -> the files
    declaring the namespace. referenced_names is the honest MISS arm
    only (a name with no resolvable home), never a substitute edge."""
    body = fn.body or ""
    if not body:
        return
    for m in CSHARP_CALL_RE.finditer(body):
        head, name = m.group(1), m.group(2)
        if name in CSHARP_NON_CALLS or (head and head.split(".")[0] in CSHARP_NON_CALLS):
            continue
        if not head:
            # bare call: same-file funcs, then any type named like it
            if name in fs.funcs:
                ctx._emit_call(fn.key, fs.path, name)
                continue
            trels = ctx._csharp_type_index.get(name, ())
            if trels:
                for trel in trels:
                    if name in ctx.files[trel].funcs:
                        ctx._emit_call(fn.key, trel, name)
                continue
            ctx.referenced_names.add(name)
            continue
        seg = head.rstrip(".:")
        root = seg.split(".")[0]
        if root in fs.members or root in ("this", "base"):
            # instance member call: same-file method surface
            if name in fs.funcs:
                ctx._emit_call(fn.key, fs.path, name)
            else:
                ctx.referenced_names.add(name)
        elif root in ctx._csharp_type_index:
            # static/type-qualified call: the declaring files' method
            for trel in ctx._csharp_type_index[root]:
                if name in ctx.files[trel].funcs:
                    ctx._emit_call(fn.key, trel, name)
        else:
            # namespace-qualified call (Game.Combat.Helper.Tick): the
            # last head segment may be the TYPE (try it first), else
            # walk head prefixes longest-first against the namespace
            # index — every file declaring the namespace could define
            # the callee (namespace-to-path, unit semantics)
            parts = seg.split(".")
            hit = False
            if len(parts) > 1 and parts[-1] in ctx._csharp_type_index:
                for trel in ctx._csharp_type_index[parts[-1]]:
                    if name in ctx.files[trel].funcs:
                        ctx._emit_call(fn.key, trel, name)
                        hit = True
            if not hit:
                for i in range(len(parts), 0, -1):
                    for ns_rel in ctx._csharp_ns_index.get(
                            ".".join(parts[:i]), ()):
                        if name in ctx.files[ns_rel].funcs:
                            ctx._emit_call(fn.key, ns_rel, name)
                            hit = True
            if not hit:
                ctx.referenced_names.add(name)


# ---- sweeps (ctx-truth, post-harvest) ----------------------------------------------
# No import_liveness_sweep, deliberately (cpp precedent): a C# using-
# directive is a NAME-RESOLUTION enabler, not a python-style module
# binding — liveness flows through the call edges scan_file builds
# (class_map + namespace-index resolution), never a blanket "every func
# of a using'd namespace is alive". Blanketing here self-immunizes
# same-namespace files: Player.cs `using Game.Combat` + Main.cs
# `namespace Game.Combat` kept EVERYTHING alive and dead_code returned
# zero rows (caught in the e2e smoke). using-static is the same story
# by v1's documented simplification.
_PASS_FACTS = harvest_facts
