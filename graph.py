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
"""

from __future__ import annotations

import hashlib
import re
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path

import nav
import predicates
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

# ---- constants ---------------------------------------------------------------
# Language-owned constants, entry-point rules and body-scan patterns live
# in extractors/ (see the import surface above). What remains here is
# language-neutral graph law: the fn-key helpers, dead-tier weights, and
# the cAST chunking knobs.




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
        for path in nav.iter_files():
            rel = nav.file_id(path)
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
        amap = harvest_autoloads(nav.ROOT, scripts_only=True)
        self.autoloads = {n: r for n, r in amap.items() if r in self.files}
        for name, rel in self.autoloads.items():
            self.class_map.setdefault(name, rel)

        # asset scenes sit outside the search index but carry animation method
        # tracks + connections that fire script funcs — parse for wiring only
        for path in (nav.ROOT / "assets").rglob(ASSET_SCENE_GLOB) if (nav.ROOT / "assets").is_dir() else ():
            rel = nav.file_id(path)
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
        return nav._read_text(nav.ROOT / rel)

    def path_for(self, rel: str) -> Path:
        return nav.ROOT / rel

    def walk_root_files(self, suffixes):
        return nav.iter_root_files(suffixes)

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
                    text = nav._read_text(nav.ROOT / rel)
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
        for rel, fs in self.files.items():
            for name, fn in fs.funcs.items():
                norm = _normalize_body(fn.body)
                if len(norm.splitlines()) < 3:
                    continue  # trivial
                h = hashlib.sha1(norm.encode()).hexdigest()
                groups[h].append(fn.key)
                norms[h] = norm
        dups: list[dict[str, object]] = []
        skipped = 0
        for h, v in groups.items():
            if len(v) < 2:
                continue
            if _pure_delegate(norms[h]):
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


def _normalize_body(body: str) -> str:
    out = []
    for line in body.splitlines():
        s = line.split("#", 1)[0].rstrip()
        if not s.strip():
            continue
        out.append("  " + s.strip())  # unify indent
    return "\n".join(out)


_SIG_LINE_RE = re.compile(r"^(?:async\s+)?(?:def|func|fn)\s+\w+")
_GUARD_RE = re.compile(r"^(?:el)?if\s+[^():]+:$")
_GUARD_RET_RE = re.compile(r"^return\s+[^()]*$")
_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*(?:\.\w+)* = [^()=]+$")
_FORWARD_RE = re.compile(r"^return\s+(?:await\s+)?[A-Za-z_][\w.]*\([\w\s,]*\)$")


def _pure_delegate(norm: str) -> bool:
    """True for thin delegation wrappers (issue #268): after an
    optional signature line, the body is only call-free guards (if/elif
    whose one-line body is a call-free return — an early-out — or a
    call-free assignment — arg normalization; at most two), at most one
    call-free assignment, and a single forwarding `return call(args)`.
    The regexes carry #116's conservatism — parens in guards/
    assignments, operators in the forwarding call's args, any extra
    statement, or a brace-language body never classify, so the filter
    can miss a wrapper but never drops real duplicated logic."""
    # _normalize_body emits a uniform two-space indent; strip it so the
    # statement regexes match shape, not indentation
    lines = [ln.strip() for ln in norm.splitlines()]
    if lines and _SIG_LINE_RE.match(lines[0]):
        lines = lines[1:]  # python bodies keep their signature line
    if not 2 <= len(lines) <= 5:
        return False
    i = 0
    while i + 1 < len(lines) and _GUARD_RE.match(lines[i]):
        if not (
            _GUARD_RET_RE.match(lines[i + 1]) or _ASSIGN_RE.match(lines[i + 1])
        ):
            break
        i += 2
    if i < len(lines) and _ASSIGN_RE.match(lines[i]):
        i += 1
    return i == len(lines) - 1 and _FORWARD_RE.match(lines[i]) is not None


# -- function-level vector index (chroma "<collection>-fns") -------------------


def _fn_collection() -> "chromadb.Collection":
    # per-config collection: two checkouts/projects sharing one .chroma dir
    # must not mix function vectors (hardcoded name collided across configs)
    return nav.fns_collection()


def _all_filesyms() -> dict[str, FileSym]:
    return get_graph().files


# -- cAST-style size-aware doc chunking (issue #76) ---------------------------

# size thresholds, calibrated on the self-index corpus (docs/comparison.md):
# the median fn body is a handful of lines — getters/stubs and one-liners
# give no recall surface of their own, and monster fns (>2k chars, the
# model-context scale) drown their own signature under unrelated body
# tokens. The whole pass is gated by the nav config knob CHUNK_CAST
# (``chunk_cast``, default 0.0 = OFF: the fresh-store #141 bench A/B
# showed no lift — recall reads the file layer, the fn layer is
# invisible to it — so flipping the default needs an A/B that shows
# one): off, every fn keeps the single
# historical doc under fn_key(rel, name); on (1.0 = calibrated, other
# positives scale the thresholds), monsters split into statement-block
# chunk docs under fn_key(rel, "name#chunkN") — chunk ids ride the
# fn-key grammar, so split_key() still resolves the parent file.
MICRO_FN_CHARS = 220    # bodies at or below this merge into class context
MONSTER_FN_CHARS = 2000  # bodies above this split at statement block boundaries
CHUNK_DOC_CAP = 8000     # per-doc ceiling for class-merged and chunk docs


def _cast_scale() -> float:
    """The cAST chunking knob: nav's CHUNK_CAST (config ``chunk_cast``),
    read at call time so config_scope rebinding is honored. 0.0 = OFF —
    sync_functions emits the pre-#141 single doc per fn, byte-identical
    ids/docs/metadata; 1.0 = calibrated thresholds; other positive
    values scale MICRO_FN_CHARS/MONSTER_FN_CHARS. Negative clamps to
    0.0 (treated as off)."""
    return max(0.0, float(getattr(nav, "CHUNK_CAST", 0.0)))


_IF_DEDENT_RE = re.compile(r"^(\s+)else:|^(\s+)elif\s|^(\s+)except|^(\s+)finally:|^(\s+)catch|^(\s+)case\b|^(\s*)@(\w)|^(\s*)}$")
_OPEN_RE = re.compile(r"[(\[{]$")
_TRIPLE_RE = re.compile(r'"""|\'\'\'')


def _fn_body_start(fn: Func) -> int:
    """Line index of the first INDENTED statement inside a fn body — the
    body proper (past the signature, which may span continuation lines).
    Signature lines rest at or above the def line's indent; the first body
    statement is strictly deeper. Reports len(lines) when the body is
    empty, so callers slice harmlessly."""
    lines = fn.body.splitlines()
    base = len(lines[0]) - len(lines[0].lstrip(" \t")) if lines else 0
    for i in range(1, len(lines)):
        nxt = lines[i]
        if nxt.strip() and (len(nxt) - len(nxt.lstrip(" \t"))) > base:
            return i
    return len(lines)


def _chunk_line_offsets(body: str) -> list[int]:
    """Line indices of top-level statement-block starts within a fn body
    PROPER (line 0 = the first statement, past the signature) — the cAST
    AST-boundary split points. base is the first statement's indent, so a
    later statement at that same indent (sequential or following a dedent)
    starts a new block, as do brace closures (`}`), decorators, and
    else/elif/catch lines. Bracket continuations never count: the opener
    line ends with an open bracket, continuation lines sit deeper than the
    statement indent, and closing-bracket lines start with the closer.
    Triple-quoted string content is skipped regardless of its indent (a
    heredoc can mine column-0 lines that merely LOOK like dedents).
    Deterministic — a pure function of the body text."""
    lines = body.splitlines()
    if not lines:
        return []
    base = len(lines[0]) - len(lines[0].lstrip(" \t"))
    out: list[int] = []
    prev_end_open = False
    in_triple: str | None = None

    def _find_triple(ln: str) -> tuple[str | None, str | None]:
        """(opener, rest) — the first triple-quote mark on the line, if any."""
        for mark in ('"""', "'''"):
            pos = ln.find(mark)
            if pos >= 0:
                return mark, ln[pos + 3 :]
        return None, None

    # the first statement may open a triple-quoted docstring itself
    t0_open, rest = _find_triple(lines[0])
    if t0_open and rest.count(t0_open) % 2 == 0:
        t0_close = rest.find(t0_open)
        if t0_close < 0:
            in_triple = t0_open
    for idx in range(1, len(lines)):
        ln = lines[idx]
        if in_triple:
            pos = ln.find(in_triple)
            if pos >= 0:
                in_triple = None
            continue
        stripped = ln.strip()
        if not stripped:
            prev_end_open = False
            continue
        if _OPEN_RE.search(ln.rstrip()):
            prev_end_open = True
            continue
        tm, rest = _find_triple(ln)
        if tm:
            if rest.count(tm) % 2 == 0:
                in_triple = None
            else:
                in_triple = tm
            prev_end_open = False
            continue
        if stripped.endswith((")", "]", "}")):
            prev_end_open = False
        ind = len(ln) - len(ln.lstrip(" \t"))
        if (
            (ind <= base or _IF_DEDENT_RE.match(ln))
            and not prev_end_open
            and not stripped.startswith((")", "]", "}"))
        ):
            out.append(idx)
        prev_end_open = False
    return out


def _first_stmt(text: str) -> int:
    """1-based line of the first non-blank line in ``text``."""
    for i, ln in enumerate(text.splitlines(), 1):
        if ln.strip():
            return i
    return 1


def _chunks(fn: Func, sig: str, blocks: list[int], scale: float = 1.0) -> list[tuple[str, int]]:
    """Statement-block chunk slices of a monster fn: (chunk_text, abs_line)
    per block, in body order. ``blocks`` are ABSOLUTE line indices of
    statement-block starts inside fn.body (including the body's first
    statement — callers compute them via _chunk_line_offsets on the body
    proper and re-base). The body's statement blocks are packed greedily
    into chunks sized to the retrieval cap — consecutive small blocks share
    a chunk (doc count stays near the pre-split value), a single oversized
    block bisects at its statement lines, hard-bisecting at the half-cap
    when the grammar sees no inner boundary (a giant literal). The
    signature rides EVERY chunk so each is a self-contained retrieval unit.
    Deterministic — a pure function of (fn, sig, blocks, scale)."""
    lines = fn.body.splitlines()
    n_lines = len(lines)
    points = sorted({b for b in blocks if 0 < b < n_lines} | {n_lines})
    cap = int(MONSTER_FN_CHARS * scale)  # scaled retrieval ceiling
    body_cap = cap - len(sig) - 2
    out: list[tuple[str, int]] = []

    def emit(start: int, end: int) -> None:
        """One chunk over body-line indices [start, end). An oversized
        segment (a single giant statement block, e.g. a 700-line loop body)
        cuts a PREFIX that fits under the cap — aligned back to the nearest
        statement boundary when one sits inside the safe prefix — then
        recurses on the remainder (cAST: oversized node with children ->
        split there instead). Prefix-fit keeps every chunk near-full, so
        the doc count stays ~chars/cap, not 2x that."""
        text = "\n".join(lines[start:end]).strip()
        if not text:
            return
        chunk = sig + "\n" + text
        first_abs = next((i for i in range(start, end) if lines[i].strip()), start)
        abs_line = fn.line + first_abs  # 1-based def line + 0-based body offset
        if len(chunk) <= cap:
            out.append((chunk, abs_line))
            return
        acc = len(sig) + 2
        cut = end
        for i in range(start, end):
            acc += len(lines[i]) + 1
            if acc >= cap:
                cut = i + 1
                break
        aligned = [b for b in blocks if start < b < cut]
        if aligned:
            cut = aligned[-1]
        if cut <= start or cut >= end:
            out.append((chunk, abs_line))  # unsplittable single line
            return
        emit(start, cut)
        emit(cut, end)

    prev = points[0]  # end of the last consumed statement block
    cut = points[0]   # start of the accumulating chunk segment
    acc = 0
    for i in range(1, len(points)):
        end = points[i]
        piece_chars = sum(len(ln) + 1 for ln in lines[prev:end])
        if acc and acc + piece_chars > body_cap:
            emit(cut, prev)  # flush the accumulated segment at a block boundary
            cut = prev
            acc = 0
        acc += piece_chars
        if end >= n_lines:
            emit(cut, end)
        prev = end
    return out


def _chunk_intro(fn: Func) -> str:
    """The fn's big-picture title line, if one opens the body: the first
    line of a docstring or a `#`/`##` comment. Kept short; anything else
    (a real first statement) is not an intro."""
    lines = fn.body.splitlines()
    if len(lines) < 2:
        return ""
    first = lines[1].strip()
    if first.startswith(('"""', "'''")):
        return first.strip("'\" ")[:48]
    if first.startswith("#") and len(first) <= 96:
        return first.lstrip("#").strip()[:48]
    return ""


def _chunk_docs(fn: Func, sig: str, blocks: list[int], scale: float = 1.0) -> list[tuple[str, int]]:
    """(doc, line) pairs for a monster fn's statement-block chunks. A body
    that opens with a docstring/title comment keeps that intro on EVERY
    later chunk (`# <first line>`), so prose retrieval does not lose the
    big-picture orientation (cAST keeps signature-first docs; a leading
    intro is part of the signature surface)."""
    intro = _chunk_intro(fn)
    if not intro:
        return _chunks(fn, sig, blocks, scale)
    chunks = _chunks(fn, sig, blocks, scale)
    out = []
    for i, (text, line_no) in enumerate(chunks, 1):
        if i > 1:
            text = sig + "\n# " + intro + "\n" + text[len(sig) :].lstrip("\n")
        out.append((text, line_no))
    return out


def _fn_doc(fs: FileSym, fn: Func, sig: str) -> str:
    """The cAST size-aware fn document (issue #76) for one (already-shaped)
    fn: ``class_ctx`` folks fold in their merged member bodies, ``chunk``
    entries are pre-built split docs, everything else keeps the historical
    signature-first raw doc. The 6000-char raw ceiling and the class/chunk
    caps preserve the pre-chunking recall surface (docs/comparison.md
    pinned the fps/recall wins; the bench golden set targets these fns)."""
    head = f"{fs.path} :: func {fn.name}({sig}){(' -> ' + fn.ret) if fn.ret else ''}"
    if fn.kind == "class_ctx":
        parts = [head]
        for mname, _line, mbody in fn.members:
            parts.append("-- " + mname + " --")
            parts.append(mbody)
        return "\n".join(parts)[:CHUNK_DOC_CAP]
    if fn.kind == "chunk":
        return fn.body[:CHUNK_DOC_CAP]
    return head + "\n" + fn.body[:6000]


def _is_micro(fn: Func, scale: float = 1.0) -> bool:
    """cAST micro-fn test (issue #76): trivial getters/stubs/one-liners —
    body past the signature at or under MICRO_FN_CHARS. When the first
    line is NOT a signature (a raw GDScript body fragment already past
    the header), measure the whole fragment — long-param masking only
    applies to bodies that actually carry their signature line."""
    lines = fn.body.splitlines()
    if not lines:
        return False
    nb = _fn_body_start(fn)
    if not lines[0].strip().endswith(":") and not lines[0].lstrip().startswith(FUNC_KEYWORD):
        body = fn.body  # raw fragment: no signature line to strip
    else:
        body = "\n".join(lines[nb:]) if nb < len(lines) else ""
    return 0 < len(body) <= int(MICRO_FN_CHARS * scale)


def _micro_groups(funcs: dict[str, Func], scale: float = 1.0) -> dict[str, list[Func]]:
    """Pure micro-fn grouping shared by the fn layer (#76) and the file
    layer (#229): each micro fn (see _is_micro) folds into the nearest
    NON-micro fn above it in source order — the class body document
    that owns its neighborhood. A micro fn above no non-micro fn stays
    standalone (nothing to merge into). Deterministic — a pure
    function of the parsed funcs."""
    order = sorted(funcs.items(), key=lambda kv: (kv[1].line, kv[0]))
    groups: dict[str, list[Func]] = {}
    for name, fn in order:
        if not _is_micro(fn, scale):
            continue
        above = [
            cn for cn, cfn in order
            if not _is_micro(cfn, scale) and cfn.line < fn.line
        ]
        if not above:
            continue  # no fn above: nothing to merge into
        groups.setdefault(above[-1], []).append(fn)
    return groups


def _overlay_class_context(funcs: dict[str, Func], fs: FileSym, scale: float = 1.0) -> None:
    """cAST micro-fn merge (issue #76): fold a class file's micro-functions
    (getters/stubs/one-liners, incl. GDScript property accessors —
    ``_set_x``/``_get_x``) into the class method doc that owns their
    neighborhood: the nearest NON-micro method above them (the class body
    document in source order). The merge is carried on the carrier fn's
    Func (kind='class_ctx'), so the fn index still emits one entry per fn —
    retrieval, dead-code, explore and the mwires roster are untouched; the
    extra context only widens the vector surface. When the whole class is
    micro (no non-micro method exists to carry the fold), fns stay
    standalone — there is nothing to merge into. Modules without a
    class_name (python modules, tool scripts) are never merged: a module
    has no class document."""
    if not fs.class_name:
        return
    groups = _micro_groups(funcs, scale)
    for carrier in sorted(groups, key=lambda c: (funcs[c].line, c)):
        add_class_ctx(funcs, carrier, groups[carrier])


def _chunked_docs(fs: FileSym, fn: Func, sig: str, scale: float = 1.0) -> list[tuple[str, int, str]]:
    """The cAST size-aware docs for ONE fn: (doc, line, key). Regression —
    unchanged fns keep a single signature-first doc under ``name``; a
    monster fn (>MONSTER_FN_CHARS * scale) splits into statement-block
    chunk docs under ``name#chunkN`` (docs grow < 1/fn — the win
    condition); a class_ctx fn (pre-folded by _overlay_class_context)
    emits one wider doc. Never mutates fs.funcs — chroma ids are the only
    surface that grows. scale <= 0 (knob off) keeps the single historical
    doc — the byte-identical pre-#141 recall surface."""
    if scale <= 0.0:
        return [(_fn_doc(fs, fn, sig), fn.line, fn.name)]
    if fn.kind == "class_ctx":
        return [(_fn_doc(fs, fn, sig), fn.line, fn.name)]
    if fn.kind == "chunk":
        return [(_fn_doc(fs, fn, sig), fn.line, fn.name)]
    body = fn.body
    if len(body) <= int(MONSTER_FN_CHARS * scale):
        return [(_fn_doc(fs, fn, sig), fn.line, fn.name)]
    # monster: split at statement blocks, driving doc ids
    lines = body.splitlines()
    nb = _fn_body_start(fn)
    body_proper = "\n".join(lines[nb:]) if nb < len(lines) else ""
    if not body_proper:
        return [(_fn_doc(fs, fn, sig), fn.line, fn.name)]
    offs = _chunk_line_offsets(body_proper)
    blocks = [nb] + [nb + i for i in offs]  # absolute (first stmt included)
    if len(blocks) < 2:
        # monster with a single giant statement: hard-bisect the body
        blocks = []
        nlines = len(lines)
        seg = nb
        step = max(1, (nlines - nb) // max(2, len(body) // int(MONSTER_FN_CHARS * scale)))
        while seg < nlines - 1:
            seg = min(nlines - 1, seg + step)
            blocks.append(seg)
    docs: list[tuple[str, int, str]] = []
    for i, (chunk, aline) in enumerate(_chunk_docs(fn, sig, blocks, scale), 1):
        docs.append((chunk, aline, f"{fn.name}#chunk{i}"))
    return docs


def _chunk_plan(fs: FileSym, funcs: dict[str, Func], scale: float = 1.0) -> None:
    """Shape the fn docs for one file BEFORE the sync loop: fold micro fns
    into class context (mutating Func.kind/members), so the loop's
    per-fn _chunked_docs sees stable shapes. Deterministic — pure function
    of the parsed FileSym + funcs."""
    _overlay_class_context(funcs, fs, scale)


# -- cAST file-doc shaping (issue #229) ---------------------------------------

# The #76 chunking lifted to the file layer — where recall actually reads
# it. The fn collection ("-fns") is invisible to recall.search (the #141
# fresh-store A/B showed no lift), so the size-aware shape earns its keep
# on the docs nav embeds per FILE: recall's vector side queries exactly
# that collection. Self-index distribution that calibrates the reuse:
# 12/58 files exceed nav.MAX_EMBED_CHARS (30k) — their bytes past the
# truncation are invisible to the vector side today (viz.py 462k = 6.5%
# visible; graph.py/nav.py/server.py all >50k); fn bodies p50=547 chars,
# micro (<=220) = 28%, monster (>2000) = 15% — the #76 thresholds already
# sit at the p25/p90 boundaries, so the file layer reuses them unchanged.
FILE_DOC_REV = 2        # shaper semantics version — bump whenever the
                        # shaper changes docs for the same input bytes;
                        # nav's doc_shape stamp rides it so shape-lineaged
                        # stores re-embed loudly instead of serving stale
                        # vectors under sha-gating (#220 law, doc side).
                        # rev 2: the "# imports:" head line (#229 extension)
FILE_SYMBOLS_CAP = 1200  # symbol-surface line budget (chars)
FILE_IMPORTS_CAP = 400   # import-surface line budget (chars) — the file's
                         # resolved imports ride the doc head (cAST's
                         # contextual-awareness gap; RepoCoder context
                         # augmentation), ~1.3% of the 30k embed budget
FILE_INTRO_CAP = 400     # module docstring / leading-comment budget

_ENC_RE = re.compile(r"^#.*?coding[:=]")


def _file_intro(text: str) -> str:
    """The file's big-picture opener (cAST keeps intros on chunks; the
    file analog): the leading module docstring or `#` comment block,
    past shebang/encoding lines. '' when the file opens with code.
    Language-neutral — both block spellings are cross-language text
    shapes, no suffix dispatch. Deterministic."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and (
        not lines[i].strip()
        or lines[i].startswith("#!")
        or _ENC_RE.match(lines[i])
    ):
        i += 1
    if i >= len(lines):
        return ""
    out: list[str] = []
    first = lines[i].lstrip()
    if first.startswith(('"""', "'''")):
        mark = first[:3]
        rest = first[3:]
        close = rest.find(mark)
        if close >= 0:
            out.append(rest[:close])
        else:
            out.append(rest)
            i += 1
            while i < len(lines):
                ln = lines[i]
                pos = ln.find(mark)
                if pos >= 0:
                    out.append(ln[:pos])
                    break
                out.append(ln)
                i += 1
    elif first.startswith("#"):
        while i < len(lines) and lines[i].lstrip().startswith("#"):
            stripped = lines[i].lstrip().lstrip("#").strip()
            if stripped:
                out.append(stripped)
            i += 1
    return "\n".join(ln for ln in out if ln.strip())[:FILE_INTRO_CAP]


def _fn_sections(fs: FileSym, fn: Func, scale: float = 1.0) -> list[str]:
    """Signature-first doc sections for ONE fn at file scale (#229): a
    normal fn is its verbatim body (bodies open at the def line —
    signature-first by construction); a monster (> MONSTER_FN_CHARS *
    scale) splits at statement-block boundaries with the FULL signature
    lines on every chunk plus the body intro — the #76 `_chunked_docs`
    mechanics reused verbatim, only the signature surface differs (the
    fn layer's chunks carry the params-only sig; file sections carry
    the whole def header). Deterministic — pure function of (fn, scale)."""
    body = fn.body
    if len(body) <= int(MONSTER_FN_CHARS * scale):
        return [body]
    lines = body.splitlines()
    nb = _fn_body_start(fn)
    if nb >= len(lines):
        return [body]  # no body proper past the signature: nothing to split
    sig = "\n".join(lines[:nb])
    return [doc for doc, _line, _key in _chunked_docs(fs, fn, sig, scale)]


def file_doc(path: Path, rel: str, text: str, scale: float = 1.0) -> str:
    """The cAST-shaped embed document for one file (issue #229) — what
    nav._rescan_locked embeds and stores in place of the raw file text.
    Size-aware, signature-first (cAST 2025; RepoBench):
    - head: path, class/extends, the full symbol surface (every fn name
      rides the doc, capped), the resolved import surface (`# imports:`,
      capped — cAST's contextual-awareness gap), and the module intro;
    - micro fns merge into the nearest non-micro fn above them — the
      class-context fold (`_micro_groups`), members as `-- name --`
      banners under their carrier;
    - monster fns split at statement-block boundaries, every chunk
      signature-first (`_fn_sections`);
    - sections flatten by (chunk index, source line): chunk 1 of EVERY
      fn embeds before chunk 2 of ANY fn, so a 460k file no longer
      buries its later fns under the 30k embed truncation;
    - assembly stays under nav.MAX_EMBED_CHARS — the embed-side
      truncation never clips shaped docs blind.
    Fallbacks keep the raw text verbatim: scale <= 0 (knob off — the
    byte-identical pre-#229 surface), no parser for the suffix, or a
    parse with no fns (nothing structural to shape). Deterministic —
    a pure function of (file bytes, rel, scale); never mutates the
    parsed FileSym, so BM25F's graph view is untouched."""
    if scale <= 0.0:
        return text
    mod = registry_for(path.suffix)
    if mod is None:
        return text
    fs = mod.parse(path, rel)
    if not fs.funcs:
        return text
    import nav  # lazy: the budget mirrors the embed-side truncation

    head = [f"# {rel}"]
    if fs.class_name:
        head.append(f"# class {fs.class_name}"
                    + (f" extends {fs.extends}" if fs.extends else ""))
    syms = (sorted(fs.funcs) + sorted(fs.signals)
            + sorted(fs.members) + sorted(fs.consts))
    line = "# symbols: " + " ".join(syms)
    if len(line) > FILE_SYMBOLS_CAP:
        keep: list[str] = []
        used = len("# symbols: ")
        room = FILE_SYMBOLS_CAP - 8  # headroom for the (+N) tail
        for name in syms:
            if used + len(name) + 1 > room:
                break
            keep.append(name)
            used += len(name) + 1
        line = "# symbols: " + " ".join(keep) + f" (+{len(syms) - len(keep)})"
    head.append(line)
    # whole-module imports (side-effect/namespace/dynamic) plus the module
    # side of named imports — the doc head mirrors what the file pulls in,
    # not the liveness contract (imported_modules alone stays whole-module-
    # alive facts; from_imports carry the named bindings)
    imps = sorted(fs.imported_modules | {m for m, _ in fs.from_imports})
    if imps:
        line = "# imports: " + " ".join(imps)
        if len(line) > FILE_IMPORTS_CAP:
            keep = []
            used = len("# imports: ")
            room = FILE_IMPORTS_CAP - 8  # headroom for the (+N) tail
            for mod in imps:
                if used + len(mod) + 1 > room:
                    break
                keep.append(mod)
                used += len(mod) + 1
            line = "# imports: " + " ".join(keep) + f" (+{len(imps) - len(keep)})"
        head.append(line)
    intro = _file_intro(text)
    if intro:
        head.append(intro)
    doc_head = "\n".join(head)

    groups = _micro_groups(fs.funcs, scale)
    folded = {fn.name for micros in groups.values() for fn in micros}
    per_fn: list[list[str]] = []
    for name, fn in sorted(fs.funcs.items(), key=lambda kv: (kv[1].line, kv[0])):
        if name in folded:
            continue
        sections = _fn_sections(fs, fn, scale)
        micros = groups.get(name)
        if micros:
            sections[-1] += "\n" + "\n".join(
                f"-- {m.name} --\n{m.body}" for m in micros
            )
        per_fn.append(sections)
    budget = int(nav.MAX_EMBED_CHARS)
    parts: list[str] = [doc_head]
    used = len(doc_head)
    for idx in range(max((len(s) for s in per_fn), default=0)):
        for sections in per_fn:
            if idx >= len(sections):
                continue
            part = sections[idx]
            if used + len(part) + 1 > budget:
                continue
            parts.append(part)
            used += len(part) + 1
    return "\n".join(parts)


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
    stay reliable. Every chroma write rides nav._db_lock (issue #117):
    the parse/read phase runs lock-free, purges and upserts serialize
    with all other writers (server auto-rescan vs CLI rescan)."""
    col = _fn_collection()
    # issue #220: the fn store stamps its embed mode like the file store;
    # a pass in the OTHER mode must not reuse sha-cached vectors from the
    # wrong vector space (sha-gating sees unchanged docs and would skip).
    # An absent key is pre-#220 real lineage, never a mismatch.
    stamped_mode = (col.metadata or {}).get("embed_mode", "real")
    mode_mismatch = stamped_mode != nav.embed_mode()
    if mode_mismatch:
        print(
            f"neuronav: fn store '{col.name}' holds {stamped_mode!r}-mode "
            f"vectors but this pass embeds {nav.embed_mode()!r} — re-embedding "
            "every function (#220)",
            file=sys.stderr,
        )
        changed = sorted(rel for rel, fs in _all_filesyms().items() if fs.funcs)
    cast = _cast_scale()  # nav CHUNK_CAST: 0.0 = legacy single-doc pass
    dirty = nav.DB_DIR / "fns.dirty"
    if dirty.is_file() and not changed:
        changed = sorted(rel for rel, fs in _all_filesyms().items() if fs.funcs)
    if col.count() == 0 and not changed:
        # first build: index every parsed function (any text language)
        changed = sorted(
            rel for rel, fs in _all_filesyms().items() if fs.funcs
        )
    populated = col.count() > 0
    purged_paths = 0
    purged_fns = 0

    # issue #117: _purge_path and the gone-ids deletes are chroma WRITES —
    # deferred to the locked phase below so they serialize with every
    # other writer per _db_lock's contract; the parse/read phase above
    # them stays lock-free (readers skip the lock)
    def _purge_path(rel: str) -> None:
        """Whole-path fn purge. Caller holds nav._db_lock."""
        nonlocal purged_paths, purged_fns
        got = nav.chroma_read(
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
        path = nav.ROOT / rel
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
            got = nav.chroma_read(
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
        with nav._db_lock():
            for rel in purge_rels:
                _purge_path(rel)
            for gone in gone_batches:
                col.delete(ids=gone)
                purged_fns += len(gone)
            added = 0
            for i in range(0, len(ids), nav.EMBED_BATCH):
                vecs = nav.embed(docs[i : i + nav.EMBED_BATCH])
                col.upsert(
                    ids=ids[i : i + nav.EMBED_BATCH],
                    embeddings=vecs,
                    documents=docs[i : i + nav.EMBED_BATCH],
                    metadatas=metas[i : i + nav.EMBED_BATCH],
                )
                added += len(vecs)
            for rid, doc, meta in moved:
                got = nav.chroma_read(
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
        nav._restamp(col, embed_mode=nav.embed_mode())
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
    top-k."""
    col = _fn_collection()
    count = col.count()
    if count == 0:
        return []
    vector = nav.embed([query])[0]
    got = nav.chroma_read(
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
        out.append(
            {
                "key": rid,
                "score": round(1.0 - float(dist), 4),
                "path": str(meta.get("path", "")),
                "func": str(meta.get("name", "")),
                "line": int(meta.get("line", 0)),
            }
        )
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
