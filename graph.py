"""neuronav structural layer: function/signal graph, dead code, duplicates.

Parses GDScript + .tscn from the checkout into an in-memory graph (language
parsers live in extractors/, dispatched via the suffix registry):
- functions with bodies (indentation-delimited)
- call edges: ClassName.func (via class_name map), bare same-file calls
- var edges: receiver.member where the destination file declares the member
- signal edges: emit sites -> connect() handlers, plus .tscn [connection] blocks
- entry roots: per-language entry-point rules ship with each extractor
  (extractors/*/ENTRY_RULES) — GDScript: autoloads, virtuals
  (_ready/_process/...), connected handlers, GUT test_*, string-callable
  references (call("x"), Callable(self, "x")), scripts referenced by
  load()/preload() string literals in bodies
- dead code = functions unreachable from roots (two confidence tiers)
- duplicates = normalized-body hashes + cosine-similar function vectors
- deep derived facts (dead tiers, duplicates, ranks, mentions) are
  cached at rescan into .neuronav/predicates.json (predicates.py) so
  queries read instead of walk (issue #71)

Zero non-vendor deps beyond nav (reuses its file walk + embed).

The doc-shaping subsystem (cAST chunk helpers, issue #76 + file-doc
shaping, issue #229) moved verbatim to docshape.py (issue #366 carve);
file_doc and the shaper names the suites pin are re-exported below —
generated docs stay byte-identical across the carve.
"""

from __future__ import annotations

import hashlib
import re
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path
import navconfig, navindex, navstore
import predicates
import recall
from extractors import (
    ASSET_SCENE_GLOB,
    BUILD_SEQUENCE,
    FN_KEY_SEP,
    FUNC_KEYWORD,
    MENTION_TOKEN_RE,
    SIGNAL_PREFIX,
    TSCN_SUFFIX,
    UNDERSCORE_SHIELD,
    sync_parseable,
    VAR_PREFIX,
    WIRE_SEQUENCE,
    harvest_autoloads,
    add_class_ctx,
    fn_key,
    registry_for,
    res_to_rel,
)
# single import surface: the build/wire choreography, the frozen fn-key
# grammar spellings, and per-language hooks are consumed through the
# package __init__ only — never a deep extractor submodule import — and
# extractor modules never import graph or nav (acyclic by construction).

from docshape import (  # noqa: E402  (#366 carve: the doc-shaping
    # subsystem lives in the leaf; this facade re-export keeps every
    # pinned name — navindex's file_doc, navstore's FILE_DOC_REV,
    # sync_functions' shaper calls, the tests' helper pins —
    # resolving unchanged)
    FILE_DOC_REV,
    MONSTER_FN_CHARS,
    _cast_scale,
    _chunk_docs,
    _chunk_line_offsets,
    _chunk_plan,
    _chunked_docs,
    _chunks,
    _fn_doc,
    _is_micro,
    file_doc,
)

# ---- constants ---------------------------------------------------------------
# Language-owned constants, entry-point rules and body-scan patterns live
# in extractors/ (see the import surface above). What remains here is
# language-neutral graph law: the fn-key helpers and dead-tier weights
# (the cAST doc-shaping knobs live in docshape.py, issue #366 carve).




def split_key(key: str) -> str:
    """File part of any node key (fn, scene, signal, member spellings):
    everything before the first separator. Bare file keys (cpp v1.1
    header-scope sources carry none) pass through whole."""
    return key.split(FN_KEY_SEP, 1)[0]

# dead-tier weights (viz J2 consumes): per-tier weight for dead-code
# candidates — "likely" 1.0, "review" 0.5 — and the dead-file share
# threshold: a file only flags dead when its dead weight reaches this
# share of its .gd func count.
DEAD_TIER_WEIGHTS = {"likely": 1.0, "review": 0.5}
DEAD_SHARE_THRESHOLD = 0.4

# impact walk knobs (issue #280): default and ceiling for the transitive
# closure depth. The walk itself is linear in edges, but the ANSWER is
# agent-facing token budget — past ~8 hops the depth histogram has said
# its falloff shape and the "+N past the cap" marker carries the rest
# honestly (#125 law).
IMPACT_DEFAULT_DEPTH = 4
IMPACT_MAX_DEPTH = 8



# ---- data model ----------------------------------------------------------------
# Func / FileSym live in extractors/model.py (language-neutral); re-exported
# here so graph.Func / graph.FileSym keep working for importers.


class Graph:
    """Whole-checkout structural graph. Build once per process (~seconds)."""

    # derived-predicate cache payload (issue #71): None = on-the-fly
    # paths. A CLASS default, not an __init__ assign: tests build bare
    # graphs via Graph.__new__ (no __init__ run) and every query must
    # still find the attribute; build() replaces it via predicates.bind.
    _pred: dict | None = None

    # liveness read-path attrs follow the same law (issue #294):
    # referenced / referenced_names / reachable / autoloads are WRITTEN
    # by __init__/build but READ by queries, so bare Graph.__new__
    # fixtures must find them too — dead_code() raised AttributeError
    # on self.referenced otherwise. The sets default to frozenset: an
    # accidental .add on a bare graph fails loudly instead of leaking
    # across instances (autoloads is only ever replaced wholesale by
    # build(), so a plain dict default is safe there).
    referenced: frozenset = frozenset()        # string-referenced (alive, not root)
    referenced_names: frozenset = frozenset()  # func names called via unresolvable receivers
    reachable: frozenset = frozenset()         # keys reachable from roots
    autoloads: dict = {}                       # autoload name -> script rel path

    def __init__(self) -> None:
        self.files: dict[str, FileSym] = {}
        self.class_map: dict[str, str] = {}  # class_name -> res:// path
        # edge[src_key] = set of dst_key; key = "path::func" or "path::SIGNAL:x"
        self.edges: dict[str, set[str]] = defaultdict(set)
        self.reverse: dict[str, set[str]] = defaultdict(set)
        self.roots: set[str] = set()
        self.referenced: set[str] = set()  # string-referenced (alive, not root)
        self.referenced_names: set[str] = set()  # func names called via unresolvable receivers
        self.edge_types: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.built_at_lines: int = 0
        self._mentions: Counter | None = None  # lazy corpus mention counts (issue #20)
        # hook accumulators (langsep): filled by the registry's build
        # sequence steps; declared here so the ctx protocol is total
        self._subclasses: dict[str, set[str]] = defaultdict(set)
        self._dyn_files: set[str] = set()
        self.tres_scripts: set[str] = set()
        self.cpp_gdvirtuals: set[str] = set()

    # -- build -------------------------------------------------------------------

    def build(self) -> "Graph":
        for path in navindex.iter_files():
            rel = navindex.file_id(path)
            extractor = registry_for(path.suffix)
            if extractor is None:
                continue
            try:
                fs = extractor.parse(path, rel)
            except OSError as e:
                # issue #117: the same race every later pass guards (the
                # .tres walk, stat_fingerprint) — a file vanishing between
                # iter_files and parse skips with a stderr note instead of
                # aborting the whole index build
                print(f"neuronav: parse skipped {rel}: {e}", file=sys.stderr)
                continue
            self.files[rel] = fs
            if fs.class_name:
                self.class_map[fs.class_name] = rel

        # autoload singletons are addressable by their project.godot name
        # autoload singletons via the registry harvest (one home shared
        # with clusters.py's inverse map); script-form only, and only
        # files the index actually carries
        amap = harvest_autoloads(navconfig.ROOT, scripts_only=True)
        self.autoloads = {n: r for n, r in amap.items() if r in self.files}
        for name, rel in self.autoloads.items():
            self.class_map.setdefault(name, rel)

        # asset scenes sit outside the search index but carry animation method
        # tracks + connections that fire script funcs — parse for wiring only
        for path in (navconfig.ROOT / "assets").rglob(ASSET_SCENE_GLOB) if (navconfig.ROOT / "assets").is_dir() else ():
            rel = navindex.file_id(path)
            if rel not in self.files:
                self.files[rel] = registry_for(path.suffix).parse(path, rel)

        # language choreography (langsep): every language-conditioned
        # pass — scene-wiring harvest, inheritance, name-literal facts,
        # python re-export/import passes — ships with its extractor; the
        # registry sequences them and graph runs them blind
        for step in BUILD_SEQUENCE:
            step(self)

        for rel, fs in self.files.items():
            extractor = registry_for(fs.ext)
            if extractor is not None:
                extractor.scan_file(fs, self)

        for step in WIRE_SEQUENCE:
            step(self)
        self._find_roots()
        self._reachable()
        # issue #71: rescan absorbs the deep derivations (dead tiers,
        # duplicates, ranks, mentions); queries then read
        self.predicates_hook()
        return self

    def predicates_hook(self) -> None:
        """Bind the derived-predicate cache (predicates.py). A thin
        indirection so tests can force the on-the-fly path."""
        predicates.bind(self)

    def _resolve_definer(self, target: str, nm: str, seen: frozenset[str] = frozenset()) -> str:
        """Rel path of the file that DEFINES `nm` imported from `target`:
        the target itself, or the submodule it re-exports the name from
        (a package __init__ surface). '' when unresolvable. Cycle-safe
        and deterministic (candidates sorted)."""
        if target not in self.files or target in seen:
            return ""
        if nm in self.files[target].funcs:
            return target
        tfs = self.files[target]
        seen = seen | {target}
        cands = sorted({m for m, n in tfs.from_imports if n == nm}
                       | ({tfs.consts[nm]} if nm in tfs.consts else set()))
        for cand in cands:
            got = self._resolve_definer(cand, nm, seen)
            if got:
                return got
        return ""

    def _ancestor_def(self, fs: FileSym, name: str) -> str:
        """Rel path of the nearest ancestor class declaring `name`, or ''."""
        base = fs.extends
        seen: set[str] = set()
        while base and base not in seen and base in self.class_map:
            seen.add(base)
            rel = self.class_map[base]
            if name in self.files[rel].funcs:
                return rel
            base = self.files[rel].extends
        return ""

    def _emit_call(self, src: str, dst: str, fname: str) -> None:
        """Call edge + virtual-dispatch completion: a call resolved to a
        base class may land on any subclass override — mirror the edge."""
        self._edge(src, fn_key(dst, fname))
        base = self.files[dst].class_name or dst
        for sub in self._subclasses.get(base, ()):
            if fname in self.files[sub].funcs:
                self._edge(src, fn_key(sub, fname))

    def _chain_dst(self, var_types: dict, head: str, mid: str) -> str:
        """Resolve head.mid to the class declaring that member, or ''."""
        cls = head if head in self.class_map else var_types.get(head)
        if not (cls and cls in self.class_map):
            return ""
        fs1 = self.files[self.class_map[cls]]
        cls2 = fs1.members.get(mid, "")
        if cls2 and cls2 in self.class_map:
            return self.class_map[cls2]
        return ""

    def _edge(self, src: str, dst: str, ty: str = "call") -> None:
        if src == dst:
            return
        self.edges[src].add(dst)
        self.reverse[dst].add(src)
        self.edge_types[(src, dst)].add(ty)

    # -- ctx protocol for extractor hooks (langsep) ------------------------------
    # extractor modules never import nav (registry stays acyclic): raw
    # file access for their build/wire/scan hooks routes through these
    # thin wrappers so the walk/read contracts (config excludes, #117
    # race guards) stay single-sourced in nav.
    def read_file(self, rel: str) -> str:
        return navindex._read_text(navconfig.ROOT / rel)

    def path_for(self, rel: str) -> Path:
        return navconfig.ROOT / rel

    def walk_root_files(self, suffixes):
        return navindex.iter_root_files(suffixes)

    def script_rels(self, fs: FileSym) -> list[str]:
        """Indexed scripts for a scene, in resolution order: ext_resource
        scripts first (file order), the attached script only when none of
        them is indexed. One authoritative cascade — viz's signal-wire
        channel resolves against the same list (map-spec-v2 §1/F13)."""
        rels = [
            s_rel
            for s in fs.scripts
            if (s_rel := res_to_rel(s)) and s_rel in self.files
        ]
        if not rels and fs.attached_script:
            s_rel = res_to_rel(fs.attached_script)
            if s_rel and s_rel in self.files:
                rels.append(s_rel)
        return rels

    def _find_roots(self) -> None:
        # entry-point rules are language-owned: each extractor module ships
        # ENTRY_RULES callables (fs, ctx) -> iterable of entry func keys
        for rel, fs in self.files.items():
            extractor = registry_for(fs.ext)
            if extractor is None:
                continue
            for rule in getattr(extractor, "ENTRY_RULES", ()):
                self.roots.update(rule(fs, ctx=self))

    def _reachable(self) -> None:
        # dynamically-invoked names (unresolvable receivers, strings) are
        # alive but have no static edge — root them so their callees survive
        if self.referenced_names:
            for rel, fs in self.files.items():
                if registry_for(fs.ext) is None:
                    continue
                for name, fn in fs.funcs.items():
                    if name in self.referenced_names:
                        self.roots.add(fn.key)
        seen: set[str] = set(self.roots) | self.referenced
        # referenced keys carry real out-edges (preloaded/dynamically
        # loaded files) — they must be traversed, not just marked alive
        queue = deque(self.roots | self.referenced)
        while queue:
            cur = queue.popleft()
            for nxt in self.edges.get(cur, ()):  # forward edges
                if nxt not in seen and not nxt.endswith(TSCN_SUFFIX):
                    seen.add(nxt)
                    queue.append(nxt)
        self.reachable = seen

    def _mention_counts(self) -> Counter:
        """Corpus-wide identifier mention counts (issue #20 tier rule).

        One tokenizing pass over every indexed file's raw text — comments
        and string literals included — cached for the graph's lifetime;
        callers never rescan per candidate. Counting is order-independent,
        so determinism is unaffected.
        """
        if self._mentions is None:
            cached = self._pred.get("mentions") if self._pred is not None else None
            if cached is not None:  # issue #71: rescan-time corpus pass
                self._mentions = Counter(cached)
                return self._mentions
            counts: Counter = Counter()
            for rel in sorted(self.files):
                try:
                    text = navindex._read_text(navconfig.ROOT / rel)
                except OSError:
                    continue
                counts.update(MENTION_TOKEN_RE.findall(text))
            self._mentions = counts
        return self._mentions

    # -- queries -------------------------------------------------------------------

    def dead_code(self, limit: int = 60) -> dict[str, object]:
        dead = self._dead_rows()
        by_tier = defaultdict(int)
        for d in dead:
            by_tier[d["tier"]] += 1
        return {
            "total": len(dead),
            "by_tier": dict(by_tier),
            "candidates": dead[:limit],
            "note": (
                "candidates only — verify before deleting. 'likely' = file has no "
                "dynamic dispatch; 'review' = file uses call()/Callable()/connect(), "
                "string-dispatch may hide callers."
            ),
        }

    def _dead_rows(self) -> list[dict[str, object]]:
        """Dead-candidate rows (tier assembly) — the shared code path
        behind dead_code and the rescan-time predicate cache
        (issue #71): what gets cached is by construction what the
        walk returns. Cached rows are copied defensively; callers
        may mutate their view without touching the cache."""
        if self._pred is not None:  # issue #71: rows derived at rescan
            return [{**d} for d in self._pred["dead"]]
        dead = []
        # wildcard handler refs (*::name, cross-file signal handlers) keep
        # same-named funcs alive; precompute the bare-name set once instead
        # of rescanning self.referenced per candidate fn (O(fns x referenced)
        # -> O(referenced), issue #43). Membership-only set: it never
        # iterates into an output path, so determinism is unchanged.
        wildcard_names = {
            r.split(FN_KEY_SEP)[-1] for r in self.referenced if r.startswith("*" + FN_KEY_SEP)
        }
        # mention-count corroboration (issue #20): one cached tokenizing
        # pass over raw corpus text, never a scan per candidate. Only C++
        # candidates consume it, so corpora without C++ files skip the pass.
        mentions = (
            self._mention_counts()
            if any(getattr(registry_for(f.ext), "MENTION_FLOOR", 0) for f in self.files.values())
            else {}
        )
        for rel, fs in self.files.items():
            mod = registry_for(fs.ext)
            if mod is None:
                continue
            joined = "\n".join(fs.funcs[f].body for f in fs.funcs) if fs.funcs else ""
            file_is_dynamic = bool(mod.DYNAMIC_HINT.search(joined))
            for name, fn in fs.funcs.items():
                if fn.key in self.reachable:
                    continue
                # wildcard handler refs (_on_x from any file) keep it alive
                if name in wildcard_names:
                    continue
                if name in self.referenced_names:
                    continue
                # cpp implicit-entry surfaces (issue #109): destructors,
                # overloaded operators and conversion operators are
                # invoked without any call site — destroyed temporaries,
                # implicit conversions, infix syntax — so liveness cannot
                # be disproven and they are never dead candidates. This
                # outranks the mention floor, which could never fire for
                # these names anyway: MENTION_TOKEN_RE tokenizes ~DtorOp
                # to 'DtorOp' and 'operator bool' to 'operator', so the
                # raw-name lookups miss by construction.
                if mod.is_entry_exempt(name):
                    continue
                tier = "review" if file_is_dynamic else "likely"
                # functions on classes extending bases we cannot resolve (engine
                # natives not in the shield, C++ addons) may be dispatched
                # natively; the VIRTUALS shield is corpus-wide (UNDERSCORE_SHIELD)
                # and language tails live behind unresolved_base_review
                if (
                    tier == "likely"
                    and fs.extends
                    and fs.extends not in self.class_map
                    and name not in UNDERSCORE_SHIELD
                    and mod.unresolved_base_review(name)
                ):
                    tier = "review"
                # python classes referenced at module scope (stand-ins
                # injected into foreign callers — duck-typed test stubs,
                # framework singletons) have their methods invoked through
                # an opaque consumer: the honest tier is review, mirroring
                # PY_HOOKS (#153 family)
                if tier == "likely" and mod.stand_in_review(fs, name):
                    tier = "review"
                # mention-count corroboration (issue #20): a cpp name that
                # keeps appearing across the corpus — unresolved same-name
                # call sites the ambiguity guard dropped, comments, string
                # dispatch tables — is wired somewhere the static pass
                # cannot see, so 'likely' overclaims its deadness. Names
                # mentioned only at their own definition stay 'likely'.
                if tier == "likely" and mod.mention_review(name, mentions):
                    tier = "review"
                dead.append({"path": rel, "func": name, "line": fn.line, "tier": tier})
        dead.sort(key=lambda d: (d["tier"], d["path"], d["line"]))
        return dead

    def symbol_graph(self, symbol: str, depth: int = 1, limit: int = 40) -> str:
        depth = max(1, min(depth, 3))
        keys = self._resolve(symbol)
        if not keys:
            return f"no function matching '{symbol}'"
        out_lines: list[str] = []
        seen_keys: set[str] = set()
        frontier = set(keys)
        for _ in range(depth):
            nxt: set[str] = set()
            for key in sorted(frontier):
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                callers = sorted(self.reverse.get(key, ()))
                callees = sorted(self.edges.get(key, ()))
                out_lines.append(self._fmt_node(key, callers, callees))
                nxt |= {c for c in callees + callers if not c.endswith(TSCN_SUFFIX)}
            frontier = nxt - seen_keys
            if not frontier:
                break
        return "\n".join(out_lines[:limit])

    def _resolve(self, symbol: str) -> list[str]:
        hits = []
        for rel, fs in self.files.items():
            if symbol in fs.funcs:
                hits.append(fn_key(rel, symbol))
            if fs.class_name == symbol:
                hits.extend(fn_key(rel, f) for f in fs.funcs)
        if not hits:
            for rel, fs in self.files.items():
                for name in fs.funcs:
                    if symbol.lower() in name.lower():
                        hits.append(fn_key(rel, name))
        return hits[:10]

    def _fmt_node(self, key: str, callers: list[str], callees: list[str]) -> str:
        def short(k: str) -> str:
            path, _, name = k.partition("::")
            return f"{path}#{name}"
        c_in = ", ".join(short(c) for c in callers[:8]) or "-"
        c_out = ", ".join(short(c) for c in callees[:8]) or "-"
        return f"{short(key)}\n    callers: {c_in}\n    callees: {c_out}"

    def impact(
        self, symbol: str, direction: str = "callers",
        max_depth: int = IMPACT_DEFAULT_DEPTH,
    ) -> dict[str, object] | None:
        """Transitive blast radius of a symbol (issue #280): everything
        that transitively CALLS it (direction="callers" — what breaks if
        it changes or disappears) or everything it transitively calls
        (direction="callees" — what it depends on), over the same
        fn-level edges symbol_graph walks one hop at a time.

        Cycle-safe deterministic BFS: visited set, sorted frontier,
        depth-bounded. Repo call chains contain cycles — layout.py
        carries the SCC machinery for the viz bake; the closure walk
        only needs to terminate and stay byte-stable, so it never
        re-enqueues. Scene pseudo-keys (*::tscn) are neither counted
        nor traversed: the answer is fn-level (v1 law — no file
        rollup).

        Returns the walk payload for server-side rendering (#125
        division — this method walks, it never formats): sorted seed
        keys, the clamped max_depth, total closure size, per-hop sorted
        key lists, ``beyond`` (nodes one hop past the depth cap — 0
        when the closure completed inside the budget, so a capped
        answer can say "+N more" honestly), and ``entries`` (closure
        members that are known entry points: chains anchored at a
        test/registration/export read differently than dead ends).
        None when the symbol misses resolution."""
        if direction not in ("callers", "callees"):
            raise ValueError(
                f"direction must be 'callers' or 'callees', got '{direction}'"
            )
        depth = max(1, min(max_depth, IMPACT_MAX_DEPTH))
        seeds = sorted(self._resolve(symbol))
        if not seeds:
            return None
        step = self.reverse if direction == "callers" else self.edges
        visited: set[str] = set(seeds)
        by_depth: list[list[str]] = []
        level: list[str] = seeds
        for _ in range(depth):
            nxt: set[str] = set()
            for key in level:
                nxt |= {
                    n for n in step.get(key, ())
                    if not n.endswith(TSCN_SUFFIX)
                }
            nxt -= visited
            if not nxt:
                break
            visited |= nxt
            by_depth.append(sorted(nxt))
            level = by_depth[-1]
        beyond = 0
        if len(by_depth) == depth:
            # the budget ran out, not the graph: count one more hop so
            # the cut can be announced instead of implied
            edge = set()
            for key in by_depth[-1]:
                edge |= {
                    n for n in step.get(key, ())
                    if not n.endswith(TSCN_SUFFIX)
                }
            beyond = len(edge - visited)
        entries = sorted(k for k in visited - set(seeds) if k in self.roots)
        return {
            "symbol": symbol,
            "seeds": seeds,
            "direction": direction,
            "max_depth": depth,
            "total": sum(len(hop) for hop in by_depth),
            "by_depth": by_depth,
            "beyond": beyond,
            "entries": entries,
        }

    # -- duplicates ---------------------------------------------------------------

    def exact_duplicates(self, limit: int = 30) -> list[dict[str, object]]:
        """Exact-duplicate function bodies (sha1 over the whitespace/
        `#`-comment-normalized body) across ALL indexed languages —
        issue #116: this was GDScript-only, so a Python or C++ repo got
        a false 'no exact duplicates found' clean bill. C++ `//`
        comments compare as body text (stripping at `//` would truncate
        res:// literals in .gd bodies) — conservative: it can miss a
        pair, never invent one. Pure-delegate groups (thin wrappers,
        issue #268) drop before anything is cached or served;
        duplicates_report carries the skip count."""
        groups, _skipped = self._dup_groups()
        return [
            {"hash": d["hash"], "members": list(d["members"])}
            for d in groups[:limit]
        ]

    def duplicates_report(self, limit: int = 30) -> dict[str, object]:
        """The full duplicates answer (issue #268): genuine groups plus
        the count of pure-delegate groups skipped — callers surface the
        skip, never hide it."""
        groups, skipped = self._dup_groups()
        return {
            "groups": [
                {"hash": d["hash"], "members": list(d["members"])}
                for d in groups[:limit]
            ],
            "groups_total": len(groups),
            "delegate_skipped": skipped,
        }

    def _dup_groups(self) -> tuple[list[dict[str, object]], int]:
        """(kept groups, skipped pure-delegate count) — the shared code
        path behind exact_duplicates/duplicates_report and the rescan-
        time predicate cache (issue #71). The delegate filter runs
        HERE, before the cache stores anything, so the cached list and
        every direct read are one truth — a stale-cache serve of an
        unfiltered list would be a bug. Dropped groups are pure-delegate
        wrappers only (#268): conservative like #116 — it can miss a
        wrapper, never drop real duplicated logic."""
        if self._pred is not None:  # issue #71: derived at rescan
            return self._pred["dups"], self._pred["dup_skips"]
        groups: dict[str, list[str]] = defaultdict(list)
        norms: dict[str, str] = {}
        mods: dict[str, object] = {}
        for rel, fs in self.files.items():
            mod = registry_for(fs.ext)
            for name, fn in fs.funcs.items():
                norm = _normalize_body(fn.body, mod)
                if len(norm.splitlines()) < 3:
                    continue  # trivial
                h = hashlib.sha1(norm.encode()).hexdigest()
                groups[h].append(fn.key)
                norms[h] = norm
                mods.setdefault(h, mod)  # group language = first member's
        dups: list[dict[str, object]] = []
        skipped = 0
        for h, v in groups.items():
            if len(v) < 2:
                continue
            if _pure_delegate(norms[h], mods[h]):
                skipped += 1  # issue #268: thin wrapper, not logic
                continue
            dups.append({"hash": h[:8], "members": sorted(v)})
        dups.sort(key=lambda d: -len(d["members"]))
        return dups, skipped

    # -- file importance: pagerank + budgeted repo map -------------------------

    def file_wires(self) -> dict[str, dict[str, int]]:
        """File-level adjacency folded from the symbol graph: file ->
        {file: wire count}. A wire is one distinct symbol-to-symbol edge
        (call/signal/var); self-wires drop, scene pseudo-keys (`rel::tscn`)
        fold onto their scene file. Sorted + deterministic — the single
        implementation shared by pagerank/repo_map and recall's 1-hop
        expansion. Served from the rescan-time predicate cache when
        present (issue #71), as a defensive copy."""
        if self._pred is not None:  # issue #71: folded at rescan
            return {fi: dict(row) for fi, row in self._pred["wires"].items()}
        out: dict[str, dict[str, int]] = {rel: {} for rel in self.files}
        for src in sorted(self.edges):
            sfi = src.partition("::")[0]
            row = out.setdefault(sfi, {})
            for dst in sorted(self.edges[src]):
                dfi = dst.partition("::")[0]
                if sfi != dfi:
                    row[dfi] = row.get(dfi, 0) + 1
        return out

    def pagerank(self, damping: float = 0.85, iters: int = 30) -> dict[str, float]:
        """PageRank over the file wire graph (edge weight = wire count).
        Deterministic by construction: uniform init, exactly `iters`
        power iterations (fixed cap, no epsilon early-exit), files visited
        in sorted index order; dangling files (no out-wires) spread their
        mass uniformly so ranks sum to ~1. Default parameters are served
        from the rescan-time predicate cache (issue #71); other
        parameterizations recompute as before."""
        if self._pred is not None and damping == 0.85 and iters == 30:
            return dict(self._pred["rank"])
        wires = self.file_wires()
        fis = sorted(wires)
        n = len(fis)
        if n == 0:
            return {}
        idx = {fi: i for i, fi in enumerate(fis)}
        out_w = [sum(wires[fi].values()) for fi in fis]
        # incoming wires as (src index, weight); built in sorted src order
        # so the float accumulation order — and thus every rank — is fixed
        incoming: list[list[tuple[int, int]]] = [[] for _ in fis]
        for i, fi in enumerate(fis):
            for dst, w in sorted(wires[fi].items()):
                incoming[idx[dst]].append((i, w))
        base = (1.0 - damping) / n
        rank = [1.0 / n] * n
        for _ in range(iters):
            dangling = sum(r for r, w in zip(rank, out_w) if w == 0)
            spread = damping * dangling / n
            nxt = [0.0] * n
            for i in range(n):
                s = base + spread
                for src_i, w in incoming[i]:
                    s += damping * w * rank[src_i] / out_w[src_i]
                nxt[i] = s
            rank = nxt
        return {fi: rank[i] for i, fi in enumerate(fis)}

    def _symbol_degrees(self) -> dict[str, dict[str, int]]:
        """Per-file func name -> wire degree (out + in symbol edges).
        SIGNAL:/VAR:/tscn pseudo-key names never match a func name, so
        they fold out naturally. Served from the rescan-time predicate
        cache when present (issue #71), as a defensive copy."""
        if self._pred is not None:  # issue #71: folded at rescan
            return {rel: dict(row) for rel, row in self._pred["deg"].items()}
        deg = {rel: dict.fromkeys(fs.funcs, 0) for rel, fs in self.files.items()}
        for src in sorted(self.edges):
            sp, _, sn = src.partition("::")
            row = deg.setdefault(sp, {})
            if sn in row:
                row[sn] += len(self.edges[src])
        for dst in sorted(self.reverse):
            dp, _, dn = dst.partition("::")
            row = deg.setdefault(dp, {})
            if dn in row:
                row[dn] += len(self.reverse[dst])
        return deg

    def repo_map(self, budget_tokens: int = 2048) -> str:
        """Aider-style token-budgeted repo map: PageRank-ordered files,
        tree-grouped by directory, each file capped to its top signatures
        by symbol wire degree (god files get a slice, not the kitchen
        sink — MAP_MAX_SIGS). Hard budget stop measured in _toks (1 token
        ~= 4 chars). Byte-stable: same graph -> identical string."""
        rank = self.pagerank()
        deg = self._symbol_degrees()
        files = sorted(self.files, key=lambda rel: (-rank.get(rel, 0.0), rel))
        lines: list[str] = []
        used = 0
        dir_stack: list[str] = []
        for rel in files:
            fs = self.files[rel]
            dparts = rel.split("/")[:-1]
            # tree headers: emit only the directory parts that changed
            keep = 0
            while (
                keep < len(dir_stack)
                and keep < len(dparts)
                and dir_stack[keep] == dparts[keep]
            ):
                keep += 1
            block = [f"{'  ' * i}{p}/" for i, p in enumerate(dparts[keep:], start=keep)]
            dir_stack = dparts
            block.append(f"{'  ' * len(dparts)}{rel.split('/')[-1]}:")
            names = sorted(
                fs.funcs, key=lambda nm: (-deg.get(rel, {}).get(nm, 0), nm)
            )[:MAP_MAX_SIGS]
            sigs = [f"class {fs.class_name}"] if fs.class_name else []
            sigs.extend(
                f"{nm}({', '.join(p for p, _t in fs.funcs[nm].params)})" for nm in names
            )
            if sigs:
                block.append(f"{'  ' * (len(dparts) + 1)}{', '.join(sigs)}")
            for ln in block:
                t = _toks(ln)
                if used + t > budget_tokens:
                    return "\n".join(lines)
                lines.append(ln)
                used += t
        return "\n".join(lines)


def _normalize_body(body: str, mod=None) -> str:
    """Uniform-indent, comment-stripped body text for dup grouping.
    Comment prefixes are language-owned (issue #295): each extractor
    declares COMMENT_PREFIXES; a language with none (or a bare ``mod=None``
    call) keeps its lines verbatim — conservative, never mangling."""
    prefixes = getattr(mod, "COMMENT_PREFIXES", ())
    out = []
    for line in body.splitlines():
        s = line
        for p in prefixes:
            s = s.split(p, 1)[0]
        s = s.rstrip()
        if not s.strip():
            continue
        out.append("  " + s.strip())  # unify indent
    return "\n".join(out)


def _pure_delegate(norm: str, mod=None) -> bool:
    """True for thin delegation wrappers (issue #268): dispatch is
    language-owned (issue #295). A language with a ``pure_delegate`` hook
    classifies its own bodies; a language with SIGNATURE_RE supplies the
    statement shapes and this packing algorithm runs them; anything else
    (no grammar declared) never classifies — conservative. The #116 law
    holds everywhere: it can miss a wrapper, never drop real duplicated
    logic. After an optional signature line, the body is only call-free
    guards (if/elif whose one-line body is a call-free return — an
    early-out — or a call-free assignment — arg normalization; at most
    two), at most one call-free assignment, and a single forwarding
    ``return call(args)``."""
    hook = getattr(mod, "pure_delegate", None)
    if hook is not None:
        return bool(hook(norm))
    sig_re = getattr(mod, "SIGNATURE_RE", None)
    if sig_re is None:
        return False  # language declares no delegate grammar
    # _normalize_body emits a uniform two-space indent; strip it so the
    # statement regexes match shape, not indentation
    lines = [ln.strip() for ln in norm.splitlines()]
    if lines and sig_re.match(lines[0]):
        lines = lines[1:]  # py bodies keep their signature line; gd strip it
    if not 2 <= len(lines) <= 5:
        return False
    i = 0
    while i + 1 < len(lines) and mod.GUARD_RE.match(lines[i]):
        if not (
            mod.GUARD_RET_RE.match(lines[i + 1]) or mod.ASSIGN_RE.match(lines[i + 1])
        ):
            break
        i += 2
    if i < len(lines) and mod.ASSIGN_RE.match(lines[i]):
        i += 1
    return i == len(lines) - 1 and mod.FORWARD_RE.match(lines[i]) is not None


# -- canonical edge-type taxonomy (issue #295) ----------------------------------
# Every emitter funnels through Graph._edge naming a member of this roster;
# consumers (archrules rule validation, clusters weighting) name subsets
# from it instead of re-spelling type literals (layout.py's ("inst",
# "attach") flag stays local: its stdlib+numpy import law forbids the
# graph import — documented deviation, PR #309 body).
EDGE_TYPES = ("call", "var", "signal", "inst", "attach", "alias")
EDGE_TYPE_SET = frozenset(EDGE_TYPES)


# -- function-level vector index (chroma "<collection>-fns") -------------------


def _fn_collection() -> "chromadb.Collection":
    # per-config collection: two checkouts/projects sharing one .chroma dir
    # must not mix function vectors (hardcoded name collided across configs)
    return navstore.fns_collection()


def _all_filesyms() -> dict[str, FileSym]:
    return get_graph().files




def sync_functions(changed: list[str], deleted: list[str]) -> dict[str, int]:
    """Re-embed functions of changed files, purge deleted files' functions,
    skipping unchanged ones via a per-entry content-hash cache (Cursor
    pattern): each stored fn keeps the sha256 of its embedded doc, so a
    rescan re-embeds only fns whose doc changed, refreshes metadata on
    line moves while reusing the stored vector, and purges fns that
    vanished from the file. At engine scale (~35k fns) the cold embed is
    ~2.3 h; the cache turns repeat rescans into minutes. Pre-cache
    collections migrate lazily: entries without a stored sha re-embed
    once. The dirty-marker self-heal still forces a full pass, but every
    cached skip verifies against the fresh parse, so dirty rebuilds stay
    correct and cheap. Purges resolve ids via where-get then
    delete(ids=...): chroma's delete(where=...) was observed to no-op
    silently under client churn while get(where=...) and delete(ids=...)
    stay reliable. Every chroma write rides navstore._db_lock (issue #117):
    the parse/read phase runs lock-free, purges and upserts serialize
    with all other writers (server auto-rescan vs CLI rescan)."""
    col = _fn_collection()
    # issue #220: the fn store stamps its embed mode like the file store;
    # a pass in the OTHER mode must not reuse sha-cached vectors from the
    # wrong vector space (sha-gating sees unchanged docs and would skip).
    # An absent key is pre-#220 real lineage, never a mismatch.
    stamped_mode = (col.metadata or {}).get("embed_mode", "real")
    mode_mismatch = stamped_mode != navstore.embed_mode()
    if mode_mismatch:
        print(
            f"neuronav: fn store '{col.name}' holds {stamped_mode!r}-mode "
            f"vectors but this pass embeds {navstore.embed_mode()!r} — re-embedding "
            "every function (#220)",
            file=sys.stderr,
        )
        changed = sorted(rel for rel, fs in _all_filesyms().items() if fs.funcs)
    cast = _cast_scale()  # nav CHUNK_CAST: 0.0 = legacy single-doc pass
    dirty = navconfig.DB_DIR / "fns.dirty"
    if dirty.is_file() and not changed:
        changed = sorted(rel for rel, fs in _all_filesyms().items() if fs.funcs)
    if navstore.col_count(col, "fn first-build gate") == 0 and not changed:
        # first build: index every parsed function (any text language)
        changed = sorted(
            rel for rel, fs in _all_filesyms().items() if fs.funcs
        )
    populated = navstore.col_count(col, "fn store populated") > 0
    purged_paths = 0
    purged_fns = 0

    # issue #117: _purge_path and the gone-ids deletes are chroma WRITES —
    # deferred to the locked phase below so they serialize with every
    # other writer per _db_lock's contract; the parse/read phase above
    # them stays lock-free (readers skip the lock)
    def _purge_path(rel: str) -> None:
        """Whole-path fn purge. Caller holds navstore._db_lock."""
        nonlocal purged_paths, purged_fns
        got = navstore.chroma_read(
            f"fn purge {rel}", lambda: col.get(where={"path": rel}, include=[])
        )
        if got["ids"]:
            col.delete(ids=got["ids"])
            purged_paths += 1
            purged_fns += len(got["ids"])

    purge_rels: list[str] = sorted(set(deleted)) if populated and deleted else []
    parser = Graph()
    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict[str, object]] = []
    moved: list[tuple[str, str, dict[str, object]]] = []
    gone_batches: list[list[str]] = []
    cached = 0
    for rel in changed:
        path = navconfig.ROOT / rel
        suffix = path.suffix
        # scenes have no funcs; only languages with an extractor are
        # parseable — a file that left parseable space is purged, not kept
        if not path.is_file() or not sync_parseable(suffix):
            if populated:
                purge_rels.append(rel)
            continue
        fs = registry_for(suffix).parse(path, rel)
        if cast > 0.0:
            _chunk_plan(fs, fs.funcs, cast)  # cAST micro-fn merge (knob on)
        current: set[str] = set()
        existing: dict[str, dict[str, object]] = {}
        if populated:
            got = navstore.chroma_read(
                f"fn cache {rel}",
                lambda: col.get(where={"path": rel}, include=["metadatas"]),
            )
            existing = {
                rid: meta or {}
                for rid, meta in zip(got["ids"], got["metadatas"])
            }
        for name, fn in fs.funcs.items():
            # signature line up front: better embeddings + agents see the IO
            # surface without opening the file
            sig = ", ".join(
                f"{p}: {t}" if t else p for p, t in fn.params
            )
            # chunk ids ride the fn-key grammar (never hand-built): the
            # key is fn_key(rel, ckey) with ckey = "name" or "name#chunkN",
            # so split_key() consumers resolve the parent file either way
            for doc, cline, ckey in _chunked_docs(fs, fn, sig, cast):
                crid = fn_key(rel, ckey)
                current.add(crid)
                sha = hashlib.sha256(doc.encode("utf-8")).hexdigest()
                meta: dict[str, object] = {
                    "path": rel, "name": name, "class_name": fs.class_name,
                    "line": cline, "sha": sha,
                }
                old = existing.get(crid)
                if (not mode_mismatch and old is not None
                        and old.get("sha") == sha):
                    if old.get("line") == cline:
                        cached += 1
                        continue
                    moved.append((crid, doc, meta))  # line move: reuse vector
                    continue
                ids.append(crid)
                docs.append(doc)
                metas.append(meta)
        gone = sorted(set(existing) - current)
        if gone:
            gone_batches.append(gone)
    try:
        with navstore._db_lock():
            for rel in purge_rels:
                _purge_path(rel)
            for gone in gone_batches:
                col.delete(ids=gone)
                purged_fns += len(gone)
            added = 0
            for i in range(0, len(ids), navstore.EMBED_BATCH):
                vecs = navstore.embed(docs[i : i + navstore.EMBED_BATCH])
                col.upsert(
                    ids=ids[i : i + navstore.EMBED_BATCH],
                    embeddings=vecs,
                    documents=docs[i : i + navstore.EMBED_BATCH],
                    metadatas=metas[i : i + navstore.EMBED_BATCH],
                )
                added += len(vecs)
            for rid, doc, meta in moved:
                got = navstore.chroma_read(
                    f"fn move {rid}",
                    lambda: col.get(ids=[rid], include=["embeddings"]),
                )
                if rid not in got["ids"]:
                    raise RuntimeError(f"fn vector vanished for {rid}")
                vec = [float(x) for x in got["embeddings"][0]]
                col.upsert(ids=[rid], embeddings=[vec], documents=[doc],
                           metadatas=[meta])
                added += 1
    except Exception:
        dirty.write_text("sync failed", encoding="utf-8")
        raise
    dirty.unlink(missing_ok=True)
    if mode_mismatch:
        # stamp the healed fn store so the next same-mode pass caches again
        navstore._restamp(col, embed_mode=navstore.embed_mode())
    return {
        "fns_upserted": added,
        "fns_cached": cached,
        "purged_paths": purged_paths,
        "purged_fns": purged_fns,
    }


def _fold_parents(rows: list[dict[str, object]], n: int) -> list[dict[str, object]]:
    """Rank-order collapse by (path, parent fn): a monster's name#chunkN
    docs are distinct chroma ids of ONE fn, so a raw top-k can be all
    siblings of a single fn (crowd-out — the #141 bench regression
    mechanism). Keeping each parent's best-ranked hit makes k results
    mean up to k distinct fns. Chunk docs carry the PARENT fn's name in
    metadata, so the fold needs no id surgery; a no-op when chunking is
    off (ids are unique per (path, name))."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, object]] = []
    for row in rows:
        key = (str(row.get("path", "")), str(row.get("func", "")))
        if key in seen:
            continue  # a name#chunkN sibling of an already-ranked fn
        seen.add(key)
        out.append(row)
        if len(out) >= n:
            break
    return out


def find_functions(query: str, n: int = 6) -> list[dict[str, object]]:
    """Semantic search over individual functions (vector index).
    Over-fetches 3n then collapses chunk siblings by (path, parent fn)
    (_fold_parents), so one monster's chunks cannot crowd out the
    top-k. Rows under the absolute relevance floor carry ``weak: True``
    (issue #297, mark-only — calibrated in recall.py; the real-embed
    noise band overlaps short-identifier golden tails, so flagging is
    honest where dropping would cost recall)."""
    col = _fn_collection()
    count = navstore.col_count(col, "fn vector-rank gate")
    if count == 0:
        return []
    vector = navstore.embed([query])[0]
    got = navstore.chroma_read(
        "fn vector ranks",
        lambda: col.query(
            query_embeddings=[vector],
            n_results=min(3 * n, count),
            include=["metadatas", "distances"],
        ),
    )
    out = []
    for rid, dist, meta in zip(
        got["ids"][0], got["distances"][0], got["metadatas"][0]
    ):
        meta = meta or {}
        score = round(1.0 - float(dist), 4)
        row = {
            "key": rid,
            "score": score,
            "path": str(meta.get("path", "")),
            "func": str(meta.get("name", "")),
            "line": int(meta.get("line", 0)),
        }
        # issue #297: one floor, two surfaces — recall owns the constant
        if score < recall.RELEVANCE_FLOOR_SIM:
            row["weak"] = True
        out.append(row)
    return _fold_parents(out, n)


# -- repo map: module surface + budget metric -----------------------------------

MAP_MAX_SIGS = 8  # per-file signature cap: god files show a slice, not everything


def _toks(s: str) -> int:
    """Token estimate for repo-map budgeting: 1 token ~= 4 chars.
    Deterministic; the map's budget contract is measured in this metric."""
    return (len(s) + 3) // 4


def pagerank(damping: float = 0.85, iters: int = 30) -> dict[str, float]:
    """File-level PageRank over the shared graph singleton (wire-weighted)."""
    return get_graph().pagerank(damping=damping, iters=iters)


def repo_map(budget_tokens: int = 2048) -> str:
    """Budgeted repo map over the shared graph singleton."""
    return get_graph().repo_map(budget_tokens=budget_tokens)


# -- module-level singleton ---------------------------------------------------

_graph: Graph | None = None


def get_graph(rebuild: bool = False) -> Graph:
    global _graph
    if _graph is None or rebuild:
        _graph = Graph().build()
    return _graph
