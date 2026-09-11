# Extractors

Per-language source extractors behind the structural graph. `graph.py` core
stays language-neutral: it never dispatches on file extensions itself and
owns no language facts. It asks this package instead:

```python
from extractors import registry_for

extractor = registry_for(path.suffix)   # module or None
if extractor:
    fs = extractor.parse(path, rel)     # -> FileSym-like
```

The shared data model (`FileSym`/`Func`, generic field names) lives in
`extractors/model.py`. Language-specific detail (GDScript parsing, engine
virtuals, test prefixes, tool bases) lives only in the per-language module.

## Current languages

| suffixes | module | exports |
|----------|--------|---------|
| `.gd`, `.tscn` | `gdscript.py` | `parse_gd`, `parse_tscn`, `parse`, `ENTRY_RULES` |
| `.py` | `python.py` | `parse`, `ENTRY_RULES` (dunder virtuals, `test_*`, module-level/`__main__`/fixture entry hints, `@property`/`@name.setter` accessors; consts = repo-module imports; graph side: `_scan_body_py` + import refs) |
| `.h`, `.hpp`, `.cpp`, `.cc`, `.cxx` | `cpp.py` | `parse`, `ENTRY_RULES` (tree-sitter-cpp front-end + stdlib macro-surface pass: `ClassDB::`/`GDVIRTUAL` registration binds, `ADD_SIGNAL`/`ADD_PROPERTY`, `emit_signal`, `memnew`; `CPP_VIRTUALS` + registration roots; mention-count floor `CPP_MENTION_FLOOR` feeds the dead tier) |

## Interface contract

An extractor module must expose:

- **`parse(path: Path, rel: str) -> FileSym`**
  One function, both args required. `path` is the absolute file path,
  `rel` the root-relative posix id used as the graph key. Returns a
  `FileSym`-like object (subclass `extractors.model.FileSym` or
  duck-type it) with at least:

  | field | meaning |
  |---|---|
  | `path`, `ext` | rel id and suffix |
  | `funcs: dict[name -> Func]` | `Func(path, name, line, body)`; `body` is the source text, used for edge scanning and duplicates |
  | `signals: set[str]` | declared signal/event names |
  | `members: dict[name -> TypeName]` | typed member vars — gates `'var'` edges |
  | `consts: dict[name -> relpath]` | `const X = preload(...)` style receivers |
   | `name_literals: set[name]` | file-level StringName defaults (`&"m"`) and exported `*method*` var defaults (`"m"`) that may dispatch dynamically |
   | `init_calls: set[name]` | bare calls inside class-level var initializer expressions (run at instantiation) |
   | `entry_hints: set[name]` | parse-declared entry funcs (`@rpc` decorators, inline `set(v):`/`get():` property-accessor blocks) — yielded as roots by an entry rule |
   | `imported_modules: set[str]` | python `import x` — keeps the module's funcs alive as a unit (conservative) |
   | `from_imports: set[(mod, name)]` | python `from x import y` — binds only y (plus the receiver const) |
   | `globals: dict[name -> type]` | C++ file-scope variables — unused statics are honest dead-code material |
   | `aliases: dict[name -> type text]` | C++ `typedef`/`using` declarations |
   | `private_members: set[name]` | C++ members declared under a private access region (stronger dead candidates) |

  Scene-like formats additionally fill `attached_script` (first script,
  kept for viz.py), `scripts` (ALL script ext_resources — handlers may live
  on any of them), `instances`, `connections` (see `model.FileSym`).

- **`ENTRY_RULES: sequence of callables (fs, ctx) -> iterable of entry func keys`**
  Each language defines its own entry points — funcs that are roots
  without callers in the source graph. `fs` is the parsed FileSym;
  `ctx` is the `Graph` under construction (exposes `.autoloads`,
  `.tres_scripts`, `.class_map`, ...). GDScript's rules: engine/test/addon
  virtuals, autoload singletons, tool/editor bases, `.tres`-referenced
   scripts, `@rpc` network entries, `_get_*`/`_set_*` accessors plus
   per-base native virtuals (`ENGINE_VIRTUALS`, e.g. the MultiplayerPeer
   extension surface) on engine-base scripts. `graph._find_roots()` iterates `registry_for(fs.ext).ENTRY_RULES`
  and unions the yielded keys into the root set.

## Adding a language (checklist)

1. Create `extractors/<lang>.py` implementing `parse(path, rel)` returning
   a FileSym-like (reuse `extractors.model.FileSym` or define compatible
   dataclasses). Define `ENTRY_RULES` for its entry points.
2. Register suffixes in `extractors/__init__.py::EXTENSIONS`.
3. Make sure the suffixes are indexed: `nav.EXTS` comes from the
    config's `"extensions"` list (default `[".gd", ".tscn"]`) — add them
    there (a second config like `config/neuronav.json` can index a different
    repo with different suffixes; `exclude_dirs` prunes e.g. `.venv`).
4. Body scanning (`graph._scan_body`) is call-syntax based today; if the
   language's call/emit syntax differs, extend it behind a per-format
   check keyed on `fs.ext` — parsing stays in the extractor, edge
   semantics stay in the graph.
5. Add an annotated fixture: `//-`-shaped goal comments (`@fn calls @tgt`,
   `signal x`, ...) in a synthetic source under `tests/fixtures/` —
   `tests/test_verifier.py` checks them against `parse()` output
   hermetically (no graph/config needed). Grammar: suite header.
6. Run: `python -X utf8 -c "import graph; g = graph.get_graph(rebuild=True); print(g.dead_code()['total'])"`
   and compare edges/dead against the previous build.
