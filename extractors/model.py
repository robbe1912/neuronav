"""Language-neutral data model shared by extractors and the graph.

FileSym field names are deliberately generic (funcs / signals / members /
consts / instances / connections) so a second language can reuse or
duck-type them without graph.py learning Godot vocabulary. Language facts
(virtual names, test prefixes, tool bases) live in the per-language
extractor module, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable, Iterator


@dataclass
class Func:
    path: str
    name: str
    line: int  # 1-based def line
    body: str
    # declared IO surface (params/ret from the signature; writes/mut_params
    # from a body scan). Language-dependent fill; empty for languages that
    # don't parse them yet. powers fn-panel signature display + mutator filter
    params: list = field(default_factory=list)      # [(name, type)]
    ret: str = ""                                   # declared return type
    writes: set = field(default_factory=set)        # members assigned (self.x =)
    mut_params: set = field(default_factory=set)    # params mutated via p.mutator(
    # cAST-style size-aware doc chunking (issue #76). Extractors keep the
    # granular single-fn entries; the graph fn-doc builder re-uses these to
    # paint class context or split monsters. kind: "raw" (parser slice),
    # "class_ctx" (micro-fn carries its class's other methods),
    # "chunk" (one statement block of a monster). members collects the
    # merged-in sibling bodies for class_ctx chunks.
    kind: str = "raw"
    members: list = field(default_factory=list)     # [(name, line, body)]

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
    # bare calls inside class-level var initializer expressions — they run
    # at instantiation, so their targets must stay alive
    init_calls: set[str] = field(default_factory=set)
    # parse-declared entry funcs: decorators like @rpc mark network entry
    # points - extractor fills names, an entry rule yields them as roots
    entry_hints: set[str] = field(default_factory=set)
    # python import graph facts: plain `import x` keeps the module's funcs
    # alive as a unit (conservative); `from x import y` binds only y, so
    # just that name (plus the receiver const) survives
    imported_modules: set[str] = field(default_factory=set)
    from_imports: set[tuple[str, str]] = field(default_factory=set)
    # c++ file-scope variables (name -> type): unused statics are honest
    # dead-code material; visibility precedes any tier-pass consumption
    globals: dict[str, str] = field(default_factory=dict)
    # c++ typedef / using-alias declarations (name -> target type text)
    aliases: dict[str, str] = field(default_factory=dict)
    # c++ members declared under a private access region (stronger dead
    # candidates than public-unused once a tier pass consumes this)
    private_members: set[str] = field(default_factory=set)


# -- cAST-style doc chunking helpers (issue #76) -----------------------------


def add_class_ctx(
    funcs: dict[str, Func],
    name: str,
    members: Iterable[Func],
    siblings: Iterable[str] | None = None,
) -> None:
    """Overlay the class document on a micro-fn's Func (pure: the caller
    owns persistence).

    ``members`` fold in class context WITHOUT losing the fn's own identity —
    the doc hard-splits on the owned signature line, and every fold carries
    a ``-- name:line --`` banner (the same convention the fn index already
    uses), so agents can still find the exact fn inside the merged doc.
    Discards classes with no usable member (caller decides). Does NOT
    re-wrap the fn signature (docstring/param-length rules live with the
    graph fn-doc builder, keep the candidate surface honest)."""
    folded = members if siblings is None else [
        m for m in members if m.name not in siblings
    ]
    if not folded:
        return
    funcs[name] = replace(
        funcs[name], kind="class_ctx", members=[(m.name, m.line, m.body) for m in folded]
    )
