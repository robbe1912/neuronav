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
from extractors.model import FileSym, Func  # noqa: F401  (re-export)

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
