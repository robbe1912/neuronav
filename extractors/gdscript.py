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

import nav
from extractors.model import FileSym, Func

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
    for name in fs.entry_hints:
        fn = fs.funcs.get(name)
        if fn:
            yield fn.key


def _entry_engine_props(fs: FileSym, ctx) -> Iterator[str]:
    """_get_*/_set_* property accessors on scripts extending engine or
    addon bases (not resolvable in class_map) are dispatched natively."""
    if fs.ext != ".gd" or not fs.extends or fs.extends in ctx.class_map:
        return
    for name, fn in fs.funcs.items():
        if ENGINE_PROP_RE.match(name):
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


def parse_gd(path: Path, rel: str) -> FileSym:
    text = nav._read_text(path)
    fs = FileSym(path=rel, ext=".gd")
    lines = text.splitlines()
    i = 0
    rpc_pending = False
    while i < len(lines):
        line = lines[i]
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
        m = SN_DEFAULT_RE.match(line)
        if m:
            fs.name_literals.add(m.group(1))
            i += 1
            continue
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
            body_lines: list[str] = []
            # triple-quoted strings can contain column-0 content that only
            # LOOKS like a dedent — track string state while consuming
            in_tq = False
            while j < len(lines):
                nxt = lines[j]
                if in_tq:
                    body_lines.append(nxt)
                    if nxt.count('"""') % 2 == 1 or nxt.count("'''") % 2 == 1:
                        in_tq = False
                    j += 1
                    continue
                if nxt.strip() == "":
                    body_lines.append(nxt)
                    j += 1
                    continue
                if _indent(nxt) > base:
                    body_lines.append(nxt)
                    if nxt.count('"""') % 2 == 1 or nxt.count("'''") % 2 == 1:
                        in_tq = True
                    j += 1
                    continue
                break
            body = "\n".join(body_lines)
            if name in fs.funcs:
                # inner classes may legally re-declare a func name; merge
                # conservatively so edges from BOTH bodies survive
                prev = fs.funcs[name]
                fs.funcs[name] = Func(
                    path=rel, name=name, line=prev.line,
                    body=prev.body + "\n" + body,
                )
            else:
                fs.funcs[name] = Func(
                    path=rel, name=name, line=i + 1, body=body,
                )
            i = j
            continue
        i += 1
    return fs


def parse_tscn(path: Path, rel: str) -> FileSym:
    text = nav._read_text(path)
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
