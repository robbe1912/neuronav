"""C++ extractor: tree-sitter-cpp front-end + stdlib macro-surface pass.

Strategy (issue #13, spec ``.team_scratch/cpp_spec.md`` §1–§3): tree-sitter
parses definitions/classes/members/enums/includes via ordered queries — it
is error-tolerant through GDCLASS-style macro damage (the grammar recovers
with a zero-width MISSING token; every real node around it still captures) —
while Godot's line-shaped registration macros (``ClassDB::bind_method``,
``ADD_SIGNAL``, ``ADD_PROPERTY``, ``BIND_ENUM_CONSTANT``, ``GDVIRTUALn``,
``emit_signal``) are harvested by stdlib regex, the one construct class
where regex is the right tool (spec §1 option (b): flat, line-shaped,
reliably matched; NOT a general C++ front-end).

The two pinned wheels (tree-sitter==0.26.0, tree-sitter-cpp==0.23.4) are
the sanctioned C++ front-end dependency — see the AGENTS.md deps line.

Determinism: every capture list is sorted by ``start_byte`` before use, so
file order, not dict order, drives extraction; same bytes -> same FileSym.
Method overloads collapse onto the first definition in file order (FileSym
keys funcs by name).

v1 surface (spec §3): defs/classes/members/enums/includes/registration
macros. General call-graph edges and writes/mut_params are deferred (v1.1)
— liveness flows from registration + virtuals + string-literal dispatch.
"""

import bisect
import re
from pathlib import Path
from typing import Iterable, NamedTuple

from tree_sitter import Language, Parser, Query, QueryCursor

from extractors.model import FileSym, Func

# suffixes handled here (extractors/__init__.py registry maps them)
CPP_EXTS = frozenset({".h", ".hpp", ".cpp", ".cc", ".cxx"})

CPP_LANG = Language(__import__("tree_sitter_cpp").language())
_PARSER = Parser(CPP_LANG)

# NOTE: never read Node.start_point / Node.end_point anywhere in this
# module — py-tree-sitter 0.26.0 has a Point refcount bug (tree-sitter
# issue #472, fix unreleased): a Point read corrupts the heap and the
# process dies at a later allocation with a bare native exit (5 /
# 0xC0000005). Line numbers are derived by bisect over newline offsets
# instead (workaround endorsed by issue #487).

# Ordered capture set (spec §3). In-class method definitions carry a
# field_identifier name, free functions an identifier, qualified
# Class::method definitions a qualified_identifier — three patterns, one
# query. Method DECLARATIONS (no body) are field_declarations and never
# match, so declaration/definition pairing yields one Func per real def.
_QUERY_SRC = """
(function_definition declarator: (function_declarator declarator: (identifier) @fn.name)) @fn.def
(function_definition declarator: (function_declarator declarator: (field_identifier) @fn.name)) @fn.def
(function_definition declarator: (function_declarator declarator: (qualified_identifier) @fnq.name)) @fnq.def
(function_definition declarator: (pointer_declarator (function_declarator declarator: (identifier) @fn.name))) @fn.def
(function_definition declarator: (pointer_declarator (function_declarator declarator: (qualified_identifier) @fnq.name))) @fnq.def
(function_definition declarator: (pointer_declarator (function_declarator declarator: (field_identifier) @fn.name))) @fn.def
(function_definition declarator: (reference_declarator (function_declarator declarator: (identifier) @fn.name))) @fn.def
(function_definition declarator: (reference_declarator (function_declarator declarator: (field_identifier) @fn.name))) @fn.def
(function_definition declarator: (function_declarator declarator: (qualified_identifier) @fnq.name)) @fnq.def
(function_definition declarator: (function_declarator declarator: (destructor_name) @fn.name)) @fn.def
(function_definition declarator: (function_declarator declarator: (operator_name) @fn.name)) @fn.def
(function_definition declarator: (operator_cast) @fn.name) @fn.def
(class_specifier name: (type_identifier) @cls.name) @cls.def
(struct_specifier name: (type_identifier) @cls.name) @cls.def
(union_specifier name: (type_identifier) @cls.name) @cls.def
(base_class_clause (type_identifier) @cls.base)
(base_class_clause (template_type name: (type_identifier) @cls.base))
(field_declaration declarator: (field_identifier) @member.name) @member.decl
(enum_specifier name: (type_identifier) @enum.name) @enum.def
(enumerator name: (identifier) @const.name) @const.def
(preproc_include path: (string_literal) @include.path) @include.def
(call_expression function: (field_expression field: (field_identifier) @call.field)) @call.def
(call_expression function: (qualified_identifier) @callq.name) @callq.def
(call_expression function: (template_function name: (identifier) @callt.name)) @callt.def
(call_expression function: (field_expression field: (template_method name: (field_identifier) @calltf.name))) @calltf.def
(pointer_expression argument: (identifier) @fref.name) @fref.def
(pointer_expression argument: (field_expression) @frefq.name) @frefq.def
(pointer_expression argument: (qualified_identifier) @frefq.name) @frefq.def
(new_expression type: (type_identifier) @new.type) @new.def
(declaration declarator: (identifier) @gvar.name) @gvar.def
(declaration declarator: (init_declarator declarator: (identifier) @gvar.name)) @gvar.def
(type_definition declarator: (type_identifier) @td.name) @td.def
(alias_declaration name: (type_identifier) @al.name) @al.def
"""
_QUERY = Query(CPP_LANG, _QUERY_SRC)

# ---- stdlib macro-surface pass (line-shaped registration macros only) ----------
# Coverage measured on the engine tree (3240 files, 2.4 ms/file for all
# regex passes): bind_method 15723/15729, bind_static 151/156 (unmatched
# are the template definition + macro wrappers — correct to skip),
# bind_vararg 18/18, ADD_PROPERTY 3682/3685 (rest are #define rows),
# ADD_SIGNAL 791/792, BIND_ENUM 4816/4816.
# ClassDB::bind_method(D_METHOD("x", "a"), &C::m) -> reflected/script dispatch.
# Pointer targets appear as &C::m, &Outer::Inner::m, and cast-disambiguated
# `(Vector<uint8_t> (FileAccess::*)(int64_t) const) & FileAccess::get_buffer`
# (core/io/file_access.cpp) — the cast may sit on either side of the & and
# nests one paren level (fn-ptr params), so both sides absorb a balanced
# cast before the final `([\w:]+)::method` lands on the real target.
_CAST = r"(?:\((?:[^()]|\([^()]*\))*\)\s*)?"
_PTR = rf"{_CAST}&\s*{_CAST}([\w:]+)::(\w+)"
RE_BIND_METHOD = re.compile(rf"ClassDB::bind_method\(\s*D_METHOD\(\s*\"([^\"]+)\"[^)]*\)\s*,\s*{_PTR}")
# legacy string-name form: ClassDB::bind_method("x", &C::m)
RE_BIND_METHOD_LEGACY = re.compile(rf"ClassDB::bind_method\(\s*\"([^\"]+)\"\s*,\s*{_PTR}")
# ClassDB::bind_static_method(<class>, D_METHOD("x"), &C::m) — the class arg
# is a quoted string, an expression (get_class_static()), or a bare ident
RE_BIND_STATIC = re.compile(
    rf"bind_static_method\(\s*[^,]+,\s*D_METHOD\(\s*\"([^\"]+)\"[^)]*\)\s*,\s*{_PTR}"
)
# legacy string-pair static form:
# bind_static_method("C", "x", &C::m) (editor/gui/editor_inspector.cpp)
RE_BIND_STATIC_LEGACY = re.compile(rf"bind_static_method\(\s*\"([^\"]+)\"\s*,\s*\"([^\"]+)\"\s*,\s*{_PTR}")
# table-wrapper form (core/variant/variant_call.cpp):
# bind_static_method(C, name, sarray(...), varray(...))
RE_BIND_STATIC_PLAIN = re.compile(r"bind_static_method\(\s*(\w+)\s*,\s*(\w+)\s*,\s*sarray")
# ClassDB::bind_vararg_method(FLAGS, "x", &C::m, ...) — name is a plain string
RE_BIND_VARARG = re.compile(rf"ClassDB::bind_vararg_method\(\s*[^,]+,\s*\"([^\"]+)\"\s*,\s*{_PTR}")
# ADD_SIGNAL(MethodInfo("sig", ...)) and StringName-wrapped spellings;
# constant-name signals (6 rows repo-wide) are v1.1, not silently mis-harvested
RE_ADD_SIGNAL_STR = re.compile(r"ADD_SIGNAL\(\s*MethodInfo\(\s*\"(\w+)\"")
RE_ADD_SIGNAL_SN = re.compile(r"ADD_SIGNAL\(\s*MethodInfo\(\s*(?:Core|Scene)StringName\(\s*(\w+)")
# ADD_PROPERTY(PropertyInfo(Variant::T, "p", ...), "set_p", "get_p").
# PropertyInfo args can nest parens two levels (vformat(...)), accessors
# may be empty, and property names may contain '/' ("transform/notify"),
# so the call is balanced-scanned (see _call_span) and split into head
# (type, prop) / tail (setter, getter) instead of one flat pattern.
RE_PROP_HEAD = re.compile(r"\s*PropertyInfo\(\s*([^,]+),\s*\"([^\"]+)\"")
RE_PROP_TAIL = re.compile(r",\s*\"(\w*)\"\s*,\s*\"(\w*)\"\s*\)\s*$")
# BIND_ENUM_CONSTANT(X) and BIND_ENUM_CONSTANT(Qualified::X)
RE_BIND_ENUM = re.compile(r"BIND_ENUM_CONSTANT\(\s*([\w:]+)\s*\)")
# bind_integer_constant(expr, "ENUM", CONST) — first arg is an expression
RE_BIND_INT_CONST = re.compile(r"bind_integer_constant\(\s*[^,]+,\s*\"([^\"]+)\"\s*,\s*([\w:]+)\s*[,)]")
# GDVIRTUAL0..10 / _R / _C / _REQ family (core/object/make_virtuals.py).
# A return type may lead the arg list (GDVIRTUALnR(RET, name, ...)) and
# _COMPAT shims carry two names (shim, real) — both engine-dispatched.
# These macro declarations carry no trailing ';', so harvest is scoped by
# balanced parens over the call, never by statement end. Arity is 0..10 —
# two-digit forms (GDVIRTUAL10R_REQUIRED) exist in the physics extensions.
RE_GDV_START = re.compile(r"\bGDVIRTUAL[0-9]+[A-Z_]*\(")
RE_GDV_NAME = re.compile(r"\b(_[a-z_]\w*)")
# emit_signal(CoreStringName(x)) / emit_signal("x") / bare emit_signal(x)
RE_EMIT = re.compile(r"emit_signal\(\s*(?:CoreStringName\()?\s*\"?(\w+)")
# CoreStringName("x") / StringName("x") anywhere — dispatch-name literals
RE_NAME_LIT = re.compile(r"\b(?:CoreStringName|StringName)\(\s*\"(\w+)\"")

# memnew(T) / memnew_arr(T) — the engine's heap construction idiom; the
# macro's argument names the constructed type (scan_calls folds it in as
# an instantiation site alongside `new T`)
RE_MEMNEW = re.compile(r"\bmemnew(?:_arr)?\s*\(\s*([A-Za-z_]\w*)")
# files whose registration macros imply dynamic dispatch — feeds the
# dead-code review-vs-likely tier for C++ (graph.dead_code)
CPP_DYNAMIC_RE = re.compile(r"ClassDB::|GDVIRTUAL|ADD_SIGNAL|ADD_PROPERTY|emit_signal")
# dead-tier mention-count corroboration floor (issue #20): a cpp name
# whose raw-text mentions across the corpus reach this count (its own
# definition plus at least one more — an unresolved same-name call site
# the ambiguity guard dropped, a comment, a string dispatch table) is
# wired somewhere the static pass cannot see, so 'likely' overclaims its
# deadness and the row drops to 'review'. Consumed by graph.dead_code.
CPP_MENTION_FLOOR = 2

# C++ Object virtuals dispatched by the engine (spec §2). GDVIRTUAL
# declarations are NOT here — they are harvested per-repo into
# ctx.cpp_gdvirtuals because the set differs by engine version. The static
# ClassDB registration hook rides along: every registered class defines it,
# and leaving it unrooted would read dead in every engine file.
CPP_VIRTUALS = frozenset(
    {
        "_set",
        "_get",
        "_get_property_list",
        "_validate_property",
        "_property_can_revert",
        "_property_get_revert",
        "_notification",
        "_to_string",
        "_bind_methods",
    }
)

# node types that can carry a parameter/return type spelling
_TYPE_NODES = frozenset(
    {
        "type_identifier",
        "primitive_type",
        "type_descriptor",
        "sized_type_specifier",
        "qualified_type_identifier",
        "template_type",
        "dependent_type",
    }
)


class Bind(NamedTuple):
    """One ClassDB registration: reflected name, class, method, 1-based line."""

    name: str
    cls: str
    method: str
    line: int


class Prop(NamedTuple):
    """One ADD_PROPERTY wiring: property, type, setter, getter, line."""

    prop: str
    ptype: str
    setter: str
    getter: str
    line: int


class Virt(NamedTuple):
    """One GDVIRTUALn declaration name, 1-based line."""

    name: str
    line: int


def _call_span(text: str, open_idx: int) -> int:
    """Index just past the balanced ``)`` for the ``(`` at open_idx.

    Registration macro calls carry nested parens and no trailing ``;``
    (GDVIRTUAL declarations) — statement-scoped regex runs past the call.
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


def _strip_template(name: str) -> str:
    """Drop a balanced ``<...>`` suffix: ``Base<int>`` -> ``Base``.

    Depth-counted, never crosses a ``;`` (both impossible in a type
    position and a cheap guard against walking into broken parses).
    """
    lt = name.find("<")
    if lt < 0:
        return name
    depth = 0
    for i in range(lt, len(name)):
        c = name[i]
        if c == "<":
            depth += 1
        elif c == ">":
            depth -= 1
            if depth == 0:
                return name[:lt].strip()
        elif c == ";" or (c == "(" and depth == 0):
            break
    return name[:lt].strip()

def harvest_registration(text: str) -> dict[str, list]:
    """All registration-macro facts from one file's text (stdlib regex).

    Used by parse() (file-local entry hints, signals, property members)
    and by graph._wire_cpp() (registration edges + the repo-wide GDVIRTUAL
    set), so both passes see identical facts. Deterministic: document
    order per family, fixed family order.
    """
    def line_of(m) -> int:
        return text.count("\n", 0, m.start()) + 1

    binds: list[Bind] = []
    for m in RE_BIND_METHOD.finditer(text):
        binds.append(Bind(m.group(1), m.group(2), m.group(3), line_of(m)))
    for m in RE_BIND_METHOD_LEGACY.finditer(text):
        binds.append(Bind(m.group(1), m.group(2), m.group(3), line_of(m)))
    for m in RE_BIND_STATIC.finditer(text):
        binds.append(Bind(m.group(1), m.group(2), m.group(3), line_of(m)))
    for m in RE_BIND_STATIC_LEGACY.finditer(text):
        binds.append(Bind(m.group(2), m.group(1), m.group(4), line_of(m)))
    for m in RE_BIND_STATIC_PLAIN.finditer(text):
        binds.append(Bind(m.group(2), m.group(1), m.group(2), line_of(m)))
    for m in RE_BIND_VARARG.finditer(text):
        binds.append(Bind(m.group(1), m.group(2), m.group(3), line_of(m)))

    props: list[Prop] = []
    for m in re.finditer(r"\bADD_PROPERTY\(", text):
        end = _call_span(text, m.end() - 1)
        call = text[m.end():end]
        head = RE_PROP_HEAD.search(call)
        tail = RE_PROP_TAIL.search(call)
        if head is None or tail is None:
            continue  # #define rows and non-PropertyInfo forms
        props.append(
            Prop(
                head.group(2),
                head.group(1).strip(),
                tail.group(1),
                tail.group(2),
                line_of(m),
            )
        )

    gdvirtuals: list[Virt] = []
    for m in RE_GDV_START.finditer(text):
        end = _call_span(text, m.end() - 1)
        for name in RE_GDV_NAME.findall(text[m.end():end]):
            gdvirtuals.append(Virt(name, line_of(m)))

    signals: list[str] = []
    for rx in (RE_ADD_SIGNAL_STR, RE_ADD_SIGNAL_SN):
        signals.extend(m.group(1) for m in rx.finditer(text))

    return {"binds": binds, "props": props, "gdvirtuals": gdvirtuals, "signals": signals}


def _find_ident(node):
    """First identifier/field_identifier in subtree (params sit under
    reference/pointer declarators: ``const String &p_x``)."""
    if node.type in ("identifier", "field_identifier"):
        return node
    for child in node.children:
        hit = _find_ident(child)
        if hit is not None:
            return hit
    return None


def _type_text(src: bytes, node) -> str:
    if node is None:
        return ""
    return src[node.start_byte:node.end_byte].decode("utf8", "replace").strip()


def _signature(src: bytes, fd) -> tuple[list[tuple[str, str]], str]:
    """[(name, type)] params + declared return type of a function_definition."""
    params: list[tuple[str, str]] = []
    ret = ""
    decl = None
    for child in fd.children:
        if child.type == "function_declarator":
            decl = child
        elif child.type in _TYPE_NODES and not ret:
            ret = _type_text(src, child)
    if decl is not None:
        for part in decl.children:
            if part.type != "parameter_list":
                continue
            for pd in part.children:
                if pd.type != "parameter_declaration":
                    continue
                ident = _find_ident(pd)
                ty = ""
                for pc in pd.children:
                    if pc.type in _TYPE_NODES:
                        ty = _type_text(src, pc)
                        break
                params.append(
                    (_type_text(src, ident) if ident is not None else "", ty)
                )
    return params, ret


def parse(path: Path, rel: str) -> FileSym:
    """Parse one C++ file (pure: reads only this file)."""
    fs = FileSym(path=rel, ext=path.suffix.lower())
    text = path.read_text(encoding="utf-8", errors="replace")
    src = text.encode("utf-8")
    tree = _PARSER.parse(src)
    caps = QueryCursor(_QUERY).captures(tree.root_node)
    line_starts = [0] + [i + 1 for i, b in enumerate(src) if b == 0x0A]

    def bytewise(key: str) -> list:
        return sorted(caps.get(key, ()), key=lambda n: n.start_byte)

    fn_defs = bytewise("fn.def") + bytewise("fnq.def")
    fn_names = bytewise("fn.name")
    fnq_names = bytewise("fnq.name")

    for fd in fn_defs:
        nm = next(
            (n for n in fn_names if fd.start_byte <= n.start_byte < fd.end_byte),
            None,
        ) or next(
            (n for n in fnq_names if fd.start_byte <= n.start_byte < fd.end_byte),
            None,
        )
        if nm is None:
            continue  # def shape with no captured name arm
        name = src[nm.start_byte:nm.end_byte].decode("utf8", "replace")
        name = name.rsplit("::", 1)[-1]  # qualified Class::method -> method
        if not name or name in fs.funcs:
            continue  # overloads collapse; first definition in file order wins
        params, ret = _signature(src, fd)
        fs.funcs[name] = Func(
            path=rel,
            name=name,
            line=bisect.bisect_right(line_starts, fd.start_byte),
            body=src[fd.start_byte:fd.end_byte].decode("utf8", "replace"),
            params=params,
            ret=ret,
        )

    # primary class: first class_specifier WITH a body; extends = its first
    # base (Godot headers hold one public engine-facing class)
    cls_defs = bytewise("cls.def")
    cls_names = bytewise("cls.name")
    cls_bases = bytewise("cls.base")
    bodies = []  # (node, name, base, is_class) in document order
    for cd in cls_defs:
        if not any(c.type == "field_declaration_list" for c in cd.children):
            continue  # forward declaration — no edge, no class_map entry
        nm = next(
            (n for n in cls_names if cd.start_byte <= n.start_byte < cd.end_byte),
            None,
        )
        if nm is None:
            continue
        base = next(
            (b for b in cls_bases if cd.start_byte <= b.start_byte < cd.end_byte),
            None,
        )
        bodies.append((
            cd,
            src[nm.start_byte:nm.end_byte].decode("utf8", "replace"),
            _strip_template(src[base.start_byte:base.end_byte].decode("utf8", "replace"))
            if base is not None else "",
            cd.type == "class_specifier",
        ))
    if bodies:
        # a class beats a struct when both carry bodies (engine headers
        # open with POD structs and close with the registered class);
        # structs/unions still surface when nothing else declares a body
        pick = next((b for b in bodies if b[3]), bodies[0])
        fs.class_name = pick[1]
        fs.extends = pick[2]

    # data members (method declarations never carry a field_identifier).
    # visibility: the nearest preceding access_specifier in the enclosing
    # body decides; absent one, class defaults private, struct/union public
    member_names = bytewise("member.name")
    for md in bytewise("member.decl"):
        nm = next(
            (n for n in member_names if md.start_byte <= n.start_byte < md.end_byte),
            None,
        )
        if nm is None:
            continue
        name = src[nm.start_byte:nm.end_byte].decode("utf8", "replace")
        ty = ""
        for c in md.children:
            if c.type in _TYPE_NODES:
                ty = _type_text(src, c)
                break
        fs.members[name] = ty
        fl = md.parent
        if fl is not None and fl.type == "field_declaration_list":
            access = (
                "private"
                if fl.parent is not None and fl.parent.type == "class_specifier"
                else "public"
            )
            for c in fl.children:
                if c.start_byte >= md.start_byte:
                    break
                if c.type == "access_specifier":
                    access = src[c.start_byte:c.end_byte].decode("utf8", "replace")
            if access == "private":
                fs.private_members.add(name)

    # enum surface: raw enumerators + BIND_ENUM_CONSTANT / integer constants
    for n in bytewise("const.name"):
        fs.consts.setdefault(src[n.start_byte:n.end_byte].decode("utf8", "replace"), "")
    for m in RE_BIND_ENUM.finditer(text):
        fs.consts.setdefault(m.group(1).rsplit("::", 1)[-1], "")
    for m in RE_BIND_INT_CONST.finditer(text):
        fs.consts.setdefault(m.group(2).rsplit("::", 1)[-1], "")

    # quoted includes only — system <...> includes are system_lib_literal
    # nodes the query never captures
    for n in bytewise("include.path"):
        inc = src[n.start_byte:n.end_byte].decode("utf8", "replace").strip('"')
        if inc:
            fs.imported_modules.add(inc)

    # string dispatch names (alive, no static edge)
    for m in RE_EMIT.finditer(text):
        fs.name_literals.add(m.group(1))
    for m in RE_NAME_LIT.finditer(text):
        fs.name_literals.add(m.group(1))

    # registration surface -> signals, property members, entry hints
    reg = harvest_registration(text)
    for sig in reg["signals"]:
        fs.signals.add(sig)
    for p in reg["props"]:
        fs.members.setdefault(p.prop, p.ptype)
    for b in reg["binds"]:
        fs.entry_hints.add(b.method)
    for p in reg["props"]:
        fs.entry_hints.add(p.setter)
        fs.entry_hints.add(p.getter)
    for v in reg["gdvirtuals"]:
        fs.entry_hints.add(v.name)

    # file-scope variables: only declarations whose parent is the
    # translation unit (or a namespace) — locals inside function bodies
    # also parse as declarations and must stay out
    gvar_names = bytewise("gvar.name")
    for gd in bytewise("gvar.def"):
        scope = gd.parent.type if gd.parent is not None else ""
        if scope not in ("translation_unit", "namespace_definition"):
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

    # typedefs + using-aliases: name -> target type (template args
    # stripped); target rides the declaration text between keyword and name
    for key, prefix in (("td", "typedef "), ("al", "")):
        names = bytewise(f"{key}.name")
        for d in bytewise(f"{key}.def"):
            nm = next(
                (n for n in names if d.start_byte <= n.start_byte < d.end_byte),
                None,
            )
            if nm is None:
                continue
            name = src[nm.start_byte:nm.end_byte].decode("utf8", "replace")
            decl = src[d.start_byte:d.end_byte].decode("utf8", "replace").strip()
            if key == "td":
                body = decl[len(prefix):]
                target = body[: body.rfind(name)].strip() if name in body else ""
            else:
                target = decl.split("=", 1)[-1].rstrip(";").strip() if "=" in decl else ""
            fs.aliases[name] = _strip_template(target)

    return fs


def scan_calls(path: Path, rel: str) -> list[dict]:
    """Call/reference/instantiation sites for graph edge minting.

    One dict per site, start_byte-ordered (deterministic): {name, line,
    kind}. kind: call (obj.method()), callq (A::b()), callt (f<T>()),
    calltf (obj.m<T>()), fref (&fn), frefq (&C::fn), new (new T).
    memnew(T)/memnew_arr(T) — the engine idiom — folds in as new-kind.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    src = text.encode("utf-8")
    tree = _PARSER.parse(src)
    caps = QueryCursor(_QUERY).captures(tree.root_node)
    line_starts = [0] + [i + 1 for i, b in enumerate(src) if b == 0x0A]
    sites: list[dict] = []
    for kind, key in (
        ("call", "call.field"),
        ("callq", "callq.name"),
        ("callt", "callt.name"),
        ("calltf", "calltf.name"),
        ("fref", "fref.name"),
        ("frefq", "frefq.name"),
        ("new", "new.type"),
    ):
        for n in sorted(caps.get(key, ()), key=lambda x: x.start_byte):
            sites.append({
                "name": src[n.start_byte:n.end_byte].decode("utf8", "replace"),
                "line": bisect.bisect_right(line_starts, n.start_byte),
                "kind": kind,
            })
    for m in RE_MEMNEW.finditer(text):
        sites.append({
            "name": m.group(1),
            "line": text.count("\n", 0, m.start()) + 1,
            "kind": "new",
        })
    sites.sort(key=lambda s: (s["line"], s["name"], s["kind"]))
    return sites
    return fs


# funcs the engine/runtime may invoke without any static call site
# (spec §3: ENTRY_RULES = [classdb_bound, gdvirtual_overrides,
# object_virtuals])


def _entry_classdb(fs: FileSym, ctx) -> Iterable[str]:
    """ClassDB-bound methods + ADD_PROPERTY accessors: reflected or
    property-system dispatch — no static caller exists."""
    if fs.ext not in CPP_EXTS:
        return
    for nm in sorted(fs.entry_hints):
        fn = fs.funcs.get(nm)
        if fn is not None:
            yield fn.key


def _entry_virtuals(fs: FileSym, ctx) -> Iterable[str]:
    """Object virtual overrides (_notification analogues), engine-called."""
    if fs.ext not in CPP_EXTS:
        return
    for nm in sorted(CPP_VIRTUALS):
        fn = fs.funcs.get(nm)
        if fn is not None:
            yield fn.key


def _entry_gdvirtual(fs: FileSym, ctx) -> Iterable[str]:
    """GDVIRTUAL script-virtual overrides. Declarations may sit in a
    different file than the overriding definition, so the repo-wide name
    set is harvested during graph build and carried on the ctx Graph as
    ``cpp_gdvirtuals``; same-file declarations also land in entry_hints."""
    if fs.ext not in CPP_EXTS:
        return
    for nm in sorted(getattr(ctx, "cpp_gdvirtuals", ())):
        fn = fs.funcs.get(nm)
        if fn is not None:
            yield fn.key


ENTRY_RULES = [_entry_classdb, _entry_virtuals, _entry_gdvirtual]
