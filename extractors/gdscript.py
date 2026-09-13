"""GDScript / Godot-scene extractor: source files -> FileSym.

Language-owned concerns live here: parsing (signatures, member
declarations, scene wiring) and entry-point rules (ENTRY_RULES — virtuals,
test roots, autoloads, tool bases). Graph-level analysis (edges,
reachability) stays in graph.py, which reaches this module through the
extractors registry.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

# extractors are leaf parsers: they read their own file and never import
# nav (nav -> extractors -> ... must never cycle back)
from extractors.model import FileSym
from extractors.common import entry_keys, merge_func, scan_indented_block

TAB_WIDTH = 4

FUNC_RE = re.compile(r"^([ \t]*)(?:static\s+)?func\s+([A-Za-z_]\w*)\s*\(")
SIGNAL_RE = re.compile(r"^[ \t]*signal\s+([A-Za-z_]\w*)")
CLASSNAME_RE = re.compile(r"^[ \t]*class_name\s+([A-Za-z_]\w*)")
EXTENDS_RE = re.compile(r"^[ \t]*extends\s+([A-Za-z_]\w*)")
# member-var type declarations at file top level: `var x: Type` / `var x := Type.new()`
MEMBER_TYPED_RE = re.compile(r"^(?:@onready\s+)?var\s+(\w+)\s*:\s*([A-Z]\w*)\s*(?:=|$)")
MEMBER_NEW_RE = re.compile(r"^(?:@onready\s+)?var\s+(\w+)\s*(?::=|=)\s*([A-Z]\w*)\.new\(")
# `const X = preload("res://path.gd")` — receiver resolves to that script
CONST_PRELOAD_RE = re.compile(
    r'^const\s+(\w+)\s*(?::[^=]+)?=\s*preload\("([^"]+)"\)'
)
# `@export var x: StringName = &"method"` / `var x := &"method"` — file-level
# name literals that may dispatch dynamically (BT task routing)
SN_DEFAULT_RE = re.compile(
    r'^\s*(?:@export\S*\s+)?var\s+\w+\s*(?::=|:\s*StringName\s*=|=)\s*&"([a-z_]\w*)"'
)
# exported method-name defaults (var name carries 'method'): BT/agent routing
# like `@export var start_method_name = "attack_start"` — plain quotes too,
# harvested unconditionally, not gated on dynamic-dispatch hints
METHOD_DEFAULT_RE = re.compile(
    r'^\s*(?:@export\S*\s+)?var\s+\w*method\w*\s*(?::=|:\s*StringName\s*=|=)\s*&?"([a-z_]\w*)"'
)
# inline property accessors: `@export var x: int = 0: set(v):` — the engine
# invokes the indented block below on export changes (set) / reads (get)
PROPERTY_ACCESSOR_RE = re.compile(
    r"^[ \t]*(?:@(?:onready|export)\S*\s+)*var\s+(\w+).+:\s*(set|get)"
    r"\s*\(\s*\w*\s*\)\s*:\s*$"
)
# next-line accessor form (equally valid Godot):
#   @export var x: T = v:
#       set(value):
#           body...
VAR_ACCESSOR_COLON_RE = re.compile(
    r"^[ \t]*(?:@(?:onready|export)\S*\s+)*var\s+(\w+).+:\s*$"
)
ACCESSOR_LINE_RE = re.compile(r"^[ \t]+(set|get)\s*\(\s*\w*\s*\)\s*:\s*$")
# class-level var initializers run at instantiation: bare calls inside the
# RHS expression (`var rise_curve: Curve = _make_overshoot_curve()`) keep
# their targets alive
VAR_INIT_RE = re.compile(
    r"^[ \t]*(?:@(?:onready|export)\S*\s+)*var\s+\w+[^=]*=(?!=)\s*(.+)$"
)
INIT_CALL_RE = re.compile(r"(?<![\w.$])([a-z_]\w*)\s*\(")
INIT_CALL_SKIP = {
    "if", "for", "while", "match", "return", "await", "super", "func",
    "preload", "load", "set", "get",
}
# animation method call tracks inside .tscn: "method": &"on_x" / "method": "on_x"
ANIM_METHOD_RE = re.compile(r'"method":\s*&?"(\w+)"')
TOOL_RE = re.compile(r"^[ \t]*(@tool|\btool\b)")
# @rpc-decorated funcs are network entry points (high-level multiplayer)
RPC_DECORATOR_RE = re.compile(r"^[ \t]*@rpc\b")
# property-accessor convention on scripts extending unresolvable (engine/
# addon) bases — the engine invokes these via property access
ENGINE_PROP_RE = re.compile(r"^_(get|set)_\w+$")


def _indent(line: str) -> int:

    expanded = line.expandtabs(TAB_WIDTH)
    return len(expanded) - len(expanded.lstrip(" "))


# ---- entry-point rules (language-owned, consumed by graph._find_roots) --------

VIRTUALS = {
    "_init", "_enter_tree", "_ready", "_exit_tree", "_process",
    "_physics_process", "_input", "_shortcut_input", "_unhandled_input",
    "_unhandled_key_input", "_gui_input", "_unhandled_mouse_input",
    "_draw", "_notification", "_get", "_set",
    "_get_property_list", "_to_string", "_integrate_forces",
    "_get_configuration_warnings",
    "_can_drop_data", "_get_drag_data", "_drop_data",
    "_make_custom_tooltip",
}
GUT_ROOTS = {"before_all", "after_all", "before_each", "after_each"}

# C++-side addon base classes dispatch these methods; unresolvable in a pure
# source graph (bases live in addons/*.gdext). Treated as entry roots.
ADDON_VIRTUALS: dict[str, set[str]] = {
    base: {"_enter", "_exit", "_tick", "_setup", "_generate_name"}
    for base in ("btaction", "btcondition", "btdecorator", "btcomposite", "bttask")
}


# entry bases that run from the editor/tooling, outside the game's call graph
# (compared against fs.extends.lower(), so store the lowercased spelling)
MANUAL_BASES = {"editorscript", "editorplugin", "scenetree"}

# native virtuals dispatched by unresolvable engine bases, beyond the
# _get_/_set_ property convention (C++ multiplayer extension surface)
ENGINE_VIRTUALS: dict[str, set[str]] = {
    base: {
        "_close", "_is_refusing_new_connections",
        "_set_refusing_new_connections", "_get_packet", "_put_packet",
        "_get_available_packet_count", "_get_max_packet_size",
        "_get_packet_peer",
    }
    for base in ("multiplayerpeer", "multiplayerpeerextension")
}


def _entry_dispatch(fs: FileSym, ctx) -> Iterator[str]:
    """Engine-dispatched virtuals, test roots, addon-dispatched methods."""
    if fs.ext != ".gd":
        return
    in_tests = fs.path.startswith("tests/")
    addon = ADDON_VIRTUALS.get(fs.extends.lower(), ())
    for name, fn in fs.funcs.items():
        if (
            name in VIRTUALS
            or name in GUT_ROOTS
            or name in addon
            or (in_tests and name.startswith("test_"))
        ):
            yield fn.key


def _entry_autoloads(fs: FileSym, ctx) -> Iterator[str]:
    """Autoload singletons: every func is an entry point."""
    if fs.path in set(ctx.autoloads.values()):
        for fn in fs.funcs.values():
            yield fn.key


def _entry_tooling(fs: FileSym, ctx) -> Iterator[str]:
    """Editor/tool scripts + data-constructed resources run outside the
    game's .gd call graph — whole files are entries."""
    if fs.ext != ".gd":
        return
    if (
        fs.is_tool
        or fs.extends.lower() in MANUAL_BASES
        or fs.path in ctx.tres_scripts
    ):
        for fn in fs.funcs.values():
            yield fn.key


def _entry_rpc(fs: FileSym, ctx) -> Iterator[str]:
    """@rpc-decorated funcs are invoked over the network — entry roots."""
    if fs.ext != ".gd":
        return
    yield from entry_keys(fs, fs.entry_hints)

def _entry_engine_props(fs: FileSym, ctx) -> Iterator[str]:
    """_get_*/_set_* property accessors (and per-base native virtuals) on
    scripts extending engine or addon bases (not resolvable in class_map)
    are dispatched natively."""
    if fs.ext != ".gd" or not fs.extends or fs.extends in ctx.class_map:
        return
    native = ENGINE_VIRTUALS.get(fs.extends.lower(), ())
    for name, fn in fs.funcs.items():
        if ENGINE_PROP_RE.match(name) or name in native:
            yield fn.key


# contract: each rule is callable(fs, ctx) -> iterable of entry func keys;
# ctx is the Graph under construction (exposes .autoloads, .tres_scripts, ...)
ENTRY_RULES = [
    _entry_dispatch,
    _entry_autoloads,
    _entry_tooling,
    _entry_rpc,
    _entry_engine_props,
]


# ---- declared IO surface (params / return type / state writes) ------------
# powers the fn panel signature line, the "writes state" label badge and the
# mutators-only filter in the viz. Purely syntactic: member writes = `self.x =`
# (GDScript 2 requires self for member assignment), param mutation = a param
# name followed by a known mutating method call.
_SIG_PARENS_RE = re.compile(r"\((.*)\)", re.S)
_RET_RE = re.compile(r"->\s*([A-Za-z_][\w.]*)")
_PARAM_RE = re.compile(r"^([A-Za-z_]\w*)\s*(?::\s*([A-Za-z_][\w.]*))?")

# mutators callable on Array/Dictionary/pass-by-ref objects
_MUTATING_METHODS = {
    "append", "append_array", "assign", "clear", "erase", "insert", "pop",
    "pop_back", "pop_front", "push_back", "push_front", "remove", "remove_at",
    "resize", "reverse", "sort", "sort_custom", "shuffle", "fill",
}


def _split_top_commas(s: str) -> list:
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _parse_signature(header: str, fname: str) -> tuple:
    """-> ([(param, type)], ret) from a func header (multi-line OK)."""
    m = _SIG_PARENS_RE.search(header)
    params: list = []
    if m:
        for chunk in _split_top_commas(m.group(1)):
            if chunk == "":
                continue
            pm = _PARAM_RE.match(chunk)
            if pm and pm.group(1) not in ("const", "ref"):  # not a bare keyword
                params.append((pm.group(1), pm.group(2) or ""))
    rm = _RET_RE.search(header)
    ret = rm.group(1) if rm else ""
    return params, ret


def _scan_io(body: str, params: list, member_names: set) -> tuple:
    """-> (writes, mut_params) member/param mutation sets for a body."""
    writes = set(re.findall(r"\bself\.([A-Za-z_]\w*)\s*=(?!=)", body))
    # augmented member writes too: self.hp -= 1
    writes |= set(re.findall(r"\bself\.([A-Za-z_]\w*)\s*(?:\+|-|\*|/|%)=(?!=)", body))
    # GDScript idiom: bare member assignment without self. — only counts when
    # the name is a declared member of this file and not shadowed by a local
    # (var declaration) or a parameter.
    locals_ = set(re.findall(r"\bvar\s+([A-Za-z_]\w*)", body)) | {p for p, _t in params}
    for m in re.finditer(r"^[ \t]*([A-Za-z_]\w*)\s*(?:\+|-|\*|/)?=(?!=)", body, re.M):
        n = m.group(1)
        if n in member_names and n not in locals_:
            writes.add(n)
    pnames = {p for p, _t in params}
    mut = set()
    for pm in re.finditer(r"\b([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*\(", body):
        if pm.group(1) in pnames and pm.group(2) in _MUTATING_METHODS:
            mut.add(pm.group(1))
    return writes, mut


def parse_gd(path: Path, rel: str) -> FileSym:
    text = path.read_text(encoding="utf-8", errors="replace")
    fs = FileSym(path=rel, ext=".gd")
    lines = text.splitlines()
    i = 0
    rpc_pending = False
    # file-top triple-quoted strings (license headers, help text, dialogue/
    # template constants) are DATA: their content must never parse as
    # declarations. Same odd-count sentinel the body scans and python.py's
    # module loop already track — the top-level loop was the missed one (#106).
    in_tq = ""
    while i < len(lines):
        line = lines[i]
        if in_tq:
            if line.count(in_tq) % 2 == 1:
                in_tq = ""  # closer seen (odd count may also reopen — rare, accept)
            i += 1
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        # triple quotes ANYWHERE on the line: everything after the first
        # delimiter is string content; only the prefix stays parseable.
        # Complete (even-count) strings vanish entirely.
        for q in ('"""', "'''"):
            cnt = line.count(q)
            if cnt:
                if cnt % 2 == 1:
                    in_tq = q
                line = line.split(q)[0]
                break
        if not line.strip():
            i += 1
            continue
        m = CLASSNAME_RE.match(line)
        if m:
            fs.class_name = m.group(1)
            i += 1
            continue
        m = EXTENDS_RE.match(line)
        if m:
            fs.extends = m.group(1)
            i += 1
            continue
        if TOOL_RE.match(line) and i < 4:
            fs.is_tool = True
            i += 1
            continue
        m = SIGNAL_RE.match(line)
        if m:
            fs.signals.add(m.group(1))
            i += 1
            continue
        m = PROPERTY_ACCESSOR_RE.match(line)
        varname = kind = None
        body_start = i + 1
        if m:
            varname, kind = m.group(1), m.group(2)
        else:
            mv = VAR_ACCESSOR_COLON_RE.match(line)
            if mv:
                k = i + 1
                while k < len(lines) and lines[k].strip() == "":
                    k += 1
                am = ACCESSOR_LINE_RE.match(lines[k]) if k < len(lines) else None
                if am:
                    varname, kind = mv.group(1), am.group(1)
                    body_start = k + 1
        if varname:
            # `set(v):` / `get():` accessor block under a class-level var —
            # parse as a rooted pseudo-func so its body's calls stay alive.
            # Small accessors then fold into class context like any other
            # micro fn (graph _overlay_class_context merges by line
            # proximity, cAST issue #76).
            base = _indent(line)
            body, j = scan_indented_block(lines, body_start, base, _indent)
            pname = f"_{kind}_{varname}"
            merge_func(fs.funcs, rel, pname, i + 1, body)
            fs.entry_hints.add(pname)
            i = j
            continue
        m = METHOD_DEFAULT_RE.match(line)
        if m:
            fs.name_literals.add(m.group(1))
            i += 1
            continue
        m = SN_DEFAULT_RE.match(line)
        if m:
            fs.name_literals.add(m.group(1))
            i += 1
            continue
        m = VAR_INIT_RE.match(line)
        if m:
            # bare calls in the initializer RHS run at instantiation —
            # harvest names, then fall through so typed/new/const branches
            # can still claim this line
            for cm in INIT_CALL_RE.finditer(m.group(1)):
                nm = cm.group(1)
                if nm not in INIT_CALL_SKIP:
                    fs.init_calls.add(nm)
        m = MEMBER_TYPED_RE.match(line)
        if m:
            fs.members[m.group(1)] = m.group(2)
            i += 1
            continue
        m = MEMBER_NEW_RE.match(line)
        if m:
            fs.members[m.group(1)] = m.group(2)
            i += 1
            continue
        m = CONST_PRELOAD_RE.match(line)
        if m:
            fs.consts[m.group(1)] = m.group(2).removeprefix("res://")
            i += 1
            continue
        if RPC_DECORATOR_RE.match(line):
            rpc_pending = True
            i += 1
            continue
        m = FUNC_RE.match(line)
        if m:
            name = m.group(2)
            if rpc_pending:
                fs.entry_hints.add(name)
                rpc_pending = False
            base = _indent(line)
            # consume multi-line signatures: keep joining until parens
            # balance and the header terminates with ':'
            header = line
            j = i + 1
            while header.count("(") > header.count(")") and j < len(lines):
                header += "\n" + lines[j]
                j += 1
            if "\n" in header and not header.rstrip().endswith(":") and j < len(lines):
                header += "\n" + lines[j]
                j += 1
            body, j = scan_indented_block(lines, j, base, _indent)
            io_params, io_ret = _parse_signature(header, name)
            # inner classes may legally re-declare a func name; merge
            # conservatively so edges from BOTH bodies survive
            merge_func(fs.funcs, rel, name, i + 1, body, params=io_params, ret=io_ret)
            i = j
            continue
        i += 1
    # IO scan runs after the whole file is parsed so member declarations that
    # appear after a func still count (GDScript allows late member decls).
    member_names = set(fs.members)
    for fn in fs.funcs.values():
        fn.writes, fn.mut_params = _scan_io(fn.body, fn.params, member_names)
    return fs


def parse_tscn(path: Path, rel: str) -> FileSym:
    text = path.read_text(encoding="utf-8", errors="replace")
    fs = FileSym(path=rel, ext=".tscn")
    ext_ids: dict[str, str] = {}
    for line in text.splitlines():
        m = re.search(
            r'\[ext_resource type="Script"[^]]*path="([^"]+)"[^]]*id="([^"]+)"',
            line,
        )
        if not m:
            m = re.search(
                r'\[ext_resource type="Script"[^]]*id="([^"]+)"[^]]*path="([^"]+)"',
                line,
            )
            if m:
                ext_ids[m.group(1)] = m.group(2)
                fs.scripts.append(m.group(2))
                continue
        else:
            ext_ids[m.group(2)] = m.group(1)
            fs.scripts.append(m.group(1))
        if fs.scripts:
            fs.attached_script = fs.attached_script or fs.scripts[0]
        m = re.search(r'\[ext_resource type="PackedScene"[^]]*path="([^"]+)"[^]]*id="([^"]+)"', line)
        if m:
            ext_ids[m.group(2)] = m.group(1)
            continue
        m = re.search(r'instance=ExtResource\("([^"]+)"\)', line)
        if m and m.group(1) in ext_ids:
            fs.instances.append(ext_ids[m.group(1)])
        m = re.search(
            r'\[connection signal="(\w+)"[^\]]*to="[^"]*"[^\]]*method="(\w+)"',
            line,
        )
        if m:
            fs.connections.append((m.group(1), m.group(2)))
    # animation method call tracks fire funcs exactly like signal handlers
    for m in ANIM_METHOD_RE.finditer(text):
        fs.connections.append(("anim", m.group(1)))
    return fs


def parse(path: Path, rel: str) -> FileSym:
    """Registry entry point: dispatch on file suffix."""
    if path.suffix == ".tscn":
        return parse_tscn(path, rel)
    return parse_gd(path, rel)

# ---- uniform shared-surface hooks (langsep) -----------------------------------
# Language facts consumed blind by graph/nav/bake/server. Bodies mirror the
# graph.py expressions they replace byte-for-byte; the cutover commit
# switches shared modules onto them.

# Godot-profile default walk (nav WALK_DEFAULTS fallback).
WALK_EXTS = (".gd", ".tscn")
# The scene-wiring suffix PAIR (deliberate siblings, not a merged set):
# SCENE_WIRING_SUFFIXES walks custom-resource files that CARRY wiring
# (.tres ext_resource/StringName routing — harvest_scene_wiring walks
# these beside the indexed tree); SCENE_FILE_SUFFIXES marks the scene
# documents THEMSELSES (.tscn — parsed for wiring shape only, never
# fn-indexed: sync_functions reparses skip them, bake pair passes route
# around them). Same family, different walks; shared modules consume
# both through the registry and never spell a language suffix.
SCENE_WIRING_SUFFIXES = frozenset({".tres"})
SCENE_FILE_SUFFIXES = frozenset({".tscn"})
# Dead-tier underscore shield, corpus-wide: Godot virtual names keep a
# function out of the review tier on unresolved bases for EVERY language —
# the shared rule consults this set for .py and .cpp files too (LJ-3).
UNDERSCORE_SHIELD = VIRTUALS
# GDScript func head keyword (graph _is_micro heuristic sniff).
FUNC_KEYWORD = "func "

from extractors.common import DYNAMIC_HINT_RE  # noqa: E402  (kept with the hooks it serves)

DYNAMIC_HINT = DYNAMIC_HINT_RE


def res_to_rel(p: str) -> str:
    """Repo-relative path from a res:// path. Logic sites only — display
    strings keep their literals (presentation is not language behavior)."""
    return p[len("res://"):] if p.startswith("res://") else p


def stat_tags(text: str) -> tuple[str, str]:
    """(class_name, extends) header sniff for nav's stat fingerprint."""
    cls = ext = ""
    for line in text.splitlines():
        s = line.strip()
        if not cls and s.startswith("class_name "):
            cls = s.split(None, 1)[1].split()[0]
        elif not ext and s.startswith("extends "):
            ext = s.split(None, 1)[1].split()[0]
    return cls, ext


def counts_dead_share(fs: FileSym) -> bool:
    """Dead-share denominator counts gd files only — preserves today's
    behavior that py/cpp dead files never flag dead_weight."""
    return fs.ext == ".gd"


def is_entry_exempt(name: str) -> bool:
    """Cpp-only rule (implicit entries); gd names never exempt here."""
    return False


def unresolved_base_review(name: str) -> bool:
    """Underscore-rule tail for gd: engine-virtual convention."""
    return name.startswith("_")


def stand_in_review(fs: FileSym, name: str) -> bool:
    """Python-only rule (module-scope stand-ins)."""
    return False


def mention_review(name: str, mentions: dict) -> bool:
    """Cpp-only rule (mention floor)."""
    return False


# ---- gd body-scan + wiring patterns (moved from graph.py, langsep) -------------
# "res://...something.gd" string literals in bodies: dynamically loaded
# scripts whose funcs must count as alive
RES_LOAD_RE = re.compile(r"res://([\w/.-]+\.gd)")
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

# project.godot [autoload] entry: Name = "*res://path/to.gd" (the * marks
# a scene-backed singleton; the script form is what the graph indexes)
AUTOLOAD_RE = re.compile(r'^(\w+)\s*=\s*"\*?res://([\w/.-]+\.gd)"')
# asset scenes sit outside the search index; graph parses them for wiring
ASSET_SCENE_GLOB = "*.tscn"


# ---- build passes + body scan (langsep: moved from graph.py, ctx=Graph) -------
from extractors.common import (  # noqa: E402
    BARE_CALL_RE,
    FN_KEY_SEP,
    MEMBER_ACCESS_RE,
    QUALIFIED_CALL_RE,
    SIGNAL_PREFIX,
    TSCN_SUFFIX,
    VAR_PREFIX,
    fold_continuations,
    fn_key,
)


def build_inheritance(ctx) -> None:
    """Transitive subclass map: base class_name -> files below it — a
    call resolved to a base may dispatch to any override."""
    for rel, fs in ctx.files.items():
        if fs.ext != ".gd":
            continue
        base = fs.extends
        seen: set[str] = set()
        while base and base not in seen and base in ctx.class_map:
            seen.add(base)
            ctx._subclasses[base].add(rel)
            base = ctx.files[ctx.class_map[base]].extends
        # path-form extends (often inner helper classes): register the
        # whole file under the base file's class_name so override
        # completion can reach it
        try:
            raw = ctx.read_file(rel)
        except OSError:
            raw = ""
        for m in PATH_EXTENDS_RE.finditer(raw):
            base_rel = res_to_rel(m.group(1))
            base_cls = ctx.files.get(base_rel, None)
            if base_cls is not None:
                # register under the rel path always, class_name when
                # the base declares one — the emitter looks up both
                ctx._subclasses[base_rel].add(rel)
                if base_cls.class_name:
                    ctx._subclasses[base_cls.class_name].add(rel)


def harvest_facts(fs: FileSym, ctx) -> None:
    """Name-literal liveness + dynamic-dispatch file gating for .gd."""
    if fs.ext != ".gd":
        return
    for nm in fs.name_literals:
        if len(nm) > 3:
            ctx.referenced_names.add(nm)
    # class-level initializer calls run at instantiation — alive
    for nm in fs.init_calls:
        ctx.referenced_names.add(nm)
    if not fs.funcs:
        return
    joined = "\n".join(f.body for f in fs.funcs.values())
    if DYNAMIC_HINT_RE.search(joined):
        ctx._dyn_files.add(fs.path)


def scan_file(fs: FileSym, ctx) -> None:
    if fs.ext != ".gd":
        return
    for fn in fs.funcs.values():
        _scan_body(fs, fn, ctx)


def _scan_body(fs: FileSym, fn: Func, ctx) -> None:
    # multi-line call arguments defeat line-based regex passes: fold
    # continuation lines (unbalanced parens/brackets) into single
    # logical lines before scanning; fn.body stays raw for display
    scan_text = fold_continuations(fn.body)
    # first-order type inference: member vars + typed params/locals in this body
    var_types = dict(fs.members)
    for pm in PARAM_TYPED_RE.finditer(scan_text):
        var_types[pm.group(1)] = pm.group(2)
    _scan_calls(fs, fn, scan_text, var_types, ctx)
    _scan_liveness(fs, fn, scan_text, ctx)
    _scan_chains(fs, fn, scan_text, var_types, ctx)
    _scan_signals(fs, fn, scan_text, ctx)


def _scan_calls(fs: FileSym, fn: Func, scan_text: str, var_types: dict, ctx) -> None:
    """Typed-receiver call edges and member-var cross-references."""
    src_key = fn.key
    for m in QUALIFIED_CALL_RE.finditer(scan_text):
        head, fname = m.group(1), m.group(2)
        cls = head if head in ctx.class_map else var_types.get(head)
        if cls and cls in ctx.class_map:
            dst = ctx.class_map[cls]
            if fname in ctx.files[dst].funcs:
                ctx._emit_call(src_key, dst, fname)
        elif head in fs.consts and fs.consts[head] in ctx.files:
            dst = fs.consts[head]
            if fname in ctx.files[dst].funcs:
                ctx._emit_call(src_key, dst, fname)
        else:
            # receiver type unknown (factory returns, variants) — the call may
            # dispatch to any same-named func; mark name alive, no edge.
            # dispatch intermediaries (.rpc()/.call_deferred()/.bind()) point
            # at the RECEIVER, not at rpc/call_deferred themselves
            if fname in DYNAMIC_METHODS:
                # builtin-shadowing user funcs (e.g. a user `bind`) are
                # valid targets of the same dispatch — keep the name
                # alive alongside the receiver head
                ctx.referenced_names.add(head)
            ctx.referenced_names.add(fname)
    # member-var cross-references: receiver.member where the receiver
    # resolves to a known class (same chain as calls above) and that
    # file actually declares the member — edges land on VAR: pseudo-nodes
    for m in MEMBER_ACCESS_RE.finditer(scan_text):
        head, member = m.group(1), m.group(2)
        cls = head if head in ctx.class_map else var_types.get(head)
        if cls and cls in ctx.class_map:
            dst = ctx.class_map[cls]
        elif head in fs.consts and fs.consts[head] in ctx.files:
            dst = fs.consts[head]
        else:
            continue
        if member in ctx.files[dst].members:
            ctx._edge(src_key, dst + VAR_PREFIX + member, ty="var")
        elif member in ctx.files[dst].funcs:
            # property-assignment form: obj.method = x targets the
            # func (setter-style) without a call paren
            ctx._emit_call(src_key, dst, member)


def _scan_liveness(fs: FileSym, fn: Func, scan_text: str, ctx) -> None:
    """Name-keeping harvest: dynamically loaded scripts, dynamic-
    dispatch string refs, callback-convention identifiers. No edges —
    these only keep funcs out of dead-code tiers."""
    src_key = fn.key
    # dynamically loaded scripts: any "res://....gd" string literal in
    # the body keeps every func of that file alive
    for m in RES_LOAD_RE.finditer(scan_text):
        loaded = m.group(1)
        if loaded in ctx.files:
            for other in ctx.files[loaded].funcs.values():
                ctx.referenced.add(other.key)
    # dynamic-dispatch harvest: method names passed to .call()/.rpc()/
    # has_method(), StringName literals, Callable(obj, "m") — receivers
    # are runtime-typed, so mark the names alive instead of an edge
    for m in DISPATCH_STR_RE.finditer(scan_text):
        nm = m.group(1)
        if len(nm) > 3:
            ctx.referenced_names.add(nm)
    for m in BARE_DISPATCH_STR_RE.finditer(scan_text):
        nm = m.group(1)
        if len(nm) > 3:
            ctx.referenced_names.add(nm)
    for m in STRINGNAME_LIT_RE.finditer(scan_text):
        ctx.referenced_names.add(m.group(1))
    if fs.path in ctx._dyn_files:
        for m in QUOTED_IDENT_RE.finditer(scan_text):
            ctx.referenced_names.add(m.group(1))
    for m in CALLABLE_TWO_RE.finditer(scan_text):
        nm = m.group(1) or m.group(2)
        if nm and len(nm) > 3:
            ctx.referenced_names.add(nm)
    # tween binders + bare callback-convention identifiers (array
    # elements, deferred refs): method refs without call parens
    for m in TWEEN_ARG_RE.finditer(scan_text):
        ctx.referenced_names.add(m.group(1))
    for m in BARE_HANDLER_RE.finditer(scan_text):
        ctx.referenced_names.add(m.group(0))
    # bare method-ref as full assignment RHS (property-assignment
    # wiring): `hub.cb = _connect_signal_handler` — scanned
    # on the RAW body because the $ anchor needs real line ends
    for m in ASSIGN_RHS_RE.finditer(fn.body):
        nm = m.group(1)
        if nm not in ASSIGN_RHS_SKIP:
            ctx.referenced_names.add(nm)


def _scan_chains(fs: FileSym, fn: Func, scan_text: str, var_types: dict, ctx) -> None:
    """Two-level typed chains, casts, and bare/inherited calls."""
    src_key = fn.key
    # two-level typed chains: ctx.teams.team_ids(...) — resolve head to
    # its class, hop through a declared member, then emit
    for m in CHAIN_CALL_RE.finditer(scan_text):
        head, mid, tail = m.group(1), m.group(2), m.group(3)
        dst = ctx._chain_dst(var_types, head, mid)
        if dst and tail in ctx.files[dst].funcs:
            ctx._emit_call(src_key, dst, tail)
        else:
            # unresolvable receiver chain (duck-typed containers):
            # same name-alive fallback as single-hop unknown receivers
            ctx.referenced_names.add(tail)
    for m in CHAIN_VAR_RE.finditer(scan_text):
        head, mid, tail = m.groups()
        dst = ctx._chain_dst(var_types, head, mid)
        if dst and tail in ctx.files[dst].members:
            ctx._edge(src_key, dst + VAR_PREFIX + tail, ty="var")
        elif dst and tail in ctx.files[dst].funcs:
            ctx._emit_call(src_key, dst, tail)
    for m in AS_CAST_CALL_RE.finditer(scan_text):
        cls, fname = m.group(1), m.group(2)
        if cls in ctx.class_map:
            dst = ctx.class_map[cls]
            if fname in ctx.files[dst].funcs:
                ctx._emit_call(src_key, dst, fname)
        else:
            ctx.referenced_names.add(fname)
    for m in BARE_CALL_RE.finditer(scan_text):
        name = m.group(1)
        if name in NON_CALLS:
            continue
        if name in fs.funcs:
            # the emitter mirrors the same-file edge onto subclass
            # overrides (incl. path-form extends files below)
            ctx._emit_call(src_key, fs.path, name)
        else:
            # inherited method call: resolve up the extends chain
            # (the emitter mirrors onto sibling overrides); base calls
            # a func it does not declare -> every subclass override
            anc = ctx._ancestor_def(fs, name)
            if anc:
                ctx._emit_call(src_key, anc, name)
            if fs.class_name and fs.class_name in ctx._subclasses:
                for sub in ctx._subclasses[fs.class_name]:
                    if name in ctx.files[sub].funcs:
                        ctx._edge(src_key, fn_key(sub, name))


def _scan_signals(fs: FileSym, fn: Func, scan_text: str, ctx) -> None:
    """Signal emits -> signal nodes; connect/Callable string refs -> handlers."""
    src_key = fn.key
    # signal emits -> signal nodes; connect/Callable string refs -> handlers
    for m in EMIT_RE.finditer(scan_text):
        sig = m.group(1) or m.group(2)
        if sig in fs.signals:
            ctx._edge(src_key, fs.path + SIGNAL_PREFIX + sig, ty="signal")
    if CONNECT_RE.search(scan_text):
        for m in STRING_NAME_RE.finditer(scan_text):
            ref = m.group(1)
            if ref in fs.funcs:
                ctx._edge(src_key, fn_key(fs.path, ref), ty="signal")
                # handlers fire on signal emit — entry points, traverse
                ctx.roots.add(fn_key(fs.path, ref))
            # cross-file: _on_* handlers commonly target other scripts
            elif ref.startswith("_on_"):
                ctx.referenced.add("*" + FN_KEY_SEP + ref)
        # direct method references (no quotes):
        #   sig.connect(_handler) / is_connected(_handler) / disconnect(...)
        for m in CONNECT_METHOD_RE.finditer(scan_text):
            ref = m.group(1)
            if ref in fs.funcs:
                ctx._edge(src_key, fn_key(fs.path, ref), ty="signal")
                ctx.roots.add(fn_key(fs.path, ref))
            else:
                # inherited handler: resolve up the extends chain
                anc = ctx._ancestor_def(fs, ref)
                if anc and ref in ctx.files[anc].funcs:
                    key = fn_key(anc, ref)
                    ctx._edge(src_key, key, ty="signal")
                    ctx.roots.add(key)


def wire_tscn(ctx) -> None:
    """Scene wiring: [connection] handlers as roots + attach/inst edges."""
    for rel, fs in ctx.files.items():
        if fs.ext != ".tscn":
            continue
        # multi-script scenes: a handler may live on ANY of the scene's
        # script ext_resources, not just the first attached one
        for script_rel in ctx.script_rels(fs):
            for _, handler in fs.connections:
                if handler in ctx.files[script_rel].funcs:
                    key = fn_key(script_rel, handler)
                    ctx.roots.add(key)
                    ctx._edge(rel + TSCN_SUFFIX, key, ty="signal")
            ctx._edge(rel + TSCN_SUFFIX, script_rel + TSCN_SUFFIX, ty="attach")
        for inst in fs.instances:
            inst_rel = res_to_rel(inst)
            if inst_rel and inst_rel in ctx.files:
                ctx._edge(rel + TSCN_SUFFIX, inst_rel + TSCN_SUFFIX, ty="inst")


def harvest_scene_wiring(ctx) -> None:
    """.tres/.res reference scripts via ext_resource — data-constructed
    classes (custom resources) whose funcs never appear in .gd callers —
    and StringName values route dynamic dispatch (LimboAI BT tasks
    export method names). Root-wide but pruned (issue #117): the walk
    comes through ctx so it honors the config's exclude contract + the
    standard cache floor."""
    ctx.tres_scripts = set()
    for path in ctx.walk_root_files(SCENE_WIRING_SUFFIXES):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in TRES_SCRIPT_RE.finditer(text):
            rel2 = res_to_rel(m.group(1))
            if rel2 in ctx.files:
                ctx.tres_scripts.add(rel2)
        for m in TRES_STRINGNAME_RE.finditer(text):
            ctx.referenced_names.add(m.group(1))


def is_wiring_only(fs: FileSym) -> bool:
    """True when the file exists in the graph only as scene wiring (no
    funcs to scan; sync reparses skip it; bake pair passes route around
    it). Scene files, not scripts."""
    return fs.ext == ".tscn"


# registry choreography binds (langsep): BUILD_SEQUENCE/WIRE_SEQUENCE
# dispatch these through the package attribute surface, which the
# module scan cannot see — one module-var bind per hook keeps the
# value-ref arm of the liveness scan honest (the same invariant that
# keeps ENTRY_RULES-listed rules alive).
_PASS_WIRING_HARVEST = harvest_scene_wiring
_PASS_INHERITANCE = build_inheritance
_PASS_TSCN_WIRE = wire_tscn


# ---- path-level predicates + project autoload harvest (langsep PR2) -----------
# SCRIPT_FILE_SUFFIXES completes the documented scene pair above: script
# documents (.gd) vs scene documents (.tscn) vs custom-resource wiring
# walk (.tres). Consumers get predicates, never suffix literals.
SCRIPT_FILE_SUFFIXES = frozenset({".gd"})


def is_scene_path(p: str) -> bool:
    """True when the repo-relative path names a scene document."""
    return p.endswith(tuple(sorted(SCENE_FILE_SUFFIXES)))


def is_script_path(p: str) -> bool:
    """True when the repo-relative path names a GDScript document."""
    return p.endswith(tuple(sorted(SCRIPT_FILE_SUFFIXES)))


# the wide form: scene-backed singletons (the `*` spelling) carry any ext;
# script-only consumers pass scripts_only=True
AUTOLOAD_ANY_RE = re.compile(r'^(\w+)\s*=\s*"\*?res://([\w/.-]+\.\w+)"')
PROJECT_FILE = "project.godot"


def harvest_autoloads(root: Path, scripts_only: bool = False) -> dict[str, str]:
    """project.godot [autoload] section: singleton name -> rel path.
    ONE home, TWO consumers (graph.py's entry map, clusters.py's inverse
    labeler map) — this replaces the pre-langsep copy-paste in both.
    scripts_only drops scene-backed singletons (graph's historical shape
    indexes scripts only)."""
    out: dict[str, str] = {}
    pg = root / PROJECT_FILE
    if not pg.is_file():
        return out
    in_auto = False
    for line in pg.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("[autoload]"):
            in_auto = True
            continue
        if s.startswith("["):
            in_auto = False
        if in_auto:
            m = AUTOLOAD_ANY_RE.match(s)
            if m and (not scripts_only or is_script_path(m.group(2))):
                out[m.group(1)] = m.group(2)
    return out


_PASS_AUTOLOADS = harvest_autoloads
