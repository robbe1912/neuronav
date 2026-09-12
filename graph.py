"""neuronav structural layer: function/signal graph, dead code, duplicates.

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
from collections import Counter, defaultdict, deque

import nav
from extractors import (
    ADDON_VIRTUALS,
    CPP_DYNAMIC_RE,
    CPP_EXTS,
    CPP_MENTION_FLOOR,
    GUT_ROOTS,
    MANUAL_BASES,
    PY_CONTROL_KEYWORDS,
    PY_HOOKS,
    VIRTUALS,
    add_class_ctx,
    harvest_registration,
    parse_gd,
    parse_tscn,
    registry_for,
    scan_calls,
)
# single import surface: language facts (VIRTUALS etc.) and the cAST model
# primitives (add_class_ctx, issue #76) are re-exported by the extractors
# package so graph.py never deep-imports an extractor submodule — extractor
# modules stay free of any graph import (acyclic).

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
# identifier-shaped token anywhere in raw corpus text (issue #20): the
# dead-tier mention-count pass counts these per file once, comments and
# string literals included — never a rescan per dead candidate
MENTION_TOKEN_RE = re.compile(r"[A-Za-z_]\w*")

# ---- fn-key grammar (single owner) -------------------------------------------
# Node keys in Graph.edges/reverse/roots/referenced are "path::func" plus
# three pseudo-node spellings: "path::tscn" (scene file node),
# "path::SIGNAL:name" (signal node), "path::VAR:member" (member-write
# node); a bare "*::name" marks name-only references. The grammar is
# FROZEN — the viz template's fnKey/keyFile logic mirrors it, so any
# change is a both-sides contract (graph.py + viz.py template), never
# one-sided. split_key's "first :: wins" is safe because extractor
# captures are identifier-shaped (never contain "::").
FN_KEY_SEP = "::"
TSCN_SUFFIX = "::tscn"
SIGNAL_PREFIX = "::SIGNAL:"
VAR_PREFIX = "::VAR:"


def fn_key(rel: str, name: str) -> str:
    """Function-node key: repo-relative path + function name."""
    return f"{rel}{FN_KEY_SEP}{name}"


def split_key(key: str) -> str:
    """File part of any node key (fn, scene, signal, member spellings):
    everything before the first separator. Bare file keys (cpp v1.1
    header-scope sources carry none) pass through whole."""
    return key.split(FN_KEY_SEP, 1)[0]
# dead-tier weights (viz J2 consumes): per-tier weight for dead-code
# candidates — "likely" 1.0, "review" 0.5 — and the dead-file share
# threshold: a file only flags dead when its dead weight reaches this
# share of its .gd func count.
DEAD_TIER_WEIGHTS = {"likely": 1.0, "review": 0.5}
DEAD_SHARE_THRESHOLD = 0.4

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
PY_NON_CALLS = PY_CONTROL_KEYWORDS | {
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
# string args, e.g. run_callback(slot, "on_target_hit")
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
        self._mentions: Counter | None = None  # lazy corpus mention counts (issue #20)

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
            if fs.ext != ".gd" and fs.ext not in CPP_EXTS:
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
            if fs.ext == ".gd" and DYNAMIC_HINT_RE.search(joined):
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
            elif fs.ext in CPP_EXTS:
                self._scan_body_cpp(fs, rel)

        self._wire_tscn()
        # C++ wiring (issue #13): .cpp files inherit their class identity
        # from the paired header, then registration macros become edges and
        # the repo-wide GDVIRTUAL override set. Must precede _find_roots —
        # the gdvirtual entry rule consumes ctx.cpp_gdvirtuals.
        self.cpp_gdvirtuals: set[str] = set()
        self._pair_cpp()
        self._wire_cpp()
        self._wire_aliases()
        self._find_roots()
        self._reachable()
        return self

    def _scan_body(self, fs: FileSym, fn: Func) -> None:
        # multi-line call arguments defeat line-based regex passes: fold
        # continuation lines (unbalanced parens/brackets) into single
        # logical lines before scanning; fn.body stays raw for display
        scan_text = _fold_continuations(fn.body)
        # first-order type inference: member vars + typed params/locals in this body
        var_types = dict(fs.members)
        for pm in PARAM_TYPED_RE.finditer(scan_text):
            var_types[pm.group(1)] = pm.group(2)
        self._scan_calls(fs, fn, scan_text, var_types)
        self._scan_liveness(fs, fn, scan_text)
        self._scan_chains(fs, fn, scan_text, var_types)
        self._scan_signals(fs, fn, scan_text)

    def _scan_calls(self, fs: FileSym, fn: Func, scan_text: str, var_types: dict) -> None:
        """Typed-receiver call edges and member-var cross-references."""
        src_key = fn.key
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
                self._edge(src_key, dst + VAR_PREFIX + member, ty="var")
            elif member in self.files[dst].funcs:
                # property-assignment form: obj.method = x targets the
                # func (setter-style) without a call paren
                self._emit_call(src_key, dst, member)

    def _scan_liveness(self, fs: FileSym, fn: Func, scan_text: str) -> None:
        """Name-keeping harvest: dynamically loaded scripts, dynamic-
        dispatch string refs, callback-convention identifiers. No edges —
        these only keep funcs out of dead-code tiers."""
        src_key = fn.key
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
        # wiring): `hub.cb = _connect_signal_handler` — scanned
        # on the RAW body because the $ anchor needs real line ends
        for m in ASSIGN_RHS_RE.finditer(fn.body):
            nm = m.group(1)
            if nm not in ASSIGN_RHS_SKIP:
                self.referenced_names.add(nm)

    def _scan_chains(self, fs: FileSym, fn: Func, scan_text: str, var_types: dict) -> None:
        """Two-level typed chains, casts, and bare/inherited calls."""
        src_key = fn.key
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
                self._edge(src_key, dst + VAR_PREFIX + tail, ty="var")
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
                            self._edge(src_key, fn_key(sub, name))

    def _scan_signals(self, fs: FileSym, fn: Func, scan_text: str) -> None:
        """Signal emits -> signal nodes; connect/Callable string refs -> handlers."""
        src_key = fn.key
        # signal emits -> signal nodes; connect/Callable string refs -> handlers
        for m in EMIT_RE.finditer(scan_text):
            sig = m.group(1) or m.group(2)
            if sig in fs.signals:
                self._edge(src_key, fs.path + SIGNAL_PREFIX + sig, ty="signal")
        if CONNECT_RE.search(scan_text):
            for m in STRING_NAME_RE.finditer(scan_text):
                ref = m.group(1)
                if ref in fs.funcs:
                    self._edge(src_key, fn_key(fs.path, ref), ty="signal")
                    # handlers fire on signal emit — entry points, traverse
                    self.roots.add(fn_key(fs.path, ref))
                # cross-file: _on_* handlers commonly target other scripts
                elif ref.startswith("_on_"):
                    self.referenced.add(f"*::{ref}")
            # direct method references (no quotes):
            #   sig.connect(_handler) / is_connected(_handler) / disconnect(...)
            for m in CONNECT_METHOD_RE.finditer(scan_text):
                ref = m.group(1)
                if ref in fs.funcs:
                    self._edge(src_key, fn_key(fs.path, ref), ty="signal")
                    self.roots.add(fn_key(fs.path, ref))
                else:
                    # inherited handler: resolve up the extends chain
                    anc = self._ancestor_def(fs, ref)
                    if anc and ref in self.files[anc].funcs:
                        key = fn_key(anc, ref)
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
        self._edge(src, fn_key(dst, fname))
        base = self.files[dst].class_name or dst
        for sub in self._subclasses.get(base, ()):
            if fname in self.files[sub].funcs:
                self._edge(src, fn_key(sub, fname))

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

    def script_rels(self, fs: FileSym) -> list[str]:
        """Indexed scripts for a scene, in resolution order: ext_resource
        scripts first (file order), the attached script only when none of
        them is indexed. One authoritative cascade — viz's signal-wire
        channel resolves against the same list (map-spec-v2 §1/F13)."""
        rels = [
            s_rel
            for s in fs.scripts
            if (s_rel := self._res_to_rel(s)) and s_rel in self.files
        ]
        if not rels and fs.attached_script:
            s_rel = self._res_to_rel(fs.attached_script)
            if s_rel and s_rel in self.files:
                rels.append(s_rel)
        return rels

    def _wire_tscn(self) -> None:
        for rel, fs in self.files.items():
            if fs.ext != ".tscn":
                continue
            # multi-script scenes: a handler may live on ANY of the scene's
            # script ext_resources, not just the first attached one
            for script_rel in self.script_rels(fs):
                for _, handler in fs.connections:
                    if handler in self.files[script_rel].funcs:
                        key = fn_key(script_rel, handler)
                        self.roots.add(key)
                        self._edge(rel + TSCN_SUFFIX, key, ty="signal")
                self._edge(rel + TSCN_SUFFIX, script_rel + TSCN_SUFFIX, ty="attach")
            for inst in fs.instances:
                inst_rel = self._res_to_rel(inst)
                if inst_rel and inst_rel in self.files:
                    self._edge(rel + TSCN_SUFFIX, inst_rel + TSCN_SUFFIX, ty="inst")

    def _res_to_rel(self, res_path: str) -> str:
        if not res_path:
            return ""
        return res_path.removeprefix("res://")

    def _resolve_include(self, src_rel: str, inc: str) -> str:
        """Repo-relative path for a quoted include of src_rel, or ''."""
        if inc in self.files:
            return inc
        parent = src_rel.rsplit("/", 1)[0] if "/" in src_rel else ""
        cand = f"{parent}/{inc}" if parent else inc
        return cand if cand in self.files else ""

    def _pair_cpp(self) -> None:
        """Give each .cpp its header's class identity (spec §2 pairing).

        Convention: a .cpp's first quoted include is its own header
        (path-ordered includes in engine code). The header stays the
        canonical class_map owner; the .cpp only fills in if unclaimed.
        """
        for rel in sorted(self.files):
            fs = self.files[rel]
            if fs.ext != ".cpp" or fs.class_name:
                continue
            own_stem = rel.rsplit("/", 1)[-1].split(".")[0]
            pair = ""
            for inc in sorted(fs.imported_modules):
                resolved = self._resolve_include(rel, inc)
                if not resolved or self.files[resolved].ext not in (".h", ".hpp"):
                    continue
                if resolved.rsplit("/", 1)[-1].split(".")[0] == own_stem:
                    pair = resolved
                    break  # exact-stem match wins outright
                pair = pair or resolved
            if pair and self.files[pair].class_name:
                fs.class_name = self.files[pair].class_name
                self.class_map.setdefault(fs.class_name, pair)

    def _wire_cpp(self) -> None:
        """Registration macros -> call edges + repo-wide GDVIRTUAL set.

        Binds/props live inside a containing function (usually
        _bind_methods): the owner is resolved by line order, mirroring how
        the engine runs registration at class-initialization time. Edge
        targets resolve file-locally first, then via class_map to the
        class's defining file. Roots come from ENTRY_RULES; these edges
        carry the call-graph wire (clusters/ PagerRank treat ty="call").
        """
        for rel in sorted(self.files):
            fs = self.files[rel]
            if fs.ext not in CPP_EXTS:
                continue
            try:
                text = nav._read_text(nav.ROOT / rel)
            except OSError:
                continue
            reg = harvest_registration(text)
            self.cpp_gdvirtuals.update(v.name for v in reg["gdvirtuals"])
            if not fs.funcs or (not reg["binds"] and not reg["props"]):
                continue
            order = sorted(fs.funcs.values(), key=lambda f: f.line)

            def container(lineno: int) -> str:
                owner = ""
                for f in order:
                    if f.line <= lineno:
                        owner = f.key
                    else:
                        break
                return owner

            def target(cls: str, name: str) -> str:
                if name in fs.funcs:
                    return fn_key(rel, name)
                class_file = self.class_map.get(cls, "")
                if class_file and name in self.files[class_file].funcs:
                    return fn_key(class_file, name)
                return ""

            for b in reg["binds"]:
                dst = target(b.cls, b.method)
                src = container(b.line)
                if dst and src:
                    self._edge(src, dst, ty="call")
            for p in reg["props"]:
                src = container(p.line)
                if not src:
                    continue
                for name in (p.setter, p.getter):
                    if not name:
                        continue
                    dst = target(fs.class_name, name)
                    if dst:
                        self._edge(src, dst, ty="call")

    def _scan_body_cpp(self, fs: FileSym, rel: str) -> None:
        """C++ body scan (issue #20): call / callback / instantiation edges.

        Sites come from extractors.cpp.scan_calls (query captures +
        memnew). Resolution mirrors the registration wiring: file-local
        funcs first, then the paired class's header via class_map. Sites
        that resolve to nothing are DROPPED for plain calls — an
        unresolved call name must never feed referenced_names (the
        same-name-elsewhere ambiguity guard) — but callback references
        (&fn / &C::fn) keep the name-aliteral liveness path: a function
        reachable ONLY through a function pointer is alive, and the
        engine's registrars are not ClassDB-shaped.
        """
        try:
            sites = scan_calls(nav.ROOT / rel, rel)
        except OSError:
            return
        if not sites or not fs.funcs:
            return
        order = sorted(fs.funcs.values(), key=lambda f: f.line)
        hdr = self.class_map.get(fs.class_name, "") if fs.class_name else ""

        def container(lineno: int) -> str:
            owner = ""
            for f in order:
                if f.line <= lineno:
                    owner = f.key
                else:
                    break
            return owner

        for site in sites:
            name = site["name"]
            kind = site["kind"]
            if kind == "new":
                cls_file = self.class_map.get(name, "")
                if cls_file and cls_file != rel:
                    src = container(site["line"])
                    if src:
                        self._edge(src, cls_file, ty="inst")
                continue
            parts = name.split("::")
            dst = ""
            if len(parts) == 2:
                cls_file = self.class_map.get(parts[0], "")
                if cls_file and parts[1] in self.files[cls_file].funcs:
                    dst = fn_key(cls_file, parts[1])
            elif name in fs.funcs:
                dst = fn_key(rel, name)
            elif hdr and name in self.files[hdr].funcs:
                dst = fn_key(hdr, name)
            if dst:
                src = container(site["line"])
                if src and src != dst:
                    self._edge(src, dst, ty="call")
            elif kind in ("fref", "frefq"):
                self.referenced_names.add(parts[-1])

    def _wire_aliases(self) -> None:
        """typedef/using targets -> alias edges to the aliased class's file
        (issue #20 T7). Cheap signal: an alias means the type is genuinely
        used; the edge keeps the defining file visible in the graph."""
        for rel in sorted(self.files):
            fs = self.files[rel]
            if fs.ext not in CPP_EXTS or not fs.aliases:
                continue
            for target in sorted(fs.aliases.values()):
                cls_file = self.class_map.get(target, "")
                if cls_file and cls_file != rel:
                    self._edge(rel, cls_file, ty="alias")

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
                if fs.ext not in (".gd", ".py") and fs.ext not in CPP_EXTS:
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
                if nxt not in seen and not nxt.endswith(TSCN_SUFFIX):
                    seen.add(nxt)
                    queue.append(nxt)
        self.reachable = seen

    def _mention_counts(self) -> Counter:
        """Corpus-wide identifier mention counts (issue #20 tier rule).

        One tokenizing pass over every indexed file's raw text — comments
        and string literals included — cached for the graph's lifetime;
        callers never rescan per candidate. Counting is order-independent,
        so determinism is unaffected.
        """
        if self._mentions is None:
            counts: Counter = Counter()
            for rel in sorted(self.files):
                try:
                    text = nav._read_text(nav.ROOT / rel)
                except OSError:
                    continue
                counts.update(MENTION_TOKEN_RE.findall(text))
            self._mentions = counts
        return self._mentions

    # -- queries -------------------------------------------------------------------

    def dead_code(self, limit: int = 60) -> dict[str, object]:
        dead = []
        # wildcard handler refs (*::name, cross-file signal handlers) keep
        # same-named funcs alive; precompute the bare-name set once instead
        # of rescanning self.referenced per candidate fn (O(fns x referenced)
        # -> O(referenced), issue #43). Membership-only set: it never
        # iterates into an output path, so determinism is unchanged.
        wildcard_names = {
            r.split("::")[-1] for r in self.referenced if r.startswith("*::")
        }
        # mention-count corroboration (issue #20): one cached tokenizing
        # pass over raw corpus text, never a scan per candidate. Only C++
        # candidates consume it, so corpora without C++ files skip the pass.
        mentions = (
            self._mention_counts()
            if any(f.ext in CPP_EXTS for f in self.files.values())
            else {}
        )
        for rel, fs in self.files.items():
            if fs.ext not in (".gd", ".py") and fs.ext not in CPP_EXTS:
                continue
            joined = "\n".join(fs.funcs[f].body for f in fs.funcs) if fs.funcs else ""
            dyn_re = CPP_DYNAMIC_RE if fs.ext in CPP_EXTS else DYNAMIC_HINT_RE
            file_is_dynamic = bool(dyn_re.search(joined))
            for name, fn in fs.funcs.items():
                if fn.key in self.reachable:
                    continue
                # wildcard handler refs (_on_x from any file) keep it alive
                if name in wildcard_names:
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
                    and name not in VIRTUALS
                    and (
                        name.startswith("_")
                        # python: stdlib serving machinery (http.server et al)
                        # invokes handler overrides reflectively — PY_HOOKS is
                        # the python analogue of the .gd underscore-virtual rule
                        or (fs.ext == ".py" and (name in PY_HOOKS or name.startswith("do_")))
                    )
                ):
                    tier = "review"
                # mention-count corroboration (issue #20): a cpp name that
                # keeps appearing across the corpus — unresolved same-name
                # call sites the ambiguity guard dropped, comments, string
                # dispatch tables — is wired somewhere the static pass
                # cannot see, so 'likely' overclaims its deadness. Names
                # mentioned only at their own definition stay 'likely'.
                if (
                    tier == "likely"
                    and fs.ext in CPP_EXTS
                    and mentions.get(name, 0) >= CPP_MENTION_FLOOR
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
                nxt |= {c for c in callees + callers if not c.endswith(TSCN_SUFFIX)}
            frontier = nxt - seen_keys
            if not frontier:
                break
        return "\n".join(out_lines[:limit])

    def _resolve(self, symbol: str) -> list[str]:
        hits = []
        for rel, fs in self.files.items():
            if symbol in fs.funcs:
                hits.append(fn_key(rel, symbol))
            if fs.class_name == symbol:
                hits.extend(fn_key(rel, f) for f in fs.funcs)
        if not hits:
            for rel, fs in self.files.items():
                for name in fs.funcs:
                    if symbol.lower() in name.lower():
                        hits.append(fn_key(rel, name))
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

    # -- file importance: pagerank + budgeted repo map -------------------------

    def file_wires(self) -> dict[str, dict[str, int]]:
        """File-level adjacency folded from the symbol graph: file ->
        {file: wire count}. A wire is one distinct symbol-to-symbol edge
        (call/signal/var); self-wires drop, scene pseudo-keys (`rel::tscn`)
        fold onto their scene file. Sorted + deterministic — the single
        implementation shared by pagerank/repo_map and recall's 1-hop
        expansion."""
        out: dict[str, dict[str, int]] = {rel: {} for rel in self.files}
        for src in sorted(self.edges):
            sfi = src.partition("::")[0]
            row = out.setdefault(sfi, {})
            for dst in sorted(self.edges[src]):
                dfi = dst.partition("::")[0]
                if sfi != dfi:
                    row[dfi] = row.get(dfi, 0) + 1
        return out

    def pagerank(self, damping: float = 0.85, iters: int = 30) -> dict[str, float]:
        """PageRank over the file wire graph (edge weight = wire count).
        Deterministic by construction: uniform init, exactly `iters`
        power iterations (fixed cap, no epsilon early-exit), files visited
        in sorted index order; dangling files (no out-wires) spread their
        mass uniformly so ranks sum to ~1."""
        wires = self.file_wires()
        fis = sorted(wires)
        n = len(fis)
        if n == 0:
            return {}
        idx = {fi: i for i, fi in enumerate(fis)}
        out_w = [sum(wires[fi].values()) for fi in fis]
        # incoming wires as (src index, weight); built in sorted src order
        # so the float accumulation order — and thus every rank — is fixed
        incoming: list[list[tuple[int, int]]] = [[] for _ in fis]
        for i, fi in enumerate(fis):
            for dst, w in sorted(wires[fi].items()):
                incoming[idx[dst]].append((i, w))
        base = (1.0 - damping) / n
        rank = [1.0 / n] * n
        for _ in range(iters):
            dangling = sum(r for r, w in zip(rank, out_w) if w == 0)
            spread = damping * dangling / n
            nxt = [0.0] * n
            for i in range(n):
                s = base + spread
                for src_i, w in incoming[i]:
                    s += damping * w * rank[src_i] / out_w[src_i]
                nxt[i] = s
            rank = nxt
        return {fi: rank[i] for i, fi in enumerate(fis)}

    def _symbol_degrees(self) -> dict[str, dict[str, int]]:
        """Per-file func name -> wire degree (out + in symbol edges).
        SIGNAL:/VAR:/tscn pseudo-key names never match a func name, so
        they fold out naturally."""
        deg = {rel: dict.fromkeys(fs.funcs, 0) for rel, fs in self.files.items()}
        for src in sorted(self.edges):
            sp, _, sn = src.partition("::")
            row = deg.setdefault(sp, {})
            if sn in row:
                row[sn] += len(self.edges[src])
        for dst in sorted(self.reverse):
            dp, _, dn = dst.partition("::")
            row = deg.setdefault(dp, {})
            if dn in row:
                row[dn] += len(self.reverse[dst])
        return deg

    def repo_map(self, budget_tokens: int = 2048) -> str:
        """Aider-style token-budgeted repo map: PageRank-ordered files,
        tree-grouped by directory, each file capped to its top signatures
        by symbol wire degree (god files get a slice, not the kitchen
        sink — MAP_MAX_SIGS). Hard budget stop measured in _toks (1 token
        ~= 4 chars). Byte-stable: same graph -> identical string."""
        rank = self.pagerank()
        deg = self._symbol_degrees()
        files = sorted(self.files, key=lambda rel: (-rank.get(rel, 0.0), rel))
        lines: list[str] = []
        used = 0
        dir_stack: list[str] = []
        for rel in files:
            fs = self.files[rel]
            dparts = rel.split("/")[:-1]
            # tree headers: emit only the directory parts that changed
            keep = 0
            while (
                keep < len(dir_stack)
                and keep < len(dparts)
                and dir_stack[keep] == dparts[keep]
            ):
                keep += 1
            block = [f"{'  ' * i}{p}/" for i, p in enumerate(dparts[keep:], start=keep)]
            dir_stack = dparts
            block.append(f"{'  ' * len(dparts)}{rel.split('/')[-1]}:")
            names = sorted(
                fs.funcs, key=lambda nm: (-deg.get(rel, {}).get(nm, 0), nm)
            )[:MAP_MAX_SIGS]
            sigs = [f"class {fs.class_name}"] if fs.class_name else []
            sigs.extend(
                f"{nm}({', '.join(p for p, _t in fs.funcs[nm].params)})" for nm in names
            )
            if sigs:
                block.append(f"{'  ' * (len(dparts) + 1)}{', '.join(sigs)}")
            for ln in block:
                t = _toks(ln)
                if used + t > budget_tokens:
                    return "\n".join(lines)
                lines.append(ln)
                used += t
        return "\n".join(lines)


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
    # per-config collection: two checkouts/projects sharing one .chroma dir
    # must not mix function vectors (hardcoded name collided across configs)
    return nav.fns_collection()


def _all_filesyms() -> dict[str, FileSym]:
    return get_graph().files


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
    return max(0.0, float(getattr(nav, "CHUNK_CAST", 0.0)))


_IF_DEDENT_RE = re.compile(r"^(\s+)else:|^(\s+)elif\s|^(\s+)except|^(\s+)finally:|^(\s+)catch|^(\s+)case\b|^(\s*)@(\w)|^(\s*)}$")
_OPEN_RE = re.compile(r"[(\[{]$")
_TRIPLE_RE = re.compile(r'"""|\'\'\'')


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


def _chunk_line_offsets(body: str) -> list[int]:
    """Line indices of top-level statement-block starts within a fn body
    PROPER (line 0 = the first statement, past the signature) — the cAST
    AST-boundary split points. base is the first statement's indent, so a
    later statement at that same indent (sequential or following a dedent)
    starts a new block, as do brace closures (`}`), decorators, and
    else/elif/catch lines. Bracket continuations never count: the opener
    line ends with an open bracket, continuation lines sit deeper than the
    statement indent, and closing-bracket lines start with the closer.
    Triple-quoted string content is skipped regardless of its indent (a
    heredoc can mine column-0 lines that merely LOOK like dedents).
    Deterministic — a pure function of the body text."""
    lines = body.splitlines()
    if not lines:
        return []
    base = len(lines[0]) - len(lines[0].lstrip(" \t"))
    out: list[int] = []
    prev_end_open = False
    in_triple: str | None = None

    def _find_triple(ln: str) -> tuple[str | None, str | None]:
        """(opener, rest) — the first triple-quote mark on the line, if any."""
        for mark in ('"""', "'''"):
            pos = ln.find(mark)
            if pos >= 0:
                return mark, ln[pos + 3 :]
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
            (ind <= base or _IF_DEDENT_RE.match(ln))
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


def _chunk_intro(fn: Func) -> str:
    """The fn's big-picture title line, if one opens the body: the first
    line of a docstring or a `#`/`##` comment. Kept short; anything else
    (a real first statement) is not an intro."""
    lines = fn.body.splitlines()
    if len(lines) < 2:
        return ""
    first = lines[1].strip()
    if first.startswith(('"""', "'''")):
        return first.strip("'\" ")[:48]
    if first.startswith("#") and len(first) <= 96:
        return first.lstrip("#").strip()[:48]
    return ""


def _chunk_docs(fn: Func, sig: str, blocks: list[int], scale: float = 1.0) -> list[tuple[str, int]]:
    """(doc, line) pairs for a monster fn's statement-block chunks. A body
    that opens with a docstring/title comment keeps that intro on EVERY
    later chunk (`# <first line>`), so prose retrieval does not lose the
    big-picture orientation (cAST keeps signature-first docs; a leading
    intro is part of the signature surface)."""
    intro = _chunk_intro(fn)
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
    if not lines[0].strip().endswith(":") and not lines[0].lstrip().startswith("func "):
        body = fn.body  # raw fragment: no signature line to strip
    else:
        body = "\n".join(lines[nb:]) if nb < len(lines) else ""
    return 0 < len(body) <= int(MICRO_FN_CHARS * scale)


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
            continue  # no class method above: nothing to merge into
        groups.setdefault(above[-1], []).append(fn)
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
    offs = _chunk_line_offsets(body_proper)
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
    for i, (chunk, aline) in enumerate(_chunk_docs(fn, sig, blocks, scale), 1):
        docs.append((chunk, aline, f"{fn.name}#chunk{i}"))
    return docs


def _chunk_plan(fs: FileSym, funcs: dict[str, Func], scale: float = 1.0) -> None:
    """Shape the fn docs for one file BEFORE the sync loop: fold micro fns
    into class context (mutating Func.kind/members), so the loop's
    per-fn _chunked_docs sees stable shapes. Deterministic — pure function
    of the parsed FileSym + funcs."""
    _overlay_class_context(funcs, fs, scale)


def sync_functions(changed: list[str], deleted: list[str]) -> dict[str, int]:
    """Re-embed functions of changed files, purge deleted files' functions,
    skipping unchanged ones via a per-entry content-hash cache (Cursor
    pattern): each stored fn keeps the sha256 of its embedded doc, so a
    rescan re-embeds only fns whose doc changed, refreshes metadata on
    line moves while reusing the stored vector, and purges fns that
    vanished from the file. At engine scale (~35k fns) the cold embed is
    ~2.3 h; the cache turns repeat rescans into minutes. Pre-cache
    collections migrate lazily: entries without a stored sha re-embed
    once. The dirty-marker self-heal still forces a full pass, but every
    cached skip verifies against the fresh parse, so dirty rebuilds stay
    correct and cheap. Purges resolve ids via where-get then
    delete(ids=...): chroma's delete(where=...) was observed to no-op
    silently under client churn while get(where=...) and delete(ids=...)
    stay reliable."""
    col = _fn_collection()
    cast = _cast_scale()  # nav CHUNK_CAST: 0.0 = legacy single-doc pass
    dirty = nav.DB_DIR / "fns.dirty"
    if dirty.is_file() and not changed:
        changed = sorted(rel for rel, fs in _all_filesyms().items() if fs.funcs)
    if col.count() == 0 and not changed:
        # first build: index every parsed function (any text language)
        changed = sorted(
            rel for rel, fs in _all_filesyms().items() if fs.funcs
        )
    populated = col.count() > 0
    purged_paths = 0
    purged_fns = 0

    def _purge_path(rel: str) -> None:
        nonlocal purged_paths, purged_fns
        got = col.get(where={"path": rel}, include=[])
        if got["ids"]:
            col.delete(ids=got["ids"])
            purged_paths += 1
            purged_fns += len(got["ids"])

    if populated and deleted:
        for p in sorted(set(deleted)):
            _purge_path(p)
    parser = Graph()
    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict[str, object]] = []
    moved: list[tuple[str, str, dict[str, object]]] = []
    cached = 0
    for rel in changed:
        path = nav.ROOT / rel
        suffix = path.suffix
        # scenes have no funcs; only languages with an extractor are
        # parseable — a file that left parseable space is purged, not kept
        if not path.is_file() or suffix not in nav.EXTS or suffix == ".tscn":
            if populated:
                _purge_path(rel)
            continue
        if suffix == ".gd":
            fs = parser._parse_gd(path, rel)
        else:
            fs = registry_for(suffix).parse(path, rel)
        if cast > 0.0:
            _chunk_plan(fs, fs.funcs, cast)  # cAST micro-fn merge (knob on)
        current: set[str] = set()
        existing: dict[str, dict[str, object]] = {}
        if populated:
            got = col.get(where={"path": rel}, include=["metadatas"])
            existing = {
                rid: meta or {}
                for rid, meta in zip(got["ids"], got["metadatas"])
            }
        for name, fn in fs.funcs.items():
            # signature line up front: better embeddings + agents see the IO
            # surface without opening the file
            sig = ", ".join(
                f"{p}: {t}" if t else p for p, t in fn.params
            )
            # chunk ids ride the fn-key grammar (never hand-built): the
            # key is fn_key(rel, ckey) with ckey = "name" or "name#chunkN",
            # so split_key() consumers resolve the parent file either way
            for doc, cline, ckey in _chunked_docs(fs, fn, sig, cast):
                crid = fn_key(rel, ckey)
                current.add(crid)
                sha = hashlib.sha256(doc.encode("utf-8")).hexdigest()
                meta: dict[str, object] = {
                    "path": rel, "name": name, "class_name": fs.class_name,
                    "line": cline, "sha": sha,
                }
                old = existing.get(crid)
                if old is not None and old.get("sha") == sha:
                    if old.get("line") == cline:
                        cached += 1
                        continue
                    moved.append((crid, doc, meta))  # line move: reuse vector
                    continue
                ids.append(crid)
                docs.append(doc)
                metas.append(meta)
        gone = sorted(set(existing) - current)
        if gone:
            col.delete(ids=gone)
            purged_fns += len(gone)
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
            for rid, doc, meta in moved:
                got = col.get(ids=[rid], include=["embeddings"])
                if rid not in got["ids"]:
                    raise RuntimeError(f"fn vector vanished for {rid}")
                vec = [float(x) for x in got["embeddings"][0]]
                col.upsert(ids=[rid], embeddings=[vec], documents=[doc],
                           metadatas=[meta])
                added += 1
    except Exception:
        dirty.write_text("sync failed", encoding="utf-8")
        raise
    dirty.unlink(missing_ok=True)
    return {
        "fns_upserted": added,
        "fns_cached": cached,
        "purged_paths": purged_paths,
        "purged_fns": purged_fns,
    }


def _fold_parents(rows: list[dict[str, object]], n: int) -> list[dict[str, object]]:
    """Rank-order collapse by (path, parent fn): a monster's name#chunkN
    docs are distinct chroma ids of ONE fn, so a raw top-k can be all
    siblings of a single fn (crowd-out — the #141 bench regression
    mechanism). Keeping each parent's best-ranked hit makes k results
    mean up to k distinct fns. Chunk docs carry the PARENT fn's name in
    metadata, so the fold needs no id surgery; a no-op when chunking is
    off (ids are unique per (path, name))."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, object]] = []
    for row in rows:
        key = (str(row.get("path", "")), str(row.get("func", "")))
        if key in seen:
            continue  # a name#chunkN sibling of an already-ranked fn
        seen.add(key)
        out.append(row)
        if len(out) >= n:
            break
    return out


def find_functions(query: str, n: int = 6) -> list[dict[str, object]]:
    """Semantic search over individual functions (vector index).
    Over-fetches 3n then collapses chunk siblings by (path, parent fn)
    (_fold_parents), so one monster's chunks cannot crowd out the
    top-k."""
    col = _fn_collection()
    count = col.count()
    if count == 0:
        return []
    vector = nav.embed([query])[0]
    got = col.query(
        query_embeddings=[vector],
        n_results=min(3 * n, count),
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
    return _fold_parents(out, n)


# -- repo map: module surface + budget metric -----------------------------------

MAP_MAX_SIGS = 8  # per-file signature cap: god files show a slice, not everything


def _toks(s: str) -> int:
    """Token estimate for repo-map budgeting: 1 token ~= 4 chars.
    Deterministic; the map's budget contract is measured in this metric."""
    return (len(s) + 3) // 4


def pagerank(damping: float = 0.85, iters: int = 30) -> dict[str, float]:
    """File-level PageRank over the shared graph singleton (wire-weighted)."""
    return get_graph().pagerank(damping=damping, iters=iters)


def repo_map(budget_tokens: int = 2048) -> str:
    """Budgeted repo map over the shared graph singleton."""
    return get_graph().repo_map(budget_tokens=budget_tokens)


# -- module-level singleton ---------------------------------------------------

_graph: Graph | None = None


def get_graph(rebuild: bool = False) -> Graph:
    global _graph
    if _graph is None or rebuild:
        _graph = Graph().build()
    return _graph
