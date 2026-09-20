"""Per-language extractors: file suffix -> parser module registry.

graph.py resolves which parser handles a file via ``registry_for(suffix)``
instead of hard-coding extensions, so a new language plugs in without
touching graph.py. The shared data model (language-neutral FileSym/Func)
lives in extractors/model.py and is re-exported here.

Contract for an extractor module (full details: extractors/README.md):
- ``parse(path: Path, rel: str) -> FileSym`` — parse one file.
- ``ENTRY_RULES`` — sequence of callables ``(fs, ctx) -> iterable of entry
  func keys``, so each language defines its own entry points.
"""

from __future__ import annotations

from extractors import c
from extractors import cpp
from extractors import csharp
from extractors import gdscript
from extractors import go
from extractors import java
from extractors import js
from extractors import lua
from extractors import python
from extractors import rust
from extractors import php
from extractors import ts
# Re-export surface = consumed surface (#200): every name below has a
# consumer outside extractors/ (graph.py, clusters.py, nav.py, server.py,
# bake/*, tools/, the langsep registry pins). Everything else stays on its
# defining module — deep imports (tests) and registry attribute access
# reach it there.
from extractors.common import (  # noqa: F401  (re-export)
    FN_KEY_SEP,
    MENTION_TOKEN_RE,
    SIGNAL_PREFIX,
    TSCN_SUFFIX,
    VAR_PREFIX,
    fn_key,
)
from extractors.gdscript import (  # noqa: F401  (re-export)
    ASSET_SCENE_GLOB,
    FUNC_KEYWORD,
    SCENE_FILE_SUFFIXES,
    UNDERSCORE_SHIELD,
    WALK_EXTS,
    counts_dead_share,
    harvest_autoloads,
    is_scene_path,
    is_script_path,
    is_wiring_only,
    res_to_rel,
)
from extractors.model import add_class_ctx  # noqa: F401  (re-export)

# suffix (lowercase) -> extractor module exposing parse() + ENTRY_RULES
EXTENSIONS: dict[str, object] = {
    ".gd": gdscript,
    ".tscn": gdscript,
    ".py": python,
    ".pyi": python,
    ".h": cpp,
    ".hpp": cpp,
    ".cpp": cpp,
    ".cc": cpp,
    ".cxx": cpp,
    ".ts": ts,
    ".tsx": ts,
    ".mts": ts,
    ".cts": ts,
    ".js": js,
    ".jsx": js,
    ".mjs": js,
    ".cjs": js,
    ".rs": rust,
    ".go": go,
    ".java": java,
    ".c": c,
    ".cs": csharp,
    ".php": php,
    ".lua": lua,
}

# issue #240: language presets + the raw-text walk suffixes — language
# facts consumed blind by onboard.py (scaffold/presets) and server.py
# (degraded-boot guidance), so they live here beside the registry, not
# in the shared modules. Registered suffixes parse structurally; the
# rest ride graph.file_doc's raw fallback (embedded + searchable, fns 0)
# until an extractor lands for them.
RAW_TEXT_EXTS = (".json", ".md")
def _derive_presets() -> dict[str, tuple[str, ...]]:
    """The preset table derives from the registry — one spelling of the
    suffix truth (issue #362, reviewer finding 12). Each language's
    structural suffixes ARE its EXTENSIONS rows read in insertion order,
    so a new registry suffix flows into every preset walking that
    language; the hand-spelled tuples this replaces were a third
    spelling that could drift. Registry insertion order is load-bearing:
    presets keep the historical suffix order byte-equal (equivalence leg
    in tests/test_project_mode.py)."""
    def lang_exts(module) -> tuple[str, ...]:
        return tuple(s for s, m in EXTENSIONS.items() if m is module)

    presets: dict[str, tuple[str, ...]] = {
        # ts trees ship js sources (the .cjs asymmetry fix): the ts
        # preset walks both families' registry suffixes.
        "ts": lang_exts(ts) + lang_exts(js) + RAW_TEXT_EXTS,
        "js": lang_exts(js) + RAW_TEXT_EXTS,
        "python": lang_exts(python) + RAW_TEXT_EXTS,
        # cpp deliberately walks NO raw text — the only structural
        # preset without .json/.md (finding 12, decided + kept): a
        # C/C++ tree's docs ride the headers, not the raw fallback.
        # Revisit only with a consumer need.
        "cpp": lang_exts(cpp),
        # gdscript's walk suffixes ARE gdscript.WALK_EXTS (issue #295:
        # single truth — the module constant is the one spelling; nav's
        # Godot-profile default and this preset must never drift apart)
        "gdscript": gdscript.WALK_EXTS,
        "rust": lang_exts(rust) + RAW_TEXT_EXTS,
        "go": lang_exts(go) + RAW_TEXT_EXTS,
        "java": lang_exts(java) + RAW_TEXT_EXTS,
        # c stays curated (finding 12): a C tree's .h headers walk WITH
        # .c even though the registry routes .h/.hpp to cpp's parser
        # (c.py's C_HEADER_EXTS) — and .hpp stays out (C++ headers are
        # the cpp preset's). The one hand-written row left, by design:
        # pairing facts the registry cannot spell.
        "c": (".c", ".h") + RAW_TEXT_EXTS,
        "csharp": lang_exts(csharp) + RAW_TEXT_EXTS,
        "php": lang_exts(php) + RAW_TEXT_EXTS,
        "lua": lang_exts(lua) + RAW_TEXT_EXTS,
    }
    # loud parity: preset keys are registry module names — a new
    # extractor without its preset row (or a preset key with no
    # extractor behind it) aborts naming the orphan, instead of leaving
    # the language half-supported: parseable via the registry yet
    # unreachable by onboard --preset / the boot guidance string.
    langs = {m.__name__.rsplit(".", 1)[-1] for m in EXTENSIONS.values()}
    assert langs == set(presets), (
        "registry/PRESETS drift: registry-only=%s presets-only=%s — "
        "every registry module needs exactly one preset key of its own "
        "name" % (sorted(langs - set(presets)), sorted(set(presets) - langs)))
    return presets


PRESETS: dict[str, tuple[str, ...]] = _derive_presets()

def sync_parseable(suffix: str) -> bool:
    """True when sync_functions may parse the suffix into funcs: an
    extractor exists AND the file is not wiring-only scene data."""
    return EXTENSIONS.get(suffix.lower()) is not None and suffix not in SCENE_FILE_SUFFIXES


def registry_for(suffix: str):
    """Extractor module handling this file suffix, or None."""
    return EXTENSIONS.get(suffix.lower())


# ---- build choreography (langsep) ----------------------------------------------
# Ordered build/wire steps composed from per-language hooks; graph.build()
# runs them blind — adding a language means a new extractor module +
# registry entry, zero graph.py diff. Steps loop ctx.files themselves and
# guard their own suffixes; ctx is the Graph (files/class_map/referenced/
# referenced_names/roots/_dyn_files/_subclasses/_edge/_emit_call/...).
def _facts_sweep(ctx) -> None:
    for fs in ctx.files.values():
        mod = registry_for(fs.ext)
        if mod is not None:
            mod.harvest_facts(fs, ctx)


BUILD_SEQUENCE = (
    gdscript.harvest_scene_wiring,
    gdscript.build_inheritance,
    _facts_sweep,
    python.rebind_reexports_sweep,
    python.import_liveness_sweep,
    python.arg_refs_sweep,
    ts.rebind_reexports_sweep,
    ts.import_liveness_sweep,
    js.rebind_reexports_sweep,
    js.import_liveness_sweep,
    rust.rebind_reexports_sweep,
    rust.import_liveness_sweep,
    java.import_liveness_sweep,
    go.interface_satisfaction_sweep,
    go.import_liveness_sweep,
    c.pair_headers,
    c.import_liveness_sweep,
    php.import_liveness_sweep,
    lua.required_sweep,
    lua.index_sweep,
    lua.import_liveness_sweep,
)

WIRE_SEQUENCE = (
    gdscript.wire_tscn,
    cpp.wire,
)


# registry choreography bind (langsep) — see extractors/gdscript.py's
# _PASS_* block for the rationale.
_PASS_FACTS = _facts_sweep
