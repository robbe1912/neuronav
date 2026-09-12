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
