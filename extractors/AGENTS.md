# AGENTS.md — extractors/

Per-language parsers behind a suffix registry. `graph.py` stays
language-neutral — it never dispatches on extensions; it asks the registry.
Full field contract: `extractors/README.md`.

## Layout

| module | role |
|---|---|
| `__init__.py` | registry: `EXTENSIONS` maps suffix -> module (`.gd`/`.tscn` -> `gdscript`, `.py` -> `python`, `.c` -> `c`, `.h`/`.hpp`/`.cpp`/`.cc`/`.cxx` -> `cpp`, `.ts`/`.tsx`/`.mts`/`.cts` -> `ts`, `.js`/`.jsx`/`.mjs`/`.cjs` -> `js`, `.rs` -> `rust`, `.go` -> `go`, `.java` -> `java`, `.cs` -> `csharp`, `.php` -> `php`, `.lua` -> `lua`); `registry_for(suffix)` returns module or None; `RAW_TEXT_EXTS` = the issue-#240 web set with no structural extractor (`.json .md`) — walked/indexed as raw `file_doc` when configured (TS went structural in #245, JS in #277); `PRESETS` = the `onboard.py init --preset <key>` extension lists — the 12 keys ts/js/python/cpp/c/gdscript/rust/go/java/csharp/php/lua (curated; the ts preset carries `.cjs` too since #277) |
| `model.py` | language-neutral dataclasses `FileSym` / `Func` — the parse output contract |
| `common.py` | shared leaf: text mechanics (`fn_key`, `entry_keys`, regexes) +, since #302, the tree-sitter front-end shells (`node_text`/`node_line`/`line_starts_of`/`ident_child`/`last_ident`/`body_block`, `rel_of_target`, the `receiver_env` scan prologue, and the `make_import_liveness_sweep` factory) — language knobs stay data at the call site (ident types, block child name, `$`-identifiers) |
| `gdscript.py` | `.gd` + `.tscn` parser, entry-point rules, IO surface scan |
| `python.py` | `.py` parser, entry-point rules, import/member facts; fn bodies sliced by AST spans (column-0 string lines no longer truncate them) |
| `c.py` | `.c` parser: tree-sitter-cpp front-end (C is a grammar subset — no new wheel) with a C-shaped model: `main()` entry, quoted includes only (angle-bracket system headers invisible by grammar), stem-only `.c`↔`.h` pairing that reads cpp-produced header FileSyms, decl-header calls resolving to the paired impl's definition, TU-scope callback-table liveness (bare fn names in brace initializers + `&fn`) |
| `php.py` | `.php` parser: tree-sitter-php front-end (binding `language_php()` — the HTML-embedding grammar, so mixed-markup `index.php` parses whole); PSR-4 namespace-path `use` resolution (java #335 shape), `use function` exact binding, external misses record nothing (loud degrade), `index.php`/`artisan`/composer-`bin` convention entries + PHPUnit attribute/`@test` hints, `$this->m` own-file-then-trait dispatch, typed-local + assignment-new receiver typing, `new` ctor edges, magic-method liveness exemption (`PHP_VIRTUALS`) |
| `cpp.py` | `.h`/`.hpp`/`.cpp`/`.cc`/`.cxx` parser: tree-sitter-cpp front-end + stdlib macro-surface pass (ClassDB/GDVIRTUAL registration harvest, ADD_SIGNAL/ADD_PROPERTY, emit_signal, memnew) |
| `ts.py` | `.ts`/`.tsx`/`.mts`/`.cts` parser: tree-sitter-typescript front-end with the ts/tsx grammar split (`.tsx` adds JSX patterns; byte-offset line numbers — never `start_point`, py-tree-sitter #472); overloads collapse to one Func at the first declaration, barrel re-exports rebind to origin definers, tsconfig `paths` aliases (JSONC-tolerant, one `extends` level) resolve only inside this module |
| `js.py` | `.js`/`.jsx`/`.mjs`/`.cjs` parser: tree-sitter-javascript front-end (the pack's binding is `language()` — there is no `language_javascript()`; `.jsx` parses under the tsx grammar from tree-sitter-typescript; byte-offset line numbers — never `start_point`, py-tree-sitter #472); CJS `require` bindings + `module.exports`/`exports.foo` beside ESM with interop defaults (`module.exports = <anon>` mints Func "default"), ESM + CJS barrels rebind to origin definers (wiring-only), jsconfig/tsconfig `paths` aliases (shared ES-family loader in ts.py), exported PascalCase components + `createRoot`/`ReactDOM.render` JSX targets + stories root the dead tiers, mixed .ts+.js specifiers resolve both directions |
| `rust.py` | `.rs` parser: tree-sitter-rust front-end (byte-offset line numbers — never `start_point`, py-tree-sitter #472); `fn main`/`#[test]`/`#[tokio::test]` entry rules (a bare `#[cfg(test)]` gate marks nothing), lib.rs pub-fn + `pub mod` closure = exported-API roots, `pub use` re-exports rebind to origin definers (barrel analogue), impl/trait methods collapse to the first declaration, trait default-method dispatch, `mod` namespaces, macros recorded as call-sites (name_literals) never expanded |
| `go.py` | `.go` parser: tree-sitter-go front-end (issue #334); `go.mod` module-prefix imports resolve to repo package dirs (package-level `imported_modules` + a per-alias `module:` binding feed the go liveness sweep — an imported dir's exported non-test funcs stay alive), a missing go.mod degrades loudly, `func main` in `package main` + `func init()` roots, `_test.go` Test/Benchmark/Fuzz/Example entry hints, `String()`/`Error()` std-interface shields (the rust law) |
| `java.py` | `.java` parser: tree-sitter-java front-end (issue #335); package-path import resolution (static-member + wildcard; JDK/jar externals miss loudly — name-level liveness at most, never a silent wrong-file edge), `main(String[])` + JUnit `@Test`/`@ParameterizedTest` entry rules (JUnit4 + JUnit5 path segments), interface default-method dispatch with `@Override` review shielding, qualified `new` ctor edges + default-ctor name-level alive |
| `csharp.py` | `.cs` parser: tree-sitter-c-sharp front-end (issue #336); namespace-to-path resolution (v1: no .sln/.csproj awareness — `using X.Y` keeps the namespace's files alive as a unit; externals resolve outside the repo), partial-class declaration merge (first declaration wins the line — the overload law), Unity MonoBehaviour lifecycle entry roots (Awake/Start/Update mirroring the Godot `_ready` rooting) + `[Test]`/`[TestMethod]`/`[Fact]` hints + the static-`Main` rule, namespace-qualified call resolution via type index + namespace-prefix walk |
| `lua.py` | `.lua` parser: tree-sitter-lua front-end (org wheel `tree-sitter-grammars`, `core` extra NOT installed — the front-end pattern); `require()` dotted-path resolution against the repo root incl. the Neovim `lua/` layout + directory `init.lua` modules, unresolved literal requires degrade loudly (one stderr note per process), `local m = require(...)` bindings in `module_vars` (whole-module liveness, js law), dynamic requires ride the mention floor, `init.lua`/`main.lua` never-required convention entries (Neovim/LÖVE) + `_spec`/`_test`/test-dir entry rules, `T.m` static dispatch (require-bound / own-file `local T = {}` / global-table corpus-wide name-based), `t:m` colon form → mention floor + same-file edge, metatable `__index = Base` keeps the base table's methods alive (go iface floor) |

## The contract

`parse(path, rel) -> FileSym` for one file. Pure: no chroma, no network, no
filesystem beyond the file being parsed. Each module also exports
`ENTRY_RULES`: a list of callables `(fs, ctx) -> iterable of entry func keys`,
where `ctx` is the `Graph` under construction (exposes `.autoloads`,
`.tres_scripts`, `.class_map`). `graph._find_roots()` unions the rules of
every registered module — dead-code reachability starts there.

`Func`: path, name, line (1-based), body, `params [(name, type)]`, ret,
`writes` (self.x=), `mut_params` (p.mutator(...)), key `"path::name"`.
`FileSym` carries the cross-language facts `graph.py` consumes: funcs,
signals, `attached_script` (first script of a .tscn — viz reads it),
`scripts` (all ext_resources), instances, connections, members (gates `var`
edges), consts (name -> repo relpath), name_literals, init_calls,
entry_hints (@rpc etc), imported_modules, from_imports; cpp fills
`globals` (file-scope statics) and `private_members` (access-region
members — stronger dead candidates), cpp/ts/rust fill `aliases`
(typedef/using, ts `type`, rust `type`).

## Dead-code exemptions (review-vs-likely tiers live in graph.py, fed from here)

`graph.dead_code()` marks an unreachable func `"review"` when its file uses
dynamic dispatch (`call()`/`Callable()`/`connect()`), else `"likely"`;
`"likely"` upgrades to `"review"` when the class extends a base unresolvable
in `class_map` and the name is not in the engine-virtual set. The exemption
sets ship with the language module:

- `gdscript.VIRTUALS` — engine-dispatched virtuals (`_ready`, `_process`,
  `_input`, `_draw`, ...): always entry roots.
- `gdscript.GUT_ROOTS` — `before_all`/`after_all`/`before_each`/`after_each`
  test harness hooks.
- `gdscript.ADDON_VIRTUALS` — C++ addon base classes (btaction, btcondition,
  btdecorator, btcomposite, bttask) dispatch `_enter`/`_exit`/`_tick`/
  `_setup`/`_generate_name`; bases live in `.gdext` bins, unresolvable in a
  source graph.
- `gdscript.MANUAL_BASES` — `editorscript`/`editorplugin`/`scenetree`: run
  from editor/tooling, whole file is an entry.
- `gdscript.ENGINE_VIRTUALS` — native virtuals dispatched by engine bases
  (multiplayerpeer extension packet surface).
- `python.PY_HOOKS` — stdlib/framework dispatch hooks (`end_headers`,
  `log_message`, `send_head`, `translate_path`, ...) invoked reflectively by
  serving machinery whose base resolves outside the repo: no static caller
  exists. Dead-scan classifies them as `review`, mirroring the .gd VIRTUALS
  rule — they are overrides, not orphans. Kept minimal: only names the
  serving machinery itself calls; extend only with evidence the framework
  really dispatches the name.
- `python.PY_VIRTUALS` — dunder dispatch (`__init__`, `__enter__`,
  `__getitem__`, ...).
- `cpp.CPP_VIRTUALS` — engine-dispatched virtuals on engine bases; the
  registration surface (`ClassDB::bind_method` / `bind_static_method` /
  `bind_vararg_method`, `GDVIRTUAL` macro declarations) yields roots.
  Files using the dynamic surface (`cpp.CPP_DYNAMIC_RE`: ClassDB,
  GDVIRTUAL, ADD_SIGNAL, ADD_PROPERTY, emit_signal) classify their
  unreachable funcs `review`, mirroring the .gd dispatch rule.
- Mention-count corroboration (issue #20): a `likely`-dead C++ name
  still mentioned elsewhere in the corpus at least
  `cpp.CPP_MENTION_FLOOR` (2) times stays `review` — names cited via
  strings/macros are not deletion fodder.
- `rust.RUST_STD_TRAIT_METHODS` — std-trait impl methods (`fmt`, `drop`,
  `hash`, `next`, operator impls, ...): operator/format/loop-dispatched
  with no textual call site, like the .gd VIRTUALS rule.
- Rust macros are never expanded: a fn referenced only from a
  `macro_rules!` template stays dead, but the template text feeds the
  mention floor, so it lands in `review` — deletion fodder it is not.

`test_selfindex.py` pins the meta-invariant: on this repo, `likely`-dead is
zero and server.py's MCP handlers stay in `review`.

## Adding a language

1. `extractors/<lang>.py` with `parse(path, rel) -> FileSym` + `ENTRY_RULES`
   (follow `python.py` for a stdlib-AST parser, `cpp.py` for a
   tree-sitter front-end + regex macro pass; `gdscript.py` for the
   scene-format variant).
2. Register suffixes in `EXTENSIONS` (`__init__.py`) — and re-export any
   language facts `graph.py` consumes (VIRTUALS-style sets, scan helpers)
   from the package too: `graph.py` imports the registry only, never an
   extractor submodule (extractors stay free of graph imports; acyclic).
3. Add the suffixes to the config `extensions` list (`navconfig.EXTS` gates the
   walk).
4. If the call syntax differs, extend `graph._scan_body` behind a per-format
   check keyed on `fs.ext` (see `_scan_body_py`).
5. Fixtures under `tests/fixtures/<lang>/` + a hardening suite (pattern:
   `test_pyhard.py`), and extend `test_crosslang.py` for the self-index
   integration.
6. Rebuild and compare edges/dead before/after — the regression floors in
   `test_target_regression` must not drift.
