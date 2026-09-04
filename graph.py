"""swmg-nav structural layer: function/signal graph, dead code, duplicates.

Parses GDScript + .tscn from the checkout into an in-memory graph:
- functions with bodies (indentation-delimited)
- call edges: ClassName.func (via class_name map), bare same-file calls
- signal edges: emit sites -> connect() handlers, plus .tscn [connection] blocks
- entry roots: autoloads, virtuals (_ready/_process/...), connected handlers,
  GUT test_*, string-callable references (call("x"), Callable(self, "x"))
- dead code = functions unreachable from roots (two confidence tiers)
- duplicates = normalized-body hashes + cosine-similar function vectors

Zero non-vendor deps beyond nav (reuses its file walk + embed).
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field

import nav

# ---- constants ---------------------------------------------------------------

VIRTUALS = {
    "_init", "_enter_tree", "_ready", "_exit_tree", "_process",
    "_physics_process", "_input", "_shortcut_input", "_unhandled_input",
    "_unhandled_key_input", "_draw", "_notification", "_get", "_set",
    "_get_property_list", "_to_string", "_integrate_forces",
    "_get_configuration_warnings",
}
GUT_ROOTS = {"before_all", "after_all", "before_each", "after_each"}

# C++-side addon base classes dispatch these methods; unresolvable in a pure
# source graph (bases live in addons/*.gdext). Treated as entry roots.
ADDON_VIRTUALS: dict[str, set[str]] = {
    base: {"_enter", "_exit", "_tick", "_setup", "_generate_name"}
    for base in ("btaction", "btcondition", "btdecorator", "btcomposite", "bttask")
}

FUNC_RE = re.compile(r"^([ \t]*)(?:static\s+)?func\s+([A-Za-z_]\w*)\s*\(")
SIGNAL_RE = re.compile(r"^[ \t]*signal\s+([A-Za-z_]\w*)")
CLASSNAME_RE = re.compile(r"^[ \t]*class_name\s+([A-Za-z_]\w*)")
EXTENDS_RE = re.compile(r"^[ \t]*extends\s+([A-Za-z_]\w*)")
QUALIFIED_CALL_RE = re.compile(r"(?<![\w.$])([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*\(")
BARE_CALL_RE = re.compile(r"(?<![\w.$])([A-Za-z_]\w*)\s*\(")
EMIT_RE = re.compile(r"emit_signal\(\s*[\"'](\w+)[\"']|([A-Za-z_]\w*)\.emit\(")
# member-var type declarations at file top level: `var x: Type` / `var x := Type.new()`
MEMBER_TYPED_RE = re.compile(r"^var\s+(\w+)\s*:\s*([A-Z]\w*)\s*(?:=|$)")
MEMBER_NEW_RE = re.compile(r"^var\s+(\w+)\s*(?::=|=)\s*([A-Z]\w*)\.new\(")
# typed locals + params anywhere in a body: `name: Type`
PARAM_TYPED_RE = re.compile(r"(?<![\w.])(\w+)\s*:\s*([A-Z]\w*)")
# animation method call tracks inside .tscn: "method": &"on_x" / "method": "on_x"
ANIM_METHOD_RE = re.compile(r'"method":\s*&?"(\w+)"')
# cast-then-call: (node as CameraShake).shake(  ->  Type.method(
AS_CAST_CALL_RE = re.compile(r"as\s+([A-Z]\w*)\)\s*\.\s*([A-Za-z_]\w*)\s*\(")
CONNECT_RE = re.compile(r"\.connect\(|Callable\(")
STRING_NAME_RE = re.compile(r"[\"']([A-Za-z_]\w*)[\"']")
TOOL_RE = re.compile(r"^[ \t]*(@tool|\btool\b)")

# bare identifiers that are engine globals/keywords, never local calls
NON_CALLS = {
    "if", "elif", "while", "for", "match", "return", "await", "func", "super",
    "and", "or", "not", "in", "is", "break", "continue", "pass", "class",
    "self", "true", "false", "null", "void", "static", "const", "var",
    "signal", "enum", "export", "onready", "tool", "yield",
    "print", "printerr", "push_error", "push_warning", "push_notice",
    "str", "int", "float", "bool", "len", "range", "abs", "absf", "absi",
    "min", "max", "minf", "maxf", "mini", "maxi", "clamp", "clampf", "clampi",
    "lerp", "lerpf", "lerp_angle", "randf", "randi", "randf_range",
    "randi_range", "randfn", "preload", "load", "resource_local_to_scene",
    "assert", "is_instance_valid", "instance_from_id", "weakref", "hash",
    "typeof", "type_string", "str_to_var", "var_to_str", "bytes_to_var",
    "var_to_bytes", "inst_to_dict", "dict_to_inst", "ord", "char",
    "range_lerp", "smoothstep", "move_toward", "ease", "step_decimals",
    "snapped", "fmod", "fposmod", "posmod", "floor", "floori", "ceil",
    "ceili", "round", "roundi", "sqrt", "pow", "sin", "cos", "tan", "asin",
    "acos", "atan", "atan2", "exp", "log", "is_nan", "is_inf", "is_finite",
    "is_equal_approx", "is_zero_approx", "sign", "signf", "signi", "seed",
    "rand_from_seed", "deg_to_rad", "rad_to_deg", "linear_to_db",
    "db_to_linear", "cartesian_to_polar", "polar_to_cartesian", "wrapi",
    "wrapf", "nearest_po2", "det", "_error", "dedent",
}

TAB_WIDTH = 4


def _indent(line: str) -> int:
    expanded = line.expandtabs(TAB_WIDTH)
    return len(expanded) - len(expanded.lstrip(" "))


# ---- data model ----------------------------------------------------------------


@dataclass
class Func:
    path: str
    name: str
    line: int  # 1-based def line
    body: str

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
    # .tscn only
    attached_script: str = ""  # res:// path of ext_resource Script
    instances: list[str] = field(default_factory=list)  # instanced scene paths
    connections: list[tuple[str, str]] = field(default_factory=list)  # (signal, method)
    # member `var x: Type` / `var x := Type.new()` at file level (gd only)
    member_types: dict[str, str] = field(default_factory=dict)


class Graph:
    """Whole-checkout structural graph. Build once per process (~seconds)."""

    def __init__(self) -> None:
        self.files: dict[str, FileSym] = {}
        self.class_map: dict[str, str] = {}  # class_name -> res:// path
        # edge[src_key] = set of dst_key; key = "path::func" or "path::SIGNAL:x"
        self.edges: dict[str, set[str]] = defaultdict(set)
        self.reverse: dict[str, set[str]] = defaultdict(set)
        self.roots: set[str] = set()
        self.referenced: set[str] = set()  # string-referenced (alive, not root)
        self.referenced_names: set[str] = set()  # func names called via unresolvable receivers
        self.built_at_lines: int = 0

    # -- parsing ---------------------------------------------------------------

    def _parse_gd(self, path: Path, rel: str) -> FileSym:
        text = nav._read_text(path)
        fs = FileSym(path=rel, ext=".gd")
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            m = CLASSNAME_RE.match(line)
            if m:
                fs.class_name = m.group(1)
                i += 1
                continue
            m = EXTENDS_RE.match(line)
            if m:
                fs.extends = m.group(1)
                i += 1
                continue
            if TOOL_RE.match(line) and i < 4:
                fs.is_tool = True
                i += 1
                continue
            m = SIGNAL_RE.match(line)
            if m:
                fs.signals.add(m.group(1))
                i += 1
                continue
            m = MEMBER_TYPED_RE.match(line)
            if m:
                fs.member_types[m.group(1)] = m.group(2)
                i += 1
                continue
            m = MEMBER_NEW_RE.match(line)
            if m:
                fs.member_types[m.group(1)] = m.group(2)
                i += 1
                continue
            m = FUNC_RE.match(line)
            if m:
                name = m.group(2)
                base = _indent(line)
                # consume multi-line signatures: keep joining until parens
                # balance and the header terminates with ':'
                header = line
                j = i + 1
                while header.count("(") > header.count(")") and j < len(lines):
                    header += "\n" + lines[j]
                    j += 1
                if "\n" in header and not header.rstrip().endswith(":") and j < len(lines):
                    header += "\n" + lines[j]
                    j += 1
                body_lines: list[str] = []
                while j < len(lines):
                    nxt = lines[j]
                    if nxt.strip() == "":
                        body_lines.append(nxt)
                        j += 1
                        continue
                    if _indent(nxt) > base:
                        body_lines.append(nxt)
                        j += 1
                        continue
                    break
                fs.funcs[name] = Func(
                    path=rel, name=name, line=i + 1,
                    body="\n".join(body_lines),
                )
                i = j
                continue
            i += 1
        return fs

    def _parse_tscn(self, path: Path, rel: str) -> FileSym:
        text = nav._read_text(path)
        fs = FileSym(path=rel, ext=".tscn")
        ext_ids: dict[str, str] = {}
        for line in text.splitlines():
            m = re.search(
                r'\[ext_resource type="Script"[^]]*path="([^"]+)"[^]]*id="([^"]+)"',
                line,
            )
            if not m:
                m = re.search(
                    r'\[ext_resource type="Script"[^]]*id="([^"]+)"[^]]*path="([^"]+)"',
                    line,
                )
                if m:
                    ext_ids[m.group(1)] = m.group(2)
                    fs.attached_script = fs.attached_script or m.group(2)
                    continue
            else:
                ext_ids[m.group(2)] = m.group(1)
                fs.attached_script = fs.attached_script or m.group(1)
            m = re.search(r'\[ext_resource type="PackedScene"[^]]*path="([^"]+)"[^]]*id="([^"]+)"', line)
            if m:
                ext_ids[m.group(2)] = m.group(1)
                continue
            m = re.search(r'instance=ExtResource\("([^"]+)"\)', line)
            if m and m.group(1) in ext_ids:
                fs.instances.append(ext_ids[m.group(1)])
            m = re.search(
                r'\[connection signal="(\w+)"[^\]]*to="[^"]*"[^\]]*method="(\w+)"',
                line,
            )
            if m:
                fs.connections.append((m.group(1), m.group(2)))
        # animation method call tracks fire funcs exactly like signal handlers
        for m in ANIM_METHOD_RE.finditer(text):
            fs.connections.append(("anim", m.group(1)))
        return fs

    # -- build -------------------------------------------------------------------

    def build(self) -> "Graph":
        for path in nav.iter_files():
            rel = nav.file_id(path)
            fs = (
                self._parse_gd(path, rel)
                if path.suffix == ".gd"
                else self._parse_tscn(path, rel)
            )
            self.files[rel] = fs
            if fs.class_name:
                self.class_map[fs.class_name] = rel

        # asset scenes sit outside the search index but carry animation method
        # tracks + connections that fire script funcs — parse for wiring only
        for path in (nav.ROOT / "assets").rglob("*.tscn") if (nav.ROOT / "assets").is_dir() else ():
            rel = nav.file_id(path)
            if rel not in self.files:
                self.files[rel] = self._parse_tscn(path, rel)

        for rel, fs in self.files.items():
            if fs.ext != ".gd":
                continue
            for fn in fs.funcs.values():
                self._scan_body(fs, fn)

        self._wire_tscn()
        self._find_roots()
        self._reachable()
        return self

    def _scan_body(self, fs: FileSym, fn: Func) -> None:
        src_key = fn.key
        # first-order type inference: member vars + typed params/locals in this body
        var_types = dict(fs.member_types)
        for pm in PARAM_TYPED_RE.finditer(fn.body):
            var_types[pm.group(1)] = pm.group(2)
        for m in QUALIFIED_CALL_RE.finditer(fn.body):
            head, fname = m.group(1), m.group(2)
            cls = head if head in self.class_map else var_types.get(head)
            if cls and cls in self.class_map:
                dst = self.class_map[cls]
                if fname in self.files[dst].funcs:
                    self._edge(src_key, f"{dst}::{fname}")
            else:
                # receiver type unknown (factory returns, variants) — the call may
                # dispatch to any same-named func; mark name alive, no edge
                self.referenced_names.add(fname)
        for m in AS_CAST_CALL_RE.finditer(fn.body):
            cls, fname = m.group(1), m.group(2)
            if cls in self.class_map:
                dst = self.class_map[cls]
                if fname in self.files[dst].funcs:
                    self._edge(src_key, f"{dst}::{fname}")
            else:
                self.referenced_names.add(fname)
        for m in BARE_CALL_RE.finditer(fn.body):
            name = m.group(1)
            if name in NON_CALLS:
                continue
            if name in fs.funcs:
                self._edge(src_key, f"{fs.path}::{name}")
        # signal emits -> signal nodes; connect/Callable string refs -> handlers
        for m in EMIT_RE.finditer(fn.body):
            sig = m.group(1) or m.group(2)
            if sig in fs.signals:
                self._edge(src_key, f"{fs.path}::SIGNAL:{sig}")
        if CONNECT_RE.search(fn.body):
            for m in STRING_NAME_RE.finditer(fn.body):
                ref = m.group(1)
                if ref in fs.funcs:
                    self._edge(src_key, f"{fs.path}::{ref}")
                    self.referenced.add(f"{fs.path}::{ref}")
                # cross-file: _on_* handlers commonly target other scripts
                elif ref.startswith("_on_"):
                    self.referenced.add(f"*::{ref}")

    def _edge(self, src: str, dst: str) -> None:
        if src == dst:
            return
        self.edges[src].add(dst)
        self.reverse[dst].add(src)

    def _wire_tscn(self) -> None:
        for rel, fs in self.files.items():
            if fs.ext != ".tscn":
                continue
            script_rel = self._res_to_rel(fs.attached_script)
            if not script_rel or script_rel not in self.files:
                continue
            for _, handler in fs.connections:
                if handler in self.files[script_rel].funcs:
                    key = f"{script_rel}::{handler}"
                    self.roots.add(key)
                    self._edge(f"{rel}::tscn", key)
            for inst in fs.instances:
                inst_rel = self._res_to_rel(inst)
                if inst_rel and inst_rel in self.files:
                    self._edge(f"{rel}::tscn", f"{inst_rel}::tscn")

    def _res_to_rel(self, res_path: str) -> str:
        if not res_path:
            return ""
        return res_path.removeprefix("res://")

    def _find_roots(self) -> None:
        for rel, fs in self.files.items():
            if fs.ext != ".gd":
                continue
            in_tests = rel.startswith("tests/")
            for name, fn in fs.funcs.items():
                key = fn.key
                if name in VIRTUALS or name in GUT_ROOTS:
                    self.roots.add(key)
                elif name in ADDON_VIRTUALS.get(fs.extends.lower(), ()):
                    self.roots.add(key)
                elif in_tests and name.startswith("test_"):
                    self.roots.add(key)
        # autoloads
        pg = nav.ROOT / "project.godot"
        if pg.is_file():
            text = nav._read_text(pg)
            in_auto = False
            for line in text.splitlines():
                if line.strip().startswith("[autoload]"):
                    in_auto = True
                    continue
                if line.strip().startswith("["):
                    in_auto = False
                if in_auto:
                    m = re.search(r'res://([\w/.-]+\.gd)', line)
                    if m and m.group(1) in self.files:
                        for fn in self.files[m.group(1)].funcs.values():
                            self.roots.add(fn.key)

    def _reachable(self) -> None:
        seen: set[str] = set(self.roots) | self.referenced
        queue = deque(self.roots)
        while queue:
            cur = queue.popleft()
            for nxt in self.edges.get(cur, ()):  # forward edges
                if nxt not in seen and not nxt.endswith("::tscn"):
                    seen.add(nxt)
                    queue.append(nxt)
        self.reachable = seen

    # -- queries -------------------------------------------------------------------

    def dead_code(self, limit: int = 60) -> dict[str, object]:
        dead = []
        dynamic_hint = re.compile(r"\.call\(|Callable\(|has_method\(|\.connect\(")
        for rel, fs in self.files.items():
            if fs.ext != ".gd":
                continue
            file_is_dynamic = bool(dynamic_hint.search("\n".join(fs.funcs[f].body for f in fs.funcs))) if fs.funcs else False
            for name, fn in fs.funcs.items():
                if fn.key in self.reachable:
                    continue
                # wildcard handler refs (_on_x from any file) keep it alive
                if any(name == r.split("::")[-1] for r in self.referenced if r.startswith("*::")):
                    continue
                if name in self.referenced_names:
                    continue
                tier = "review" if file_is_dynamic else "likely"
                # functions on classes extending bases we cannot resolve (engine
                # natives not in VIRTUALS, C++ addons) may be dispatched natively
                if (
                    tier == "likely"
                    and fs.extends
                    and fs.extends not in self.class_map
                    and name.startswith("_")
                    and name not in VIRTUALS
                ):
                    tier = "review"
                dead.append({"path": rel, "func": name, "line": fn.line, "tier": tier})
        dead.sort(key=lambda d: (d["tier"], d["path"], d["line"]))
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
            for key in frontier:
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                callers = sorted(self.reverse.get(key, ()))
                callees = sorted(self.edges.get(key, ()))
                out_lines.append(self._fmt_node(key, callers, callees))
                nxt |= {c for c in callees + callers if not c.endswith("::tscn")}
            frontier = nxt - seen_keys
            if not frontier:
                break
        return "\n".join(out_lines[:limit])

    def _resolve(self, symbol: str) -> list[str]:
        hits = []
        for rel, fs in self.files.items():
            if symbol in fs.funcs:
                hits.append(f"{rel}::{symbol}")
            if fs.class_name == symbol:
                hits.extend(f"{rel}::{f}" for f in fs.funcs)
        if not hits:
            for rel, fs in self.files.items():
                for name in fs.funcs:
                    if symbol.lower() in name.lower():
                        hits.append(f"{rel}::{name}")
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
        groups: dict[str, list[str]] = defaultdict(list)
        for rel, fs in self.files.items():
            if fs.ext != ".gd":
                continue
            for name, fn in fs.funcs.items():
                norm = _normalize_body(fn.body)
                if len(norm.splitlines()) < 3:
                    continue  # trivial
                groups[hashlib.sha1(norm.encode()).hexdigest()].append(fn.key)
        dups = [
            {"hash": h[:8], "members": sorted(v)}
            for h, v in groups.items()
            if len(v) > 1
        ]
        dups.sort(key=lambda d: -len(d["members"]))
        return dups[:limit]


def _normalize_body(body: str) -> str:
    out = []
    for line in body.splitlines():
        s = line.split("#", 1)[0].rstrip()
        if not s.strip():
            continue
        out.append("  " + s.strip())  # unify indent
    return "\n".join(out)


# -- function-level vector index (chroma "swmg-fns") ---------------------------


def _fn_collection() -> "chromadb.Collection":
    import chromadb

    client = chromadb.PersistentClient(path=str(nav.DB_DIR))
    return client.get_or_create_collection(
        name="swmg-fns",
        metadata={"hnsw:space": "cosine"},
    )


def _all_filesyms() -> dict[str, FileSym]:
    return get_graph().files


def sync_functions(changed: list[str], deleted: list[str]) -> dict[str, int]:
    """Re-embed functions of changed files, purge deleted files' functions."""
    col = _fn_collection()
    stale = sorted(set(changed) | set(deleted))
    if stale and col.count():
        for p in stale:
            col.delete(where={"path": p})
    if col.count() == 0 and not changed:
        # first build: index every .gd function
        changed = sorted(
            rel for rel, fs in _all_filesyms().items() if fs.ext == ".gd"
        )
    parser = Graph()
    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict[str, object]] = []
    for rel in changed:
        path = nav.ROOT / rel
        if not path.is_file() or path.suffix != ".gd":
            continue
        fs = parser._parse_gd(path, rel)
        for name, fn in fs.funcs.items():
            ids.append(f"{rel}::{name}")
            docs.append(f"{rel} :: func {name}\n{fn.body[:6000]}")
            metas.append(
                {"path": rel, "name": name, "class_name": fs.class_name,
                 "line": fn.line}
            )
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
    return {"fns_upserted": added, "purged_paths": len(stale)}


def find_functions(query: str, n: int = 6) -> list[dict[str, object]]:
    """Semantic search over individual functions (vector index)."""
    col = _fn_collection()
    count = col.count()
    if count == 0:
        return []
    vector = nav.embed([query])[0]
    got = col.query(
        query_embeddings=[vector],
        n_results=min(n, count),
        include=["metadatas", "distances"],
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
    return out


# -- module-level singleton ---------------------------------------------------

_graph: Graph | None = None


def get_graph(rebuild: bool = False) -> Graph:
    global _graph
    if _graph is None or rebuild:
        _graph = Graph().build()
    return _graph
