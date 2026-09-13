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
from extractors.common import (  # noqa: F401  (re-export)
    BARE_CALL_RE,
    DYNAMIC_HINT_RE,
    FN_KEY_SEP,
    fold_continuations,
    fn_key,
    MENTION_TOKEN_RE,
    MEMBER_ACCESS_RE,
    PY_CONTROL_KEYWORDS,
    QUALIFIED_CALL_RE,
    SIGNAL_PREFIX,
    TSCN_SUFFIX,
    VAR_PREFIX,
)
from extractors.cpp import (  # noqa: F401  (re-export)
    CPP_DYNAMIC_RE,
    CPP_EXTS,
    CPP_MENTION_FLOOR,
    harvest_registration,
    is_implicit_entry,
    scan_calls,
)
from extractors.gdscript import (  # noqa: F401  (re-export)
    ADDON_VIRTUALS,
    ASSET_SCENE_GLOB,
    ASSIGN_RHS_RE,
    ASSIGN_RHS_SKIP,
    AUTOLOAD_RE,
    BARE_DISPATCH_STR_RE,
    BARE_HANDLER_RE,
    CALLABLE_TWO_RE,
    AS_CAST_CALL_RE,
    ASSIGN_RHS_RE,
    ASSIGN_RHS_SKIP,
    CALLABLE_TWO_RE,
    AS_CAST_CALL_RE,
    CHAIN_CALL_RE,
    CHAIN_VAR_RE,
    CONNECT_METHOD_RE,
    CONNECT_RE,
    counts_dead_share,
    DISPATCH_STR_RE,
    DYNAMIC_METHODS,
    EMIT_RE,
    FUNC_KEYWORD,
    GUT_ROOTS,
    is_wiring_only,
    MANUAL_BASES,
    NON_CALLS,
    PARAM_TYPED_RE,
    PATH_EXTENDS_RE,
    QUOTED_IDENT_RE,
    RES_LOAD_RE,
    SCENE_WIRING_SUFFIXES,
    STRING_NAME_RE,
    STRINGNAME_LIT_RE,
    TRES_SCRIPT_RE,
    TRES_STRINGNAME_RE,
    TWEEN_ARG_RE,
    UNDERSCORE_SHIELD,
    VIRTUALS,
    WALK_EXTS,
    WIRING_ONLY_SUFFIXES,
    parse_gd,
    parse_tscn,
    res_to_rel,
)
from extractors.model import FileSym, Func, add_class_ctx  # noqa: F401  (re-export)
from extractors.python import (  # noqa: F401  (re-export)
    PY_ANNOT_ASSIGN_RE,
    PY_ATTR_CALL_RE,
    PY_BARE_CALL_RE,
    PY_CHAIN_CALL_RE,
    PY_HOOKS,
    PY_LOCAL_NEW_RE,
    PY_MODULE_ASSIGN_RE,
    PY_NON_CALLS,
    PY_PARAM_TYPED_RE,
    PY_RESULT_CALL_RE,
    PY_SUBSCRIPT_CALL_RE,
    PY_WITH_AS_RE,
)

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


def sync_parseable(suffix: str) -> bool:
    """True when sync_functions may parse the suffix into funcs: an
    extractor exists AND the file is not wiring-only scene data."""
    return EXTENSIONS.get(suffix.lower()) is not None and suffix not in WIRING_ONLY_SUFFIXES


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
