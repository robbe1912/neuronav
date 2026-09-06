"""swmg-nav structural layer: function/signal graph, dead code, duplicates.

Parses GDScript + .tscn from the checkout into an in-memory graph (language
parsers live in extractors/, dispatched via the suffix registry):
- functions with bodies (indentation-delimited)
- call edges: ClassName.func (via class_name map), bare same-file calls
- var edges: receiver.member where the destination file declares the member
- signal edges: emit sites -> connect() handlers, plus .tscn [connection] blocks
- entry roots: per-language entry-point rules ship with each extractor
  (extractors/*/ENTRY_RULES) — GDScript: autoloads, virtuals
  (_ready/_process/...), connected handlers, GUT test_*, string-callable
  references (call("x"), Callable(self, "x")), scripts referenced by
  load()/preload() string literals in bodies
- dead code = functions unreachable from roots (two confidence tiers)
- duplicates = normalized-body hashes + cosine-similar function vectors

Zero non-vendor deps beyond nav (reuses its file walk + embed).
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict, deque

import nav
from extractors import registry_for
from extractors.model import FileSym, Func  # noqa: F401  (re-export)
# language fact needed by the dead-code tier heuristic (native dispatch names)
from extractors.gdscript import VIRTUALS, GUT_ROOTS, ADDON_VIRTUALS, MANUAL_BASES, parse_gd, parse_tscn

# ---- constants ---------------------------------------------------------------
# Language-owned constants and entry-point rules (VIRTUALS, GUT_ROOTS,
# ADDON_VIRTUALS, MANUAL_BASES, ENTRY_RULES) live in extractors/gdscript.py;
# VIRTUALS is imported for the dead-code tier heuristic only.

QUALIFIED_CALL_RE = re.compile(r"(?<![\w.$])([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*\(")
# receiver.member access that is NOT a call: member name lowercase-initial
# (vars), negative lookahead rejects optional-whitespace-then-paren
MEMBER_ACCESS_RE = re.compile(
    r"(?<![\w.$])([A-Za-z_]\w*)\s*\.\s*([a-z_]\w*)\b(?!\s*\()"
)
# "res://...something.gd" string literals in bodies: dynamically loaded
# scripts whose funcs must count as alive
RES_LOAD_RE = re.compile(r"res://([\w/.-]+\.gd)")
BARE_CALL_RE = re.compile(r"(?<![\w.$])([A-Za-z_]\w*)\s*\(")

# ---- python scanning (companion to extractors/python.py) ----------------------
PY_ATTR_CALL_RE = re.compile(r"(?<![\w.$])([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*\(")
PY_CHAIN_CALL_RE = re.compile(
    r"(?<![\w.$])([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*\("
)
PY_BARE_CALL_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*\(")
# `name: Type` params and `x = Klass(` locals (capitalized = user
# classes); hints keep a flat generic subscript (dict[str, Widget]) so
# subscript access can resolve the value classes inside
PY_PARAM_TYPED_RE = re.compile(r"[(,]\s*([A-Za-z_]\w*)\s*:\s*([A-Za-z_]\w*(?:\[[^\]=]+\])?)")
PY_LOCAL_NEW_RE = re.compile(r"(?<![\w.!=<>])([A-Za-z_]\w*)\s*=(?!=)\s*([A-Z]\w*)\s*\(")
# with/async-with target bound from a constructor: with Session() as s
PY_WITH_AS_RE = re.compile(
    r"(?<![\w.])(?:async\s+)?with\s+([A-Z]\w*)\s*\([^()]*\)\s+as\s+([A-Za-z_]\w*)"
)
# annotated local: local: Widget = ... / pairs: dict[str, Widget] = ...
PY_ANNOT_ASSIGN_RE = re.compile(
    r"(?<![\w.])([A-Za-z_]\w*)\s*:\s*([A-Za-z_]\w*(?:\[[^\]=]+\])?)\s*=(?!=)"
)
# box[k].method( / self.box[k].method( — subscript access into a hint
PY_SUBSCRIPT_CALL_RE = re.compile(
    r"(?<![\w.$])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\[[^\]]*\]\s*\.\s*([A-Za-z_]\w*)\s*\("
)
# local bound from an imported call: extractor = registry_for(...)
PY_MODULE_ASSIGN_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*=\s*([a-z_]\w*)\s*\(")
# imported_call(args).method( — registry_for(path.suffix).parse(...)
PY_RESULT_CALL_RE = re.compile(r"([A-Za-z_]\w*)\s*\(([^()]*)\)\s*\.\s*([A-Za-z_]\w*)\s*\(")
PY_NON_CALLS = {
    "if", "for", "while", "elif", "return", "assert", "del", "print",
    "lambda", "not", "await", "with", "except", "raise", "yield",
    "in", "is", "and", "or", "nonlocal", "global", "import", "from",
    "len", "range", "str", "int", "float", "bool", "list", "dict", "set",
    "tuple", "isinstance", "issubclass", "type", "sorted", "reversed",
    "min", "max", "sum", "enumerate", "zip", "open", "getattr", "setattr",
    "hasattr", "repr", "abs", "any", "all", "filter", "map", "dir", "id",
    "hash", "iter", "next", "vars", "format", "bytes", "super", "exit",
    "quit", "help", "input", "round", "divmod", "pow", "chr", "ord", "hex",
    "oct", "bin", "frozenset", "bytearray", "complex", "object",
    "staticmethod", "classmethod", "property", "dataclass", "field",
    "Exception", "ValueError", "TypeError", "RuntimeError", "KeyError",
    "IndexError", "OSError", "IOError", "StopIteration", "FileNotFoundError",
    "NotImplementedError",
}
EMIT_RE = re.compile(r"emit_signal\(\s*[\"'](\w+)[\"']|([A-Za-z_]\w*)\.emit\(")
# .tres/.res ext_resource lines: type="Script" path="res://..."
TRES_SCRIPT_RE = re.compile(r'ext_resource\s+type="Script"[^>]*path="([^"]+)"')
# signal wiring via direct method references: sig.connect(_handler)
CONNECT_METHOD_RE = re.compile(r"\.(?:connect|disconnect|is_connected)\(\s*([A-Za-z_]\w*)")
# typed locals + params anywhere in a body: `name: Type`
PARAM_TYPED_RE = re.compile(r"(?<![\w.])(\w+)\s*:\s*([A-Z]\w*)")
# cast-then-call: (node as CameraShake).shake(  ->  Type.method(
AS_CAST_CALL_RE = re.compile(r"as\s+([A-Z]\w*)\)\s*\.\s*([A-Za-z_]\w*)\s*\(")
CONNECT_RE = re.compile(r"\.connect\(|Callable\(")
STRING_NAME_RE = re.compile(r"[\"']([A-Za-z_]\w*)[\"']")
# dynamic-dispatch string harvest: method names in .call()/.rpc()/
# has_method() string args and Callable(obj, "m") constructions have
# runtime-typed receivers — keep same-named funcs alive, no static edge
DISPATCH_STR_RE = re.compile(
    r'\.(?:call|call_deferred|callv|rpc|rpc_id|rpc_config|has_method)'
    r'\(\s*&?"([a-z_]\w*)"'
)
# receiver-less dispatch on implicit self: bare call_deferred("x") / rpc("x")
BARE_DISPATCH_STR_RE = re.compile(
    r'(?<![\w.])(?:call|call_deferred|callv|rpc|rpc_id|has_method)'
    r'\(\s*&?"([a-z_]\w*)"'
)
STRINGNAME_LIT_RE = re.compile(r'&"([a-z_]\w{3,})"')
CALLABLE_TWO_RE = re.compile(
    r'Callable\s*\(\s*[\w.]+\s*,\s*&?"([a-z_]\w*)"\s*\)'
    r'|Callable\s*\(\s*[\w.]+\s*,\s*([A-Za-z_]\w*)\s*\)'
)
# quoted identifier-shaped strings in bodies of files that use dynamic
# dispatch (file-level gate) — callback-name conventions leak into plain
# string args, e.g. handle_animation_callback(slot, "on_cast_hold_end")
QUOTED_IDENT_RE = re.compile(r"""["']([a-z_]\w{3,})["']""")
# bare callback-convention identifiers (_on_*) in argument/array positions:
# method references without call parens, e.g. ["QUIT", color, _on_quit]
BARE_HANDLER_RE = re.compile(r'(?<![\w."&])_on_[a-z_]\w*')
# bare method-ref as the FULL right-hand side of an assignment (raw, not
# folded: the $ anchor needs real line ends): `obj.prop = _handler`
ASSIGN_RHS_RE = re.compile(r"(?<![=!<>+\-*/%&|^])=\s*([a-z_]\w*)\s*$", re.M)
ASSIGN_RHS_SKIP = {"true", "false", "null", "self"}
# tween binders reference methods without parens: tween_method(_set_reveal)
TWEEN_ARG_RE = re.compile(
    r'\.(?:tween_method|tween_callback|tween_property)\(\s*&?"?([A-Za-z_]\w{3,})"?'
)
# two-level receiver chains: ctx.teams.team_ids(...) — resolve head, hop
# through a declared member to the second class, then emit
CHAIN_CALL_RE = re.compile(
    r'(?<![\w.$])([A-Za-z_]\w*)\s*\.\s*([a-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*\('
)
CHAIN_VAR_RE = re.compile(
    r'(?<![\w.$])([A-Za-z_]\w*)\s*\.\s*([a-z_]\w*)\s*\.\s*([a-z_]\w*)\b(?!\s*\()'
)
# StringName values inside .tres (BT task routing): start_method_name = &"x"
TRES_STRINGNAME_RE = re.compile(r'&"([a-z_]\w{3,})"')
# file-level dynamic-dispatch hints enabling the quoted-ident harvest
DYNAMIC_HINT_RE = re.compile(
    r'\.call\(|\.call_deferred|Callable\(|has_method\(|\.connect\(|\.rpc\(|\.emit\('
)
# path-form extends (incl. inner classes): extends "res://....gd"
PATH_EXTENDS_RE = re.compile(r'^\s*extends\s+"(res://[^"]+\.gd)"', re.M)

# bare identifiers that are engine globals/keywords, never local calls
DYNAMIC_METHODS = {"rpc", "rpc_id", "call", "call_deferred", "callv", "bind", "emit", "emit_signal", "notify_property_list_changed"}
NON_CALLS = {
    "if", "elif", "while", "for", "match", "return", "await", "func", "super",
    "and", "or", "not", "in", "is", "break", "continue", "pass", "class",
    "self", "true", "false", "null", "void", "static", "const", "var",
    "signal", "enum", "export", "onready", "tool", "yield",
    "print", "printerr", "push_error", "push_warning", "push_notice",
    "str", "int", "float", "bool", "len", "range", "abs", "absf", "absi",
    "min", "max", "minf", "maxf", "mini", "maxi", "clamp", "clampf", "clampi",
    "lerp", "lerpf", "lerp_angle", "randf", "randi", "randf_range",
    "randi_range", "randfn", "preload", "load", "resource_local_to_scene",
    "assert", "is_instance_valid", "instance_from_id", "weakref", "hash",
    "typeof", "type_string", "str_to_var", "var_to_str", "bytes_to_var",
    "var_to_bytes", "inst_to_dict", "dict_to_inst", "ord", "char",
    "range_lerp", "smoothstep", "move_toward", "ease", "step_decimals",
    "snapped", "fmod", "fposmod", "posmod", "floor", "floori", "ceil",
    "ceili", "round", "roundi", "sqrt", "pow", "sin", "cos", "tan", "asin",
    "acos", "atan", "atan2", "exp", "log", "is_nan", "is_inf", "is_finite",
    "is_equal_approx", "is_zero_approx", "sign", "signf", "signi", "seed",
    "rand_from_seed", "deg_to_rad", "rad_to_deg", "linear_to_db",
    "db_to_linear", "cartesian_to_polar", "polar_to_cartesian", "wrapi",
    "wrapf", "nearest_po2", "det", "_error", "dedent",
}


# ---- data model ----------------------------------------------------------------
# Func / FileSym live in extractors/model.py (language-neutral); re-exported
# here so graph.Func / graph.FileSym keep working for importers.


def _fold_continuations(body: str) -> str:
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


_HINT_VALUE_RE = re.compile(r"^[A-Za-z_]\w*\[([^\]]*)\]")


def _hint_value_classes(hint: str) -> list[str]:
    """Value classes inside a flat generic hint's outer subscript:
    ``dict[str, Widget]`` -> ``['Widget']`` (Union members included)."""
    m = _HINT_VALUE_RE.match(hint)
    return re.findall(r"\b[A-Z]\w*", m.group(1)) if m else []


class Graph:
    """Whole-checkout structural graph. Build once per process (~seconds)."""

    def __init__(self) -> None:
        self.files: dict[str, FileSym] = {}
        self.class_map: dict[str, str] = {}  # class_name -> res:// path
        # edge[src_key] = set of dst_key; key = "path::func" or "path::SIGNAL:x"
        self.edges: dict[str, set[str]] = defaultdict(set)
        self.reverse: dict[str, set[str]] = defaultdict(set)
        self.roots: set[str] = set()
        self.referenced: set[str] = set()  # string-referenced (alive, not root)
        self.referenced_names: set[str] = set()  # func names called via unresolvable receivers
        self.edge_types: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.built_at_lines: int = 0

    # -- parsing ---------------------------------------------------------------
    # language parsing is delegated to extractors/ via the suffix registry;
    # these wrappers keep the historical method names for internal callers

    def _parse_gd(self, path: Path, rel: str) -> FileSym:
        return parse_gd(path, rel)

    def _parse_tscn(self, path: Path, rel: str) -> FileSym:
        return parse_tscn(path, rel)

    # -- build -------------------------------------------------------------------

    def build(self) -> "Graph":
        for path in nav.iter_files():
            rel = nav.file_id(path)
            extractor = registry_for(path.suffix)
            if extractor is None:
                continue
            fs = extractor.parse(path, rel)
            self.files[rel] = fs
            if fs.class_name:
                self.class_map[fs.class_name] = rel

        # autoload singletons are addressable by their project.godot name
        self.autoloads = self._parse_autoloads()
        for name, rel in self.autoloads.items():
            self.class_map.setdefault(name, rel)

        # asset scenes sit outside the search index but carry animation method
        # tracks + connections that fire script funcs — parse for wiring only
        for path in (nav.ROOT / "assets").rglob("*.tscn") if (nav.ROOT / "assets").is_dir() else ():
            rel = nav.file_id(path)
            if rel not in self.files:
                self.files[rel] = registry_for(path.suffix).parse(path, rel)

        # .tres/.res reference scripts via ext_resource — data-constructed
        # classes (custom resources) whose funcs never appear in .gd callers
        self.tres_scripts: set[str] = set()
        for path in nav.ROOT.rglob("*.tres"):
            if any(part in {".git", ".godot"} for part in path.parts):
                continue
            try:
                text = nav._read_text(path)
            except OSError:
                continue
            for m in TRES_SCRIPT_RE.finditer(text):
                rel2 = m.group(1).removeprefix("res://")
                if rel2 in self.files:
                    self.tres_scripts.add(rel2)
            # StringName values route dynamic dispatch (LimboAI BT tasks
            # export method names): keep matching funcs alive repo-wide
            for m in TRES_STRINGNAME_RE.finditer(text):
                self.referenced_names.add(m.group(1))

        # transitive subclass map: base class_name -> files below it — a
        # call resolved to a base may dispatch to any override
        self._subclasses: dict[str, set[str]] = defaultdict(set)
        for rel, fs in self.files.items():
            if fs.ext != ".gd":
                continue
            base = fs.extends
            seen: set[str] = set()
            while base and base not in seen and base in self.class_map:
                seen.add(base)
                self._subclasses[base].add(rel)
                base = self.files[self.class_map[base]].extends
            # path-form extends (often inner helper classes): register the
            # whole file under the base file's class_name so override
            # completion can reach it
            try:
                raw = nav._read_text(nav.ROOT / rel)
            except OSError:
                raw = ""
            for m in PATH_EXTENDS_RE.finditer(raw):
                base_rel = m.group(1).removeprefix("res://")
                base_cls = self.files.get(base_rel, None)
                if base_cls is not None:
                    # register under the rel path always, class_name when
                    # the base declares one — _emit_call looks up both
                    self._subclasses[base_rel].add(rel)
                    if base_cls.class_name:
                        self._subclasses[base_cls.class_name].add(rel)

        # files using dynamic dispatch: quoted identifier strings in their
        # bodies are candidate method names; parse-time StringName defaults
        # (BT exports) join the same referenced-name pool
        self._dyn_files: set[str] = set()
        for rel, fs in self.files.items():
            if fs.ext != ".gd":
                continue
            for nm in fs.name_literals:
                if len(nm) > 3:
                    self.referenced_names.add(nm)
            # class-level initializer calls run at instantiation — alive
            for nm in fs.init_calls:
                self.referenced_names.add(nm)
            if not fs.funcs:
                continue
            joined = "\n".join(f.body for f in fs.funcs.values())
            if DYNAMIC_HINT_RE.search(joined):
                self._dyn_files.add(rel)

        # python import liveness: a PLAIN `import x` binds the namespace -
        # the module may be reached dynamically, so its funcs stay alive
        # as a unit. A `from x import y` selects exactly one name: only
        # that func (if it is one) survives the import; siblings do not.
        for rel, fs in self.files.items():
            if fs.ext != ".py":
                continue
            for mod in fs.imported_modules:
                if mod in self.files:
                    for other in self.files[mod].funcs.values():
                        self.referenced.add(other.key)
            for mod, nm in fs.from_imports:
                if mod in self.files and nm in self.files[mod].funcs:
                    self.referenced.add(f"{mod}::{nm}")

        for rel, fs in self.files.items():
            if fs.ext == ".gd":
                for fn in fs.funcs.values():
                    self._scan_body(fs, fn)
            elif fs.ext == ".py":
                for fn in fs.funcs.values():
                    self._scan_body_py(fs, fn)

        self._wire_tscn()
        self._find_roots()
        self._reachable()
        return self

    def _scan_body(self, fs: FileSym, fn: Func) -> None:
        src_key = fn.key
        # multi-line call arguments defeat line-based regex passes: fold
        # continuation lines (unbalanced parens/brackets) into single
        # logical lines before scanning; fn.body stays raw for display
        scan_text = _fold_continuations(fn.body)
        # first-order type inference: member vars + typed params/locals in this body
        var_types = dict(fs.members)
        for pm in PARAM_TYPED_RE.finditer(scan_text):
            var_types[pm.group(1)] = pm.group(2)
        for m in QUALIFIED_CALL_RE.finditer(scan_text):
            head, fname = m.group(1), m.group(2)
            cls = head if head in self.class_map else var_types.get(head)
            if cls and cls in self.class_map:
                dst = self.class_map[cls]
                if fname in self.files[dst].funcs:
                    self._emit_call(src_key, dst, fname)
            elif head in fs.consts and fs.consts[head] in self.files:
                dst = fs.consts[head]
                if fname in self.files[dst].funcs:
                    self._emit_call(src_key, dst, fname)
            else:
                # receiver type unknown (factory returns, variants) — the call may
                # dispatch to any same-named func; mark name alive, no edge.
                # dispatch intermediaries (.rpc()/.call_deferred()/.bind()) point
                # at the RECEIVER, not at rpc/call_deferred themselves
                if fname in DYNAMIC_METHODS:
                    # builtin-shadowing user funcs (e.g. a user `bind`) are
                    # valid targets of the same dispatch — keep the name
                    # alive alongside the receiver head
                    self.referenced_names.add(head)
                self.referenced_names.add(fname)
        # member-var cross-references: receiver.member where the receiver
        # resolves to a known class (same chain as calls above) and that
        # file actually declares the member — edges land on VAR: pseudo-nodes
        for m in MEMBER_ACCESS_RE.finditer(scan_text):
            head, member = m.group(1), m.group(2)
            cls = head if head in self.class_map else var_types.get(head)
            if cls and cls in self.class_map:
                dst = self.class_map[cls]
            elif head in fs.consts and fs.consts[head] in self.files:
                dst = fs.consts[head]
            else:
                continue
            if member in self.files[dst].members:
                self._edge(src_key, f"{dst}::VAR:{member}", ty="var")
            elif member in self.files[dst].funcs:
                # property-assignment form: obj.method = x targets the
                # func (setter-style) without a call paren
                self._emit_call(src_key, dst, member)
        # dynamically loaded scripts: any "res://....gd" string literal in
        # the body keeps every func of that file alive
        for m in RES_LOAD_RE.finditer(scan_text):
            loaded = m.group(1)
            if loaded in self.files:
                for other in self.files[loaded].funcs.values():
                    self.referenced.add(other.key)
        # dynamic-dispatch harvest: method names passed to .call()/.rpc()/
        # has_method(), StringName literals, Callable(obj, "m") — receivers
        # are runtime-typed, so mark the names alive instead of an edge
        for m in DISPATCH_STR_RE.finditer(scan_text):
            nm = m.group(1)
            if len(nm) > 3:
                self.referenced_names.add(nm)
        for m in BARE_DISPATCH_STR_RE.finditer(scan_text):
            nm = m.group(1)
            if len(nm) > 3:
                self.referenced_names.add(nm)
        for m in STRINGNAME_LIT_RE.finditer(scan_text):
            self.referenced_names.add(m.group(1))
        if fs.path in self._dyn_files:
            for m in QUOTED_IDENT_RE.finditer(scan_text):
                self.referenced_names.add(m.group(1))
        for m in CALLABLE_TWO_RE.finditer(scan_text):
            nm = m.group(1) or m.group(2)
            if nm and len(nm) > 3:
                self.referenced_names.add(nm)
        # tween binders + bare callback-convention identifiers (array
        # elements, deferred refs): method refs without call parens
        for m in TWEEN_ARG_RE.finditer(scan_text):
            self.referenced_names.add(m.group(1))
        for m in BARE_HANDLER_RE.finditer(scan_text):
            self.referenced_names.add(m.group(0))
        # bare method-ref as full assignment RHS (property-assignment
        # wiring): `magic_system.cb = _connect_equipped_signal` — scanned
        # on the RAW body because the $ anchor needs real line ends
        for m in ASSIGN_RHS_RE.finditer(fn.body):
            nm = m.group(1)
            if nm not in ASSIGN_RHS_SKIP:
                self.referenced_names.add(nm)
        # two-level typed chains: ctx.teams.team_ids(...) — resolve head to
        # its class, hop through a declared member, then emit
        for m in CHAIN_CALL_RE.finditer(scan_text):
            head, mid, tail = m.group(1), m.group(2), m.group(3)
            dst = self._chain_dst(var_types, head, mid)
            if dst and tail in self.files[dst].funcs:
                self._emit_call(src_key, dst, tail)
            else:
                # unresolvable receiver chain (duck-typed containers):
                # same name-alive fallback as single-hop unknown receivers
                self.referenced_names.add(tail)
        for m in CHAIN_VAR_RE.finditer(scan_text):
            head, mid, tail = m.groups()
            dst = self._chain_dst(var_types, head, mid)
            if dst and tail in self.files[dst].members:
                self._edge(src_key, f"{dst}::VAR:{tail}", ty="var")
            elif dst and tail in self.files[dst].funcs:
                self._emit_call(src_key, dst, tail)
        for m in AS_CAST_CALL_RE.finditer(scan_text):
            cls, fname = m.group(1), m.group(2)
            if cls in self.class_map:
                dst = self.class_map[cls]
                if fname in self.files[dst].funcs:
                    self._emit_call(src_key, dst, fname)
            else:
                self.referenced_names.add(fname)
        for m in BARE_CALL_RE.finditer(scan_text):
            name = m.group(1)
            if name in NON_CALLS:
                continue
            if name in fs.funcs:
                # _emit_call mirrors the same-file edge onto subclass
                # overrides (incl. path-form extends files below)
                self._emit_call(src_key, fs.path, name)
            else:
                # inherited method call: resolve up the extends chain
                # (_emit_call mirrors onto sibling overrides); base calls
                # a func it does not define -> every subclass override
                anc = self._ancestor_def(fs, name)
                if anc:
                    self._emit_call(src_key, anc, name)
                if fs.class_name and fs.class_name in self._subclasses:
                    for sub in self._subclasses[fs.class_name]:
                        if name in self.files[sub].funcs:
                            self._edge(src_key, f"{sub}::{name}")
        # signal emits -> signal nodes; connect/Callable string refs -> handlers
        for m in EMIT_RE.finditer(scan_text):
            sig = m.group(1) or m.group(2)
            if sig in fs.signals:
                self._edge(src_key, f"{fs.path}::SIGNAL:{sig}", ty="signal")
        if CONNECT_RE.search(scan_text):
            for m in STRING_NAME_RE.finditer(scan_text):
                ref = m.group(1)
                if ref in fs.funcs:
                    self._edge(src_key, f"{fs.path}::{ref}", ty="signal")
                    # handlers fire on signal emit — entry points, traverse
                    self.roots.add(f"{fs.path}::{ref}")
                # cross-file: _on_* handlers commonly target other scripts
                elif ref.startswith("_on_"):
                    self.referenced.add(f"*::{ref}")
            # direct method references (no quotes):
            #   sig.connect(_handler) / is_connected(_handler) / disconnect(...)
            for m in CONNECT_METHOD_RE.finditer(scan_text):
                ref = m.group(1)
                if ref in fs.funcs:
                    self._edge(src_key, f"{fs.path}::{ref}", ty="signal")
                    self.roots.add(f"{fs.path}::{ref}")
                else:
                    # inherited handler: resolve up the extends chain
                    anc = self._ancestor_def(fs, ref)
                    if anc and ref in self.files[anc].funcs:
                        key = f"{anc}::{ref}"
                        self._edge(src_key, key, ty="signal")
                        self.roots.add(key)

    def _scan_body_py(self, fs: FileSym, fn: Func) -> None:
        """Python body scan: call edges via typed receivers, class_map
        classes, and from-import consts (module-file receivers)."""
        src_key = fn.key
        scan_text = _fold_continuations(fn.body)
        # receiver types: self-members from the extractor + typed params
        # + constructor locals in this body
        var_types = dict(fs.members)
        for pm in PY_PARAM_TYPED_RE.finditer(scan_text):
            var_types[pm.group(1)] = pm.group(2)
        for m in PY_ANNOT_ASSIGN_RE.finditer(scan_text):
            var_types[m.group(1)] = m.group(2)
        for m in PY_WITH_AS_RE.finditer(scan_text):
            var_types[m.group(2)] = m.group(1)
        for m in PY_LOCAL_NEW_RE.finditer(scan_text):
            var_types[m.group(1)] = m.group(2)
        # x = imported_name(...): the local becomes a module-object
        # receiver — resolve x.method( against that module (and the
        # modules it re-exports, since registries return submodules)
        for m in PY_MODULE_ASSIGN_RE.finditer(scan_text):
            mod = fs.consts.get(m.group(2), "")
            if mod in self.files:
                var_types[m.group(1)] = "module:" + mod
        # obj.method( — head resolves via class_map (repo classes), typed
        # receivers, from-import consts (module-file receivers), or
        # module-object locals bound from an imported call
        for m in PY_ATTR_CALL_RE.finditer(scan_text):
            head, meth = m.group(1), m.group(2)
            if head in ("self", "cls"):
                if meth in fs.funcs:
                    self._emit_call(src_key, fs.path, meth)
                continue
            vt = var_types.get(head, "")
            if vt.startswith("module:"):
                for dst in self._module_method_dsts(vt[len("module:"):], meth):
                    self._emit_call(src_key, dst, meth)
                continue
            cls = head if head in self.class_map else var_types.get(head, "")
            if cls and cls in self.class_map:
                dst = self.class_map[cls]
            elif head in fs.consts and fs.consts[head] in self.files:
                dst = fs.consts[head]
            else:
                continue
            if meth in self.files[dst].funcs:
                self._emit_call(src_key, dst, meth)
        # imported_call(args).method( — calling an imported function then
        # a method on the result (registry_for(suffix).parse(...)): the
        # const's module chain supplies the candidate defs
        for m in PY_RESULT_CALL_RE.finditer(scan_text):
            head, meth = m.group(1), m.group(3)
            mod = fs.consts.get(head, "")
            if mod in self.files:
                for dst in self._module_method_dsts(mod, meth):
                    self._emit_call(src_key, dst, meth)
        # two-level chains: self.g.greet( / api.client.run(
        for m in PY_CHAIN_CALL_RE.finditer(scan_text):
            head, mid, tail = m.group(1), m.group(2), m.group(3)
            if head in ("self", "cls"):
                cls = var_types.get(mid, "")
                dst = self.class_map.get(cls, "")
            else:
                dst = self._chain_dst(var_types, head, mid)
            if dst and tail in self.files[dst].funcs:
                self._emit_call(src_key, dst, tail)
        # box[k].method( / self.box[k].method( — subscript access into a
        # generic hint (dict[str, Widget]): the capitalized names inside
        # the outer subscript are the receiver candidates
        for m in PY_SUBSCRIPT_CALL_RE.finditer(scan_text):
            head, meth = m.group(1), m.group(2)
            parts = head.split(".")
            if len(parts) > 1 and parts[0] not in ("self", "cls"):
                continue
            hint = var_types.get(parts[-1], "")
            for vc in _hint_value_classes(hint):
                dst = self.class_map.get(vc, "")
                if dst and meth in self.files[dst].funcs:
                    self._emit_call(src_key, dst, meth)
        # bare name( — same-file funcs, then from-import module funcs
        for m in PY_BARE_CALL_RE.finditer(scan_text):
            name = m.group(1)
            if name in PY_NON_CALLS:
                continue
            if name in fs.funcs:
                self._emit_call(src_key, fs.path, name)
                continue
            dst = fs.consts.get(name, "")
            if dst in self.files and name in self.files[dst].funcs:
                self._emit_call(src_key, dst, name)

    def _module_method_dsts(self, mod_rel: str, meth: str) -> list[str]:
        """Files that may define `meth` reached through module `mod_rel`:
        the module itself plus the modules it imports (re-export surface —
        registries return submodules listed in their imports)."""
        if mod_rel not in self.files:
            return []
        cands = [mod_rel]
        mod_fs = self.files[mod_rel]
        for reexport in mod_fs.consts.values():
            if reexport in self.files and reexport != mod_rel:
                cands.append(reexport)
        return sorted({c for c in cands if meth in self.files[c].funcs})

    def _ancestor_def(self, fs: FileSym, name: str) -> str:
        """Rel path of the nearest ancestor class declaring `name`, or ''."""
        base = fs.extends
        seen: set[str] = set()
        while base and base not in seen and base in self.class_map:
            seen.add(base)
            rel = self.class_map[base]
            if name in self.files[rel].funcs:
                return rel
            base = self.files[rel].extends
        return ""

    def _emit_call(self, src: str, dst: str, fname: str) -> None:
        """Call edge + virtual-dispatch completion: a call resolved to a
        base class may land on any subclass override — mirror the edge."""
        self._edge(src, f"{dst}::{fname}")
        base = self.files[dst].class_name or dst
        for sub in self._subclasses.get(base, ()):
            if fname in self.files[sub].funcs:
                self._edge(src, f"{sub}::{fname}")

    def _chain_dst(self, var_types: dict, head: str, mid: str) -> str:
        """Resolve head.mid to the class declaring that member, or ''."""
        cls = head if head in self.class_map else var_types.get(head)
        if not (cls and cls in self.class_map):
            return ""
        fs1 = self.files[self.class_map[cls]]
        cls2 = fs1.members.get(mid, "")
        if cls2 and cls2 in self.class_map:
            return self.class_map[cls2]
        return ""

    def _edge(self, src: str, dst: str, ty: str = "call") -> None:
        if src == dst:
            return
        self.edges[src].add(dst)
        self.reverse[dst].add(src)
        self.edge_types[(src, dst)].add(ty)

    def _wire_tscn(self) -> None:
        for rel, fs in self.files.items():
            if fs.ext != ".tscn":
                continue
            # multi-script scenes: a handler may live on ANY of the scene's
            # script ext_resources, not just the first attached one
            script_rels = [
                s_rel
                for s in fs.scripts
                if (s_rel := self._res_to_rel(s)) and s_rel in self.files
            ]
            if not script_rels and fs.attached_script:
                s_rel = self._res_to_rel(fs.attached_script)
                if s_rel and s_rel in self.files:
                    script_rels.append(s_rel)
            for script_rel in script_rels:
                for _, handler in fs.connections:
                    if handler in self.files[script_rel].funcs:
                        key = f"{script_rel}::{handler}"
                        self.roots.add(key)
                        self._edge(f"{rel}::tscn", key, ty="signal")
                self._edge(f"{rel}::tscn", f"{script_rel}::tscn", ty="attach")
            for inst in fs.instances:
                inst_rel = self._res_to_rel(inst)
                if inst_rel and inst_rel in self.files:
                    self._edge(f"{rel}::tscn", f"{inst_rel}::tscn", ty="inst")

    def _res_to_rel(self, res_path: str) -> str:
        if not res_path:
            return ""
        return res_path.removeprefix("res://")

    def _parse_autoloads(self) -> dict[str, str]:
        """project.godot [autoload] section: singleton name -> rel path."""
        out: dict[str, str] = {}
        pg = nav.ROOT / "project.godot"
        if not pg.is_file():
            return out
        in_auto = False
        for line in nav._read_text(pg).splitlines():
            if line.strip().startswith("[autoload]"):
                in_auto = True
                continue
            if line.strip().startswith("["):
                in_auto = False
            if in_auto:
                m = re.match(
                    r'^(\w+)\s*=\s*"\*?res://([\w/.-]+\.gd)"', line.strip()
                )
                if m and m.group(2) in self.files:
                    out[m.group(1)] = m.group(2)
        return out

    def _find_roots(self) -> None:
        # entry-point rules are language-owned: each extractor module ships
        # ENTRY_RULES callables (fs, ctx) -> iterable of entry func keys
        for rel, fs in self.files.items():
            extractor = registry_for(fs.ext)
            if extractor is None:
                continue
            for rule in getattr(extractor, "ENTRY_RULES", ()):
                self.roots.update(rule(fs, ctx=self))

    def _reachable(self) -> None:
        # dynamically-invoked names (unresolvable receivers, strings) are
        # alive but have no static edge — root them so their callees survive
        if self.referenced_names:
            for rel, fs in self.files.items():
                if fs.ext not in (".gd", ".py"):
                    continue
                for name, fn in fs.funcs.items():
                    if name in self.referenced_names:
                        self.roots.add(fn.key)
        seen: set[str] = set(self.roots) | self.referenced
        # referenced keys carry real out-edges (preloaded/dynamically
        # loaded files) — they must be traversed, not just marked alive
        queue = deque(self.roots | self.referenced)
        while queue:
            cur = queue.popleft()
            for nxt in self.edges.get(cur, ()):  # forward edges
                if nxt not in seen and not nxt.endswith("::tscn"):
                    seen.add(nxt)
                    queue.append(nxt)
        self.reachable = seen

    # -- queries -------------------------------------------------------------------

    def dead_code(self, limit: int = 60) -> dict[str, object]:
        dead = []
        for rel, fs in self.files.items():
            if fs.ext not in (".gd", ".py"):
                continue
            file_is_dynamic = bool(DYNAMIC_HINT_RE.search("\n".join(fs.funcs[f].body for f in fs.funcs))) if fs.funcs else False
            for name, fn in fs.funcs.items():
                if fn.key in self.reachable:
                    continue
                # wildcard handler refs (_on_x from any file) keep it alive
                if any(name == r.split("::")[-1] for r in self.referenced if r.startswith("*::")):
                    continue
                if name in self.referenced_names:
                    continue
                tier = "review" if file_is_dynamic else "likely"
                # functions on classes extending bases we cannot resolve (engine
                # natives not in VIRTUALS, C++ addons) may be dispatched natively
                if (
                    tier == "likely"
                    and fs.extends
                    and fs.extends not in self.class_map
                    and name.startswith("_")
                    and name not in VIRTUALS
                ):
                    tier = "review"
                dead.append({"path": rel, "func": name, "line": fn.line, "tier": tier})
        dead.sort(key=lambda d: (d["tier"], d["path"], d["line"]))
        by_tier = defaultdict(int)
        for d in dead:
            by_tier[d["tier"]] += 1
        return {
            "total": len(dead),
            "by_tier": dict(by_tier),
            "candidates": dead[:limit],
            "note": (
                "candidates only — verify before deleting. 'likely' = file has no "
                "dynamic dispatch; 'review' = file uses call()/Callable()/connect(), "
                "string-dispatch may hide callers."
            ),
        }

    def symbol_graph(self, symbol: str, depth: int = 1, limit: int = 40) -> str:
        depth = max(1, min(depth, 3))
        keys = self._resolve(symbol)
        if not keys:
            return f"no function matching '{symbol}'"
        out_lines: list[str] = []
        seen_keys: set[str] = set()
        frontier = set(keys)
        for _ in range(depth):
            nxt: set[str] = set()
            for key in frontier:
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                callers = sorted(self.reverse.get(key, ()))
                callees = sorted(self.edges.get(key, ()))
                out_lines.append(self._fmt_node(key, callers, callees))
                nxt |= {c for c in callees + callers if not c.endswith("::tscn")}
            frontier = nxt - seen_keys
            if not frontier:
                break
        return "\n".join(out_lines[:limit])

    def _resolve(self, symbol: str) -> list[str]:
        hits = []
        for rel, fs in self.files.items():
            if symbol in fs.funcs:
                hits.append(f"{rel}::{symbol}")
            if fs.class_name == symbol:
                hits.extend(f"{rel}::{f}" for f in fs.funcs)
        if not hits:
            for rel, fs in self.files.items():
                for name in fs.funcs:
                    if symbol.lower() in name.lower():
                        hits.append(f"{rel}::{name}")
        return hits[:10]

    def _fmt_node(self, key: str, callers: list[str], callees: list[str]) -> str:
        def short(k: str) -> str:
            path, _, name = k.partition("::")
            return f"{path}#{name}"
        c_in = ", ".join(short(c) for c in callers[:8]) or "-"
        c_out = ", ".join(short(c) for c in callees[:8]) or "-"
        return f"{short(key)}\n    callers: {c_in}\n    callees: {c_out}"

    # -- duplicates ---------------------------------------------------------------

    def exact_duplicates(self, limit: int = 30) -> list[dict[str, object]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for rel, fs in self.files.items():
            if fs.ext != ".gd":
                continue
            for name, fn in fs.funcs.items():
                norm = _normalize_body(fn.body)
                if len(norm.splitlines()) < 3:
                    continue  # trivial
                groups[hashlib.sha1(norm.encode()).hexdigest()].append(fn.key)
        dups = [
            {"hash": h[:8], "members": sorted(v)}
            for h, v in groups.items()
            if len(v) > 1
        ]
        dups.sort(key=lambda d: -len(d["members"]))
        return dups[:limit]


def _normalize_body(body: str) -> str:
    out = []
    for line in body.splitlines():
        s = line.split("#", 1)[0].rstrip()
        if not s.strip():
            continue
        out.append("  " + s.strip())  # unify indent
    return "\n".join(out)


# -- function-level vector index (chroma "<collection>-fns") -------------------


def _fn_collection() -> "chromadb.Collection":
    import chromadb

    client = chromadb.PersistentClient(path=str(nav.DB_DIR))
    # per-config collection: two checkouts/projects sharing one .chroma dir
    # must not mix function vectors (hardcoded name collided across configs)
    col = client.get_or_create_collection(
        name=f"{nav.COLLECTION}-fns",
        metadata={"hnsw:space": "cosine"},
    )
    nav._check_model(col)
    return col


def _all_filesyms() -> dict[str, FileSym]:
    return get_graph().files


def sync_functions(changed: list[str], deleted: list[str]) -> dict[str, int]:
    """Re-embed functions of changed files, purge deleted files' functions.
    Self-healing: an interrupted sync (embed failure, process kill) leaves a
    dirty marker; the next call with no changes does a full rebuild so the
    index never stays silently stale."""
    col = _fn_collection()
    dirty = nav.DB_DIR / "fns.dirty"
    if dirty.is_file() and not changed:
        changed = sorted(rel for rel, fs in _all_filesyms().items() if fs.funcs)
    stale = sorted(set(changed) | set(deleted))
    if stale and col.count():
        for p in stale:
            col.delete(where={"path": p})
    if col.count() == 0 and not changed:
        # first build: index every parsed function (any text language)
        changed = sorted(
            rel for rel, fs in _all_filesyms().items() if fs.funcs
        )
    parser = Graph()
    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict[str, object]] = []
    for rel in changed:
        path = nav.ROOT / rel
        suffix = path.suffix
        # scenes have no funcs; only languages with an extractor are parseable
        if not path.is_file() or suffix not in nav.EXTS or suffix == ".tscn":
            continue
        if suffix == ".gd":
            fs = parser._parse_gd(path, rel)
        else:
            from extractors import registry_for

            fs = registry_for(suffix).parse(path, rel)
        for name, fn in fs.funcs.items():
            ids.append(f"{rel}::{name}")
            # signature line up front: better embeddings + agents see the IO
            # surface without opening the file
            sig = ", ".join(
                f"{p}: {t}" if t else p for p, t in fn.params
            )
            ret = f" -> {fn.ret}" if fn.ret else ""
            docs.append(
                f"{rel} :: func {name}({sig}){ret}\n{fn.body[:6000]}"
            )
            metas.append(
                {"path": rel, "name": name, "class_name": fs.class_name,
                 "line": fn.line}
            )
    try:
        with nav._db_lock():
            added = 0
            for i in range(0, len(ids), nav.EMBED_BATCH):
                vecs = nav.embed(docs[i : i + nav.EMBED_BATCH])
                col.upsert(
                    ids=ids[i : i + nav.EMBED_BATCH],
                    embeddings=vecs,
                    documents=docs[i : i + nav.EMBED_BATCH],
                    metadatas=metas[i : i + nav.EMBED_BATCH],
                )
                added += len(vecs)
    except Exception:
        dirty.write_text("sync failed", encoding="utf-8")
        raise
    dirty.unlink(missing_ok=True)
    return {"fns_upserted": added, "purged_paths": len(stale)}


def find_functions(query: str, n: int = 6) -> list[dict[str, object]]:
    """Semantic search over individual functions (vector index)."""
    col = _fn_collection()
    count = col.count()
    if count == 0:
        return []
    vector = nav.embed([query])[0]
    got = col.query(
        query_embeddings=[vector],
        n_results=min(n, count),
        include=["metadatas", "distances"],
    )
    out = []
    for rid, dist, meta in zip(
        got["ids"][0], got["distances"][0], got["metadatas"][0]
    ):
        meta = meta or {}
        out.append(
            {
                "key": rid,
                "score": round(1.0 - float(dist), 4),
                "path": str(meta.get("path", "")),
                "func": str(meta.get("name", "")),
                "line": int(meta.get("line", 0)),
            }
        )
    return out


# -- module-level singleton ---------------------------------------------------

_graph: Graph | None = None


def get_graph(rebuild: bool = False) -> Graph:
    global _graph
    if _graph is None or rebuild:
        _graph = Graph().build()
    return _graph
