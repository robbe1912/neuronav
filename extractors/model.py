"""Language-neutral data model shared by extractors and the graph.

FileSym field names are deliberately generic (funcs / signals / members /
consts / instances / connections) so a second language can reuse or
duck-type them without graph.py learning Godot vocabulary. Language facts
(virtual names, test prefixes, tool bases) live in the per-language
extractor module, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Func:
    path: str
    name: str
    line: int  # 1-based def line
    body: str

    @property
    def key(self) -> str:
        return f"{self.path}::{self.name}"


@dataclass
class FileSym:
    path: str
    ext: str
    class_name: str = ""
    extends: str = ""
    is_tool: bool = False
    funcs: dict[str, Func] = field(default_factory=dict)
    signals: set[str] = field(default_factory=set)
    # scene-like formats only (.tscn)
    attached_script: str = ""  # first attached script (viz.py reads this)
    scripts: list[str] = field(default_factory=list)  # ALL script ext_resources
    instances: list[str] = field(default_factory=list)  # instanced scene paths
    connections: list[tuple[str, str]] = field(default_factory=list)  # (signal, method)
    # member declarations: name -> TypeName (gates 'var' edges)
    members: dict[str, str] = field(default_factory=dict)
    # const receivers: name -> resource rel path (const X = preload(...))
    consts: dict[str, str] = field(default_factory=dict)
    # file-level identifier-shaped name literals (e.g. StringName defaults
    # like `@export var x: StringName = &"method"`) that may dispatch
    # dynamically — harvested into the graph's referenced-name set
    name_literals: set[str] = field(default_factory=set)
    # parse-declared entry funcs: decorators like @rpc mark network entry
    # points — extractor fills names, an entry rule yields them as roots
    entry_hints: set[str] = field(default_factory=set)
