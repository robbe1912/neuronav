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
from extractors.common import PY_CONTROL_KEYWORDS  # noqa: F401  (re-export)
from extractors.cpp import (  # noqa: F401  (re-export)
    CPP_DYNAMIC_RE,
    CPP_EXTS,
    CPP_MENTION_FLOOR,
    harvest_registration,
    scan_calls,
)
from extractors.gdscript import (  # noqa: F401  (re-export)
    ADDON_VIRTUALS,
    GUT_ROOTS,
    MANUAL_BASES,
    VIRTUALS,
    parse_gd,
    parse_tscn,
)
from extractors.model import FileSym, Func  # noqa: F401  (re-export)
from extractors.python import PY_HOOKS  # noqa: F401  (re-export)

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


def registry_for(suffix: str):
    """Extractor module handling this file suffix, or None."""
    return EXTENSIONS.get(suffix.lower())
