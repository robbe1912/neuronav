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

from extractors import cpp
from extractors import gdscript
from extractors import python
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
    ".h": cpp,
    ".hpp": cpp,
    ".cpp": cpp,
    ".cc": cpp,
    ".cxx": cpp,
}

# issue #240: language presets + the raw-text walk suffixes — language
# facts consumed blind by onboard.py (scaffold/presets) and server.py
# (degraded-boot guidance), so they live here beside the registry, not
# in the shared modules. Registered suffixes parse structurally; the
# rest ride graph.file_doc's raw fallback (embedded + searchable, fns 0)
# until an extractor lands for them (a TS extractor is the tracked
# follow-up).
RAW_TEXT_EXTS = (
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".mts", ".cts", ".json", ".md",
)
PRESETS: dict[str, tuple[str, ...]] = {
    "ts": RAW_TEXT_EXTS,
    "js": (".js", ".jsx", ".mjs", ".cjs", ".json", ".md"),
    "python": (".py", ".pyi", ".json", ".md"),
    "cpp": (".h", ".hpp", ".cpp", ".cc", ".cxx"),
    "gdscript": (".gd", ".tscn"),
}


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
)

WIRE_SEQUENCE = (
    gdscript.wire_tscn,
    cpp.wire,
)


# registry choreography bind (langsep) — see extractors/gdscript.py's
# _PASS_* block for the rationale.
_PASS_FACTS = _facts_sweep
